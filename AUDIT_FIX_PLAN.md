# План исправления Antigravity MCP

Дата аудита: 27 августа 2026 г.
Основной сервер: `agy_server.py`
Исходный эксплуатационный отчёт: `ANTIGRAVITY_MCP_REPORT.md`

## Цель

Сделать OAuth-based MCP executor предсказуемым и безопасным при работе Codex с
Gemini через `agy`: исключить перехват исполняемых файлов, преждевременные
таймауты, параллельное редактирование одного workspace, потерю диагностики и
незамеченные частичные изменения.

## Исходное состояние

- Primary-путь: Codex → MCP stdio → `antigravity_cli_execute` → `agy` → Gemini.
- `server.py` — устаревшая SDK/API-key реализация, не зарегистрированная в
  текущей конфигурации Codex.
- Штатный `unittest discover`: 59/59 PASS.
- `py_compile`, `pip check`, `git diff --check`: PASS.
- Запуск тестов в другом порядке (`test_server` перед `test_mcp_protocol`)
  воспроизводимо падает из-за загрязнения `sys.modules`.
- Установленный `agy` во время аудита автоматически обновился с 1.1.19 до
  1.1.22. Все итоговые проверки выполнять на 1.1.22 или новее с фиксацией
  фактической версии.
- Live OAuth smoke в рамках планирования не запускался.

## Подтверждённые проблемы

### P0 — безопасность и целостность данных

1. `shutil.which("agy")` на Windows может выбрать подложный `agy.exe` из
   текущего недоверенного repository. Относительные `git` и `taskkill` имеют
   аналогичный риск поиска executable.
2. В текущей Codex MCP-конфигурации отсутствует `tool_timeout_sec`; default
   клиента 60 секунд конфликтует с внутренним timeout wrapper 840 секунд.
3. `EXECUTION_LOCK` действует только внутри одного Python process. Одновременно
   работающие экземпляры MCP могут изменять один workspace параллельно.
4. Ошибки валидации MCP включают исходное `input_value` и могут вернуть клиенту
   переданный секрет.
5. CLI stderr, exit code и JSON `error` теряются; разные ошибки сворачиваются в
   `Antigravity CLI task failed`.
6. После `accept-edits` нет обязательного postflight. Частичный diff после
   error/timeout остаётся незамеченным.
7. Default режима — `accept-edits`; запись разрешается без явного выбора.

### P1 — надёжность и наблюдаемость

1. Не различаются `workspace_not_git`, `git_trust_denied`,
   `path_outside_allowed_root`, `path_not_found` и `git_unavailable`.
2. Ошибка `process.kill()` типа `OSError`/`PermissionError` может прервать
   timeout/cancellation cleanup.
3. Лимит prompt считает Python characters, а не Windows UTF-16 command-line
   units; Unicode-heavy prompt проходит validation, но не запускается.
4. Runtime failure возвращается как MCP `isError=false` с внутренним
   `status="ERROR"`.
5. Нет `run_id`, timestamps, duration, CLI version, `retryable` и признака
   полноты metadata.
6. `conversation_id=None` и `usage={}` неотличимы от корректных пустых данных.
7. Heartbeat синтетический, не содержит `run_id` и текущего состояния.
8. Обрезка результата сохраняет только начало и может удалить итоговую сводку
   и результаты тестов.

### P2 — тесты и сопровождение

1. `test_server.py` оставляет заглушки в `sys.modules`, поэтому suite зависит
   от порядка импорта.
2. Protocol suite не проверяет redaction validation errors, runtime
   `isError`, фактические defaults, alternate thinking levels и завершение
   fixture process.
3. `TEST_MATRIX.md` и `TEST_REPORT.md` относятся к старому commit.
4. Указанный в test matrix secret scan не имеет воспроизводимой команды.
5. POSIX transport проверяется только mock-тестами; реальный STDIO suite
   Windows-only.

## Решения по пунктам эксплуатационного отчёта

