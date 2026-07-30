CREATE TABLE IF NOT EXISTS collection_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'degraded', 'failed', 'cancelled')),
    app_version TEXT,
    git_commit TEXT,
    source_policy TEXT,
    collection_mode TEXT NOT NULL DEFAULT 'incremental' CHECK (collection_mode IN ('incremental', 'full')),
    config_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    found_count INTEGER NOT NULL DEFAULT 0,
    unique_count INTEGER NOT NULL DEFAULT 0,
    loaded_count INTEGER NOT NULL DEFAULT 0,
    rejected_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS search_queries (
    id INTEGER PRIMARY KEY,
    expression TEXT NOT NULL,
    normalized_expression TEXT NOT NULL,
    query_group TEXT,
    purpose TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    version TEXT NOT NULL DEFAULT '1',
    UNIQUE (normalized_expression, version)
);

CREATE TABLE IF NOT EXISTS vacancies (
    hh_id TEXT PRIMARY KEY,
    alternate_url TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    first_source TEXT NOT NULL,
    latest_source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_pages (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES collection_runs(id),
    query_id INTEGER NOT NULL REFERENCES search_queries(id),
    area_id INTEGER NOT NULL DEFAULT -1,
    date_from TEXT NOT NULL DEFAULT '',
    date_to TEXT NOT NULL DEFAULT '',
    page INTEGER NOT NULL,
    request_url TEXT,
    request_params_json TEXT NOT NULL DEFAULT '{}',
    requested_at TEXT NOT NULL,
    http_status INTEGER,
    result_count INTEGER,
    is_last_page INTEGER NOT NULL DEFAULT 0 CHECK (is_last_page IN (0, 1)),
    error_type TEXT,
    error_message TEXT,
    UNIQUE (run_id, query_id, area_id, date_from, date_to, page)
);

CREATE TABLE IF NOT EXISTS vacancy_query_hits (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES collection_runs(id),
    query_id INTEGER NOT NULL REFERENCES search_queries(id),
    area_id INTEGER NOT NULL DEFAULT -1,
    date_from TEXT NOT NULL DEFAULT '',
    date_to TEXT NOT NULL DEFAULT '',
    vacancy_hh_id TEXT NOT NULL REFERENCES vacancies(hh_id),
    page INTEGER,
    rank INTEGER,
    observed_at TEXT NOT NULL,
    UNIQUE (run_id, query_id, area_id, date_from, date_to, vacancy_hh_id)
);

CREATE TABLE IF NOT EXISTS vacancy_snapshots (
    id INTEGER PRIMARY KEY,
    vacancy_hh_id TEXT NOT NULL REFERENCES vacancies(hh_id),
    run_id INTEGER NOT NULL REFERENCES collection_runs(id),
    observed_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    title TEXT NOT NULL,
    description_html TEXT,
    description_text TEXT,
    published_at TEXT,
    created_at TEXT,
    expires_at TEXT,
    archived INTEGER,
    employer_id TEXT,
    employer_name TEXT,
    area_id INTEGER,
    area_name TEXT,
    federal_district TEXT,
    federal_subject TEXT,
    locality TEXT,
    salary_from REAL,
    salary_to REAL,
    salary_currency TEXT,
    salary_gross INTEGER,
    salary_frequency TEXT,
    source TEXT NOT NULL,
    completeness_json TEXT NOT NULL DEFAULT '{}',
    raw_payload BLOB,
    raw_content_type TEXT,
    raw_compression TEXT,
    raw_size INTEGER,
    raw_hash TEXT,
    redaction_applied INTEGER NOT NULL DEFAULT 0 CHECK (redaction_applied IN (0, 1)),
    redaction_version TEXT,
    redaction_types_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE (vacancy_hh_id, content_hash)
);

CREATE TABLE IF NOT EXISTS collection_errors (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES collection_runs(id),
    stage TEXT NOT NULL,
    query_id INTEGER REFERENCES search_queries(id),
    area_id INTEGER,
    vacancy_hh_id TEXT REFERENCES vacancies(hh_id),
    error_type TEXT NOT NULL,
    http_status INTEGER,
    message TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    occurred_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_search_pages_run ON search_pages(run_id);
CREATE INDEX IF NOT EXISTS idx_hits_vacancy ON vacancy_query_hits(vacancy_hh_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_vacancy ON vacancy_snapshots(vacancy_hh_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_errors_run ON collection_errors(run_id);
