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

В Windows PowerShell установите актуальную официальную версию CLI инструкцией из документации (в текущем checkout проверена версия `1.1.22`):

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

До запуска проверяется абсолютный Git-root. Необязательный `working_directory` принимает абсолютный путь или путь относительно текущего каталога MCP process и должен указывать точно на Git-root; пустое значение использует текущий каталог process. После canonical resolve разрешён только process cwd или его descendant: `..`, symlink или junction, ведущие наружу, отклоняются. Например, при process cwd `W:\HARDDEV` разрешён `W:\HARDDEV\smartgold`. Trust bypass не добавляется автоматически. Prompt запрещает дочернему агенту использовать MCP, plugins, subagents и сеть; slash/skill expansion дополнительно отключён флагом CLI.

Allow rules минимальны: чтение workspace, необходимые редактирования и явно разрешённые проверки. Shell, сеть, parent directories и внешние credentials не разрешаются глобально. `run_command` остаётся OS trust boundary.

`--sandbox` обязателен как дополнительная защита host. AppContainer ограничивает процессы, но не гарантирует абсолютную недоступность каждого пути вне workspace (например, отдельные временные каталоги могут быть видимы). Основная граница — permission engine без bypass-флага, проверенный Git-root и последующий diff-review. Если sandbox backend недоступен, executor возвращает ошибку и не переключается на неограниченный host execution.

## MCP-контракт

Совместимый синхронный tool `antigravity_cli_execute` принимает `task`, необязательные `context`, `verification` и `working_directory` (default — пустая строка), `thinking_level` (`low|medium|high`, default `medium`), `mode` (`plan|accept-edits`, default `plan`), `acknowledge_review` (boolean, default `false`) и optional UUID `conversation_id`. Для каждого editing-вызова оператор должен явно выбрать `mode=accept-edits`. `acknowledge_review=true` нужен только после ручной проверки partial/unknown результата, когда wrapper вернул `review_required`.

Выход: `status`, `result`, `model`, `thinking_level`, `mode`, `usage`, `conversation_id`, `result_truncated`, `error_type`, `exit_code`, `retryable`, `run_id`, `started_at`, `finished_at`, `duration_seconds`, `cli_version`, `metadata_complete`, `usage_available`, `conversation_id_available`, `preexisting_dirty`, `worktree_changed`, `changed_paths`, `postflight_complete`, `requires_review`.

Persistent manager добавляет шесть tools:

- `antigravity_agent_spawn` — валидирует тот же request, сохраняет `queued`, запускает background task и сразу возвращает `agent_id`;
- `antigravity_agent_status` — возвращает durable snapshot и terminal output;
- `antigravity_agent_list` — bounded history одного разрешённого Git workspace;
- `antigravity_agent_wait` — ждёт не более 60 секунд за один MCP call, не отменяя run при wait timeout;
- `antigravity_agent_followup` — создаёт дочерний run через сохранённый `conversation_id`; безопасный default режима снова `plan`;
- `antigravity_agent_interrupt` — идемпотентно выставляет cross-process cancel flag и отменяет локальное дерево процессов.

Состояния: `queued`, `running`, `completed`, `failed`, `interrupted`; terminal state не перезаписывается. SQLite store использует stdlib, не требует новой зависимости и хранится в `%LOCALAPPDATA%\antiagent\agents.sqlite3` (`XDG_STATE_HOME`/`~/.local/state/antiagent` на POSIX). В БД нет исходных prompt/task/context/verification. Result ограничен 256 KiB, history — 1000 terminal rows, active runs — 32. Stale `queued|running` получает `failed` и `manager_error=manager_lost`.

## MCP-конфигурация Windows

Рекомендуемый способ — официальный CLI Codex:

```powershell
codex.cmd mcp add antigravity_cli_executor -- "<ABSOLUTE_PATH_TO_REPO>\.venv\Scripts\python.exe" "<ABSOLUTE_PATH_TO_REPO>\agy_server.py"
codex.cmd mcp get antigravity_cli_executor
```

После добавления перезапустите Codex CLI, IDE extension или desktop app. Они используют общую MCP-конфигурацию. Для локального STDIO server поле MCP `Auth` может отображаться как `Unsupported`: OAuth выполняет вложенный `agy` через Windows Credential Manager, а не MCP-транспорт.

Эквивалентная ручная конфигурация:

```toml
[mcp_servers.antigravity_cli_executor]
command = 'W:\_python\antiagent\.venv\Scripts\python.exe'
args = ['W:\_python\antiagent\agy_server.py']
cwd = 'W:\_python\antiagent'
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
  'antigravity_cli_execute',
]
```

В этом checkout готовый project-scoped шаблон находится в `.codex/config.toml`, а custom Codex-agent — в `.codex/agents/antigravity_worker.toml`. Agent использует `gpt-5.6-luna` только как дешёвый proxy внутри native Codex thread, имеет `sandbox_mode=read-only` и MCP allowlist из семи tools; фактическую coding-задачу выполняет Gemini/Antigravity. Для другого проекта замените абсолютные пути на его trusted root и `.venv`. Указывайте Python из `.venv`; не подставляйте глобальный `python` или глобальный MCP. Не коммитьте keyring exports, токены, `.env` или временные sandbox artifacts. Wrapper timeout — 840 секунд, поэтому `tool_timeout_sec` Codex должен быть 900 секунд.

Custom agents загружаются при старте Codex. После добавления или изменения TOML полностью перезапустите CLI/app/IDE session. Fine-grained запрета shell отдельным полем custom-agent schema нет: здесь применяются read-only sandbox, MCP allowlist и developer instructions. Родительский Codex остаётся владельцем lifecycle/UI, разрешений, diff review и тестов.

## Smoke и тесты

```powershell
agy --version
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe "<ABSOLUTE_PATH_TO_REPO>\smoke_agy.py"
```

Acceptance criteria:

1. `tools/list` возвращает семь tools; schema валидирует `low|medium|high`, default `medium`, `plan`, `accept-edits`, boolean `acknowledge_review` (default `false`), optional UUID `conversation_id` и строковый `working_directory`.
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
