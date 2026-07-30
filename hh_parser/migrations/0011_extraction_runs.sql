CREATE TABLE IF NOT EXISTS extraction_runs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('relevance', 'features', 'skills')),
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'degraded', 'failed')),
    extractor_version TEXT NOT NULL,
    config_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    selected_count INTEGER NOT NULL DEFAULT 0,
    processed_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS extraction_errors (
    id INTEGER PRIMARY KEY,
    extraction_run_id INTEGER NOT NULL REFERENCES extraction_runs(id),
    snapshot_id INTEGER REFERENCES vacancy_snapshots(id),
    error_type TEXT NOT NULL,
    message TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
