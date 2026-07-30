CREATE TABLE IF NOT EXISTS vacancy_requests (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES collection_runs(id),
    vacancy_hh_id TEXT NOT NULL REFERENCES vacancies(hh_id),
    source TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    http_status INTEGER,
    error_type TEXT,
    error_message TEXT,
    reason_code TEXT
);

CREATE INDEX IF NOT EXISTS idx_vacancy_requests_run_vacancy
ON vacancy_requests(run_id, vacancy_hh_id);
