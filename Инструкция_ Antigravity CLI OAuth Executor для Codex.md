# Antigravity CLI OAuth Executor для Codex

`agy_server.py` — единственный production MCP executor. Он запускает официальный Antigravity CLI (`agy`) как subprocess с OAuth-сессией браузера; API-key/SDK-ветки в проекте нет.

## Официальные источники

- [Install](https://antigravity.google/docs/cli/install)
- [Headless mode](https://antigravity.google/docs/cli/headless)
- [Sandbox](https://antigravity.google/docs/cli/sandbox)
- [Permissions](https://antigravity.google/docs/cli/permissions)
- [Official repository](https://github.com/google-antigravity/antigravity-cli)

API keys в executor не используются. OAuth выполняется через браузер под пользовательским Pro-аккаунтом, а credentials хранит системный keyring Antigravity CLI. Wrapper не читает keyring, не принимает credentials и не передаёт API-key переменные окружения дочернему процессу. Wrapper не логирует `stderr` CLI. Это снижает риск утечки, но не является абсолютной гарантией: модель может вернуть секрет в обычном model response, поэтому основной агент не должен передавать секреты в task и обязан проверять результат перед принятием.

Не сохраняйте OAuth state, cookies или токены в репозитории, `.env`, логах или MCP response.

Предупреждение о Gemini CLI individual OAuth, отключённом 18 июня 2026 года, относится только к причине миграции. Этот проект использует Antigravity CLI, не Gemini CLI; fallback на API key запрещён.

## Установка и OAuth

В Windows PowerShell установите актуальную официальную версию CLI инструкцией из документации (в текущем checkout проверена версия `1.1.24`):

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://antigravity.google/cli/install.ps1 | iex"
agy --version
agy
```

Перезапустите PowerShell после установки, чтобы обновился `PATH`; до перезапуска запускайте `& "$env:LOCALAPPDATA\agy\bin\agy.exe"`. Завершите browser OAuth интерактивно. После входа MCP запускает `agy` через `agy_server.py`; напрямую указывать `agy` как MCP server нельзя.

Python executor запускайте только из локального `.venv`, созданного по pinned-зависимостям проекта: глобальный MCP (`mcp 1.27`) несовместим с этой веткой и не должен использоваться.

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Не используйте неподтверждённые переменные вроде `GEMINI_CLI_HOME` для session isolation: wrapper не меняет расположение keyring, если это не описано официальной документацией Antigravity CLI.

## Режимы

Поддерживаются только `thinking_level`: `low`, `medium`, `high`; default — `medium`. Выбирается модель `gemini-3.7-flash-{level}`. Поддерживаются режимы `plan` (анализ без изменений) и `accept-edits` (явно разрешённые изменения); default режима — `plan`.

В unattended MCP adapter всегда передаёт `--sandbox` и `--disable-slash-commands`. `--dangerously-skip-permissions` никогда не используется. `accept-edits` разрешает штатные изменения файлов внутри workspace; shell-команды и операции, требующие подтверждения, в headless режиме soft-denied, если пользователь отдельно не добавил узкое allow-правило в настройках CLI. Проверки результата независимо запускает основной агент.

## Workspace, permissions и sandbox

До запуска проверяется абсолютный Git-root. Необязательный `working_directory` принимает абсолютный путь или путь относительно текущего каталога MCP process и должен указывать точно на Git-root; пустое значение использует текущий каталог process. Глобальная MCP-регистрация намеренно не задаёт `cwd`, поэтому process наследует Git-root текущей Codex-сессии. После canonical resolve разрешён только process cwd или его descendant: `..`, symlink или junction, ведущие наружу, отклоняются. Trust bypass не добавляется автоматически. Prompt запрещает дочернему агенту использовать MCP, plugins, subagents и сеть; slash/skill expansion дополнительно отключён флагом CLI.

Allow rules минимальны: чтение workspace, необходимые редактирования и явно разрешённые проверки. Shell, сеть, parent directories и внешние credentials не разрешаются глобально. `run_command` остаётся OS trust boundary.

`--sandbox` обязателен как дополнительная защита host. AppContainer ограничивает процессы, но не гарантирует абсолютную недоступность каждого пути вне workspace (например, отдельные временные каталоги могут быть видимы). Основная граница — permission engine без bypass-флага, проверенный Git-root и последующий diff-review. Если sandbox backend недоступен, executor возвращает ошибку и не переключается на неограниченный host execution.

## MCP-контракт

Совместимый синхронный tool `antigravity_cli_execute` принимает `task`, необязательные `context`, `verification` и `working_directory` (default — пустая строка), `thinking_level` (`low|medium|high`, default `medium`), `mode` (`plan|accept-edits`, default `plan`), `acknowledge_review` (boolean, default `false`), optional UUID `conversation_id`, optional `expected_marker` (непустая строка до 256 символов) и единственный поддерживаемый `payload_mode=workspace`. Для каждого editing-вызова оператор должен явно выбрать `mode=accept-edits`. `acknowledge_review=true` нужен только после ручной проверки partial/unknown результата, когда wrapper вернул `review_required`. Если непустой успешный ответ не содержит marker, wrapper возвращает `verification_failed`, не повторяя marker в ошибке или логах. Structured failed/no-content результаты сохраняют только allowlisted usage counters.

Wrapper сначала учитывает точный машинный `error_type` CLI, затем применяет
ограниченную классификацию bounded diagnostics. Поэтому сетевой запрет, проблема
профиля, OAuth timeout, payload policy или headless tool denial не маскируются
как общие `invalid_json`, `no_content` либо wrapper timeout. Устаревший startup
шум авторизации не перекрывает точный terminal code. Raw stdout/stderr при этом
не возвращаются и не сохраняются.

Выход: `status`, `result`, `model`, `thinking_level`, `mode`, `usage`, `conversation_id`, `result_truncated`, `error_type`, `exit_code`, `retryable`, `run_id`, `started_at`, `finished_at`, `duration_seconds`, `cli_version`, `metadata_complete`, `usage_available`, `conversation_id_available`, `preexisting_dirty`, `worktree_changed`, `changed_paths`, `postflight_complete`, `requires_review`, `payload_mode`, `file_scope_enforced`, `shell_denied`.

Execution имеет `payload_mode=workspace` и возвращает
`file_scope_enforced=false`, `shell_denied=false`. CLI `1.1.24` не имеет
официальных file allowlist/mandatory deny-shell primitives, поэтому
неподдерживаемые `prompt_only` и `scoped_files` удалены из публичной MCP-схемы.
Относительный `@file` и запрет shell в prompt уменьшают вероятность лишних
tool-запросов, но не являются технической границей.

`antigravity_doctor` выполняет только локальный preflight: bounded
`agy --version`, boundary declaration, пробную запись wrapper state и Git
preflight. В проверенном Antigravity CLI `1.1.24` нет официальных `auth`/`doctor`
subcommands, поэтому tool честно возвращает `auth_probe=unsupported`,
`network_probe=not_run`, `oauth_ready=unknown`. Он не читает профиль/keyring и
не может запустить browser OAuth.

Persistent manager добавляет шесть tools:

- `antigravity_agent_spawn` — валидирует тот же request, сохраняет `queued`, запускает background task и сразу возвращает `agent_id`;
- `antigravity_agent_status` — возвращает durable snapshot и terminal output;
- `antigravity_agent_list` — bounded history одного разрешённого Git workspace;
- `antigravity_agent_wait` — ждёт не более 60 секунд за один MCP call, не отменяя run при wait timeout;
- `antigravity_agent_followup` — создаёт дочерний run через сохранённый `conversation_id`; безопасный default режима снова `plan`;
- `antigravity_agent_interrupt` — идемпотентно выставляет cross-process cancel flag и отменяет локальное дерево процессов.

Состояния: `queued`, `running`, `completed`, `failed`, `interrupted`; terminal state не перезаписывается. SQLite store использует stdlib, не требует новой зависимости и хранится в `%LOCALAPPDATA%\antiagent\agents.sqlite3` (`XDG_STATE_HOME`/`~/.local/state/antiagent` на POSIX). В БД нет исходных prompt/task/context/verification. Result ограничен 256 KiB, history — 1000 terminal rows, active runs — 32. Stale `queued|running` получает `failed` и `manager_error=manager_lost`.

`agent_wait` ограничивает также отправку progress notification: зависший клиент
не может растянуть заданный wait timeout. Обычный poll без stale-кандидатов не
открывает SQLite write-транзакцию, что уменьшает конкуренцию с heartbeat и
terminal persistence. NDJSON stdout и stderr постоянно drain-ятся, но в памяти
удерживаются только ограниченные diagnostics и финальный structured result;
большое число промежуточных событий само по себе не считается output overflow.

## MCP-конфигурация Windows

Сначала установите переносимую команду через `pipx`, затем зарегистрируйте её
через официальный CLI Codex:

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
py -m pipx install .
.\.venv\Scripts\python.exe -m antiagent_setup
```

Для последующих обновлений сначала полностью закройте все Codex-клиенты, затем
из этого source checkout используйте только fail-closed путь:

```powershell
py -m antiagent_upgrade
```

Он проверяет отсутствие активного `antiagent-mcp.exe` до запуска `pipx`, не
завершает процессы автоматически, обновляет регистрацию только после успешной
установки и требует полный restart плюс `doctor`/live smoke.

После добавления перезапустите Codex CLI, IDE extension или desktop app. Они используют общую MCP-конфигурацию. Для локального STDIO server поле MCP `Auth` может отображаться как `Unsupported`: OAuth выполняет вложенный `agy` через Windows Credential Manager, а не MCP-транспорт.

Эквивалентная ручная конфигурация:

```toml
[mcp_servers.antigravity_cli_executor]
command = '<ABSOLUTE-PIPX-SHIM-PATH>'
experimental_environment = 'local'
enabled = true
required = true
startup_timeout_sec = 30
tool_timeout_sec = 900
enabled_tools = [
  'antigravity_agent_spawn',
  'antigravity_agent_list',
  'antigravity_agent_status',
  'antigravity_agent_wait',
  'antigravity_agent_followup',
  'antigravity_agent_interrupt',
  'antigravity_doctor',
  'antigravity_cli_execute',
]

[mcp_servers.antigravity_cli_executor.env]
ANTIAGENT_EXECUTION_BOUNDARY = 'host'
```

Project-scoped MCP-таблицу намеренно не храните в `.codex/config.toml`: она перекрывает пользовательский абсолютный launcher и при отсутствии pipx shim в `PATH` ломает старт с `program not found`. Custom Codex-agent находится в `.codex/agents/antigravity_worker.toml`. Local placement само по себе не доказывает доступ к Windows keyring. Boundary подтверждается только read-only live smoke после полного restart; подробности — в `HOST_SIDE_DEPLOYMENT.md`. Agent использует `gpt-5.6-luna` только как дешёвый proxy внутри native Codex thread, имеет `sandbox_mode=read-only` и MCP allowlist из восьми tools; фактическую coding-задачу выполняет Gemini/Antigravity. Для другого проекта копируются только нужные worker/skill-файлы; глобальную MCP-регистрацию и пути менять не нужно. Не коммитьте keyring exports, токены, `.env` или временные sandbox artifacts. Wrapper timeout — 840 секунд, поэтому user-level `tool_timeout_sec` при ручной настройке Codex должен быть 900 секунд.

Custom agents загружаются при старте Codex. После добавления или изменения TOML полностью перезапустите CLI/app/IDE session. Fine-grained запрета shell отдельным полем custom-agent schema нет: здесь применяются read-only sandbox, MCP allowlist и developer instructions. Родительский Codex остаётся владельцем lifecycle/UI, разрешений, diff review и тестов.

## Smoke и тесты

```powershell
agy --version
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe .\smoke_mcp.py <КОРЕНЬ_GIT_ПРОЕКТА>
.\.venv\Scripts\python.exe .\smoke_agy.py
```

`smoke_mcp.py` завершается ненулевым кодом не только при транспортной ошибке,
но и при `checks_passed=false`, неготовом workspace/boundary/state либо
устаревшей output schema установленного MCP. Совпадения одних имён восьми tools
недостаточно: после upgrade требуется полный restart Codex, затем повторный
smoke.

Acceptance criteria:

1. `tools/list` возвращает восемь tools; schema валидирует `low|medium|high`, default `medium`, `plan`, `accept-edits`, boolean `acknowledge_review` (default `false`), optional UUID `conversation_id`, bounded `expected_marker` и строковый `working_directory`; doctor schema не содержит credentials или profile path.
2. `working_directory` разрешается как абсолютный путь или относительно process cwd; пустой default использует process cwd. После canonical resolve путь обязан быть process cwd или его descendant и точным Git-root; выход через `..`, symlink или junction и остальные каталоги отклоняются. Subprocess получает этот абсолютный Git-root в `cwd`.
3. Adapter передаёт `--sandbox` и `--disable-slash-commands`.
4. stdout JSON, malformed JSON, non-zero exit и timeout дают безопасный структурированный ответ.
5. Timeout завершает процесс (`terminate`/`kill` + `wait`) и очищает временное состояние.
6. Wrapper не передаёт API-key переменные окружения и не логирует `stderr`; основной агент не передаёт секреты в task и проверяет model response на случайно возвращённые секреты.
7. Permission engine работает без bypass-флага; sandbox активен как дополнительное, но не абсолютное ограничение host.
8. Diff независимо проверяется основным Codex-агентом.
9. Каждый ответ сообщает `run_id`, timestamps, duration, exit code, typed `error_type`, `retryable`, фактическую `cli_version` и явные признаки доступности usage/conversation metadata.
10. Каждый вызов сообщает Git postflight: `preexisting_dirty`, `worktree_changed`, `changed_paths`, `postflight_complete`, `requires_review`; полный diff через MCP не возвращается.
11. Lifecycle проверяет `spawn/wait/status/list/followup/interrupt`, persistence новым store instance, cross-store cancellation, immediate-cancel race, terminal immutability, capacity/history/output bounds и отсутствие prompt в snapshot/SQLite schema.

Тесты используют subprocess injection/mock и не требуют сети или browser login. Реальный OAuth smoke выполняется отдельно после ручного входа.

В проекте намеренно оставлен один поддерживаемый путь: `agy_server.py` + OAuth CLI. Старую несовместимую SDK/API-key реализацию следует хранить только в истории Git или отдельном архивном репозитории.