| Наблюдение | Решение |
|---|---|
| Non-Git workspace | Не запускать editing-agent; вернуть typed `workspace_not_git` |
| Committed repo вне process root | Сохранить security boundary; вернуть `path_outside_allowed_root` |
| Windows path mismatch | Логировать безопасный canonical path и typed preflight result |
| Missing heartbeat | Добавить `run_id` и state; не обещать отображение progress клиентом |
| `Promise.all` скрывает children | Внешний orchestration, не часть этого Python MCP |
| Partial diff | Обязательный Git preflight/postflight без автоматического rollback |
| Deterministic retry | Возвращать `retryable=false`; wrapper самостоятельно не ретраит |
| Missing conversation/usage | Явные `metadata_complete` и missing fields |
| Dirty tree | Не отклонять; отделять pre-existing changes от новых |
| 220-line shard | Не вводить искусственный лимит по числу строк |

## План реализации

### Этап 1. Зафиксировать дефекты тестами

До production-кода добавить минимальные regression tests для:

1. подложного `agy.exe`/`git.exe` в workspace;
2. утечки sentinel-secret через invalid MCP arguments;
3. runtime error с MCP `isError=true`;
4. timeout/cancellation при `process.kill()` → `OSError`;
5. UTF-16 overflow prompt;
6. двух MCP processes, конкурирующих за один workspace;
7. CLI error/timeout, оставившего partial diff;
8. clean, dirty и pre-existing dirty worktree;
9. запуска тестов в обратном порядке.

### Этап 2. Укрепить запуск процессов

Файлы: `agy_server.py`, `test_agy_server.py`.

1. Разрешать только абсолютные доверенные пути к `agy`, `git` и системному
   `taskkill`.
2. На Windows сначала использовать официальный `%LOCALAPPDATA%\agy\bin\agy.exe`;
   custom path принимать только через явную конфигурацию.
3. Не искать executable в process cwd.
4. Добавить межпроцессный lock, ключом которого является canonical workspace.
   Использовать стандартную библиотеку; ожидание lock включить в общий timeout.
5. Сделать cleanup best-effort для всех `OSError`, сохраняя повторный выброс
   `CancelledError`.
6. Проверять фактический Windows command-line UTF-16 budget после сборки argv.

### Этап 3. Исправить MCP-контракт и диагностику

Файлы: `agy_server.py`, `test_mcp_protocol.py`, `_mcp_protocol_fixture.py`.

1. Изменить default `mode` на `plan`.
2. Добавить typed preflight errors:
   `invalid_request`, `path_not_found`, `path_outside_allowed_root`,
   `workspace_not_git`, `workspace_not_root`, `git_trust_denied`,
   `git_unavailable`, `cli_unavailable`.
3. Сохранить безопасные `exit_code` и классификацию CLI status/error.
4. Не возвращать необработанный stderr. Извлекать только allowlisted тип ошибки
   после redaction.
5. Для runtime/business failures возвращать MCP `isError=true`, сохраняя
   structured metadata.
6. Добавить `run_id`, `started_at`, `finished_at`, `duration_seconds`,
   `cli_version`, `retryable`, `metadata_complete`.
7. Progress-сообщения снабдить `run_id` и состоянием `queued|running`.
8. Результат обрезать как head + marker + tail.

### Этап 4. Добавить безопасный Git postflight

Файлы: `agy_server.py`, `test_agy_server.py`.

1. До запуска снять ограниченный `git status --porcelain=v1`.
2. В `finally` повторить status независимо от SUCCESS/error/timeout/cancel.
3. Вернуть:
   `preexisting_dirty`, `worktree_changed`, `changed_paths`,
   `postflight_complete`, `requires_review`.
4. Не возвращать полный diff через MCP.
5. Не выполнять `checkout`, `reset`, `stash`, удаление или rollback.
6. При partial/unknown state выставить `requires_review=true`; следующий
   editing-вызов должен быть отклонён до явного решения оператора.

### Этап 5. Исправить конфигурацию Codex

Обновить документацию и шаблон project-scoped `.codex/config.toml`:

```toml
[mcp_servers.antigravity_cli_executor]
command = "W:/_python/antiagent/.venv/Scripts/python.exe"
args = ["W:/_python/antiagent/agy_server.py"]
cwd = "W:/_python/antiagent"
enabled = true
required = true
startup_timeout_sec = 30
tool_timeout_sec = 900
enabled_tools = ["antigravity_cli_execute"]
```

