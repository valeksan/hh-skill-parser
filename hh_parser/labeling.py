"""CSV interchange for reproducible manual relevance review."""

from __future__ import annotations

import csv
from pathlib import Path

from .storage import Database

FIELDS = ("snapshot_id", "hh_id", "title", "description", "employer", "auto_label", "auto_score", "auto_reasons", "manual_label", "manual_reason")


def export_labeling(database: Database, path: str | Path) -> int:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT s.id snapshot_id, s.vacancy_hh_id hh_id, s.title, s.description_text description, "
            "s.employer_name employer, l.label auto_label, l.score auto_score, l.reasons_json auto_reasons, "
            "l.manual_label, l.manual_reason FROM vacancy_snapshots s JOIN relevance_labels l ON l.snapshot_id=s.id "
            "ORDER BY s.id"
        ).fetchall()
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(dict(row) for row in rows)
    return len(rows)
