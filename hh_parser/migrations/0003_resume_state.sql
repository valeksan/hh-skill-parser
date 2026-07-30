ALTER TABLE vacancy_snapshots ADD COLUMN last_seen_at TEXT;

UPDATE vacancy_snapshots
SET last_seen_at = observed_at
WHERE last_seen_at IS NULL;

ALTER TABLE collection_errors ADD COLUMN resolved_at TEXT;

CREATE TABLE IF NOT EXISTS vacancy_snapshot_observations (
    run_id INTEGER NOT NULL REFERENCES collection_runs(id),
    vacancy_hh_id TEXT NOT NULL REFERENCES vacancies(hh_id),
    snapshot_id INTEGER NOT NULL REFERENCES vacancy_snapshots(id),
    observed_at TEXT NOT NULL,
    PRIMARY KEY (run_id, snapshot_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshot_observations_run_vacancy
ON vacancy_snapshot_observations(run_id, vacancy_hh_id);
