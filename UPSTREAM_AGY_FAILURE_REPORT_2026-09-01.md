# Архивный отчёт о сбоях Antigravity CLI и Antiagent

Дата: 2026-09-01
Проверенная версия CLI: `1.1.22`

Этот файл перенесён из репозитория проекта, на котором впервые воспроизводились
сбои. Проектные имена и локальные checkout-пути обезличены; исходные временные
логи не входят в репозиторий Antiagent. Приведённые ниже короткие сигнатуры
сохранены как исторические доказательства для разработчика wrapper/CLI.

## Назначение отчёта

Этот документ фиксирует воспроизводимые отказы Antigravity CLI при запуске из
агентной среды Windows и изменения, необходимые для надёжного использования CLI
как автоматического субагента. Секреты, OAuth-коды и токены в отчёт не включены.

Проверка показала четыре независимых класса проблем:

1. внешний sandbox запрещает CLI доступ к OAuth-профилю и сети;
2. CLI недостаточно точно обрабатывает недоступный профиль и процесс
   авторизации;
3. headless permission-модель мешает ограниченному read-only аудиту;
4. передача приватных файлов может быть отдельно остановлена approval-policy
   оркестратора до вызова модели.

Последний пункт не является ошибкой OAuth или модели, и его нельзя смешивать с
ошибками самого CLI.

## 1. Служебный профиль недоступен в sandbox

В sandbox CLI пытался писать в профиль
`C:\Users\<user>\.gemini\antigravity-cli`, несмотря на переданный `--log-file`
в рабочем каталоге.

Зафиксированные сообщения:

```text
Failed to initialize crash reporter: failed to setup crash output:
open C:\Users\<user>\.gemini\antigravity-cli\crashes\crash_....log:
Access is denied.
```

Источник: `tmp/agy-first-screen-authorized.log`, строка 2.

```text
summary store open failed (corrupt_on_open), recreating:
summary_store: open db: unable to open database file

summary store recreate failed:
summary_store: open db: unable to open database file
```

Источник: тот же лог, строки 66–67.

```text
Failed to resolve GeminiDir ".gemini":
.gemini must be an absolute path: path is not absolute, falling back to default
```

Источник: `tmp/agy-models-20260901.log`, строка 56.

### Вывод

Флаг `--log-file` перенаправляет основной журнал, но не управляет crash output,
SQLite summary store, settings, cache, keyring и очисткой старых логов. Поэтому
одного доступного файла лога недостаточно для запуска в ограниченной среде.

### Требуемое исправление

- добавить единый `--app-data-dir` либо отдельные `--config-dir`, `--state-dir`,
  `--cache-dir` и `--crash-dir`;
- принимать абсолютный путь и показывать фактически разрешённые пути через
  `agy doctor`;
- при read-only профиле отключать необязательные writer-компоненты, а не
  диагностировать профиль как повреждённый;
- гарантировать, что `--log-file` не оставляет неожиданные журналы в другом
  каталоге.

## 2. Сетевой запрет ошибочно выглядит как отсутствие авторизации

Внешний sandbox запрещал загрузку Playwright и сетевые обращения:

```text
failed to install playwright ...
connectex: An attempt was made to access a socket in a way forbidden by its
access permissions
```

Источник: `tmp/agy-ui-runtime-prepush.log`, строки 102–104.

```text
Post "https://play.googleapis.com/log" ...
socket ... forbidden by its access permissions
```

Источник: тот же лог, строки 105–126.

После сетевого отказа наружу выходила цепочка:

```text
Print mode: silent auth failed
Print mode: triggering interactive OAuth
Print mode: auth timed out
```

Источник: строки 98–99 и 128.

### Вывод

Первичной причиной являлась недоступность профиля или сети, но итоговая
диагностика сводилась к общей проблеме входа.

### Требуемое исправление

- ввести отдельные машинные коды `AUTH_MISSING`, `PROFILE_UNREADABLE`,
  `PROFILE_NOT_WRITABLE`, `NETWORK_DENIED` и `OAUTH_TIMEOUT`;
- перед OAuth выполнять preflight профиля и обязательных сетевых адресов;
- не загружать Playwright для headless-сессии с действующим keyring;
- не делать telemetry endpoint обязательным для авторизации;
- выводить одну корневую причину вместо повторяющихся производных ошибок.

## 3. Гонка при начальной авторизации и ложный error-spam

Даже успешный запуск сначала многократно сообщал:

```text
You are not logged into Antigravity
Auth mode is unspecified, skipping fetchAvailableModels
```

Позже в том же процессе успешно выполнялись:

