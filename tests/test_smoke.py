import csv
import gzip
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import parse_skills
import start
from hh_parser.storage import Database
from hh_parser.normalization import normalize_api_vacancy, normalize_html_vacancy
from hh_parser.areas import (
    AreaSelectionError, find_overlaps, flatten_area_tree, parse_area_lines,
    select_catalog_areas, validate_area_ids,
)
from hh_parser.collector import Collector
from hh_parser.cli import build_parser as build_research_parser, run_collect, run_resume
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

    def test_orchestrator_invokes_parser_run_command(self):
        with mock.patch.object(start.subprocess, "run") as run_mock:
            start.run_parser_for_area(1, "Москва", "missing-test-output.csv")

        command = run_mock.call_args.args[0]
        self.assertEqual(command[:3], [sys.executable, "parse_skills.py", "run"])

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


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "research.sqlite3")
        self.database.migrate()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_migrations_are_recorded_and_idempotent(self):
        self.database.migrate()

        with self.database.connect() as connection:
            migrations = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }

        self.assertEqual(
            [row["version"] for row in migrations],
            ["0001_initial.sql", "0002_area_catalog.sql", "0003_resume_state.sql", "0004_snapshot_metadata.sql", "0005_snapshot_links.sql"],
        )
        self.assertTrue(
            {
                "collection_runs", "search_queries", "search_pages",
                "vacancies", "vacancy_query_hits", "vacancy_snapshots", "collection_errors",
                "snapshot_key_skills", "snapshot_roles", "snapshot_industries",
            }.issubset(tables)
        )

    def test_fixture_collection_is_idempotent(self):
        run_id = self.database.start_run({"area": 1, "source": "api"}, source_policy="auto")
        query_id = self.database.upsert_query("воинский учет", query_group="military")
        self.database.upsert_vacancy("123", source="api", alternate_url="https://hh.ru/vacancy/123")
        self.database.record_search_page(
            run_id, query_id, page=0, area_id=1, request_params={"text": "воинский учет"},
            http_status=200, result_count=1, is_last_page=True,
        )
        self.database.record_query_hit(run_id, query_id, "123", area_id=1, page=0, rank=0)
        snapshot = {"content_hash": "fixture-hash", "title": "Специалист по воинскому учету", "source": "api"}
        self.assertTrue(self.database.record_snapshot(run_id, "123", snapshot))

        self.database.upsert_vacancy("123", source="api")
        self.database.record_search_page(run_id, query_id, page=0, area_id=1, http_status=200, result_count=1)
        self.database.record_query_hit(run_id, query_id, "123", area_id=1, page=0, rank=0)
        self.assertFalse(self.database.record_snapshot(run_id, "123", snapshot))

        with self.database.connect() as connection:
            hit_count = connection.execute("SELECT COUNT(*) FROM vacancy_query_hits").fetchone()[0]
            snapshot_count = connection.execute("SELECT COUNT(*) FROM vacancy_snapshots").fetchone()[0]
            page_count = connection.execute("SELECT COUNT(*) FROM search_pages").fetchone()[0]

        self.assertEqual((hit_count, snapshot_count, page_count), (1, 1, 1))

    def test_transaction_rolls_back_all_writes(self):
        with self.assertRaises(RuntimeError):
            with self.database.transaction() as connection:
                self.database.upsert_vacancy("rollback", source="api", connection=connection)
                raise RuntimeError("stop")

        with self.database.connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM vacancies WHERE hh_id = 'rollback'").fetchone()[0]

        self.assertEqual(count, 0)

    def test_area_catalog_round_trip_uses_latest_version(self):
        tree = [{"id": "113", "name": "Россия", "areas": [
            {"id": "1", "name": "Москва", "parent_id": "113", "areas": []},
        ]}]
        catalog_id = self.database.store_area_catalog(tree, source_url="https://api.hh.ru/areas")
        loaded_id, catalog = self.database.load_area_catalog()

        self.assertEqual(loaded_id, catalog_id)
        self.assertEqual(select_catalog_areas(catalog, "113", "leaf"), ["1"])

    def test_collector_records_hits_before_global_card_deduplication(self):
        collector = Collector(self.database)
        run_id = collector.start({"fixture": True}, ["1"])
        detail_calls = []

        def search(query, area_id):
            return [{"id": "123", "name": "Специалист", "alternate_url": "https://hh.ru/vacancy/123"}]

        def detail(candidate):
            detail_calls.append(candidate["id"])
            return {"id": candidate["id"], "name": "Специалист", "description": "<p>Текст</p>"}

        counters = collector.collect(run_id, ["1"], ["воинский учет", "ГО и ЧС"], search=search, detail=detail)
        with self.database.connect() as connection:
            hit_count = connection.execute("SELECT COUNT(*) FROM vacancy_query_hits").fetchone()[0]
            snapshot_count = connection.execute("SELECT COUNT(*) FROM vacancy_snapshots").fetchone()[0]
            status = connection.execute("SELECT status FROM collection_runs WHERE id = ?", (run_id,)).fetchone()[0]

        self.assertEqual(counters, {"found": 2, "unique": 1, "loaded": 1, "errors": 0})
        self.assertEqual(detail_calls, ["123"])
        self.assertEqual((hit_count, snapshot_count, status), (2, 1, "completed"))

    def test_collector_resumes_after_card_interruption_without_repeating_search_or_loaded_card(self):
        collector = Collector(self.database)
        run_id = collector.start({"fixture": True}, ["1", "2"])
        search_calls = []
        detail_calls = []

        def search(query, area_id):
            search_calls.append(area_id)
            return [{"id": area_id, "name": f"Vacancy {area_id}", "_source": "api"}]

        def interrupted_detail(candidate):
            detail_calls.append(candidate["id"])
            if candidate["id"] == "2":
                raise KeyboardInterrupt()
            return {"id": candidate["id"], "name": f"Vacancy {candidate['id']}"}

        with self.assertRaises(KeyboardInterrupt):
            collector.collect(
                run_id, ["1", "2"], ["воинский учет"],
                search=search, detail=interrupted_detail,
            )

        resumed_detail_calls = []

        def resumed_detail(candidate):
            resumed_detail_calls.append(candidate["id"])
            return {"id": candidate["id"], "name": f"Vacancy {candidate['id']}"}

        counters = Collector(self.database).resume(
            run_id, ["воинский учет"], search=search, detail=resumed_detail,
        )

        self.assertEqual(search_calls, ["1", "2"])
        self.assertEqual(detail_calls, ["1", "2"])
        self.assertEqual(resumed_detail_calls, ["2"])
        self.assertEqual(counters, {"found": 2, "unique": 2, "loaded": 2, "errors": 0})

    def test_retry_resolves_card_error_and_observes_unchanged_snapshot(self):
        first_run = self.database.start_run({"fixture": 1}, source_policy="api")
        self.database.upsert_vacancy("123", source="api")
        snapshot = normalize_api_vacancy(
            {"id": "123", "name": "Специалист"},
            observed_at="2026-01-01T00:00:00+00:00",
        )
        self.assertTrue(self.database.record_snapshot(first_run, "123", snapshot))

        collector = Collector(self.database)
        run_id = collector.start({"fixture": 2}, ["1"])
        attempts = 0

        def search(query, area_id):
            return [{"id": "123", "name": "Специалист", "_source": "api"}]

        def flaky_detail(candidate):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary")
            return {"id": "123", "name": "Специалист"}

        first = collector.collect(
            run_id, ["1"], ["воинский учет"], search=search, detail=flaky_detail
        )
        second = Collector(self.database).resume(
            run_id, ["воинский учет"], search=search, detail=flaky_detail
        )

        self.assertEqual(first["errors"], 1)
        self.assertEqual(second, {"found": 1, "unique": 1, "loaded": 1, "errors": 0})
        with self.database.connect() as connection:
            snapshot_count = connection.execute(
                "SELECT COUNT(*) FROM vacancy_snapshots WHERE vacancy_hh_id = '123'"
            ).fetchone()[0]
            resolved = connection.execute(
                "SELECT resolved_at FROM collection_errors WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
        self.assertEqual(snapshot_count, 1)
        self.assertIsNotNone(resolved)

    def test_collect_and_resume_cli_use_frozen_catalog_scope(self):
        self.database.store_area_catalog(
            [{"id": "113", "name": "Россия", "areas": [
                {"id": "1", "name": "Москва", "parent_id": "113", "areas": []},
            ]}],
            source_url="https://api.hh.ru/areas",
        )
        queries_path = Path(self.temp_dir.name) / "queries.txt"
        queries_path.write_text("воинский учет\n", encoding="utf-8")
        parser = build_research_parser()
        settings = parser.parse_args([
            "collect", "--database", str(self.database.path), "--queries-file", str(queries_path),
            "--area", "1",
        ])

        class Source:
            def __init__(self):
                self.search_calls = 0
                self.detail_calls = 0

            def search_page(self, query, area_id, *, page, date_from=None, date_to=None):
                self.search_calls += 1
                if page != 0:
                    raise AssertionError("unexpected extra page")
                return [{"id": "123", "name": "Специалист", "_source": "api"}], True

            def detail(self, candidate):
                self.detail_calls += 1
                if self.detail_calls == 1:
                    raise RuntimeError("temporary")
                return {"id": candidate["id"], "name": "Специалист"}

        source = Source()
        run_id, counters = run_collect(settings, source_factory=lambda _: source)
        self.assertEqual(counters, {"found": 1, "unique": 1, "loaded": 0, "errors": 1})

        resumed = run_resume(
            parser.parse_args([
                "resume", "--database", str(self.database.path), "--queries-file", str(queries_path),
                "--run-id", str(run_id),
            ]),
            source_factory=lambda _: source,
        )
        self.assertEqual(resumed, {"found": 1, "unique": 1, "loaded": 1, "errors": 0})
        self.assertEqual((source.search_calls, source.detail_calls), (1, 2))

    def test_paginated_collector_records_each_page_and_flags_depth_saturation(self):
        collector = Collector(self.database)
        run_id = collector.start({"fixture": True}, ["1"])
        requested_pages = []

        def search_page(query, area_id, *, page, date_from=None, date_to=None):
            requested_pages.append(page)
            return ([{"id": str(page), "name": "Специалист", "_source": "api"}], page == 1)

        counters = collector.collect_paginated(
            run_id, ["1"], ["воинский учет"], search_page=search_page,
            detail=lambda candidate: {"id": candidate["id"], "name": "Специалист"},
        )
        self.assertEqual(requested_pages, [0, 1])
        self.assertEqual(counters, {"found": 2, "unique": 2, "loaded": 2, "errors": 0})

        saturated_run = Collector(self.database).start({"fixture": "saturated"}, ["1"])
        saturated = Collector(self.database).collect_paginated(
            saturated_run, ["1"], ["воинский учет"],
            search_page=lambda query, area_id, *, page, **_: ([], False),
            detail=lambda candidate: candidate, max_pages=1,
        )
        self.assertEqual(saturated["errors"], 1)
        with self.database.connect() as connection:
            error_type = connection.execute(
                "SELECT error_type FROM collection_errors WHERE run_id = ?", (saturated_run,)
            ).fetchone()[0]
        self.assertEqual(error_type, "SearchDepthSaturated")

    def test_date_window_is_persisted_and_reused_by_resume(self):
        collector = Collector(self.database)
        run_id = collector.start({"fixture": "dates"}, ["1"])
        calls = []

        def search_page(query, area_id, *, page, date_from=None, date_to=None):
            calls.append((page, date_from, date_to))
            return ([{"id": "123", "name": "Специалист", "_source": "api"}], True)

        collector.collect_paginated(
            run_id, ["1"], ["воинский учет"], search_page=search_page,
            detail=lambda candidate: {"id": candidate["id"], "name": "Специалист"},
            date_from="2026-01-01", date_to="2026-01-31",
        )
        with self.database.connect() as connection:
            page = connection.execute(
                "SELECT date_from, date_to FROM search_pages WHERE run_id = ?", (run_id,)
            ).fetchone()
        self.assertEqual(calls, [(0, "2026-01-01", "2026-01-31")])
        self.assertEqual(tuple(page), ("2026-01-01", "2026-01-31"))


class NormalizationTests(unittest.TestCase):
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
        self.assertTrue(snapshot["redaction_applied"])

    def test_snapshot_metadata_creates_normalized_multivalue_links(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "research.sqlite3")
            database.migrate()
            run_id = database.start_run({"fixture": True})
            database.upsert_vacancy("1", source="api")
            snapshot = normalize_api_vacancy({
                "id": "1", "name": "Специалист",
                "key_skills": [{"name": "Воинский учет"}],
                "professional_roles": [{"id": "1", "name": "Специалист"}],
                "industries": [{"id": "7.540", "name": "Оборонная промышленность"}],
            })
            database.record_snapshot(run_id, "1", snapshot)
            with database.connect() as connection:
                skill = connection.execute("SELECT skill_name FROM snapshot_key_skills").fetchone()[0]
                role = connection.execute("SELECT role_id, role_name FROM snapshot_roles").fetchone()
                industry = connection.execute(
                    "SELECT industry_id, industry_name FROM snapshot_industries"
                ).fetchone()
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
        )
        self.assertEqual(items[0]["_source"], "api")
        self.assertTrue(is_last)
        self.assertEqual(session.headers["HH-User-Agent"], "test-app/1.0 (dev@example.com)")
        self.assertNotIn("User-Agent", session.headers)
        self.assertNotIn("Authorization", session.headers)
        self.assertEqual(session.calls[0][1]["params"]["date_to"], "2026-01-31")


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


if __name__ == "__main__":
    unittest.main()
