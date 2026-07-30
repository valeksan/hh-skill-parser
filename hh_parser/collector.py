"""Database-first collection orchestration, independent of HTTP transport."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from .normalization import normalize_api_vacancy
from .storage import Database


class Collector:
    """Collect candidates before relevance decisions; persist every stage."""

    def __init__(self, database: Database):
        self.database = database
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

    def collect(
        self, run_id: int, area_ids: Iterable[str], queries: Iterable[str], *,
        search: Callable[[str, str], list[dict[str, Any]]],
        detail: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, int]:
        """Collect fixture/live transport results with durable per-stage writes."""
        self.loaded_ids = self.database.observed_vacancy_ids(run_id)
        for area_id in area_ids:
            for expression in queries:
                query_id = self.database.upsert_query(expression)
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
                        self.database.record_snapshot(run_id, vacancy_id, snapshot)
                        self.database.resolve_errors(
                            run_id, "vacancy", vacancy_hh_id=vacancy_id
                        )
                    except Exception as error:
                        self.loaded_ids.discard(vacancy_id)
                        self.database.record_error(
                            run_id, "vacancy", type(error).__name__, str(error), query_id=query_id,
                            area_id=int(area_id), vacancy_hh_id=vacancy_id,
                        )
        counters = self.database.run_counters(run_id)
        self.database.finish_run(run_id, "completed" if not counters["errors"] else "degraded", counters)
        return counters

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
