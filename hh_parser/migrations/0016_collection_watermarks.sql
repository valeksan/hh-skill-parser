CREATE TABLE IF NOT EXISTS collection_watermarks (
    scope_hash TEXT PRIMARY KEY,
    scope_json TEXT NOT NULL,
    watermark_date TEXT NOT NULL,
    run_id INTEGER NOT NULL REFERENCES collection_runs(id),
    advanced_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_collection_watermarks_run
ON collection_watermarks(run_id);
