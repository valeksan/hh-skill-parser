CREATE TABLE IF NOT EXISTS area_catalog_versions (
    id INTEGER PRIMARY KEY,
    source_url TEXT NOT NULL,
    host TEXT NOT NULL,
    locale TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS areas (
    catalog_version_id INTEGER NOT NULL REFERENCES area_catalog_versions(id),
    hh_id TEXT NOT NULL,
    parent_hh_id TEXT,
    name TEXT NOT NULL,
    depth INTEGER NOT NULL,
    PRIMARY KEY (catalog_version_id, hh_id)
);

CREATE TABLE IF NOT EXISTS run_areas (
    run_id INTEGER NOT NULL REFERENCES collection_runs(id),
    area_hh_id TEXT NOT NULL,
    catalog_version_id INTEGER REFERENCES area_catalog_versions(id),
    selection_source TEXT NOT NULL,
    PRIMARY KEY (run_id, area_hh_id)
);

CREATE INDEX IF NOT EXISTS idx_areas_parent ON areas(catalog_version_id, parent_hh_id);
