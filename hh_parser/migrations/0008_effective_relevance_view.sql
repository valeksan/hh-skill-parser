CREATE VIEW IF NOT EXISTS effective_relevance_labels AS
SELECT
    snapshot_id,
    label AS auto_label,
    score AS auto_score,
    reasons_json AS auto_reasons_json,
    classifier_version,
    calculated_at,
    manual_label,
    manual_reason,
    manual_labeled_at,
    COALESCE(manual_label, label) AS effective_label,
    CASE WHEN manual_label IS NOT NULL THEN manual_reason ELSE reasons_json END AS effective_reason
FROM relevance_labels;
