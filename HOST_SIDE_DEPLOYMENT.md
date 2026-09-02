# Host-side deployment Antigravity executor

Дата: 2026-09-01

## Зачем нужен отдельный execution boundary

Авторизация Antigravity CLI принадлежит Windows-пользователю и его обычной
сессии ОС. Она может использовать системный keyring, штатный профиль CLI и
сетевой доступ, которые отсутствуют у shell sandbox. Поэтому сообщение CLI о
необходимости входа не всегда означает, что Gemini действительно разлогинен:
это может быть недоступный профиль, state directory или сеть.

Правильная схема:

```text
Codex agent
  └─ shell и операции с workspace: sandbox
  └─ local stdio MCP process: host trust component
       └─ agy child: тот же Windows identity и штатный keyring
            └─ внутренний agy --sandbox и permission engine остаются включены
```

`ANTIAGENT_EXECUTION_BOUNDARY=host` — декларация оператора, а не механизм
повышения привилегий. Wrapper проверяет только точное значение `host`. Если
переменная отсутствует или имеет другое значение, сервер продолжает работу для
совместимости, но пишет безопасное предупреждение и не заявляет OAuth readiness.

## Что гарантирует конфигурация Codex

Официальный Codex config поддерживает для stdio MCP поля `command`, `args`,
`cwd`, `env`, `env_vars` и `experimental_environment`. Значение
`experimental_environment = "local"` фиксирует local placement и не отправляет
stdio server в remote executor. Оно само по себе не доказывает доступ к keyring
и не выводит процесс из sandbox, наложенного внешним runtime.

- [Codex configuration reference](https://developers.openai.com/codex/config-reference/)
- [Codex MCP configuration](https://developers.openai.com/codex/mcp/)

Используйте [codex-host-mcp.example.toml](codex-host-mcp.example.toml) как inert
шаблон после установки `antiagent-mcp` через `pipx`. Перед применением замените
placeholder на абсолютный путь shim; `cwd` намеренно не задаётся.

## Обязательные свойства host process

1. MCP process и ручной `agy` работают под одним Windows SID.
2. User-level MCP запускается по заранее разрешённому абсолютному пути pipx
   shim, а не через поиск bare-команды из текущего project cwd.
3. Унаследованный process cwd является точным Git-root текущего проекта.
4. State root по умолчанию в `%LOCALAPPDATA%\antiagent` доступен только этому
   пользователю и допускает создание
   `agents.sqlite3`, `locks/` и `scratch/`.
5. `agy --version` успешно выполняется в том же host context.
6. Read-only MCP smoke в `mode=plan` завершается непустым `SUCCESS` без browser
   OAuth, повторного входа и изменений Git.

`agy --version` проверяет только наличие исполняемого файла. Доступ к
существующей OAuth-сессии подтверждает успешный authenticated `plan` либо
будущий официальный non-interactive `agy auth status --json`, если CLI
предоставит стабильный контракт.

Post-restart smoke 2 сентября 2026 года завершился `SUCCESS` без browser/re-auth:
marker `ANTIAGENT_POST_RESTART_OAUTH_OK_20260902`, CLI `1.1.24`, run
`56667d0c878b4a318efc2707525160d1`, `total_tokens=16565`, Git не изменён.

## Настройка

1. Откройте обычный PowerShell под тем Windows-пользователем, который уже
   авторизовал Antigravity/Gemini.
2. Проверьте CLI и установите переносимую MCP-команду из Git-root Antiagent:

   ```powershell
   agy --version
   py -m pip install --user pipx
   py -m pipx ensurepath
   py -m pipx install .
   Get-Command antiagent-mcp -ErrorAction Stop
   ```

3. Зарегистрируйте одну пользовательскую MCP-команду без `cwd`:

   ```powershell
   $AntiagentMcp = (Get-Command antiagent-mcp -CommandType Application -ErrorAction Stop).Source
   codex.cmd mcp add antigravity_cli_executor --env ANTIAGENT_EXECUTION_BOUNDARY=host -- $AntiagentMcp
   ```

4. Не держите конкурирующие регистрации одного MCP. Project-scoped шаблон
   нужен только для разработки самого Antiagent.
5. Полностью перезапустите Codex. State root и MCP environment фиксируются при
   старте процесса.
6. Проверьте регистрацию через `codex mcp get antigravity_cli_executor`.
   Команда подтверждает config, но не доступ к keyring.
7. Вызовите `antigravity_doctor`: local checks должны пройти, но
   `oauth_ready=unknown` останется ожидаемым до live smoke.
8. Выполните один live MCP smoke через `antigravity_cli_execute` или lifecycle
   tools: `mode=plan`, `thinking_level=low`, точный marker, без файловых правок.
9. Убедитесь, что marker непустой, Git не изменился и browser/re-auth не
   запускался. Только после этого boundary считается операционно подтверждённой.

## Что запрещено

- копировать `.gemini`, cookie, OAuth token или keyring между пользователями;
- экспортировать keyring в файл или environment variable;
- запускать MCP как `LocalSystem` или другого service account;
- отключать внутренний `agy --sandbox` или использовать
  `--dangerously-skip-permissions`;
- считать `ANTIAGENT_EXECUTION_BOUNDARY=host` доказательством без live smoke;
- автоматически запускать browser OAuth после `profile_unreadable` или
  `network_denied`.

## Диагностика

| Наблюдение | Machine code | Действие |
|---|---|---|
| Профиль недоступен процессу | `profile_unreadable` | Проверить placement и Windows identity |
| Штатный CLI profile нельзя изменить | `profile_not_writable` | Проверить host identity и доступ CLI к своему профилю |
| Wrapper DB state root недоступен | `state_unavailable` | Исправить `ANTIAGENT_STATE_DIR` и его ACL |
| Wrapper lock/scratch недоступен | `workspace_lock_unavailable` | Исправить `ANTIAGENT_STATE_DIR` и его ACL |
| Сеть запрещена runtime | `network_denied` | Проверить host network policy |
| Достоверно нет login | `auth_missing` | Авторизовать CLI вручную в host session |
| Интерактивный OAuth истёк | `oauth_timeout` | Повторить вручную вне sandbox |
| Boundary не объявлена | startup warning | Исправить placement/config и перезапустить |

Raw stderr, содержимое keyring, профиль и environment не должны попадать в MCP
result, SQLite или операторский отчёт.

## Если local stdio всё равно sandboxed

TOML и wrapper не могут снять ограничение, унаследованное от parent process.
Нужен отдельно запущенный same-user broker с аутентифицированным локальным
transport (например, защищённый named pipe). Это отдельное архитектурное
изменение; текущий stdio server к уже запущенному broker не подключается.
