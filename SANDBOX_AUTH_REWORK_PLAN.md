# План переделки Antigravity: host-side OAuth и корректная диагностика sandbox

Дата: 2026-09-01

Статус: этап A выполнен, этап B следующий

Основные компоненты: `agy_server.py`, `agent_manager.py`, MCP-конфигурация Codex

## 1. Цель

Устранить главный эксплуатационный отказ: Antigravity CLI уже авторизован через
Windows keyring, но при запуске внутри внешнего sandbox не может прочитать
профиль или обратиться к сети, ошибочно считает сессию отсутствующей и запускает
повторный интерактивный OAuth.

Целевая схема:

```text
Codex sandbox
    -> локальный MCP stdio
    -> host-side antiagent process
    -> preflight профиля/сети/CLI
    -> agy --sandbox
    -> Gemini
```

Внешний sandbox ограничивает Codex и его shell. Host-side MCP process получает
доступ к штатному пользовательскому профилю, Windows Credential Manager и сети.
Внутренний `agy --sandbox`, permission engine, Git-root validation и postflight
ограничивают доступ модели к workspace.

## 2. Не цели и запреты

- Не копировать OAuth-токены, cookies, keyring exports или credentials в
  workspace, временные review packets, переменные MCP request или логи.
- Не добавлять API-key fallback.
- Не отключать `agy --sandbox` и не использовать
  `--dangerously-skip-permissions`.
- Не трактовать approval-policy как auth failure и не обходить approval gate.
- Не выполнять автоматический `git reset`, rollback, commit или push.
- Не обещать, что Python wrapper может самостоятельно перенести процесс через
  trust boundary: host-side способ запуска задаётся владельцем Codex runtime.

## 3. Подтверждённая цепочка отказа

1. OAuth-сессия существует в системном keyring.
2. Внешний sandbox запрещает доступ к профилю, crash/state каталогам либо сети.
3. CLI получает `Access denied`, socket denial или ошибку summary store.
4. Ошибка сворачивается в `silent auth failed`.
5. CLI запускает interactive OAuth в headless process.
6. Вызов завершается общим timeout и выглядит как истёкшая авторизация.

Это нужно исправлять двумя независимыми мерами:

- правильной границей запуска — MCP/`agy` на host-side;
- правильной диагностикой — profile/network denial не запускает и не предлагает
  повторный OAuth.

## 4. Инварианты безопасности

1. Wrapper никогда не читает содержимое keyring и OAuth-файлов.
2. Диагностика профиля проверяет только путь и доступность, без вывода имён
   внутренних файлов.
3. Raw `stderr`, prompt и environment не возвращаются через MCP и не пишутся в
   application log.
4. Классификатор возвращает только allowlisted machine code.
5. Любой editing-run остаётся под workspace lock и Git postflight.
6. `plan` остаётся режимом по умолчанию.
7. Неизвестный отказ остаётся `cli_error`; он не превращается в ложный
   `auth_missing`.

## 5. Целевой MCP-контракт

### 5.1. Диагностические коды

Добавить к `error_type`:

- `profile_unreadable` — профиль/keyring-related state недоступен процессу;
- `profile_not_writable` — обязательный CLI state нельзя записать;
- `network_denied` — socket/endpoint заблокирован sandbox/firewall;
- `auth_missing` — CLI достоверно сообщил об отсутствии сохранённой сессии;
- `oauth_timeout` — интерактивный OAuth стартовал, но не завершился;
- `permission_denied` — headless tool permission soft/hard deny;
- `policy_denied` — внешний или CLI policy gate отказал в payload;
- `no_content` — CLI сообщил SUCCESS, но не вернул содержательный ответ.

Приоритет первопричин при нескольких сообщениях:

```text
policy > profile > network > permission > oauth_timeout > auth_missing > timeout
```

Например, `network denied -> silent auth failed -> OAuth timeout` возвращает
`network_denied`, а не `oauth_timeout`.

### 5.2. Диагностический tool

Добавить `antigravity_doctor` после стабилизации CLI contract. Он должен
возвращать только:

