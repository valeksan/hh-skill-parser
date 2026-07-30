"""HH area-file parsing and catalog-tree selection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


class AreaSelectionError(ValueError):
    """Invalid explicit area selection."""


@dataclass(frozen=True)
class Area:
    hh_id: str
    name: str
    parent_id: str | None
    depth: int


def parse_area_lines(lines: Iterable[str]) -> list[str]:
    """Read one numeric HH area ID per non-comment line."""
    result: list[str] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(lines, start=1):
        value = raw_line.split("#", 1)[0].strip()
        if not value:
            continue
        if not value.isdigit() or int(value) <= 0:
            raise AreaSelectionError(f"line {line_number}: expected positive HH area ID")
        if value in seen:
            raise AreaSelectionError(f"line {line_number}: duplicate HH area ID {value}")
        seen.add(value)
        result.append(value)
    if not result:
        raise AreaSelectionError("area file contains no active IDs")
    return result


def load_area_file(path: str | Path) -> list[str]:
    """Load areas.txt-style file."""
    with Path(path).open(encoding="utf-8") as handle:
        return parse_area_lines(handle)


def flatten_area_tree(tree: list[dict[str, Any]]) -> dict[str, Area]:
    """Flatten official /areas response without coordinates or other extra fields."""
    result: dict[str, Area] = {}

    def visit(node: dict[str, Any], depth: int, fallback_parent: str | None) -> None:
        area_id = str(node["id"])
        if area_id in result:
            raise AreaSelectionError(f"duplicate area ID in catalog: {area_id}")
        parent_id = node.get("parent_id") or fallback_parent
        result[area_id] = Area(area_id, str(node["name"]), str(parent_id) if parent_id else None, depth)
        for child in node.get("areas") or []:
            visit(child, depth + 1, area_id)

    for root in tree:
        visit(root, 0, None)
    return result


def validate_area_ids(area_ids: Iterable[str], catalog: dict[str, Area]) -> list[str]:
    """Ensure explicit IDs exist in frozen catalog."""
    values = list(area_ids)
    unknown = [area_id for area_id in values if area_id not in catalog]
    if unknown:
        raise AreaSelectionError(f"unknown HH area IDs: {', '.join(unknown)}")
    return values


def select_catalog_areas(catalog: dict[str, Area], root_id: str, level: str) -> list[str]:
    """Select root, direct children, or leaves of one catalog subtree."""
    if root_id not in catalog:
        raise AreaSelectionError(f"unknown root HH area ID: {root_id}")
    if level not in {"root", "children", "leaf"}:
        raise AreaSelectionError("area level must be root, children, or leaf")
    children: dict[str, list[str]] = {}
    for area in catalog.values():
        if area.parent_id:
            children.setdefault(area.parent_id, []).append(area.hh_id)
    if level == "root":
        return [root_id]
    if level == "children":
        return sorted(children.get(root_id, []), key=int)

    result: list[str] = []
    stack = [root_id]
    while stack:
        current = stack.pop()
        descendants = children.get(current, [])
        if descendants:
            stack.extend(descendants)
        else:
            result.append(current)
    return sorted(result, key=int)


def find_overlaps(area_ids: Iterable[str], catalog: dict[str, Area]) -> list[tuple[str, str]]:
    """Return selected (parent, descendant) pairs."""
    selected = validate_area_ids(area_ids, catalog)
    selected_set = set(selected)
    overlaps: list[tuple[str, str]] = []
    for area_id in selected:
        parent_id = catalog[area_id].parent_id
        while parent_id:
            if parent_id in selected_set:
                overlaps.append((parent_id, area_id))
            parent_id = catalog[parent_id].parent_id if parent_id in catalog else None
    return sorted(overlaps, key=lambda pair: (int(pair[0]), int(pair[1])))
