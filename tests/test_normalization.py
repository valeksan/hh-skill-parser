import csv
import gzip
import hashlib
import json
import os
import tempfile
from datetime import date
import unittest
from pathlib import Path
from unittest import mock

import requests

from hh_parser.storage import Database
from hh_parser.normalization import normalize_api_vacancy, normalize_html_vacancy
from hh_parser.sources.html import HHHtmlAntiBotError, HHHtmlSource
from hh_parser.areas import (
    AreaSelectionError, find_overlaps, flatten_area_tree, parse_area_lines,
    resolve_russia_geography, select_catalog_areas, validate_area_ids,
)
from hh_parser.collector import Collector, split_date_window
from hh_parser.extractors.offline import extract as run_offline_extraction
from hh_parser.extractors.features import extract_features
from hh_parser.history import repost_key
from hh_parser.export import export_marts, export_query_hits, export_roles, export_skills, export_vacancies
from hh_parser.discovery import discover_skill_candidates, import_skill_candidates
from hh_parser.stats import vacancy_stats
from hh_parser.cli import (
    apply_defaults, build_parser as build_research_parser, run_collect, run_coverage, run_resume, run_retry, resolve_collection_window,
    run_areas_list, run_areas_sync, run_areas_validate, run_export_labeling,
    run_db, run_discover, run_import_labeling, run_import_skill_candidates, run_extract, run_export_skills, run_export_vacancies, run_maintenance, run_stats,
)
from hh_parser.config import cli_defaults, load_config
from hh_parser.labeling import stratified_sample
from hh_parser.pilot import create_pilot, pilot_report
from hh_parser.query_specs import QuerySpec, load_query_specs
from hh_parser.skill_dictionary import load_skill_dictionary
from hh_parser.relevance import classify_relevance
from hh_parser.sources.api import HHApiSource