```text
keyringAuth: loaded token ... expired=false
ChainedAuth: authenticated via keyring
OAuth: authenticated successfully as <redacted>
URL: https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels
Propagating selected model override to backend:
label="Gemini 3.7 Flash (High)"
```

Источник: `tmp/agy-models-20260901.log`, строки 7 и 78–89.

### Вывод

Pollers моделей, экспериментов и user info запускаются раньше завершения
silent-auth. Кроме того, префикс `ERROR: logging before google.Init` применяется
даже к строкам уровней INFO и WARN, что делает успешный запуск похожим на
аварийный.

### Требуемое исправление

- завершать keyring/silent-auth до запуска фоновых pollers;
- добавить нейтральное состояние `AUTH_INITIALIZING`;
- после успешного silent-auth не повторять устаревшие `not logged in`;
- дедуплицировать параллельные refresh-запросы;
- инициализировать logging до старта компонентов и сохранять настоящий уровень
  каждой записи.

## 4. Доказанный успешный запуск вне внешнего sandbox

Вне внешнего sandbox CLI прочитал keyring, восстановил OAuth, загрузил список
моделей и выбрал `Gemini 3.7 Flash (High)`. В другом запуске зарегистрированы
реальные запросы `streamGenerateContent` с `ResponseID`.

Источники:

- `tmp/agy-models-20260901.log`, строки 78–89;
- `tmp/agy-welcome-review-3-7.log`, строки 164–169.

Это доказывает, что аккаунт, сохранённая OAuth-сессия и сама модель были
работоспособны. Ошибки sandbox-запусков нельзя трактовать как общий отказ
Antigravity service.

Проверенный диагностический вызов:

```powershell
agy models --log-file <git-root>\tmp\agy-models.log
```

Шаблон ограниченного read-only аудита:

```powershell
agy --print "Контекст: @relative/file. Ничего не меняй и не вызывай shell; верни краткий аудит" --mode plan --model gemini-3.7-flash-high --output-format text --print-timeout 2m --log-file <git-root>\tmp\agy-audit.log
```

## 5. Несогласованность `--model` и `--effort`

При передаче модели с уже закодированным уровнем High вместе с отдельным effort
зафиксировано:

```text
failed to apply model override: failed to resolve effort:
--effort is not supported for model "gemini-3.7-flash-high"
```

Источник: `tmp/agy-welcome-review-3-7-interactive.log`, строка 87.

Одновременно `agy --help` предлагает общий флаг
`--effort low|medium|high`, но не показывает его совместимость с моделями.

### Требуемое исправление

- выбрать одну каноническую схему: базовый model ID плюс `--effort` либо полный
  model ID без отдельного effort;
- возвращать из `agy models --json` поля `supports_effort`, допустимые значения
  и канонический ID;
- диагностировать конфликт до запуска server/backend;
- не сообщать сначала `model not in local config, defaulting to CCPA`, если
  после авторизации эта же модель успешно разрешается.

## 6. Headless permission-модель блокирует read-only работу

Даже в `plan`-режиме зафиксировано:

```text
Print mode: soft-denying tool confirmation "ListDir" at step 10
```

Источник: `tmp/agy-welcome-review-3-7.log`, строка 170.

После soft-deny не гарантируются полезный финальный ответ и однозначный
ненулевой exit code. Явно переданный `@relative-file` также может привести к
запросу `ListDir` или `RunCommand`, хотя задача требует чтения одного файла.

### Требуемое исправление

- добавить `--allow-read <file>`, `--allow-read-tree <directory>`,
  `--allow-write <file>` и `--deny-shell`;
- выполнять permission preflight до обращения к модели;
- возвращать ненулевой exit code при denied tool, если содержательный финальный
  ответ не сформирован;
- возвращать имя инструмента, безопасные аргументы, требуемое узкое разрешение
  и причину отказа;
- загружать `@relative-file` host-обёрткой без обязательного `ListDir` и shell;
- не использовать `--dangerously-skip-permissions` как штатный headless
  workaround.

## 7. Approval-policy для приватных исходников — отдельный барьер

Во время UI-аудита было зафиксировано:

```text
approval gate отклонил передачу локальных UI-файлов внешнему сервису без
отдельного явного согласия пользователя
```

Источник: обезличенная запись журнала исходного проекта от 2026-09-01.

Повторный запуск вне sandbox также не дошёл до модели:

```text
approval-review запретил передачу перечисленных приватных файлов во внешний
Google service без отдельного явного согласия на payload
```

Источник: обезличенная запись журнала исходного проекта от 2026-09-01.

### Вывод

