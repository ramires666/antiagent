# Antigravity CLI OAuth Executor для Codex

`agy_server.py` — primary MCP executor. Он запускает официальный Antigravity CLI (`agy`) как subprocess с OAuth-сессией браузера. Старая SDK-реализация в `server.py` сохраняется как backup.

## Официальные источники

- [Install](https://antigravity.google/docs/cli/install)
- [Headless mode](https://antigravity.google/docs/cli/headless)
- [Sandbox](https://antigravity.google/docs/cli/sandbox)
- [Permissions](https://antigravity.google/docs/cli/permissions)
- [Official repository](https://github.com/google-antigravity/antigravity-cli)

API keys в executor не используются. OAuth выполняется через браузер под пользовательским Pro-аккаунтом, а credentials хранит системный keyring Antigravity CLI. Wrapper не читает keyring и не принимает credentials. Не сохраняйте OAuth state, cookies или токены в репозитории, `.env`, логах или MCP response.

Предупреждение о Gemini CLI individual OAuth, отключённом 18 июня 2026 года, относится только к причине миграции. Этот проект использует Antigravity CLI, не Gemini CLI; fallback на API key запрещён.

## Установка и OAuth

В Windows PowerShell установите официальную версию CLI 1.1.16 инструкцией из документации:

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://antigravity.google/cli/install.ps1 | iex"
agy --version
agy
```

Завершите browser OAuth интерактивно. После входа MCP запускает `agy` через `agy_server.py`; напрямую указывать `agy` как MCP server нельзя.

Не используйте неподтверждённые переменные вроде `GEMINI_CLI_HOME` для session isolation: wrapper не меняет расположение keyring, если это не описано официальной документацией Antigravity CLI.

## Режимы

Поддерживаются только `thinking_level`: `low`, `medium`, `high`; default — `medium`. Выбирается модель `gemini-3.7-flash-{level}`. Также поддерживаются режимы `plan` (анализ без изменений) и `accept-edits` (явно разрешённые изменения).

В unattended MCP adapter всегда передаёт `--sandbox` и `--disable-slash-commands`. В `plan` сохраняются штатные permission rules. Явно выбранный `accept-edits` дополнительно включает `--dangerously-skip-permissions`, чтобы headless-агент мог запускать проверки без зависания; этот флаг допустим только вместе с sandbox, в доверенном Git-workspace и с обязательным последующим review основным агентом.

## Workspace, permissions и sandbox

До запуска проверяется абсолютный Git-root. Trust bypass не добавляется автоматически. Prompt запрещает дочернему агенту использовать MCP, plugins, subagents и сеть; slash/skill expansion дополнительно отключён флагом CLI.

Allow rules минимальны: чтение workspace, необходимые редактирования и явно разрешённые проверки. Shell, сеть, parent directories и внешние credentials не разрешаются глобально. `run_command` остаётся OS trust boundary.

`--sandbox` обязателен для host protection. AppContainer/Windows sandbox settings задаются явно по официальной документации. Если sandbox backend недоступен, executor возвращает ошибку и не переключается на неограниченный host execution.

## MCP-конфигурация Windows

```toml
[mcp_servers.antigravity_cli_executor]
command = "<ABSOLUTE_PATH_TO_PYTHON>"
args = ["<ABSOLUTE_PATH_TO_REPO>\\agy_server.py"]
```

Используйте абсолютные пути и placeholders. Не коммитьте keyring exports, токены, `.env` или временные sandbox artifacts.

## Smoke и тесты

```powershell
agy --version
python -m unittest discover -v
python <ABSOLUTE_PATH_TO_REPO>\\smoke_agy.py
```

Acceptance criteria:

1. Schema валидирует `low|medium|high`, default `medium`, `plan` и `accept-edits`.
2. Subprocess получает абсолютный Git-root в `cwd`; каталог без `.git` отклоняется.
3. Adapter передаёт `--sandbox` и `--disable-slash-commands`.
4. stdout JSON, malformed JSON, non-zero exit и timeout дают безопасный структурированный ответ.
5. Timeout завершает процесс (`terminate`/`kill` + `wait`) и очищает временное состояние.
6. OAuth/access tokens отсутствуют в argv, env-дампах, stderr, логах и MCP response.
7. Sandbox блокирует доступ за пределы workspace.
8. Diff независимо проверяется основным Codex-агентом.

Тесты используют subprocess injection/mock и не требуют сети или browser login. Реальный OAuth smoke выполняется отдельно после ручного входа.

## Backup

`server.py`, `smoke_antigravity.py` и связанные тесты относятся к прежней Python SDK-ветке и не удаляются при развитии `agy_server.py`.
