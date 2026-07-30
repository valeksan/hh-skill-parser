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


class SmokeTests(unittest.TestCase):
    def test_help_command_succeeds(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "help"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--source", result.stdout)
        self.assertIn("--no-chart", result.stdout)

    def test_no_arguments_shows_help_without_starting_collection(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("chart", result.stdout)
        self.assertIn("run", result.stdout)

    def test_commands_require_explicit_command_name(self):
        self.assertEqual(parse_skills.parse_command_arguments([]), ("help", []))
        self.assertEqual(
            parse_skills.parse_command_arguments(["run", "--no-chart"]),
            ("run", ["--no-chart"]),
        )
        self.assertEqual(
            parse_skills.parse_command_arguments(["chart", "--chart-input", "saved.csv"]),
            ("chart", ["--chart-input", "saved.csv"]),
        )
        with self.assertRaises(SystemExit):
            parse_skills.parse_command_arguments(["--help"])

    def test_parse_html_vacancy_page_extracts_title_description_and_skills(self):
        html_text = """
        <html>
          <body>
            <h1 data-qa="vacancy-title">ML Engineer</h1>
            <div data-qa="vacancy-description">
              <p>Python, SQL and Airflow in production.</p>
            </div>
            <script>
              window.__data = {
                "keySkills":[{"name":"Python"},{"name":"Airflow"}],"driverLicenseTypes":[]
              };
            </script>
          </body>
        </html>
        """
        vacancy = {"id": "123456", "name": "fallback title"}

        parsed = parse_skills.parse_html_vacancy_page(html_text, vacancy)

        self.assertEqual(parsed["id"], "123456")
        self.assertEqual(parsed["name"], "ML Engineer")
        self.assertIn("Python, SQL and Airflow", parsed["description"])
        self.assertEqual(
            parsed["key_skills"],
            [{"name": "Python"}, {"name": "Airflow"}],
        )

    def test_resolve_processing_mode_switches_html_to_description_when_enabled(self):
        settings = parse_skills.cli_parse(
            ["--mode", "key-skills", "--html-description-fallback", "--no-chart"]
        )

        effective_mode = parse_skills.resolve_processing_mode(
            settings,
            {"_source": "html"},
        )

        self.assertEqual(effective_mode, "description")

    def test_load_dotenv_file_respects_existing_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            dotenv_path = Path(temp_dir) / ".env"
            dotenv_path.write_text(
                "HH_NO_CHART=1\nCUSTOM_VALUE=from_file\n",
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {"CUSTOM_VALUE": "from_env"}, clear=False):
                loaded = parse_skills.load_dotenv_file(str(dotenv_path), override=False)

                self.assertTrue(loaded)
                self.assertEqual(os.environ["CUSTOM_VALUE"], "from_env")
                self.assertEqual(os.environ["HH_NO_CHART"], "1")

    def test_save_result_csv_writes_header_and_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "skills.csv"

            parse_skills.save_result_csv({"python": 3, "sql": 2}, file_path=output_path)

            with output_path.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.reader(handle))

        self.assertEqual(rows[0], ["Count", "Skill"])
        self.assertEqual(rows[1], ["3", "python"])
        self.assertEqual(rows[2], ["2", "sql"])

    def test_generate_chart_loads_existing_csv_without_collection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "skills.csv"
            input_path.write_text(
                "Count,Skill\n3,python\n2,sql\n",
                encoding="utf-8",
            )

            with mock.patch.object(parse_skills, "pyplot", object()), mock.patch.object(
                parse_skills, "save_result_chart"
            ) as chart_mock:
                parse_skills.generate_chart_from_csv(
                    str(input_path),
                    "chart.png",
                    10,
                )

        chart_mock.assert_called_once_with(
            {"python": 3, "sql": 2},
            skills_show_count=10,
            file_path="chart.png",
        )

    def test_cli_accepts_chart_input_argument(self):
        settings = parse_skills.cli_parse(["--chart-input", "saved.csv"])

        self.assertEqual(settings.chart_input, "saved.csv")

    def test_load_queries_creates_default_file_when_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            queries_path = Path(temp_dir) / "queries.txt"

            queries = parse_skills.load_queries(str(queries_path))

            self.assertTrue(queries_path.exists())
            self.assertGreater(len(queries), 0)
            self.assertIn("ai wizard intern", queries)

    def test_queries_are_normalized_and_quoted_once_for_hh(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            queries_path = Path(temp_dir) / "queries.txt"
            queries_path.write_text(
                '"Специалист по военному учету"\n'
                "Начальник мобилизационного отдела\n",
                encoding="utf-8",
            )

            queries = parse_skills.load_queries(str(queries_path))

        self.assertEqual(
            queries,
            [
                "Специалист по военному учету",
                "Начальник мобилизационного отдела",
            ],
        )
        self.assertEqual(
            parse_skills.build_exact_search_query(queries[0]),
            '"Специалист по военному учету"',
        )
        self.assertEqual(
            parse_skills.build_exact_search_query('"Специалист по военному учету"'),
            '"Специалист по военному учету"',
        )

    def test_load_skills_whitelist_creates_default_file_when_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            whitelist_path = Path(temp_dir) / "skills_whitelist.txt"

            skills = parse_skills.load_skills_whitelist(str(whitelist_path))

            self.assertTrue(whitelist_path.exists())
            self.assertIn("python", skills)
            self.assertIn("терпение к легаси", skills)

    def test_skills_whitelist_aliases_return_one_canonical_skill(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            whitelist_path = Path(temp_dir) / "skills_whitelist.txt"
            whitelist_path.write_text(
                "воинский учет | военный учет | ведение воинского учета\n",
                encoding="utf-8",
            )

            skills = parse_skills.load_skills_whitelist(str(whitelist_path))
            extracted = parse_skills.extract_skills(
                "Ведение воинского учета и военный учет сотрудников.",
                skills,
            )

        self.assertEqual(skills["военный учет"], "воинский учет")
        self.assertEqual(extracted, ["воинский учет"])

    def test_mobilization_title_filter_covers_added_query_categories(self):
        accepted_titles = [
            "Специалист по воинскому учету",
            "Специалист по ГО и ЧС",
            "Главный специалист режимно-секретного подразделения",
            "Специалист по защите государственной тайны",
        ]

        for title in accepted_titles:
            self.assertTrue(
                parse_skills.is_valid_mobilization_vacancy({"name": title}),
                title,
            )

        self.assertFalse(
            parse_skills.is_valid_mobilization_vacancy({"name": "Специалист по продажам"})
        )

    def test_auto_source_switches_to_html_after_first_ddos_block(self):
        with mock.patch.object(parse_skills, "get_vacancies_from_api") as api_mock, mock.patch.object(
            parse_skills, "get_vacancies_from_html", return_value=[{"id": "1", "name": "x"}]
        ) as html_mock:
            parse_skills.AUTO_SOURCE_FORCE_HTML = False
            api_mock.side_effect = parse_skills.SourceBlockedError("blocked")

            first = parse_skills.get_vacancies("data scientist", area=1, source="auto")
            second = parse_skills.get_vacancies("ml engineer", area=1, source="auto")

            self.assertEqual(first, [{"id": "1", "name": "x"}])
            self.assertEqual(second, [{"id": "1", "name": "x"}])
            self.assertEqual(api_mock.call_count, 1)
            self.assertEqual(html_mock.call_count, 2)

    def test_both_mode_merges_and_deduplicates_skills_per_vacancy(self):
        data = {
            "key_skills": [{"name": "Python"}, {"name": "SQL"}],
            "description": "<p>python and sql and airflow</p>",
        }

        with mock.patch.object(parse_skills, "load_skills_whitelist", return_value={"python", "sql", "airflow"}):
            skills = parse_skills.get_skills_from_both_sources(data)

        self.assertEqual(skills, ["python", "sql", "airflow"])

    def test_db_init_command_creates_sqlite_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "research.sqlite3"
            result = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "db", "init", "--database", str(database_path)],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(database_path.exists())
            self.assertIn("SQLite schema ready", result.stdout)
