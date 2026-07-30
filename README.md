# HH Mobilization Skills Research

Сбор и анализ вакансий, связанных с мобилизационной подготовкой, воинским
учётом, бронированием, ГО/ЧС и режимно-секретной работой.

Основной процесс CLI-пакета `hh-skill-parser`: публичный JSON API HH.ru →
SQLite → офлайн-обработка → экспорт. SQLite — источник истины.

## Архитектура

![Архитектура: источники HH.ru → сбор → SQLite → офлайн-обработка → аналитика и эксплуатация](docs/images/architecture.png)

Сеть нужна только для `areas sync`, `collect`, `resume` и `retry`. Все
извлечения, проверка результатов, экспорт, статистика и обслуживание БД работают
из SQLite.

## Установка

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Для детерминированной локальной проверки без сети:

```bash
python -m unittest discover -s tests -p 'test_*.py'
# Та же команда через Makefile:
make test
```

## Запуск

```bash
# Сначала сохранить версионируемый каталог регионов.
hh-skill-parser areas sync

# Новая коллекция с SQLite. --area можно повторять.
# Первый инкрементальный интервал задаёт нижнюю границу; без него собираются вакансии только за текущий день.
# Следующие запуски используют совместимую контрольную дату с перекрытием в один день.
hh-skill-parser collect --area 1 --area 2 --area 13 --area 7 --date-from 2026-01-01

# Либо использовать проверяемый файл HH area ID.
hh-skill-parser areas validate --areas-file areas.txt
hh-skill-parser collect --areas-file areas.txt

# HTML — отдельный явно выбранный источник, без автоматического перехода от API к HTML.
# Каталог областей предварительно синхронизируется через API.
hh-skill-parser collect --source html --area 1 --date-from 2026-01-01

# Продолжение того же запуска после сбоя.
hh-skill-parser resume --run-id RUN_ID

# `full` — отдельный исторический интервал; старые snapshots не удаляет.
hh-skill-parser collect --collection-mode full --area 1 --date-from 2025-01-01 --date-to 2025-12-31

# Производные данные строятся отдельно, только из SQLite, без запросов к HH.
hh-skill-parser extract relevance
hh-skill-parser extract --snapshot all features
hh-skill-parser extract --skills-file skills_whitelist.txt skills

# Аналитический CSV только из SQLite. Сеть/extract не запускаются.
hh-skill-parser export vacancies --output vacancies.csv --snapshot latest --relevance relevant
hh-skill-parser export skills --output vacancy_skills.csv
hh-skill-parser export marts --output-dir marts --relevance relevant

# ANA-1: фиксированная выборка из 100 строк, затем вручную заполнить labels и импортировать CSV.
hh-skill-parser pilot create --batch-id 2026-07-30-v1 --output pilot.csv
hh-skill-parser import labeling pilot.csv
hh-skill-parser pilot report --batch-id 2026-07-30-v1 --output pilot-report.json

# Счётчики JSON по той же офлайн-выборке.
hh-skill-parser stats --snapshot latest --query-family military
```

Идентификатор запуска и счётчики печатаются в JSON. Область, страницы, найденные
вакансии, snapshots и ошибки сохраняются в SQLite; повторный запуск не требует
удаления progress-файлов. Watermark продвигается только после полного успешного
инкрементального запуска; degraded run оставляет его прежним. `full` не очищает
history и не меняет incremental watermark.
`extract` по умолчанию обрабатывает latest snapshot каждой вакансии. Фильтры
`--run-id`, `--area`, `--source`, `--date-from/--date-to` ограничивают выборку.
Автоматические labels/features/skills не выполняются при `collect`/`resume`.
`export vacancies` поддерживает `--run-id`, `--area`, `--relevance`,
`--query-family`, `--date-from/--date-to`; multivalue fields сохранены JSON-строками.
`stats` использует те же фильтры, возвращает counts по relevance/source без сети.
`pilot create` фиксирует выбранные snapshot IDs, filters и query specs в SQLite;
`pilot report` пересчитывает union precision/recall-in-pilot, overlap и marginal gain
только по сохранённому batch и manual labels. `unknown`/blank не входят в binary metrics.
HTML anti-bot/interstitial страница сохраняется как ошибка run, не как вакансия.

## Первый запуск, возобновление и coverage

`areas sync` требует сети и создаёт versioned catalog. После этого выберите
explicit IDs либо catalog selection; collect сохраняет query specs, areas и
catalog version в run. Первый `incremental` использует `--date-from` как нижнюю
границу (либо только today); успешный run двигает watermark. Повторный
incremental читает compatible watermark с overlap. `full` требует обе даты,
историю не удаляет и watermark не меняет.

Если процесс оборвался, не запускайте новый collect для того же scope:

```bash
hh-skill-parser resume --run-id 42
hh-skill-parser coverage --run-id 42
hh-skill-parser retry --run-id 42 --max-attempts 3
hh-skill-parser coverage --run-id 42
```

