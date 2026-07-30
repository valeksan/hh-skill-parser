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
    """Load TOML specs or legacy one-expression-per-line input."""
    path = Path(path)
    if path.suffix != ".toml":
        values = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                  if line.strip() and not line.lstrip().startswith("#")]
        if not values:
            raise ValueError("query file contains no active expressions")
        return [QuerySpec(id=f"legacy-{index}", expression=value) for index, value in enumerate(values, 1)]
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
        result.append(QuerySpec(query_id, expression.strip(), row.get("group"), row.get("purpose"), version,
                                tuple(fields), row.get("enabled", True)))
    active = [item for item in result if item.enabled]
    if not active:
        raise ValueError("query TOML contains no enabled expressions")
    return active
