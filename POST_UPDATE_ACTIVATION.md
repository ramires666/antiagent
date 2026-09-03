# Активация обновлённого Antiagent MCP

Изменение исходников Antiagent не обновляет уже запущенный MCP. Codex загружает
MCP-конфигурацию и процесс сервера при старте, поэтому новый субагент или новый
чат внутри старого процесса продолжит использовать старый runtime snapshot.

## Когда эта процедура обязательна

Выполните её после изменений runtime, установки, регистрации, MCP-схемы или
lifecycle, в том числе файлов `agy_server.py`, `agent_manager.py`,
`response_diagnostics.py`, `runtime_identity.py`, `antiagent_setup.py`,
`antiagent_upgrade.py` и `pyproject.toml`.

Для изменений только документации или тестов без изменения поставляемого
runtime переустановка не требуется.

## Как включить обновление на Windows

1. Сохраните работу и полностью закройте все процессы, использующие Codex:
   Codex CLI, desktop app, IDE и их дочерние сессии. Обновлятор намеренно не
   завершает процессы автоматически.
2. Откройте новый PowerShell и выполните из checkout Antiagent:

   ```powershell
   Set-Location -LiteralPath 'C:\projects\antiagent'
   py -m antiagent_upgrade
   ```

   Команда безопасно обновит pipx-установку и заново зарегистрирует абсолютный
   launcher MCP в пользовательской конфигурации Codex.
3. Если обновлятор сообщает, что `antiagent-mcp.exe` всё ещё запущен, закройте
   оставшийся процесс Codex и повторите команду. Не обходите process guard
   прямым запуском `pipx` с флагом принудительной переустановки.
4. После успешного обновления запустите полностью новый верхнеуровневый процесс
   Codex. Новый чат или субагент в старом процессе не подходит.

## Как проверить, что загружен новый MCP

В новой сессии Codex вызовите MCP-инструмент `antigravity_doctor` с
`working_directory=""`. Это не команда PowerShell. Проверка должна вернуть:

- `checks_passed=true`;
- `cli_available=true`;
- `workspace_status="ready"`;
- `execution_boundary_declared=true`;
- `state_writable=true`;
- объект `runtime` присутствует, а `runtime.drift_reasons=[]`.

Затем в PowerShell из Git-root Antiagent запустите строгий smoke установленного
launcher:

```powershell
.\.venv\Scripts\python.exe .\smoke_mcp.py C:\projects\antiagent
```

Не передавайте скрипту путь `.venv\Scripts\antiagent-mcp.exe`: это может
проверить старую entry point-установку вместо актуального pipx launcher.

Наконец, выполните через MCP один ограниченный live smoke в `mode="plan"`:

- попросите вернуть уникальный `expected_marker` без изменения файлов;
- дождитесь terminal state через `antigravity_agent_wait` или
  `antigravity_agent_status`;
- проверьте `status="SUCCESS"`, наличие маркера, `worktree_changed=false` и
  пустой `changed_paths`;
- сравните `git status --short` до и после запуска.

## Критерии готовности

Обновлённый MCP можно использовать, когда одновременно выполнены все условия:

- `py -m antiagent_upgrade` завершился успешно;
- Codex полностью перезапущен после обновления;
- `antigravity_doctor` прошёл все локальные проверки;
- `smoke_mcp.py` завершился с кодом `0` и не сообщил о stale schema/runtime;
- live plan smoke вернул маркер и не изменил Git-worktree.

Если live smoke не проходит только из-за provider quota/usage limit, локальная
установка всё равно может быть корректной. Не создавайте повторные одинаковые
запуски: используйте предусмотренный fallback на native worker, а provider
проверьте позже одним новым bounded-запросом.

## Если после обновления видна старая схема

Это означает, что обновлена копия на диске, но текущий процесс Codex всё ещё
держит старый MCP snapshot либо был проверен не тот launcher. Полностью закройте
Codex, снова выполните `py -m antiagent_upgrade`, запустите новый Codex и
повторите doctor, строгий smoke и live marker smoke в указанном порядке.
