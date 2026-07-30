ALTER TABLE vacancy_snapshots ADD COLUMN published_at_source_offset TEXT;
ALTER TABLE vacancy_snapshots ADD COLUMN created_at_source_offset TEXT;
ALTER TABLE vacancy_snapshots ADD COLUMN expires_at_source_offset TEXT;
