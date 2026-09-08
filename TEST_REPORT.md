# Финальный отчёт тестирования

## Проверка перед push, 8 сентября 2026

- `.venv/Scripts/python.exe -m unittest discover -q`: 225 тестов за 41.666 s,
  `OK (skipped=2)`. Включены 8 новых регрессий queue budget: config, blocked
  admission, остаток бюджета для OS lock, общий deadline и cleanup.
- Root просмотрел изменения кода и документации; `git diff --check`: OK.
- Antigravity подготовил queue patch и тесты; после явного проектного согласия
  передача исходников прошла host auto-review. Это не отключение защиты Codex.
- Обновление публичной lease-телеметрии и обработка потери lease ещё не выполнены.
- Установка 0.5.0/schema 4 требует полного handoff по POST_UPDATE_ACTIVATION.md.

## Дополнение 8 сентября 2026: браузер, 0.5.0

- Полный `python -m unittest discover -q`: 217 тестов, `OK (skipped=2)`,
  36.073 s, после завершения сборки и стабилизации package metadata.
- 36 целевых тестов wiring/bridge/MCP protocol/smoke/packaging: OK.
- Реальный `smoke_browser.py --mode isolated ... --live` с Chrome DevTools MCP
  1.8.0: 16 инструментов, `live=true`, `marker_found=true`; создана и закрыта
  синтетическая локальная вкладка в временном профиле.
- Проверены MCP initialize/tools-list, пустой список в `disabled`, реальные
  отказы evaluation/JavaScript URL/initScript в proxy.
- Wheel `antiagent_mcp-0.5.0-py3-none-any.whl` собран; модуль
  `antiagent_browser.py` и console script включены. `git diff --check`: OK.
- Не выполнены: подключение к реальной пользовательской сессии и полная цепочка
  через установленный обновлённый MCP. Нужны регистрация browser bridge,
  настройки разрешений, полный restart/upgrade по POST_UPDATE_ACTIVATION.md;
  для user_session — явное предоставление сессии и подтверждение Chrome.
- Промежуточный полный прогон во время сборки поймал два
  `stale_runtime_snapshot`: metadata изменились в процессе тестов. Повторный
  полный прогон без изменений окружения прошёл.

Далее сохранён предыдущий отчёт как история базовой реализации.

Дата отчёта: 3 сентября 2026 г.
Code/config baseline: Antiagent `0.4.1`

## Среда

- Windows 11, build `26200`.
- Python `3.14.6` из `.venv`.
- MCP `2.0.0`, Pydantic `2.13.5`.
- Antigravity CLI (`agy`) `1.1.25`.
- Git `2.55.0.windows.3`.

## Итог

После исправления ошибок из отчёта 2026-09-03 deterministic suite содержит
**201 тест**, включая **6 тестов MCP STDIO**. На Windows два POSIX-only
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
- MCP default `mode=plan`; единственный/default `thinking_level=high`; CLI всегда получает `gemini-3.8-flash-high` и `--effort high`; typed diagnostics; runtime failures возвращаются как MCP `isError=true` при сохранении structured metadata; validation/redaction не раскрывают prompt, stdout, stderr или secrets.
- `agy --output-format stream-json` читается во время выполнения. Непустой terminal response имеет приоритет; при его отсутствии bounded recovery использует только `agent_response.text_delta`, никогда thinking/tool. Diagnostics сохраняют источник ответа и сырые структурные счётчики без model text.
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

- `agy models` через пользовательскую OAuth-сессию подтвердил доступность
  `gemini-3.8-flash-high` (`Gemini 3.8 Flash (High)`).
- Source-runtime managed smoke на `gemini-3.8-flash-high`, `--effort high`,
  `mode=plan`: **успешно**; marker найден, `response_source=result_response`,
  Git не изменён.
- Установленный MCP после обновления пакета и полного перезапуска Codex ещё
  требует обязательного `doctor` + strict `smoke_mcp.py` + live marker smoke.

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
