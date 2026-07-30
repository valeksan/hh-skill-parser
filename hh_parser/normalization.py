"""Normalize HH API/HTML vacancy payloads before SQLite storage."""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from typing import Any

from bs4 import BeautifulSoup

from .storage import json_value, utc_now

REDACTED_KEYS = {
    "address", "contacts", "contact", "contact_person", "email", "emails", "phone",
    "phones", "metro", "lat", "lng", "latitude", "longitude", "coordinates",
}
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
PHONE_RE = re.compile(r"(?<!\d)(?:\+?7|8)[\s().-]*\d(?:[\s().-]*\d){8,10}(?!\d)")
ADDRESS_LINE_RE = re.compile(r"(?im)^\s*(?:адрес|место работы)\s*:\s*[^\n<]+")
REDACTION_VERSION = "1"


def sanitize_text(value: str | None) -> tuple[str, list[str]]:
    """Remove common contact data and explicit address lines from text."""
    text = value or ""
    types: list[str] = []
    text, emails = EMAIL_RE.subn("[redacted-email]", text)
    if emails:
        types.append("email")
    text, phones = PHONE_RE.subn("[redacted-phone]", text)
    if phones:
        types.append("phone")
    text, addresses = ADDRESS_LINE_RE.subn("[redacted-address]", text)
    if addresses:
        types.append("address")
    return text, types


def redact_payload(value: Any, removed: set[str] | None = None) -> tuple[Any, list[str]]:
    """Drop structured personal/exact-location fields recursively."""
    removed = removed if removed is not None else set()
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized_key = key.casefold()
            if normalized_key in REDACTED_KEYS:
                removed.add(normalized_key)
                continue
            result[key], _ = redact_payload(item, removed)
        return result, sorted(removed)
    if isinstance(value, list):
        return [redact_payload(item, removed)[0] for item in value], sorted(removed)
    if isinstance(value, str):
        sanitized, text_types = sanitize_text(value)
        removed.update(text_types)
        return sanitized, sorted(removed)
    return value, sorted(removed)


def _text_from_html(description_html: str) -> str:
    return BeautifulSoup(description_html, "html.parser").get_text(" ", strip=True)


def _compressed_json(payload: Any) -> tuple[bytes, str]:
    raw_json = json_value(payload).encode("utf-8")
    return gzip.compress(raw_json), hashlib.sha256(raw_json).hexdigest()


def _id(value: Any) -> str | None:
    """Return stable HH ID from an object/string, leaving absent values NULL."""
    if isinstance(value, dict):
        value = value.get("id")
    return str(value) if value is not None else None


def _public_list(values: Any) -> list[dict[str, Any]]:
    """Keep only public ID/name pairs from a multi-value API field."""
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        if isinstance(value, dict):
            result.append({key: value[key] for key in ("id", "name") if value.get(key) is not None})
        elif isinstance(value, str):
            result.append({"name": value})
    return result


def _snapshot(
    data: dict[str, Any], *, source: str, raw_payload: Any, observed_at: str | None = None
) -> dict[str, Any]:
    redacted_payload, redaction_types = redact_payload(raw_payload)
    description_html, html_types = sanitize_text(data.get("description", ""))
    description_text, text_types = sanitize_text(_text_from_html(description_html))
    redaction_types = sorted(set(redaction_types + html_types + text_types))

    employer = data.get("employer") or {}
    area = data.get("area") or {}
    salary = data.get("salary_range") or data.get("salary") or {}
    frequency = salary.get("frequency") or salary.get("mode") or {}
    if isinstance(frequency, dict):
        frequency = frequency.get("id") or frequency.get("name")

    work_format = data.get("work_format") or data.get("work_formats") or []
    if not isinstance(work_format, list):
        work_format = [work_format]
    roles = _public_list(data.get("professional_roles"))
    industries = _public_list(data.get("industries"))
    key_skills = _public_list(data.get("key_skills"))
    languages = _public_list(data.get("languages"))
    department = data.get("department") or {}

    fingerprint = {
        "title": data.get("name", ""), "description_html": description_html,
        "published_at": data.get("published_at"), "created_at": data.get("created_at"),
        "expires_at": data.get("expires_at"), "archived": data.get("archived"),
        "employer_id": employer.get("id"), "employer_name": employer.get("name"),
        "raw": redacted_payload,
    }
    content_hash = hashlib.sha256(json_value(fingerprint).encode("utf-8")).hexdigest()
    compressed, raw_hash = _compressed_json(redacted_payload)
    return {
        "observed_at": observed_at or utc_now(), "content_hash": content_hash,
        "title": data.get("name", ""), "description_html": description_html,
        "description_text": description_text, "published_at": data.get("published_at"),
        "created_at": data.get("created_at"), "expires_at": data.get("expires_at"),
        "archived": data.get("archived"), "employer_id": employer.get("id"),
        "employer_name": employer.get("name"), "area_id": area.get("id"),
        "area_name": area.get("name"), "salary_from": salary.get("from"),
        "salary_to": salary.get("to"), "salary_currency": salary.get("currency"),
        "salary_gross": salary.get("gross"), "salary_frequency": frequency, "source": source,
        "employer_type": employer.get("type"), "employer_trusted": employer.get("trusted"),
        "employer_accredited_it": employer.get("accredited_it_employer"),
        "experience_id": _id(data.get("experience")), "employment_id": _id(data.get("employment")),
        "schedule_id": _id(data.get("schedule")), "work_formats": _public_list(work_format),
        "roles": roles, "industries": industries, "key_skills": key_skills, "languages": languages,
        "department_id": _id(department), "department_name": department.get("name"),
        "vacancy_type_id": _id(data.get("type")),
        "completeness": {"description": bool(description_text), "published_at": bool(data.get("published_at"))},
        "raw_payload": compressed, "raw_content_type": "application/json",
        "raw_compression": "gzip", "raw_size": len(compressed), "raw_hash": raw_hash,
        "redaction_applied": bool(redaction_types), "redaction_version": REDACTION_VERSION,
        "redaction_types": redaction_types,
    }


def normalize_api_vacancy(data: dict[str, Any], *, observed_at: str | None = None) -> dict[str, Any]:
    """Normalize one API detail response into storage snapshot fields."""
    return _snapshot(data, source="api", raw_payload=data, observed_at=observed_at)


def normalize_html_vacancy(
    data: dict[str, Any], html_text: str, *, observed_at: str | None = None
) -> dict[str, Any]:
    """Normalize parsed HTML card and store only redacted compressed HTML payload."""
    sanitized_html, redaction_types = sanitize_text(html_text)
    snapshot = _snapshot(data, source="html", raw_payload={"html": sanitized_html}, observed_at=observed_at)
    raw_bytes = sanitized_html.encode("utf-8")
    snapshot["raw_payload"] = gzip.compress(raw_bytes)
    snapshot["raw_content_type"] = "text/html"
    snapshot["raw_size"] = len(snapshot["raw_payload"])
    snapshot["raw_hash"] = hashlib.sha256(raw_bytes).hexdigest()
    snapshot["redaction_types"] = sorted(set(snapshot["redaction_types"] + redaction_types))
    snapshot["redaction_applied"] = bool(snapshot["redaction_types"])
    return snapshot
