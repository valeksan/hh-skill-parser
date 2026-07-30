# Рабочий план: тестирование, эксплуатация и документация

Связанные документы: [контекст](da_mobilization_parser_context.md) ·
[master-план](da_mobilization_parser_plan.md)

## REL-1

**Сквозные и golden тесты.**

- [ ] Добавить fixture-only E2E:
  `areas → collect → interrupt → resume → extract → label → export → stats`.
- [ ] Проверить, что collection работает без skill dictionary и optional
  analytical dependencies.
- [ ] Добавить golden discovery/import/re-extract и DA-query scenarios.
- [ ] Покрыть malformed API/HTML, missing fields, anonymous employer, no salary,
  archived vacancy, timezone offsets и privacy markers.
- [ ] Проверить, что offline commands делают zero transport calls и не меняют
  collection status/counters.
- [ ] Разделить разросшийся `tests/test_smoke.py` по компонентам после фиксации
  E2E, не снижая coverage.

Приёмка:

- локальный suite детерминирован и не требует сети;
- повторный E2E даёт те же логические rows и не создаёт дублей;
- ошибки тестируются вместе с их persisted coverage/status.

## REL-2

**Эксплуатация.**

- [x] Добавить согласованный SQLite backup workflow с WAL checkpoint и проверкой
  восстановленной копии.
- [x] Документировать оценку роста raw payload и безопасную purge/restore
  процедуру.
- [x] Добавить opt-in live smoke для HH API: малый scope, строгий timeout,
  отсутствие запуска в обычном test suite.
- [x] Проверять совместимость schema до collect/resume/export и выдавать
  actionable error.
- [x] Зафиксировать рекомендуемые команды регулярного incremental и
  периодического full run без встроенного scheduler.

Приёмка:

- backup восстанавливается в отдельный файл и проходит `db check`;
- live smoke не пишет секреты и явно сообщает partial/degraded result;
- destructive maintenance по умолчанию работает только в preview.

## REL-3

**Пользовательская документация.**

- [x] Синхронизировать README, `config.example.toml`, Makefile и `--help` со всеми
  завершёнными COL/ANA задачами.
- [x] Исправить локальную test-команду на реально работающий discovery-вызов.
- [x] Описать первый запуск, resume, offline reprocessing, labeling review,
  discovery review, export manifest и recovery.
- [x] Добавить data dictionary, примеры DA-запросов и объяснение coverage limits.
- [x] Явно разделить package DB workflow и legacy `parse_skills.py`; не смешивать
  их source/fallback semantics.
- [x] После SQLite-derived compatibility export удалить legacy pipeline;
  потребители используют `marts/top_skills_rf.csv` и manifest.

Приёмка:

- команды из документации выполняются на чистой fixture DB;
- ни один example не требует реального token;
- ограничения полноты и privacy сформулированы без скрытых допущений.
