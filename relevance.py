"""Deterministic, explainable candidate relevance; never filters collection."""

from __future__ import annotations

import re

VERSION = "1"
STRONG = ("мобилизац", "воинск", "военно-учет", "гражданск.*оборон", "гостайн", "режимно-секрет")


def classify_relevance(title: str, description: str) -> tuple[str, float, list[str]]:
    text = f"{title}\n{description}".casefold()
    reasons = [f"signal:{pattern}" for pattern in STRONG if re.search(pattern, text)]
    score = float(len(reasons))
    return ("relevant" if score >= 1 else "borderline", score, reasons)
