"""Offline analytical exports from SQLite source-of-truth data."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from .storage import Database
from .privacy import safe_employer_name


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
        for row in rows:
            exported = dict(row)
            exported["employer_name"] = safe_employer_name(
                exported["employer_id"], exported["employer_type"], exported["employer_name"],
            )
            writer.writerow(exported)
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


def export_marts(
    database: Database, output_dir: str | Path, *, snapshot_scope: str = "latest",
    run_ids: list[int] | None = None, area_ids: list[str] | None = None,
    relevance: str | None = None, query_family: str | None = None,
    date_from: str | None = None, date_to: str | None = None, parquet: bool = False,
) -> dict[str, Any]:
    """Build complete offline DA mart bundle plus reproducibility manifest."""
    if snapshot_scope not in {"latest", "all"}:
        raise ValueError("snapshot_scope must be 'latest' or 'all'")
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    clauses, values = _scope_clauses(run_ids, area_ids, relevance, query_family, date_from, date_to)
    source = "latest_vacancy_snapshots" if snapshot_scope == "latest" else "vacancy_snapshots"
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    selected = (
        "SELECT DISTINCT s.*, COALESCE(labels.effective_label, 'unknown') AS effective_label "
        f"FROM {source} s LEFT JOIN effective_relevance_labels labels ON labels.snapshot_id=s.id"
        + (" JOIN vacancy_snapshot_observations observations ON observations.snapshot_id=s.id" if run_ids else "")
        + where
    )
    marts = {
        "publication_trends": "SELECT COALESCE(substr(published_at,1,10),substr(observed_at,1,10)) AS publication_day, effective_label, COUNT(DISTINCT vacancy_hh_id) AS vacancy_count FROM selected GROUP BY 1,2 ORDER BY 1,2",
        "geography": "SELECT federal_district, federal_subject, locality, area_id, area_name, effective_label, COUNT(DISTINCT vacancy_hh_id) AS vacancy_count FROM selected GROUP BY 1,2,3,4,5,6 ORDER BY 1,2,3,4,6",
        "employers": "SELECT employer_id, employer_name, employer_type, effective_label, COUNT(DISTINCT vacancy_hh_id) AS vacancy_count FROM selected GROUP BY 1,2,3,4 ORDER BY vacancy_count DESC, employer_id",
        "industries": "SELECT json_extract(value, '$.id') AS industry_id, json_extract(value, '$.name') AS industry_name, effective_label, COUNT(DISTINCT vacancy_hh_id) AS vacancy_count FROM selected, json_each(COALESCE(industries_json, '[]')) GROUP BY 1,2,3 ORDER BY vacancy_count DESC, industry_name",
        "topics_skills": "SELECT k.topic_family, k.canonical_name AS skill, selected.effective_label, COUNT(DISTINCT selected.vacancy_hh_id) AS vacancy_count FROM selected JOIN vacancy_skills vs ON vs.snapshot_id=selected.id JOIN skills k ON k.id=vs.skill_id GROUP BY 1,2,3 ORDER BY vacancy_count DESC, skill",
        "skill_cooccurrence": "SELECT a.canonical_name AS skill_a, b.canonical_name AS skill_b, COUNT(DISTINCT selected.vacancy_hh_id) AS vacancy_count FROM selected JOIN vacancy_skills va ON va.snapshot_id=selected.id JOIN skills a ON a.id=va.skill_id JOIN vacancy_skills vb ON vb.snapshot_id=selected.id JOIN skills b ON b.id=vb.skill_id WHERE a.canonical_name < b.canonical_name GROUP BY 1,2 ORDER BY vacancy_count DESC, skill_a, skill_b",
        "salary": "SELECT salary_currency, salary_frequency, salary_gross, effective_label, COUNT(*) AS vacancy_count, AVG(CASE WHEN salary_from IS NOT NULL AND salary_to IS NOT NULL THEN (salary_from+salary_to)/2.0 ELSE COALESCE(salary_from,salary_to) END) AS salary_midpoint_avg FROM selected GROUP BY 1,2,3,4 ORDER BY 1,2,3,4",
        "employment": "SELECT experience_id, employment_id, schedule_id, work_format_json, effective_label, COUNT(DISTINCT vacancy_hh_id) AS vacancy_count FROM selected GROUP BY 1,2,3,4,5 ORDER BY vacancy_count DESC",
        "edits": "SELECT h.history_event, COUNT(*) AS snapshot_count, COUNT(DISTINCT h.vacancy_hh_id) AS vacancy_count FROM vacancy_history h JOIN selected ON selected.id=h.id GROUP BY 1 ORDER BY 1",
        "reposts": "SELECT g.repost_key, g.key_version, g.publication_count, g.first_observed_at, g.last_observed_at FROM repost_groups g JOIN snapshot_repost_keys k ON k.repost_key=g.repost_key AND k.key_version=g.key_version JOIN selected ON selected.id=k.snapshot_id GROUP BY 1,2,3,4,5 ORDER BY g.publication_count DESC, g.repost_key",
        "missing_data": "SELECT 'published_at' AS field, SUM(published_at IS NULL) AS missing_count, COUNT(*) AS total_count FROM selected UNION ALL SELECT 'area_id',SUM(area_id IS NULL),COUNT(*) FROM selected UNION ALL SELECT 'salary',SUM(salary_from IS NULL AND salary_to IS NULL),COUNT(*) FROM selected UNION ALL SELECT 'employer_id',SUM(employer_id IS NULL),COUNT(*) FROM selected",
        "query_noise": "SELECT q.query_group, q.id AS query_id, q.expression, COUNT(DISTINCT h.vacancy_hh_id) AS hit_vacancies, SUM(selected.effective_label='relevant') AS relevant_vacancies, SUM(selected.effective_label='irrelevant') AS irrelevant_vacancies FROM vacancy_query_hits h JOIN search_queries q ON q.id=h.query_id JOIN selected ON selected.vacancy_hh_id=h.vacancy_hh_id GROUP BY 1,2,3 ORDER BY hit_vacancies DESC, query_id",
        "coverage_errors": "SELECT r.id AS run_id,r.status,r.found_count,r.unique_count,r.loaded_count,r.error_count,COUNT(e.id) AS persisted_errors FROM collection_runs r LEFT JOIN collection_errors e ON e.run_id=r.id GROUP BY 1,2,3,4,5,6 ORDER BY r.id",
    }
    outputs: dict[str, dict[str, Any]] = {}
    with database.connect() as connection:
        connection.execute("CREATE TEMP TABLE selected AS " + selected, values)
        for name, query in marts.items():
            rows = [dict(row) for row in connection.execute(query)]
            path = directory / f"{name}.csv"
            _write_csv(rows, tuple(rows[0]) if rows else _fields_for_query(connection, query), path)
            item: dict[str, Any] = {"rows": len(rows), "csv": path.name, "sha256": _sha256(path)}
            if parquet:
                parquet_path = path.with_suffix(".parquet")
                _write_parquet(rows, parquet_path)
                item.update({"parquet": parquet_path.name, "parquet_sha256": _sha256(parquet_path)})
            outputs[name] = item
        legacy_rows = [dict(row) for row in connection.execute(
            "SELECT COUNT(DISTINCT selected.vacancy_hh_id) AS Count, k.canonical_name AS Skill "
            "FROM selected JOIN vacancy_skills vs ON vs.snapshot_id=selected.id "
            "JOIN skills k ON k.id=vs.skill_id GROUP BY k.canonical_name ORDER BY Count DESC, Skill"
        )]
        legacy_path = directory / "top_skills_rf.csv"
        _write_csv(legacy_rows, ("Count", "Skill"), legacy_path)
        outputs["top_skills_rf"] = {"rows": len(legacy_rows), "csv": legacy_path.name, "sha256": _sha256(legacy_path)}
        if parquet:
            legacy_parquet = legacy_path.with_suffix(".parquet")
            _write_parquet(legacy_rows, legacy_parquet)
            outputs["top_skills_rf"].update({"parquet": legacy_parquet.name, "parquet_sha256": _sha256(legacy_parquet)})
        schema_versions = [row["version"] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
        dictionaries = [row["dictionary_version"] for row in connection.execute("SELECT DISTINCT dictionary_version FROM skills ORDER BY dictionary_version")]
        runs = [dict(row) for row in connection.execute("SELECT id, config_hash, config_json FROM collection_runs WHERE id IN (SELECT DISTINCT run_id FROM vacancy_snapshot_observations WHERE snapshot_id IN (SELECT id FROM selected)) ORDER BY id")]
    manifest = {"generated_at": datetime.now(timezone.utc).isoformat(), "database": str(database.path), "schema_versions": schema_versions, "filters": {"snapshot": snapshot_scope, "run_ids": run_ids or [], "area_ids": area_ids or [], "relevance": relevance, "query_family": query_family, "date_from": date_from, "date_to": date_to}, "runs": runs, "dictionary_versions": dictionaries, "outputs": outputs}
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"outputs": outputs, "manifest": str(directory / "manifest.json")}


def _scope_clauses(run_ids: list[int] | None, area_ids: list[str] | None, relevance: str | None, query_family: str | None, date_from: str | None, date_to: str | None) -> tuple[list[str], list[Any]]:
    clauses, values = [], []
    if run_ids:
        clauses.append("observations.run_id IN (" + ", ".join("?" for _ in run_ids) + ")"); values.extend(run_ids)
    if area_ids:
        clauses.append("CAST(s.area_id AS TEXT) IN (" + ", ".join("?" for _ in area_ids) + ")"); values.extend(area_ids)
    if relevance: clauses.append("labels.effective_label = ?"); values.append(relevance)
    if query_family:
        clauses.append("EXISTS (SELECT 1 FROM vacancy_query_hits h JOIN search_queries q ON q.id=h.query_id WHERE h.vacancy_hh_id=s.vacancy_hh_id AND q.query_group=?)"); values.append(query_family)
    if date_from: clauses.append("substr(s.published_at,1,10) >= ?"); values.append(date_from)
    if date_to: clauses.append("substr(s.published_at,1,10) <= ?"); values.append(date_to)
    return clauses, values


def _fields_for_query(connection: Any, query: str) -> tuple[str, ...]:
    cursor = connection.execute(query + " LIMIT 0")
    return tuple(column[0] for column in cursor.description)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_parquet(rows: list[dict[str, Any]], path: Path) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as error:
        raise ValueError("Parquet export requires pyarrow; install with: pip install -e '.[parquet]'") from error
    pq.write_table(pa.Table.from_pylist(rows), path)
