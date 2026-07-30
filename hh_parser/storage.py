"""SQLite storage for reproducible HH collection runs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    """Return UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


def json_value(value: Any) -> str:
    """Serialize deterministic JSON suitable for SQLite text columns."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Database:
    """Small transactional repository. Every write is safe to repeat."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5_000, wal: bool = True):
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self.wal = wal

    def connect(self) -> sqlite3.Connection:
        """Open configured SQLite connection."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        if self.wal:
            connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Open one explicit transaction; rollback on any exception."""
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def migrate(self) -> None:
        """Apply packaged migrations exactly once, in filename order."""
        migrations_dir = Path(__file__).with_name("migrations")
        with self.transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {
                row["version"]
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            for migration in sorted(migrations_dir.glob("*.sql")):
                if migration.name in applied:
                    continue
                connection.executescript(migration.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (migration.name, utc_now()),
                )

    def start_run(
        self,
        config: dict[str, Any],
        *,
        source_policy: str | None = None,
        collection_mode: str = "incremental",
        app_version: str | None = None,
        git_commit: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        """Create immutable run metadata and return its ID."""
        if collection_mode not in {"incremental", "full"}:
            raise ValueError("collection_mode must be 'incremental' or 'full'")
        config_json = json_value(config)
        config_hash = hashlib.sha256(config_json.encode("utf-8")).hexdigest()
        values = (
            utc_now(), "running", app_version, git_commit, source_policy,
            collection_mode, config_json, config_hash,
        )
        sql = (
            "INSERT INTO collection_runs("
            "started_at, status, app_version, git_commit, source_policy, collection_mode, "
            "config_json, config_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        )
        if connection is not None:
            return int(connection.execute(sql, values).lastrowid)
        with self.transaction() as tx:
            return int(tx.execute(sql, values).lastrowid)

    def finish_run(
        self,
        run_id: int,
        status: str,
        counters: dict[str, int] | None = None,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """Close run and persist collection counters."""
        allowed = {"completed", "degraded", "failed", "cancelled"}
        if status not in allowed:
            raise ValueError(f"status must be one of {sorted(allowed)}")
        counters = counters or {}
        columns = {
            "found": "found_count", "unique": "unique_count", "loaded": "loaded_count",
            "rejected": "rejected_count", "errors": "error_count",
        }
        assignments = ["finished_at = ?", "status = ?"]
        values: list[Any] = [utc_now(), status]
        for name, column in columns.items():
            if name in counters:
                assignments.append(f"{column} = ?")
                values.append(counters[name])
        values.append(run_id)
        sql = f"UPDATE collection_runs SET {', '.join(assignments)} WHERE id = ?"
        if connection is not None:
            connection.execute(sql, values)
            return
        with self.transaction() as tx:
            tx.execute(sql, values)

    def prepare_run_resume(
        self, run_id: int, *, connection: sqlite3.Connection | None = None
    ) -> None:
        """Reopen an existing finite run without changing its frozen scope."""
        sql = (
            "UPDATE collection_runs SET status = 'running', finished_at = NULL "
            "WHERE id = ? AND status IN ('running', 'degraded', 'failed', 'cancelled')"
        )
        if connection is not None:
            cursor = connection.execute(sql, (run_id,))
            if cursor.rowcount != 1:
                raise ValueError(f"run {run_id} is missing or already completed")
            return
        with self.transaction() as tx:
            self.prepare_run_resume(run_id, connection=tx)

    def upsert_query(
        self,
        expression: str,
        *,
        version: str = "1",
        query_group: str | None = None,
        purpose: str | None = None,
        enabled: bool = True,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        """Insert/update query specification and return its ID."""
        normalized = " ".join(expression.split())
        values = (expression, normalized, query_group, purpose, int(enabled), version)
        sql = (
            "INSERT INTO search_queries(expression, normalized_expression, query_group, purpose, enabled, version) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(normalized_expression, version) DO UPDATE SET "
            "expression=excluded.expression, query_group=excluded.query_group, "
            "purpose=excluded.purpose, enabled=excluded.enabled "
            "RETURNING id"
        )
        if connection is not None:
            return int(connection.execute(sql, values).fetchone()[0])
        with self.transaction() as tx:
            return int(tx.execute(sql, values).fetchone()[0])

    def upsert_vacancy(
        self,
        hh_id: str | int,
        *,
        source: str,
        alternate_url: str | None = None,
        seen_at: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """Create stable vacancy entity or update latest observation."""
        seen_at = seen_at or utc_now()
        values = (str(hh_id), alternate_url, seen_at, seen_at, source, source)
        sql = (
            "INSERT INTO vacancies(hh_id, alternate_url, first_seen_at, last_seen_at, first_source, latest_source) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(hh_id) DO UPDATE SET "
            "alternate_url=COALESCE(excluded.alternate_url, vacancies.alternate_url), "
            "last_seen_at=excluded.last_seen_at, latest_source=excluded.latest_source"
        )
        if connection is not None:
            connection.execute(sql, values)
            return
        with self.transaction() as tx:
            tx.execute(sql, values)

    def record_search_page(
        self, run_id: int, query_id: int, *, page: int, area_id: int | None = None,
        date_from: str | None = None, date_to: str | None = None,
        request_url: str | None = None, request_params: dict[str, Any] | None = None,
        http_status: int | None = None, result_count: int | None = None,
        is_last_page: bool = False, error_type: str | None = None,
        error_message: str | None = None, connection: sqlite3.Connection | None = None,
    ) -> None:
        """Idempotently record one retrieved or failed search-result page."""
        values = (run_id, query_id, area_id if area_id is not None else -1,
                  date_from or "", date_to or "", page, request_url,
                  json_value(request_params or {}), utc_now(), http_status, result_count,
                  int(is_last_page), error_type, error_message)
        sql = (
            "INSERT INTO search_pages(run_id, query_id, area_id, date_from, date_to, page, request_url, "
            "request_params_json, requested_at, http_status, result_count, is_last_page, error_type, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id, query_id, area_id, date_from, date_to, page) DO UPDATE SET "
            "requested_at=excluded.requested_at, request_url=excluded.request_url, "
            "request_params_json=excluded.request_params_json, http_status=excluded.http_status, "
            "result_count=excluded.result_count, is_last_page=excluded.is_last_page, "
            "error_type=excluded.error_type, error_message=excluded.error_message"
        )
        self._execute(sql, values, connection)

    def record_query_hit(
        self, run_id: int, query_id: int, vacancy_hh_id: str | int, *,
        area_id: int | None = None, date_from: str | None = None, date_to: str | None = None,
        page: int | None = None, rank: int | None = None, observed_at: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """Record query-to-vacancy relationship exactly once per run/window."""
        values = (run_id, query_id, area_id if area_id is not None else -1,
                  date_from or "", date_to or "", str(vacancy_hh_id), page,
                  rank, observed_at or utc_now())
        sql = (
            "INSERT INTO vacancy_query_hits(run_id, query_id, area_id, date_from, date_to, vacancy_hh_id, page, rank, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id, query_id, area_id, date_from, date_to, vacancy_hh_id) DO UPDATE SET "
            "page=excluded.page, rank=excluded.rank, observed_at=excluded.observed_at"
        )
        self._execute(sql, values, connection)

    def record_snapshot(
        self, run_id: int, vacancy_hh_id: str | int, snapshot: dict[str, Any], *,
        connection: sqlite3.Connection | None = None,
    ) -> bool:
        """Store sanitized snapshot. Return True only when content is new."""
        required = {"content_hash", "title", "source"}
        missing = required - snapshot.keys()
        if missing:
            raise ValueError(f"snapshot missing required fields: {sorted(missing)}")
        observed_at = snapshot.get("observed_at", utc_now())
        values = (
            str(vacancy_hh_id), run_id, observed_at,
            snapshot["content_hash"], snapshot["title"], snapshot.get("description_html"),
            snapshot.get("description_text"), snapshot.get("published_at"), snapshot.get("created_at"),
            snapshot.get("expires_at"), snapshot.get("archived"), snapshot.get("employer_id"),
            snapshot.get("employer_name"), snapshot.get("area_id"), snapshot.get("area_name"),
            snapshot.get("federal_district"), snapshot.get("federal_subject"), snapshot.get("locality"),
            snapshot.get("salary_from"), snapshot.get("salary_to"), snapshot.get("salary_currency"),
            snapshot.get("salary_gross"), snapshot.get("salary_frequency"), snapshot["source"],
            json_value(snapshot.get("completeness", {})), snapshot.get("raw_payload"),
            snapshot.get("raw_content_type"), snapshot.get("raw_compression"), snapshot.get("raw_size"),
            snapshot.get("raw_hash"), int(snapshot.get("redaction_applied", False)),
            snapshot.get("redaction_version"), json_value(snapshot.get("redaction_types", [])),
            observed_at,
            snapshot.get("employer_type"), snapshot.get("employer_trusted"),
            snapshot.get("employer_accredited_it"), snapshot.get("experience_id"),
            snapshot.get("employment_id"), snapshot.get("schedule_id"),
            json_value(snapshot.get("work_formats", [])), json_value(snapshot.get("roles", [])),
            json_value(snapshot.get("industries", [])), json_value(snapshot.get("key_skills", [])),
            json_value(snapshot.get("languages", [])), snapshot.get("department_id"),
            snapshot.get("department_name"), snapshot.get("vacancy_type_id"),
        )
        sql = (
            "INSERT INTO vacancy_snapshots(vacancy_hh_id, run_id, observed_at, content_hash, title, description_html, "
            "description_text, published_at, created_at, expires_at, archived, employer_id, employer_name, area_id, "
            "area_name, federal_district, federal_subject, locality, salary_from, salary_to, salary_currency, salary_gross, "
            "salary_frequency, source, completeness_json, raw_payload, raw_content_type, raw_compression, raw_size, raw_hash, "
            "redaction_applied, redaction_version, redaction_types_json, last_seen_at, employer_type, "
            "employer_trusted, employer_accredited_it, experience_id, employment_id, schedule_id, "
            "work_format_json, roles_json, industries_json, key_skills_json, languages_json, department_id, "
            "department_name, vacancy_type_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(vacancy_hh_id, content_hash) DO NOTHING"
        )
        def store(tx: sqlite3.Connection) -> bool:
            inserted = tx.execute(sql, values).rowcount == 1
            row = tx.execute(
                "SELECT id FROM vacancy_snapshots WHERE vacancy_hh_id = ? AND content_hash = ?",
                (str(vacancy_hh_id), snapshot["content_hash"]),
            ).fetchone()
            snapshot_id = int(row["id"])
            tx.execute(
                "UPDATE vacancy_snapshots SET last_seen_at = ? WHERE id = ?",
                (observed_at, snapshot_id),
            )
            tx.execute(
                "INSERT INTO vacancy_snapshot_observations(run_id, vacancy_hh_id, snapshot_id, observed_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(run_id, snapshot_id) DO UPDATE SET "
                "observed_at = excluded.observed_at",
                (run_id, str(vacancy_hh_id), snapshot_id, observed_at),
            )
            self._store_snapshot_links(tx, snapshot_id, snapshot)
            return inserted
        if connection is not None:
            return store(connection)
        with self.transaction() as tx:
            return store(tx)

    @staticmethod
    def _store_snapshot_links(
        connection: sqlite3.Connection, snapshot_id: int, snapshot: dict[str, Any],
    ) -> None:
        """Store analytical many-value links without discarding snapshot JSON."""
        for skill in snapshot.get("key_skills", []):
            name = skill.get("name") if isinstance(skill, dict) else None
            if name:
                connection.execute(
                    "INSERT INTO snapshot_key_skills(snapshot_id, skill_name) VALUES (?, ?) "
                    "ON CONFLICT(snapshot_id, skill_name) DO NOTHING",
                    (snapshot_id, str(name)),
                )
        for role in snapshot.get("roles", []):
            name = role.get("name") if isinstance(role, dict) else None
            if name:
                connection.execute(
                    "INSERT INTO snapshot_roles(snapshot_id, role_id, role_name) VALUES (?, ?, ?) "
                    "ON CONFLICT(snapshot_id, role_name) DO UPDATE SET role_id = excluded.role_id",
                    (snapshot_id, role.get("id"), str(name)),
                )
        for industry in snapshot.get("industries", []):
            name = industry.get("name") if isinstance(industry, dict) else None
            if name:
                connection.execute(
                    "INSERT INTO snapshot_industries(snapshot_id, industry_id, industry_name) VALUES (?, ?, ?) "
                    "ON CONFLICT(snapshot_id, industry_name) DO UPDATE SET industry_id = excluded.industry_id",
                    (snapshot_id, industry.get("id"), str(name)),
                )

    def record_error(
        self, run_id: int, stage: str, error_type: str, message: str, *,
        query_id: int | None = None, area_id: int | None = None,
        vacancy_hh_id: str | int | None = None, http_status: int | None = None,
        attempt: int = 1, connection: sqlite3.Connection | None = None,
    ) -> None:
        """Append auditable collection error."""
        values = (run_id, stage, query_id, area_id,
                  str(vacancy_hh_id) if vacancy_hh_id is not None else None,
                  error_type, http_status, message, attempt, utc_now())
        self._execute(
            "INSERT INTO collection_errors(run_id, stage, query_id, area_id, vacancy_hh_id, error_type, "
            "http_status, message, attempt, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values, connection,
        )

    def resolve_errors(
        self, run_id: int, stage: str, *, query_id: int | None = None,
        area_id: int | None = None, vacancy_hh_id: str | int | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """Mark matching retryable failures resolved after successful work."""
        clauses = ["run_id = ?", "stage = ?", "resolved_at IS NULL"]
        values: list[Any] = [run_id, stage]
        for column, value in (
            ("query_id", query_id), ("area_id", area_id),
            ("vacancy_hh_id", str(vacancy_hh_id) if vacancy_hh_id is not None else None),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                values.append(value)
        sql = f"UPDATE collection_errors SET resolved_at = ? WHERE {' AND '.join(clauses)}"
        self._execute(sql, (utc_now(), *values), connection)

    def search_page_succeeded(
        self, run_id: int, query_id: int, *, area_id: int | None = None,
        page: int = 0, date_from: str | None = None, date_to: str | None = None,
    ) -> bool:
        """Return whether one search work unit was durably completed."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM search_pages WHERE run_id = ? AND query_id = ? AND area_id = ? "
                "AND date_from = ? AND date_to = ? AND page = ? "
                "AND http_status BETWEEN 200 AND 299 AND error_type IS NULL",
                (run_id, query_id, area_id if area_id is not None else -1,
                 date_from or "", date_to or "", page),
            ).fetchone()
        return row is not None

    def load_query_hits(
        self, run_id: int, query_id: int, *, area_id: int | None = None,
        date_from: str | None = None, date_to: str | None = None,
        page: int | None = None,
    ) -> list[dict[str, Any]]:
        """Rebuild minimal detail work items from durable query hits."""
        with self.connect() as connection:
            clauses = [
                "h.run_id = ?", "h.query_id = ?", "h.area_id = ?",
                "h.date_from = ?", "h.date_to = ?",
            ]
            values: list[Any] = [
                run_id, query_id, area_id if area_id is not None else -1,
                date_from or "", date_to or "",
            ]
            if page is not None:
                clauses.append("h.page = ?")
                values.append(page)
            rows = connection.execute(
                "SELECT h.vacancy_hh_id AS id, h.rank, v.alternate_url, v.latest_source AS _source "
                "FROM vacancy_query_hits h JOIN vacancies v ON v.hh_id = h.vacancy_hh_id "
                f"WHERE {' AND '.join(clauses)} ORDER BY h.rank, h.vacancy_hh_id",
                values,
            ).fetchall()
        return [dict(row) for row in rows]

    def observed_vacancy_ids(self, run_id: int) -> set[str]:
        """Return cards already normalized successfully in this run."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT vacancy_hh_id FROM vacancy_snapshot_observations WHERE run_id = ?",
                (run_id,),
            ).fetchall()
        return {str(row["vacancy_hh_id"]) for row in rows}

    def get_run_areas(self, run_id: int) -> list[str]:
        """Load immutable area worklist for resume."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT area_hh_id FROM run_areas WHERE run_id = ? ORDER BY rowid",
                (run_id,),
            ).fetchall()
        if not rows:
            raise ValueError(f"run {run_id} has no frozen areas")
        return [str(row["area_hh_id"]) for row in rows]

    def run_counters(self, run_id: int) -> dict[str, int]:
        """Calculate durable run counters, including only unresolved errors."""
        with self.connect() as connection:
            found = connection.execute(
                "SELECT COUNT(*) FROM vacancy_query_hits WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            unique = connection.execute(
                "SELECT COUNT(DISTINCT vacancy_hh_id) FROM vacancy_query_hits WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            loaded = connection.execute(
                "SELECT COUNT(DISTINCT vacancy_hh_id) FROM vacancy_snapshot_observations WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            errors = connection.execute(
                "SELECT COUNT(*) FROM collection_errors WHERE run_id = ? AND resolved_at IS NULL",
                (run_id,),
            ).fetchone()[0]
        return {"found": found, "unique": unique, "loaded": loaded, "errors": errors}

    def store_area_catalog(
        self, tree: list[dict[str, Any]], *, source_url: str, host: str = "hh.ru",
        locale: str = "RU", connection: sqlite3.Connection | None = None,
    ) -> int:
        """Store one immutable, coordinate-free `/areas` catalog snapshot."""
        from .areas import flatten_area_tree

        payload_hash = hashlib.sha256(json_value(tree).encode("utf-8")).hexdigest()
        catalog = flatten_area_tree(tree)
        sql = (
            "INSERT INTO area_catalog_versions(source_url, host, locale, fetched_at, payload_hash) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(payload_hash) DO UPDATE SET "
            "source_url=excluded.source_url, host=excluded.host, locale=excluded.locale "
            "RETURNING id"
        )
        def store(tx: sqlite3.Connection) -> int:
            catalog_id = int(tx.execute(sql, (source_url, host, locale, utc_now(), payload_hash)).fetchone()[0])
            for area in catalog.values():
                tx.execute(
                    "INSERT INTO areas(catalog_version_id, hh_id, parent_hh_id, name, depth) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(catalog_version_id, hh_id) DO UPDATE SET "
                    "parent_hh_id=excluded.parent_hh_id, name=excluded.name, depth=excluded.depth",
                    (catalog_id, area.hh_id, area.parent_id, area.name, area.depth),
                )
            return catalog_id
        if connection is not None:
            return store(connection)
        with self.transaction() as tx:
            return store(tx)

    def load_area_catalog(self, catalog_version_id: int | None = None) -> tuple[int, dict[str, Any]]:
        """Return latest or requested catalog as flat area rows."""
        with self.connect() as connection:
            if catalog_version_id is None:
                row = connection.execute("SELECT id FROM area_catalog_versions ORDER BY id DESC LIMIT 1").fetchone()
                if row is None:
                    raise ValueError("area catalog is empty; run `areas sync` first")
                catalog_version_id = int(row["id"])
            rows = connection.execute(
                "SELECT hh_id, parent_hh_id, name, depth FROM areas WHERE catalog_version_id = ?",
                (catalog_version_id,),
            ).fetchall()
        from .areas import Area
        return catalog_version_id, {
            row["hh_id"]: Area(row["hh_id"], row["name"], row["parent_hh_id"], row["depth"])
            for row in rows
        }

    def set_run_areas(
        self, run_id: int, area_ids: list[str], *, catalog_version_id: int | None,
        selection_source: str, connection: sqlite3.Connection | None = None,
    ) -> None:
        """Freeze selected work areas for deterministic resume."""
        def store(tx: sqlite3.Connection) -> None:
            for area_id in area_ids:
                tx.execute(
                    "INSERT INTO run_areas(run_id, area_hh_id, catalog_version_id, selection_source) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(run_id, area_hh_id) DO NOTHING",
                    (run_id, str(area_id), catalog_version_id, selection_source),
                )
        if connection is not None:
            store(connection)
            return
        with self.transaction() as tx:
            store(tx)

    def _execute(self, sql: str, values: tuple[Any, ...], connection: sqlite3.Connection | None) -> None:
        if connection is not None:
            connection.execute(sql, values)
            return
        with self.transaction() as tx:
            tx.execute(sql, values)
