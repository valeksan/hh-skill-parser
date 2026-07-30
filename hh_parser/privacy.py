"""Privacy-safe display helpers for analyst-facing output."""

from __future__ import annotations

import hashlib


PRIVATE_EMPLOYER_TYPES = frozenset({"individual", "private", "person", "entrepreneur", "ip", "ип"})


def is_private_employer(employer_type: object, employer_name: object) -> bool:
    """Identify HH private-employer variants without treating companies as people."""
    kind = str(employer_type or "").strip().casefold()
    name = str(employer_name or "").strip().casefold()
    return kind in PRIVATE_EMPLOYER_TYPES or name.startswith("ип ") or name.startswith("ип.")


def safe_employer_name(employer_id: object, employer_type: object, employer_name: object) -> str | None:
    """Hide personal display names while keeping a stable analysis pseudonym."""
    if employer_name in (None, ""):
        return None
    if not is_private_employer(employer_type, employer_name):
        return str(employer_name)
    stable_value = str(employer_id or employer_name)
    digest = hashlib.sha256(stable_value.encode("utf-8")).hexdigest()[:12]
    return f"private-employer-{digest}"
