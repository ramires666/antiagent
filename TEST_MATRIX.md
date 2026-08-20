# MCP test matrix

Цель: проверить не только Python helpers, но и реальный STDIO JSON-RPC путь клиента до `agy_server.py`. Все автоматические тесты работают offline, без OAuth, API keys, сети и изменений пользовательских файлов.

## 1. MCP protocol

| Сценарий | Ожидаемый результат | Проверка |
|---|---|---|
| STDIO process start and initialize | Согласована поддерживаемая версия MCP; имя сервера корректно | Real `ClientSession` |
| `tools/list` | Доступен ровно ожидаемый tool | Real `ClientSession` |
| Tool schema | `task` required; `thinking_level=low|medium|high`; `mode=plan|accept-edits`; defaults корректны | Real `ClientSession` |
| Output schema | Все поля structured response и их типы описаны; неизвестные поля запрещены | Real `ClientSession` |
| Valid `tools/call` | Структурированный `CallToolResult`; stdout содержит только JSON-RPC | Real `ClientSession` |
| Unknown tool | Стандартная MCP ошибка без падения server process | Real `ClientSession` |
| Missing `task` | Schema/validation error, без запуска Git или CLI | Real `ClientSession` |
| Wrong argument types | Schema/validation error, без запуска Git или CLI | Real `ClientSession` |
| Invalid enum values | Schema/validation error или безопасный structured error согласно MCP SDK | Real `ClientSession` |
| Non-Git working directory | Безопасный structured error, сессия остаётся рабочей | Real `ClientSession` |
| Progress token | Сервер может отправить `notifications/progress`; отсутствие поддержки клиента не ломает вызов | Client callback/unit |
| Cancellation | Вызов отменяется, subprocess tree завершается, MCP session остаётся согласованной | Client/unit |
| Sequential calls after an error | Следующий вызов работает; lock и protocol stream не повреждены | Real `ClientSession` |

## 2. Input and workspace errors

- `task`: пустой, whitespace, non-string.
- `context` и `verification`: non-string.
- Prompt: ниже лимита, ровно на лимите, выше лимита.
- Prompt formatting and stripping.
- Git: success, command exception, timeout/non-zero, empty/invalid output, nested directory, non-repository.
- Validation and prompt-size errors happen before Git/CLI resolution.

## 3. Configuration and CLI resolution

- Timeout: unset, valid, invalid, zero/negative, above maximum.
- CLI resolution priority: explicit environment path, `PATH`, `%LOCALAPPDATA%`, unavailable.
- Missing CLI returns a generic safe error.
- Exact argv: prompt, mode, model, effort, `json`, print timeout, sandbox, disabled slash commands, Git root.
- No shell and no `--dangerously-skip-permissions` in either mode.
- Child environment removes API keys, tokens, secrets, passwords, credentials and Google credential files.

## 4. Subprocess lifecycle

- Spawn success and OSError/ValueError.
- Correct cwd, stdin/stdout/stderr and platform creation flags.
- Global lock serializes execution and releases after success/error/timeout/cancellation.
- Inner timeout and outer timeout while waiting for the lock.
- Caller cancellation re-raises `CancelledError` after cleanup.
- Unexpected reader/process exceptions return safe errors after cleanup.
- Windows Job Object: success plus create/configure/open/assign failures.
- Tree-kill: Windows success/fallback/error and POSIX success/fallback.
- Actual Windows descendant termination regression test.

## 5. Output bounds and normalization

- stdout/stderr empty, normal, exact limit and over limit.
- Process exit before/after pipe EOF; reader failure; killed-process wait failure.
- stderr never appears in tool result or logs.
- JSON: malformed, scalar/list, non-zero exit, `status != SUCCESS`, missing/non-string response.
- Result truncation and `result_truncated` flag.
- Usage allowlist; non-dict, bool/string, negative and non-finite numbers.
- Conversation ID: valid, empty, non-string, path-like, whitespace and overlong.
- Unknown response fields and secret markers are discarded from structured metadata.
- Generic error logs and results contain no prompt/stdout/stderr/environment values.

## 6. Release acceptance

1. Full `unittest discover -v` succeeds twice consecutively.
2. Real STDIO protocol suite succeeds without browser or network.
3. `py_compile` succeeds for server, smoke and test files.
4. MCP schema snapshot contains only documented arguments and enums.
5. `git diff --check` and repository secret scan succeed.
6. No child `agy`/test process remains after timeout or cancellation tests.
7. Optional authenticated live smoke remains read-only and is not part of the deterministic suite.

## 7. Фактический статус на commit `015b21f`

Среда последней проверки: Windows 11 (`10.0.26200.0`), Python `3.13.4`, MCP `2.0.0`, Pydantic `2.13.4`, AnyIO `4.14.2` из `.venv`.

- Всего: **55 тестов, 55 PASS**.
- Полный discovery прошёл два раза подряд без ошибок.
- Реальный STDIO-контур проверен через `mcp.ClientSession`: initialize, `tools/list`, input/output schema, успешный `tools/call`, unknown tool, обязательный `task`, неверные типы и enum, не-Git cwd, progress notifications, отмена вызова и повторный вызов в той же сессии.
- Deterministic STDIO fixture (`_mcp_protocol_fixture.py`) подменяет `_run_cli`; он не запускает настоящий `agy`, не использует OAuth, браузер, API keys или сеть.
- Проверены ошибки и lifecycle: validation/Git/CLI resolution, spawn/timeout/overflow/reader failure, lock recovery, cancellation/reap, Windows Job Object и tree-kill, bounds/JSON normalization, progress heartbeat и serialization.
- В Windows descendant regression исправлена прежняя проверка `tasklist`, которая могла дать false-negative при ошибке команды или совпадении строки: теперь тест проверяет exact PID через `OpenProcess`/`GetExitCodeProcess` и закрывает полученный handle.

Матрица отражает проверенные reachable branches, но не является обещанием математического 100% покрытия. Не выполнялись authenticated live OAuth smoke и проверка с реальным `agy`/реальным аккаунтом; это сознательное ограничение deterministic/offline suite.
