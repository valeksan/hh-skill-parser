"""Offline deterministic skill-candidate mining from sanitized SQLite corpus."""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .skill_dictionary import SkillDictionary, load_skill_dictionary
from .storage import Database

FIELDS = ("candidate", "normalized", "source", "document_count", "relevance_lift", "strata_coverage", "example_hh_ids", "example_titles", "evidence", "decision", "canonical_skill", "topic_family", "reviewer_reason")
STOPWORDS = frozenset("и в во на с по для от до за из к о об а но или не что как при под над без для опыт работа обязанности условия компания сотрудник специалист вакансия".split())
TOKEN_RE = re.compile(r"[a-zа-я0-9]+(?:-[a-zа-я0-9]+)*", re.I)


def normalize_candidate(value: str) -> str:
    return " ".join(TOKEN_RE.findall(value.casefold().replace("ё", "е")))


def _ngrams(text: str) -> set[str]:
    words = [word for word in TOKEN_RE.findall(text.casefold().replace("ё", "е")) if word not in STOPWORDS]
    result: set[str] = set()
    for width in range(1, 5):
        for index in range(len(words) - width + 1):
            phrase = " ".join(words[index:index + width])
            if len(phrase) >= 3:
                result.add(phrase)
    return result


def discover_skill_candidates(database: Database, dictionary: SkillDictionary, *, min_document_frequency: int = 2) -> list[dict[str, Any]]:
    """Rank unknown key-skill/n-gram candidates; reads only sanitized local data."""
    if min_document_frequency < 1:
        raise ValueError("min document frequency must be positive")
    snapshots = database.selected_snapshots(snapshot_scope="latest")
    with database.connect() as connection:
        labels = {int(row["snapshot_id"]): row["effective_label"] for row in connection.execute("SELECT snapshot_id, effective_label FROM effective_relevance_labels")}
    known = {normalize_candidate(alias) for alias in dictionary.aliases}
    stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"docs": set(), "relevant": set(), "irrelevant": set(), "sources": set(), "examples": [], "strata": set()})
    relevant_docs = irrelevant_docs = 0
    for snapshot in snapshots:
        snapshot_id = int(snapshot["id"])
        label = labels.get(snapshot_id, "unknown")
        if label in {"relevant", "borderline"}:
            relevant_docs += 1
        elif label == "irrelevant":
            irrelevant_docs += 1
        candidates = {"ngram": _ngrams(f"{snapshot.get('title') or ''} {snapshot.get('description_text') or ''}")}
        candidates["key_skill"] = {normalize_candidate(str(item.get("name"))) for item in snapshot.get("key_skills", []) if isinstance(item, dict) and item.get("name")}
        for source, phrases in candidates.items():
            for phrase in phrases:
                if not phrase or phrase in known or phrase in STOPWORDS:
                    continue
                item = stats[phrase]
                item["docs"].add(snapshot_id)
                item["sources"].add(source)
                if label in {"relevant", "borderline"}:
                    item["relevant"].add(snapshot_id)
                elif label == "irrelevant":
                    item["irrelevant"].add(snapshot_id)
                item["strata"].add((str(snapshot.get("area_id") or "unknown"), str(snapshot.get("published_at") or snapshot.get("observed_at") or "unknown")[:7]))
                if len(item["examples"]) < 3:
                    item["examples"].append((str(snapshot["vacancy_hh_id"]), str(snapshot.get("title") or ""), (snapshot.get("description_text") or "")[:180]))
    rows = []
    for phrase, item in stats.items():
        count = len(item["docs"])
        if count < min_document_frequency:
            continue
        lift = (len(item["relevant"]) / relevant_docs if relevant_docs else 0) - (len(item["irrelevant"]) / irrelevant_docs if irrelevant_docs else 0)
        examples = sorted(item["examples"])
        rows.append({
            "candidate": phrase, "normalized": phrase, "source": "|".join(sorted(item["sources"])),
            "document_count": count, "relevance_lift": round(lift, 6),
            "strata_coverage": len(item["strata"]), "example_hh_ids": "|".join(value[0] for value in examples),
            "example_titles": "|".join(value[1] for value in examples), "evidence": " | ".join(value[2] for value in examples),
            "decision": "", "canonical_skill": "", "topic_family": "", "reviewer_reason": "",
        })
    return sorted(rows, key=lambda row: (-row["relevance_lift"], -row["document_count"], row["candidate"]))


def export_skill_candidates(rows: list[dict[str, Any]], path: str | Path) -> int:
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def import_skill_candidates(review_path: str | Path, dictionary_path: str | Path, output_path: str | Path) -> int:
    """Apply reviewed candidates into a new immutable dictionary file."""
    source = Path(dictionary_path)
    output = Path(output_path)
    if source.resolve() == output.resolve():
        raise ValueError("--output must differ from --skills-file; dictionary versions are immutable")
    if output.exists():
        raise ValueError("--output already exists; dictionary versions are immutable")
    dictionary = SkillDictionary(dict(load_skill_dictionary(source).aliases), "")
    aliases = dict(dictionary.aliases)
    with Path(review_path).open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"candidate", "decision", "canonical_skill"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError("skill candidate CSV requires candidate, decision, canonical_skill columns")
        rows = list(reader)
    handled: set[str] = set()
    applied = 0
    for row in rows:
        candidate = normalize_candidate(row.get("candidate") or "")
        decision = (row.get("decision") or "").strip().casefold()
        if not decision:
            continue
        if decision not in {"approve", "reject", "merge"}:
            raise ValueError(f"invalid candidate decision: {decision}")
        if not candidate or candidate in handled:
            raise ValueError("candidate decisions must be non-empty and unique")
        handled.add(candidate)
        if decision == "reject":
            continue
        canonical = normalize_candidate(row.get("canonical_skill") or candidate)
        if not canonical:
            raise ValueError("approve/merge requires canonical_skill")
        if decision == "merge" and canonical not in set(aliases.values()):
            raise ValueError(f"merge target is not an existing canonical skill: {canonical}")
        existing = aliases.get(candidate)
        if existing is not None and existing != canonical:
            raise ValueError(f"candidate alias {candidate!r} already belongs to {existing!r}")
        aliases[canonical] = canonical
        aliases[candidate] = canonical
        applied += 1
    grouped: dict[str, list[str]] = defaultdict(list)
    for alias, canonical in aliases.items():
        grouped[canonical].append(alias)
    lines = []
    for canonical in sorted(grouped):
        lines.append(" | ".join([canonical, *sorted(alias for alias in grouped[canonical] if alias != canonical)]))
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return applied
