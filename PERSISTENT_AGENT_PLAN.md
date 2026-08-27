# План: Antigravity как исполнитель Codex-субагентов

Дата: 27.08.2026
Статус: в работе
Базовый commit: `2e35462331d43bc8ab58053a0cb498c505b2350b`

## Цель

Сохранить Codex главным оркестратором, владельцем UI, потоков субагентов, разрешений,
review и тестов, а ограниченные coding-задачи выполнять через уже работающий и
локально авторизованный Gemini/Antigravity MCP.

Полная подмена внутреннего runtime Codex невозможна через публичную MCP-схему:
нативные thread UI, forked context, sandbox/approval lifecycle и системные
`spawn/follow-up/wait/interrupt` остаются функциями клиента Codex. Достижимая
замена — project-scoped Codex-agent `antigravity_worker`, который сохраняет
нативный поток Codex, но передаёт фактическое выполнение в Antigravity.

## Что уже подтверждено

- Текущий `antigravity_cli_execute` доступен и успешно выполнил реальную задачу
  в `plan` через `gemini-3.7-flash-low`.
- `agy 1.1.22` поддерживает продолжение с `--conversation <id>`.
- Codex загружает project-scoped custom agents из `.codex/agents/*.toml`.
- Обязательные поля custom agent: `name`, `description`,
  `developer_instructions`.
- Модель, reasoning, sandbox и MCP allowlist можно задавать в том же TOML;
  отдельного поля для единственного разрешённого tool нет — используется
  `mcp_servers.<id>.enabled_tools`.
- Корень Git чист; удалённая legacy SDK-ветка уже отсутствует. `.env`, `.venv`,
  `_mcp_protocol_fixture.py` и текущие отчёты не являются мусором.

## Минимальная архитектура

```text
Codex root / UI
  -> native custom subagent: antigravity_worker
    -> MCP lifecycle tools
      -> asyncio task in the current MCP process
        -> existing execute_with_antigravity_cli()
          -> agy CLI / Gemini
```

Новый daemon, очередь сообщений и сторонняя БД не нужны. Менеджер использует
один существующий MCP-процесс, `asyncio` и встроенный `sqlite3`.

## Persistent state

SQLite хранится в пользовательском каталоге состояния (`LOCALAPPDATA` на
Windows, `XDG_STATE_HOME`/`~/.local/state` на POSIX). Каталог и файл создаются с
приватными правами там, где ОС поддерживает POSIX mode.

Хранятся только:

- `agent_id`, `parent_agent_id`, `owner_id`;
- canonical workspace, mode, thinking level;
- `queued|running|completed|failed|interrupted`;
- timestamps и `cancel_requested`;
- полученный `conversation_id`;
- ограниченный structured result существующего executor.

Не хранятся исходные `task`, `context`, `verification`, credentials и stderr.
После рестарта доступны история, результат и follow-up завершённой conversation.
Незавершённый prompt намеренно не восстанавливается: зависшая запись после
максимального runtime помечается `failed/manager_lost`.

## MCP lifecycle tools

1. `antigravity_agent_spawn` — валидирует запрос, сохраняет `queued`, запускает
   background task и сразу возвращает `agent_id`.
2. `antigravity_agent_list` — bounded список безопасных snapshots с фильтром по
   status/workspace.
3. `antigravity_agent_status` — текущий snapshot и terminal result.
4. `antigravity_agent_wait` — ждёт terminal state ограниченное число секунд;
   timeout ожидания не отменяет агент.
5. `antigravity_agent_followup` — создаёт дочерний run с conversation ID
   завершённого родителя.
6. `antigravity_agent_interrupt` — идемпотентно выставляет cancel flag;
   локальная или другая живая MCP-копия замечает его и отменяет процесс tree.

Синхронный `antigravity_cli_execute` сохраняется для совместимости и простых
одноразовых вызовов.

## Состояния и переходы

