"""Offline analytical exports from SQLite source-of-truth data."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .storage import Database


VACANCY_FIELDS = (
    "snapshot_id", "hh_id", "observed_at", "last_seen_at", "source", "title",
    "description", "published_at", "published_at_source_offset", "created_at",
    "created_at_source_offset", "expires_at", "expires_at_source_offset", "archived",
    "employer_id", "employer_name", "employer_type", "area_id", "area_name",
    "federal_district", "federal_subject", "locality", "salary_from", "salary_to",
    "salary_currency", "salary_gross", "salary_frequency", "experience_id",
    "employment_id", "schedule_id", "work_formats", "roles", "industries",
    "key_skills", "languages", "department_id", "department_name", "vacancy_type_id",
    "completeness", "effective_label", "query_families",
)
SKILL_FIELDS = (
    "snapshot_id", "hh_id", "title", "published_at", "area_id", "area_name",
    "skill", "topic_family", "dictionary_version", "source", "matched_alias",
    "match_count", "extractor_version",
)
ROLE_FIELDS = ("snapshot_id", "hh_id", "title", "role_id", "role_name")
QUERY_HIT_FIELDS = ("run_id", "query_id", "query_group", "query_expression", "area_id", "date_from", "date_to", "hh_id", "page", "rank", "observed_at")


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
        "s.title, s.description_text AS description, s.published_at, s.published_at_source_offset, "
        "s.created_at, s.created_at_source_offset, s.expires_at, s.expires_at_source_offset, s.archived, "
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


def export_skills(database: Database, path: str | Path) -> int:
    """Write normalized latest vacancy-skill evidence CSV from SQLite only."""
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT snapshot_id, vacancy_hh_id AS hh_id, title, published_at, area_id, area_name, "
            "skill, topic_family, dictionary_version, source, matched_alias, match_count, extractor_version "
            "FROM vacancy_skill_matrix ORDER BY snapshot_id, skill, source, matched_alias"
        ).fetchall()
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SKILL_FIELDS)
        writer.writeheader()
        writer.writerows(dict(row) for row in rows)
    return len(rows)


def export_roles(database: Database, path: str | Path) -> int:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT s.id AS snapshot_id, s.vacancy_hh_id AS hh_id, s.title, r.role_id, r.role_name "
            "FROM latest_vacancy_snapshots s JOIN snapshot_roles r ON r.snapshot_id = s.id "
            "ORDER BY s.id, r.role_name"
        ).fetchall()
    return _write_csv(rows, ROLE_FIELDS, path)


def export_query_hits(database: Database, path: str | Path) -> int:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT h.run_id, h.query_id, q.query_group, q.expression AS query_expression, h.area_id, "
            "h.date_from, h.date_to, h.vacancy_hh_id AS hh_id, h.page, h.rank, h.observed_at "
            "FROM vacancy_query_hits h JOIN search_queries q ON q.id = h.query_id "
            "ORDER BY h.run_id, h.query_id, h.area_id, h.page, h.rank, h.vacancy_hh_id"
        ).fetchall()
    return _write_csv(rows, QUERY_HIT_FIELDS, path)


def _write_csv(rows: list[Any], fields: tuple[str, ...], path: str | Path) -> int:
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(dict(row) for row in rows)
    return len(rows)
