"""Explicit HTML transport for HH vacancy pages; never used as API fallback."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

import requests
from bs4 import BeautifulSoup


class HHHtmlAntiBotError(RuntimeError):
    """Returned page is an interstitial, not a vacancy/search result."""


class HHHtmlSource:
    """Fetch public HH HTML with its own session and normalize only known markup."""

    source_name = "html"
    base_url = "https://hh.ru"
    search_path = "/search/vacancy"
    _anti_bot_markers = (
        "captcha", "access denied", "ddos-guard", "checking your browser",
        "проверка безопасности", "подтвердите, что вы не робот",
    )

    def __init__(self, *, user_agent: str, timeout: float = 30.0,
                 session: requests.Session | None = None, host: str = "hh.ru", locale: str = "RU",
                 **_ignored: Any):
        if not user_agent.strip():
            raise ValueError("HH HTML user agent must not be empty")
        if timeout <= 0:
            raise ValueError("HH HTML timeout must be positive")
        if host != "hh.ru":
            raise ValueError("HTML source currently supports only hh.ru")
        if not locale.strip():
            raise ValueError("HH HTML locale must not be empty")
        self.timeout, self.host, self.locale = timeout, host, locale
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"})
        self.last_response_status: int | None = None

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> str:
        response = self.session.get(f"{self.base_url}{path}", params=params, timeout=self.timeout)
        self.last_response_status = int(getattr(response, "status_code", 200))
        response.raise_for_status()
        text = response.text
        if self._is_anti_bot(text):
            raise HHHtmlAntiBotError("HH HTML anti-bot page received")
        return text

    @classmethod
    def _is_anti_bot(cls, html: str) -> bool:
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True).casefold()
        return any(marker in text for marker in cls._anti_bot_markers)

    def search_page(self, expression: str, area_id: str, *, page: int, per_page: int = 100,
                    date_from: str | None = None, date_to: str | None = None,
                    search_fields: tuple[str, ...] = ()) -> tuple[list[dict[str, Any]], bool]:
        if page < 0:
            raise ValueError("page must not be negative")
        if bool(date_from) != bool(date_to):
            raise ValueError("date_from and date_to must be provided together")
        params = self.search_request_params(
            expression, area_id, page=page, per_page=per_page, date_from=date_from,
            date_to=date_to, search_fields=search_fields,
        )
        html = self._get(self.search_path, params=params)
        items = self.parse_search_page(html)
        return items, not items or not self._has_next_page(html, page)

    @staticmethod
    def search_request_params(expression: str, area_id: str, *, page: int, per_page: int = 100,
                              date_from: str | None = None, date_to: str | None = None,
                              search_fields: tuple[str, ...] = ()) -> dict[str, Any]:
        params: dict[str, Any] = {"text": expression, "area": area_id, "page": page}
        if date_from:
            params.update({"date_from": date_from, "date_to": date_to})
        if search_fields:
            params["search_field"] = list(search_fields)
        return params

    def detail(self, candidate: dict[str, Any]) -> dict[str, Any]:
        vacancy_id = str(candidate["id"])
        html = self._get(f"/vacancy/{vacancy_id}")
        data = self.parse_vacancy_page(html, vacancy_id)
        data["_source"] = self.source_name
        data["_html"] = html
        return data

    @staticmethod
    def _has_next_page(html: str, page: int) -> bool:
        soup = BeautifulSoup(html, "html.parser")
        for link in soup.select("a[href]"):
            href = link.get("href", "")
            if "page=" in href and re.search(rf"(?:[?&])page={page + 1}(?:&|$)", href):
                return True
        return False

    @staticmethod
    def parse_search_page(html: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for link in soup.select('a[data-qa="serp-item__title"], a[href*="/vacancy/"]'):
            href = str(link.get("href", ""))
            match = re.search(r"/vacancy/(\d+)", href)
            if not match or match.group(1) in seen:
                continue
            vacancy_id = match.group(1)
            seen.add(vacancy_id)
            result.append({"id": vacancy_id, "name": link.get_text(" ", strip=True),
                           "alternate_url": href, "_source": "html"})
        return result

    @classmethod
    def parse_vacancy_page(cls, html: str, vacancy_id: str | None = None) -> dict[str, Any]:
        """Extract public JobPosting JSON-LD first, then stable HH data-qa fields."""
        soup = BeautifulSoup(html, "html.parser")
        job = next((row for row in cls._json_ld(soup) if row.get("@type") == "JobPosting"), {})
        title = cls._text(soup, '[data-qa="vacancy-title"]') or job.get("title")
        description = cls._html(soup, '[data-qa="vacancy-description"]') or job.get("description")
        employer_name = cls._text(soup, '[data-qa="vacancy-company__details"]') or cls._name(job.get("hiringOrganization"))
        area_name = cls._text(soup, '[data-qa="vacancy-view-raw-address"]') or cls._name(job.get("jobLocation"))
        salary = cls._salary(job.get("baseSalary"))
        archived = cls._archived(soup)
        result: dict[str, Any] = {
            "id": vacancy_id, "name": title, "description": description,
            "published_at": job.get("datePosted"), "expires_at": job.get("validThrough"),
            "archived": archived, "employer": {"name": employer_name} if employer_name else {},
            "area": {"name": area_name} if area_name else {}, "salary": salary,
            "employment": cls._value(job.get("employmentType")),
        }
        return result

    @staticmethod
    def _text(soup: BeautifulSoup, selector: str) -> str | None:
        node = soup.select_one(selector)
        return node.get_text(" ", strip=True) if node else None

    @staticmethod
    def _html(soup: BeautifulSoup, selector: str) -> str | None:
        node = soup.select_one(selector)
        return "".join(str(child) for child in node.contents) if node else None

    @staticmethod
    def _name(value: Any) -> str | None:
        if isinstance(value, dict):
            return value.get("name") or value.get("address", {}).get("addressLocality")
        if isinstance(value, list) and value:
            return HHHtmlSource._name(value[0])
        return value if isinstance(value, str) else None

    @staticmethod
    def _value(value: Any) -> dict[str, str] | None:
        if isinstance(value, str):
            return {"id": value, "name": value}
        return None

    @staticmethod
    def _salary(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        specification = value.get("value") if isinstance(value.get("value"), dict) else value
        return {"from": specification.get("minValue"), "to": specification.get("maxValue"),
                "currency": specification.get("currency") or value.get("currency")}

    @staticmethod
    def _archived(soup: BeautifulSoup) -> bool | None:
        text = soup.get_text(" ", strip=True).casefold()
        if "вакансия в архиве" in text or "вакансия закрыта" in text:
            return True
        return None

    @staticmethod
    def _json_ld(soup: BeautifulSoup) -> Iterable[dict[str, Any]]:
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                value = json.loads(script.string or script.get_text())
            except (TypeError, json.JSONDecodeError):
                continue
            rows = value if isinstance(value, list) else value.get("@graph", [value]) if isinstance(value, dict) else []
            for row in rows:
                if isinstance(row, dict):
                    yield row