| Было | Событие | Стало |
|---|---|---|
| — | корректный spawn | `queued` |
| `queued` | execution начинает существующий executor | `running` |
| `queued` / `running` | interrupt/cancel flag | `interrupted` |
| `running` | executor `SUCCESS` | `completed` |
| `running` | executor `ERROR`/exception | `failed` |
| `queued` / `running` | stale дольше max runtime | `failed/manager_lost` |

Terminal states неизменяемы. Follow-up создаёт новый `agent_id` и не изменяет
историю родителя.

## Этапы с отдельным commit/push

### 1. Контракт и план

- [x] Проверить официальную схему Codex custom agents.
- [x] Проверить доступность старого Antigravity MCP на реальном простом запросе.
- [x] Зафиксировать архитектуру, границы и acceptance в этом файле.

### 2. Conversation resume и durable store

- [x] Добавить безопасный optional `conversation_id` в общий execution path.
- [x] Добавить stdlib SQLite store с атомарными transitions и bounded reads.
- [x] Не сохранять исходный prompt/context/verification.
- [x] Покрыть schema, transitions, persistence и stale reconciliation тестами.

### 3. Lifecycle MCP API

- [x] Добавить шесть lifecycle tools.
- [x] Реализовать background execution, bounded wait и cross-process cancel flag.
- [x] Реализовать follow-up через `--conversation`.
- [x] Проверить raw STDIO `tools/list` и реальные последовательности
  spawn/status/wait/followup/interrupt.

### 4. Codex custom agent и правила маршрутизации

- [ ] Добавить `.codex/agents/antigravity_worker.toml`.
- [ ] Разрешить worker только Antigravity lifecycle/execute MCP tools.
- [ ] Обновить `AGENTS.md`: дешёвые ограниченные coding-задачи сначала
  делегируются `antigravity_worker`; архитектура, security, destructive work,
  секреты и final review остаются у Codex.
- [ ] Обновить `.codex/config.toml` allowlist.

### 5. Acceptance, документация и уборка

- [ ] Два последовательных deterministic test passes и reverse-order pass.
- [ ] `py_compile`, `pip check`, TOML parse, `git diff --check`.
- [ ] Реальный MCP STDIO lifecycle run.
- [ ] Один OAuth smoke через manager в `plan`; editing smoke — только в
  изолированном временном Git-репозитории с review diff.
- [ ] Обновить operator guide, technical guide, test matrix/report и исходный
  audit report.
- [ ] Удалять только доказанный generated/cache мусор; `.env` и `.venv`
  сохранить локально, tracked test fixture и отчёты сохранить.
- [ ] Проверить clean worktree и совпадение `main` с `origin/main`.

## Definition of Done

- Codex видит `antigravity_worker` после перезапуска сессии.
- Worker может spawn, wait, interrupt и продолжить terminal conversation.
- Менеджер не теряет terminal status/result после нового Python process.
- Один workspace по-прежнему защищён межпроцессным lock.
- Partial edits продолжают требовать явный review acknowledgement.
- Все MCP validation/runtime failures остаются redacted и typed.
- Документация содержит готовое правило для вставки в `AGENTS.md` и честно
  перечисляет отличия от нативных Codex-субагентов.

## Осознанные ограничения

- После аварийного завершения MCP нельзя продолжить уже запущенный OS-процесс:
  для этого нужен отдельный supervisor/daemon, который сейчас не оправдан.
- Gemini не получает нативный Codex forked transcript: worker передаёт ему
  только явно заданные task/context/verification.
- MCP не может самостоятельно создать нативный Codex UI-thread; его создаёт
  родительский Codex через custom agent.
- Автоматическое делегирование определяется инструкциями и выбором модели, а не
  технически неотключаемым hook. Правило делается максимально явным, но root
  Codex всё равно обязан контролировать применимость и результат.

## Официальные источники Codex

- [Custom subagents](https://developers.openai.com/codex/subagents)
- [MCP configuration](https://developers.openai.com/codex/mcp)
- [Config reference](https://developers.openai.com/codex/config-reference)
