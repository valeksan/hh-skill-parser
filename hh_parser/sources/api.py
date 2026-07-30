"""Documented public HH API transport for database-first collection."""

from __future__ import annotations

from collections.abc import Callable
from time import sleep
from typing import Any

import requests


class HHApiSource:
    """Fetch HH vacancy search/detail JSON without browser fallback state."""

    base_url = "https://api.hh.ru"

    def __init__(
        self, *, user_agent: str, timeout: float = 30.0,
        access_token: str | None = None, session: requests.Session | None = None,
        max_retries: int = 3, retry_backoff: float = 1.0,
        sleep_fn: Callable[[float], None] = sleep,
    ):
        if not user_agent.strip():
            raise ValueError("HH API user agent must not be empty")
        if timeout <= 0:
            raise ValueError("HH API timeout must be positive")
        if max_retries < 0:
            raise ValueError("HH API max_retries must not be negative")
        if retry_backoff < 0:
            raise ValueError("HH API retry_backoff must not be negative")
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.sleep_fn = sleep_fn
        self.session = session or requests.Session()
        self.session.headers.update({
            "HH-User-Agent": user_agent,
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        })
        if access_token:
            self.session.headers["Authorization"] = f"Bearer {access_token}"
        self._auth_degradation: dict[str, int] | None = None

    def consume_auth_degradation(self) -> dict[str, int] | None:
        """Return one token-failure event without exposing its credential."""
        event = self._auth_degradation
        self._auth_degradation = None
        return event

    @staticmethod
    def _retry_after(response: requests.Response, fallback: float) -> float:
        """Use numeric Retry-After when supplied; malformed/date values use backoff."""
        value = response.headers.get("Retry-After")
        if value is None:
            return fallback
        try:
            return max(0.0, float(value))
        except ValueError:
            return fallback

    def _get(self, path: str, **kwargs: Any) -> requests.Response:
        """Request API; retry only transient failures, then fail with original status."""
        auth_retry = True
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(f"{self.base_url}{path}", timeout=self.timeout, **kwargs)
            except (requests.ConnectionError, requests.Timeout):
                if attempt >= self.max_retries:
                    raise
                self.sleep_fn(self.retry_backoff * (2 ** attempt))
                continue
            status = getattr(response, "status_code", 200)
            if status in {401, 403} and auth_retry and "Authorization" in self.session.headers:
                self.session.headers.pop("Authorization", None)
                self._auth_degradation = self._auth_degradation or {"http_status": int(status)}
                auth_retry = False
                continue
            if status == 429 or 500 <= status <= 599:
                if attempt < self.max_retries:
                    self.sleep_fn(self._retry_after(response, self.retry_backoff * (2 ** attempt)))
                    continue
            response.raise_for_status()
            return response
        raise RuntimeError("unreachable HH API retry state")

    def search(self, expression: str, area_id: str, *, per_page: int = 100) -> list[dict[str, Any]]:
        """Fetch first documented search page; caller persists coverage unit."""
        items, _ = self.search_page(expression, area_id, page=0, per_page=per_page)
        return items

    def areas(self) -> list[dict[str, Any]]:
        """Fetch official geographic area catalog."""
        payload = self._get("/areas").json()
        if not isinstance(payload, list):
            raise ValueError("HH API areas response must be a list")
        return payload

    def search_page(
        self, expression: str, area_id: str, *, page: int, per_page: int = 100,
        date_from: str | None = None, date_to: str | None = None,
        search_fields: tuple[str, ...] = (),
    ) -> tuple[list[dict[str, Any]], bool]:
        """Fetch one documented page and report whether it is final."""
        if not 1 <= per_page <= 100:
            raise ValueError("per_page must be between 1 and 100")
        if page < 0:
            raise ValueError("page must not be negative")
        if bool(date_from) != bool(date_to):
            raise ValueError("date_from and date_to must be provided together")
        params = {"text": expression, "area": area_id, "per_page": per_page, "page": page}
        if search_fields:
            params["search_field"] = list(search_fields)
        if date_from:
            params["date_from"] = date_from
            params["date_to"] = date_to
        response = self._get("/vacancies", params=params)
        payload = response.json()
        items = payload.get("items")
        if not isinstance(items, list):
            raise ValueError("HH API search response has no items list")
        result: list[dict[str, Any]] = []
        for item in items:
            candidate = dict(item)
            candidate["_source"] = "api"
            result.append(candidate)
        pages = payload.get("pages")
        is_last = not result or (isinstance(pages, int) and page + 1 >= pages)
        return result, is_last

    def detail(self, candidate: dict[str, Any]) -> dict[str, Any]:
        """Fetch one vacancy card by HH ID."""
        response = self._get(f"/vacancies/{candidate['id']}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("HH API vacancy response must be an object")
        payload["_source"] = "api"
        return payload
