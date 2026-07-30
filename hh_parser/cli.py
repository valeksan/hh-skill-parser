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

from .areas import AreaSelectionError, find_overlaps, load_area_file, select_catalog_areas, validate_area_ids
from .collector import Collector
from .sources.api import HHApiSource
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


def validate_date_range(date_from: str | None, date_to: str | None) -> None:
    """Require finite, ordered date window when either bound is requested."""
    if bool(date_from) != bool(date_to):
        raise ValueError("--date-from and --date-to must be specified together")
    if date_from and date_from > date_to:
        raise ValueError("--date-from must not be after --date-to")


def load_query_file(path: str | Path) -> list[str]:
    """Load nonempty, non-comment HH expressions without altering syntax."""
    values = [
        line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not values:
        raise ValueError("query file contains no active expressions")
    return values


def add_transport_arguments(parser: argparse.ArgumentParser) -> None:
    """Add API connection options shared by collect and resume."""
    parser.add_argument("--source", choices=["api"], default="api")
    parser.add_argument("--access-token", default=os.environ.get("HH_ACCESS_TOKEN"))
    parser.add_argument("--user-agent", default=os.environ.get("HH_USER_AGENT", DEFAULT_USER_AGENT))
    parser.add_argument("--request-timeout", type=float, default=30.0)


def build_parser() -> argparse.ArgumentParser:
    """Build command parser without importing legacy CLI options."""
    parser = argparse.ArgumentParser(prog="hh-skill-parser")
    commands = parser.add_subparsers(dest="command", required=True)

    collect = commands.add_parser("collect", help="collect HH vacancies into SQLite")
    collect.add_argument("--database", default=os.environ.get("HH_DATABASE", DEFAULT_DATABASE))
    collect.add_argument("--queries-file", default="queries.txt")
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
    resume.add_argument("--database", default=os.environ.get("HH_DATABASE", DEFAULT_DATABASE))
    resume.add_argument("--run-id", required=True, type=int)
    resume.add_argument("--queries-file", default="queries.txt")
    resume.add_argument("--max-pages", type=int, default=20)
    add_transport_arguments(resume)
    return parser


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
        access_token=settings.access_token or None,
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
        "queries_file": str(settings.queries_file), "area_ids": area_ids,
        "catalog_version_id": catalog_version_id, "selection_source": selection_source,
        "source": settings.source, "request_timeout": settings.request_timeout,
        "collection_mode": settings.collection_mode, "max_pages": settings.max_pages,
        "date_from": settings.date_from, "date_to": settings.date_to,
        "date_slice_min_days": settings.date_slice_min_days,
        "date_overlap_days": settings.date_overlap_days,
    }
    collector = Collector(database)
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
        return Collector(database).resume_sliced(
            settings.run_id, load_query_file(settings.queries_file),
            search_page=source.search_page, detail=source.detail,
            max_pages=config.get("max_pages", settings.max_pages),
            date_from=config["date_from"], date_to=config["date_to"],
            min_window_days=config.get("date_slice_min_days", 1),
            overlap_days=config.get("date_overlap_days", 1),
        )
    return Collector(database).resume_paginated(
        settings.run_id, load_query_file(settings.queries_file),
        search_page=source.search_page, detail=source.detail,
        max_pages=config.get("max_pages", settings.max_pages),
    )


def main(argv: list[str] | None = None) -> None:
    """Run collect/resume command and print machine-readable result."""
    settings = build_parser().parse_args(argv)
    try:
        if settings.command == "collect":
            run_id, counters = run_collect(settings)
            print(json.dumps({"run_id": run_id, **counters}, ensure_ascii=False, sort_keys=True))
        else:
            counters = run_resume(settings)
            print(json.dumps({"run_id": settings.run_id, **counters}, ensure_ascii=False, sort_keys=True))
    except (AreaSelectionError, OSError, ValueError, requests.RequestException) as error:
        raise SystemExit(f"error: {error}") from error


if __name__ == "__main__":
    main()
