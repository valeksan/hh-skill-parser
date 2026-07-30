"""Deterministic feature extraction from normalized vacancy snapshots."""

from __future__ import annotations

import re
from typing import Any

VERSION = "1"

TOPICS = {
    "mobilization": (r"мобилизац",),
    "military_registration": (r"воинск", r"военно[- ]уч[её]т"),
    "reservation": (r"бронирован", r"отсрочк"),
    "civil_defense": (r"гражданск.{0,20}оборон", r"\bго\s*(?:и|/)\s*чс\b"),
    "state_secrets": (r"гостайн", r"режимно[- ]секрет", r"секретн.{0,20}делопроизвод"),
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


def extract_features(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Return stable topic, text, salary, publication, and job-condition features."""
    title = snapshot.get("title") or ""
    description = snapshot.get("description_text") or ""
    text = f"{title}\n{description}".casefold()
    features = [
        _feature("description.length", "number", len(description)),
        _feature("description.present", "boolean", bool(description)),
        _feature("salary.present", "boolean", snapshot.get("salary_from") is not None or snapshot.get("salary_to") is not None),
        _feature("work.remote", "boolean", any("удален" in str(item.get("name", "")).casefold() or "remote" in str(item.get("id", "")).casefold() for item in snapshot.get("work_formats", []))),
    ]
    for name, patterns in TOPICS.items():
        evidence = [pattern for pattern in patterns if re.search(pattern, text)]
        features.append(_feature(f"topic.{name}", "json", evidence))
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
    if published:
        features.append(_feature("publication.month", "text", str(published)[:7]))
    return features
