"""Database-first collection commands exposed by package CLI."""

from __future__ import annotations

import argparse
from datetime import date
import json
import os
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
from .export import export_query_hits, export_roles, export_skills, export_vacancies
from .config import cli_defaults, load_config
from .labeling import export_labeling, import_labeling
from .query_specs import QuerySpec, load_query_specs
from .skill_dictionary import load_skill_dictionary
from .sources.api import HHApiSource
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


def validate_date_range(date_from: str | None, date_to: str | None) -> None:
    """Require finite, ordered date window when either bound is requested."""
    if bool(date_from) != bool(date_to):
        raise ValueError("--date-from and --date-to must be specified together")
    if date_from and date_from > date_to:
        raise ValueError("--date-from must not be after --date-to")


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


def add_transport_arguments(parser: argparse.ArgumentParser) -> None:
    """Add API connection options shared by collect and resume."""
    parser.add_argument("--source", choices=["api"], default="api")
    parser.add_argument("--access-token", default=os.environ.get("HH_ACCESS_TOKEN"))
    parser.add_argument("--user-agent", default=os.environ.get("HH_USER_AGENT", DEFAULT_USER_AGENT))
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument("--max-retries", type=nonnegative_int, default=3)
    parser.add_argument("--retry-backoff", type=float, default=1.0)


