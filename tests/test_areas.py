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
from relevance import classify_relevance
from hh_parser.sources.api import HHApiSource


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

    def test_area_file_ignores_comments_and_rejects_duplicate(self):
        self.assertEqual(parse_area_lines(["# Russia\n", "1 # Moscow\n", "13\n"]), ["1", "13"])
        with self.assertRaisesRegex(AreaSelectionError, "duplicate"):
            parse_area_lines(["1\n", "1\n"])

    def test_catalog_selection_and_overlap_detection(self):
        self.assertEqual(select_catalog_areas(self.catalog, "113", "leaf"), ["1", "14"])
        self.assertEqual(find_overlaps(["13", "14"], self.catalog), [("13", "14")])
        with self.assertRaisesRegex(AreaSelectionError, "unknown"):
            validate_area_ids(["999"], self.catalog)

    def test_areas_cli_sync_list_and_validate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "areas.sqlite3"
            parser = build_research_parser()

            class Source:
                def areas(self):
                    return [{"id": "113", "name": "Россия", "areas": [
                        {"id": "1", "name": "Москва", "areas": []},
                    ]}]

            synced = run_areas_sync(
                parser.parse_args(["areas", "--database", str(database_path), "sync"]),
                source_factory=lambda _: Source(),
            )
            listed = run_areas_list(parser.parse_args([
                "areas", "--database", str(database_path), "list", "--catalog-version", str(synced),
                "--level", "leaf",
            ]))
            validated = run_areas_validate(parser.parse_args([
                "areas", "--database", str(database_path), "validate", "--area", "1",
            ]))
        self.assertEqual(listed, [{"catalog_version": str(synced), "id": "1", "name": "Москва"}])
        self.assertEqual(validated, ["1"])