Это решение внешнего оркестратора до отправки project context. Оно не доказывает
отказ OAuth, CLI или модели. Обход approval-review недопустим.

### Улучшение интеграции

- добавить `agy context --dry-run --json` со списком файлов, объёмом, hashes и
  destination;
- разрешить передавать заранее обезличенный review packet через stdin;
- разделить режимы `prompt-only` и `files-upload`;
- формировать машинно читаемый manifest внешнего payload для пользовательского
  согласия;
- поддержать одноразовое разрешение только конкретных файлов.

## 8. Логи и API процесса неудобны для агентной оркестрации

После успешной интерактивной сессии также зарегистрировано:

```text
cleanStaleLogs: failed to read dir log:
open log: The system cannot find the file specified
```

Источник: `tmp/agy-welcome-review-3-7-interactive.log`, строка 208.

Внутренние логи, onboarding, ANSI-последовательности и финальный ответ могут
смешиваться, поэтому вызывающий агент не всегда может доказать результат по
stdout и exit code.

### Требуемое исправление

- добавить `agy auth status --json` и `agy doctor --json`;
- оставлять в stdout только результат, а diagnostics направлять в stderr;
- предоставить стабильный JSONL/NDJSON agent protocol;
- возвращать `run_id`, `conversation_id`, `resolved_model`, `auth_method`,
  `permission_requests`, `files_read`, `files_changed`, `final_status` и
  `exit_code`;
- различать `completed`, `completed_with_denials`, `blocked_auth`,
  `blocked_policy`, `timeout` и `model_error`;
- поддержать scoped patch output вместо неограниченной записи;
- гарантировать отсутствие ANSI при `--output-format json` и `stream-json`.

## 9. Приоритет исправлений

### P0 — блокирует автоматическое применение

1. Настраиваемый app-data/profile directory.
2. Раздельные exit codes для auth, profile, network и policy.
3. Headless read/write allowlist и permission preflight.

### P1 — существенно снижает надёжность

1. Устранение auth startup race и ложного error-spam.
2. Чистый JSONL agent protocol.
3. `auth status`, `doctor` и payload dry-run.

### P2 — улучшает предсказуемость и сопровождение

1. Нормализация `--model` и `--effort`.
2. Lazy/optional Playwright и telemetry.
3. Scope-lock для параллельных edit-сессий.

## Практический итог

В текущем состоянии Antigravity можно использовать как субагента при
одновременном выполнении четырёх условий:

- процесс запущен вне внешнего sandbox, блокирующего OAuth-профиль и сеть;
- доступен штатный профиль с сохранённым keyring;
- внешний payload явно разрешён либо не содержит приватных project files;
- задача получает ограниченный относительный `@file`-контекст и самостоятельно
  перепроверяется основным агентом.

Для полностью автоматической и безопасной оркестрации CLI пока не хватает
управляемого профиля, узкой permission allowlist и строгого машинного протокола.

## Дополнение 2026-09-02 — причина `program not found` и исправление Antiagent

### Наблюдаемый отказ

Нативный субагент Codex останавливался ещё до выполнения задачи:

```text
Fatal error: Failed to initialize session: required MCP servers failed to initialize:
antigravity_cli_executor: program not found
```

При этом прямой host-side запуск `agy` проходил OAuth и короткий read-only
smoke. Следовательно, ошибка находилась не в OAuth и не в доступности модели, а
на границе запуска MCP (Model Context Protocol — протокол подключения внешнего
инструмента к Codex).

### Подтверждённая первопричина

1. `antiagent-mcp.exe` был установлен pipx, но каталог pipx shim отсутствовал в
   `PATH` процесса Codex.
2. User config и trusted-project `.codex/config.toml` запускали bare-команду
   `antiagent-mcp`, поэтому Windows не мог найти executable.
3. Project config имел приоритет над user config. Даже исправленная
   пользовательская регистрация снова перекрывалась bare-командой проекта.
4. `ANTIAGENT_EXECUTION_BOUNDARY=host` является проверяемой декларацией границы
   для wrapper, но не превращает sandbox-процесс в host-процесс и не выдаёт ему
   доступ к профилю/сети.

### Исправление в Git-репозитории Antiagent

Коммит: `031adbb` — `Исправить переносимую регистрацию MCP (Antiagent/Codex setup)`.

- добавлен `antiagent-codex-install` / `python -m antiagent_setup`;
- launcher ищется по runtime environment (`ANTIAGENT_MCP_PATH`, sibling console
  script, `PIPX_BIN_DIR`, user profile, затем `PATH`) и сохраняется абсолютным;