def build_parser() -> argparse.ArgumentParser:
    """Build command parser without importing legacy CLI options."""
    parser = argparse.ArgumentParser(prog="hh-skill-parser")
    commands = parser.add_subparsers(dest="command", required=True)

    collect = commands.add_parser("collect", help="collect HH vacancies into SQLite")
    collect.add_argument("--config")
    collect.add_argument("--database", default=os.environ.get("HH_DATABASE", DEFAULT_DATABASE))
    collect.add_argument("--queries-file", default="query_specs.toml")
    collect.add_argument("--area", action="append", default=[])
    collect.add_argument("--areas-file")
    collect.add_argument("--areas-source", choices=["explicit", "catalog"], default="explicit")
    collect.add_argument("--area-root", default="113")
    collect.add_argument("--area-level", choices=["root", "children", "leaf"], default="root")
    collect.add_argument("--catalog-version", type=int)
    collect.add_argument("--allow-area-overlap", action="store_true")
    collect.add_argument("--collection-mode", choices=["incremental", "full"], default="incremental")
    collect.add_argument("--max-pages", type=int, default=20)
    collect.add_argument("--date-from", type=parse_iso_date)
    collect.add_argument("--date-to", type=parse_iso_date)
    collect.add_argument("--date-slice-min-days", type=int, default=1)
    collect.add_argument("--date-overlap-days", type=int, default=1)
    add_transport_arguments(collect)

    resume = commands.add_parser("resume", help="resume one degraded/interrupted SQLite run")
    resume.add_argument("--config")
    resume.add_argument("--database", default=os.environ.get("HH_DATABASE", DEFAULT_DATABASE))
    resume.add_argument("--run-id", required=True, type=int)
    resume.add_argument("--queries-file", default="query_specs.toml")
    resume.add_argument("--max-pages", type=int, default=20)
    add_transport_arguments(resume)

    extract = commands.add_parser("extract", help="derive offline data from stored snapshots")
    extract.add_argument("--config")
    extract.add_argument("--database", default=os.environ.get("HH_DATABASE", DEFAULT_DATABASE))
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
    export.add_argument("--database", default=os.environ.get("HH_DATABASE", DEFAULT_DATABASE))
    export_commands = export.add_subparsers(dest="export_command", required=True)
    labeling_export = export_commands.add_parser("labeling", help="export relevance-labeling CSV")
    labeling_export.add_argument("--output", required=True)
    labeling_export.add_argument("--sample-size", type=nonnegative_int, default=0)
    labeling_export.add_argument("--sample-seed", default="0")
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

    stats = commands.add_parser("stats", help="calculate DB-only vacancy statistics")
    stats.add_argument("--config")
    stats.add_argument("--database", default=os.environ.get("HH_DATABASE", DEFAULT_DATABASE))
    stats.add_argument("--snapshot", choices=["latest", "all"], default="latest")
    stats.add_argument("--run-id", action="append", type=int, default=[])
    stats.add_argument("--area", action="append", default=[])
    stats.add_argument("--relevance", choices=["relevant", "borderline", "irrelevant", "unknown"])
    stats.add_argument("--query-family")
    stats.add_argument("--date-from", type=parse_iso_date)
    stats.add_argument("--date-to", type=parse_iso_date)

    maintenance = commands.add_parser("maintenance", help="explicit database maintenance")
    maintenance.add_argument("--database", default=os.environ.get("HH_DATABASE", DEFAULT_DATABASE))
    maintenance_commands = maintenance.add_subparsers(dest="maintenance_command", required=True)
    purge_raw = maintenance_commands.add_parser("purge-raw", help="preview or purge old raw payload BLOBs only")
    purge_raw.add_argument("--before", required=True, type=parse_iso_date)
    purge_raw.add_argument("--execute", action="store_true", help="perform purge; default is preview")
    purge_raw.add_argument("--confirm", help="required exact value: PURGE_RAW_PAYLOADS")

    db = commands.add_parser("db", help="database lifecycle commands")
    db.add_argument("--database", default=os.environ.get("HH_DATABASE", DEFAULT_DATABASE))
    db_commands = db.add_subparsers(dest="db_command", required=True)
    db_commands.add_parser("migrate", help="apply packaged SQLite migrations")
    db_commands.add_parser("check", help="run SQLite integrity check")
    db_commands.add_parser("checkpoint", help="checkpoint SQLite WAL without deleting data")
    reset = db_commands.add_parser("reset", help="preview or clear all collected/derived data")
    reset.add_argument("--yes", action="store_true", help="permanently clear data; default is preview")

    discover = commands.add_parser("discover", help="mine skill candidates from stored corpus")
    discover.add_argument("--database", default=os.environ.get("HH_DATABASE", DEFAULT_DATABASE))
    discover_commands = discover.add_subparsers(dest="discover_command", required=True)
    discover_skills = discover_commands.add_parser("skills", help="export unknown skill candidates CSV")
    discover_skills.add_argument("--output", required=True)
    discover_skills.add_argument("--skills-file", default="skills_whitelist.txt")
    discover_skills.add_argument("--min-document-frequency", type=nonnegative_int, default=2)

    labeling_import = commands.add_parser("import", help="import SQLite data")
    labeling_import.add_argument("--config")
    labeling_import.add_argument("--database", default=os.environ.get("HH_DATABASE", DEFAULT_DATABASE))
    import_commands = labeling_import.add_subparsers(dest="import_command", required=True)
    labeling_import_csv = import_commands.add_parser("labeling", help="import reviewed relevance-labeling CSV")
    labeling_import_csv.add_argument("path")
    candidates_import = import_commands.add_parser("skill-candidates", help="apply reviewed skill candidates to new dictionary")
    candidates_import.add_argument("path")
    candidates_import.add_argument("--skills-file", required=True)
    candidates_import.add_argument("--output", required=True)

    areas = commands.add_parser("areas", help="manage versioned HH area catalog")
    areas.add_argument("--database", default=os.environ.get("HH_DATABASE", DEFAULT_DATABASE))
    areas_commands = areas.add_subparsers(dest="areas_command", required=True)
    areas_sync = areas_commands.add_parser("sync", help="fetch and store official HH /areas catalog")
    areas_sync.add_argument("--root", default="113")
    areas_sync.add_argument("--user-agent", default=os.environ.get("HH_USER_AGENT", DEFAULT_USER_AGENT))
    areas_sync.add_argument("--request-timeout", type=float, default=30.0)
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


def make_source(settings: argparse.Namespace) -> HHApiSource:
    """Create public API transport; token is never passed to run config."""
    return HHApiSource(
        user_agent=settings.user_agent, timeout=settings.request_timeout,
        access_token=getattr(settings, "access_token", None) or None,
        max_retries=getattr(settings, "max_retries", 3),
        retry_backoff=getattr(settings, "retry_backoff", 1.0),
    )


