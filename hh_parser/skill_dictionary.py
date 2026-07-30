"""Versioned local skill dictionary and deterministic text matching."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

VERSION = "1"


def normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def topic_family(name: str) -> str | None:
    if "воинск" in name or "военн" in name or "запас" in name:
        return "military_registration"
    if "мобилизац" in name or "брониров" in name or "отсроч" in name:
        return "mobilization"
    if "гражданск" in name or "чрезвычайн" in name:
        return "civil_defense"
    if "гостайн" in name or "секрет" in name:
        return "state_secrets"
    return None


@dataclass(frozen=True)
class SkillDictionary:
    aliases: dict[str, str]
    version: str

    @property
    def canonical(self) -> set[str]:
        return set(self.aliases.values())

    def matches(self, text: str) -> list[tuple[str, str, int]]:
        result = []
        for alias, canonical in sorted(self.aliases.items(), key=lambda item: (-len(item[0]), item[0])):
            count = len(re.findall(rf"(?<!\w){re.escape(alias)}(?!\w)", text, flags=re.IGNORECASE))
            if count:
                result.append((canonical, alias, count))
        return result


def load_skill_dictionary(path: str | Path) -> SkillDictionary:
    """Load canonical `name | alias` lines with global alias-conflict check."""
    aliases: dict[str, str] = {}
    payload: list[str] = []
    for line_number, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        values = [normalize(value) for value in line.split("|") if normalize(value)]
        if not values:
            continue
        canonical = values[0]
        payload.append("|".join(values))
        for alias in values:
            existing = aliases.get(alias)
            if existing is not None and existing != canonical:
                raise ValueError(f"skill alias {alias!r} at line {line_number} already belongs to {existing!r}")
            aliases[alias] = canonical
    if not aliases:
        raise ValueError("skill dictionary contains no active aliases")
    digest = hashlib.sha256("\n".join(payload).encode("utf-8")).hexdigest()[:16]
    return SkillDictionary(aliases, f"{VERSION}:{digest}")
