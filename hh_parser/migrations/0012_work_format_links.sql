CREATE TABLE IF NOT EXISTS snapshot_work_formats (
    snapshot_id INTEGER NOT NULL REFERENCES vacancy_snapshots(id),
    work_format_id TEXT,
    work_format_name TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, work_format_name)
);

CREATE INDEX IF NOT EXISTS idx_snapshot_work_formats_id ON snapshot_work_formats(work_format_id);
