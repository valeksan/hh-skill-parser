import csv
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import parse_skills


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "parse_skills.py"


class SmokeTests(unittest.TestCase):
    def test_help_command_succeeds(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--source", result.stdout)
        self.assertIn("--no-chart", result.stdout)

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

    def test_load_queries_creates_default_file_when_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            queries_path = Path(temp_dir) / "queries.txt"

            queries = parse_skills.load_queries(str(queries_path))

            self.assertTrue(queries_path.exists())
            self.assertGreater(len(queries), 0)
            self.assertIn("ai wizard intern", queries)

    def test_load_skills_whitelist_creates_default_file_when_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            whitelist_path = Path(temp_dir) / "skills_whitelist.txt"

            skills = parse_skills.load_skills_whitelist(str(whitelist_path))

            self.assertTrue(whitelist_path.exists())
            self.assertIn("python", skills)
            self.assertIn("терпение к легаси", skills)

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


if __name__ == "__main__":
    unittest.main()
