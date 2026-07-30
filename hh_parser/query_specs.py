"""Versioned external HH search expressions, separate from local extractors."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


@dataclass(frozen=True)
class QuerySpec:
    id: str
    expression: str
    group: str | None = None
    purpose: str | None = None
    version: str = "1"
    search_fields: tuple[str, ...] = ()
    enabled: bool = True


def load_query_specs(path: str | Path) -> list[QuerySpec]:
    """Load versioned TOML query specifications."""
    path = Path(path)
    if path.suffix != ".toml":
        raise ValueError("query specifications must use a .toml file")
    with path.open("rb") as handle:
        document = tomllib.load(handle)
    version = str(document.get("version", "1"))
    rows = document.get("query")
    if not isinstance(rows, list) or not rows:
        raise ValueError("query TOML must contain one or more [[query]] tables")
    result = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each [[query]] must be a table")
        unknown = set(row) - {"id", "group", "expression", "search_fields", "purpose", "enabled"}
        if unknown:
            raise ValueError(f"unknown query key: {sorted(unknown)[0]}")
        query_id, expression = row.get("id"), row.get("expression")
        if not isinstance(query_id, str) or not query_id or not isinstance(expression, str) or not expression.strip():
            raise ValueError("query id and expression must be nonempty strings")
        if query_id in seen:
            raise ValueError(f"duplicate query id: {query_id}")
        seen.add(query_id)
        fields = row.get("search_fields", [])
        if not isinstance(fields, list) or not all(isinstance(item, str) for item in fields):
            raise ValueError(f"query {query_id}: search_fields must be string array")
        if len(fields) != len(set(fields)):
            raise ValueError(f"query {query_id}: search_fields must not contain duplicates")
        unknown_fields = set(fields) - ALLOWED_SEARCH_FIELDS
        if unknown_fields:
            raise ValueError(f"query {query_id}: unsupported search_fields: {sorted(unknown_fields)[0]}")
        for name, value in (("group", row.get("group")), ("purpose", row.get("purpose"))):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"query {query_id}: {name} must be nonempty string")
        if not isinstance(row.get("enabled", True), bool):
            raise ValueError(f"query {query_id}: enabled must be boolean")
        result.append(QuerySpec(query_id, expression.strip(), row.get("group"), row.get("purpose"), version,
                                tuple(fields), row.get("enabled", True)))
    active = [item for item in result if item.enabled]
    if not active:
        raise ValueError("query TOML contains no enabled expressions")
    return active
ALLOWED_SEARCH_FIELDS = frozenset({"name", "description"})