- Codex CLI ищется через `CODEX_CLI_PATH`, установленный Codex App path и `PATH`;
- регистрация выполняется штатными `codex mcp remove/add/get` без фиксированного
  checkout и без `cwd`;
- project-level MCP block удалён, чтобы trusted project не перекрывал user-level
  абсолютный launcher;
- `smoke_mcp.py` также запускает абсолютный executable;
- добавлены regression-тесты разрешения путей, аргументов регистрации и
  отсутствия project override; документация переведена на универсальный
  установщик.

Код не содержит имени Windows-пользователя, буквы диска или пути конкретного
проекта. Абсолютный путь возникает только локально при исполнении установщика.

### Проверки после исправления

```text
Ran 124 tests in 32.646s
OK (skipped=2)
```

Повторный stdio smoke после восстановления локальной pipx-установки:

```json
{
  "tools": [
    "antigravity_agent_followup",
    "antigravity_agent_interrupt",
    "antigravity_agent_list",
    "antigravity_agent_spawn",
    "antigravity_agent_status",
    "antigravity_agent_wait",
    "antigravity_cli_execute",
    "antigravity_doctor"
  ],
  "doctor": {
    "checks_passed": true,
    "cli_available": true,
    "cli_version": "1.1.24",
    "execution_boundary_declared": true,
    "state_writable": true,
    "workspace_status": "ready"
  }
}
```

Реальный provider/OAuth lifecycle smoke:

```json
{"status":"ok","marker_found":true,"git_status_unchanged":true,"workspace_is_git_root":true,"managed_lifecycle":true,"thinking_level":"low","mode":"plan"}
```

### Дополнение 2026-09-02 — устранение оставшихся wrapper-сбоев

Коммит Antiagent: `6e024bb` —
`Устранить сбои обновления и схемы запуска (Antiagent runtime)`.

- добавлен source-checkout модуль `antiagent_upgrade.py`;
- перед любым `pipx install --force` модуль перечисляет процессы через Windows
  Toolhelp API и fail-closed останавливается, если жив `antiagent-mcp.exe` либо
  проверка процессов невозможна;
- guard не завершает процессы автоматически и не начинает изменение pipx до
  успешного preflight;
- после успешного pipx он обновляет абсолютную Codex MCP registration;
- из публичной MCP-схемы удалены неподдерживаемые `prompt_only` и
  `scoped_files`: `agy 1.1.24` не имеет file allowlist/mandatory deny-shell и
  не может честно обеспечить эти режимы;
- protocol/packaging regression-тесты синхронизированы с восемью tools и
  фактическими input/output fields;
- Antiagent skill и инструкции обновлены единым lifecycle/retry контрактом.

Полный offline regression:

```text
Ran 130 tests in 33.386s
OK (skipped=2)
```

Live dry-run нового guard в текущей активной Codex-сессии обнаружил семь
процессов `antiagent-mcp.exe` и завершился до `pipx`, то есть воспроизведённый
ранее `WinError 32` больше не возникает в штатном upgrade workflow.

Три параллельных provider/OAuth audit-run через MCP завершились `SUCCESS` на
`agy 1.1.24`, включая `low`, `medium` и `high` thinking, с terminal lifecycle и
неизменённым worktree. Первая попытка с `payload_mode=scoped_files` была
отклонена до CLI как неподдерживаемая; именно поэтому режим удалён из публичной
схемы версии `0.2.2`.

### Оставшиеся upstream-ограничения

- Codex загружает MCP snapshot при старте. Обновлённый package/schema появится
  только после полного закрытия и нового запуска Codex CLI/app/IDE; новый
  субагент внутри старой сессии snapshot не обновляет.
- `agy 1.1.24` не предоставляет file allowlist и deny-shell. Относительные
  `@file` и запрет tools в prompt — best effort, не security boundary.
- Headless `permission_denied` нельзя безопасно обходить
  `--dangerously-skip-permissions`. Нужно передать достаточный inline context,
  выполнить интерактивный запуск с узким разрешением либо перейти к native
  subagent.
- Интерактивный provider-run может зависнуть на генерации. Bounded wait,
  `status`, затем `interrupt` предотвращают бесконечное ожидание, но не чинят
  upstream UX.

### Каноническая команда обновления на Windows

После полного закрытия всех клиентов Codex, из Git-root Antiagent:

```powershell
py -m antiagent_upgrade
```

После запуска нового Codex выполнить `antigravity_doctor` и один read-only
lifecycle smoke с `expected_marker`. Секреты, OAuth-коды, email профиля и
содержимое keyring в диагностике и этом отчёте намеренно отсутствуют.
