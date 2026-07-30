CREATE TABLE IF NOT EXISTS relevance_pilot_batches (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    sample_seed TEXT NOT NULL,
    requested_size INTEGER NOT NULL CHECK (requested_size > 0),
    filters_json TEXT NOT NULL,
    query_specs_json TEXT NOT NULL,
    selection_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relevance_pilot_items (
    batch_id TEXT NOT NULL REFERENCES relevance_pilot_batches(id) ON DELETE CASCADE,
    snapshot_id INTEGER NOT NULL REFERENCES vacancy_snapshots(id),
    stratum_json TEXT NOT NULL,
    PRIMARY KEY (batch_id, snapshot_id)
);

CREATE INDEX IF NOT EXISTS idx_relevance_pilot_items_snapshot
ON relevance_pilot_items(snapshot_id);
