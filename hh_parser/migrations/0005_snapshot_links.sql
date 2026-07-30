CREATE TABLE IF NOT EXISTS snapshot_key_skills (
    snapshot_id INTEGER NOT NULL REFERENCES vacancy_snapshots(id),
    skill_name TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, skill_name)
);

CREATE TABLE IF NOT EXISTS snapshot_roles (
    snapshot_id INTEGER NOT NULL REFERENCES vacancy_snapshots(id),
    role_id TEXT,
    role_name TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, role_name)
);

CREATE TABLE IF NOT EXISTS snapshot_industries (
    snapshot_id INTEGER NOT NULL REFERENCES vacancy_snapshots(id),
    industry_id TEXT,
    industry_name TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, industry_name)
);

CREATE INDEX IF NOT EXISTS idx_snapshot_key_skills_name ON snapshot_key_skills(skill_name);
CREATE INDEX IF NOT EXISTS idx_snapshot_roles_id ON snapshot_roles(role_id);
CREATE INDEX IF NOT EXISTS idx_snapshot_industries_id ON snapshot_industries(industry_id);
