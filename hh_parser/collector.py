"""Database-first collection orchestration, independent of HTTP transport."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date, timedelta
from typing import Any

from .normalization import normalize_api_vacancy
from .extractors.features import VERSION as FEATURES_VERSION, extract_features
from .query_specs import QuerySpec
from .skill_dictionary import SkillDictionary, topic_family
from relevance import VERSION as RELEVANCE_VERSION, classify_relevance
from .storage import Database


class Collector:
    """Collect candidates before relevance decisions; persist every stage."""

    def __init__(self, database: Database, *, transport: Any | None = None, skill_dictionary: SkillDictionary | None = None):
        self.database = database
        self.transport = transport
        self.skill_dictionary = skill_dictionary
        self.skill_ids = (
            database.sync_skill_dictionary(skill_dictionary.aliases, skill_dictionary.version, topic_family)
            if skill_dictionary else {}
        )
        self.loaded_ids: set[str] = set()

    def start(
        self, config: dict[str, Any], area_ids: list[str], *, catalog_version_id: int | None = None,
        selection_source: str = "explicit", source_policy: str = "api",
    ) -> int:
        self.database.migrate()
        run_id = self.database.start_run(config, source_policy=source_policy)
        self.database.set_run_areas(
            run_id, area_ids, catalog_version_id=catalog_version_id, selection_source=selection_source,
        )
        return run_id

    @staticmethod
    def _query(value: str | QuerySpec) -> tuple[str, dict[str, Any]]:
        if isinstance(value, QuerySpec):
            return value.expression, {"version": value.version, "query_group": value.group, "purpose": value.purpose}
        return value, {}

    def collect(
        self, run_id: int, area_ids: Iterable[str], queries: Iterable[str], *,
        search: Callable[[str, str], list[dict[str, Any]]],
        detail: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, int]:
        """Collect fixture/live transport results with durable per-stage writes."""
        self.loaded_ids = self.database.observed_vacancy_ids(run_id)
        for area_id in area_ids:
            for query in queries:
                expression, metadata = self._query(query)
                query_id = self.database.upsert_query(expression, **metadata)
                if self.database.search_page_succeeded(
                    run_id, query_id, area_id=int(area_id)
                ):
                    candidates = self.database.load_query_hits(
                        run_id, query_id, area_id=int(area_id)
                    )
                else:
                    try:
                        candidates = search(expression, str(area_id))
                        for rank, candidate in enumerate(candidates):
                            vacancy_id = str(candidate["id"])
                            self.database.upsert_vacancy(
                                vacancy_id, source=candidate.get("_source", "api"),
                                alternate_url=candidate.get("alternate_url"),
                            )
                            self.database.record_query_hit(
                                run_id, query_id, vacancy_id, area_id=int(area_id),
                                page=0, rank=rank,
                            )
                        self.database.record_search_page(
                            run_id, query_id, page=0, area_id=int(area_id), http_status=200,
                            result_count=len(candidates), is_last_page=True,
                        )
                        self.database.resolve_errors(
                            run_id, "search", query_id=query_id, area_id=int(area_id)
                        )
                    except Exception as error:
                        self.database.record_search_page(
                            run_id, query_id, page=0, area_id=int(area_id),
                            error_type=type(error).__name__, error_message=str(error),
                            is_last_page=False,
                        )
                        self.database.record_error(
                            run_id, "search", type(error).__name__, str(error),
                            query_id=query_id, area_id=int(area_id),
                        )
                        continue
                for candidate in candidates:
                    vacancy_id = str(candidate["id"])
                    if vacancy_id in self.loaded_ids:
                        continue
                    self.loaded_ids.add(vacancy_id)
                    try:
                        payload = detail(candidate)
                        snapshot = normalize_api_vacancy(payload)
                        self._store_snapshot(run_id, vacancy_id, snapshot)
                        self.database.resolve_errors(
                            run_id, "vacancy", vacancy_hh_id=vacancy_id
                        )
                    except Exception as error:
                        self.loaded_ids.discard(vacancy_id)
                        self.database.record_error(
                            run_id, "vacancy", type(error).__name__, str(error), query_id=query_id,
                            area_id=int(area_id), vacancy_hh_id=vacancy_id,
                        )
        return self._finish(run_id)

    def resume(
        self, run_id: int, queries: Iterable[str], *,
        search: Callable[[str, str], list[dict[str, Any]]],
        detail: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, int]:
        """Resume exact frozen area scope from durable DB state."""
        self.database.prepare_run_resume(run_id)
        return self.collect(
            run_id, self.database.get_run_areas(run_id), queries,
            search=search, detail=detail,
        )

    def collect_paginated(
        self, run_id: int, area_ids: Iterable[str], queries: Iterable[str], *,
        search_page: Callable[..., tuple[list[dict[str, Any]], bool]],
        detail: Callable[[dict[str, Any]], dict[str, Any]], max_pages: int = 20,
        date_from: str | None = None, date_to: str | None = None,
    ) -> dict[str, int]:
        """Collect paginated API work units; flag depth saturation for slicing."""
        if not 1 <= max_pages <= 20:
            raise ValueError("max_pages must be between 1 and 20")
        if bool(date_from) != bool(date_to):
            raise ValueError("date_from and date_to must be provided together")
        self.loaded_ids = self.database.observed_vacancy_ids(run_id)
        for area_id in area_ids:
            for query in queries:
                expression, metadata = self._query(query)
                query_id = self.database.upsert_query(expression, **metadata)
                saturated = self._collect_search_unit(
                    run_id, query_id, expression, int(area_id), search_page, detail,
                    max_pages=max_pages, date_from=date_from, date_to=date_to,
                )
                if saturated:
                    self.database.record_error(
                        run_id, "coverage", "SearchDepthSaturated",
                        f"reached {max_pages * 100} result depth; split by date window",
                        query_id=query_id, area_id=int(area_id),
                        date_from=date_from, date_to=date_to,
                    )
        return self._finish(run_id)

    def collect_sliced(
        self, run_id: int, area_ids: Iterable[str], queries: Iterable[str], *,
        search_page: Callable[..., tuple[list[dict[str, Any]], bool]],
        detail: Callable[[dict[str, Any]], dict[str, Any]], max_pages: int = 20,
        date_from: str, date_to: str, min_window_days: int = 1,
        overlap_days: int = 1,
    ) -> dict[str, int]:
        """Split saturated finite date windows until every unit fits API depth."""
        if not 1 <= max_pages <= 20:
            raise ValueError("max_pages must be between 1 and 20")
        if min_window_days < 1 or overlap_days < 0:
            raise ValueError("min_window_days must be positive and overlap_days non-negative")
        self.loaded_ids = self.database.observed_vacancy_ids(run_id)
        for area_id in area_ids:
            for query in queries:
                expression, metadata = self._query(query)
                query_id = self.database.upsert_query(expression, **metadata)
                pending = [(date_from, date_to)]
                while pending:
                    window_from, window_to = pending.pop(0)
                    saturated = self._collect_search_unit(
                        run_id, query_id, expression, int(area_id), search_page, detail,
                        max_pages=max_pages, date_from=window_from, date_to=window_to,
                    )
                    if not saturated:
                        continue
                    children = split_date_window(
                        window_from, window_to, min_window_days=min_window_days,
                        overlap_days=overlap_days,
                    )
                    if children:
                        pending.extend(children)
                    else:
                        self.database.record_error(
                            run_id, "coverage", "SearchDepthSaturated",
                            f"cannot split {window_from}..{window_to} below {min_window_days} day(s)",
                            query_id=query_id, area_id=int(area_id),
                            date_from=window_from, date_to=window_to,
                        )
        return self._finish(run_id)

    def _finish(self, run_id: int) -> dict[str, int]:
        """Persist transport degradation before calculating final run state."""
        consume = getattr(self.transport, "consume_auth_degradation", None)
        if callable(consume):
            event = consume()
            if event:
                self.database.record_error(
                    run_id, "auth", "AuthenticationDegraded",
                    "HH access token was rejected; continued with public API",
                    http_status=event.get("http_status"),
                )
        counters = self.database.run_counters(run_id)
        self.database.finish_run(run_id, "completed" if not counters["errors"] else "degraded", counters)
        return counters

    def resume_paginated(
        self, run_id: int, queries: Iterable[str], *,
        search_page: Callable[..., tuple[list[dict[str, Any]], bool]],
        detail: Callable[[dict[str, Any]], dict[str, Any]], max_pages: int = 20,
        date_from: str | None = None, date_to: str | None = None,
    ) -> dict[str, int]:
        """Resume paginated collection from DB pages and frozen areas."""
        self.database.prepare_run_resume(run_id)
        return self.collect_paginated(
            run_id, self.database.get_run_areas(run_id), queries,
            search_page=search_page, detail=detail, max_pages=max_pages,
            date_from=date_from, date_to=date_to,
        )

    def resume_sliced(
        self, run_id: int, queries: Iterable[str], *,
        search_page: Callable[..., tuple[list[dict[str, Any]], bool]],
        detail: Callable[[dict[str, Any]], dict[str, Any]], max_pages: int,
        date_from: str, date_to: str, min_window_days: int, overlap_days: int,
    ) -> dict[str, int]:
        """Resume deterministic date-window split tree from stored pages."""
        self.database.prepare_run_resume(run_id)
        return self.collect_sliced(
            run_id, self.database.get_run_areas(run_id), queries,
            search_page=search_page, detail=detail, max_pages=max_pages,
            date_from=date_from, date_to=date_to, min_window_days=min_window_days,
            overlap_days=overlap_days,
        )

    def _collect_search_unit(
        self, run_id: int, query_id: int, expression: str, area_id: int,
        search_page: Callable[..., tuple[list[dict[str, Any]], bool]],
        detail: Callable[[dict[str, Any]], dict[str, Any]], *, max_pages: int,
        date_from: str | None, date_to: str | None,
    ) -> bool:
        """Collect one query×area×window. Return True only for API-depth saturation."""
        for page in range(max_pages):
            if self.database.search_page_succeeded(
                run_id, query_id, area_id=area_id, page=page,
                date_from=date_from, date_to=date_to,
            ):
                candidates = self.database.load_query_hits(
                    run_id, query_id, area_id=area_id, page=page,
                    date_from=date_from, date_to=date_to,
                )
                is_last = self._stored_page_is_last(
                    run_id, query_id, area_id, page, date_from, date_to,
                )
            else:
                try:
                    candidates, is_last = search_page(
                        expression, str(area_id), page=page,
                        date_from=date_from, date_to=date_to,
                    )
                    for rank, candidate in enumerate(candidates):
                        vacancy_id = str(candidate["id"])
                        self.database.upsert_vacancy(
                            vacancy_id, source=candidate.get("_source", "api"),
                            alternate_url=candidate.get("alternate_url"),
                        )
                        self.database.record_query_hit(
                            run_id, query_id, vacancy_id, area_id=area_id,
                            page=page, rank=rank, date_from=date_from, date_to=date_to,
                        )
                    self.database.record_search_page(
                        run_id, query_id, page=page, area_id=area_id,
                        date_from=date_from, date_to=date_to, http_status=200,
                        result_count=len(candidates), is_last_page=is_last,
                    )
                    self.database.resolve_errors(
                        run_id, "search", query_id=query_id, area_id=area_id,
                        date_from=date_from, date_to=date_to,
                    )
                except Exception as error:
                    self.database.record_search_page(
                        run_id, query_id, page=page, area_id=area_id,
                        date_from=date_from, date_to=date_to,
                        error_type=type(error).__name__, error_message=str(error),
                    )
                    self.database.record_error(
                        run_id, "search", type(error).__name__, str(error),
                        query_id=query_id, area_id=area_id,
                        date_from=date_from, date_to=date_to,
                    )
                    return False
            self._load_candidates(run_id, candidates, detail, query_id, area_id)
            if is_last:
                self.database.resolve_errors(
                    run_id, "coverage", query_id=query_id, area_id=area_id,
                    date_from=date_from, date_to=date_to,
                )
                return False
        return True

    def _load_candidates(
        self, run_id: int, candidates: Iterable[dict[str, Any]],
        detail: Callable[[dict[str, Any]], dict[str, Any]], query_id: int, area_id: int,
    ) -> None:
        """Load each unseen card; failed cards remain eligible for resume."""
        for candidate in candidates:
            vacancy_id = str(candidate["id"])
            if vacancy_id in self.loaded_ids:
                continue
            self.loaded_ids.add(vacancy_id)
            try:
                snapshot = normalize_api_vacancy(detail(candidate))
                self._store_snapshot(run_id, vacancy_id, snapshot)
                self.database.resolve_errors(run_id, "vacancy", vacancy_hh_id=vacancy_id)
            except Exception as error:
                self.loaded_ids.discard(vacancy_id)
                self.database.record_error(
                    run_id, "vacancy", type(error).__name__, str(error), query_id=query_id,
                    area_id=area_id, vacancy_hh_id=vacancy_id,
                )

    def _store_snapshot(self, run_id: int, vacancy_id: str, snapshot: dict[str, Any]) -> None:
        """Persist snapshot, automatic relevance, and versioned deterministic features."""
        self.database.record_snapshot(run_id, vacancy_id, snapshot)
        snapshot_id = self.database.snapshot_id(vacancy_id, snapshot["content_hash"])
        label, score, reasons = classify_relevance(snapshot["title"], snapshot.get("description_text") or "")
        self.database.upsert_auto_relevance(snapshot_id, label, score, reasons, RELEVANCE_VERSION)
        self.database.upsert_features(snapshot_id, extract_features(snapshot), FEATURES_VERSION)
        if self.skill_dictionary:
            matches = []
            for source, text in (("title", snapshot.get("title") or ""), ("description", snapshot.get("description_text") or "")):
                matches.extend((canonical, source, alias, count) for canonical, alias, count in self.skill_dictionary.matches(text))
            for skill in snapshot.get("key_skills", []):
                if isinstance(skill, dict) and skill.get("name"):
                    matches.extend((canonical, "key_skill", alias, count) for canonical, alias, count in self.skill_dictionary.matches(str(skill["name"])))
            self.database.upsert_vacancy_skills(snapshot_id, matches, self.skill_ids, self.skill_dictionary.version)

    def _stored_page_is_last(
        self, run_id: int, query_id: int, area_id: int, page: int,
        date_from: str | None, date_to: str | None,
    ) -> bool:
        """Read final-page marker when resuming a successful page."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT is_last_page FROM search_pages WHERE run_id = ? AND query_id = ? "
                "AND area_id = ? AND date_from = ? AND date_to = ? AND page = ?",
                (run_id, query_id, area_id, date_from or "", date_to or "", page),
            ).fetchone()
        return bool(row["is_last_page"]) if row else False


def split_date_window(
    date_from: str, date_to: str, *, min_window_days: int, overlap_days: int,
) -> list[tuple[str, str]]:
    """Bisect inclusive date range with controlled overlap, or return no children."""
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if end < start:
        raise ValueError("date_from must not be after date_to")
    if (end - start).days <= min_window_days:
        return []
    midpoint = start + timedelta(days=(end - start).days // 2)
    right_start = max(start, midpoint - timedelta(days=overlap_days))
    if right_start <= start:
        right_start = midpoint
    return [(start.isoformat(), midpoint.isoformat()), (right_start.isoformat(), end.isoformat())]
