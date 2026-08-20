# Финальный отчёт тестирования

Дата отчёта: 20 августа 2026 г.
Commit: `015b21f`

## Среда

- ОС: Windows 11, build `10.0.26200.0`.
- Python из `.venv`: `3.13.4`.
- MCP: `2.0.0`.
- Pydantic: `2.13.4`.
- AnyIO: `4.14.2`.

## Итог

Итоговый набор содержит 55 тестов. Полный discovery успешно выполнен два раза подряд:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe -m unittest discover -q
```

Результат обоих прогонов: **55 tests, OK**.

Дополнительно выполнены финальные targeted-проверки `test_agy_server.py` и `test_mcp_protocol.py`, `py_compile` для исходников и тестов, а также `git diff --check`.

## Что проверено

Проверены input/workspace validation, Git-root detection, timeout/configuration, CLI resolution, exact argv и безопасное окружение subprocess. Отдельно покрыты spawn failures, output bounds, stdout/stderr draining, malformed/non-zero JSON, normalization usage/conversation ID, truncation и generic errors без вывода prompt/stdout/stderr/secrets.

Lifecycle-проверки включают global lock и восстановление после timeout, cancellation с повторным вызовом, bounded reader overflow, reader/process failures, Windows Job Object setup/cleanup, Windows exact-PID tree-kill, POSIX fallback, timeout/cancellation reap и закрытие transport.

Progress проверен для queued/running/heartbeat, cancellation/reaping heartbeat, callback failures и конкурентных callbacks: локальный progress lock сохраняет последовательную нумерацию.

## Реальный STDIO MCP-контур

`test_mcp_protocol.py` запускает отдельный STDIO server process и использует настоящий `mcp.ClientSession`. Проверены initialize, `tools/list`, input/output schemas, успешный `tools/call`, unknown tool, missing/wrong arguments, invalid enums, non-Git cwd, progress callback, read timeout, cancelled call и последующий вызов в той же MCP-сессии.

Для этих сценариев используется `_mcp_protocol_fixture.py`: он подменяет `_run_cli` на deterministic response fixture. Поэтому тесты не запускают настоящий `agy`, не требуют OAuth/browser login, API keys, сети или пользовательских файлов.

## Исправленный false-negative

В Windows descendant regression прежняя проверка через текстовый вывод `tasklist` могла считать процесс завершённым, если сама команда завершилась ошибкой или строковый поиск дал неоднозначное совпадение. Проверка исправлена на exact PID через `OpenProcess` и `GetExitCodeProcess`; каждый открытый handle закрывается. Тест теперь проверяет завершение parent и descendant и содержит защитный cleanup через `taskkill`.

## Ограничения

Authenticated live OAuth smoke с реальным аккаунтом и реальным `agy` **не выполнялся**. Это намеренное ограничение: deterministic suite должна оставаться offline, воспроизводимой и не зависеть от credentials, браузера, сети и внешнего CLI. Поэтому отчёт подтверждает поведение wrapper/MCP protocol и lifecycle fixture-процесса, но не подтверждает доступность OAuth-сессии или фактический ответ production `agy`.

Coverage matrix фиксирует проверенные reachable branches и известные ограничения; это не заявление о математическом 100% покрытии.

## Воспроизведение

Из корня репозитория в PowerShell:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe -m unittest discover -q
.\.venv\Scripts\python.exe -m unittest -v test_agy_server.py test_mcp_protocol.py
.\.venv\Scripts\python.exe -m py_compile agy_server.py server.py smoke_agy.py smoke_antigravity.py test_agy_server.py test_mcp_protocol.py test_server.py _mcp_protocol_fixture.py
git diff --check
```
