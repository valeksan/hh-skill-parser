CREATE TABLE IF NOT EXISTS features (
    snapshot_id INTEGER NOT NULL REFERENCES vacancy_snapshots(id),
    name TEXT NOT NULL,
    value_type TEXT NOT NULL CHECK (value_type IN ('boolean', 'number', 'text', 'json')),
    value_text TEXT,
    value_number REAL,
    value_json TEXT,
    extractor_version TEXT NOT NULL,
    calculated_at TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, name, extractor_version)
);

CREATE INDEX IF NOT EXISTS idx_features_name_number ON features(name, value_number);