def run_collect(
    settings: argparse.Namespace, *, source_factory: Callable[[argparse.Namespace], Any] = make_source,
) -> tuple[int, dict[str, int]]:
    """Start finite DB-backed collection and return run ID with durable counters."""
    database = Database(settings.database)
    database.migrate()
    validate_date_range(settings.date_from, settings.date_to)
    if settings.date_slice_min_days < 1 or settings.date_overlap_days < 0:
        raise ValueError("date slice minimum must be positive; overlap must be non-negative")
    area_ids, catalog_version_id, selection_source = resolve_collect_areas(settings, database)
    queries = load_query_file(settings.queries_file)
    source = source_factory(settings)
    config = {
        "queries_file": str(settings.queries_file), "query_specs": freeze_query_specs(queries), "area_ids": area_ids,
        "catalog_version_id": catalog_version_id, "selection_source": selection_source,
        "source": settings.source, "request_timeout": settings.request_timeout,
        "max_retries": settings.max_retries, "retry_backoff": settings.retry_backoff,
        "collection_mode": settings.collection_mode, "max_pages": settings.max_pages,
        "date_from": settings.date_from, "date_to": settings.date_to,
        "date_slice_min_days": settings.date_slice_min_days,
        "date_overlap_days": settings.date_overlap_days,
    }
    collector = Collector(database, transport=source)
    run_id = collector.start(
        config, area_ids, catalog_version_id=catalog_version_id,
        selection_source=selection_source, source_policy=settings.source,
    )
    if settings.date_from:
        counters = collector.collect_sliced(
            run_id, area_ids, queries, search_page=source.search_page,
            detail=source.detail, max_pages=settings.max_pages,
            date_from=settings.date_from, date_to=settings.date_to,
            min_window_days=settings.date_slice_min_days,
            overlap_days=settings.date_overlap_days,
        )
    else:
        counters = collector.collect_paginated(
            run_id, area_ids, queries, search_page=source.search_page,
            detail=source.detail, max_pages=settings.max_pages,
        )
    return run_id, counters


def run_resume(
    settings: argparse.Namespace, *, source_factory: Callable[[argparse.Namespace], Any] = make_source,
) -> dict[str, int]:
    """Resume DB work without rediscovering areas or losing previous pages/cards."""
    database = Database(settings.database)
    database.migrate()
    config = database.run_config(settings.run_id)
    source = source_factory(settings)
    if config.get("date_from"):
        return Collector(database, transport=source).resume_sliced(
            settings.run_id, load_frozen_query_specs(config, settings.queries_file),
            search_page=source.search_page, detail=source.detail,
            max_pages=config.get("max_pages", settings.max_pages),
            date_from=config["date_from"], date_to=config["date_to"],
            min_window_days=config.get("date_slice_min_days", 1),
            overlap_days=config.get("date_overlap_days", 1),
        )
    return Collector(database, transport=source).resume_paginated(
        settings.run_id, load_frozen_query_specs(config, settings.queries_file),
        search_page=source.search_page, detail=source.detail,
        max_pages=config.get("max_pages", settings.max_pages),
    )


def run_export_labeling(settings: argparse.Namespace) -> int:
    """Write all auto-labeled snapshots as reviewable CSV."""
    database = Database(settings.database)
    database.migrate()
    return export_labeling(
        database, settings.output, sample_size=settings.sample_size, sample_seed=settings.sample_seed,
    )


def run_export_vacancies(settings: argparse.Namespace) -> int:
    """Write filtered vacancy CSV without invoking HH or an extractor."""
    validate_date_range(settings.date_from, settings.date_to)
    database = Database(settings.database)
    database.migrate()
    return export_vacancies(
        database, settings.output, snapshot_scope=settings.snapshot,
        run_ids=settings.run_id or None, area_ids=settings.area or None,
        relevance=settings.relevance, query_family=settings.query_family,
        date_from=settings.date_from, date_to=settings.date_to,
    )


