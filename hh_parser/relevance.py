"""Deterministic, explainable candidate relevance; never filters collection."""

from __future__ import annotations

import re

VERSION = "2"
STRONG = ("мобилизац", "воинск", "военно-учет", "гражданск.*оборон", "гостайн", "режимно-секрет")
BENEFIT_MOBILIZATION = (
    r"(?:отсрочк\w*|брон[ьяи]?|бронировани\w*)[^.\n]{0,80}мобилизац",
    r"мобилизац[^.\n]{0,80}(?:отсрочк\w*|брон[ьяи]?|бронировани\w*)",
)


def _has_non_benefit_signal(title: str, description: str) -> bool:
    """Identify subject-matter work that outweighs an employer benefit statement."""
    title_text = title.casefold()
    text = f"{title}\n{description}".casefold()
    return bool(re.search(STRONG[0], title_text)) or any(re.search(pattern, text) for pattern in STRONG[1:])


def classify_relevance(title: str, description: str) -> tuple[str, float, list[str]]:
    text = f"{title}\n{description}".casefold()
    benefit_reasons = [f"exclude:benefit:{pattern}" for pattern in BENEFIT_MOBILIZATION if re.search(pattern, text)]
    if benefit_reasons and not _has_non_benefit_signal(title, description):
        return "irrelevant", 0.0, benefit_reasons
    reasons = [f"signal:{pattern}" for pattern in STRONG if re.search(pattern, text)]
    score = float(len(reasons))
    return ("relevant" if score >= 1 else "borderline", score, reasons)
