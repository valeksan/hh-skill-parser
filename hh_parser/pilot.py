"""Persistent, offline relevance-pilot sampling and query-family metrics."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .labeling import FIELDS, stratified_sample
from .privacy import safe_employer_name
from .storage import Database, json_value, utc_now


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _where(*, run_ids: list[int], area_ids: list[str], date_from: str | None, date_to: str | None, snapshot_scope: str) -> tuple[str, list[object]]:
    clauses, values = ["1=1"], []
    if run_ids:
        clauses.append(f"s.run_id IN ({','.join('?' for _ in run_ids)})")
        values.extend(run_ids)
    if area_ids:
        clauses.append(f"s.area_id IN ({','.join('?' for _ in area_ids)})")
        values.extend(area_ids)
    if date_from:
        clauses.append("COALESCE(s.published_at, s.observed_at) >= ?")
        values.append(date_from)
    if date_to:
        clauses.append("COALESCE(s.published_at, s.observed_at) < date(?, '+1 day')")
        values.append(date_to)
    if snapshot_scope == "latest":
        clauses.append("NOT EXISTS (SELECT 1 FROM vacancy_snapshots newer WHERE newer.vacancy_hh_id=s.vacancy_hh_id AND (newer.observed_at>s.observed_at OR (newer.observed_at=s.observed_at AND newer.id>s.id)))")
    return " AND ".join(clauses), values


def _candidate_rows(database: Database, filters: dict[str, Any]) -> list[dict[str, object]]:
    where, values = _where(**filters)
    sql = (
        "SELECT s.id snapshot_id, s.vacancy_hh_id hh_id, s.title, s.description_text description, "
        "s.employer_id _employer_id, s.employer_type _employer_type, s.employer_name employer, "
        "COALESCE((SELECT group_concat(query_group, '|') FROM (SELECT DISTINCT COALESCE(q.query_group, '') query_group "
        "FROM vacancy_query_hits h JOIN search_queries q ON q.id=h.query_id "
        "WHERE h.vacancy_hh_id=s.vacancy_hh_id AND h.run_id=s.run_id ORDER BY query_group)), '') query_families, "
        "l.auto_label, l.auto_score, l.auto_reasons_json auto_reasons, l.manual_label, l.manual_reason, "
        "l.effective_label, l.effective_reason, s.area_id _area_id, COALESCE(s.published_at, s.observed_at) _period "
        "FROM vacancy_snapshots s JOIN effective_relevance_labels l ON l.snapshot_id=s.id "
        f"WHERE {where} ORDER BY s.id"
    )
    with database.connect() as connection:
        rows = [dict(row) for row in connection.execute(sql, values).fetchall()]
    for row in rows:
        row["employer"] = safe_employer_name(row["_employer_id"], row["_employer_type"], row["employer"])
    return rows


def _stratum(row: dict[str, object]) -> dict[str, str]:
    return {
        "query_families": str(row.get("query_families") or "unknown"),
        "area_id": str(row.get("_area_id") or "unknown"),
        "period": str(row.get("_period") or "unknown")[:7] or "unknown",
        "auto_label": str(row.get("auto_label") or "unknown"),
    }


def create_pilot(database: Database, batch_id: str, *, sample_size: int, sample_seed: str, filters: dict[str, Any]) -> tuple[int, list[dict[str, object]]]:
    rows = _candidate_rows(database, filters)
    selected = [dict(row) for row in stratified_sample(rows, sample_size, sample_seed)]
    if not selected:
        raise ValueError("pilot scope contains no auto-labeled snapshots")
    with database.transaction() as connection:
        # Scope is frozen from selected snapshots, never from a later query-spec file.
        ids = [int(row["snapshot_id"]) for row in selected]
        specs = [dict(row) for row in connection.execute(
            f"SELECT DISTINCT q.expression, q.query_group, q.purpose, q.enabled, q.version "
            "FROM search_queries q JOIN vacancy_query_hits h ON h.query_id=q.id "
            "JOIN vacancy_snapshots s ON s.vacancy_hh_id=h.vacancy_hh_id AND s.run_id=h.run_id "
            f"WHERE s.id IN ({','.join('?' for _ in ids)}) "
            "ORDER BY q.version, q.query_group, q.expression", ids
        ).fetchall()]
        selection_hash = _hash(ids)
        try:
            connection.execute(
                "INSERT INTO relevance_pilot_batches(id, created_at, sample_seed, requested_size, filters_json, query_specs_json, selection_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (batch_id, utc_now(), sample_seed, sample_size, json_value(filters), json_value(specs), selection_hash),
            )
        except Exception as error:
            if "UNIQUE" in str(error).upper():
                raise ValueError(f"pilot batch already exists: {batch_id}") from error
            raise
        connection.executemany(
            "INSERT INTO relevance_pilot_items(batch_id, snapshot_id, stratum_json) VALUES (?, ?, ?)",
            [(batch_id, int(row["snapshot_id"]), json_value(_stratum(row))) for row in selected],
        )
    return len(selected), selected


def export_pilot_labels(rows: list[dict[str, object]], path: str | Path) -> None:
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in FIELDS} for row in rows)


def pilot_report(database: Database, batch_id: str, *, min_per_stratum: int = 5) -> dict[str, object]:
    if min_per_stratum < 1:
        raise ValueError("min per stratum must be positive")
    with database.connect() as connection:
        batch = connection.execute("SELECT * FROM relevance_pilot_batches WHERE id=?", (batch_id,)).fetchone()
        if batch is None:
            raise ValueError(f"pilot batch does not exist: {batch_id}")
        rows = [dict(row) for row in connection.execute(
            "SELECT i.snapshot_id, i.stratum_json, COALESCE((SELECT group_concat(query_group, '|') FROM ("
            "SELECT DISTINCT COALESCE(q.query_group, '') query_group FROM vacancy_query_hits h "
            "JOIN search_queries q ON q.id=h.query_id JOIN vacancy_snapshots s ON s.id=i.snapshot_id "
            "WHERE h.vacancy_hh_id=s.vacancy_hh_id AND h.run_id=s.run_id ORDER BY query_group)), '') query_families, "
            "l.auto_label, l.auto_score, l.auto_reasons_json, l.classifier_version, "
            "l.manual_label, l.manual_reason, l.manual_labeled_at FROM relevance_pilot_items i "
            "JOIN effective_relevance_labels l ON l.snapshot_id=i.snapshot_id WHERE i.batch_id=? ORDER BY i.snapshot_id", (batch_id,)
        ).fetchall()]
    labels = Counter(str(row["manual_label"] or "unlabeled") for row in rows)
    labeled = [row for row in rows if row["manual_label"] not in (None, "unknown")]
    positives = [row for row in labeled if row["manual_label"] == "relevant"]
    families = sorted({family for row in rows for family in str(row["query_families"] or "").split("|") if family})
    family_metrics = []
    for family in families:
        family_rows = [row for row in rows if family in str(row["query_families"] or "").split("|")]
        family_labeled = [row for row in family_rows if row["manual_label"] not in (None, "unknown")]
        family_positive = [row for row in family_labeled if row["manual_label"] == "relevant"]
        exclusive = [row for row in family_positive if str(row["query_families"] or "").split("|") == [family]]
        family_metrics.append({"query_family": family, "sampled": len(family_rows), "labeled": len(family_labeled), "positive": len(family_positive), "precision": len(family_positive) / len(family_labeled) if family_labeled else None, "recall_in_pilot": len(family_positive) / len(positives) if positives else None, "overlap": sum("|" in str(row["query_families"] or "") for row in family_rows), "marginal_gain": len(exclusive), "marginal_gain_share": len(exclusive) / len(positives) if positives else None})
    strata = Counter(str(row["stratum_json"]) for row in rows)
    label_set = [{key: row[key] for key in ("snapshot_id", "manual_label", "manual_reason", "manual_labeled_at")} for row in rows]
    compared = [row for row in labeled if row["auto_label"] is not None]
    confusion = Counter((str(row["auto_label"]), str(row["manual_label"])) for row in compared)
    disagreements = [row for row in compared if row["auto_label"] != row["manual_label"]]
    extractor_versions = sorted({str(row["classifier_version"]) for row in compared if row["classifier_version"]})
    return {"batch_id": batch_id, "selection_version": batch["selection_hash"], "created_at": batch["created_at"], "filters": json.loads(batch["filters_json"]), "query_specs": json.loads(batch["query_specs_json"]), "label_set_version": _hash(label_set), "sample": {"requested": batch["requested_size"], "selected": len(rows), "seed": batch["sample_seed"], "labels": dict(sorted(labels.items())), "labeled_for_binary_metrics": len(labeled)}, "union": {"positive": len(positives), "precision": len(positives) / len(labeled) if labeled else None, "recall_in_pilot": 1.0 if positives else None, "recall_note": "Recall is relative to manually labeled positives in this query-selected pilot; SQLite corpus has no unqueried control sample."}, "relevance_extractor": {"versions": extractor_versions, "compared": len(compared), "disagreements": len(disagreements), "confusion": [{"auto_label": auto, "manual_label": manual, "count": count} for (auto, manual), count in sorted(confusion.items())], "evidence": [{"snapshot_id": row["snapshot_id"], "auto_label": row["auto_label"], "auto_score": row["auto_score"], "auto_reasons": json.loads(row["auto_reasons_json"]), "manual_label": row["manual_label"], "manual_reason": row["manual_reason"]} for row in disagreements]}, "query_families": family_metrics, "insufficient_strata": [{"stratum": json.loads(key), "selected": count, "minimum": min_per_stratum} for key, count in sorted(strata.items()) if count < min_per_stratum]}
