# DA marts: dictionary

`hh-skill-parser export --database DB marts --output-dir OUT` создаёт offline
bundle. Все vacancy-derived CSV используют один scope (`--snapshot`, `--run-id`,
`--area`, `--relevance`, `--query-family`, date range) и `effective_label`
(manual label при наличии, иначе auto label).

- `publication_trends`: publication day × relevance.
- `geography`: district/subject/locality/area × relevance.
- `employers`, `industries`, `salary`, `employment`: standard DA dimensions.
- `topics_skills`, `skill_cooccurrence`: normalized skill evidence and pairs.
- `edits`, `reposts`: snapshot history and repost-key publications.
- `missing_data`: NULL counts; zero never means missing.
- `query_noise`: hits with labels from selected scope.
- `coverage_errors`: persisted collection run counters/errors; run-level, not
  vacancy-scoped.
- `top_skills_rf`: legacy `Count,Skill`, generated from `vacancy_skills`.

`manifest.json` is reproduction contract: timestamp, DB location, applied schema
migrations, scope, involved frozen run config/hash, dictionary versions, rows and
SHA-256 per file. `--parquet` needs `pip install -e '.[parquet]'`.

Examples:

```sql
SELECT publication_day, SUM(vacancy_count) FROM publication_time_series
GROUP BY publication_day ORDER BY publication_day;

SELECT skill, COUNT(DISTINCT vacancy_hh_id) FROM vacancy_skill_matrix
GROUP BY skill ORDER BY 2 DESC;
```