class NormalizationTests(unittest.TestCase):
    def test_api_normalization_converts_source_timestamps_to_utc_and_keeps_offsets(self):
        snapshot = normalize_api_vacancy({
            "id": "1", "name": "Специалист",
            "published_at": "2026-07-01T00:30:00+03:00",
            "created_at": "2026-06-30T23:00:00-04:00",
            "expires_at": "2026-08-01T00:00:00Z",
        })

        self.assertEqual(snapshot["published_at"], "2026-06-30T21:30:00+00:00")
        self.assertEqual(snapshot["published_at_source_offset"], "+03:00")
        self.assertEqual(snapshot["created_at"], "2026-07-01T03:00:00+00:00")
        self.assertEqual(snapshot["created_at_source_offset"], "-04:00")
        self.assertEqual(snapshot["expires_at_source_offset"], "+00:00")

        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "research.sqlite3")
            database.migrate()
            run_id = database.start_run({"fixture": "timestamps"})
            database.upsert_vacancy("1", source="api")
            database.record_snapshot(run_id, "1", snapshot)
            with database.connect() as connection:
                stored = connection.execute(
                    "SELECT published_at, published_at_source_offset, created_at_source_offset "
                    "FROM vacancy_snapshots"
                ).fetchone()
        self.assertEqual(tuple(stored), ("2026-06-30T21:30:00+00:00", "+03:00", "-04:00"))

    def test_api_normalization_redacts_contacts_and_exact_location_before_compression(self):
        snapshot = normalize_api_vacancy({
            "id": "1", "name": "Специалист", "description": "Пишите user@example.com или +7 (999) 123-45-67",
            "contacts": {"email": "user@example.com", "phones": [{"number": "+79991234567"}]},
            "address": {"street": "Ленина", "lat": 55.75, "lng": 37.62, "metro": {"name": "Охотный ряд"}},
            "employer": {"id": "42", "name": "АО Пример"}, "area": {"id": "1", "name": "Москва"},
            "experience": {"id": "between1And3", "name": "От 1 года до 3 лет"},
            "employment": {"id": "full", "name": "Полная занятость"},
            "schedule": {"id": "fullDay", "name": "Полный день"},
            "work_format": [{"id": "REMOTE", "name": "Удалённо"}],
            "professional_roles": [{"id": "1", "name": "Специалист"}],
            "industries": [{"id": "7.540", "name": "Оборонная промышленность"}],
            "key_skills": [{"name": "Воинский учет"}],
            "languages": [{"id": "rus", "name": "Русский"}],
            "department": {"id": "1", "name": "Первый отдел"}, "type": {"id": "open"},
            "salary": {"from": 100000, "to": None, "currency": "RUR", "gross": False},
        }, observed_at="2026-01-01T00:00:00+00:00")

        raw = gzip.decompress(snapshot["raw_payload"]).decode("utf-8")
        self.assertNotIn("user@example.com", raw)
        self.assertNotIn("123-45-67", raw)
        self.assertNotIn("Ленина", raw)
        self.assertIn("[redacted-email]", snapshot["description_text"])
        self.assertEqual(snapshot["employer_id"], "42")
        self.assertEqual(snapshot["area_name"], "Москва")
        self.assertEqual(snapshot["experience_id"], "between1And3")
        self.assertEqual(snapshot["employment_id"], "full")
        self.assertEqual(snapshot["work_formats"], [{"id": "REMOTE", "name": "Удалённо"}])
        self.assertEqual(snapshot["roles"], [{"id": "1", "name": "Специалист"}])
        self.assertEqual(snapshot["key_skills"], [{"name": "Воинский учет"}])
        self.assertEqual(snapshot["department_name"], "Первый отдел")
        self.assertTrue(snapshot["completeness"]["fields"]["description"]["present"])
        self.assertEqual(snapshot["completeness"]["fields"]["description"]["source"], "api")
        self.assertEqual(
            snapshot["completeness"]["fields"]["published_at"]["missing_reason"],
            "not_provided_by_source",
        )
        self.assertTrue(snapshot["redaction_applied"])

    def test_redaction_is_recursive_and_raw_hash_uses_only_redacted_payload(self):
        payload = {
            "id": "nested", "name": "Специалист", "description": "location: Secret street 1",
            "metadata": [{"contactPerson": {"email": "nested@example.com"}, "telegram": "@private"}],
            "office": {"geo_lat": 55.75, "geo_lon": 37.62},
        }
        snapshot = normalize_api_vacancy(payload)
        raw = gzip.decompress(snapshot["raw_payload"])
        self.assertNotIn(b"nested@example.com", raw)
        self.assertNotIn(b"@private", raw)
        self.assertNotIn(b"55.75", raw)
        self.assertNotIn(b"Secret street", raw)
        self.assertEqual(snapshot["raw_hash"], hashlib.sha256(raw).hexdigest())
        self.assertNotEqual(snapshot["raw_hash"], hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest())

    def test_inspect_raw_writes_one_decompressed_payload_without_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "research.sqlite3")
            database.migrate()
            run_id = database.start_run({"fixture": "inspect-raw"})
            database.upsert_vacancy("1", source="api")
            snapshot = normalize_api_vacancy({"id": "1", "name": "Специалист", "description": "mail@example.com"})
            database.record_snapshot(run_id, "1", snapshot)
            snapshot_id = database.snapshot_id("1", snapshot["content_hash"])
            output = Path(temp_dir) / "raw.json"
            settings = build_research_parser().parse_args([
                "maintenance", "--database", str(database.path), "inspect-raw",
                "--snapshot-id", str(snapshot_id), "--output", str(output),
            ])
            result = run_maintenance(settings)
            inspected = output.read_text(encoding="utf-8")
            with database.connect() as connection:
                stored = connection.execute("SELECT raw_payload FROM vacancy_snapshots WHERE id = ?", (snapshot_id,)).fetchone()[0]
        self.assertEqual(result["content_type"], "application/json")
        self.assertIn("[redacted-email]", inspected)
        self.assertNotIn("mail@example.com", inspected)
        self.assertIsNotNone(stored)

    def test_snapshot_metadata_creates_normalized_multivalue_links(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "research.sqlite3")
            database.migrate()
            run_id = database.start_run({"fixture": True})
            database.upsert_vacancy("1", source="api")
            snapshot = normalize_api_vacancy({
                "id": "1", "name": "Специалист",
                "work_format": [{"id": "REMOTE", "name": "Удалённо"}],
                "key_skills": [{"name": "Воинский учет"}],
                "professional_roles": [{"id": "1", "name": "Специалист"}],
                "industries": [{"id": "7.540", "name": "Оборонная промышленность"}],
            })
            database.record_snapshot(run_id, "1", snapshot)
            with database.connect() as connection:
                work_format = connection.execute(
                    "SELECT work_format_id, work_format_name FROM snapshot_work_formats"
                ).fetchone()
                skill = connection.execute("SELECT skill_name FROM snapshot_key_skills").fetchone()[0]
                role = connection.execute("SELECT role_id, role_name FROM snapshot_roles").fetchone()
                industry = connection.execute(
                    "SELECT industry_id, industry_name FROM snapshot_industries"
                ).fetchone()
        self.assertEqual(tuple(work_format), ("REMOTE", "Удалённо"))
        self.assertEqual(skill, "Воинский учет")
        self.assertEqual(tuple(role), ("1", "Специалист"))
        self.assertEqual(tuple(industry), ("7.540", "Оборонная промышленность"))

    def test_html_normalization_keeps_compressed_redacted_html(self):
        snapshot = normalize_html_vacancy(
            {"id": "1", "name": "Специалист", "description": "<p>mail@example.com</p>"},
            "<html><body><p>mail@example.com</p></body></html>",
        )

        raw_html = gzip.decompress(snapshot["raw_payload"]).decode("utf-8")
        self.assertEqual(snapshot["raw_content_type"], "text/html")
        self.assertNotIn("mail@example.com", raw_html)
        self.assertIn("[redacted-email]", raw_html)
