"""Database-first collection commands exposed by package CLI."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
import getpass
import hashlib
import json
import os
import secrets
import sys
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

import requests

from .areas import (
    AreaSelectionError, find_overlaps, load_area_file, select_catalog_areas,
    validate_area_ids,
)
from .collector import Collector
from .discovery import discover_skill_candidates, export_skill_candidates, import_skill_candidates
from .extractors.offline import extract as run_extraction
from .export import export_marts, export_query_hits, export_roles, export_skills, export_vacancies
from .config import cli_defaults, load_config
from .labeling import export_labeling, import_labeling
from .oauth import (
    authorization_url, pkce_pair, read_token_file, request_token, token_metadata,
    wait_for_authorization_code, write_token_file,
)
from .pilot import create_pilot, export_pilot_labels, pilot_report
from .query_specs import QuerySpec, load_query_specs
from .skill_dictionary import load_skill_dictionary
from .sources.api import HHApiSource
from .sources.html import HHHtmlSource
from .stats import vacancy_stats
from .storage import Database

DEFAULT_DATABASE = "hh_mobilization.sqlite3"
DEFAULT_USER_AGENT = "hh-skill-parser/1.0 (contact@example.invalid)"


def parse_iso_date(value: str) -> str:
    """Validate stable date-only backfill boundary accepted by HH API."""
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from error
    return value


def nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected non-negative integer") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("expected non-negative integer")
    return parsed


def positive_int(value: str) -> int:
    parsed = nonnegative_int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("expected positive integer")
    return parsed


def validate_date_range(date_from: str | None, date_to: str | None) -> None:
    """Require finite, ordered date window when either bound is requested."""
    if bool(date_from) != bool(date_to):
        raise ValueError("--date-from and --date-to must be specified together")
    if date_from and date_from > date_to:
        raise ValueError("--date-from must not be after --date-to")


def collection_scope(
    *, queries: list[QuerySpec], area_ids: list[str], catalog_version_id: int | None,
    settings: argparse.Namespace,
) -> dict[str, Any]:
    """Return only fields whose changes require an independent watermark."""
    return {
        "query_specs": freeze_query_specs(queries), "area_ids": area_ids,
        "catalog_version_id": catalog_version_id, "source": settings.source,
        "host": settings.host, "locale": settings.locale, "max_pages": settings.max_pages,
        "date_slice_min_days": settings.date_slice_min_days,
        "date_overlap_days": settings.date_overlap_days,
    }


def scope_hash(scope: dict[str, Any]) -> str:
    """Hash deterministic compatible-scope identity without collection window."""
    encoded = json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resolve_collection_window(
    settings: argparse.Namespace, database: Database, watermark_scope_hash: str,
) -> tuple[str, str, str | None]:
    """Resolve finite effective window before run creation or network activity."""
    if settings.collection_mode == "full":
        validate_date_range(settings.date_from, settings.date_to)
        if not settings.date_from:
            raise ValueError("full collection requires --date-from and --date-to")
        return settings.date_from, settings.date_to, None
    if settings.incremental_overlap_days < 0:
        raise ValueError("incremental overlap must be non-negative")
    watermark = database.collection_watermark(watermark_scope_hash)
    end = settings.date_to or date.today().isoformat()
    if watermark:
        start = (date.fromisoformat(watermark) - timedelta(days=settings.incremental_overlap_days)).isoformat()
    else:
        start = settings.date_from or end
    if start > end:
        raise ValueError("effective incremental window starts after --date-to")
    return start, end, watermark


def load_query_file(path: str | Path):
    """Load nonempty, non-comment HH expressions without altering syntax."""
    return load_query_specs(path)


def freeze_query_specs(queries: list[QuerySpec]) -> list[dict[str, Any]]:
    """Serialize complete query scope into run config for reproducible resume."""
    return [
        {
            "id": query.id, "expression": query.expression, "group": query.group,
            "purpose": query.purpose, "version": query.version,
            "search_fields": list(query.search_fields),
        }
        for query in queries
    ]


def load_frozen_query_specs(config: dict[str, Any], fallback_path: str | Path) -> list[QuerySpec]:
    """Use immutable run query scope; retain compatibility with pre-freeze runs."""
    rows = config.get("query_specs")
    if rows is None:
        return load_query_file(fallback_path)
    if not isinstance(rows, list) or not rows:
        raise ValueError("run has invalid frozen query specifications")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("run has invalid frozen query specifications")
        try:
            query = QuerySpec(
                id=row["id"], expression=row["expression"], group=row.get("group"),
                purpose=row.get("purpose"), version=row["version"],
                search_fields=tuple(row.get("search_fields", [])),
            )
        except (KeyError, TypeError) as error:
            raise ValueError("run has invalid frozen query specifications") from error
        if not isinstance(query.id, str) or not query.id or not isinstance(query.expression, str) or not query.expression:
            raise ValueError("run has invalid frozen query specifications")
        result.append(query)
    return result


def add_transport_arguments(parser: argparse.ArgumentParser, *, html_source: bool = True) -> None:
    """Add API connection options shared by collect and resume."""
    parser.add_argument("--source", choices=["api", "html"] if html_source else ["api"], default="api")
    parser.add_argument("--access-token", default=os.environ.get("HH_ACCESS_TOKEN"))
    parser.add_argument("--token-file", help="OAuth token JSON written by auth login; never printed")
    parser.add_argument("--user-agent", default=os.environ.get("HH_USER_AGENT", DEFAULT_USER_AGENT))
    parser.add_argument("--host", default="hh.ru")
    parser.add_argument("--locale", default="RU")
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument("--max-retries", type=nonnegative_int, default=3)
    parser.add_argument("--retry-backoff", type=float, default=1.0)


def add_database_arguments(parser: argparse.ArgumentParser) -> None:
    """Add portable SQLite connection settings shared by DB-backed commands."""
    parser.add_argument("--database", default=os.environ.get("HH_DATABASE", DEFAULT_DATABASE))
    parser.add_argument("--busy-timeout-ms", type=nonnegative_int, default=5_000)
    parser.add_argument("--no-wal", dest="wal", action="store_false", default=True)


def database_for(settings: argparse.Namespace) -> Database:
    """Open SQLite using CLI/config settings; legacy direct callers keep defaults."""
    return Database(
        settings.database,
        busy_timeout_ms=getattr(settings, "busy_timeout_ms", 5_000),
        wal=getattr(settings, "wal", True),
    )


def build_parser() -> argparse.ArgumentParser:
    """Build command parser without importing legacy CLI options."""
    parser = argparse.ArgumentParser(prog="hh-skill-parser")
    commands = parser.add_subparsers(dest="command", required=True)

    collect = commands.add_parser("collect", help="collect HH vacancies into SQLite")
    collect.add_argument("--config")
    add_database_arguments(collect)
    collect.add_argument("--queries-file", default="query_specs.toml")
    collect.add_argument("--area", action="append", default=[])
    collect.add_argument("--areas-file")
    collect.add_argument("--areas-source", choices=["explicit", "catalog"], default="explicit")
    collect.add_argument("--area-root", default="113")
    collect.add_argument("--area-level", choices=["root", "children", "leaf"], default="root")
    collect.add_argument("--catalog-version", type=int)
    collect.add_argument("--allow-area-overlap", action="store_true")
    collect.add_argument(
        "--collection-mode", choices=["incremental", "full"], default="incremental",
        help="incremental continues compatible watermark; full requires explicit date range",
    )
    collect.add_argument("--max-pages", type=int, default=20)
    collect.add_argument(
        "--write-batch-size", type=positive_int, default=1,
        help="transactional snapshot writes per batch; pages/hits remain durable first",
    )
    collect.add_argument("--date-from", type=parse_iso_date, help="inclusive initial/full window start (YYYY-MM-DD)")
    collect.add_argument("--date-to", type=parse_iso_date, help="inclusive window end; incremental defaults to today")
    collect.add_argument("--date-slice-min-days", type=int, default=1)
    collect.add_argument("--date-overlap-days", type=int, default=1)
    collect.add_argument(
        "--incremental-overlap-days", type=nonnegative_int, default=1,
        help="days to rescan before compatible incremental watermark",
    )
    collect.add_argument(
        "--store-raw", action="store_true",
        help="store sanitized source payloads; disabled by default to minimize retained source data",
    )
    add_transport_arguments(collect)

    resume = commands.add_parser("resume", help="resume one degraded/interrupted SQLite run")
    resume.add_argument("--config")
    add_database_arguments(resume)
    resume.add_argument("--run-id", required=True, type=int)
    resume.add_argument("--queries-file", default="query_specs.toml")
    resume.add_argument("--max-pages", type=int, default=20)
    add_transport_arguments(resume)

    retry = commands.add_parser("retry", help="retry unresolved search/card units in one run")
    retry.add_argument("--config")
    add_database_arguments(retry)
    retry.add_argument("--run-id", required=True, type=int)
    retry.add_argument("--max-attempts", type=nonnegative_int, default=3)
    add_transport_arguments(retry)

    coverage = commands.add_parser("coverage", help="report persisted collection coverage; no network")
    coverage.add_argument("--config")
    add_database_arguments(coverage)
    coverage.add_argument("--run-id", required=True, type=int)

    runs = commands.add_parser("runs", help="list persisted collection runs; no network")
    runs.add_argument("--config")
    add_database_arguments(runs)
    runs.add_argument("--status", choices=["running", "completed", "degraded", "failed", "cancelled"])
    runs.add_argument("--limit", type=positive_int, default=20)

    extract = commands.add_parser("extract", help="derive offline data from stored snapshots")
    extract.add_argument("--config")
    add_database_arguments(extract)
    extract.add_argument("--run-id", action="append", type=int, default=[])
    extract.add_argument("--area", action="append", default=[])
    extract.add_argument("--source")
    extract.add_argument("--date-from", type=parse_iso_date)
    extract.add_argument("--date-to", type=parse_iso_date)
    extract.add_argument("--snapshot", choices=["latest", "all"], default="latest")
    extract.add_argument("--skills-file", default="skills_whitelist.txt")
    extract_commands = extract.add_subparsers(dest="extract_command", required=True)
    for name in ("relevance", "features", "skills"):
        extract_commands.add_parser(name, help=f"rebuild {name} from SQLite snapshots")

    export = commands.add_parser("export", help="export SQLite data")
    export.add_argument("--config")
    add_database_arguments(export)
    export_commands = export.add_subparsers(dest="export_command", required=True)
    labeling_export = export_commands.add_parser("labeling", help="export relevance-labeling CSV")
    labeling_export.add_argument("--output", required=True)
    labeling_export.add_argument("--sample-size", type=nonnegative_int, default=0)
    labeling_export.add_argument("--sample-seed", default="0")
    pilot = commands.add_parser("pilot", help="create and measure persistent relevance-labeling pilots")
    pilot.add_argument("--config")
    add_database_arguments(pilot)
    pilot_commands = pilot.add_subparsers(dest="pilot_command", required=True)
    pilot_create = pilot_commands.add_parser("create", help="write deterministic pilot CSV and persist selection")
    pilot_create.add_argument("--batch-id", required=True)
    pilot_create.add_argument("--output", required=True)
    pilot_create.add_argument("--sample-size", type=positive_int, default=100)
    pilot_create.add_argument("--sample-seed", default="0")
    pilot_create.add_argument("--snapshot", choices=["latest", "all"], default="latest")
    pilot_create.add_argument("--run-id", action="append", type=int, default=[])
    pilot_create.add_argument("--area", action="append", default=[])
    pilot_create.add_argument("--date-from", type=parse_iso_date)
    pilot_create.add_argument("--date-to", type=parse_iso_date)
    pilot_report_command = pilot_commands.add_parser("report", help="calculate offline query-family pilot metrics")
    pilot_report_command.add_argument("--batch-id", required=True)
    pilot_report_command.add_argument("--output", required=True)
    pilot_report_command.add_argument("--min-per-stratum", type=positive_int, default=5)
    vacancies_export = export_commands.add_parser("vacancies", help="export vacancy snapshots CSV")
    vacancies_export.add_argument("--output", required=True)
    vacancies_export.add_argument("--snapshot", choices=["latest", "all"], default="latest")
    vacancies_export.add_argument("--run-id", action="append", type=int, default=[])
    vacancies_export.add_argument("--area", action="append", default=[])
    vacancies_export.add_argument("--relevance", choices=["relevant", "borderline", "irrelevant", "unknown"])
    vacancies_export.add_argument("--query-family")
    vacancies_export.add_argument("--date-from", type=parse_iso_date)
    vacancies_export.add_argument("--date-to", type=parse_iso_date)
    skills_export = export_commands.add_parser("skills", help="export normalized vacancy-skill CSV")
    skills_export.add_argument("--output", required=True)
    roles_export = export_commands.add_parser("roles", help="export normalized vacancy-role CSV")
    roles_export.add_argument("--output", required=True)
    query_hits_export = export_commands.add_parser("query-hits", help="export normalized query-hit CSV")
    query_hits_export.add_argument("--output", required=True)
    marts_export = export_commands.add_parser("marts", help="export reproducible DA mart bundle from SQLite")
    marts_export.add_argument("--output-dir", required=True)
    marts_export.add_argument("--snapshot", choices=["latest", "all"], default="latest")
    marts_export.add_argument("--run-id", action="append", type=int, default=[])
    marts_export.add_argument("--area", action="append", default=[])
    marts_export.add_argument("--relevance", choices=["relevant", "borderline", "irrelevant", "unknown"])
    marts_export.add_argument("--query-family")
    marts_export.add_argument("--date-from", type=parse_iso_date)
    marts_export.add_argument("--date-to", type=parse_iso_date)
    marts_export.add_argument("--parquet", action="store_true", help="also write Parquet; requires pyarrow")

    stats = commands.add_parser("stats", help="calculate DB-only vacancy statistics")
    stats.add_argument("--config")
    add_database_arguments(stats)
    stats.add_argument("--snapshot", choices=["latest", "all"], default="latest")
    stats.add_argument("--run-id", action="append", type=int, default=[])
    stats.add_argument("--area", action="append", default=[])
    stats.add_argument("--relevance", choices=["relevant", "borderline", "irrelevant", "unknown"])
    stats.add_argument("--query-family")
    stats.add_argument("--date-from", type=parse_iso_date)
    stats.add_argument("--date-to", type=parse_iso_date)

    maintenance = commands.add_parser("maintenance", help="explicit database maintenance")
    add_database_arguments(maintenance)
    maintenance_commands = maintenance.add_subparsers(dest="maintenance_command", required=True)
    purge_raw = maintenance_commands.add_parser("purge-raw", help="preview or purge old raw payload BLOBs only")
    purge_raw.add_argument("--before", required=True, type=parse_iso_date)
    purge_raw.add_argument("--execute", action="store_true", help="perform purge; default is preview")
    purge_raw.add_argument("--confirm", help="required exact value: PURGE_RAW_PAYLOADS")
    inspect_raw = maintenance_commands.add_parser("inspect-raw", help="decompress one sanitized raw payload to a file")
    inspect_raw.add_argument("--snapshot-id", required=True, type=positive_int)
    inspect_raw.add_argument("--output", required=True, help="destination file; payload is never printed")

    db = commands.add_parser("db", help="database lifecycle commands")
    add_database_arguments(db)
    db_commands = db.add_subparsers(dest="db_command", required=True)
    db_commands.add_parser("migrate", help="apply packaged SQLite migrations")
    db_commands.add_parser("check", help="run SQLite integrity check")
    db_commands.add_parser("checkpoint", help="checkpoint SQLite WAL without deleting data")
    backup = db_commands.add_parser("backup", help="checkpoint WAL and create verified SQLite backup")
    backup.add_argument("--output", required=True, help="new backup SQLite path; must not exist")
    restore = db_commands.add_parser("restore", help="restore verified backup into separate SQLite file")
    restore.add_argument("--input", required=True, help="existing backup SQLite path")
    restore.add_argument("--output", required=True, help="restored SQLite path")
    restore.add_argument("--overwrite", action="store_true", help="replace existing restore output")
    reset = db_commands.add_parser("reset", help="preview or clear all collected/derived data")
    reset.add_argument("--yes", action="store_true", help="permanently clear data; default is preview")

    smoke = commands.add_parser("smoke", help="explicit opt-in HH API connectivity smoke")
    smoke_commands = smoke.add_subparsers(dest="smoke_command", required=True)
    live_smoke = smoke_commands.add_parser("live", help="one small HH API search; never writes SQLite")
    live_smoke.add_argument("--confirm-live", action="store_true", help="required: permit one real HH API request")
    live_smoke.add_argument("--query", default="воинский учет")
    live_smoke.add_argument("--area", default="1")
    live_smoke.add_argument("--request-timeout", type=float, default=5.0)
    live_smoke.add_argument("--access-token", default=os.environ.get("HH_ACCESS_TOKEN"))
    live_smoke.add_argument("--token-file", help="OAuth token JSON written by auth login")
    live_smoke.add_argument("--user-agent", default=os.environ.get("HH_USER_AGENT", DEFAULT_USER_AGENT))
    live_smoke.add_argument("--host", default="hh.ru")
    live_smoke.add_argument("--locale", default="RU")

    auth = commands.add_parser("auth", help="obtain and refresh HH OAuth tokens locally")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    auth_login = auth_commands.add_parser("login", help="open browser OAuth login and save token file")
    auth_login.add_argument("--client-id", required=True)
    auth_login.add_argument("--client-secret", default=os.environ.get("HH_CLIENT_SECRET"))
    auth_login.add_argument("--redirect-uri", default="http://127.0.0.1:8765/callback")
    auth_login.add_argument("--token-file", default=".hh_oauth_token.json")
    auth_login.add_argument("--callback-timeout", type=positive_int, default=300)
    auth_login.add_argument("--no-browser", action="store_true", help="print authorization URL; keep callback server waiting")
    auth_login.add_argument("--overwrite", action="store_true", help="replace existing token file")
    auth_login.add_argument("--user-agent", default=os.environ.get("HH_USER_AGENT", DEFAULT_USER_AGENT))
    auth_refresh = auth_commands.add_parser("refresh", help="refresh token file after access token expiry")
    auth_refresh.add_argument("--client-id", required=True)
    auth_refresh.add_argument("--client-secret", default=os.environ.get("HH_CLIENT_SECRET"))
    auth_refresh.add_argument("--token-file", default=".hh_oauth_token.json")
    auth_refresh.add_argument("--user-agent", default=os.environ.get("HH_USER_AGENT", DEFAULT_USER_AGENT))

    discover = commands.add_parser("discover", help="mine skill candidates from stored corpus")
    add_database_arguments(discover)
    discover_commands = discover.add_subparsers(dest="discover_command", required=True)
    discover_skills = discover_commands.add_parser("skills", help="export unknown skill candidates CSV")
    discover_skills.add_argument("--output", required=True)
    discover_skills.add_argument("--skills-file", default="skills_whitelist.txt")
    discover_skills.add_argument("--min-document-frequency", type=nonnegative_int, default=2)
    discover_skills.add_argument("--batch-id", help="persist immutable evidence batch before manual review")

    labeling_import = commands.add_parser("import", help="import SQLite data")
    labeling_import.add_argument("--config")
    add_database_arguments(labeling_import)
    import_commands = labeling_import.add_subparsers(dest="import_command", required=True)
    labeling_import_csv = import_commands.add_parser("labeling", help="import reviewed relevance-labeling CSV")
    labeling_import_csv.add_argument("path")
    candidates_import = import_commands.add_parser("skill-candidates", help="apply reviewed skill candidates to new dictionary")
    candidates_import.add_argument("path")
    candidates_import.add_argument("--skills-file", required=True)
    candidates_import.add_argument("--output", required=True)
    candidates_import.add_argument("--batch-id", help="persist decisions against prior discover batch")

    areas = commands.add_parser("areas", help="manage versioned HH area catalog")
    add_database_arguments(areas)
    areas_commands = areas.add_subparsers(dest="areas_command", required=True)
    areas_sync = areas_commands.add_parser("sync", help="fetch and store official HH /areas catalog")
    areas_sync.add_argument("--root", default="113")
    add_transport_arguments(areas_sync, html_source=False)
    areas_list = areas_commands.add_parser("list", help="list a frozen catalog selection")
    areas_list.add_argument("--catalog-version", type=int)
    areas_list.add_argument("--root", default="113")
    areas_list.add_argument("--level", choices=["root", "children", "leaf"], default="root")
    areas_validate = areas_commands.add_parser("validate", help="validate explicit area IDs against catalog")
    areas_validate.add_argument("--catalog-version", type=int)
    areas_validate.add_argument("--area", action="append", default=[])
    areas_validate.add_argument("--areas-file")
    areas_validate.add_argument("--allow-area-overlap", action="store_true")
    return parser


def apply_defaults(parser: argparse.ArgumentParser, defaults: dict[str, Any]) -> None:
    """Override argparse action defaults, including command-specific options."""
    for action in parser._actions:
        if action.dest in defaults:
            action.default = defaults[action.dest]
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                apply_defaults(child, defaults)


def resolve_collect_areas(settings: argparse.Namespace, database: Database) -> tuple[list[str], int | None, str]:
    """Resolve one immutable selection before network collection starts."""
    explicit = list(settings.area)
    if settings.areas_file:
        if explicit:
            raise AreaSelectionError("--area and --areas-file cannot be combined")
        explicit = load_area_file(settings.areas_file)
    if explicit:
        if settings.areas_source != "explicit":
            raise AreaSelectionError("explicit area IDs require --areas-source explicit")
        catalog_version, catalog = database.load_area_catalog(settings.catalog_version)
        validate_area_ids(explicit, catalog)
        overlaps = find_overlaps(explicit, catalog)
        if overlaps and not settings.allow_area_overlap:
            pairs = ", ".join(f"{parent}/{child}" for parent, child in overlaps)
            raise AreaSelectionError(f"overlapping selected areas: {pairs}; pass --allow-area-overlap")
        return explicit, catalog_version, "cli" if settings.area else "file"
    if settings.areas_source != "catalog":
        raise AreaSelectionError("specify --area, --areas-file, or --areas-source catalog")
    catalog_version, catalog = database.load_area_catalog(settings.catalog_version)
    selected = select_catalog_areas(catalog, settings.area_root, settings.area_level)
    overlaps = find_overlaps(selected, catalog)
    if overlaps and not settings.allow_area_overlap:
        pairs = ", ".join(f"{parent}/{child}" for parent, child in overlaps)
        raise AreaSelectionError(f"overlapping selected areas: {pairs}; pass --allow-area-overlap")
    return selected, catalog_version, "catalog"


def make_source(settings: argparse.Namespace) -> Any:
    """Create requested transport. Source choice is explicit; no fallback policy exists."""
    access_token = getattr(settings, "access_token", None)
    token_file = getattr(settings, "token_file", None)
    if token_file:
        if access_token:
            raise ValueError("--access-token and --token-file cannot be combined")
        access_token = read_token_file(token_file)["access_token"]
    options = dict(
        user_agent=settings.user_agent, timeout=settings.request_timeout,
        access_token=access_token or None,
        host=settings.host, locale=settings.locale,
        max_retries=getattr(settings, "max_retries", 3),
        retry_backoff=getattr(settings, "retry_backoff", 1.0),
    )
    if settings.source == "html":
        return HHHtmlSource(**options)
    return HHApiSource(**options)


def run_collect(
    settings: argparse.Namespace, *, source_factory: Callable[[argparse.Namespace], Any] = make_source,
) -> tuple[int, dict[str, int]]:
    """Start finite DB-backed collection and return run ID with durable counters."""
    database = database_for(settings)
    database.migrate()
    database.require_compatible_schema()
    if settings.date_slice_min_days < 1 or settings.date_overlap_days < 0:
        raise ValueError("date slice minimum must be positive; overlap must be non-negative")
    area_ids, catalog_version_id, selection_source = resolve_collect_areas(settings, database)
    queries = load_query_file(settings.queries_file)
    watermark_scope = collection_scope(
        queries=queries, area_ids=area_ids, catalog_version_id=catalog_version_id, settings=settings,
    )
    watermark_scope_hash = scope_hash(watermark_scope)
    effective_date_from, effective_date_to, watermark_before = resolve_collection_window(
        settings, database, watermark_scope_hash,
    )
    source = source_factory(settings)
    config = {
        "queries_file": str(settings.queries_file), "query_specs": freeze_query_specs(queries), "area_ids": area_ids,
        "catalog_version_id": catalog_version_id, "selection_source": selection_source,
        "source": settings.source, "request_timeout": settings.request_timeout,
        "host": settings.host, "locale": settings.locale,
        "database_wal": settings.wal, "database_busy_timeout_ms": settings.busy_timeout_ms,
        "max_retries": settings.max_retries, "retry_backoff": settings.retry_backoff,
        "collection_mode": settings.collection_mode, "max_pages": settings.max_pages,
        "write_batch_size": settings.write_batch_size, "store_raw": settings.store_raw,
        "date_from": effective_date_from, "date_to": effective_date_to,
        "date_slice_min_days": settings.date_slice_min_days,
        "date_overlap_days": settings.date_overlap_days,
        "incremental_overlap_days": settings.incremental_overlap_days,
        "effective_mode": settings.collection_mode,
        "effective_date_from": effective_date_from, "effective_date_to": effective_date_to,
        "watermark_before": watermark_before, "watermark_scope": watermark_scope,
        "watermark_scope_hash": watermark_scope_hash,
    }
    collector = Collector(
        database, transport=source, write_batch_size=settings.write_batch_size,
        store_raw=settings.store_raw,
    )
    run_id = collector.start(
        config, area_ids, catalog_version_id=catalog_version_id,
        selection_source=selection_source, source_policy=settings.source,
    )
    counters = collector.collect_sliced(
        run_id, area_ids, queries, search_page=source.search_page,
        detail=source.detail, max_pages=settings.max_pages,
        date_from=effective_date_from, date_to=effective_date_to,
        min_window_days=settings.date_slice_min_days,
        overlap_days=settings.date_overlap_days,
    )
    return run_id, counters


def run_resume(
    settings: argparse.Namespace, *, source_factory: Callable[[argparse.Namespace], Any] = make_source,
) -> dict[str, int]:
    """Resume DB work without rediscovering areas or losing previous pages/cards."""
    database = database_for(settings)
    database.migrate()
    database.require_compatible_schema()
    config = database.run_config(settings.run_id)
    source_options = {
        **vars(settings), "host": config.get("host", settings.host),
        "locale": config.get("locale", settings.locale),
        "source": config.get("source", settings.source),
    }
    source_settings = argparse.Namespace(**source_options)
    source = source_factory(source_settings)
    if config.get("effective_date_from", config.get("date_from")):
        return Collector(
            database, transport=source, write_batch_size=config.get("write_batch_size", 1),
            store_raw=config.get("store_raw", False),
        ).resume_sliced(
            settings.run_id, load_frozen_query_specs(config, settings.queries_file),
            search_page=source.search_page, detail=source.detail,
            max_pages=config.get("max_pages", settings.max_pages),
            date_from=config.get("effective_date_from", config["date_from"]),
            date_to=config.get("effective_date_to", config["date_to"]),
            min_window_days=config.get("date_slice_min_days", 1),
            overlap_days=config.get("date_overlap_days", 1),
        )
    return Collector(
        database, transport=source, write_batch_size=config.get("write_batch_size", 1),
        store_raw=config.get("store_raw", False),
    ).resume_paginated(
        settings.run_id, load_frozen_query_specs(config, settings.queries_file),
        search_page=source.search_page, detail=source.detail,
        max_pages=config.get("max_pages", settings.max_pages),
    )


def run_retry(settings: argparse.Namespace, *, source_factory: Callable[[argparse.Namespace], Any] = make_source) -> dict[str, int]:
    database = database_for(settings)
    database.migrate()
    if settings.max_attempts < 1:
        raise ValueError("max attempts must be positive")
    config = database.run_config(settings.run_id)
    source = source_factory(argparse.Namespace(**{
        **vars(settings), "host": config.get("host", settings.host),
        "locale": config.get("locale", settings.locale), "source": config.get("source", settings.source),
    }))
    return Collector(
        database, transport=source, write_batch_size=config.get("write_batch_size", 1),
        store_raw=config.get("store_raw", False),
    ).retry_unresolved(
        settings.run_id, search_page=source.search_page, detail=source.detail, max_attempts=settings.max_attempts,
    )


def run_coverage(settings: argparse.Namespace) -> list[dict[str, Any]]:
    database = database_for(settings)
    database.migrate()
    database.run_config(settings.run_id)
    return database.coverage_report(settings.run_id)


def run_runs(settings: argparse.Namespace) -> list[dict[str, Any]]:
    """List recent collection runs from SQLite without making network requests."""
    database = database_for(settings)
    database.migrate()
    database.require_compatible_schema()
    return database.list_runs(status=settings.status, limit=settings.limit)


def run_export_labeling(settings: argparse.Namespace) -> int:
    """Write all auto-labeled snapshots as reviewable CSV."""
    database = database_for(settings)
    database.migrate()
    database.require_compatible_schema()
    return export_labeling(
        database, settings.output, sample_size=settings.sample_size, sample_seed=settings.sample_seed,
    )


def run_pilot(settings: argparse.Namespace) -> dict[str, Any]:
    database = database_for(settings)
    database.migrate()
    if settings.pilot_command == "create":
        validate_date_range(settings.date_from, settings.date_to)
        filters = {"run_ids": settings.run_id, "area_ids": settings.area, "date_from": settings.date_from, "date_to": settings.date_to, "snapshot_scope": settings.snapshot}
        count, rows = create_pilot(database, settings.batch_id, sample_size=settings.sample_size, sample_seed=settings.sample_seed, filters=filters)
        export_pilot_labels(rows, settings.output)
        return {"batch_id": settings.batch_id, "rows": count, "output": settings.output}
    report = pilot_report(database, settings.batch_id, min_per_stratum=settings.min_per_stratum)
    Path(settings.output).write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"batch_id": settings.batch_id, "output": settings.output, "rows": report["sample"]["selected"]}


def run_export_vacancies(settings: argparse.Namespace) -> int:
    """Write filtered vacancy CSV without invoking HH or an extractor."""
    validate_date_range(settings.date_from, settings.date_to)
    database = database_for(settings)
    database.migrate()
    database.require_compatible_schema()
    return export_vacancies(
        database, settings.output, snapshot_scope=settings.snapshot,
        run_ids=settings.run_id or None, area_ids=settings.area or None,
        relevance=settings.relevance, query_family=settings.query_family,
        date_from=settings.date_from, date_to=settings.date_to,
    )


def run_export_skills(settings: argparse.Namespace) -> int:
    database = database_for(settings)
    database.migrate()
    database.require_compatible_schema()
    return export_skills(database, settings.output)


def run_export_relation(settings: argparse.Namespace) -> int:
    database = database_for(settings)
    database.migrate()
    database.require_compatible_schema()
    return export_roles(database, settings.output) if settings.export_command == "roles" else export_query_hits(database, settings.output)


def run_export_marts(settings: argparse.Namespace) -> dict[str, Any]:
    validate_date_range(settings.date_from, settings.date_to)
    database = database_for(settings)
    database.migrate()
    database.require_compatible_schema()
    return export_marts(
        database, settings.output_dir, snapshot_scope=settings.snapshot, run_ids=settings.run_id or None,
        area_ids=settings.area or None, relevance=settings.relevance, query_family=settings.query_family,
        date_from=settings.date_from, date_to=settings.date_to, parquet=settings.parquet,
    )


def run_stats(settings: argparse.Namespace) -> dict[str, Any]:
    """Calculate filtered counts from SQLite only."""
    validate_date_range(settings.date_from, settings.date_to)
    database = database_for(settings)
    database.migrate()
    return vacancy_stats(
        database, snapshot_scope=settings.snapshot, run_ids=settings.run_id or None,
        area_ids=settings.area or None, relevance=settings.relevance,
        query_family=settings.query_family, date_from=settings.date_from, date_to=settings.date_to,
    )


def run_maintenance(settings: argparse.Namespace) -> dict[str, Any]:
    """Run explicit, narrowly scoped maintenance action."""
    database = database_for(settings)
    database.migrate()
    if settings.maintenance_command == "inspect-raw":
        payload, content_type = database.read_raw_payload(settings.snapshot_id)
        Path(settings.output).write_bytes(payload)
        return {"snapshot_id": settings.snapshot_id, "output": str(settings.output), "bytes": len(payload), "content_type": content_type}
    if settings.maintenance_command != "purge-raw":
        raise ValueError(f"unsupported maintenance command: {settings.maintenance_command}")
    if not settings.execute:
        return {"dry_run": True, "before": settings.before, **database.raw_payload_purge_summary(settings.before)}
    if settings.confirm != "PURGE_RAW_PAYLOADS":
        raise ValueError("purge requires --execute --confirm PURGE_RAW_PAYLOADS")
    return {"dry_run": False, "before": settings.before, **database.purge_raw_payloads(settings.before)}


def run_db(settings: argparse.Namespace) -> dict[str, Any]:
    """Run schema migration or explicit full data reset."""
    if settings.db_command == "restore":
        return {"input": str(settings.input), "output": str(settings.output), "verified": True,
                **Database.restore_to(settings.input, settings.output, overwrite=settings.overwrite)}
    database = database_for(settings)
    database.migrate()
    if settings.db_command == "migrate":
        return {"migrated": True}
    if settings.db_command == "check":
        result = database.integrity_check()
        return {"ok": result == ["ok"], "result": result}
    if settings.db_command == "checkpoint":
        return database.checkpoint()
    if settings.db_command == "backup":
        return {"output": str(settings.output), "verified": True, **database.backup_to(settings.output)}
    summary = database.reset_data_summary()
    if not settings.yes:
        return {"dry_run": True, "tables": summary}
    return {"dry_run": False, "tables": database.reset_data()}


def run_live_smoke(
    settings: argparse.Namespace, *, source_factory: Callable[[argparse.Namespace], Any] = make_source,
) -> dict[str, Any]:
    """Run exactly one bounded real API search, without DB writes or secret output."""
    if not settings.confirm_live:
        raise ValueError("live smoke requires --confirm-live")
    if not 0 < settings.request_timeout <= 10:
        raise ValueError("live smoke --request-timeout must be > 0 and <= 10 seconds")
    source_settings = argparse.Namespace(**{
        **vars(settings), "source": "api", "max_retries": 0, "retry_backoff": 0.0,
    })
    try:
        items, is_last_page = source_factory(source_settings).search_page(
            settings.query, settings.area, page=0, per_page=1,
        )
    except (requests.RequestException, ValueError) as error:
        return {"status": "degraded", "partial": True, "error_type": type(error).__name__}
    return {"status": "completed", "partial": False, "items": len(items), "is_last_page": is_last_page}


def oauth_client_secret(settings: argparse.Namespace) -> str:
    """Read client secret without exposing it through arguments, files, or output."""
    secret = settings.client_secret or getpass.getpass("HH OAuth client secret: ")
    if not secret:
        raise ValueError("HH OAuth client secret is required")
    return secret


def persisted_oauth_token(token: dict[str, Any]) -> dict[str, Any]:
    """Keep only token fields needed for future API use/refresh."""
    stored = {key: token[key] for key in ("access_token", "refresh_token", "expires_in", "token_type") if key in token}
    stored["obtained_at"] = date.today().isoformat()
    return stored


def run_auth_login(
    settings: argparse.Namespace, *, opener: Callable[[str], bool] = webbrowser.open,
    callback_waiter: Callable[[str, str, int], str] = wait_for_authorization_code,
    token_requester: Callable[..., dict[str, Any]] = request_token,
) -> dict[str, Any]:
    """Complete local OAuth authorization-code flow with PKCE and save no client secret."""
    client_secret = oauth_client_secret(settings)
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(32)
    url = authorization_url(
        client_id=settings.client_id, redirect_uri=settings.redirect_uri, state=state, challenge=challenge,
    )
    print(json.dumps({"authorization_url": url}, ensure_ascii=False), file=sys.stderr, flush=True)
    if not settings.no_browser:
        opener(url)
    code = callback_waiter(settings.redirect_uri, state, settings.callback_timeout)
    token = token_requester({
        "grant_type": "authorization_code", "client_id": settings.client_id,
        "client_secret": client_secret, "redirect_uri": settings.redirect_uri,
        "code": code, "code_verifier": verifier,
    }, user_agent=settings.user_agent)
    write_token_file(settings.token_file, persisted_oauth_token(token), overwrite=settings.overwrite)
    return {"status": "completed", "token_file": str(settings.token_file), **token_metadata(token)}


def run_auth_refresh(
    settings: argparse.Namespace, *, token_requester: Callable[..., dict[str, Any]] = request_token,
) -> dict[str, Any]:
    """Refresh an existing token file in place without printing its secret values."""
    current = read_token_file(settings.token_file)
    refresh_token = current.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ValueError("token file has no refresh_token; run auth login again")
    token = token_requester({
        "grant_type": "refresh_token", "client_id": settings.client_id,
        "client_secret": oauth_client_secret(settings), "refresh_token": refresh_token,
    }, user_agent=settings.user_agent)
    write_token_file(settings.token_file, persisted_oauth_token(token), overwrite=True)
    return {"status": "completed", "token_file": str(settings.token_file), **token_metadata(token)}


def run_discover(settings: argparse.Namespace) -> int:
    database = database_for(settings)
    database.migrate()
    dictionary = load_skill_dictionary(settings.skills_file)
    rows = discover_skill_candidates(database, dictionary, min_document_frequency=settings.min_document_frequency)
    if settings.batch_id:
        database.store_skill_review_batch(settings.batch_id, dictionary.version, {
            "min_document_frequency": settings.min_document_frequency,
            "snapshot_scope": "latest",
        }, rows)
    return export_skill_candidates(rows, settings.output)


def run_import_skill_candidates(settings: argparse.Namespace) -> int:
    database = database_for(settings)
    database.migrate()
    return import_skill_candidates(settings.path, settings.skills_file, settings.output, database=database, batch_id=settings.batch_id)


def run_import_labeling(settings: argparse.Namespace) -> int:
    """Apply reviewed labels from CSV without overwriting automatic labels."""
    database = database_for(settings)
    database.migrate()
    return import_labeling(database, settings.path)


def run_extract(settings: argparse.Namespace) -> dict[str, int]:
    """Run selected deterministic extractor with no HH transport or collection state."""
    validate_date_range(settings.date_from, settings.date_to)
    database = database_for(settings)
    database.migrate()
    dictionary = load_skill_dictionary(settings.skills_file) if settings.extract_command == "skills" else None
    return run_extraction(
        database, settings.extract_command, snapshot_scope=settings.snapshot,
        run_ids=settings.run_id or None, area_ids=settings.area or None, source=settings.source,
        date_from=settings.date_from, date_to=settings.date_to, skill_dictionary=dictionary,
    )


def run_areas_sync(
    settings: argparse.Namespace, *, source_factory: Callable[[argparse.Namespace], Any] = make_source,
) -> int:
    """Fetch and version official HH geographic tree."""
    database = database_for(settings)
    database.migrate()
    source = source_factory(settings)
    catalog_id = database.store_area_catalog(
        source.areas(), source_url=f"{getattr(source, 'base_url', HHApiSource.base_url)}/areas",
        host=settings.host, locale=settings.locale,
    )
    _, catalog = database.load_area_catalog(catalog_id)
    validate_area_ids([settings.root], catalog)
    return catalog_id


def run_areas_list(settings: argparse.Namespace) -> list[dict[str, str]]:
    """Return deterministic area list for one frozen catalog version."""
    database = database_for(settings)
    database.migrate()
    catalog_id, catalog = database.load_area_catalog(settings.catalog_version)
    selected = select_catalog_areas(catalog, settings.root, settings.level)
    return [
        {"catalog_version": str(catalog_id), "id": area_id, "name": catalog[area_id].name}
        for area_id in selected
    ]


def run_areas_validate(settings: argparse.Namespace) -> list[str]:
    """Validate CLI/file area selection before collection network work."""
    if settings.area and settings.areas_file:
        raise AreaSelectionError("--area and --areas-file cannot be combined")
    values = list(settings.area) if settings.area else load_area_file(settings.areas_file) if settings.areas_file else []
    if not values:
        raise AreaSelectionError("specify --area or --areas-file")
    database = database_for(settings)
    database.migrate()
    _, catalog = database.load_area_catalog(settings.catalog_version)
    validate_area_ids(values, catalog)
    overlaps = find_overlaps(values, catalog)
    if overlaps and not settings.allow_area_overlap:
        pairs = ", ".join(f"{parent}/{child}" for parent, child in overlaps)
        raise AreaSelectionError(f"overlapping selected areas: {pairs}; pass --allow-area-overlap")
    return values


def main(argv: list[str] | None = None) -> None:
    """Run collect/resume command and print machine-readable result."""
    argv = argv if argv is not None else os.sys.argv[1:]
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument("--config")
    config_path, _ = config_parser.parse_known_args(argv)
    parser = build_parser()
    apply_defaults(parser, cli_defaults(load_config(config_path.config)))
    environment = {
        "access_token": os.environ.get("HH_ACCESS_TOKEN"),
        "user_agent": os.environ.get("HH_USER_AGENT"),
        "database": os.environ.get("HH_DATABASE"),
    }
    apply_defaults(parser, {key: value for key, value in environment.items() if value is not None})
    settings = parser.parse_args(argv)
    try:
        if settings.command == "collect":
            run_id, counters = run_collect(settings)
            print(json.dumps({"run_id": run_id, **counters}, ensure_ascii=False, sort_keys=True))
        elif settings.command == "resume":
            counters = run_resume(settings)
            print(json.dumps({"run_id": settings.run_id, **counters}, ensure_ascii=False, sort_keys=True))
        elif settings.command == "retry":
            print(json.dumps({"run_id": settings.run_id, **run_retry(settings)}, ensure_ascii=False, sort_keys=True))
        elif settings.command == "coverage":
            print(json.dumps(run_coverage(settings), ensure_ascii=False, sort_keys=True))
        elif settings.command == "runs":
            print(json.dumps(run_runs(settings), ensure_ascii=False, sort_keys=True))
        elif settings.command == "export":
            if settings.export_command == "labeling":
                rows = run_export_labeling(settings)
            elif settings.export_command == "vacancies":
                rows = run_export_vacancies(settings)
            elif settings.export_command == "skills":
                rows = run_export_skills(settings)
            elif settings.export_command == "marts":
                print(json.dumps(run_export_marts(settings), ensure_ascii=False, sort_keys=True))
                return
            else:
                rows = run_export_relation(settings)
            print(json.dumps({"rows": rows}, ensure_ascii=False, sort_keys=True))
        elif settings.command == "pilot":
            print(json.dumps(run_pilot(settings), ensure_ascii=False, sort_keys=True))
        elif settings.command == "areas":
            if settings.areas_command == "sync":
                print(json.dumps({"catalog_version": run_areas_sync(settings)}, sort_keys=True))
            elif settings.areas_command == "list":
                print(json.dumps(run_areas_list(settings), ensure_ascii=False, sort_keys=True))
            else:
                print(json.dumps({"area_ids": run_areas_validate(settings)}, ensure_ascii=False, sort_keys=True))
        elif settings.command == "extract":
            print(json.dumps(run_extract(settings), ensure_ascii=False, sort_keys=True))
        elif settings.command == "stats":
            print(json.dumps(run_stats(settings), ensure_ascii=False, sort_keys=True))
        elif settings.command == "maintenance":
            print(json.dumps(run_maintenance(settings), ensure_ascii=False, sort_keys=True))
        elif settings.command == "db":
            print(json.dumps(run_db(settings), ensure_ascii=False, sort_keys=True))
        elif settings.command == "auth":
            result = run_auth_login(settings) if settings.auth_command == "login" else run_auth_refresh(settings)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        elif settings.command == "smoke":
            print(json.dumps(run_live_smoke(settings), ensure_ascii=False, sort_keys=True))
        elif settings.command == "discover":
            print(json.dumps({"rows": run_discover(settings)}, ensure_ascii=False, sort_keys=True))
        elif settings.command == "import":
            rows = run_import_labeling(settings) if settings.import_command == "labeling" else run_import_skill_candidates(settings)
            print(json.dumps({"rows": rows}, ensure_ascii=False, sort_keys=True))
        else:
            raise SystemExit("error: unsupported command")
    except (AreaSelectionError, OSError, ValueError, requests.RequestException) as error:
        raise SystemExit(f"error: {error}") from error


if __name__ == "__main__":
    main()
