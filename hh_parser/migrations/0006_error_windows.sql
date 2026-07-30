ALTER TABLE collection_errors ADD COLUMN date_from TEXT;
ALTER TABLE collection_errors ADD COLUMN date_to TEXT;

CREATE INDEX IF NOT EXISTS idx_errors_run_window
ON collection_errors(run_id, query_id, area_id, date_from, date_to, resolved_at);
