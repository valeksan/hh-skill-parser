"""Validated TOML settings for database-first HH collection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


ALLOWED: dict[str, set[str]] = {
    "hh": {"access_token", "user_agent", "request_timeout", "max_retries", "retry_backoff", "host", "locale"},
    "database": {"path", "wal", "busy_timeout_ms"},
    "collection": {
        "mode", "max_pages", "areas_file", "areas_source", "area_root", "area_level",
        "date_from", "date_to", "date_slice_min_days", "date_overlap_days", "incremental_overlap_days",
    },
    "search": {"queries_file"},
}


def load_config(path: str | Path | None) -> dict[str, Any]:
    """Load known TOML keys. Missing config intentionally means built-in defaults."""
    if path is None:
        return {}
    config_path = Path(path)
    if not config_path.is_file():
        raise ValueError(f"config file does not exist: {config_path}")
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)
    if not isinstance(data, dict):
        raise ValueError("config root must be a TOML table")
    for section, values in data.items():
        if section not in ALLOWED:
            raise ValueError(f"unknown config section: [{section}]")
        if not isinstance(values, dict):
            raise ValueError(f"config section [{section}] must be a table")
        unknown = set(values) - ALLOWED[section]
        if unknown:
            raise ValueError(f"unknown config key: [{section}].{sorted(unknown)[0]}")
    return data


def cli_defaults(config: dict[str, Any]) -> dict[str, Any]:
    """Translate TOML keys to argparse destinations; token stays out of run config."""
    validate_config(config)
    hh = config.get("hh", {})
    database = config.get("database", {})
    collection = config.get("collection", {})
    search = config.get("search", {})
    defaults = {
        "access_token": hh.get("access_token"), "user_agent": hh.get("user_agent"),
        "host": hh.get("host"), "locale": hh.get("locale"),
        "request_timeout": hh.get("request_timeout"), "database": database.get("path"),
        "wal": database.get("wal"), "busy_timeout_ms": database.get("busy_timeout_ms"),
        "max_retries": hh.get("max_retries"), "retry_backoff": hh.get("retry_backoff"),
        "collection_mode": collection.get("mode"), "max_pages": collection.get("max_pages"),
        "areas_file": collection.get("areas_file"), "areas_source": collection.get("areas_source"),
        "area_root": collection.get("area_root"), "area_level": collection.get("area_level"),
        "date_from": collection.get("date_from"), "date_to": collection.get("date_to"),
        "date_slice_min_days": collection.get("date_slice_min_days"),
        "date_overlap_days": collection.get("date_overlap_days"),
        "incremental_overlap_days": collection.get("incremental_overlap_days"),
        "queries_file": search.get("queries_file"),
    }
    return {key: value for key, value in defaults.items() if value is not None}


def validate_config(config: dict[str, Any]) -> None:
    """Reject bad scalar types/ranges before database or network activity."""
    hh = config.get("hh", {})
    database = config.get("database", {})
    collection = config.get("collection", {})
    for section, key in (
        ("hh", "access_token"), ("hh", "user_agent"), ("hh", "host"),
        ("hh", "locale"), ("database", "path"),
    ):
        value = config.get(section, {}).get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"[{section}].{key} must be a string")
    wal = database.get("wal")
    if wal is not None and not isinstance(wal, bool):
        raise ValueError("[database].wal must be a boolean")
    busy_timeout = database.get("busy_timeout_ms")
    if busy_timeout is not None and (
        not isinstance(busy_timeout, int) or isinstance(busy_timeout, bool) or busy_timeout < 0
    ):
        raise ValueError("[database].busy_timeout_ms must be a non-negative integer")
    timeout = hh.get("request_timeout")
    if timeout is not None and (not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0):
        raise ValueError("[hh].request_timeout must be positive")
    max_retries = hh.get("max_retries")
    if max_retries is not None and (not isinstance(max_retries, int) or isinstance(max_retries, bool) or max_retries < 0):
        raise ValueError("[hh].max_retries must be a non-negative integer")
    retry_backoff = hh.get("retry_backoff")
    if retry_backoff is not None and (not isinstance(retry_backoff, (int, float)) or isinstance(retry_backoff, bool) or retry_backoff < 0):
        raise ValueError("[hh].retry_backoff must be non-negative")
    max_pages = collection.get("max_pages")
    if max_pages is not None and (not isinstance(max_pages, int) or isinstance(max_pages, bool) or not 1 <= max_pages <= 20):
        raise ValueError("[collection].max_pages must be between 1 and 20")
    if collection.get("mode") is not None and collection["mode"] not in {"incremental", "full"}:
        raise ValueError("[collection].mode must be incremental or full")
    for key in ("date_slice_min_days", "date_overlap_days", "incremental_overlap_days"):
        value = collection.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            raise ValueError(f"[collection].{key} must be a non-negative integer")
    if collection.get("date_slice_min_days") is not None and collection["date_slice_min_days"] < 1:
        raise ValueError("[collection].date_slice_min_days must be positive")
