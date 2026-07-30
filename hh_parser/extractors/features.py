"""Deterministic feature extraction from normalized vacancy snapshots."""

from __future__ import annotations

import re
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

VERSION = "3"

TOPICS = {
    "mobilization": (r"мобилизац",),
    "military_registration": (r"воинск", r"военно[- ]уч[её]т"),
    "reservation": (r"бронирован", r"отсрочк"),
    "civil_defense": (r"гражданск.{0,20}оборон", r"\bго\s*(?:и|/)\s*чс\b"),
    "state_secrets": (r"гостайн", r"режимно[- ]секрет", r"секретн.{0,20}делопроизвод"),
}
SIGNALS = {
    "security_clearance": (
        r"допуск\w{0,20}\s+(?:к\s+)?(?:государственн\w*\s+)?тайн",
        r"форм[аы]\s+допуск",
    ),
    "military_id": (r"военн\w{0,20}\s+билет",),
    "wartime": (r"военн\w{0,20}\s+врем",),
    "exercise": (r"(?:учен\w*|тренировк\w*)\s+(?:по\s+)?(?:го|чс|гражданск)",),
    "government_interaction": (
        r"(?:взаимодейств\w*|переписк\w*)[^.\n]{0,80}(?:военкомат|военн\w* комиссариат|минобороны|мчс)",
    ),
}
SENIORITY = (
    ("head", ("руководител", "начальник", "директор")),
    ("senior", ("старш", "ведущ", "главн")),
    ("junior", ("младш", "стажер", "стажёр", "ассистент")),
)


def _feature(name: str, value_type: str, value: Any) -> dict[str, Any]:
    field = {"boolean": "value_number", "number": "value_number", "text": "value_text", "json": "value_json"}[value_type]
    if value_type == "boolean":
        value = int(bool(value))
    return {"name": name, "value_type": value_type, field: value}


def _parse_iso_time(value: Any) -> datetime | None:
    """Parse HH ISO timestamp, preserving absent/unparseable source values as NULL."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc)


def _normalized_text(value: Any) -> str:
    return re.sub(r"\W+", " ", str(value or "").casefold().replace("ё", "е")).strip()


def repost_fingerprint(snapshot: dict[str, Any]) -> str:
    """Stable non-reversible key: normalized title + employer + sanitized text hash."""
    description_hash = hashlib.sha256(_normalized_text(snapshot.get("description_text")).encode("utf-8")).hexdigest()
    employer = str(snapshot.get("employer_id") or _normalized_text(snapshot.get("employer_name")))
    key = {"title": _normalized_text(snapshot.get("title")), "employer": employer, "description": description_hash}
    return hashlib.sha256(json.dumps(key, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def extract_features(snapshot: dict[str, Any], *, repost_count: int = 1) -> list[dict[str, Any]]:
    """Return stable topic, text, salary, publication, and job-condition features."""
    title = snapshot.get("title") or ""
    description = snapshot.get("description_text") or ""
    text = f"{title}\n{description}".casefold()
    features = [
        _feature("description.length", "number", len(description)),
        _feature("description.present", "boolean", bool(description)),
        _feature("salary.present", "boolean", snapshot.get("salary_from") is not None or snapshot.get("salary_to") is not None),
        _feature("work.remote", "boolean", any("удален" in str(item.get("name", "")).casefold() or "remote" in str(item.get("id", "")).casefold() for item in snapshot.get("work_formats", []))),
        _feature("duplicate.repost_fingerprint", "text", repost_fingerprint(snapshot)),
        _feature("duplicate.repost_count", "number", repost_count),
        _feature("duplicate.is_repost", "boolean", repost_count > 1),
    ]
    for name, patterns in TOPICS.items():
        evidence = [pattern for pattern in patterns if re.search(pattern, text)]
        features.append(_feature(f"topic.{name}", "json", evidence))
    for name, patterns in SIGNALS.items():
        evidence = [pattern for pattern in patterns if re.search(pattern, text)]
        features.extend((
            _feature(f"signal.{name}", "boolean", evidence),
            _feature(f"signal.{name}.evidence", "json", evidence),
        ))
    for level, words in SENIORITY:
        if any(word in title.casefold() for word in words):
            features.append(_feature("title.seniority", "text", level))
            break
    else:
        features.append(_feature("title.seniority", "text", "unknown"))
    values = [value for value in (snapshot.get("salary_from"), snapshot.get("salary_to")) if value is not None]
    if values:
        features.append(_feature("salary.midpoint", "number", sum(values) / len(values)))
    for source, target in (("employment_id", "employment.id"), ("experience_id", "experience.id"), ("schedule_id", "schedule.id")):
        if snapshot.get(source):
            features.append(_feature(target, "text", snapshot[source]))
    published = snapshot.get("published_at")
    published_time = _parse_iso_time(published)
    if published_time:
        features.extend((
            _feature("publication.month", "text", published_time.strftime("%Y-%m")),
            _feature("publication.day", "text", published_time.date().isoformat()),
            _feature("publication.week", "text", f"{published_time:%G}-W{published_time:%V}"),
        ))
        observed_time = _parse_iso_time(snapshot.get("observed_at"))
        if observed_time:
            features.append(_feature(
                "publication.age_days", "number",
                max(0, (observed_time.astimezone(timezone.utc).date() - published_time.astimezone(timezone.utc).date()).days),
            ))
    monthly_frequency = str(snapshot.get("salary_frequency") or "").casefold()
    rub_currency = str(snapshot.get("salary_currency") or "").upper() in {"RUB", "RUR"}
    if values and rub_currency and monthly_frequency in {"month", "monthly", "месяц"}:
        features.append(_feature("salary.monthly_rub", "number", sum(values) / len(values)))
    return features
