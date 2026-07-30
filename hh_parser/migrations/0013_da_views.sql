CREATE VIEW IF NOT EXISTS latest_vacancy_snapshots AS
SELECT s.*
FROM vacancy_snapshots s
WHERE NOT EXISTS (
    SELECT 1
    FROM vacancy_snapshots newer
    WHERE newer.vacancy_hh_id = s.vacancy_hh_id
      AND (newer.last_seen_at > s.last_seen_at
           OR (newer.last_seen_at = s.last_seen_at AND newer.id > s.id))
);

CREATE VIEW IF NOT EXISTS relevant_vacancies AS
SELECT s.*, r.effective_label, r.effective_reason
FROM latest_vacancy_snapshots s
JOIN effective_relevance_labels r ON r.snapshot_id = s.id
WHERE r.effective_label = 'relevant';

CREATE VIEW IF NOT EXISTS vacancy_skill_matrix AS
SELECT
    s.id AS snapshot_id,
    s.vacancy_hh_id,
    s.title,
    s.published_at,
    s.area_id,
    s.area_name,
    skill.canonical_name AS skill,
    skill.topic_family,
    skill.dictionary_version,
    evidence.source,
    evidence.matched_alias,
    evidence.match_count,
    evidence.extractor_version
FROM latest_vacancy_snapshots s
JOIN vacancy_skills evidence ON evidence.snapshot_id = s.id
JOIN skills skill ON skill.id = evidence.skill_id;

CREATE VIEW IF NOT EXISTS publication_time_series AS
SELECT
    COALESCE(substr(published_at, 1, 10), substr(observed_at, 1, 10)) AS publication_day,
    COUNT(DISTINCT vacancy_hh_id) AS vacancy_count
FROM latest_vacancy_snapshots
GROUP BY publication_day;

CREATE VIEW IF NOT EXISTS vacancy_geography AS
SELECT
    federal_district,
    federal_subject,
    locality,
    area_id,
    area_name,
    COUNT(DISTINCT vacancy_hh_id) AS vacancy_count
FROM latest_vacancy_snapshots
GROUP BY federal_district, federal_subject, locality, area_id, area_name;

CREATE VIEW IF NOT EXISTS vacancy_employers AS
SELECT
    employer_id,
    employer_name,
    employer_type,
    COUNT(DISTINCT vacancy_hh_id) AS vacancy_count,
    COUNT(*) AS snapshot_count,
    MIN(v.first_seen_at) AS first_seen_at,
    MAX(v.last_seen_at) AS last_seen_at
FROM latest_vacancy_snapshots s
JOIN vacancies v ON v.hh_id = s.vacancy_hh_id
GROUP BY employer_id, employer_name, employer_type;

CREATE VIEW IF NOT EXISTS vacancy_salary AS
SELECT
    s.id AS snapshot_id,
    s.vacancy_hh_id,
    s.published_at,
    s.area_id,
    s.employer_id,
    s.salary_from,
    s.salary_to,
    CASE
        WHEN s.salary_from IS NOT NULL AND s.salary_to IS NOT NULL
        THEN (s.salary_from + s.salary_to) / 2.0
        ELSE COALESCE(s.salary_from, s.salary_to)
    END AS salary_midpoint,
    s.salary_currency,
    s.salary_gross,
    s.salary_frequency
FROM latest_vacancy_snapshots s;
