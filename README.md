# HH Mobilization Skills Research

Ветка `da-mob`: сбор и анализ навыков из вакансий, связанных с мобилизационной подготовкой, воинским учётом, бронированием, ГО/ЧС и режимно-секретной работой.

Основной сбор выполняется через публичный JSON API HH.ru и сохраняется в SQLite.
Legacy `parse_skills.py` остаётся для одиночного CSV-режима.

## Установка

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Для быстрой локальной проверки:

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

## Запуск

```bash
# Сначала сохранить versioned каталог регионов.
hh-skill-parser areas sync

# Новая DB-backed коллекция. --area можно повторять.
# Первый incremental range задаёт нижнюю границу; без неё собирается только today.
# Последующие идут от compatible watermark с overlap (default: 1 day).
hh-skill-parser collect --area 1 --area 2 --area 13 --area 7 --date-from 2026-01-01

# Либо использовать проверяемый файл HH area ID.
hh-skill-parser areas validate --areas-file areas.txt
hh-skill-parser collect --areas-file areas.txt

# HTML — отдельный явный source, без API→HTML fallback.
# Каталог областей предварительно синхронизируется через API.
hh-skill-parser collect --source html --area 1 --date-from 2026-01-01

# Продолжение того же run после сбоя.
hh-skill-parser resume --run-id RUN_ID

# Full — отдельный historical range; старые snapshots не удаляет.
hh-skill-parser collect --collection-mode full --area 1 --date-from 2025-01-01 --date-to 2025-12-31

# Производные данные строятся отдельно, только из SQLite, без запросов к HH.
hh-skill-parser extract relevance
hh-skill-parser extract features --snapshot all
hh-skill-parser extract --skills-file skills_whitelist.txt skills

# Аналитический CSV только из SQLite. Сеть/extract не запускаются.
hh-skill-parser export vacancies --output vacancies.csv --snapshot latest --relevance relevant
hh-skill-parser export skills --output vacancy_skills.csv

# ANA-1: fixed 100-row pilot, then manually fill labels and import CSV.
hh-skill-parser pilot create --batch-id 2026-07-30-v1 --output pilot.csv
hh-skill-parser import labeling pilot.csv
hh-skill-parser pilot report --batch-id 2026-07-30-v1 --output pilot-report.json

# JSON counts по той же offline выборке.
hh-skill-parser stats --snapshot latest --query-family military
```

Run ID и счётчики печатаются в JSON. Область, страницы, hits, snapshots и ошибки
сохраняются в SQLite; повторный запуск не требует удаления progress-файлов.
Watermark продвигается только после полного successful incremental run; degraded
run оставляет его прежним. `full` не очищает history и не меняет incremental
watermark.
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

Регулярный запуск: incremental ежедневно/по расписанию внешним scheduler; full —
отдельно, периодически, с явным historical range. Встроенного scheduler нет:

```bash
hh-skill-parser collect --area 1 --area 2 --date-from 2026-01-01
hh-skill-parser collect --collection-mode full --area 1 --date-from 2026-01-01 --date-to 2026-06-30
```

## Конфигурация DB-backed CLI

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

Skill discovery review создаёт новый dictionary file, исходный не меняет:

```bash
hh-skill-parser discover skills --batch-id review-2026-07 --output skill_candidates.csv
# Заполнить decision: approve|reject|merge, затем:
hh-skill-parser import skill-candidates skill_candidates.csv \
  --skills-file skills_whitelist.txt --output skills_whitelist.v2.txt --batch-id review-2026-07
```

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

## Команды `parse_skills.py`

```bash
# Показать справку; пустой вызов делает то же самое
python parse_skills.py help

# Обычный одиночный сбор
python parse_skills.py run --source html --mode description

# Построить PNG из уже собранной общей статистики, без сетевого сбора
python parse_skills.py chart --chart-input top_skills_rf.csv -o top_skills_rf.png
```

`chart` не меняет CSV и не обращается к HH. Для него нужен `matplotlib`:

```bash
pip install -e ".[chart]"
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

`queries.txt` remains supported for compatibility: one expression per line,
searched with HH default fields.

### `queries.txt`

Legacy поисковые фразы — по одной на строку. DB-backed CLI does not modify them;
старый `parse_skills.py` сохраняет свой legacy exact-title behaviour.

```text
Специалист по мобилизационной подготовке
Воинский учет
Специалист по ГО и ЧС
```

Текущий набор разделён на три контура:

- прямые мобилизационные и военно-учётные роли;
- гражданская оборона и чрезвычайные ситуации;
- первый отдел и режимно-секретная работа.

Изменение `queries.txt` относится только к новому run; существующий run сохраняет
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
- `top_skills_rf.csv` — legacy CSV-экспорт;
- `top_skills_rf.png` — график, созданный командой `chart` из итогового CSV;

## Фильтрация

DB-backed collector сохраняет каждый query hit до загрузки карточки и не делает
title-only rejection. Relevance/features/skills вычисляются отдельными offline
командами. Описанный ниже title filter относится только к legacy
`parse_skills.py`.

## Сеть и ограничения HH

DB-backed collector uses public `https://api.hh.ru` JSON API with one
`HH-User-Agent`, optional Bearer token, gzip and bounded transient retries.
Invalid token is removed after one auth event; run continues unauthenticated.
Legacy HTML collection may be limited by anti-bot protection and uses its own
legacy browser/proxy settings.

Сорок запросов по десяти зонам — длительный сбор. Для продолжения используйте
`hh-skill-parser resume --run-id RUN_ID`.

## Лицензия

MIT. См. [LICENSE](LICENSE).
