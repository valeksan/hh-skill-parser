CREATE TABLE IF NOT EXISTS skill_review_batches (
    id INTEGER PRIMARY KEY,
    batch_id TEXT NOT NULL UNIQUE,
    dictionary_version TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skill_review_candidates (
    batch_id INTEGER NOT NULL REFERENCES skill_review_batches(id),
    candidate_normalized TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    candidate_json TEXT NOT NULL,
    PRIMARY KEY (batch_id, candidate_normalized)
);

CREATE TABLE IF NOT EXISTS skill_candidate_reviews (
    batch_id INTEGER NOT NULL REFERENCES skill_review_batches(id),
    candidate_normalized TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approve', 'reject', 'merge')),
    canonical_skill TEXT,
    reviewer_reason TEXT,
    reviewed_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, candidate_normalized),
    FOREIGN KEY (batch_id, candidate_normalized)
        REFERENCES skill_review_candidates(batch_id, candidate_normalized)
);

CREATE INDEX IF NOT EXISTS idx_skill_candidate_reviews_reject
ON skill_candidate_reviews(candidate_normalized, evidence_hash, decision);
