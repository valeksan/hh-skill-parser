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
from hh_parser.extractors.features import extract_features, repost_fingerprint
from hh_parser.export import export_query_hits, export_roles, export_skills, export_vacancies
from hh_parser.discovery import discover_skill_candidates, import_skill_candidates
from hh_parser.stats import vacancy_stats
from hh_parser.cli import (
    apply_defaults, build_parser as build_research_parser, run_collect, run_coverage, run_resume, run_retry, resolve_collection_window,
    run_areas_list, run_areas_sync, run_areas_validate, run_export_labeling,
    run_db, run_discover, run_import_labeling, run_import_skill_candidates, run_extract, run_export_skills, run_export_vacancies, run_maintenance, run_stats,
)
from hh_parser.config import cli_defaults, load_config
from hh_parser.labeling import stratified_sample
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
            ["0001_initial.sql", "0002_area_catalog.sql", "0003_resume_state.sql", "0004_snapshot_metadata.sql", "0005_snapshot_links.sql", "0006_error_windows.sql", "0007_relevance_labels.sql", "0008_effective_relevance_view.sql", "0009_features.sql", "0010_skills.sql", "0011_extraction_runs.sql", "0012_work_format_links.sql", "0013_da_views.sql", "0014_vacancy_text_fts.sql", "0015_timestamp_offsets.sql", "0016_collection_watermarks.sql", "0017_collection_coverage.sql", "0018_vacancy_requests.sql"],
        )
        self.assertTrue(
            {
                "collection_runs", "search_queries", "search_pages",
                "vacancies", "vacancy_query_hits", "vacancy_snapshots", "collection_errors",
                "vacancy_requests",
                "snapshot_key_skills", "snapshot_roles", "snapshot_industries", "snapshot_work_formats",
                "features",
                "skills", "skill_aliases", "vacancy_skills",
                "extraction_runs", "extraction_errors",
                "collection_watermarks",
            }.issubset(tables)
        )

    def test_collector_derives_geography_from_frozen_area_catalog(self):
        catalog_id = self.database.store_area_catalog([{
            "id": "113", "name": "Россия", "areas": [{
                "id": "10", "name": "Сибирский федеральный округ", "areas": [{
                    "id": "20", "name": "Новосибирская область", "areas": [{
                        "id": "30", "name": "Новосибирск", "areas": [],
                    }],
                }],
            }],
        }], source_url="fixture")
        collector = Collector(self.database)
        run_id = collector.start(
            {"catalog_version_id": catalog_id}, ["30"], catalog_version_id=catalog_id,
        )
        collector.collect(
            run_id, ["30"], ["воинский учет"],
            search=lambda *_: [{"id": "geo-1", "name": "Специалист", "_source": "api"}],
            detail=lambda _: {"id": "geo-1", "name": "Специалист", "area": {"id": "30", "name": "Новосибирск"}},
        )
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT federal_district, federal_subject, locality FROM vacancy_snapshots"
            ).fetchone()
        self.assertEqual(tuple(row), ("Сибирский федеральный округ", "Новосибирская область", "Новосибирск"))

    def test_geography_does_not_infer_unknown_catalog_paths(self):
        catalog = flatten_area_tree([{"id": "1", "name": "Other", "areas": []}])
        self.assertEqual(
            resolve_russia_geography("1", catalog),
            {"federal_district": None, "federal_subject": None, "locality": None},
        )

    def test_da_views_use_latest_snapshot_and_effective_relevance(self):
        run_id = self.database.start_run({"fixture": "views"})
        self.database.upsert_vacancy("123", source="api")
        first = {
            "content_hash": "views-first", "title": "Старый", "source": "api",
            "observed_at": "2026-07-01T00:00:00+00:00", "published_at": "2026-06-30T12:00:00+00:00",
            "employer_id": "42", "employer_name": "АО Пример", "salary_from": 100000,
        }
        latest = {**first, "content_hash": "views-latest", "title": "Новый", "observed_at": "2026-07-02T00:00:00+00:00", "salary_to": 120000}
        self.database.record_snapshot(run_id, "123", first)
        self.database.record_snapshot(run_id, "123", latest)
        snapshot_id = self.database.snapshot_id("123", "views-latest")
        self.database.upsert_auto_relevance(snapshot_id, "borderline", 0.0, [], "test")
        self.database.set_manual_relevance(snapshot_id, "relevant", "reviewed")
        with self.database.connect() as connection:
            latest_row = connection.execute("SELECT title FROM latest_vacancy_snapshots").fetchone()
            relevant = connection.execute("SELECT title, effective_label FROM relevant_vacancies").fetchone()
            time_series = connection.execute("SELECT publication_day, vacancy_count FROM publication_time_series").fetchone()
            employer = connection.execute("SELECT employer_name, vacancy_count FROM vacancy_employers").fetchone()
            salary = connection.execute("SELECT salary_midpoint FROM vacancy_salary").fetchone()
            matrix_count = connection.execute("SELECT COUNT(*) FROM vacancy_skill_matrix").fetchone()[0]
            geography_count = connection.execute("SELECT COUNT(*) FROM vacancy_geography").fetchone()[0]
        self.assertEqual(latest_row["title"], "Новый")
        self.assertEqual(tuple(relevant), ("Новый", "relevant"))
        self.assertEqual(tuple(time_series), ("2026-06-30", 1))
        self.assertEqual(tuple(employer), ("АО Пример", 1))
        self.assertEqual(salary["salary_midpoint"], 110000)
        self.assertEqual(matrix_count, 0)
        self.assertEqual(geography_count, 1)

    def test_fts_search_reads_stored_text_only(self):
        run_id = self.database.start_run({"fixture": "fts"})
        self.database.upsert_vacancy("fts-1", source="api")
        self.database.record_snapshot(run_id, "fts-1", {
            "content_hash": "fts-hash", "title": "Воинский учет",
            "description_text": "Бронирование сотрудников", "source": "api",
        })
        rows = self.database.search_text("воинский")
        self.assertEqual([(row["vacancy_hh_id"], row["title"]) for row in rows], [("fts-1", "Воинский учет")])
        with self.assertRaises(ValueError):
            self.database.search_text("")

    def test_vacancy_export_is_db_only_and_honors_filters(self):
        run_id = self.database.start_run({"fixture": "vacancy-export"})
        query_id = self.database.upsert_query("воинский учет", query_group="military")
        self.database.upsert_vacancy("export-1", source="api")
        self.database.record_query_hit(run_id, query_id, "export-1", area_id=1)
        self.database.record_snapshot(run_id, "export-1", {
            "content_hash": "export-hash", "title": "Воинский учет", "source": "api",
            "description_text": "Бронирование", "area_id": 1, "area_name": "Москва",
            "employer_id": "private-42", "employer_name": "ИП Иванов Иван Иванович", "employer_type": "individual",
            "published_at": "2026-07-01T12:00:00+00:00", "roles": [{"id": "1", "name": "Специалист"}],
        })
        snapshot_id = self.database.snapshot_id("export-1", "export-hash")
        self.database.upsert_auto_relevance(snapshot_id, "relevant", 1.0, ["signal"], "test")
        output = Path(self.temp_dir.name) / "vacancies.csv"
        rows = export_vacancies(
            self.database, output, area_ids=["1"], relevance="relevant", query_family="military",
            date_from="2026-07-01", date_to="2026-07-01",
        )
        with output.open(encoding="utf-8", newline="") as handle:
            exported = list(csv.DictReader(handle))
        self.assertEqual(rows, 1)
        self.assertEqual(exported[0]["hh_id"], "export-1")
        self.assertEqual(exported[0]["effective_label"], "relevant")
        self.assertEqual(exported[0]["query_families"], "military")
        self.assertEqual(exported[0]["roles"], '[{"id":"1","name":"Специалист"}]')
        self.assertEqual(exported[0]["employer_id"], "private-42")
        self.assertEqual(exported[0]["employer_type"], "individual")
        self.assertTrue(exported[0]["employer_name"].startswith("private-employer-"))
        self.assertNotIn("Иванов", exported[0]["employer_name"])

        settings = build_research_parser().parse_args([
            "export", "--database", str(self.database.path), "vacancies", "--output", str(output),
            "--snapshot", "all", "--run-id", str(run_id),
        ])
        self.assertEqual(run_export_vacancies(settings), 1)

    def test_stats_are_db_only_and_honor_scope_filters(self):
        run_id = self.database.start_run({"fixture": "stats"})
        query_id = self.database.upsert_query("воинский учет", query_group="military")
        self.database.upsert_vacancy("stats-1", source="api")
        self.database.record_query_hit(run_id, query_id, "stats-1", area_id=1)
        self.database.record_snapshot(run_id, "stats-1", {
            "content_hash": "stats-hash", "title": "Воинский учет", "source": "api",
            "area_id": 1, "published_at": "2026-07-02T12:00:00+00:00",
        })
        snapshot_id = self.database.snapshot_id("stats-1", "stats-hash")
        self.database.upsert_auto_relevance(snapshot_id, "relevant", 1.0, ["signal"], "test")
        summary = vacancy_stats(
            self.database, area_ids=["1"], relevance="relevant", query_family="military",
        )
        self.assertEqual(summary["snapshots"], 1)
        self.assertEqual(summary["vacancies"], 1)
        self.assertEqual(summary["relevant"], 1)
        self.assertEqual(summary["by_source"], {"api": 1})
        settings = build_research_parser().parse_args([
            "stats", "--database", str(self.database.path), "--run-id", str(run_id),
        ])
        self.assertEqual(run_stats(settings)["vacancies"], 1)

    def test_raw_purge_is_preview_by_default_and_keeps_snapshots(self):
        run_id = self.database.start_run({"fixture": "raw-purge"})
        for vacancy_id, observed_at, raw_payload in (
            ("raw-old", "2024-01-01T00:00:00+00:00", b"old-raw"),
            ("raw-new", "2026-01-01T00:00:00+00:00", b"new-raw"),
        ):
            self.database.upsert_vacancy(vacancy_id, source="api")
            self.database.record_snapshot(run_id, vacancy_id, {
                "content_hash": f"hash-{vacancy_id}", "title": vacancy_id, "source": "api",
                "observed_at": observed_at, "raw_payload": raw_payload, "raw_compression": "gzip",
                "raw_size": len(raw_payload), "raw_hash": f"raw-{vacancy_id}",
            })
        parser = build_research_parser()
        preview = parser.parse_args([
            "maintenance", "--database", str(self.database.path), "purge-raw", "--before", "2025-01-01",
        ])
        self.assertEqual(run_maintenance(preview), {
            "dry_run": True, "before": "2025-01-01", "snapshots": 1, "raw_bytes": 7,
        })
        with self.assertRaisesRegex(ValueError, "--execute --confirm"):
            run_maintenance(parser.parse_args([
                "maintenance", "--database", str(self.database.path), "purge-raw", "--before", "2025-01-01", "--execute",
            ]))
        purged = run_maintenance(parser.parse_args([
            "maintenance", "--database", str(self.database.path), "purge-raw", "--before", "2025-01-01",
            "--execute", "--confirm", "PURGE_RAW_PAYLOADS",
        ]))
        self.assertEqual(purged, {
            "dry_run": False, "before": "2025-01-01", "snapshots": 1, "raw_bytes": 7, "purged": 1,
        })
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT vacancy_hh_id, title, raw_payload, raw_hash FROM vacancy_snapshots ORDER BY vacancy_hh_id"
            ).fetchall()
        self.assertEqual(rows[0]["vacancy_hh_id"], "raw-new")
        self.assertEqual(rows[0]["raw_payload"], b"new-raw")
        self.assertEqual(tuple(rows[1]), ("raw-old", "raw-old", None, "raw-raw-old"))

    def test_db_reset_previews_then_clears_data_but_keeps_schema(self):
        run_id = self.database.start_run({"fixture": "reset"})
        self.database.upsert_vacancy("reset-1", source="api")
        self.database.record_snapshot(run_id, "reset-1", {
            "content_hash": "reset-hash", "title": "Воинский учет", "source": "api",
            "description_text": "Бронирование",
        })
        parser = build_research_parser()
        preview = run_db(parser.parse_args(["db", "--database", str(self.database.path), "reset"]))
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["tables"]["vacancy_snapshots"], 1)
        cleared = run_db(parser.parse_args(["db", "--database", str(self.database.path), "reset", "--yes"]))
        self.assertFalse(cleared["dry_run"])
        self.assertEqual(cleared["tables"]["vacancy_snapshots"], 1)
        self.assertEqual(self.database.search_text("воинский"), [])
        self.assertEqual(self.database.run_counters(self.database.start_run({"fixture": "fresh"})), {
            "found": 0, "unique": 0, "loaded": 0, "errors": 0,
        })

    def test_db_check_reports_healthy_sqlite(self):
        settings = build_research_parser().parse_args(["db", "--database", str(self.database.path), "check"])
        self.assertEqual(run_db(settings), {"ok": True, "result": ["ok"]})

    def test_db_checkpoint_keeps_sqlite_usable(self):
        settings = build_research_parser().parse_args(["db", "--database", str(self.database.path), "checkpoint"])
        result = run_db(settings)
        self.assertEqual(result["busy"], 0)
        self.assertIn("log_frames", result)

    def test_skill_discovery_is_local_deterministic_and_excludes_known_aliases(self):
        run_id = self.database.start_run({"fixture": "discovery"})
        for vacancy_id in ("discover-1", "discover-2"):
            self.database.upsert_vacancy(vacancy_id, source="api")
            self.database.record_snapshot(run_id, vacancy_id, {
                "content_hash": f"hash-{vacancy_id}", "title": "Специалист", "source": "api",
                "description_text": "Новая технология для воинского учета", "key_skills": [{"name": "Новая технология"}],
            })
            self.database.upsert_auto_relevance(
                self.database.snapshot_id(vacancy_id, f"hash-{vacancy_id}"), "relevant", 1.0, [], "test",
            )
        skills_file = Path(self.temp_dir.name) / "skills.txt"
        skills_file.write_text("воинский учет\n", encoding="utf-8")
        dictionary = load_skill_dictionary(skills_file)
        first = discover_skill_candidates(self.database, dictionary, min_document_frequency=2)
        second = discover_skill_candidates(self.database, dictionary, min_document_frequency=2)
        self.assertEqual(first, second)
        candidates = {row["candidate"]: row for row in first}
        self.assertIn("новая технология", candidates)
        self.assertNotIn("воинский учет", candidates)
        output = Path(self.temp_dir.name) / "candidates.csv"
        settings = build_research_parser().parse_args([
            "discover", "--database", str(self.database.path), "skills", "--output", str(output),
            "--skills-file", str(skills_file), "--min-document-frequency", "2",
        ])
        self.assertGreater(run_discover(settings), 0)

    def test_skill_candidate_import_writes_new_dictionary_without_mutating_source(self):
        source = Path(self.temp_dir.name) / "skills.txt"
        source.write_text("воинский учет | военный учет\n", encoding="utf-8")
        review = Path(self.temp_dir.name) / "review.csv"
        review.write_text(
            "candidate,decision,canonical_skill\nновая технология,approve,новая технология\nкадровый резерв,merge,воинский учет\nшум,reject,\n",
            encoding="utf-8",
        )
        output = Path(self.temp_dir.name) / "skills-v2.txt"
        self.assertEqual(import_skill_candidates(review, source, output), 2)
        self.assertEqual(source.read_text(encoding="utf-8"), "воинский учет | военный учет\n")
        dictionary = load_skill_dictionary(output)
        self.assertEqual(dictionary.aliases["кадровый резерв"], "воинский учет")
        self.assertEqual(dictionary.aliases["новая технология"], "новая технология")
        output_cli = Path(self.temp_dir.name) / "skills-v3.txt"
        settings = build_research_parser().parse_args([
            "import", "--database", str(self.database.path), "skill-candidates", str(review),
            "--skills-file", str(source), "--output", str(output_cli),
        ])
        self.assertEqual(run_import_skill_candidates(settings), 2)

    def test_skill_export_writes_normalized_latest_evidence(self):
        run_id = self.database.start_run({"fixture": "skill-export"})
        self.database.upsert_vacancy("skill-export-1", source="api")
        self.database.record_snapshot(run_id, "skill-export-1", {
            "content_hash": "skill-export-hash", "title": "Воинский учет", "source": "api",
            "description_text": "Военный учет сотрудников",
        })
        dictionary_file = Path(self.temp_dir.name) / "skills.txt"
        dictionary_file.write_text("воинский учет | военный учет\n", encoding="utf-8")
        run_offline_extraction(self.database, "skills", skill_dictionary=load_skill_dictionary(dictionary_file))
        output = Path(self.temp_dir.name) / "skills.csv"
        self.assertEqual(export_skills(self.database, output), 2)
        with output.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual({row["source"] for row in rows}, {"title", "description"})
        settings = build_research_parser().parse_args([
            "export", "--database", str(self.database.path), "skills", "--output", str(output),
        ])
        self.assertEqual(run_export_skills(settings), 2)

    def test_role_and_query_hit_exports_are_db_only(self):
        run_id = self.database.start_run({"fixture": "relations"})
        query_id = self.database.upsert_query("воинский учет", query_group="military")
        self.database.upsert_vacancy("relation-1", source="api")
        self.database.record_query_hit(run_id, query_id, "relation-1", area_id=1, page=0, rank=0)
        self.database.record_snapshot(run_id, "relation-1", {
            "content_hash": "relation-hash", "title": "Специалист", "source": "api",
            "roles": [{"id": "1", "name": "Специалист"}],
        })
        roles = Path(self.temp_dir.name) / "roles.csv"
        hits = Path(self.temp_dir.name) / "hits.csv"
        self.assertEqual(export_roles(self.database, roles), 1)
        self.assertEqual(export_query_hits(self.database, hits), 1)

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

    def test_run_config_redacts_credentials_before_json_and_hash(self):
        run_id = self.database.start_run({
            "access_token": "secret-token", "nested": {"Authorization": "Bearer secret-token"},
            "ordinary": "kept",
        })
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT config_json, config_hash FROM collection_runs WHERE id = ?", (run_id,)
            ).fetchone()
        self.assertNotIn("secret-token", row["config_json"])
        self.assertEqual(
            row["config_json"],
            '{"access_token":"[redacted]","nested":{"Authorization":"[redacted]"},"ordinary":"kept"}',
        )
        self.assertEqual(row["config_hash"], hashlib.sha256(row["config_json"].encode("utf-8")).hexdigest())

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

    def test_offline_extraction_stores_relevance_and_versioned_features(self):
        collector = Collector(self.database)
        run_id = collector.start({"fixture": "features"}, ["1"])
        collector.collect(
            run_id, ["1"], ["воинский учет"],
            search=lambda *_: [{"id": "123", "name": "Главный специалист", "_source": "api"}],
            detail=lambda _: {
                "id": "123", "name": "Главный специалист",
                "description": "Воинский учет и бронирование сотрудников",
                "salary": {"from": 100000, "to": 120000},
                "work_format": [{"id": "REMOTE", "name": "Удаленно"}],
            },
        )
        counters_before = self.database.run_counters(run_id)
        result = run_offline_extraction(self.database, "relevance")
        self.assertEqual(result["processed"], 1)
        repeated = run_offline_extraction(self.database, "relevance")
        self.assertEqual(repeated["processed"], 1)
        self.assertEqual(self.database.run_counters(run_id), counters_before)
        result = run_offline_extraction(self.database, "features")
        self.assertEqual(result["processed"], 1)
        with self.database.connect() as connection:
            label = connection.execute("SELECT label FROM relevance_labels").fetchone()[0]
            features = {
                row["name"]: row for row in connection.execute(
                    "SELECT name, value_text, value_number, value_json FROM features"
                )
            }
        self.assertEqual(label, "relevant")
        self.assertEqual(features["title.seniority"]["value_text"], "senior")
        self.assertEqual(features["salary.midpoint"]["value_number"], 110000)
        self.assertEqual(features["work.remote"]["value_number"], 1)
        self.assertIn("бронирован", features["topic.reservation"]["value_json"])

    def test_features_extract_security_and_publication_signals(self):
        features = {
            item["name"]: item
            for item in extract_features({
                "title": "Специалист",
                "description_text": (
                    "Требуется допуск к государственной тайне, военный билет, "
                    "работа в военное время. Участие в учениях по ГО. "
                    "Взаимодействие с военкоматом."
                ),
                "published_at": "2026-07-01T12:00:00+03:00",
                "observed_at": "2026-07-04T12:00:00+03:00",
                "salary_from": 100000,
                "salary_to": 120000,
                "salary_currency": "RUB",
                "salary_frequency": "MONTH",
                "work_formats": [],
            })
        }
        for signal in ("security_clearance", "military_id", "wartime", "exercise", "government_interaction"):
            self.assertEqual(features[f"signal.{signal}"]["value_number"], 1)
            self.assertTrue(features[f"signal.{signal}.evidence"]["value_json"])
        self.assertEqual(features["publication.day"]["value_text"], "2026-07-01")
        self.assertEqual(features["publication.week"]["value_text"], "2026-W27")
        self.assertEqual(features["publication.age_days"]["value_number"], 3)
        self.assertEqual(features["salary.monthly_rub"]["value_number"], 110000)

    def test_offline_features_detect_repost_by_title_employer_and_text(self):
        run_id = self.database.start_run({"fixture": "repost"})
        snapshots = []
        for vacancy_id in ("repost-1", "repost-2"):
            self.database.upsert_vacancy(vacancy_id, source="api")
            snapshot = {
                "content_hash": f"repost-{vacancy_id}", "title": "Специалист по воинскому учету",
                "description_text": "Ведение воинского учета сотрудников.", "employer_id": "42",
                "employer_name": "АО Пример", "source": "api",
            }
            self.database.record_snapshot(run_id, vacancy_id, snapshot)
            snapshots.append(snapshot)
        self.assertEqual(repost_fingerprint(snapshots[0]), repost_fingerprint(snapshots[1]))
        run_offline_extraction(self.database, "features")
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT name, value_number FROM features WHERE name IN ('duplicate.repost_count', 'duplicate.is_repost') "
                "ORDER BY snapshot_id, name"
            ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [
            ("duplicate.is_repost", 1.0), ("duplicate.repost_count", 2.0),
            ("duplicate.is_repost", 1.0), ("duplicate.repost_count", 2.0),
        ])

    def test_offline_skill_extraction_stores_versioned_evidence(self):
        skills_path = Path(self.temp_dir.name) / "skills.txt"
        skills_path.write_text("воинский учет | военный учет\nбронирование\n", encoding="utf-8")
        collector = Collector(self.database)
        run_id = collector.start({"fixture": "skills"}, ["1"])
        collector.collect(
            run_id, ["1"], ["воинский учет"],
            search=lambda *_: [{"id": "123", "name": "Военный учет", "_source": "api"}],
            detail=lambda _: {
                "id": "123", "name": "Военный учет", "description": "Воинский учет и бронирование",
                "key_skills": [{"name": "Бронирование"}],
            },
        )
        result = run_offline_extraction(
            self.database, "skills", skill_dictionary=load_skill_dictionary(skills_path),
        )
        self.assertEqual(result["processed"], 1)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT s.canonical_name, v.source, v.matched_alias, v.match_count "
                "FROM vacancy_skills v JOIN skills s ON s.id=v.skill_id ORDER BY s.canonical_name, v.source"
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                ("бронирование", "description", "бронирование", 1),
                ("бронирование", "key_skill", "бронирование", 1),
                ("воинский учет", "description", "воинский учет", 1),
                ("воинский учет", "title", "военный учет", 1),
            ],
        )

    def test_extract_cli_defaults_to_latest_snapshot_without_transport(self):
        run_id = self.database.start_run({"fixture": "extract-cli"})
        self.database.upsert_vacancy("123", source="api")
        self.database.record_snapshot(run_id, "123", {
            "content_hash": "extract-cli", "title": "Воинский учет", "source": "api",
        })
        settings = build_research_parser().parse_args([
            "extract", "--database", str(self.database.path), "relevance",
        ])
        result = run_extract(settings)
        self.assertEqual(result, {"run_id": 1, "selected": 1, "processed": 1, "errors": 0})
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM relevance_labels").fetchone()[0], 1)

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

    def test_effective_relevance_view_prefers_manual_label(self):
        run_id = self.database.start_run({"fixture": "manual-label"})
        self.database.upsert_vacancy("123", source="api")
        self.database.record_snapshot(run_id, "123", {
            "content_hash": "manual-label-hash", "title": "Специалист", "source": "api",
        })
        snapshot_id = self.database.snapshot_id("123", "manual-label-hash")
        self.database.upsert_auto_relevance(snapshot_id, "borderline", 0.0, [], "test")
        self.database.set_manual_relevance(snapshot_id, "relevant", "reviewed")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT auto_label, effective_label, effective_reason FROM effective_relevance_labels"
            ).fetchone()
        self.assertEqual(tuple(row), ("borderline", "relevant", "reviewed"))

    def test_relevance_reextract_keeps_manual_label(self):
        run_id = self.database.start_run({"fixture": "manual-reextract"})
        self.database.upsert_vacancy("123", source="api")
        self.database.record_snapshot(run_id, "123", {
            "content_hash": "manual-reextract-hash", "title": "Специалист",
            "description_text": "Воинский учет", "source": "api",
        })
        snapshot_id = self.database.snapshot_id("123", "manual-reextract-hash")
        self.database.upsert_auto_relevance(snapshot_id, "relevant", 1.0, ["old"], "old")
        self.database.set_manual_relevance(snapshot_id, "irrelevant", "reviewed")
        run_offline_extraction(self.database, "relevance")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT label, manual_label, manual_reason FROM relevance_labels WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        self.assertEqual(tuple(row), ("relevant", "irrelevant", "reviewed"))

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

    def test_resume_uses_frozen_query_spec_not_changed_file(self):
        self.database.store_area_catalog(
            [{"id": "113", "name": "Россия", "areas": [
                {"id": "1", "name": "Москва", "parent_id": "113", "areas": []},
            ]}], source_url="https://api.hh.ru/areas",
        )
        queries_path = Path(self.temp_dir.name) / "queries.toml"
        queries_path.write_text(
            "version = 'v1'\n[[query]]\nid = 'broad'\nexpression = 'мобилизац*'\n"
            "search_fields = ['name', 'description']\n", encoding="utf-8",
        )
        parser = build_research_parser()
        settings = parser.parse_args([
            "collect", "--database", str(self.database.path), "--queries-file", str(queries_path), "--area", "1",
        ])

        class Source:
            def __init__(self):
                self.calls = []

            def search_page(self, query, area_id, **kwargs):
                self.calls.append((query, kwargs.get("search_fields")))
                if len(self.calls) == 1:
                    raise RuntimeError("interrupt")
                return [], True

            def detail(self, candidate):
                return candidate

        source = Source()
        run_id, _ = run_collect(settings, source_factory=lambda _: source)
        queries_path.write_text(
            "[[query]]\nid = 'changed'\nexpression = 'другая фраза'\nsearch_fields = ['name']\n",
            encoding="utf-8",
        )
        run_resume(
            parser.parse_args(["resume", "--database", str(self.database.path), "--run-id", str(run_id)]),
            source_factory=lambda _: source,
        )
        self.assertEqual(source.calls, [
            ("мобилизац*", ("name", "description")),
            ("мобилизац*", ("name", "description")),
        ])

    def test_incremental_uses_compatible_watermark_overlap_and_advances_only_when_complete(self):
        self.database.store_area_catalog(
            [{"id": "113", "name": "Россия", "areas": [
                {"id": "1", "name": "Москва", "parent_id": "113", "areas": []},
            ]}], source_url="https://api.hh.ru/areas",
        )
        queries_path = Path(self.temp_dir.name) / "queries.txt"
        queries_path.write_text("воинский учет\n", encoding="utf-8")
        parser = build_research_parser()

        class Source:
            def __init__(self, fail_detail=False):
                self.windows = []
                self.fail_detail = fail_detail

            def search_page(self, _query, _area, *, page, date_from=None, date_to=None):
                self.windows.append((page, date_from, date_to))
                return [{"id": "123", "name": "Специалист", "_source": "api"}], True

            def detail(self, candidate):
                if self.fail_detail:
                    raise RuntimeError("temporary")
                return {"id": candidate["id"], "name": "Специалист"}

        first = Source()
        first_run, first_counts = run_collect(parser.parse_args([
            "collect", "--database", str(self.database.path), "--queries-file", str(queries_path), "--area", "1",
            "--date-from", "2026-01-01", "--date-to", "2026-01-10", "--incremental-overlap-days", "2",
        ]), source_factory=lambda _: first)
        self.assertEqual(first_counts["errors"], 0)
        self.assertEqual(first.windows, [(0, "2026-01-01", "2026-01-10")])
        first_config = self.database.run_config(first_run)
        self.assertEqual((first_config["effective_mode"], first_config["effective_date_from"], first_config["effective_date_to"]), ("incremental", "2026-01-01", "2026-01-10"))
        self.assertEqual(self.database.collection_watermark(first_config["watermark_scope_hash"]), "2026-01-10")

        second = Source()
        second_run, second_counts = run_collect(parser.parse_args([
            "collect", "--database", str(self.database.path), "--queries-file", str(queries_path), "--area", "1",
            "--date-to", "2026-01-15", "--incremental-overlap-days", "2",
        ]), source_factory=lambda _: second)
        self.assertEqual(second_counts["errors"], 0)
        self.assertEqual(second.windows, [(0, "2026-01-08", "2026-01-15")])
        second_config = self.database.run_config(second_run)
        self.assertEqual(second_config["watermark_before"], "2026-01-10")
        self.assertEqual(self.database.collection_watermark(second_config["watermark_scope_hash"]), "2026-01-15")
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM vacancy_snapshots WHERE vacancy_hh_id = '123'").fetchone()[0], 1)

        broken = Source(fail_detail=True)
        broken_run, broken_counts = run_collect(parser.parse_args([
            "collect", "--database", str(self.database.path), "--queries-file", str(queries_path), "--area", "1",
            "--date-to", "2026-01-20", "--incremental-overlap-days", "2",
        ]), source_factory=lambda _: broken)
        self.assertEqual(broken_counts["errors"], 1)
        broken_config = self.database.run_config(broken_run)
        self.assertEqual(self.database.collection_watermark(broken_config["watermark_scope_hash"]), "2026-01-15")

    def test_full_requires_explicit_window_and_does_not_create_watermark(self):
        self.database.store_area_catalog(
            [{"id": "113", "name": "Россия", "areas": [
                {"id": "1", "name": "Москва", "parent_id": "113", "areas": []},
            ]}], source_url="https://api.hh.ru/areas",
        )
        queries_path = Path(self.temp_dir.name) / "queries.txt"
        queries_path.write_text("воинский учет\n", encoding="utf-8")
        parser = build_research_parser()
        settings = parser.parse_args(["collect", "--collection-mode", "full", "--area", "1"])
        with self.assertRaisesRegex(ValueError, "full collection requires"):
            resolve_collection_window(settings, self.database, "scope")
        calls = []

        class Source:
            @staticmethod
            def search_page(_query, _area, *, page, date_from=None, date_to=None):
                calls.append((page, date_from, date_to))
                return [], True

            @staticmethod
            def detail(candidate):
                return candidate

        run_id, counters = run_collect(parser.parse_args([
            "collect", "--database", str(self.database.path), "--collection-mode", "full", "--area", "1",
            "--queries-file", str(queries_path), "--date-from", "2025-01-01", "--date-to", "2025-12-31",
        ]), source_factory=lambda _: Source())
        self.assertEqual((counters, calls), ({"found": 0, "unique": 0, "loaded": 0, "errors": 0}, [(0, "2025-01-01", "2025-12-31")]))
        config = self.database.run_config(run_id)
        with self.database.connect() as connection:
            mode = connection.execute("SELECT collection_mode FROM collection_runs WHERE id = ?", (run_id,)).fetchone()[0]
            watermarks = connection.execute("SELECT COUNT(*) FROM collection_watermarks").fetchone()[0]
        self.assertEqual((mode, config["effective_mode"], watermarks), ("full", "full", 0))

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

    def test_paginated_collector_persists_transport_http_status(self):
        class Response:
            status_code = 429

        error = requests.HTTPError("HTTP 429")
        error.response = Response()
        run_id = Collector(self.database).start({"fixture": "http-status"}, ["1"])
        counters = Collector(self.database).collect_paginated(
            run_id, ["1"], ["воинский учет"],
            search_page=lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
            detail=lambda candidate: candidate,
        )

        self.assertEqual(counters["errors"], 1)
        with self.database.connect() as connection:
            page = connection.execute(
                "SELECT http_status FROM search_pages WHERE run_id = ?", (run_id,)
            ).fetchone()
            recorded = connection.execute(
                "SELECT http_status FROM collection_errors WHERE run_id = ?", (run_id,)
            ).fetchone()
        self.assertEqual((page["http_status"], recorded["http_status"]), (429, 429))

    def test_snapshot_batch_keeps_pages_hits_durable_and_loads_all_cards(self):
        run_id = Collector(self.database, write_batch_size=2).start({"fixture": "batch"}, ["1"])
        Collector(self.database, write_batch_size=2).collect_paginated(
            run_id, ["1"], ["воинский учет"],
            search_page=lambda *_args, **_kwargs: ([
                {"id": "1", "name": "Один", "_source": "api"},
                {"id": "2", "name": "Два", "_source": "api"},
                {"id": "3", "name": "Три", "_source": "api"},
            ], True),
            detail=lambda candidate: {"id": candidate["id"], "name": candidate["name"]},
        )
        with self.database.connect() as connection:
            persisted = connection.execute(
                "SELECT (SELECT COUNT(*) FROM search_pages WHERE run_id=?), "
                "(SELECT COUNT(*) FROM vacancy_query_hits WHERE run_id=?), "
                "(SELECT COUNT(*) FROM vacancy_snapshot_observations WHERE run_id=?)",
                (run_id, run_id, run_id),
            ).fetchone()
        self.assertEqual(tuple(persisted), (1, 3, 3))

    def test_card_transport_outcomes_and_counters_are_persisted(self):
        run_id = Collector(self.database).start({"fixture": "card-audit"}, ["1"])
        counters = Collector(self.database).collect_paginated(
            run_id, ["1"], ["воинский учет"],
            search_page=lambda *_args, **_kwargs: ([{"id": "1", "name": "Один", "_source": "api"}], True),
            detail=lambda candidate: {"id": candidate["id"], "name": candidate["name"]},
        )
        with self.database.connect() as connection:
            request = connection.execute(
                "SELECT source, http_status, reason_code FROM vacancy_requests WHERE run_id=?", (run_id,),
            ).fetchone()
        self.assertEqual(tuple(request), ("api", 200, "success"))
        self.assertEqual(self.database.counter_reconciliation(run_id), {
            "persisted": counters, "recorded": counters, "matches": True,
        })

    def test_finish_run_rejects_counter_drift(self):
        run_id = self.database.start_run({"fixture": "counter-drift"})
        with self.assertRaisesRegex(ValueError, "do not match"):
            self.database.finish_run(run_id, "completed", {
                "found": 1, "unique": 1, "loaded": 1, "errors": 0,
            })

    def test_coverage_report_and_retry_only_unresolved_card(self):
        parser = build_research_parser()
        run_id = Collector(self.database).start({"fixture": "coverage"}, ["1"])
        calls = []

        def search_page(_query, _area, *, page, **_kwargs):
            calls.append(("search", page))
            return [{"id": "123", "name": "Специалист", "_source": "api"}], True

        def broken_detail(_candidate):
            calls.append(("detail", "broken"))
            raise requests.Timeout("temporary")

        Collector(self.database).collect_paginated(
            run_id, ["1"], ["воинский учет"], search_page=search_page, detail=broken_detail,
        )
        coverage = run_coverage(parser.parse_args([
            "coverage", "--database", str(self.database.path), "--run-id", str(run_id),
        ]))
        self.assertEqual(len(coverage), 1)
        self.assertEqual(
            {key: coverage[0][key] for key in ("requested", "completed", "saturated", "failed", "cards_requested", "cards_loaded", "cards_missing", "card_failures")},
            {"requested": 1, "completed": 1, "saturated": 0, "failed": 0, "cards_requested": 1, "cards_loaded": 0, "cards_missing": 1, "card_failures": 1},
        )

        class Source:
            source_name = "api"
            search_page = staticmethod(lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("search must not retry")))

            @staticmethod
            def detail(candidate):
                calls.append(("detail", candidate["id"]))
                return {"id": candidate["id"], "name": "Специалист"}

        counters = run_retry(parser.parse_args([
            "retry", "--database", str(self.database.path), "--run-id", str(run_id), "--max-attempts", "2",
        ]), source_factory=lambda _: Source())
        self.assertEqual(counters["errors"], 0)
        self.assertEqual(calls, [("search", 0), ("detail", "broken"), ("detail", "123")])

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

    def test_paginated_collector_passes_query_spec_search_fields(self):
        run_id = Collector(self.database).start({"fixture": "query-fields"}, ["1"])
        received = []

        def search_page(_query, _area_id, **kwargs):
            received.append(kwargs.get("search_fields"))
            return [], True

        Collector(self.database).collect_paginated(
            run_id, ["1"], [QuerySpec("broad", "мобилизац*", search_fields=("name", "description"))],
            search_page=search_page, detail=lambda candidate: candidate,
        )
        self.assertEqual(received, [("name", "description")])

    def test_paginated_collector_persists_actual_safe_api_request_params(self):
        class Transport:
            base_url = "https://api.hh.ru"

            @staticmethod
            def search_request_params(expression, area_id, **kwargs):
                return {
                    "text": expression, "area": area_id, "page": kwargs["page"], "per_page": 100,
                    "host": "hh.ru", "locale": "RU", "search_field": list(kwargs["search_fields"]),
                }

        run_id = Collector(self.database, transport=Transport()).start({"fixture": "request-params"}, ["1"])
        Collector(self.database, transport=Transport()).collect_paginated(
            run_id, ["1"], [QuerySpec("broad", "мобилизац*", search_fields=("name", "description"))],
            search_page=lambda *_args, **_kwargs: ([], True), detail=lambda candidate: candidate,
        )
        with self.database.connect() as connection:
            page = connection.execute(
                "SELECT request_url, request_params_json FROM search_pages WHERE run_id = ?", (run_id,)
            ).fetchone()
        self.assertEqual(page["request_url"], "https://api.hh.ru/vacancies")
        self.assertEqual(
            page["request_params_json"],
            '{"area":"1","host":"hh.ru","locale":"RU","page":0,"per_page":100,"search_field":["name","description"],"text":"мобилизац*"}',
        )

    def test_saturated_date_window_splits_until_every_child_fits(self):
        collector = Collector(self.database)
        run_id = collector.start({"fixture": "split"}, ["1"])
        calls = []

        def search_page(query, area_id, *, page, date_from=None, date_to=None):
            calls.append((date_from, date_to, page))
            fits = (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days <= 1
            vacancy_id = f"{date_from}-{date_to}"
            return ([{"id": vacancy_id, "name": "Специалист", "_source": "api"}], fits)

        counters = collector.collect_sliced(
            run_id, ["1"], ["воинский учет"], search_page=search_page,
            detail=lambda candidate: {"id": candidate["id"], "name": "Специалист"},
            max_pages=1, date_from="2026-01-01", date_to="2026-01-05",
            min_window_days=1, overlap_days=1,
        )
        self.assertEqual(counters["errors"], 0)
        self.assertIn(("2026-01-01", "2026-01-05", 0), calls)
        self.assertTrue(all((end != "2026-01-05" or start != "2026-01-01") for start, end, _ in calls[1:]))
        self.assertEqual(
            split_date_window("2026-01-01", "2026-01-03", min_window_days=1, overlap_days=1),
            [("2026-01-01", "2026-01-02"), ("2026-01-02", "2026-01-03")],
        )

    def test_window_error_resolution_does_not_hide_sibling_window(self):
        run_id = self.database.start_run({"fixture": "window-errors"})
        query_id = self.database.upsert_query("воинский учет")
        for date_from, date_to in (("2026-01-01", "2026-01-02"), ("2026-01-02", "2026-01-03")):
            self.database.record_error(
                run_id, "coverage", "SearchDepthSaturated", "fixture", query_id=query_id,
                area_id=1, date_from=date_from, date_to=date_to,
            )
        self.database.resolve_errors(
            run_id, "coverage", query_id=query_id, area_id=1,
            date_from="2026-01-01", date_to="2026-01-02",
        )
        with self.database.connect() as connection:
            unresolved = connection.execute(
                "SELECT date_from FROM collection_errors WHERE run_id = ? AND resolved_at IS NULL",
                (run_id,),
            ).fetchall()
        self.assertEqual([row["date_from"] for row in unresolved], ["2026-01-02"])


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


class ConfigTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
