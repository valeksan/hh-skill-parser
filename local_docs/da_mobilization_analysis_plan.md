# Рабочий план: поиск, разметка и аналитика

Связанные документы: [контекст](da_mobilization_parser_context.md) ·
[master-план](da_mobilization_parser_plan.md)

Работы выполняются только по сохранённому sanitized corpus. Сеть HH для них не
используется.

## ANA-1

**Валидация поискового контура.**

- [x] Выгрузить детерминированный стратифицированный pilot на 100 вакансий и
  выполнить manual labeling.
- [x] Посчитать union precision/recall, overlap и marginal gain каждой query
  family; отдельно показать strata без достаточного числа примеров.
- [x] Добавить positive/negative fixtures для сильных, точных и неоднозначных
  выражений.
- [x] Уточнить/отключить шумные low-gain queries и выпустить новую immutable
  version `query_specs`.
- [x] Сохранить report с version выборки, label set, query specs и filters.

Приёмка:

- решение по каждой query family опирается на сохранённые labels и метрики;
- report воспроизводится из SQLite;
- изменение query specs не меняет scope уже начатого run.

Результат: `pilot create` фиксирует выборку и её query specs в SQLite, CSV
размечается через существующий `import labeling`, `pilot report` сохраняет
version выборки/labels, filters и метрики. Фактический review выполняется на
конкретной production DB; без unqueried control sample recall трактуется только
внутри query-selected pilot. `query_specs.toml` выпущен как immutable `2026-07-30`.

## ANA-2

**Качество skill discovery.**

- [x] Дополнить текущие 1–4 n-grams collocations и TF-IDF/relevance-lift
  кандидатами с минимальным document frequency.
- [x] Исключать known aliases, boilerplate, query text, employer names и
  географические названия.
- [x] В ranking учесть area/time/query-family coverage и marginal gain.
- [x] Хранить review batch, evidence/version и решения `approve|reject|merge` в
  SQLite либо immutable sidecar manifest.
- [x] Не предлагать отклонённый неизменившийся кандидат повторно.
- [x] Проверять alias conflicts и воспроизводимое полное перестроение
  `vacancy_skills` выбранной dictionary version.

Приёмка:

- golden corpus даёт стабильный порядок, frequency, lift и evidence;
- каждый approved alias имеет review provenance;
- смена dictionary version не делает сетевых запросов и не меняет manual labels.

## ANA-3

**DA-витрины и экспорты.**

- [x] Добавить views/exports для:
  - publication trends;
  - district/subject/locality;
  - employers, industries, edits и reposts;
  - topics/skills и co-occurrence;
  - salary, experience, employment и work format;
  - coverage/errors/missing-data/query noise.
- [x] Использовать effective relevance и единые filters во всех витринах.
- [x] Добавить export manifest: время, DB/schema version, run/config/query/
  dictionary versions, filters, row counts и hashes файлов.
- [x] Добавить optional Parquet с понятной подсказкой установки `pyarrow`;
  отсутствие dependency не должно ломать CSV.
- [x] Формировать legacy `top_skills_rf.csv` из SQLite, а не из отдельного
  состояния collector-а.
- [x] Добавить data dictionary и проверенные SQL examples.

Приёмка:

- все outputs строятся без transport и implicit extract;
- CSV/Parquet одного scope логически эквивалентны;
- multivalue данные имеют стабильную JSON либо нормализованную схему;
- manifest позволяет точно повторить export.

Результат: `export marts --output-dir DIR` строит CSV bundle только из SQLite;
`--parquet` добавляет эквивалентные Parquet (optional extra `.[parquet]`). Один
scope применяется к vacancy-derived marts; `coverage_errors` намеренно показывает
все persisted run-level coverage/errors. `manifest.json` содержит scope, миграции,
связанные run configs/hashes, dictionary versions, row counts и SHA-256 файлов.
Multivalue `work_format_json` остаётся стабильным JSON; industries и skills
нормализованы. `top_skills_rf.csv` имеет legacy колонки `Count,Skill`.

## ANA-4

**Качество производных признаков.**

- [x] Добавить golden проверки time/geography/employer/salary/topic/skill slices.
- [x] Проверить monthly RUB только при известной frequency/rate; неизвестное не
  конвертировать предположением.
- [x] Добавить явные missing-data reasons для каждого поля, используемого в DA.
- [x] Оценить manual-label ошибки relevance extractor после pilot и версионировать
  согласованные изменения правил.

Приёмка:

- каждое автоматическое решение имеет version, reasons и evidence;
- пропуск отличается от нулевого значения;
- повторная extraction идемпотентна.

Результат: `features` v5 сохраняет availability reason для publication, salary
midpoint и monthly RUB; monthly RUB допускается только для RUB/RUR с явно
месячной frequency (FX rate не предполагается). Normalization сохраняет status
и missing reason всех полей DA; `export marts` выдаёт `missing_data.csv` по
полю и причине. `pilot report` выдаёт versioned confusion/evidence auto vs
manual labels. Golden fixture покрывает time/geography/employer/salary/topic/
skill marts и повторный features extract.
