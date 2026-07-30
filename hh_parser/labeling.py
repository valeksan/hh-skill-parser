"""CSV interchange for reproducible manual relevance review."""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Mapping
from pathlib import Path

from .storage import Database

FIELDS = ("snapshot_id", "hh_id", "title", "description", "employer", "query_families", "auto_label", "auto_score", "auto_reasons", "manual_label", "manual_reason")


def _rank(seed: str, value: str) -> str:
    return hashlib.sha256(f"{seed}:{value}".encode("utf-8")).hexdigest()


def stratified_sample(
    rows: list[Mapping[str, object]], sample_size: int = 0, sample_seed: str = "0",
) -> list[Mapping[str, object]]:
    """Return all rows or deterministic balanced sample across review strata."""
    if sample_size < 0:
        raise ValueError("sample size must not be negative")
    if sample_size == 0 or sample_size >= len(rows):
        return rows
    strata: dict[tuple[str, str, str, str], list[Mapping[str, object]]] = {}
    for row in rows:
        period = str(row.get("_period") or "unknown")[:7] or "unknown"
        key = (
            str(row.get("query_families") or "unknown"), str(row.get("_area_id") or "unknown"),
            period, str(row.get("auto_label") or "unknown"),
        )
        strata.setdefault(key, []).append(row)
    buckets = [
        sorted(values, key=lambda row: _rank(sample_seed, str(row["snapshot_id"])))
        for _, values in sorted(strata.items(), key=lambda item: _rank(sample_seed, repr(item[0])))
    ]
    selected: list[Mapping[str, object]] = []
    while len(selected) < sample_size and any(buckets):
        for bucket in buckets:
            if bucket and len(selected) < sample_size:
                selected.append(bucket.pop(0))
    return sorted(selected, key=lambda row: int(str(row["snapshot_id"])))


def export_labeling(
    database: Database, path: str | Path, *, sample_size: int = 0, sample_seed: str = "0",
) -> int:
    """Export all candidates or deterministic stratified review sample."""
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT s.id snapshot_id, s.vacancy_hh_id hh_id, s.title, s.description_text description, "
            "s.employer_name employer, COALESCE((SELECT group_concat(query_group, '|') FROM ("
            "SELECT DISTINCT COALESCE(q.query_group, '') AS query_group "
            "FROM vacancy_query_hits h JOIN search_queries q ON q.id=h.query_id "
            "WHERE h.vacancy_hh_id=s.vacancy_hh_id ORDER BY query_group)), '') query_families, "
            "l.label auto_label, l.score auto_score, l.reasons_json auto_reasons, "
            "l.manual_label, l.manual_reason, s.area_id _area_id, "
            "COALESCE(s.published_at, s.observed_at) _period "
            "FROM vacancy_snapshots s JOIN relevance_labels l ON l.snapshot_id=s.id "
            "ORDER BY s.id"
        ).fetchall()
    rows = stratified_sample([dict(row) for row in rows], sample_size, sample_seed)
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in FIELDS} for row in rows)
    return len(rows)


def import_labeling(database: Database, path: str | Path) -> int:
    """Idempotently apply nonblank reviewed labels from a labeling CSV."""
    with Path(path).open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or not {"snapshot_id", "manual_label", "manual_reason"}.issubset(reader.fieldnames):
            raise ValueError("labeling CSV requires snapshot_id, manual_label, manual_reason columns")
        rows = list(reader)
    applied = 0
    for row in rows:
        label = (row.get("manual_label") or "").strip()
        if not label:
            continue
        try:
            snapshot_id = int(row["snapshot_id"])
        except (TypeError, ValueError) as error:
            raise ValueError("snapshot_id must be an integer") from error
        database.set_manual_relevance(snapshot_id, label, (row.get("manual_reason") or "").strip())
        applied += 1
    return applied
