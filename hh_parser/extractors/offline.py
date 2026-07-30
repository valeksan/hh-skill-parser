"""Offline derived-data extraction over already sanitized SQLite snapshots."""

from __future__ import annotations

from typing import Any

from relevance import VERSION as RELEVANCE_VERSION, classify_relevance

from ..skill_dictionary import SkillDictionary, topic_family
from ..storage import Database
from .features import VERSION as FEATURES_VERSION, extract_features


def extract(
    database: Database, kind: str, *, snapshot_scope: str = "latest", run_ids: list[int] | None = None,
    area_ids: list[str] | None = None, source: str | None = None, date_from: str | None = None,
    date_to: str | None = None, skill_dictionary: SkillDictionary | None = None,
) -> dict[str, int]:
    """Rebuild one deterministic derived layer without changing source rows."""
    if kind == "relevance":
        version = RELEVANCE_VERSION
    elif kind == "features":
        version = FEATURES_VERSION
    elif kind == "skills" and skill_dictionary:
        version = skill_dictionary.version
    elif kind == "skills":
        raise ValueError("skills extraction requires --skills-file")
    else:
        raise ValueError(f"unsupported extraction kind: {kind}")
    selection = {"snapshot_scope": snapshot_scope, "run_ids": run_ids or [], "area_ids": area_ids or [],
                 "source": source, "date_from": date_from, "date_to": date_to}
    snapshots = database.selected_snapshots(**selection)
    extraction_run_id = database.start_extraction_run(kind, version, selection, len(snapshots))
    skill_ids = database.sync_skill_dictionary(skill_dictionary.aliases, version, topic_family) if skill_dictionary else {}
    processed = errors = 0
    for snapshot in snapshots:
        snapshot_id = int(snapshot["id"])
        try:
            if kind == "relevance":
                label, score, reasons = classify_relevance(snapshot["title"], snapshot.get("description_text") or "")
                database.upsert_auto_relevance(snapshot_id, label, score, reasons, version)
            elif kind == "features":
                database.upsert_features(snapshot_id, extract_features(snapshot), version)
            else:
                matches = []
                for match_source, text in (("title", snapshot.get("title") or ""), ("description", snapshot.get("description_text") or "")):
                    matches.extend((canonical, match_source, alias, count) for canonical, alias, count in skill_dictionary.matches(text))
                for skill in snapshot.get("key_skills", []):
                    if isinstance(skill, dict) and skill.get("name"):
                        matches.extend((canonical, "key_skill", alias, count) for canonical, alias, count in skill_dictionary.matches(str(skill["name"])))
                database.upsert_vacancy_skills(snapshot_id, matches, skill_ids, version)
            processed += 1
        except Exception as error:
            errors += 1
            database.record_extraction_error(extraction_run_id, snapshot_id, error)
    database.finish_extraction_run(extraction_run_id, processed, errors)
    return {"run_id": extraction_run_id, "selected": len(snapshots), "processed": processed, "errors": errors}