- найден ли абсолютный executable и его версия;
- какой runtime/state root разрешён wrapper;
- доступен ли заявленный profile directory: `unknown|missing|readable|denied`;
- является ли process host-side по явной конфигурации оператора;
- готов ли Git/workspace preflight;
- какие проверки невозможно выполнить без обращения к CLI или сети.

Doctor не должен запускать OAuth, читать keyring, печатать username, полный путь
профиля или делать произвольный сетевой запрос.

## 6. Этапы реализации

### Этап A — строгая диагностика результата (P0, выполнен)

Файлы: `agy_server.py`, `test_agy_server.py`, `test_mcp_protocol.py`.

- [x] Сохранять bounded `stderr` только внутри `CliRunResult`.
- [x] Классифицировать только финальный failed/timeout result.
- [x] Не логировать и не возвращать raw `stderr`.
- [x] Добавить перечисленные machine codes в Pydantic/MCP schema.
- [x] Отклонять пустой `SUCCESS.response` как `no_content`.
- [x] Оставлять неизвестный failure как `cli_error`.
- [x] Добавить precedence tests на составные цепочки ошибок.

Критерий приёмки: deterministic fixture воспроизводит каждый класс; sentinel из
stderr отсутствует в result, logs и structured snapshot.

### Этап B — настраиваемый wrapper state root (P0)

Файлы: `agy_server.py`, `agent_manager.py`, тесты.

- [ ] Ввести единый `ANTIAGENT_STATE_DIR` с абсолютным путём.
- [ ] Разместить под ним `locks/`, `agents.sqlite3` и review markers либо явно
      документировать исключения.
- [ ] Не доверять относительному пути, symlink/junction наружу или обычному
      файлу вместо каталога.
- [ ] Возвращать typed `state_unavailable`, не маскировать его под auth.
- [ ] Сохранить безопасный platform default для запусков без переменной.
- [ ] Добавить Windows regression: недоступный системный TEMP не ломает запуск
      при заданном `ANTIAGENT_STATE_DIR`.

Критерий приёмки: suite проходит с запрещённым default TEMP и доступным явным
state root; никаких credentials в новом каталоге нет.

### Этап C — host-side deployment contract (P0)

Файлы: новый deployment-раздел документации; `.codex/config.toml` только после
review существующих пользовательских изменений.

- [ ] Явно назвать MCP process host-side trust component.
- [ ] Добавить startup self-check `ANTIAGENT_EXECUTION_BOUNDARY=host`.
- [ ] Если boundary не объявлена, не утверждать готовность OAuth; выдавать
      диагностическое предупреждение, но сохранить совместимость.
- [ ] Описать, что sandbox runtime должен разрешать запуск локального stdio MCP
      за пределами shell sandbox.
- [ ] Запретить рекомендации по копированию `.gemini` или keyring export.
- [ ] Добавить операторскую проверку: одинаковый Windows identity, доступ к
      штатному профилю, успешный `agy --version`, затем live `plan` smoke.

Критерий приёмки: при host-side запуске используется существующий keyring без
повторного OAuth; при sandboxed запуске возвращается profile/network code.

### Этап D — CLI auth/profile preflight (P0, зависит от CLI)

- [ ] Предпочесть официальный `agy auth status --json`/`agy doctor --json`, если
      CLI предоставит стабильный non-interactive contract.
- [ ] До появления официального contract не парсить keyring и не изобретать
      undocumented home variables.
- [ ] Не запускать browser OAuth автоматически после profile/network denial.
- [ ] Отделить optional telemetry failure от обязательного Gemini endpoint.
- [ ] Добавить короткий bounded probe без Playwright.

Критерий приёмки: пять искусственных сценариев — missing auth, unreadable
profile, non-writable state, denied network, OAuth timeout — возвращают разные
коды и никогда не зависают до полного task timeout без точной причины.

### Этап E — payload manifest и узкие permissions (P1)

