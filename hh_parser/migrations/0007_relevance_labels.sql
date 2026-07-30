CREATE TABLE IF NOT EXISTS relevance_labels (
    snapshot_id INTEGER PRIMARY KEY REFERENCES vacancy_snapshots(id),
    label TEXT NOT NULL CHECK (label IN ('relevant', 'borderline', 'irrelevant', 'unknown')),
    score REAL NOT NULL,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    classifier_version TEXT NOT NULL,
    calculated_at TEXT NOT NULL,
    manual_label TEXT CHECK (manual_label IN ('relevant', 'borderline', 'irrelevant', 'unknown')),
    manual_reason TEXT,
    manual_labeled_at TEXT
);
