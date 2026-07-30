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
            "User-Agent": user_agent,
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        })
        if access_token:
            self.session.headers["Authorization"] = f"Bearer {access_token}"

    def search(self, expression: str, area_id: str, *, per_page: int = 100) -> list[dict[str, Any]]:
        """Fetch first documented search page; caller persists coverage unit."""
        items, _ = self.search_page(expression, area_id, page=0, per_page=per_page)
        return items

    def search_page(
        self, expression: str, area_id: str, *, page: int, per_page: int = 100,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Fetch one documented page and report whether it is final."""
        if not 1 <= per_page <= 100:
            raise ValueError("per_page must be between 1 and 100")
        if page < 0:
            raise ValueError("page must not be negative")
        response = self.session.get(
            f"{self.base_url}/vacancies",
            params={"text": expression, "area": area_id, "per_page": per_page, "page": page},
            timeout=self.timeout,
        )
        response.raise_for_status()
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
        response = self.session.get(
            f"{self.base_url}/vacancies/{candidate['id']}", timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("HH API vacancy response must be an object")
        payload["_source"] = "api"
        return payload
