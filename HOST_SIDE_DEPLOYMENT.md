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
шаблон. Не копируйте его в конфигурацию, пока не определены реальные абсолютные
пути и не подтверждено, что выбранный Codex runtime запускает local stdio MCP в
обычном пользовательском контексте.

## Обязательные свойства host process

1. MCP process и ручной `agy` работают под одним Windows SID.
2. `command`, `args`, `cwd`, `ANTIAGENT_STATE_DIR` и при необходимости
   `ANTIGRAVITY_CLI_PATH` — абсолютные локальные пути.
3. State root доступен только этому пользователю и допускает создание
   `agents.sqlite3`, `locks/` и `scratch/`.
4. `agy --version` успешно выполняется в том же host context.
5. Read-only MCP smoke в `mode=plan` завершается непустым `SUCCESS` без browser
   OAuth, повторного входа и изменений Git.

`agy --version` проверяет только наличие исполняемого файла. Доступ к
существующей OAuth-сессии подтверждает успешный authenticated `plan` либо
будущий официальный non-interactive `agy auth status --json`, если CLI
предоставит стабильный контракт.

## Настройка

1. Откройте обычный PowerShell под тем Windows-пользователем, который уже
   авторизовал Antigravity/Gemini.
2. Найдите абсолютные пути:

   ```powershell
   (Get-Command agy -ErrorAction Stop).Source
   (Get-Command python -ErrorAction Stop).Source
   agy --version
   ```

3. Создайте приватный state root вне sandbox-only каталога. Не помещайте туда
   токены или копии профиля.
4. Заполните inert TOML-шаблон и добавьте запись ровно в одну конфигурацию:
   project-scoped `.codex/config.toml` доверенного репозитория или user-level
   config. Не держите две конкурирующие регистрации одного MCP.
5. Полностью перезапустите Codex. State root и MCP environment фиксируются при
   старте процесса.
6. Проверьте регистрацию через `codex mcp get antigravity_cli_executor`.
   Команда подтверждает config, но не доступ к keyring.
7. Выполните один live MCP smoke через `antigravity_cli_execute` или lifecycle
   tools: `mode=plan`, `thinking_level=low`, точный marker, без файловых правок.
8. Убедитесь, что marker непустой, Git не изменился и browser/re-auth не
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