- [ ] Разделить `prompt_only` и `scoped_files`.
- [ ] Добавить dry-run manifest: relative path, bytes, hash, destination.
- [ ] Требовать отдельное approval для приватного external payload.
- [ ] Поддержать `allow_read`, `allow_read_tree`, `allow_write`, `deny_shell`.
- [ ] Не предоставлять весь workspace через `--add-dir`, если задача получила
      только review packet.
- [ ] Возвращать denied tool, безопасные аргументы и требуемое разрешение.

Критерий приёмки: задача с одним разрешённым файлом не может читать соседний
файл или запускать shell; approval manifest совпадает с фактическим payload.

### Этап F — budget и строгий success contract (P1)

- [ ] Добавить лимиты `max_files`, `max_context_bytes`, `max_input_tokens`,
      `max_tool_calls`.
- [ ] Публиковать usage даже для `no_content` и failed runs.
- [ ] Разрешить expected marker или JSON Schema для проверяемых задач.
- [ ] Не принимать `metadata_complete=true` как доказательство полезного ответа.
- [ ] Отдельно учитывать cache-read и tool-generated context.

Критерий приёмки: пустой ответ и выход за budget завершаются typed error до
финального acceptance; run не может незаметно прочитать весь repository.

### Этап G — lifecycle и concurrency (P2)

- [ ] После рестарта немедленно завершать orphaned local tasks как
      `manager_lost`, либо ввести полноценный lease/heartbeat recovery.
- [ ] Разделить exclusive edit lock и shared read-only lease.
- [ ] Capacity учитывать per workspace и per owner/session.
- [ ] Сохранить terminal immutability и cross-process cancellation.

## 7. Тестовая матрица

| Сценарий | Ожидаемый результат |
|---|---|
| Profile `Access denied`, затем auth timeout | `profile_unreadable` |
| Socket forbidden, затем OAuth timeout | `network_denied` |
| Только достоверный `not logged in` | `auth_missing` |
| Только interactive OAuth timeout | `oauth_timeout` |
| `soft-denying tool confirmation` | `permission_denied` |
| Approval gate rejection | `policy_denied` |
| SUCCESS + пустой response | `no_content` |
| Неизвестный non-zero result | `cli_error` |
| Sentinel в stderr | Sentinel нигде не возвращается и не логируется |
| Недоступный default TEMP + явный state root | Запуск и lock работают |
| Два editing process одного workspace | Один владеет lock |
| Два read-only process после shared lease | Оба могут выполняться |

## 8. Порядок rollout

1. Добавить regression tests для Этапа A.
2. Реализовать внутренний stderr classifier и `no_content`.
3. Прогнать deterministic suite и MCP STDIO contract.
4. Реализовать `ANTIAGENT_STATE_DIR`.
5. Обновить документацию host-side boundary после отдельного review dirty
   файлов пользователя.
6. Выполнить один разрешённый live `plan` smoke.
7. Только после успешного `plan` выполнить изолированный `accept-edits` smoke.

## 9. Финальная проверка

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe -m py_compile agy_server.py agent_manager.py
.\.venv\Scripts\python.exe -m pip check
git diff --check
git status --short
```

Дополнительно:

- два последовательных прогона suite;
- MCP `initialize`, `tools/list` и каждый lifecycle tool;
- filename-only tracked-secret scan;
- проверка отсутствия raw stderr/prompt/environment в SQLite и MCP result;
- live smoke фиксирует CLI version, run ID, status, usage и неизменный Git.

## 10. Definition of Done

- Существующая OAuth-сессия используется без повторной авторизации при
  host-side запуске.
- Sandbox/profile/network failures различаются машинными кодами.
- Profile/network denial никогда не маскируется как `auth_missing`.
- Wrapper не читает и не копирует credentials.
- Raw stderr не выходит за границу процесса.
- Пустой SUCCESS не принимается как выполненная работа.
- State/lock paths работают в ограниченной Windows-среде.
- Все deterministic tests и MCP STDIO tests проходят.
- Документация ясно различает внешний sandbox и внутренний `agy --sandbox`.
