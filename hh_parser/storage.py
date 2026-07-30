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
        values = (
            str(vacancy_hh_id), run_id, snapshot.get("observed_at", utc_now()),
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
        )
        sql = (
            "INSERT INTO vacancy_snapshots(vacancy_hh_id, run_id, observed_at, content_hash, title, description_html, "
            "description_text, published_at, created_at, expires_at, archived, employer_id, employer_name, area_id, "
            "area_name, federal_district, federal_subject, locality, salary_from, salary_to, salary_currency, salary_gross, "
            "salary_frequency, source, completeness_json, raw_payload, raw_content_type, raw_compression, raw_size, raw_hash, "
            "redaction_applied, redaction_version, redaction_types_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(vacancy_hh_id, content_hash) DO NOTHING"
        )
        if connection is not None:
            return connection.execute(sql, values).rowcount == 1
        with self.transaction() as tx:
            return tx.execute(sql, values).rowcount == 1

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

    def _execute(self, sql: str, values: tuple[Any, ...], connection: sqlite3.Connection | None) -> None:
        if connection is not None:
            connection.execute(sql, values)
            return
        with self.transaction() as tx:
            tx.execute(sql, values)
