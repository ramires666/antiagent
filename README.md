# Antiagent: Antigravity для Codex

Этот проект подключает OAuth-аутентифицированный Antigravity CLI к Codex через
локальный stdio MCP process. Codex управляет задачей, разрешениями, review и
тестами, а Gemini/Antigravity выполняет небольшую leaf coding-задачу.

Исторические воспроизводимые сигнатуры отказов CLI, sandbox, OAuth и headless
permissions собраны в
[`UPSTREAM_AGY_FAILURE_REPORT_2026-09-01.md`](UPSTREAM_AGY_FAILURE_REPORT_2026-09-01.md).

В репозитории уже находятся:

- MCP server `agy_server.py`;
- persistent manager с lifecycle tools;
- безопасная live-телеметрия wrapper-этапов и `agy stream-json`;
- переносимый установщик `antiagent-codex-install`;
- custom Codex agent `.codex/agents/antigravity_worker.toml`;
- repo-scoped skill `.agents/skills/antigravity-executor/SKILL.md`.

## 1. Подготовьте Antigravity и Python

Установите официальный Antigravity CLI, запустите `agy` и завершите OAuth-вход
в браузере. API key этому проекту не нужен. Подробная установка и модель
безопасности описаны в
[инструкции executor](<Инструкция_ Antigravity CLI OAuth Executor для Codex.md>).

Создайте локальное окружение из корня репозитория:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
agy --version
```

## 2. Установите переносимую MCP-команду

Установите проект как отдельное приложение через `pipx`. `pipx` создаёт
изолированное окружение и публикует команду `antiagent-mcp` в `PATH`; поэтому
конфигурация Codex не зависит от каталога checkout, буквы диска или имени
пользователя:

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
py -m pipx install .
.\.venv\Scripts\python.exe -m antiagent_setup --dry-run
```

После изменения исходников полностью закройте Codex CLI, desktop app и IDE,
затем обновляйте установленное приложение из текущего checkout безопасной
командой:

```powershell
py -m antiagent_upgrade
```

Команда сначала через Windows Toolhelp API доказывает, что
`antiagent-mcp.exe` не запущен, и только затем вызывает `pipx`. Если проверка
процессов невозможна или найден активный MCP, обновление завершается до любых
изменений. Процессы автоматически не завершаются. После успешного обновления
команда заново регистрирует абсолютный launcher; Codex нужно полностью
перезапустить и проверить через `antigravity_doctor` и live read-only smoke.
Точная последовательность действий и критерии готовности описаны в
[`POST_UPDATE_ACTIVATION.md`](POST_UPDATE_ACTIVATION.md).

## 3. Подключите MCP к Codex один раз

Запустите установщик: он находит pipx shim и Codex CLI независимо от буквы
диска и имени пользователя, затем регистрирует абсолютный launcher в
пользовательской конфигурации. `cwd` не задаётся: MCP наследует Git-root текущей
Codex-сессии. Абсолютный путь обязателен, иначе Windows может не найти bare
команду или выбрать одноимённый executable из недоверенного текущего проекта:

```powershell
.\.venv\Scripts\python.exe -m antiagent_setup
```

Не добавляйте дублирующий `[mcp_servers.antigravity_cli_executor]` в проектный
`.codex/config.toml`: project-scoped таблица перекрывает пользовательский
абсолютный launcher и снова делает запуск зависимым от `PATH`.

Запускайте Codex из Git-root целевого проекта и полностью перезапустите CLI,
desktop app или IDE extension после изменения MCP-конфигурации.
Полный контракт local placement, host boundary и проверка существующего OAuth
описаны в [host-side deployment guide](HOST_SIDE_DEPLOYMENT.md). Значение
`ANTIAGENT_EXECUTION_BOUNDARY=host` является декларацией оператора; готовность
подтверждается только новым read-only MCP smoke после перезапуска.

Для безопасной локальной диагностики используйте `antigravity_doctor`. Он
проверяет только версию CLI, boundary declaration, wrapper state и Git-root;
OAuth/keyring и сеть не читаются, поэтому `oauth_ready` остаётся `unknown`.

## 4. Подключите skill

Дополнительная установка для этого checkout не требуется. Codex автоматически
ищет repo-scoped skills в `.agents/skills`, поэтому файл
`.agents/skills/antigravity-executor/SKILL.md` будет обнаружен при запуске из
корня проекта или его дочернего каталога. Это соответствует
[официальной документации OpenAI](https://developers.openai.com/codex/skills).

Проверьте skill через `/skills` и вызовите явно:

```text
$antigravity-executor найди причину этой небольшой ошибки, сначала только план
```

Codex также может выбрать skill автоматически, когда запрос соответствует его
описанию. Если новый skill не появился, полностью перезапустите Codex.

Чтобы использовать skill в другом репозитории, скопируйте всю папку
`.agents/skills/antigravity-executor` в `.agents/skills` целевого Git-root и
при необходимости `.codex/agents/antigravity_worker.toml` в `.codex/agents`.
Повторная MCP-регистрация не нужна. Один skill не создаёт MCP-подключение
автоматически.

## 5. Разрешите изменения после плана

Первый запуск всегда выполняется в безопасном `mode=plan`. Для конкретного
согласованного изменения напишите:

```text
$antigravity-executor разрешаю выполнить предложенный план в mode=accept-edits.
После результата сам проверь git diff и релевантные тесты.
```

Antigravity не получает задачи на архитектуру, security, destructive-операции,
секреты, commit или push. Финальное решение всегда остаётся за основным Codex.

Для задачи с машинно проверяемым ответом можно передать `expected_marker`
(непустая строка до 256 символов). Если успешный ответ не содержит marker,
wrapper возвращает typed `verification_failed`; значение marker в ошибке и
логах не повторяется. Structured failed runs сохраняют только allowlisted
счётчики usage.

Текущий CLI `1.1.24` не предоставляет строгий file allowlist/deny-shell.
Поэтому публичная MCP-схема допускает только `payload_mode=workspace`, а
результат честно сообщает `file_scope_enforced=false` и `shell_denied=false`.
Передавайте минимальный контекст и относительные `@file`-ссылки; это уменьшает
число tool-запросов, но не является технической границей доступа. При
`permission_denied` не включайте `--dangerously-skip-permissions`: используйте
узкий интерактивный запуск либо native fallback.

Во время выполнения `execute` и `agent_wait` отправляют MCP progress с общей
шкалой `0..100`. В durable snapshot поле `progress` содержит текущую фазу,
последние 16 безопасных событий, blocker, следующее действие, heartbeat,
elapsed/idle и принадлежность manager process. Процент отражает только этапы
оболочки (`progress_basis=wrapper_phase`), а `indeterminate=true` честно означает,
что внутренний процент и ETA Gemini неизвестны. Из телеметрии исключены prompt,
context, пути, argv, raw stdout/stderr, tool arguments и текст ответа модели.

## 6. Проверка проекта

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe .\smoke_mcp.py <КОРЕНЬ_GIT_ПРОЕКТА>
.\.venv\Scripts\python.exe smoke_agy.py
git diff --check
```

Smoke с реальным провайдером требует действующего OAuth и доступного лимита.
Unit-тесты не требуют браузерного входа.

Дополнительные материалы:

- [краткое руководство](КАК_ПОЛЬЗОВАТЬСЯ.md);
- [техническая инструкция](<Инструкция_ Antigravity CLI OAuth Executor для Codex.md>);
- [отчёт тестирования](TEST_REPORT.md).
