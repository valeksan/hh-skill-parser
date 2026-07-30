CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    topic_family TEXT,
    dictionary_version TEXT NOT NULL,
    UNIQUE (canonical_name, dictionary_version)
);

CREATE TABLE IF NOT EXISTS skill_aliases (
    id INTEGER PRIMARY KEY,
    skill_id INTEGER NOT NULL REFERENCES skills(id),
    alias_normalized TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS vacancy_skills (
    snapshot_id INTEGER NOT NULL REFERENCES vacancy_snapshots(id),
    skill_id INTEGER NOT NULL REFERENCES skills(id),
    source TEXT NOT NULL CHECK (source IN ('title', 'description', 'key_skill')),
    matched_alias TEXT NOT NULL,
    match_count INTEGER NOT NULL CHECK (match_count > 0),
    evidence_json TEXT NOT NULL DEFAULT '[]',
    extractor_version TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, skill_id, source, matched_alias, extractor_version)
);

CREATE INDEX IF NOT EXISTS idx_vacancy_skills_skill ON vacancy_skills(skill_id);
