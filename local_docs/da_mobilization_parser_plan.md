# План развития HH Mobilization Parser

Актуально на: 2026-07-30
Проверено по: `f96f851` + текущий worktree

Этот файл — короткий индекс оставшихся работ. Принятые решения и неизменяемые
ограничения находятся в [контексте проекта](da_mobilization_parser_context.md).
Детали и критерии приёмки вынесены в рабочие планы:

- [сбор и качество исходных данных](da_mobilization_collection_plan.md);
- [поиск, разметка и аналитика](da_mobilization_analysis_plan.md);
- [тестирование, эксплуатация и документация](da_mobilization_release_plan.md).

## Текущее состояние

Уже есть DB-first CLI, SQLite migrations, versioned area catalog, frozen scope и
resume, API pagination, разбиение насыщенных дат, сохранение hits до карточек,
нормализация и redaction, история snapshots, offline extractors, labeling,
skill discovery/review, FTS, CSV exports, stats и безопасные DB maintenance
команды. На текущем состоянии проходят 91 тест:

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

Это baseline, а не список задач. Подробную историю реализованного поддерживать в
планах больше не нужно: источником истины для неё служат код, migrations, тесты и
git history.

## Очерёдность работ

1. **P0 — надёжность корпуса**
   - [x] [COL-1](da_mobilization_collection_plan.md#col-1):
     реализовать реальную семантику `incremental`/`full`.
   - [x] [COL-2](da_mobilization_collection_plan.md#col-2):
     закрыть измеримость coverage и повторные попытки.
   - [x] [COL-3](da_mobilization_collection_plan.md#col-3):
     довести явный HTML-источник до общего snapshot contract.
   - [x] [COL-4](da_mobilization_collection_plan.md#col-4):
     завершить privacy/raw hardening.
   - [x] [COL-5](da_mobilization_collection_plan.md#col-5):
     формализовать edits/reposts в истории вакансии.

2. **P1 — доказуемое качество поиска и аналитики**
   - [x] [ANA-1](da_mobilization_analysis_plan.md#ana-1):
     провести pilot-разметку и измерить качество query families.
   - [x] [ANA-2](da_mobilization_analysis_plan.md#ana-2):
     улучшить discovery и сохранять историю review.
   - [x] [ANA-3](da_mobilization_analysis_plan.md#ana-3):
     добавить недостающие DA-витрины и воспроизводимые экспорты.
   - [x] [ANA-4](da_mobilization_analysis_plan.md#ana-4):
     закрепить качество производных признаков golden-проверками.

3. **P2 — готовность к регулярным запускам**
   - [ ] [REL-1](da_mobilization_release_plan.md#rel-1):
     закрыть сквозные/golden сценарии.
   - [ ] [REL-2](da_mobilization_release_plan.md#rel-2):
     добавить backup/restore и контролируемый live smoke.
   - [ ] [REL-3](da_mobilization_release_plan.md#rel-3):
     синхронизировать документацию с финальным workflow.

## Ближайшая итерация

Рекомендуемый следующий вертикальный срез:

1. `COL-1` — watermark + overlap для incremental run;
2. `COL-2` — coverage report и retry unresolved units;
3. `ANA-1` — pilot на 100 вакансий и решение по шумным query families;
4. соответствующие части `REL-1`.

Итерация завершена, когда результат воспроизводится из SQLite, имеет тесты и не
требует повторных HH-запросов для пересчёта производных данных.

## Общее Definition of Done

Задача считается завершённой, если:

- поведение доступно через package CLI и документировано в `--help`;
- schema/config/CLI изменения валидируются до сетевого вызова;
- повторный запуск или `resume` не создаёт логических дублей;
- ошибки и неполное покрытие явно сохранены, а не выданы за успех;
- секреты, контакты и точная география не попадают в SQLite, логи или exports;
- offline-команды не обращаются к HH и не меняют collection state;
- добавлены fixture-тесты, а весь локальный suite проходит.

## Не входит в текущий план

- гарантированно полная историческая база удалённых вакансий HH;
- scheduler внутри приложения;
- PostgreSQL, DuckDB и notebook до появления подтверждённой потребности;
- ML/LLM-классификация, embeddings и внешняя отправка текста вакансий;
- автоматическое признание навыком без сохранённого evidence и ручного review.
