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
    configure_file_logger,
)
from hh_parser.config import cli_defaults, load_config
from hh_parser.labeling import stratified_sample
from hh_parser.pilot import create_pilot, pilot_report
from hh_parser.query_specs import QuerySpec, load_query_specs
from hh_parser.skill_dictionary import load_skill_dictionary
from hh_parser.relevance import classify_relevance
from hh_parser.sources.api import HHApiSource


class ConfigTests(unittest.TestCase):
    def test_file_logger_writes_next_to_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "research.sqlite3"
            logger, log_path = configure_file_logger(database_path)
            try:
                logger.info("test event")
                database = Database(database_path)
                database.migrate()
                run_id = database.start_run({"fixture": "file-log"})
                database.upsert_vacancy("123", source="api")
                database.record_error(run_id, "vacancy", "Timeout", "temporary", vacancy_hh_id="123")
                for handler in logger.handlers:
                    handler.flush()
                log_contents = log_path.read_text(encoding="utf-8")
                self.assertEqual(log_path.parent, Path(temp_dir) / "logs")
                self.assertIn("INFO test event", log_contents)
                self.assertIn("WARNING collection_error", log_contents)
            finally:
                for handler in logger.handlers[:]:
                    logger.removeHandler(handler)
                    handler.close()

    def test_labeling_sample_is_deterministic_and_stratified(self):
        rows = [
            {"snapshot_id": 1, "query_families": "military", "_area_id": 1, "_period": "2026-01-01", "auto_label": "relevant"},
            {"snapshot_id": 2, "query_families": "military", "_area_id": 1, "_period": "2026-01-01", "auto_label": "relevant"},
            {"snapshot_id": 3, "query_families": "civil", "_area_id": 2, "_period": "2026-02-01", "auto_label": "borderline"},
            {"snapshot_id": 4, "query_families": "civil", "_area_id": 2, "_period": "2026-02-01", "auto_label": "borderline"},
        ]
        first = stratified_sample(rows, 2, "pilot")
        second = stratified_sample(rows, 2, "pilot")
        self.assertEqual(first, second)
        self.assertEqual({row["auto_label"] for row in first}, {"relevant", "borderline"})

    def test_labeling_cli_exports_and_imports_manual_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "research.sqlite3")
            database.migrate()
            run_id = Collector(database).start({"fixture": "labeling"}, ["1"])
            database.upsert_vacancy("123", source="api")
            snapshot = normalize_api_vacancy({
                "id": "123", "name": "Специалист", "description": "Воинский учет сотрудников",
            })
            database.record_snapshot(run_id, "123", snapshot)
            database.upsert_auto_relevance(
                database.snapshot_id("123", snapshot["content_hash"]), "relevant", 1.0,
                ["signal:воинск"], "test",
            )
            export_path = Path(temp_dir) / "labels.csv"
            parser = build_research_parser()
            self.assertEqual(
                run_export_labeling(parser.parse_args([
                    "export", "--database", str(database.path), "labeling", "--output", str(export_path),
                ])),
                1,
            )
            with export_path.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertIn("query_families", rows[0])
            rows[0]["manual_label"] = "relevant"
            rows[0]["manual_reason"] = "reviewed"
            with export_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
            self.assertEqual(
                run_import_labeling(parser.parse_args([
                    "import", "--database", str(database.path), "labeling", str(export_path),
                ])),
                1,
            )
            with database.connect() as connection:
                label = connection.execute("SELECT manual_label, manual_reason FROM relevance_labels").fetchone()
        self.assertEqual(tuple(label), ("relevant", "reviewed"))

    def test_pilot_persists_selection_and_reports_query_family_metrics(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "research.sqlite3")
            database.migrate()
            run_id = Collector(database).start({"fixture": "pilot"}, ["1"])
            strong = database.upsert_query("мобилизац*", version="pilot-v1", query_group="strong_markers")
            exact = database.upsert_query('"воинский учет"', version="pilot-v1", query_group="stable_phrases")
            ambiguous = database.upsert_query('"первый отдел"', version="pilot-v1", query_group="controlled")
            for hh_id, label, queries in (("1", "relevant", [strong]), ("2", "relevant", [exact]), ("3", "irrelevant", [ambiguous]), ("4", "relevant", [strong, ambiguous])):
                database.upsert_vacancy(hh_id, source="api")
                snapshot = normalize_api_vacancy({"id": hh_id, "name": f"Вакансия {hh_id}"})
                database.record_snapshot(run_id, hh_id, snapshot)
                snapshot_id = database.snapshot_id(hh_id, snapshot["content_hash"])
                database.upsert_auto_relevance(snapshot_id, "borderline", 0.0, [], "test")
                database.set_manual_relevance(snapshot_id, label, "fixture")
                for query_id in queries:
                    database.record_query_hit(run_id, query_id, hh_id, area_id=1)
            count, _ = create_pilot(database, "pilot-v1", sample_size=100, sample_seed="seed", filters={"run_ids": [run_id], "area_ids": [], "date_from": None, "date_to": None, "snapshot_scope": "all"})
            report = pilot_report(database, "pilot-v1", min_per_stratum=5)
        self.assertEqual(count, 4)
        self.assertEqual(report["union"]["precision"], 3 / 4)
        self.assertEqual(report["union"]["recall_in_pilot"], 1.0)
        self.assertEqual({row["query_family"] for row in report["query_families"]}, {"strong_markers", "stable_phrases", "controlled"})
        self.assertTrue(report["selection_version"])
        self.assertTrue(report["label_set_version"])
        self.assertEqual(report["relevance_extractor"]["versions"], ["test"])
        self.assertEqual(report["relevance_extractor"]["disagreements"], 4)

    def test_toml_config_maps_to_cli_defaults_and_rejects_unknown_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.toml"
            path.write_text(
                "[database]\npath = 'from-toml.sqlite3'\n"
                "[collection]\nmode = 'full'\nmax_pages = 3\n"
                "[search]\nqueries_file = 'queries.toml.txt'\n",
                encoding="utf-8",
            )
            config = load_config(path)
            defaults = cli_defaults(config)
            settings = build_research_parser()
            apply_defaults(settings, defaults)
            parsed = settings.parse_args(["collect", "--area", "1"])
        self.assertEqual(parsed.database, "from-toml.sqlite3")
        self.assertEqual((parsed.collection_mode, parsed.max_pages), ("full", 3))
        self.assertEqual(parsed.queries_file, "queries.toml.txt")

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bad.toml"
            path.write_text("[collection]\nunknown = 1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown config key"):
                load_config(path)

    def test_database_toml_defaults_allow_cli_override_and_validate_types(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.toml"
            path.write_text(
                "[database]\npath = 'from-toml.sqlite3'\nwal = false\nbusy_timeout_ms = 9000\n",
                encoding="utf-8",
            )
            parser = build_research_parser()
            apply_defaults(parser, cli_defaults(load_config(path)))
            parsed = parser.parse_args(["db", "--no-wal", "--busy-timeout-ms", "1", "check"])
        self.assertEqual(parsed.database, "from-toml.sqlite3")
        self.assertFalse(parsed.wal)
        self.assertEqual(parsed.busy_timeout_ms, 1)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bad.toml"
            path.write_text("[database]\nwal = 'no'\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, r"\[database\]\.wal must be a boolean"):
                cli_defaults(load_config(path))

    def test_versioned_query_specs_keep_hh_expression_unquoted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "queries.toml"
            path.write_text(
                "version = 'v2'\n[[query]]\nid = 'broad'\ngroup = 'markers'\n"
                "expression = 'мобилизац*'\nsearch_fields = ['name', 'description']\n",
                encoding="utf-8",
            )
            specs = load_query_specs(path)
        self.assertEqual(specs[0].expression, "мобилизац*")
        self.assertEqual(specs[0].version, "v2")

    def test_query_specs_reject_unsupported_or_repeated_search_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "queries.toml"
            path.write_text(
                "[[query]]\nid = 'bad'\nexpression = 'x'\nsearch_fields = ['name', 'name']\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicates"):
                load_query_specs(path)
            path.write_text(
                "[[query]]\nid = 'bad'\nexpression = 'x'\nsearch_fields = ['company_name']\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unsupported"):
                load_query_specs(path)

    def test_relevance_is_explainable_and_keeps_uncertain_candidates(self):
        self.assertEqual(classify_relevance("Специалист", "Воинский учет сотрудников")[0], "relevant")
        label, score, reasons = classify_relevance(
            "Python-разработчик", "Аккредитованная IT-компания. Предоставляем отсрочку от мобилизации.",
        )
        self.assertEqual(label, "irrelevant")
        self.assertEqual(score, 0.0)
        self.assertTrue(reasons[0].startswith("exclude:benefit:"))
        self.assertEqual(
            classify_relevance("Специалист по воинскому учету", "Есть отсрочка от мобилизации")[0],
            "relevant",
        )
        label, score, reasons = classify_relevance("Специалист", "Документооборот")
        self.assertEqual((label, score, reasons), ("borderline", 0.0, []))


class AreaTests(unittest.TestCase):
    def setUp(self):
        self.catalog = flatten_area_tree([
            {"id": "113", "name": "Россия", "areas": [
                {"id": "1", "name": "Москва", "parent_id": "113", "areas": []},
                {"id": "13", "name": "ЦФО", "parent_id": "113", "areas": [
                    {"id": "14", "name": "Тверь", "parent_id": "13", "areas": []},
                ]},
            ]},
        ])
