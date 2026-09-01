# Antiagent: Antigravity для Codex

Этот проект подключает OAuth-аутентифицированный Antigravity CLI к Codex через
локальный stdio MCP process. Codex управляет задачей, разрешениями, review и
тестами, а Gemini/Antigravity выполняет небольшую leaf coding-задачу.

В репозитории уже находятся:

- MCP server `agy_server.py`;
- persistent manager с lifecycle tools;
- custom Codex agent `.codex/agents/antigravity_worker.toml`;
- repo-scoped skill `.agents/skills/antigravity-executor/SKILL.md`;
- готовая MCP-конфигурация `.codex/config.toml`.

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

## 2. Подключите MCP к Codex

Файл `.codex/config.toml` уже настроен для checkout
`C:\projects\antiagent`. Если репозиторий находится в другом месте, замените
абсолютные пути в `command`, `args` и `cwd`.

Если project-scoped config не используется, альтернативно зарегистрируйте
server в пользовательской конфигурации через Codex CLI. Выберите один способ,
чтобы не дублировать запись:

```powershell
codex.cmd mcp add antigravity_cli_executor -- "C:\projects\antiagent\.venv\Scripts\python.exe" "C:\projects\antiagent\agy_server.py"
codex.cmd mcp get antigravity_cli_executor
```

Запускайте Codex из Git-root этого проекта и полностью перезапустите CLI,
desktop app или IDE extension после изменения MCP/custom-agent конфигурации.
Полный контракт local placement, host boundary и проверка существующего OAuth
описаны в [host-side deployment guide](HOST_SIDE_DEPLOYMENT.md). Значение
`ANTIAGENT_EXECUTION_BOUNDARY=host` является декларацией оператора; готовность
подтверждается только новым read-only MCP smoke после перезапуска.

Для безопасной локальной диагностики используйте `antigravity_doctor`. Он
проверяет только версию CLI, boundary declaration, wrapper state и Git-root;
OAuth/keyring и сеть не читаются, поэтому `oauth_ready` остаётся `unknown`.

## 3. Подключите skill

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
подключите там MCP server. Один skill не создаёт MCP-подключение автоматически.

## 4. Разрешите изменения после плана

Первый запуск всегда выполняется в безопасном `mode=plan`. Для конкретного
согласованного изменения напишите:

```text
$antigravity-executor разрешаю выполнить предложенный план в mode=accept-edits.
После результата сам проверь git diff и релевантные тесты.
```

Antigravity не получает задачи на архитектуру, security, destructive-операции,
секреты, commit или push. Финальное решение всегда остаётся за основным Codex.

## 5. Проверка проекта

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe smoke_agy.py
git diff --check
```

Smoke с реальным провайдером требует действующего OAuth и доступного лимита.
Unit-тесты не требуют браузерного входа.

Дополнительные материалы:

- [краткое руководство](КАК_ПОЛЬЗОВАТЬСЯ.md);
- [техническая инструкция](<Инструкция_ Antigravity CLI OAuth Executor для Codex.md>);
- [отчёт тестирования](TEST_REPORT.md).
