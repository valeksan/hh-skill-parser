"""Offline analytical exports from SQLite source-of-truth data."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .storage import Database


VACANCY_FIELDS = (
    "snapshot_id", "hh_id", "observed_at", "last_seen_at", "source", "title",
    "description", "published_at", "created_at", "expires_at", "archived",
    "employer_id", "employer_name", "employer_type", "area_id", "area_name",
    "federal_district", "federal_subject", "locality", "salary_from", "salary_to",
    "salary_currency", "salary_gross", "salary_frequency", "experience_id",
    "employment_id", "schedule_id", "work_formats", "roles", "industries",
    "key_skills", "languages", "department_id", "department_name", "vacancy_type_id",
    "completeness", "effective_label", "query_families",
)


def export_vacancies(
    database: Database, path: str | Path, *, snapshot_scope: str = "latest",
    run_ids: list[int] | None = None, area_ids: list[str] | None = None,
    relevance: str | None = None, query_family: str | None = None,
    date_from: str | None = None, date_to: str | None = None,
) -> int:
    """Write stable CSV from SQLite. No collector, extractor, or network activity."""
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
    query = (
        "SELECT DISTINCT s.id AS snapshot_id, s.vacancy_hh_id AS hh_id, s.observed_at, s.last_seen_at, s.source, "
        "s.title, s.description_text AS description, s.published_at, s.created_at, s.expires_at, s.archived, "
        "s.employer_id, s.employer_name, s.employer_type, s.area_id, s.area_name, s.federal_district, "
        "s.federal_subject, s.locality, s.salary_from, s.salary_to, s.salary_currency, s.salary_gross, "
        "s.salary_frequency, s.experience_id, s.employment_id, s.schedule_id, s.work_format_json AS work_formats, "
        "s.roles_json AS roles, s.industries_json AS industries, s.key_skills_json AS key_skills, "
        "s.languages_json AS languages, s.department_id, s.department_name, s.vacancy_type_id, "
        "s.completeness_json AS completeness, labels.effective_label, "
        "COALESCE((SELECT group_concat(query_group, '|') FROM (SELECT DISTINCT COALESCE(queries.query_group, '') AS query_group "
        "FROM vacancy_query_hits hits JOIN search_queries queries ON queries.id = hits.query_id "
        "WHERE hits.vacancy_hh_id = s.vacancy_hh_id ORDER BY query_group)), '') AS query_families "
        f"FROM {source} s{joins}{where} ORDER BY s.id"
    )
    with database.connect() as connection:
        rows = connection.execute(query, values).fetchall()
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=VACANCY_FIELDS)
        writer.writeheader()
        writer.writerows(dict(row) for row in rows)
    return len(rows)