`coverage` читает сохранённые единицы поиска и загрузки карточек, сеть не
вызывает. `completed` означает обработанные единицы, не полноту всех результатов
HH: API pagination ограничена `--max-pages`, насыщенные интервалы режутся до
`--date-slice-min-days`, а failed/saturated/missing cards остаются видимыми в
coverage и errors. `degraded`/partial run нельзя считать полным корпусом.

## Офлайн-переобработка и ручная проверка

Все эти команды читают SQLite, не меняют collection counters/status и не ходят
в HH. После изменения extractor, dictionary или ручных labels можно безопасно
повторить их на том же наборе данных:

```bash
hh-skill-parser extract relevance
hh-skill-parser extract --snapshot all features
hh-skill-parser extract --skills-file skills_whitelist.txt skills

# Ручная проверка релевантности.
hh-skill-parser export labeling --output labels.csv --sample-size 100 --sample-seed 20260730
# Заполните только поддерживаемые колонки label/reason, затем:
hh-skill-parser import labeling labels.csv

# Фиксированная выборка: выборка и filters сохраняются в SQLite; report воспроизводим.
hh-skill-parser pilot create --batch-id 2026-07-30-v1 --output pilot.csv
hh-skill-parser import labeling pilot.csv
hh-skill-parser pilot report --batch-id 2026-07-30-v1 --output pilot-report.json
```

Поиск новых навыков начинается с ручной проверки: команда экспортирует evidence
и никогда не меняет текущий dictionary. Решения `approve|reject|merge` создают
только новый dictionary.

```bash
hh-skill-parser discover skills --batch-id review-2026-07 --output skill_candidates.csv
hh-skill-parser import skill-candidates skill_candidates.csv \
  --skills-file skills_whitelist.txt --output skills_whitelist.v2.txt --batch-id review-2026-07
hh-skill-parser extract --skills-file skills_whitelist.v2.txt skills
```

## DA-витрины, manifest и словарь данных

`export marts` создаёт набор CSV только из SQLite. `manifest.json` фиксирует
время генерации, путь к БД, migrations, filters, contributing runs/config hashes,
dictionary versions, row counts и SHA-256 каждого output. Сохраняйте manifest
вместе с отчётом; повторяйте export с теми же filters для сравнения.

```bash
# Relevant latest snapshots для одного запуска, региона и query family.
hh-skill-parser export marts --output-dir marts-42 --run-id 42 --area 1 \
  --relevance relevant --query-family military

# Примеры SQL-запросов к SQLite DB.
sqlite3 hh_mobilization.sqlite3 \
  "SELECT publication_day, vacancy_count FROM publication_time_series ORDER BY publication_day;"
sqlite3 hh_mobilization.sqlite3 \
  "SELECT q.query_group, COUNT(DISTINCT h.vacancy_hh_id) AS hits FROM vacancy_query_hits h JOIN search_queries q ON q.id=h.query_id GROUP BY q.query_group ORDER BY hits DESC;"
```

| Объект | Назначение |
| --- | --- |
| `collection_runs` | Зафиксированный scope сбора, status, counters и config hash. |
| `vacancy_query_hits` | Попадание поиска до загрузки карточки; evidence для recall/coverage. |
| `vacancy_snapshots` | Redacted normalized versions вакансии; latest view выбирает текущую. |
| `effective_relevance_labels` | Manual label при наличии, иначе automatic label. |
| `vacancy_skills` / `vacancy_skill_matrix` | Evidence dictionary/extractor для нормализованных skills. |
| `vacancy_history`, `repost_groups` | Наблюдаемые edits, archive state, potential repost grouping. |
| Reporting views / marts CSV | Производные reporting datasets, не collection source. |

Marts включают динамику публикаций, географию, работодателей, отрасли, темы и
skills, совместную встречаемость skills, зарплаты, занятость, edits, reposts,
пропуски данных, query noise и сохранённые coverage errors.
`top_skills_rf.csv` в наборе marts — SQLite-derived compatibility export.

Регулярный запуск: incremental ежедневно/по расписанию внешним scheduler; full —
отдельно, периодически, с явным historical range. Встроенного scheduler нет:

```bash
hh-skill-parser collect --area 1 --area 2 --date-from 2026-01-01
hh-skill-parser collect --collection-mode full --area 1 --date-from 2026-01-01 --date-to 2026-06-30
```

## Конфигурация CLI с SQLite

Скопируйте `config.example.toml` в `config.toml`. Реальный token в example не
хранится. Приоритет настроек: явный CLI argument → environment → TOML → built-in
default. `HH_ACCESS_TOKEN` перекрывает `[hh].access_token`; token редактируется
перед записью run config и не попадает в SQLite, JSON output или errors.

```bash
cp config.example.toml config.toml
hh-skill-parser collect --config config.toml --areas-file areas.txt
```

`areas.txt` содержит ровно один HH area ID на строку. Пустые строки, строки с
`#` и inline comment разрешены. Пример — `areas.example.txt`. Перед первым
сбором каталог должен быть сохранён через `areas sync`; collection фиксирует
его version в run и `resume` не обновляет scope.

