"""DB-only aggregate statistics for reproducible DA slices."""

from __future__ import annotations

from typing import Any

from .storage import Database


def vacancy_stats(
    database: Database, *, snapshot_scope: str = "latest", run_ids: list[int] | None = None,
    area_ids: list[str] | None = None, relevance: str | None = None,
    query_family: str | None = None, date_from: str | None = None, date_to: str | None = None,
) -> dict[str, Any]:
    """Return stable aggregate counts from stored snapshots, with no side effects."""
    if snapshot_scope not in {"latest", "all"}:
        raise ValueError("snapshot_scope must be 'latest' or 'all'")
    clauses: list[str] = []
    values: list[Any] = []
    joins = " LEFT JOIN effective_relevance_labels labels ON labels.snapshot_id = s.id"
    if run_ids:
        joins += " JOIN vacancy_snapshot_observations observations ON observations.snapshot_id = s.id"
        placeholders = ", ".join("?" for _ in run_ids)
        clauses.append(f"observations.run_id IN ({placeholders})")
        values.extend(run_ids)
    if area_ids:
        placeholders = ", ".join("?" for _ in area_ids)
        clauses.append(f"CAST(s.area_id AS TEXT) IN ({placeholders})")
        values.extend(area_ids)
    if relevance:
        clauses.append("labels.effective_label = ?")
        values.append(relevance)
    if query_family:
        clauses.append(
            "EXISTS (SELECT 1 FROM vacancy_query_hits hits JOIN search_queries queries "
            "ON queries.id = hits.query_id WHERE hits.vacancy_hh_id = s.vacancy_hh_id "
            "AND queries.query_group = ?)"
        )
        values.append(query_family)
    if date_from:
        clauses.append("substr(s.published_at, 1, 10) >= ?")
        values.append(date_from)
    if date_to:
        clauses.append("substr(s.published_at, 1, 10) <= ?")
        values.append(date_to)
    source = "latest_vacancy_snapshots" if snapshot_scope == "latest" else "vacancy_snapshots"
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    selected = (
        "SELECT DISTINCT s.id, s.vacancy_hh_id, s.source, s.published_at, "
        "COALESCE(labels.effective_label, 'unknown') AS effective_label "
        f"FROM {source} s{joins}{where}"
    )
    with database.connect() as connection:
        summary = connection.execute(
            "WITH selected AS (" + selected + ") "
            "SELECT COUNT(*) AS snapshots, COUNT(DISTINCT vacancy_hh_id) AS vacancies, "
            "MIN(substr(published_at, 1, 10)) AS first_publication_day, "
            "MAX(substr(published_at, 1, 10)) AS last_publication_day, "
            "SUM(effective_label = 'relevant') AS relevant, "
            "SUM(effective_label = 'borderline') AS borderline, "
            "SUM(effective_label = 'irrelevant') AS irrelevant, "
            "SUM(effective_label = 'unknown') AS unknown FROM selected",
            values,
        ).fetchone()
        by_source = connection.execute(
            "WITH selected AS (" + selected + ") "
            "SELECT source, COUNT(*) AS snapshots FROM selected GROUP BY source ORDER BY source",
            values,
        ).fetchall()
    result = dict(summary)
    for name in ("relevant", "borderline", "irrelevant", "unknown"):
        result[name] = int(result[name] or 0)
    result["by_source"] = {str(row["source"]): int(row["snapshots"]) for row in by_source}
    return result
