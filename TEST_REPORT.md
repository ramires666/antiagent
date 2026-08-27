# Финальный отчёт тестирования

Дата отчёта: 27 августа 2026 г.
Baseline: `cd41f44`

## Среда

- Windows 11, build `26200`.
- Python `3.13.4` из `.venv`.
- MCP `2.0.0`, Pydantic `2.13.4`, AnyIO `4.14.2`.
- Antigravity CLI (`agy`) `1.1.22`.
- Codex CLI `0.147.0`.
- Git `2.55.0.windows.4`.

## Итог

После удаления legacy-ветки deterministic suite содержит **73 теста**, включая **5 тестов MCP STDIO**. Два последовательных discovery и отдельный прогон targeted suites в обратном порядке завершились `OK`; также прошли `py_compile`, `pip check` и `git diff --check`.

Проверка не заявляет математическое 100% покрытие: тестируются reachable и критические error/lifecycle ветки.

## Что исправлено и проверено

- Безопасное разрешение абсолютных executable-путей (`agy`, `git`, системный `taskkill`), запрет shell, Windows UTF-16 command-line budget, cleanup и exact-PID process-tree termination.
- Межпроцессный workspace lock, общий deadline, восстановление после timeout/cancellation и отсутствие surviving descendants.
- MCP default `mode=plan`; typed diagnostics; runtime failures возвращаются как MCP `isError=true` при сохранении structured metadata; validation/redaction не раскрывают prompt, stdout, stderr или secrets.
- `run_id`, timestamps, duration, CLI version, retryability и completeness metadata; progress содержит run id и state.
- Git preflight/postflight для `accept-edits`: bounded status snapshot, `preexisting_dirty`, `worktree_changed`, `changed_paths`, `postflight_complete`, `requires_review`; persistent review marker и явный `acknowledge_review`; destructive rollback не выполняется.
- Codex MCP timeout настроен на `900` секунд при wrapper timeout `840` секунд.
- Удалены legacy SDK-файлы и зависимость `google-antigravity`; остался один production path через OAuth CLI.

## Реальный STDIO MCP-контур

`test_mcp_protocol.py` запускает отдельный STDIO process и настоящий `mcp.ClientSession`: initialize, `tools/list`, схемы, успешный call, unknown/missing/wrong arguments, invalid enums, non-Git cwd, progress, cancellation и последовательный call после ошибки. Fixture подменяет только CLI response, поэтому deterministic tests offline и не требуют OAuth.

## Live smoke

- Authenticated `agy` OAuth smoke в `plan`: **успешно**, контрольный marker найден в ответе, Git не изменён.
- Изолированный `accept-edits` smoke: **SUCCESS**; изменён только `README.txt`, postflight complete, полный diff просмотрен; временный test repository удалён.

Это единственная проверка, зависящая от внешней OAuth-сессии и реального CLI; она не входит в deterministic count.

## Ограничения

Postflight fingerprint использует `git status` и метаданные файлов, а не content hashes. Автоматического rollback нет: partial/unknown state оставляет `requires_review=true` и требует явного решения оператора.

## Tracked-secret scan

Финальный filename-only scan высокоуверенных форматов credentials и отдельно подозрительных tracked-имён вернул **0 файлов**. Значения секретов команда не печатает:

```powershell
$pattern = '(AIza[0-9A-Za-z_-]{35}|sk-[0-9A-Za-z_-]{20,}|gh[pousr]_[0-9A-Za-z]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----)'
git grep -Il -E $pattern -- .
git ls-files | Select-String -Pattern '(^|/)(\.env($|\.)|.*\.(pem|key|p12|pfx|log)$|credentials?($|[._-]))'
```

## Воспроизведение

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe -m unittest discover -q
.\.venv\Scripts\python.exe -m unittest -v test_mcp_protocol.py test_agy_server.py
.\.venv\Scripts\python.exe -m unittest -v test_agy_server.py test_mcp_protocol.py
.\.venv\Scripts\python.exe -m py_compile agy_server.py smoke_agy.py test_agy_server.py test_mcp_protocol.py _mcp_protocol_fixture.py
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

Legacy `server.py`, `smoke_antigravity.py` и `test_server.py` намеренно отсутствуют и не должны включаться в команды.
