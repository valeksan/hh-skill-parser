"""Documented public HH API transport for database-first collection."""

from __future__ import annotations

from typing import Any

import requests


class HHApiSource:
    """Fetch HH vacancy search/detail JSON without browser fallback state."""

    base_url = "https://api.hh.ru"

    def __init__(
        self, *, user_agent: str, timeout: float = 30.0,
        access_token: str | None = None, session: requests.Session | None = None,
    ):
        if not user_agent.strip():
            raise ValueError("HH API user agent must not be empty")
        if timeout <= 0:
            raise ValueError("HH API timeout must be positive")
        self.timeout = timeout
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

    def _get(self, path: str, **kwargs: Any) -> requests.Response:
        """Request API; one rejected bearer token degrades to public API."""
        response = self.session.get(f"{self.base_url}{path}", timeout=self.timeout, **kwargs)
        try:
            response.raise_for_status()
        except requests.HTTPError:
            status = getattr(response, "status_code", None)
            if status not in {401, 403} or "Authorization" not in self.session.headers:
                raise
            self.session.headers.pop("Authorization", None)
            if self._auth_degradation is None:
                self._auth_degradation = {"http_status": int(status)}
            response = self.session.get(f"{self.base_url}{path}", timeout=self.timeout, **kwargs)
            response.raise_for_status()
        return response

    def search(self, expression: str, area_id: str, *, per_page: int = 100) -> list[dict[str, Any]]:
        """Fetch first documented search page; caller persists coverage unit."""
        items, _ = self.search_page(expression, area_id, page=0, per_page=per_page)
        return items

    def search_page(
        self, expression: str, area_id: str, *, page: int, per_page: int = 100,
        date_from: str | None = None, date_to: str | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Fetch one documented page and report whether it is final."""
        if not 1 <= per_page <= 100:
            raise ValueError("per_page must be between 1 and 100")
        if page < 0:
            raise ValueError("page must not be negative")
        if bool(date_from) != bool(date_to):
            raise ValueError("date_from and date_to must be provided together")
        params = {"text": expression, "area": area_id, "per_page": per_page, "page": page}
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
