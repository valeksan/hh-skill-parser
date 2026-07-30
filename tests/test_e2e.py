import csv
import tempfile
import unittest
from pathlib import Path

from hh_parser.cli import (
    build_parser,
    run_areas_sync,
    run_collect,
    run_discover,
    run_export_marts,
    run_import_skill_candidates,
    run_resume,
    run_extract,
    run_stats,
)
from hh_parser.storage import Database


class FixtureE2ETests(unittest.TestCase):
    """Deterministic complete workflow; every transport response is local."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "research.sqlite3"
        self.queries_path = self.root / "queries.toml"
        self.queries_path.write_text(
            'version = "fixture-v1"\n\n[[query]]\nid = "registration"\n'
            'group = "fixture"\nexpression = "воинский учет"\n'
            'search_fields = ["name"]\n', encoding="utf-8",
        )
        self.parser = build_parser()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_fixture_workflow_is_resumable_offline_and_idempotent(self):
        class Source:
            def __init__(self):
                self.search_calls = 0
                self.detail_calls = 0

            def areas(self):
                return [{"id": "113", "name": "Россия", "areas": [
                    {"id": "1", "name": "Москва", "parent_id": "113", "areas": []},
                ]}]

            def search_page(self, *_args, page, **_kwargs):
                self.search_calls += 1
                if page != 0:
                    raise AssertionError(f"unexpected page: {page}")
                return ([{"id": "fixture-1", "name": "Специалист по воинскому учету", "_source": "api"}], True)

            def detail(self, candidate):
                self.detail_calls += 1
                if self.detail_calls == 1:
                    raise KeyboardInterrupt()
                return {
                    "id": candidate["id"], "name": "Специалист по воинскому учету",
                    "description": "Воинский учет, бронирование, кадровый резерв.",
                    "published_at": "2026-07-01T12:00:00+05:00",
                    "archived": True, "employer": None, "salary": None,
                    "key_skills": [{"name": "Бронирование"}],
                }

        source = Source()
        catalog_id = run_areas_sync(
            self.parser.parse_args(["areas", "--database", str(self.database_path), "sync"]),
            source_factory=lambda _: source,
        )
        self.assertEqual(catalog_id, 1)

        collect = self.parser.parse_args([
            "collect", "--database", str(self.database_path), "--queries-file", str(self.queries_path),
            "--area", "1", "--date-from", "2026-07-01", "--date-to", "2026-07-01",
        ])
        with self.assertRaises(KeyboardInterrupt):
            run_collect(collect, source_factory=lambda _: source)
        database = Database(self.database_path)
        run_id = 1
        self.assertEqual(database.run_counters(run_id), {"found": 1, "unique": 1, "loaded": 0, "errors": 0})

        resumed = run_resume(
            self.parser.parse_args(["resume", "--database", str(self.database_path), "--run-id", str(run_id)]),
            source_factory=lambda _: source,
        )
        self.assertEqual(resumed, {"found": 1, "unique": 1, "loaded": 1, "errors": 0})
        self.assertEqual((source.search_calls, source.detail_calls), (1, 2))

        before_counters = database.run_counters(run_id)
        with database.connect() as connection:
            before_status = connection.execute("SELECT status FROM collection_runs WHERE id = ?", (run_id,)).fetchone()[0]
        run_extract(self.parser.parse_args(["extract", "--database", str(self.database_path), "relevance"]))
        run_extract(self.parser.parse_args(["extract", "--database", str(self.database_path), "features"]))
        self.assertEqual(database.run_counters(run_id), before_counters)
        with database.connect() as connection:
            self.assertEqual(connection.execute("SELECT status FROM collection_runs WHERE id = ?", (run_id,)).fetchone()[0], before_status)

        dictionary = self.root / "skills.txt"
        dictionary.write_text("воинский учет\n", encoding="utf-8")
        candidates = self.root / "candidates.csv"
        discovered = run_discover(self.parser.parse_args([
            "discover", "--database", str(self.database_path), "skills", "--skills-file", str(dictionary),
            "--min-document-frequency", "1", "--output", str(candidates),
        ]))
        self.assertGreater(discovered, 0)
        with candidates.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        row = next(item for item in rows if item["candidate"] == "бронирование")
        row.update({"decision": "approve", "canonical_skill": "бронирование", "topic_family": "registration"})
        with candidates.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        expanded_dictionary = self.root / "skills-expanded.txt"
        self.assertEqual(run_import_skill_candidates(self.parser.parse_args([
            "import", "--database", str(self.database_path), "skill-candidates", str(candidates),
            "--skills-file", str(dictionary), "--output", str(expanded_dictionary),
        ])), 1)
        run_extract(self.parser.parse_args([
            "extract", "--database", str(self.database_path), "--skills-file", str(expanded_dictionary), "skills",
        ]))

        marts = self.root / "marts"
        manifest = run_export_marts(self.parser.parse_args([
            "export", "--database", str(self.database_path), "marts", "--output-dir", str(marts),
        ]))
        stats = run_stats(self.parser.parse_args(["stats", "--database", str(self.database_path)]))
        self.assertEqual(manifest["outputs"]["query_noise"]["rows"], 1)
        self.assertEqual(stats["vacancies"], 1)
        self.assertTrue((marts / "manifest.json").exists())
        self.assertEqual(database.run_counters(run_id), before_counters)
        self.assertEqual((source.search_calls, source.detail_calls), (1, 2))


if __name__ == "__main__":
    unittest.main()
