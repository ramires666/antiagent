# Активация обновлённого Antiagent MCP

Изменение исходников Antiagent не обновляет уже запущенный MCP. Codex загружает
MCP-конфигурацию и процесс сервера при старте, поэтому новый субагент или новый
чат внутри старого процесса продолжит использовать старый runtime snapshot.

## Когда эта процедура обязательна

Выполните её после изменений runtime, установки, регистрации, MCP-схемы или
lifecycle, в том числе файлов `agy_server.py`, `agent_manager.py`,
`response_diagnostics.py`, `runtime_identity.py`, `antiagent_setup.py`,
`antiagent_upgrade.py` и `pyproject.toml`, а также после реализации этапов из
[WORK_PLAN.md](WORK_PLAN.md).

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

- укажите `thinking_level="high"`; runtime должен сообщить
  `model="gemini-3.8-flash-high"`;
- попросите вернуть уникальный `expected_marker` без изменения файлов;
- дождитесь terminal state через `antigravity_agent_wait` или
  `antigravity_agent_status`;
- проверьте `status="SUCCESS"`, наличие маркера, `worktree_changed=false` и
  пустой `changed_paths`;
- сравните `git status --short` до и после запуска.

## Критерии готовности

### Дополнение для очереди ожидания и блокировок

Очередь ожидания рабочей области управляется `ANTIAGENT_QUEUE_TIMEOUT_SECONDS`
(default 60s, допустимый диапазон 1..300s, невалидные значения безопасно используют 60s).
Ожидание в durable-очереди и захват OS lock делят общий бюджет очереди, ограниченный
остатком общего дедлайна задачи (`min(queue_budget, remaining_deadline)`), без
перезапуска и без предоставления нового полного бюджета при переходе к исполнению.
При превышении бюджета возвращается неизменный код `workspace_lock_timeout`.
Обновление публичной телеметрии lease отложено; старый snapshot в progress
не доказывает истечение фактического lease. Потеря lease не заявляет
автоматическую отмену задачи (cancellation): целостность защищается OS lock, а
полная обработка потери lease реализуется отдельным этапом по [WORK_PLAN.md](WORK_PLAN.md).

### Дополнение для браузера (0.5.0, schema revision 4)

После полного закрытия Codex и `py -m antiagent_upgrade` новый doctor должен
показывать `runtime.schema_revision="4"`. Строгий `smoke_mcp.py` дополнительно
проверяет `browser_mode` во всех трёх инструментах запуска, включая followup.
Затем выполните обычный bounded marker smoke без браузера.

Для браузерной функции отдельно установите и зарегистрируйте `antiagent_browser`
по [BROWSER.md](BROWSER.md), задайте точечные разрешения инструментов в Antigravity
и выполните `smoke_browser.py --mode isolated --node "<ABSOLUTE-NODE>" --script
"<ABSOLUTE-BACKEND-SCRIPT>" --live`. Ожидаются `marker_found=true` и `live=true`.
В новом верхнеуровневом Codex выполните ещё один bounded live запуск через
`antigravity_agent_spawn` с `browser_mode="isolated"`: откройте публичную страницу,
получите её заголовок и сравните Git до/после. Только этот тест подтверждает всю
цепочку Codex → Antiagent → agy → browser. Для `user_session` пользователь должен
включить remote debugging и подтвердить Chrome; отдельно проверьте тот же smoke
с `--mode user_session`. Не объявляйте пользовательскую сессию проверенной по
результату чистого профиля или unit-тестов. Исходники не активируют установленный MCP.

### Общие критерии

Отказ auto-review самого Codex до доставки MCP-вызова не создаёт structured
agent snapshot и не исправляется переустановкой CLI или повторными запусками.
Проверяйте причину отказа и проектное разрешение на передачу кода получателю
в AGENTS.md; это разрешение не отменяет политику хоста.

Обновлённый MCP можно использовать, когда одновременно выполнены все условия:

- `py -m antiagent_upgrade` завершился успешно;
- Codex полностью перезапущен после обновления;
- `antigravity_doctor` прошёл все локальные проверки;
- `smoke_mcp.py` завершился с кодом `0` и не сообщил о stale schema/runtime;
- live plan smoke вернул маркер и не изменил Git-worktree.
- live plan smoke сообщил `gemini-3.8-flash-high`, `thinking_level=high`, а
  diagnostics указали непустой `response_source`;

Если live smoke не проходит только из-за provider quota/usage limit, локальная
установка всё равно может быть корректной. Не создавайте повторные одинаковые
запуски: используйте предусмотренный fallback на native worker, а provider
проверьте позже одним новым bounded-запросом.

## Если после обновления видна старая схема

Это означает, что обновлена копия на диске, но текущий процесс Codex всё ещё
держит старый MCP snapshot либо был проверен не тот launcher. Полностью закройте
Codex, снова выполните `py -m antiagent_upgrade`, запустите новый Codex и
повторите doctor, строгий smoke и live marker smoke в указанном порядке.
