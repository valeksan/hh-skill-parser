"""CSV interchange for reproducible manual relevance review."""

from __future__ import annotations

import csv
from pathlib import Path

from .storage import Database

FIELDS = ("snapshot_id", "hh_id", "title", "description", "employer", "query_families", "auto_label", "auto_score", "auto_reasons", "manual_label", "manual_reason")


def export_labeling(database: Database, path: str | Path) -> int:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT s.id snapshot_id, s.vacancy_hh_id hh_id, s.title, s.description_text description, "
            "s.employer_name employer, COALESCE((SELECT group_concat(query_group, '|') FROM ("
            "SELECT DISTINCT COALESCE(q.query_group, '') AS query_group "
            "FROM vacancy_query_hits h JOIN search_queries q ON q.id=h.query_id "
            "WHERE h.vacancy_hh_id=s.vacancy_hh_id ORDER BY query_group)), '') query_families, "
            "l.label auto_label, l.score auto_score, l.reasons_json auto_reasons, "
            "l.manual_label, l.manual_reason FROM vacancy_snapshots s JOIN relevance_labels l ON l.snapshot_id=s.id "
            "ORDER BY s.id"
        ).fetchall()
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(dict(row) for row in rows)
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