def run_export_skills(settings: argparse.Namespace) -> int:
    database = Database(settings.database)
    database.migrate()
    return export_skills(database, settings.output)


def run_export_relation(settings: argparse.Namespace) -> int:
    database = Database(settings.database)
    database.migrate()
    return export_roles(database, settings.output) if settings.export_command == "roles" else export_query_hits(database, settings.output)


def run_stats(settings: argparse.Namespace) -> dict[str, Any]:
    """Calculate filtered counts from SQLite only."""
    validate_date_range(settings.date_from, settings.date_to)
    database = Database(settings.database)
    database.migrate()
    return vacancy_stats(
        database, snapshot_scope=settings.snapshot, run_ids=settings.run_id or None,
        area_ids=settings.area or None, relevance=settings.relevance,
        query_family=settings.query_family, date_from=settings.date_from, date_to=settings.date_to,
    )


def run_maintenance(settings: argparse.Namespace) -> dict[str, Any]:
    """Run explicit, narrowly scoped maintenance action."""
    database = Database(settings.database)
    database.migrate()
    if settings.maintenance_command != "purge-raw":
        raise ValueError(f"unsupported maintenance command: {settings.maintenance_command}")
    if not settings.execute:
        return {"dry_run": True, "before": settings.before, **database.raw_payload_purge_summary(settings.before)}
    if settings.confirm != "PURGE_RAW_PAYLOADS":
        raise ValueError("purge requires --execute --confirm PURGE_RAW_PAYLOADS")
    return {"dry_run": False, "before": settings.before, **database.purge_raw_payloads(settings.before)}


def run_db(settings: argparse.Namespace) -> dict[str, Any]:
    """Run schema migration or explicit full data reset."""
    database = Database(settings.database)
    database.migrate()
    if settings.db_command == "migrate":
        return {"migrated": True}
    if settings.db_command == "check":
        result = database.integrity_check()
        return {"ok": result == ["ok"], "result": result}
    if settings.db_command == "checkpoint":
        return database.checkpoint()
    summary = database.reset_data_summary()
    if not settings.yes:
        return {"dry_run": True, "tables": summary}
    return {"dry_run": False, "tables": database.reset_data()}


def run_discover(settings: argparse.Namespace) -> int:
    database = Database(settings.database)
    database.migrate()
    dictionary = load_skill_dictionary(settings.skills_file)
    rows = discover_skill_candidates(database, dictionary, min_document_frequency=settings.min_document_frequency)
    return export_skill_candidates(rows, settings.output)


def run_import_skill_candidates(settings: argparse.Namespace) -> int:
    return import_skill_candidates(settings.path, settings.skills_file, settings.output)


def run_import_labeling(settings: argparse.Namespace) -> int:
    """Apply reviewed labels from CSV without overwriting automatic labels."""
    database = Database(settings.database)
    database.migrate()
    return import_labeling(database, settings.path)


def run_extract(settings: argparse.Namespace) -> dict[str, int]:
    """Run selected deterministic extractor with no HH transport or collection state."""
    validate_date_range(settings.date_from, settings.date_to)
    database = Database(settings.database)
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
    database = Database(settings.database)
    database.migrate()
    source = source_factory(settings)
    catalog_id = database.store_area_catalog(
        source.areas(), source_url=f"{getattr(source, 'base_url', HHApiSource.base_url)}/areas",
    )
    _, catalog = database.load_area_catalog(catalog_id)
    validate_area_ids([settings.root], catalog)
    return catalog_id


def run_areas_list(settings: argparse.Namespace) -> list[dict[str, str]]:
    """Return deterministic area list for one frozen catalog version."""
    database = Database(settings.database)
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
    database = Database(settings.database)
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
        elif settings.command == "export":
            if settings.export_command == "labeling":
                rows = run_export_labeling(settings)
            elif settings.export_command == "vacancies":
                rows = run_export_vacancies(settings)
            elif settings.export_command == "skills":
                rows = run_export_skills(settings)
            else:
                rows = run_export_relation(settings)
            print(json.dumps({"rows": rows}, ensure_ascii=False, sort_keys=True))
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
