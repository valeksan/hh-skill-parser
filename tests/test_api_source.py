import csv
import gzip
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import date
import unittest
from pathlib import Path
from unittest import mock

import requests

import parse_skills
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


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "parse_skills.py"


class ApiSourceTests(unittest.TestCase):
    def test_api_source_uses_one_hh_identity_header_and_date_window_params(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"items": [{"id": "1", "name": "Специалист"}], "pages": 1}

        class Session:
            def __init__(self):
                self.headers = {}
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response()

        session = Session()
        source = HHApiSource(user_agent="test-app/1.0 (dev@example.com)", session=session)
        items, is_last = source.search_page(
            "воинский учет", "1", page=0, date_from="2026-01-01", date_to="2026-01-31",
            search_fields=("name", "description"),
        )
        self.assertEqual(items[0]["_source"], "api")
        self.assertTrue(is_last)
        self.assertEqual(session.headers["HH-User-Agent"], "test-app/1.0 (dev@example.com)")
        self.assertNotIn("User-Agent", session.headers)
        self.assertNotIn("Authorization", session.headers)
        self.assertEqual(session.calls[0][1]["params"]["date_to"], "2026-01-31")
        self.assertEqual(session.calls[0][1]["params"]["search_field"], ["name", "description"])
        self.assertEqual(session.calls[0][1]["params"]["host"], "hh.ru")
        self.assertEqual(session.calls[0][1]["params"]["locale"], "RU")

    def test_api_source_passes_configured_host_and_locale_to_catalog_and_card(self):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return [] if len(session.calls) == 1 else {"id": "1", "name": "Специалист"}

        class Session:
            def __init__(self):
                self.headers = {}
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response()

        session = Session()
        source = HHApiSource(user_agent="test-app/1.0", host="hh.kz", locale="EN", session=session)
        self.assertEqual(source.areas(), [])
        source.detail({"id": "1"})
        self.assertEqual(session.calls[0][1]["params"], {"host": "hh.kz", "locale": "EN"})
        self.assertEqual(session.calls[1][1]["params"], {"host": "hh.kz", "locale": "EN"})

    def test_api_source_rejects_unknown_host_or_empty_locale(self):
        with self.assertRaisesRegex(ValueError, "host"):
            HHApiSource(user_agent="test-app/1.0", host="invalid.example")
        with self.assertRaisesRegex(ValueError, "locale"):
            HHApiSource(user_agent="test-app/1.0", locale="")

    def test_rejected_bearer_token_retries_public_api_and_emits_one_safe_event(self):
        class Response:
            def __init__(self, status_code, payload=None):
                self.status_code = status_code
                self.payload = payload or {"items": [], "pages": 0}

            def raise_for_status(self):
                if self.status_code >= 400:
                    error = requests.HTTPError(f"HTTP {self.status_code}")
                    error.response = self
                    raise error

            def json(self):
                return self.payload

        class Session:
            def __init__(self):
                self.headers = {}
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append((url, dict(self.headers), kwargs))
                return Response(401 if len(self.calls) == 1 else 200)

        session = Session()
        source = HHApiSource(user_agent="test-app/1.0", access_token="secret-token", session=session)
        source.search_page("воинский учет", "1", page=0)

        self.assertIn("Authorization", session.calls[0][1])
        self.assertNotIn("Authorization", session.calls[1][1])
        self.assertNotIn("Authorization", session.headers)
        self.assertEqual(source.consume_auth_degradation(), {"http_status": 401})
        self.assertIsNone(source.consume_auth_degradation())

    def test_api_source_retries_429_using_retry_after(self):
        class Response:
            def __init__(self, status_code, headers=None):
                self.status_code = status_code
                self.headers = headers or {}

            def raise_for_status(self):
                if self.status_code >= 400:
                    error = requests.HTTPError(f"HTTP {self.status_code}")
                    error.response = self
                    raise error

            def json(self):
                return {"items": [], "pages": 0}

        class Session:
            def __init__(self):
                self.headers = {}
                self.calls = 0

            def get(self, _url, **_kwargs):
                self.calls += 1
                return Response(429, {"Retry-After": "2"}) if self.calls == 1 else Response(200)

        pauses = []
        session = Session()
        source = HHApiSource(
            user_agent="test-app/1.0", session=session, max_retries=2,
            retry_backoff=0.1, sleep_fn=pauses.append,
        )
        items, is_last = source.search_page("воинский учет", "1", page=0)

        self.assertEqual(items, [])
        self.assertTrue(is_last)
        self.assertEqual(session.calls, 2)
        self.assertEqual(pauses, [2.0])

    def test_collector_marks_run_degraded_after_safe_auth_fallback(self):
        class Transport:
            def consume_auth_degradation(self):
                return {"http_status": 403}

        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "research.sqlite3")
            database.migrate()
            run_id = Collector(database, transport=Transport()).start({"fixture": "auth"}, ["1"])
            counters = Collector(database, transport=Transport()).collect_paginated(
                run_id, ["1"], ["воинский учет"],
                search_page=lambda *_args, **_kwargs: ([], True), detail=lambda candidate: candidate,
            )
            with database.connect() as connection:
                run = connection.execute("SELECT status FROM collection_runs WHERE id = ?", (run_id,)).fetchone()
                error = connection.execute(
                    "SELECT error_type, http_status, message FROM collection_errors WHERE run_id = ?", (run_id,)
                ).fetchone()
        self.assertEqual(run["status"], "degraded")
        self.assertEqual(tuple(error[:2]), ("AuthenticationDegraded", 403))
        self.assertNotIn("secret-token", error["message"])
