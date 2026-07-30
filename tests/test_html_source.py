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


class HtmlSourceTests(unittest.TestCase):
    def test_html_jobposting_and_selectors_normalize_to_snapshot_contract(self):
        html = """
        <html><head><script type="application/ld+json">{
          "@context":"https://schema.org", "@type":"JobPosting", "title":"Аналитик",
          "datePosted":"2026-07-01T10:00:00+03:00", "validThrough":"2026-08-01T00:00:00+03:00",
          "hiringOrganization":{"name":"АО Пример"},
          "jobLocation":{"address":{"addressLocality":"Москва"}},
          "baseSalary":{"currency":"RUB","value":{"minValue":100000,"maxValue":150000}}
        }</script></head><body>
          <h1 data-qa="vacancy-title">Аналитик данных</h1>
          <div data-qa="vacancy-description"><p>Воинский учет: mail@example.com</p></div>
        </body></html>"""
        data = HHHtmlSource.parse_vacancy_page(html, "42")
        snapshot = normalize_html_vacancy(data, html)
        self.assertEqual(snapshot["title"], "Аналитик данных")
        self.assertEqual(snapshot["employer_name"], "АО Пример")
        self.assertEqual(snapshot["area_name"], "Москва")
        self.assertEqual(snapshot["salary_from"], 100000)
        self.assertEqual(snapshot["published_at"], "2026-07-01T07:00:00+00:00")
        self.assertTrue(snapshot["completeness"]["fields"]["employer"]["present"])
        self.assertIn("[redacted-email]", snapshot["description_text"])

    def test_html_anonymous_archived_missing_fields_and_anti_bot_are_honest(self):
        archived = "<html><body><div>Вакансия в архиве</div><h1 data-qa='vacancy-title'>Сторож</h1></body></html>"
        data = HHHtmlSource.parse_vacancy_page(archived, "43")
        snapshot = normalize_html_vacancy(data, archived)
        self.assertTrue(snapshot["archived"])
        self.assertIsNone(snapshot["employer_name"])
        self.assertEqual(snapshot["completeness"]["fields"]["description"]["missing_reason"], "empty_or_not_provided_by_source")

        class Response:
            status_code = 200
            text = "<html><body>Подтвердите, что вы не робот</body></html>"
            def raise_for_status(self): pass
        class Session:
            def __init__(self): self.headers = {}
            def get(self, *_args, **_kwargs): return Response()
        with self.assertRaises(HHHtmlAntiBotError):
            HHHtmlSource(user_agent="test/1", session=Session()).detail({"id": "43"})

    def test_html_search_extracts_only_vacancy_ids(self):
        items = HHHtmlSource.parse_search_page("""
          <a data-qa='serp-item__title' href='/vacancy/123'>One</a>
          <a href='/vacancy/123?x=1'>Duplicate</a><a href='/employer/4'>No</a>""")
        self.assertEqual(items, [{"id": "123", "name": "One", "alternate_url": "/vacancy/123", "_source": "html"}])