Правила:

- wrapper timeout — 840 секунд, Codex timeout — 900 секунд;
- `cwd` задавать на trusted project root или узкий разрешённый parent;
- не использовать общий `W:\` как security boundary;
- каждый результат должен содержать фактическую версию `agy`.

### Этап 6. Удалить legacy SDK-ветку

Рекомендуемый вариант:

1. удалить `server.py`, `smoke_antigravity.py`, `test_server.py`;
2. удалить старую SDK-инструкцию;
3. удалить `google-antigravity` из `requirements.txt`;
4. оставить один production path: `agy_server.py` + OAuth CLI.

Если SDK backup необходимо сохранить, вынести его в отдельный архивный проект.
Не смешивать его lifecycle, API-key и security-модель с primary executor.

### Этап 7. Обновить тесты и документацию

1. Устранить зависимость тестов от import order.
2. Добавить raw-wire проверки JSON-RPC/MCP error semantics и redaction.
3. Обновить `TEST_MATRIX.md`, `TEST_REPORT.md`, `КАК_ПОЛЬЗОВАТЬСЯ.md` и OAuth
   инструкцию.
4. Добавить воспроизводимый tracked-secret scan, выводящий только имена файлов.
5. Явно отметить Windows как основную release platform либо добавить POSIX
   STDIO integration run.
6. Обновить исходный `ANTIGRAVITY_MCP_REPORT.md` фактическими результатами.

## Финальная проверка

Порядок acceptance:

1. `unittest discover` два раза подряд;
2. suite в обратном порядке импорта;
3. `py_compile`, `pip check`, `git diff --check`;
4. secret sentinel и tracked-secret scan;
5. real MCP stdio initialize/tools/list/tools/call;
6. Git/non-Git/outside-root/clean/dirty matrix;
7. multi-process workspace lock regression;
8. timeout/cancellation и отсутствие surviving descendants;
9. partial-edit postflight matrix;
10. один явно разрешённый live OAuth smoke в `plan`;
11. после него изолированный `accept-edits` smoke с полным diff review.

## Definition of Done

## Фактический статус выполнения (baseline `cd41f44`, 27.08.2026)

Все этапы 1–7 выполнены:

- [x] дефекты зафиксированы regression-тестами;
- [x] запуск executable, UTF-16 budget, process-tree cleanup и межпроцессный lock усилены;
- [x] MCP contract, typed diagnostics, redaction, `isError`, metadata и progress исправлены;
- [x] Git preflight/postflight, persistent review marker и `acknowledge_review` реализованы;
- [x] Codex timeout настроен на 900 секунд при wrapper timeout 840 секунд;
- [x] legacy SDK path удалён, dependency и устаревшие команды убраны;
- [x] тесты и документация обновлены.

Результат после следующего persistent-manager этапа: 90 deterministic тестов, включая 6 STDIO MCP тестов; один POSIX-only permission test ожидаемо skipped на Windows. Добавлены durable SQLite lifecycle, conversation follow-up, cross-process cancellation и project-agent `antigravity_worker`. Исторический authenticated live OAuth smoke успешен; повторный managed smoke дошёл до terminal state без Git-изменений, но внешний provider вернул usage limit. Filename-only tracked-secret scan вернул 0 файлов. Ограничения: postflight использует status и file metadata без content hashes, автоматического rollback и отдельного crash-surviving daemon нет.

- Ни один executable не разрешается через недоверенный current directory.
- Codex timeout больше общего wrapper timeout.
- Одновременно один workspace изменяет не более одного MCP process.
- Validation/CLI errors не возвращают secrets и имеют typed classification.
- Любой `accept-edits` вызов заканчивается Git postflight, включая timeout и
  cancellation.
- Runtime failures имеют MCP `isError=true`.
- Missing usage/conversation metadata обозначены явно.
- Все deterministic tests проходят независимо от порядка.
- Live smoke проходит на зафиксированной версии `agy`.
- В репозитории остаётся один поддерживаемый production executor.
