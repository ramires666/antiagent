# Финальный отчёт тестирования

Дата отчёта: 3 сентября 2026 г.
Code/config baseline: Antiagent `0.4.0`

## Среда

- Windows 11, build `26200`.
- Python `3.14.6` из `.venv`.
- MCP `2.0.0`, Pydantic `2.13.5`.
- Antigravity CLI (`agy`) `1.1.24`.
- Git `2.55.0.windows.3`.

## Итог

После исправления ошибок из отчёта 2026-09-03 deterministic suite содержит
**197 тестов**, включая **6 тестов MCP STDIO**. На Windows два POSIX-only
permission test ожидаемо отмечены `skipped`. Полный discovery завершился `OK`;
также проходят compile, packaging/smoke validation и `git diff --check`.

Проверка не заявляет математическое 100% покрытие: тестируются reachable и критические error/lifecycle ветки.

## Что исправлено и проверено

- Безопасное разрешение абсолютных executable-путей (`agy`, `git`, системный `taskkill`), запрет shell, Windows UTF-16 command-line budget, cleanup и exact-PID process-tree termination.
- Shared `plan` / exclusive `accept-edits` workspace admission, честная очередь
  с owner/position, renewable lease, writer fairness, stale cleanup и release
  после cancellation. Стресс из 32 concurrent plan дошёл до mock CLI без lock timeout.
- Пять отдельных content failure-кодов вместо неразличимого `no_content`,
  bounded structural diagnostics и retry только для двух transient plan-ошибок.
- Verification сообщает hash правила, marker mismatch/schema failure и только
  безопасный manual-review suffix. Runtime identity сверяет binary/version до и
  после запуска и fail-fast возвращает `stale_runtime_snapshot`.
- Terminal feedback elapsed/idle больше не растёт после завершения.
- MCP default `mode=plan`; typed diagnostics; runtime failures возвращаются как MCP `isError=true` при сохранении structured metadata; validation/redaction не раскрывают prompt, stdout, stderr или secrets.
- `agy --output-format stream-json` читается во время выполнения; model text delta не сохраняется, наружу идут только allowlisted step index/state/type и ограниченный final result.
- `run_id`, timestamps, duration, CLI version, retryability и completeness metadata; progress использует шкалу wrapper-этапов `0..100`, heartbeat, blocker, next action, elapsed/idle и manager status без выдуманного Gemini ETA.
- Git preflight/postflight для `accept-edits`: bounded status snapshot, `preexisting_dirty`, `worktree_changed`, `changed_paths`, `postflight_complete`, `requires_review`; persistent review marker и явный `acknowledge_review`; destructive rollback не выполняется.
- Codex MCP timeout настроен на `900` секунд при wrapper timeout `840` секунд.
- Удалены legacy SDK-файлы и зависимость `google-antigravity`; остался один production path через OAuth CLI.
- Добавлен stdlib SQLite `AgentStore`: additive migration `progress_json`, persistence между process instances, условные transitions, terminal immutability, heartbeat-aware stale reconciliation, capacity 32, terminal history 1000 и output limit 256 KiB; prompt/context/verification не сохраняются.
- Recent activity ограничена 16 allowlisted событиями, последовательные heartbeat coalesce; произвольные code/step/next-action и raw stdout/stderr в telemetry не попадают.
- Добавлены lifecycle tools `spawn/list/status/wait/followup/interrupt`; follow-up использует валидированный UUID `--conversation`, wait ограничен 60 секундами на call, interrupt работает через общий SQLite cancel flag между store/process instances.
- Добавлен project-scoped `.codex/agents/antigravity_worker.toml` и обязательное правило cost-first routing в `AGENTS.md`; Codex остаётся владельцем UI/lifecycle, review и тестов.

## Реальный STDIO MCP-контур

`test_mcp_protocol.py` запускает отдельный STDIO process и настоящий `mcp.ClientSession`: initialize, восемь tools в `tools/list`, схемы, synchronous call, полный lifecycle `spawn/wait/followup/list/interrupt`, unknown/missing/wrong arguments, redaction, non-Git cwd, progress `0..100`, cancellation и последовательный call после ошибки. Fixture подменяет только CLI response и использует отдельную временную SQLite БД, поэтому deterministic tests offline и не требуют OAuth.

## Live smoke

- Authenticated `agy` OAuth smoke в `plan`: **успешно**, контрольный marker найден в ответе, Git не изменён.
- Изолированный `accept-edits` smoke: **SUCCESS**; изменён только `README.txt`, postflight complete, полный diff просмотрен; временный test repository удалён.
- Новый managed lifecycle smoke был повторно запущен после реализации manager: lifecycle дошёл до terminal `failed`, Git остался неизменным; внешний Antigravity provider в это время вернул usage limit. Это внешний лимит, поэтому новый OAuth success не заявляется и повторный запрос автоматически не выполнялся.
- Отдельный реальный `agy 1.1.24 --output-format stream-json` smoke подтвердил события `init`, `step_update` и `result`; MCP wrapper после обновления пакета и перезапуска Codex ещё требует повторного live smoke.

Это единственная проверка, зависящая от внешней OAuth-сессии и реального CLI; она не входит в deterministic count.

## Ограничения

Postflight fingerprint использует `git status` и метаданные файлов, а не content hashes. Автоматического rollback нет: partial/unknown state оставляет `requires_review=true` и требует явного решения оператора. `progress_percent` отражает только наблюдаемые wrapper phases; внутренний процент и ETA Gemini намеренно не заявляются.

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
.\.venv\Scripts\python.exe -m unittest -v test_mcp_protocol.py test_agy_server.py test_agent_manager.py
.\.venv\Scripts\python.exe -m unittest -v test_agent_manager.py test_agy_server.py test_mcp_protocol.py
.\.venv\Scripts\python.exe -m py_compile agy_server.py agent_manager.py smoke_agy.py test_agy_server.py test_agent_manager.py test_mcp_protocol.py _mcp_protocol_fixture.py
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

Legacy `server.py`, `smoke_antigravity.py` и `test_server.py` намеренно отсутствуют и не должны включаться в команды.
