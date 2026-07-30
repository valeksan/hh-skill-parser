CREATE TABLE IF NOT EXISTS snapshot_repost_keys (
    snapshot_id INTEGER PRIMARY KEY REFERENCES vacancy_snapshots(id),
    repost_key TEXT NOT NULL,
    key_version TEXT NOT NULL,
    calculated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshot_repost_keys_key
ON snapshot_repost_keys(repost_key, key_version);

CREATE VIEW IF NOT EXISTS vacancy_history AS
WITH ordered AS (
    SELECT
        s.*,
        LAG(s.id) OVER (
            PARTITION BY s.vacancy_hh_id ORDER BY s.observed_at, s.id
        ) AS previous_snapshot_id,
        LAG(COALESCE(s.archived, 0)) OVER (
            PARTITION BY s.vacancy_hh_id ORDER BY s.observed_at, s.id
        ) AS previous_archived
    FROM vacancy_snapshots s
)
SELECT
    ordered.*,
    CASE WHEN previous_snapshot_id IS NULL THEN 0 ELSE 1 END AS is_content_edit,
    CASE
        WHEN previous_snapshot_id IS NULL THEN 'initial'
        WHEN COALESCE(archived, 0) <> previous_archived THEN 'archive_state_change'
        ELSE 'content_edit'
    END AS history_event
FROM ordered;

CREATE VIEW IF NOT EXISTS repost_publications AS
SELECT
    k.repost_key,
    k.key_version,
    s.vacancy_hh_id,
    MIN(s.id) AS first_snapshot_id,
    MIN(s.observed_at) AS first_observed_at,
    COUNT(*) AS matching_snapshot_count
FROM snapshot_repost_keys k
JOIN vacancy_snapshots s ON s.id = k.snapshot_id
GROUP BY k.repost_key, k.key_version, s.vacancy_hh_id;

CREATE VIEW IF NOT EXISTS repost_groups AS
SELECT
    repost_key,
    key_version,
    COUNT(*) AS publication_count,
    MIN(first_observed_at) AS first_observed_at,
    MAX(first_observed_at) AS last_observed_at
FROM repost_publications
GROUP BY repost_key, key_version;
