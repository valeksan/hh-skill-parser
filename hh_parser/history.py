"""Stable history keys for normalized vacancy snapshots."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


REPOST_KEY_VERSION = "1"


def _normalized_text(value: Any) -> str:
    return re.sub(r"\W+", " ", str(value or "").casefold().replace("ё", "е")).strip()


def repost_key(snapshot: dict[str, Any]) -> str:
    """Return non-reversible key for separately posted equivalent vacancy text.

    Key deliberately excludes HH vacancy ID and state/timestamps. It is calculated
    only from already-redacted normalized fields, so matching IDs are candidates
    for repost analysis, not a claim that postings are identical entities.
    """
    description_hash = hashlib.sha256(_normalized_text(snapshot.get("description_text")).encode("utf-8")).hexdigest()
    employer = str(snapshot.get("employer_id") or _normalized_text(snapshot.get("employer_name")))
    material = {"title": _normalized_text(snapshot.get("title")), "employer": employer, "description": description_hash}
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
