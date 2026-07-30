"""Offline deterministic skill-candidate mining from sanitized SQLite corpus."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .skill_dictionary import SkillDictionary, load_skill_dictionary
from .storage import Database

FIELDS = ("candidate", "normalized", "source", "document_count", "relevance_lift", "area_coverage", "time_coverage", "query_family_coverage", "strata_coverage", "marginal_gain", "example_hh_ids", "example_titles", "evidence", "evidence_hash", "decision", "canonical_skill", "topic_family", "reviewer_reason")
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


def _evidence_hash(row: dict[str, Any]) -> str:
    fields = ("normalized", "source", "document_count", "example_hh_ids", "evidence")
    return hashlib.sha256(json.dumps({key: row[key] for key in fields}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def discover_skill_candidates(database: Database, dictionary: SkillDictionary, *, min_document_frequency: int = 2) -> list[dict[str, Any]]:
    """Rank unknown key-skill/n-gram candidates; reads only sanitized local data."""
    if min_document_frequency < 1:
        raise ValueError("min document frequency must be positive")
    snapshots = database.selected_snapshots(snapshot_scope="latest")
    with database.connect() as connection:
        labels = {int(row["snapshot_id"]): row["effective_label"] for row in connection.execute("SELECT snapshot_id, effective_label FROM effective_relevance_labels")}
    known = {normalize_candidate(alias) for alias in dictionary.aliases}
    with database.connect() as connection:
        excluded = known | {
            normalize_candidate(value)
            for row in connection.execute(
                "SELECT employer_name, area_name, federal_district, federal_subject, locality FROM vacancy_snapshots"
            ) for value in row if value
        }
        query_words = {
            word for row in connection.execute("SELECT expression FROM search_queries")
            for word in _ngrams(str(row["expression"]))
        }
        families: dict[str, set[str]] = defaultdict(set)
        for row in connection.execute(
            "SELECT h.vacancy_hh_id, q.query_group FROM vacancy_query_hits h JOIN search_queries q ON q.id = h.query_id"
        ):
            families[str(row["vacancy_hh_id"])].add(str(row["query_group"] or "ungrouped"))
    rejected = database.rejected_skill_candidates()
    stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"docs": set(), "relevant": set(), "irrelevant": set(), "sources": set(), "examples": [], "areas": set(), "times": set(), "families": set()})
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
                if not phrase or phrase in excluded or phrase in query_words or phrase in STOPWORDS:
                    continue
                item = stats[phrase]
                item["docs"].add(snapshot_id)
                item["sources"].add(source)
                if label in {"relevant", "borderline"}:
                    item["relevant"].add(snapshot_id)
                elif label == "irrelevant":
                    item["irrelevant"].add(snapshot_id)
                item["areas"].add(str(snapshot.get("area_id") or "unknown"))
                item["times"].add(str(snapshot.get("published_at") or snapshot.get("observed_at") or "unknown")[:7])
                item["families"].update(families.get(str(snapshot["vacancy_hh_id"]), {"unqueried"}))
                if len(item["examples"]) < 3:
                    item["examples"].append((str(snapshot["vacancy_hh_id"]), str(snapshot.get("title") or ""), (snapshot.get("description_text") or "")[:180]))
    rows = []
    for phrase, item in stats.items():
        count = len(item["docs"])
        if count < min_document_frequency:
            continue
        lift = (len(item["relevant"]) / relevant_docs if relevant_docs else 0) - (len(item["irrelevant"]) / irrelevant_docs if irrelevant_docs else 0)
        examples = sorted(item["examples"])
        area_coverage, time_coverage, family_coverage = len(item["areas"]), len(item["times"]), len(item["families"])
        row = {
            "candidate": phrase, "normalized": phrase, "source": "|".join(sorted(item["sources"])),
            "document_count": count, "relevance_lift": round(lift, 6),
            "area_coverage": area_coverage, "time_coverage": time_coverage, "query_family_coverage": family_coverage,
            "strata_coverage": area_coverage * time_coverage * family_coverage,
            "marginal_gain": round(max(lift, 0) * count * (1 + area_coverage + time_coverage + family_coverage), 6),
            "example_hh_ids": "|".join(value[0] for value in examples),
            "example_titles": "|".join(value[1] for value in examples), "evidence": " | ".join(value[2] for value in examples),
            "decision": "", "canonical_skill": "", "topic_family": "", "reviewer_reason": "",
        }
        row["evidence_hash"] = _evidence_hash(row)
        if (phrase, row["evidence_hash"]) not in rejected:
            rows.append(row)
    return sorted(rows, key=lambda row: (-row["marginal_gain"], -row["relevance_lift"], -row["document_count"], row["candidate"]))


def export_skill_candidates(rows: list[dict[str, Any]], path: str | Path) -> int:
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def import_skill_candidates(review_path: str | Path, dictionary_path: str | Path, output_path: str | Path, *, database: Database | None = None, batch_id: str | None = None) -> int:
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
    if batch_id is not None:
        if database is None:
            raise ValueError("skill review batch requires database")
        database.record_skill_candidate_reviews(batch_id, [
            {"candidate": normalize_candidate(row.get("candidate") or ""), "decision": (row.get("decision") or "").strip().casefold(),
             "canonical_skill": normalize_candidate(row.get("canonical_skill") or ""), "reviewer_reason": row.get("reviewer_reason") or ""}
            for row in rows if (row.get("decision") or "").strip()
        ])
    return applied
