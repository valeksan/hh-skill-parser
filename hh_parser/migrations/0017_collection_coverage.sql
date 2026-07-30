ALTER TABLE search_pages ADD COLUMN source TEXT NOT NULL DEFAULT 'api';
ALTER TABLE collection_errors ADD COLUMN source TEXT;
ALTER TABLE collection_errors ADD COLUMN reason_code TEXT;

CREATE INDEX IF NOT EXISTS idx_errors_retryable
ON collection_errors(run_id, stage, resolved_at, attempt, query_id, area_id, vacancy_hh_id);
