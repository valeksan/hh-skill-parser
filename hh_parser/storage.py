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


SENSITIVE_CONFIG_KEY_PARTS = ("token", "secret", "password", "authorization", "api_key", "apikey")


def redact_config(value: Any) -> Any:
    """Copy config with credentials removed before durable storage or hashing."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            name = str(key).casefold().replace("-", "_")
            result[key] = "[redacted]" if any(part in name for part in SENSITIVE_CONFIG_KEY_PARTS) else redact_config(item)
        return result
    if isinstance(value, (list, tuple)):
        return [redact_config(item) for item in value]
    return value


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
        config_json = json_value(redact_config(config))
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
        expected_keys = {"found", "unique", "loaded", "errors"}
        if expected_keys.issubset(counters):
            persisted = self.run_counters(run_id)
            if {name: counters[name] for name in expected_keys} != persisted:
                raise ValueError("run counters do not match persisted collection rows")
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

    def collection_watermark(self, scope_hash: str) -> str | None:
        """Return latest fully covered date for one immutable collection scope."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT watermark_date FROM collection_watermarks WHERE scope_hash = ?",
                (scope_hash,),
            ).fetchone()
        return str(row["watermark_date"]) if row else None

    def advance_collection_watermark(
        self, scope_hash: str, scope: dict[str, Any], watermark_date: str, run_id: int,
        *, connection: sqlite3.Connection | None = None,
    ) -> None:
        """Advance, never rewind, successful coverage for a compatible scope."""
        sql = (
            "INSERT INTO collection_watermarks(scope_hash, scope_json, watermark_date, run_id, advanced_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(scope_hash) DO UPDATE SET "
            "scope_json=excluded.scope_json, watermark_date=excluded.watermark_date, "
            "run_id=excluded.run_id, advanced_at=excluded.advanced_at "
            "WHERE excluded.watermark_date > collection_watermarks.watermark_date"
        )
        self._execute(sql, (scope_hash, json_value(scope), watermark_date, run_id, utc_now()), connection)

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
        error_message: str | None = None, source: str = "api",
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """Idempotently record one retrieved or failed search-result page."""
        values = (run_id, query_id, area_id if area_id is not None else -1,
                  date_from or "", date_to or "", page, request_url,
                  json_value(request_params or {}), utc_now(), http_status, result_count,
                  int(is_last_page), error_type, error_message, source)
        sql = (
            "INSERT INTO search_pages(run_id, query_id, area_id, date_from, date_to, page, request_url, "
            "request_params_json, requested_at, http_status, result_count, is_last_page, error_type, error_message, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id, query_id, area_id, date_from, date_to, page) DO UPDATE SET "
            "requested_at=excluded.requested_at, request_url=excluded.request_url, "
            "request_params_json=excluded.request_params_json, http_status=excluded.http_status, "
            "result_count=excluded.result_count, is_last_page=excluded.is_last_page, "
            "error_type=excluded.error_type, error_message=excluded.error_message, source=excluded.source"
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
            snapshot.get("description_text"), snapshot.get("published_at"), snapshot.get("published_at_source_offset"),
            snapshot.get("created_at"), snapshot.get("created_at_source_offset"), snapshot.get("expires_at"),
            snapshot.get("expires_at_source_offset"), snapshot.get("archived"), snapshot.get("employer_id"),
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
            "description_text, published_at, published_at_source_offset, created_at, created_at_source_offset, "
            "expires_at, expires_at_source_offset, archived, employer_id, employer_name, area_id, "
            "area_name, federal_district, federal_subject, locality, salary_from, salary_to, salary_currency, salary_gross, "
            "salary_frequency, source, completeness_json, raw_payload, raw_content_type, raw_compression, raw_size, raw_hash, "
            "redaction_applied, redaction_version, redaction_types_json, last_seen_at, employer_type, "
            "employer_trusted, employer_accredited_it, experience_id, employment_id, schedule_id, "
            "work_format_json, roles_json, industries_json, key_skills_json, languages_json, department_id, "
            "department_name, vacancy_type_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
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

    def record_vacancy_request(
        self, run_id: int, vacancy_hh_id: str | int, *, source: str, http_status: int | None,
        error_type: str | None = None, error_message: str | None = None,
        reason_code: str | None = None, connection: sqlite3.Connection | None = None,
    ) -> None:
        """Append one card transport outcome, independent from snapshot persistence."""
        reason_code = reason_code or (self.error_reason(error_type, http_status) if error_type else "success")
        self._execute(
            "INSERT INTO vacancy_requests(run_id, vacancy_hh_id, source, requested_at, http_status, "
            "error_type, error_message, reason_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, str(vacancy_hh_id), source, utc_now(), http_status, error_type, error_message, reason_code),
            connection,
        )

    def counter_reconciliation(self, run_id: int) -> dict[str, Any]:
        """Compare frozen run counters with current persisted collection rows."""
        persisted = self.run_counters(run_id)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT found_count, unique_count, loaded_count, error_count FROM collection_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"run {run_id} does not exist")
        recorded = {
            "found": int(row["found_count"]), "unique": int(row["unique_count"]),
            "loaded": int(row["loaded_count"]), "errors": int(row["error_count"]),
        }
        return {"persisted": persisted, "recorded": recorded, "matches": persisted == recorded}

    def snapshot_id(self, vacancy_hh_id: str | int, content_hash: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM vacancy_snapshots WHERE vacancy_hh_id = ? AND content_hash = ?",
                (str(vacancy_hh_id), content_hash),
            ).fetchone()
        if row is None:
            raise ValueError("stored snapshot is missing")
        return int(row["id"])

    def upsert_auto_relevance(self, snapshot_id: int, label: str, score: float, reasons: list[str], version: str) -> None:
        """Refresh automatic label without overwriting manual review fields."""
        with self.transaction() as tx:
            tx.execute(
                "INSERT INTO relevance_labels(snapshot_id, label, score, reasons_json, classifier_version, calculated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(snapshot_id) DO UPDATE SET "
                "label=excluded.label, score=excluded.score, reasons_json=excluded.reasons_json, "
                "classifier_version=excluded.classifier_version, calculated_at=excluded.calculated_at",
                (snapshot_id, label, score, json_value(reasons), version, utc_now()),
            )

    def set_manual_relevance(self, snapshot_id: int, label: str, reason: str | None) -> None:
        if label not in {"relevant", "borderline", "irrelevant", "unknown"}:
            raise ValueError(f"invalid manual label: {label}")
        with self.transaction() as tx:
            cursor = tx.execute(
                "UPDATE relevance_labels SET manual_label=?, manual_reason=?, manual_labeled_at=? WHERE snapshot_id=?",
                (label, reason or None, utc_now(), snapshot_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"snapshot {snapshot_id} has no auto relevance label")

    def upsert_features(self, snapshot_id: int, features: list[dict[str, Any]], version: str) -> None:
        """Replace one extractor version's values without affecting other versions."""
        with self.transaction() as tx:
            tx.execute("DELETE FROM features WHERE snapshot_id = ? AND extractor_version = ?", (snapshot_id, version))
            for feature in features:
                tx.execute(
                    "INSERT INTO features(snapshot_id, name, value_type, value_text, value_number, value_json, extractor_version, calculated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (snapshot_id, feature["name"], feature["value_type"], feature.get("value_text"),
                     feature.get("value_number"), json_value(feature["value_json"]) if "value_json" in feature else None,
                     version, utc_now()),
                )

    def sync_skill_dictionary(self, aliases: dict[str, str], version: str, topic_for: Any) -> dict[str, int]:
        """Persist canonical skills and reject alias reassignment across versions."""
        with self.transaction() as tx:
            ids: dict[str, int] = {}
            for canonical in sorted(set(aliases.values())):
                row = tx.execute(
                    "INSERT INTO skills(canonical_name, topic_family, dictionary_version) VALUES (?, ?, ?) "
                    "ON CONFLICT(canonical_name, dictionary_version) DO UPDATE SET topic_family=excluded.topic_family "
                    "RETURNING id",
                    (canonical, topic_for(canonical), version),
                ).fetchone()
                ids[canonical] = int(row["id"])
            for alias, canonical in aliases.items():
                row = tx.execute(
                    "SELECT s.canonical_name FROM skill_aliases a JOIN skills s ON s.id=a.skill_id "
                    "WHERE a.alias_normalized = ?", (alias,),
                ).fetchone()
                if row is not None and row["canonical_name"] != canonical:
                    raise ValueError(f"skill alias {alias!r} conflicts with existing dictionary")
                tx.execute(
                    "INSERT INTO skill_aliases(skill_id, alias_normalized) VALUES (?, ?) "
                    "ON CONFLICT(alias_normalized) DO UPDATE SET skill_id=excluded.skill_id",
                    (ids[canonical], alias),
                )
        return ids

    def upsert_vacancy_skills(
        self, snapshot_id: int, matches: list[tuple[str, str, str, int]], skill_ids: dict[str, int], version: str,
    ) -> None:
        """Persist every deterministic skill-evidence source for one snapshot."""
        with self.transaction() as tx:
            tx.execute("DELETE FROM vacancy_skills WHERE snapshot_id = ? AND extractor_version = ?", (snapshot_id, version))
            for canonical, source, alias, count in matches:
                tx.execute(
                    "INSERT INTO vacancy_skills(snapshot_id, skill_id, source, matched_alias, match_count, evidence_json, extractor_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (snapshot_id, skill_ids[canonical], source, alias, count, json_value([alias]), version),
                )

    def selected_snapshots(
        self, *, snapshot_scope: str = "latest", run_ids: list[int] | None = None,
        area_ids: list[str] | None = None, source: str | None = None,
        date_from: str | None = None, date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        """Load sanitized snapshots for offline extraction; never contacts HH."""
        if snapshot_scope not in {"latest", "all"}:
            raise ValueError("snapshot_scope must be 'latest' or 'all'")
        clauses: list[str] = []
        values: list[Any] = []
        joins = ""
        if run_ids:
            placeholders = ", ".join("?" for _ in run_ids)
            joins += " JOIN vacancy_snapshot_observations o ON o.snapshot_id = s.id"
            clauses.append(f"o.run_id IN ({placeholders})")
            values.extend(run_ids)
        if area_ids:
            placeholders = ", ".join("?" for _ in area_ids)
            clauses.append(f"CAST(s.area_id AS TEXT) IN ({placeholders})")
            values.extend(area_ids)
        if source:
            clauses.append("s.source = ?")
            values.append(source)
        if date_from:
            clauses.append("substr(s.published_at, 1, 10) >= ?")
            values.append(date_from)
        if date_to:
            clauses.append("substr(s.published_at, 1, 10) <= ?")
            values.append(date_to)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        scope = ""
        if snapshot_scope == "latest":
            scope = (
                " JOIN (SELECT vacancy_hh_id, MAX(last_seen_at) AS latest_seen "
                "FROM vacancy_snapshots GROUP BY vacancy_hh_id) newest "
                "ON newest.vacancy_hh_id = s.vacancy_hh_id AND newest.latest_seen = s.last_seen_at"
            )
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT s.* FROM vacancy_snapshots s" + scope + joins + " " + where + " ORDER BY s.id",
                values,
            ).fetchall()
        snapshots: list[dict[str, Any]] = []
        for row in rows:
            snapshot = dict(row)
            for name in ("work_format", "roles", "industries", "key_skills", "languages", "completeness"):
                snapshot[name if name != "work_format" else "work_formats"] = json.loads(snapshot.pop(f"{name}_json"))
            snapshots.append(snapshot)
        return snapshots

    def search_text(self, query: str, *, limit: int = 100) -> list[dict[str, Any]]:
        """Search stored title/description FTS index; never contacts HH."""
        if not query.strip():
            raise ValueError("FTS query must not be empty")
        if not 1 <= limit <= 10_000:
            raise ValueError("FTS limit must be between 1 and 10000")
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT s.id, s.vacancy_hh_id, s.title, s.description_text, fts.rank "
                "FROM vacancy_text_fts fts JOIN vacancy_snapshots s ON s.id = fts.rowid "
                "WHERE vacancy_text_fts MATCH ? ORDER BY fts.rank LIMIT ?",
                (query, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def raw_payload_purge_summary(self, before: str) -> dict[str, int]:
        """Return reclaimable raw-BLOB count/size; source snapshots remain intact."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS snapshots, COALESCE(SUM(raw_size), 0) AS raw_bytes "
                "FROM vacancy_snapshots WHERE raw_payload IS NOT NULL AND date(last_seen_at) < date(?)",
                (before,),
            ).fetchone()
        return {"snapshots": int(row["snapshots"]), "raw_bytes": int(row["raw_bytes"])}

    def purge_raw_payloads(self, before: str) -> dict[str, int]:
        """Permanently drop only compressed raw payload BLOBs before date boundary."""
        summary = self.raw_payload_purge_summary(before)
        with self.transaction() as tx:
            cursor = tx.execute(
                "UPDATE vacancy_snapshots SET raw_payload = NULL, raw_compression = NULL, raw_size = NULL "
                "WHERE raw_payload IS NOT NULL AND date(last_seen_at) < date(?)",
                (before,),
            )
        summary["purged"] = int(cursor.rowcount)
        return summary

    _RESET_TABLES = (
        "extraction_errors", "extraction_runs", "vacancy_skills", "skill_aliases", "skills",
        "features", "relevance_labels", "snapshot_work_formats", "snapshot_industries",
        "snapshot_roles", "snapshot_key_skills", "vacancy_snapshot_observations",
        "collection_errors", "vacancy_query_hits", "search_pages", "run_areas",
        "vacancy_snapshots", "vacancies", "search_queries", "collection_runs",
        "areas", "area_catalog_versions",
    )

    def reset_data_summary(self) -> dict[str, int]:
        """Count removable collection/derived rows while preserving schema/migrations."""
        with self.connect() as connection:
            counts = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in self._RESET_TABLES
            }
        return {table: count for table, count in counts.items() if count}

    def reset_data(self) -> dict[str, int]:
        """Permanently remove all collected/derived data; schema stays ready for fresh scan."""
        summary = self.reset_data_summary()
        with self.transaction() as tx:
            for table in self._RESET_TABLES:
                tx.execute(f"DELETE FROM {table}")
        return summary

    def integrity_check(self) -> list[str]:
        """Run SQLite integrity check without changing collected data."""
        with self.connect() as connection:
            return [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]

    def checkpoint(self) -> dict[str, int]:
        """Checkpoint WAL without deleting source/derived rows."""
        with self.connect() as connection:
            row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return {"busy": int(row[0]), "log_frames": int(row[1]), "checkpointed_frames": int(row[2])}

    def start_extraction_run(self, kind: str, version: str, config: dict[str, Any], selected_count: int) -> int:
        """Create durable run metadata for a local, rebuildable extractor."""
        if kind not in {"relevance", "features", "skills"}:
            raise ValueError(f"invalid extraction kind: {kind}")
        config_json = json_value(config)
        with self.transaction() as tx:
            return int(tx.execute(
                "INSERT INTO extraction_runs(kind, status, extractor_version, config_json, config_hash, started_at, selected_count) "
                "VALUES (?, 'running', ?, ?, ?, ?, ?)",
                (kind, version, config_json, hashlib.sha256(config_json.encode("utf-8")).hexdigest(), utc_now(), selected_count),
            ).lastrowid)

    def finish_extraction_run(self, extraction_run_id: int, processed_count: int, error_count: int) -> None:
        """Close extractor run after every selected snapshot was attempted."""
        with self.transaction() as tx:
            tx.execute(
                "UPDATE extraction_runs SET status = ?, finished_at = ?, processed_count = ?, error_count = ? WHERE id = ?",
                ("completed" if not error_count else "degraded", utc_now(), processed_count, error_count, extraction_run_id),
            )

    def record_extraction_error(self, extraction_run_id: int, snapshot_id: int | None, error: Exception) -> None:
        with self.transaction() as tx:
            tx.execute(
                "INSERT INTO extraction_errors(extraction_run_id, snapshot_id, error_type, message, occurred_at) VALUES (?, ?, ?, ?, ?)",
                (extraction_run_id, snapshot_id, type(error).__name__, str(error), utc_now()),
            )

    @staticmethod
    def _store_snapshot_links(
        connection: sqlite3.Connection, snapshot_id: int, snapshot: dict[str, Any],
    ) -> None:
        """Store analytical many-value links without discarding snapshot JSON."""
        for work_format in snapshot.get("work_formats", []):
            name = work_format.get("name") if isinstance(work_format, dict) else None
            if name:
                connection.execute(
                    "INSERT INTO snapshot_work_formats(snapshot_id, work_format_id, work_format_name) VALUES (?, ?, ?) "
                    "ON CONFLICT(snapshot_id, work_format_name) DO UPDATE SET work_format_id = excluded.work_format_id",
                    (snapshot_id, work_format.get("id"), str(name)),
                )
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
        attempt: int = 1, date_from: str | None = None, date_to: str | None = None,
        source: str | None = None, reason_code: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """Append auditable collection error."""
        reason_code = reason_code or self.error_reason(error_type, http_status)
        values = (run_id, stage, query_id, area_id,
                  str(vacancy_hh_id) if vacancy_hh_id is not None else None,
                  error_type, http_status, message, attempt, utc_now(), date_from, date_to, source, reason_code)
        self._execute(
            "INSERT INTO collection_errors(run_id, stage, query_id, area_id, vacancy_hh_id, error_type, "
            "http_status, message, attempt, occurred_at, date_from, date_to, source, reason_code) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values, connection,
        )

    @staticmethod
    def error_reason(error_type: str, http_status: int | None) -> str:
        """Return stable, non-secret failure class for coverage and retry."""
        if http_status == 429:
            return "rate_limited"
        if http_status in {401, 403}:
            return "authorization"
        if http_status is not None and 500 <= http_status <= 599:
            return "server_error"
        if http_status is not None and 400 <= http_status <= 499:
            return "client_error"
        if "Timeout" in error_type:
            return "timeout"
        if "Connection" in error_type:
            return "connection"
        return "transport_or_parse"

    def unresolved_errors(self, run_id: int, *, max_attempts: int) -> list[dict[str, Any]]:
        """Load retryable work only; completed work never appears here."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT e.*, q.expression, q.version, q.query_group, q.purpose FROM collection_errors e "
                "LEFT JOIN search_queries q ON q.id = e.query_id "
                "WHERE e.run_id = ? AND e.resolved_at IS NULL AND e.stage IN ('search', 'vacancy') "
                "AND e.attempt < ? AND e.id IN (SELECT MAX(id) FROM collection_errors "
                "WHERE run_id = ? AND resolved_at IS NULL AND stage IN ('search', 'vacancy') "
                "GROUP BY stage, query_id, area_id, vacancy_hh_id, date_from, date_to) ORDER BY e.id",
                (run_id, max_attempts, run_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def coverage_report(self, run_id: int) -> list[dict[str, Any]]:
        """Build coverage solely from persisted pages, hits, snapshots and errors."""
        with self.connect() as connection:
            rows = connection.execute(
                "WITH units AS ("
                " SELECT run_id, query_id, area_id, date_from, date_to FROM search_pages WHERE run_id = ?"
                " UNION SELECT run_id, query_id, area_id, COALESCE(date_from,''), COALESCE(date_to,'')"
                " FROM collection_errors WHERE run_id = ? AND query_id IS NOT NULL AND area_id IS NOT NULL"
                "), cards AS ("
                " SELECT h.run_id,h.query_id,h.area_id,h.date_from,h.date_to,"
                " COUNT(DISTINCT h.vacancy_hh_id) AS cards_requested,"
                " COUNT(DISTINCT o.vacancy_hh_id) AS cards_loaded"
                " FROM vacancy_query_hits h LEFT JOIN vacancy_snapshot_observations o"
                " ON o.run_id=h.run_id AND o.vacancy_hh_id=h.vacancy_hh_id"
                " WHERE h.run_id=? GROUP BY h.run_id,h.query_id,h.area_id,h.date_from,h.date_to"
                ") SELECT q.query_group, q.expression, u.area_id, NULLIF(u.date_from,'') AS date_from,"
                " NULLIF(u.date_to,'') AS date_to, 1 AS requested,"
                " MAX(CASE WHEN p.http_status BETWEEN 200 AND 299 AND p.error_type IS NULL THEN 1 ELSE 0 END) AS completed,"
                " MAX(CASE WHEN e.stage='coverage' AND e.error_type='SearchDepthSaturated' AND e.resolved_at IS NULL THEN 1 ELSE 0 END) AS saturated,"
                " MAX(CASE WHEN e.stage='search' AND e.resolved_at IS NULL THEN 1 ELSE 0 END) AS failed,"
                " COALESCE(c.cards_requested,0) AS cards_requested, COALESCE(c.cards_loaded,0) AS cards_loaded,"
                " COALESCE(c.cards_requested,0)-COALESCE(c.cards_loaded,0) AS cards_missing,"
                " SUM(CASE WHEN e.stage='vacancy' AND e.resolved_at IS NULL THEN 1 ELSE 0 END) AS card_failures"
                " FROM units u JOIN search_queries q ON q.id=u.query_id"
                " LEFT JOIN search_pages p ON p.run_id=u.run_id AND p.query_id=u.query_id AND p.area_id=u.area_id"
                " AND p.date_from=u.date_from AND p.date_to=u.date_to"
                " LEFT JOIN collection_errors e ON e.run_id=u.run_id AND e.query_id=u.query_id AND e.area_id=u.area_id"
                " AND COALESCE(e.date_from,'')=u.date_from AND COALESCE(e.date_to,'')=u.date_to"
                " LEFT JOIN cards c ON c.run_id=u.run_id AND c.query_id=u.query_id AND c.area_id=u.area_id"
                " AND c.date_from=u.date_from AND c.date_to=u.date_to"
                " GROUP BY q.query_group,q.expression,u.area_id,u.date_from,u.date_to,c.cards_requested,c.cards_loaded"
                " ORDER BY q.query_group,q.expression,u.area_id,u.date_from,u.date_to",
                (run_id, run_id, run_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def resolve_errors(
        self, run_id: int, stage: str, *, query_id: int | None = None,
        area_id: int | None = None, vacancy_hh_id: str | int | None = None,
        date_from: str | None = None, date_to: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """Mark matching retryable failures resolved after successful work."""
        clauses = ["run_id = ?", "stage = ?", "resolved_at IS NULL"]
        values: list[Any] = [run_id, stage]
        for column, value in (
            ("query_id", query_id), ("area_id", area_id),
            ("vacancy_hh_id", str(vacancy_hh_id) if vacancy_hh_id is not None else None),
            ("date_from", date_from), ("date_to", date_to),
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

    def run_config(self, run_id: int) -> dict[str, Any]:
        """Load immutable, redacted run configuration for safe resume."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT config_json FROM collection_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"run {run_id} does not exist")
        return json.loads(row["config_json"])

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