## Очистка raw payload

Сканированные snapshots, hits, labels, features и exports команда не удаляет.
Отдельная maintenance-команда удаляет только compressed raw BLOB старше границы.
Первый вызов всегда preview:

```bash
hh-skill-parser maintenance --database hh_mobilization.sqlite3 purge-raw --before 2025-01-01

# Необратимо удалить только показанные raw BLOB.
hh-skill-parser maintenance --database hh_mobilization.sqlite3 purge-raw --before 2025-01-01 \
  --execute --confirm PURGE_RAW_PAYLOADS
```

Проверка одного уже redacted raw payload не печатает BLOB в терминал или export:

```bash
hh-skill-parser maintenance --database hh_mobilization.sqlite3 inspect-raw \
  --snapshot-id 42 --output snapshot-42.raw
```

Рост raw оценивается до purge через preview (`raw_bytes`) или локально:

```bash
sqlite3 hh_mobilization.sqlite3 'SELECT COUNT(*), COALESCE(SUM(raw_size), 0) FROM vacancy_snapshots WHERE raw_payload IS NOT NULL;'
```

## Backup и recovery

Backup делает WAL checkpoint, SQLite-consistent copy и `integrity_check`. Backup
и restore не перезаписывают файл по умолчанию. Restore всегда идёт в отдельный
путь и проверяется до/после копирования:

```bash
hh-skill-parser db --database hh_mobilization.sqlite3 backup --output backups/hh-2026-07-30.sqlite3
hh-skill-parser db restore --input backups/hh-2026-07-30.sqlite3 --output restored/hh.sqlite3
hh-skill-parser db --database restored/hh.sqlite3 check
```

`--overwrite` у `db restore` заменяет уже существующий restore output; исходный
backup не меняется.

## Live smoke

Обычный test suite не вызывает HH. Только явный opt-in smoke делает один API
search (`per_page=1`), timeout максимум 10 секунд, retries выключены, SQLite не
меняется. JSON содержит `completed` либо `degraded/partial`, без token:

```bash
hh-skill-parser smoke live --confirm-live --area 1 --request-timeout 5
```

## Полный новый скан

Обычный `collect` никогда не очищает прошлые результаты. Для полного старта с
чистой DB используйте отдельную команду. Без `--yes` она только покажет таблицы
и число строк:

```bash
hh-skill-parser db --database hh_mobilization.sqlite3 reset

# Необратимо очистить collected/derived data. Schema и migrations сохранятся.
hh-skill-parser db --database hh_mobilization.sqlite3 reset --yes

# Проверить SQLite без изменения data.
hh-skill-parser db --database hh_mobilization.sqlite3 check
```

## Входные файлы

### `query_specs.toml`

Default DB-backed collector uses versioned query specs. Each query keeps HH
expression unchanged and explicitly selects `name`, `description`, or both.
This makes exact title roles and broad thematic corpus separate, without forced
outer quotes.

```toml
version = "2026-07-30"

[[query]]
id = "strong-mobilization"
group = "strong_markers"
expression = "мобилизац*"
search_fields = ["name", "description"]
purpose = "broad corpus"
```

Текущий набор разделён на три контура:

- прямые мобилизационные и военно-учётные роли;
- гражданская оборона и чрезвычайные ситуации;
- первый отдел и режимно-секретная работа.

Изменение `query_specs.toml` относится только к новому run; существующий run сохраняет
свой frozen scope и возобновляется через `resume --run-id`.

### `skills_whitelist.txt`

Содержит навыки, извлекаемые из описаний вакансий. Поддерживает alias-группы:

```text
воинский учет | военный учет | ведение воинского учета
```

Первый элемент — каноническое название в итоговом CSV. Все варианты после `|` считаются тем же навыком; в одной вакансии он учитывается один раз. Одиночные строки также допустимы.

Alias объединяет только варианты одного навыка. Смежные сущности, например воинский учёт и бронирование, остаются разными строками статистики.

## Результаты

- `hh_mobilization.sqlite3` — source of truth для DB-backed сбора;
- `marts/` — воспроизводимый DA bundle с `manifest.json`;
- `marts/top_skills_rf.csv` — SQLite-derived compatibility export навыков.

## Фильтрация

DB-backed collector сохраняет каждый query hit до загрузки карточки и не делает
title-only rejection. Relevance/features/skills вычисляются отдельными offline
командами.

## Сеть и ограничения HH

DB-backed collector uses public `https://api.hh.ru` JSON API with one
`HH-User-Agent`, optional Bearer token, gzip and bounded transient retries.
Invalid token is removed after one auth event; run continues unauthenticated.

Сорок запросов по десяти зонам — длительный сбор. Для продолжения используйте
`hh-skill-parser resume --run-id RUN_ID`.

## Лицензия

MIT. См. [LICENSE](LICENSE).
