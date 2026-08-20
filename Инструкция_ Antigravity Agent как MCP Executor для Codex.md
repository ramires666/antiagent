# Antigravity Agent → MCP Executor → Codex

> **Цель:** собрать локальный MCP-сервер, который предоставляет Codex один высокоуровневый tool `antigravity_execute`. Codex передаёт ему задачу, Antigravity получает доступ к тому же репозиторию, самостоятельно читает и изменяет файлы, запускает команды и тесты, после чего возвращает Codex итоговый отчёт.
>
> Инструкция рассчитана на junior-разработчика и специально написана так, чтобы все шаги можно было выполнять буквально сверху вниз.
>
> Актуальность инструкции: **20 августа 2026 года**.

---

# 1. Что именно мы строим

Итоговая архитектура:

```text
Пользователь
    │
    ▼
┌───────────────────────────────┐
│            Codex              │
│                               │
│ planner / reviewer / manager  │
└───────────────┬───────────────┘
                │
                │ MCP tool call
                │ antigravity_execute(...)
                ▼
┌───────────────────────────────┐
│       antigravity-mcp         │
│                               │
│ Python MCP server             │
│ transport = stdio             │
└───────────────┬───────────────┘
                │
                │ Agent.chat(...)
                ▼
┌───────────────────────────────┐
│      Google Antigravity       │
│                               │
│ autonomous coding executor    │
│                               │
│ - читает файлы                │
│ - ищет код                    │
│ - изменяет файлы              │
│ - создаёт файлы               │
│ - запускает shell-команды     │
│ - запускает тесты             │
│ - исправляет ошибки           │
└───────────────┬───────────────┘
                │
                ▼
       тот же Git repository
                │
                ▼
┌───────────────────────────────┐
│             Codex             │
│                               │
│ проверяет diff и тесты        │
└───────────────────────────────┘
```

Мы **не будем** экспортировать каждый внутренний инструмент Antigravity через MCP.

То есть НЕ делаем:

```text
read_file
edit_file
run_command
search_code
...
```

Вместо этого MCP предоставляет один высокоуровневый инструмент:

```text
antigravity_execute
```

И Codex говорит ему примерно:

```text
Реализуй refresh-token rotation.
Сохрани публичный API.
После изменений запусти pytest и mypy.
```

После этого **сам Antigravity** выполняет внутренний agent loop:

```text
прочитал код
→ нашёл нужные файлы
→ изменил код
→ запустил тест
→ увидел ошибку
→ снова изменил код
→ снова запустил тест
→ закончил задачу
```

Это основная идея wrapper-а.

---

# 2. Почему wrapper должен быть отдельным проектом

Не помещайте MCP-wrapper внутрь рабочего репозитория приложения.

Плохо:

```text
my-project/
├── src/
├── tests/
└── antigravity_mcp/
```

Лучше:

```text
~/tools/
└── antigravity-mcp-wrapper/
    ├── .venv/
    ├── requirements.txt
    └── server.py

~/projects/
└── my-project/
    ├── .git/
    ├── src/
    └── tests/
```

Причина простая.

Antigravity должен видеть:

```text
~/projects/my-project
```

но ему совершенно не обязательно видеть или изменять собственный MCP-wrapper.

Итого у нас будут две разные директории.

### Директория wrapper-а

Например:

```text
/home/alex/tools/antigravity-mcp-wrapper
```

### Рабочий repository

Например:

```text
/home/alex/projects/my-project
```

Запомните это разделение.

---

# 3. Требования

Нужно:

- Python 3.10 или новее;
- рекомендую Python 3.11;
- Codex CLI;
- Gemini API key;
- Git;
- рабочий repository;
- интернет для первоначальной установки Python-пакетов.

И MCP Python SDK, и Antigravity требуют Python `>=3.10`.

Для этой инструкции используем:

```text
Python:               3.11
google-antigravity:   0.1.12
mcp:                  2.0.0
```

По состоянию на 20 августа 2026 года PyPI показывает Antigravity `0.1.12`, опубликованный 13 августа 2026 года.

А официальный MCP Python SDK сейчас находится на стабильной ветке v2; PyPI публикует `mcp 2.0.0`. В v2 старый `FastMCP` переименован в `MCPServer`.

---

# 4. Проверяем Python

В терминале:

```bash
python3 --version
```

или:

```bash
python --version
```

Нужно получить что-то вроде:

```text
Python 3.11.9
```

или:

```text
Python 3.12.x
```

Если вывод:

```text
Python 3.9
```

то этот Python не используем.

На Linux/macOS желательно явно использовать:

```bash
python3.11
```

Проверка:

```bash
python3.11 --version
```

---

# 5. Проверяем Codex

Выполнить:

```bash
codex --version
```

Если Codex установлен, увидите что-то примерно такое:

```text
codex-cli 0.xxx.x
```

Если команда:

```text
codex: command not found
```

Codex можно установить официальным installer-ом или через npm/Homebrew. В официальном репозитории сейчас указаны варианты:

```bash
npm install -g @openai/codex
```

или на macOS:

```bash
brew install --cask codex
```



После установки снова:

```bash
codex --version
```

---

# 6. Проверяем Git repository

Перейти в проект:

```bash
cd /ABSOLUTE/PATH/TO/YOUR/PROJECT
```

Например:

```bash
cd /home/alex/projects/my-project
```

Проверить:

```bash
git status
```

Должно работать.

Затем получить абсолютный путь:

```bash
pwd
```

Например:

```text
/home/alex/projects/my-project
```

**Скопируйте этот путь.**

Он понадобится позже.

В инструкции будем обозначать его:

```text
/ABSOLUTE/PATH/TO/YOUR/PROJECT
```

---

# 7. Создаём отдельную директорию wrapper-а

Например:

```bash
mkdir -p ~/tools/antigravity-mcp-wrapper
cd ~/tools/antigravity-mcp-wrapper
```

Проверить:

```bash
pwd
```

Получится примерно:

```text
/home/alex/tools/antigravity-mcp-wrapper
```

---

# 8. Создаём Python virtual environment

На Linux/macOS:

```bash
python3.11 -m venv .venv
```

Активировать:

```bash
source .venv/bin/activate
```

После этого:

```bash
which python
```

должен показать примерно:

```text
/home/alex/tools/antigravity-mcp-wrapper/.venv/bin/python
```

Это очень важно.

Не должно быть:

```text
/usr/bin/python
```

или:

```text
/usr/local/bin/python
```

Должен использоваться именно:

```text
.../antigravity-mcp-wrapper/.venv/bin/python
```

---

# 9. Обновляем pip

```bash
python -m pip install --upgrade pip
```

Проверить:

```bash
python -m pip --version
```

---

# 10. Создаём requirements.txt

Создать файл:

```text
requirements.txt
```

Содержимое:

```text
google-antigravity==0.1.12
mcp[cli]==2.0.0
```

Установить:

```bash
python -m pip install -r requirements.txt
```

Это может занять заметное время, потому что Antigravity включает platform-specific runtime.

Очень важный момент: Antigravity нельзя корректно установить простым клонированием GitHub-репозитория — официальный пакет содержит скомпилированный runtime binary в PyPI wheel. Поэтому устанавливаем именно через `pip`.

---

# 11. Проверяем установку пакетов

Выполнить:

```bash
python - <<'PY'
from importlib.metadata import version

print("google-antigravity:", version("google-antigravity"))
print("mcp:", version("mcp"))

from google.antigravity import Agent, LocalAgentConfig, CapabilitiesConfig
from mcp.server import MCPServer

print("Imports OK")
PY
```

Ожидаемый результат:

```text
google-antigravity: 0.1.12
mcp: 2.0.0
Imports OK
```

Если здесь ошибка — дальше НЕ идти.

Сначала исправить установку.

---

# 12. Настраиваем Gemini API key

Antigravity использует переменную окружения с каноническим именем:

```text
GEMINI_API_KEY
```

Wrapper сначала использует уже установленную process environment. Это главный
и рекомендуемый путь: в Codex укажите `env_vars = ["GEMINI_API_KEY"]`, а сам
Codex запускайте из environment, где ключ уже задан. Если переменной нет,
wrapper безопасно пытается прочитать `.env` только из собственной директории
wrapper-а (не из target repository) стандартными средствами Python. Для этого
не нужен и не устанавливается `python-dotenv`.

Приоритет источников:

1. `GEMINI_API_KEY` в process environment — всегда побеждает `.env`;
2. `.env` рядом с `server.py` — fallback для локального запуска;
3. иначе credentials считаются отсутствующими.

В `.env` допустима простая запись `NAME=value`; комментарии и пустые строки
игнорируются. Значение никогда не выводится в лог, stdout или диагностический
отчёт. Файл `.env` должен оставаться в `.gitignore`.

На Linux/macOS можно задать ключ явно:

```bash
export GEMINI_API_KEY="ВАШ_КЛЮЧ"
```

Проверять сам ключ через `echo` нежелательно. Проверяйте только наличие:

```bash
python - <<'PY'
import os
print(bool(os.environ.get("GEMINI_API_KEY")))
PY
```

Ожидаемый результат:

```text
True
```

Историческое имя `GEMINI_APIKEY` принимается только как deprecated
compatibility alias, если каноническое имя не задано. При его использовании
wrapper выдаёт предупреждение, но никогда не печатает значение. Новые `.env`,
shell-команды и config должны использовать только `GEMINI_API_KEY`; alias
нужно постепенно удалить.

---

# 13. НЕ хранить API key внутри server.py

Никогда не писать:

```python
API_KEY = "AIza..."
```

Никогда не писать ключ в:

```text
server.py
requirements.txt
README
.codex/config.toml
git repository
```

Ключ передаётся через process environment или локальный `.env` fallback,
описанный выше. `.env` остаётся локальным и игнорируется Git.

---

# 14. Очень важный принцип stdio MCP

Наш MCP server работает через:

```text
stdio
```

То есть Codex и MCP-server общаются через стандартные:

```text
stdin
stdout
```

Следовательно:

> **НЕЛЬЗЯ делать обычные `print()` в stdout из server.py.**

Например, это плохо:

```python
print("Server started")
```

Почему?

Потому что Codex ждёт в stdout сообщения MCP protocol.

Если туда попадёт:

```text
Server started
```

протокол может сломаться.

Поэтому:

```text
stdout = MCP protocol only
stderr = logs
```

Логи пишем через `logging`, настроенный на:

```python
sys.stderr
```

---

# 15. Создаём server.py

В директории:

```text
~/tools/antigravity-mcp-wrapper
```

создать:

```text
server.py
```

Полное содержимое:

```python
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Literal

from google.antigravity import (
    Agent,
    BuiltinTools,
    CapabilitiesConfig,
    GeminiAPIEndpoint,
    GeminiModelOptions,
    LocalAgentConfig,
    ModelTarget,
    ThinkingLevel,
)
from google.antigravity.hooks import policy
from mcp.server import MCPServer


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
#
# ВАЖНО:
# MCP stdio использует stdout для протокола.
#
# Поэтому любые наши diagnostic logs должны идти только в stderr.
#

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)

logger = logging.getLogger("antigravity-mcp")


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = MCPServer(
    "Antigravity Coding Executor"
)


# ---------------------------------------------------------------------------
# Runtime settings
# ---------------------------------------------------------------------------

DEFAULT_TASK_TIMEOUT_SEC = 840
DEFAULT_MAX_RESULT_CHARS = 30_000
ThinkingLevelName = Literal["low", "medium", "high"]
ALLOWED_THINKING_LEVELS = ("low", "medium", "high")
EXECUTOR_ALLOWED_TOOLS = (
    BuiltinTools.LIST_DIR,
    BuiltinTools.FIND_FILE,
    BuiltinTools.SEARCH_DIR,
    BuiltinTools.VIEW_FILE,
    BuiltinTools.CREATE_FILE,
    BuiltinTools.EDIT_FILE,
    BuiltinTools.RUN_COMMAND,
    BuiltinTools.FINISH,
)


def read_positive_int_env(name: str, default: int) -> int:
    """
    Read a positive integer from environment.

    If the variable is missing or invalid, use default.
    """

    raw = os.environ.get(name)

    if raw is None:
        return default

    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer. Using default=%s",
            name,
            raw,
            default,
        )
        return default

    if value <= 0:
        logger.warning(
            "%s=%r must be > 0. Using default=%s",
            name,
            raw,
            default,
        )
        return default

    return value


TASK_TIMEOUT_SEC = read_positive_int_env(
    "ANTIGRAVITY_TASK_TIMEOUT_SEC",
    DEFAULT_TASK_TIMEOUT_SEC,
)

MAX_RESULT_CHARS = read_positive_int_env(
    "ANTIGRAVITY_MAX_RESULT_CHARS",
    DEFAULT_MAX_RESULT_CHARS,
)


# ---------------------------------------------------------------------------
# Concurrency protection
# ---------------------------------------------------------------------------
#
# Два автономных coding-agent-а НЕ должны одновременно изменять один repo.
#
# Даже если MCP client случайно отправит несколько вызовов параллельно,
# wrapper выполнит их последовательно.
#

EXECUTION_LOCK = asyncio.Lock()


# ---------------------------------------------------------------------------
# System instructions for Antigravity
# ---------------------------------------------------------------------------

SYSTEM_INSTRUCTIONS = """
You are an autonomous coding executor working for another coding agent.

The outer agent is OpenAI Codex.

Your responsibility is to IMPLEMENT the requested coding task inside the
provided workspace.

You are an executor, not merely an advisor.

GENERAL RULES

1. Inspect the repository before making changes.

2. If AGENTS.md, README.md, CONTRIBUTING.md or project-specific instructions
   exist, read the relevant instructions before editing code.

3. Work only on the requested task.

4. Avoid unrelated refactoring.

5. Prefer the smallest correct change that satisfies the task.

6. Preserve existing public APIs unless the task explicitly requires changing
   them.

7. Use the repository's existing coding style, architecture and conventions.

8. You may read files, search code, create files and modify files.

9. You may execute commands and tests when needed.

10. When a command or test fails, inspect the failure and attempt to fix the
    problem.

11. Do not stop after the first implementation if relevant tests are failing.

12. Run the most relevant available verification before finishing.

13. Never invoke Codex.

14. Never start another instance of this MCP server.

15. Never recursively delegate the task back to the outer agent.

16. Do not run git push.

17. Do not create commits unless the task explicitly asks for a commit.

18. Do not use git reset --hard, git clean -fd, or similar destructive
    repository-wide commands.

19. Do not discard unrelated existing user changes.

20. Do not modify files outside the configured workspace.

21. If requirements are slightly ambiguous, inspect the repository and make
    the safest reasonable implementation instead of asking the outer agent to
    perform work you can do yourself.

FINAL RESPONSE

At the end, return a concise implementation report containing:

- status
- summary of what was implemented
- important files changed
- commands/tests executed
- whether verification passed
- any remaining issues or assumptions

Do not include a long tutorial in the final response.
The outer Codex agent will inspect the repository and review your changes.
""".strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_workspace() -> Path:
    """
    MCP process cwd is expected to be the repository root.

    Codex sets this through:
        mcp_servers.<name>.cwd
    """

    workspace = Path.cwd().resolve()

    if not workspace.exists():
        raise RuntimeError(
            f"Workspace does not exist: {workspace}"
        )

    if not workspace.is_dir():
        raise RuntimeError(
            f"Workspace is not a directory: {workspace}"
        )

    # Require cwd to be the repository root, not merely any directory inside
    # a repository.  .git may be a directory or a git-worktree marker file.
    git_marker = workspace / ".git"
    if not git_marker.is_dir() and not git_marker.is_file():
        raise RuntimeError(
            "Workspace must be the Git repository root with a .git "
            f"file or directory: {workspace}"
        )

    return workspace


def build_agent_prompt(
    task: str,
    context: str,
    verification: str,
    workspace: Path,
) -> str:
    parts: list[str] = []

    parts.append(
        f"""
WORKSPACE

{workspace}

TASK

{task.strip()}
""".strip()
    )

    if context.strip():
        parts.append(
            f"""
ADDITIONAL CONTEXT

{context.strip()}
""".strip()
        )

    if verification.strip():
        parts.append(
            f"""
REQUIRED VERIFICATION

{verification.strip()}
""".strip()
        )

    parts.append(
        """
EXECUTION REQUIREMENT

Actually implement the task in the workspace.

Do not only describe a possible solution.

Inspect the code, make the required changes, run relevant verification and
iterate on failures where reasonably possible.
""".strip()
    )

    return "\n\n".join(parts)


def truncate_result(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_RESULT_CHARS:
        return text, False

    truncated = (
        text[:MAX_RESULT_CHARS]
        + "\n\n"
        + "[Antigravity result truncated by MCP wrapper]"
    )

    return truncated, True


def build_model_target(
    thinking_level: ThinkingLevelName,
) -> ModelTarget:
    if thinking_level not in ALLOWED_THINKING_LEVELS:
        raise ValueError("thinking_level must be low, medium, or high")
    return ModelTarget(
        # google-antigravity 0.1.12 does not reliably infer a model from the
        # endpoint options. Without this explicit name runtime fails with:
        # tModel: model is empty.
        name="gemini-3.7-flash",
        endpoint=GeminiAPIEndpoint(
            options=GeminiModelOptions(
                thinking_level=ThinkingLevel(thinking_level)
            )
        )
    )


async def execute_with_antigravity(
    *,
    workspace: Path,
    prompt: str,
    thinking_level: ThinkingLevelName,
) -> str:
    """
    Start one isolated Antigravity agent session and execute one task.
    """

    config = LocalAgentConfig(
        system_instructions=SYSTEM_INSTRUCTIONS,

        # Enables builtin capabilities including file modification tools.
        capabilities=CapabilitiesConfig(
            enabled_tools=list(EXECUTOR_ALLOWED_TOOLS), enable_subagents=False
        ),

        # Restricts Antigravity file tools to this workspace.
        workspaces=[str(workspace)],

        # Deny by default; grant only the tools required by the coding
        # executor. This is not an OS-level security sandbox.
        policies=[
            policy.deny_all(),
            *[policy.allow(tool.value) for tool in EXECUTOR_ALLOWED_TOOLS],
        ],

        # Selects the native Antigravity/Gemini thinking level.
        model=build_model_target(thinking_level),
    )

    async with Agent(config) as agent:
        response = await agent.chat(prompt)
        return await response.text()


# ---------------------------------------------------------------------------
# MCP tool
# ---------------------------------------------------------------------------

@mcp.tool()
async def antigravity_execute(
    task: str,
    context: str = "",
    verification: str = "",
    thinking_level: ThinkingLevelName = "medium",
) -> dict[str, Any]:
    """
    Delegate an implementation task to the Antigravity coding agent.

    Antigravity can inspect and modify the current repository, execute
    commands, run tests, debug failures and iterate on the requested change.

    Parameters:
        task:
            The coding task Antigravity must actually implement.

        context:
            Optional additional repository or architecture context.

        verification:
            Optional commands or acceptance criteria Antigravity should verify.

        thinking_level:
            Native Antigravity/Gemini thinking level: low, medium, or high.
            Defaults to medium.

    Use this tool for implementation work that benefits from an autonomous
    coding executor.

    After this tool returns, inspect the repository diff and independently
    verify the resulting changes.
    """

    started_at = time.monotonic()

    # Validate every argument before touching workspace state or waiting for
    # the execution lock.
    if not isinstance(task, str) or not task.strip():
        return {
            "status": "error",
            "error_type": "invalid_request",
            "message": "task must be a non-empty string",
        }

    if not isinstance(context, str):
        return {
            "status": "error",
            "error_type": "invalid_request",
            "message": "context must be a string",
        }

    if not isinstance(verification, str):
        return {
            "status": "error",
            "error_type": "invalid_request",
            "message": "verification must be a string",
        }

    if thinking_level not in ALLOWED_THINKING_LEVELS:
        return {
            "status": "error",
            "error_type": "invalid_request",
            "message": "thinking_level must be low, medium, or high",
        }

    workspace = get_workspace()

    prompt = build_agent_prompt(
        task=task,
        context=context,
        verification=verification,
        workspace=workspace,
    )

    logger.info(
        "Received task for workspace=%s thinking=%s",
        workspace,
        thinking_level,
    )

    try:
        # The timeout covers lock wait and Antigravity execution together.
        async def run_serialized() -> str:
            async with EXECUTION_LOCK:
                return await execute_with_antigravity(
                    workspace=workspace,
                    prompt=prompt,
                    thinking_level=thinking_level,
                )

        result = await asyncio.wait_for(
            run_serialized(),
            timeout=TASK_TIMEOUT_SEC,
        )

    except asyncio.TimeoutError:
        duration = round(time.monotonic() - started_at, 2)

        logger.error(
            "Antigravity task timed out after %s seconds",
            TASK_TIMEOUT_SEC,
        )

        return {
            "status": "error",
            "error_type": "timeout",
            "workspace": str(workspace),
            "duration_seconds": duration,
            "thinking_level": thinking_level,
            "message": f"Execution exceeded {TASK_TIMEOUT_SEC} seconds.",
        }

    except Exception:
        duration = round(time.monotonic() - started_at, 2)

        # Do not expose or log exception text: it may contain credentials,
        # local paths, prompts, or provider internals.
        logger.error("Antigravity execution failed")

        return {
            "status": "error",
            "error_type": "execution_failed",
            "workspace": str(workspace),
            "duration_seconds": duration,
            "thinking_level": thinking_level,
            "message": "Antigravity execution failed; inspect server logs.",
        }

    duration = round(time.monotonic() - started_at, 2)

    result, was_truncated = truncate_result(result)

    logger.info(
        "Antigravity task completed in %.2f seconds",
        duration,
    )

    return {
        "status": "completed",
        "workspace": str(workspace),
        "duration_seconds": duration,
        "thinking_level": thinking_level,
        "result_truncated": was_truncated,
        "result": result,
        "next_action": "Codex should inspect git diff and verify independently.",
    }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
```

---

# 16. Почему используется MCPServer, а не FastMCP

В старых примерах можно встретить:

```python
from mcp.server.fastmcp import FastMCP
```

Для свежего MCP Python SDK v2 это уже неправильный вариант.

Используем:

```python
from mcp.server import MCPServer
```

и:

```python
mcp = MCPServer("Antigravity Coding Executor")
```

Официальная документация v2 прямо указывает, что:

```text
FastMCP → MCPServer
```

а `pip install mcp` теперь устанавливает v2.

---

# 17. Почему используется stdio

В конце:

```python
mcp.run(transport="stdio")
```

Это означает:

```text
Codex запускает server.py как subprocess
        │
        ├── stdin
        └── stdout
```

Нам не нужны:

```text
HTTP server
port
nginx
TLS
OAuth
Docker networking
```

для локального первого варианта.

MCP v2 официально поддерживает stdio.

---

# 18. Что делает CapabilitiesConfig

Без:

```python
capabilities=CapabilitiesConfig()
```

Antigravity по умолчанию работает гораздо более ограниченно.

Официальная документация Antigravity указывает, что `CapabilitiesConfig()` включает builtin capabilities, в том числе write tools.

Поэтому для coding executor нам нужно:

```python
capabilities=CapabilitiesConfig()
```

Иначе может получиться агент, который отлично объясняет, что нужно изменить, но не может нормально выполнить изменения.

---

# 19. Что делает workspaces

В конфигурации:

```python
workspaces=[str(workspace)]
```

мы сообщаем Antigravity:

```text
работать нужно в этой директории
```

При использовании `workspaces` Antigravity автоматически добавляет workspace policy для файловых tools, ограничивающую `view_file`, `create_file` и `edit_file` указанными workspace-каталогами.

То есть если repository:

```text
/home/alex/projects/shop
```

Antigravity получает workspace:

```text
/home/alex/projects/shop
```

---

# 20. Важное ограничение workspaces

`workspaces` — это **не полноценная OS sandbox**.

Она ограничивает встроенные файловые operations Antigravity.

Но executor также получает:

```python
run_command
```

через явный allowlist `BuiltinTools`:

```python
policies=[
    policy.deny_all(),
    *[policy.allow(tool.value) for tool in EXECUTOR_ALLOWED_TOOLS],
]
```

Shell-команда теоретически может обратиться за пределы workspace.

Например:

```bash
cat ~/.ssh/config
```

или:

```bash
rm -rf ../something
```

Поэтому:

> Этот MVP следует запускать только в доверенной локальной среде и на доверенных задачах.

Если нужен реально изолированный production executor, следующий уровень архитектуры:

```text
Codex
  │
  ▼
MCP wrapper
  │
  ▼
Docker / sandbox / isolated VM
  │
  ▼
Antigravity
  │
  ▼
mounted repository
```

---

# 21. Почему используется deny-by-default allowlist

Antigravity по умолчанию консервативен относительно shell-команд.

Default policy блокирует `run_command`, поэтому для автономного coding
executor-а задаём deny-by-default и явный allowlist:

```python
policy.deny_all()
```



Именно поэтому мы пишем:

```python
policies=[
    policy.deny_all(),
    *[policy.allow(tool.value) for tool in EXECUTOR_ALLOWED_TOOLS],
]
```

Иначе агент может изменить файл, но упереться в невозможность выполнить:

```bash
pytest
npm test
npm run lint
mypy
go test
cargo test
```

---

# 22. Почему allowlist не заменяет OS sandbox

Даже deny-by-default allowlist не ограничивает произвольный shell-код,
разрешённый через `BuiltinTools.RUN_COMMAND`. `run_command` остаётся OS trust
boundary: команда может читать или менять данные за пределами workspace.
Для недоверенных задач по-прежнему обязательны sandbox/container/VM,
ограниченный пользователь и минимальные filesystem/network permissions.
`policy.allow_all()` — отклонённый, менее безопасный development-вариант, а не
рекомендация.

---

# 23. Проверяем синтаксис server.py

В wrapper directory:

```bash
cd ~/tools/antigravity-mcp-wrapper
```

Активировать venv:

```bash
source .venv/bin/activate
```

Проверить compilation:

```bash
python -m py_compile server.py
```

Если команда ничего не вывела — всё хорошо.

Если traceback — исправить ошибку до следующего шага.

---

# 24. Проверяем импорт server.py

Выполнить:

```bash
python - <<'PY'
import server
print("server import OK", file=__import__("sys").stderr)
PY
```

Нужно получить:

```text
server import OK
```

---

# 25. Проверяем Antigravity отдельно от MCP

Перед подключением Codex полезно проверить сам Antigravity.

Создать:

```text
smoke_antigravity.py
```

Содержимое:

```python
import asyncio
from pathlib import Path

from google.antigravity import (
    Agent,
    BuiltinTools,
    CapabilitiesConfig,
    LocalAgentConfig,
)
from google.antigravity.hooks import policy

SMOKE_ALLOWED_TOOLS = (
    BuiltinTools.LIST_DIR,
    BuiltinTools.FIND_FILE,
    BuiltinTools.SEARCH_DIR,
    BuiltinTools.VIEW_FILE,
    BuiltinTools.FINISH,
)


async def main() -> None:
    workspace = Path.cwd().resolve()

    config = LocalAgentConfig(
        system_instructions=(
            "You are testing repository access. "
            "Do not modify anything."
        ),
        capabilities=CapabilitiesConfig(
            enabled_tools=list(SMOKE_ALLOWED_TOOLS), enable_subagents=False
        ),
        workspaces=[str(workspace)],
        # Read-only smoke test: no write or shell capability.
        policies=[
            policy.deny_all(),
            *[policy.allow(tool.value) for tool in SMOKE_ALLOWED_TOOLS],
        ],
    )

    async with Agent(config) as agent:
        response = await agent.chat(
            "Inspect this repository without modifying anything. "
            "Tell me the repository purpose and list up to five important files."
        )

        print(await response.text())


if __name__ == "__main__":
    asyncio.run(main())
```

---

# 26. Запускаем smoke test правильно

Важно запускать его **из рабочего repository**.

Например:

```bash
cd /home/alex/projects/my-project
```

Затем:

```bash
/home/alex/tools/antigravity-mcp-wrapper/.venv/bin/python \
    /home/alex/tools/antigravity-mcp-wrapper/smoke_antigravity.py
```

Smoke script и helper/tests используют тот же secure loader, что и `server.py`:
process environment имеет приоритет, затем читается `.env` рядом с wrapper-ом.
Секрет не нужно экспортировать вручную, если локальный `.env` уже создан и
остался вне Git. Smoke выводит только статус/результат проверки, но не key.

Antigravity должен:

1. запуститься;
2. увидеть repository;
3. прочитать файлы;
4. вернуть описание.

Если здесь ошибка с API key:

```text
GEMINI_API_KEY
```

значит key не попал ни в process environment, ни в `.env` wrapper-а.

В этом terminal:

```bash
export GEMINI_API_KEY="..."
```

и повторить.

Либо проверьте только наличие файла (не печатая его содержимое):

```bash
test -f /home/alex/tools/antigravity-mcp-wrapper/.env
```

---

# 27. Проверяем MCP-server через MCP Inspector

MCP SDK с `[cli]` устанавливает command:

```bash
mcp
```

Официальная документация предлагает использовать:

```bash
mcp dev server.py
```

для разработки MCP-server.

Но нам важно, чтобы current directory был именно repository.

Поэтому:

```bash
cd /ABSOLUTE/PATH/TO/YOUR/PROJECT
```

Затем:

```bash
/ABSOLUTE/PATH/TO/WRAPPER/.venv/bin/mcp \
    dev \
    /ABSOLUTE/PATH/TO/WRAPPER/server.py
```

Например:

```bash
cd /home/alex/projects/my-project

/home/alex/tools/antigravity-mcp-wrapper/.venv/bin/mcp \
    dev \
    /home/alex/tools/antigravity-mcp-wrapper/server.py
```

---

# 28. Что проверить в MCP Inspector

Нужно увидеть tool:

```text
antigravity_execute
```

У него должны быть параметры:

```text
task
context
verification
thinking_level
```

Где:

```text
task
```

обязательный.

А:

```text
context
verification
thinking_level
```

опциональные.

Для `thinking_level` Inspector должен показывать enum:

```text
low
medium
high
```

Значение по умолчанию — `medium`.

Это уровень внутреннего thinking Antigravity/Gemini. Он не меняет
`model_reasoning_effort` внешнего Codex: reasoning самого Codex настраивается
отдельно через профиль или CLI.

В `google-antigravity==0.1.12` имя модели нужно передавать явно в
`ModelTarget(name="gemini-3.7-flash", endpoint=...)`. Одних `endpoint` options
с `thinking_level` недостаточно: при пустом имени runtime завершается ошибкой
`tModel: model is empty`. Поэтому выбранный уровень thinking управляет только
параметрами endpoint, а `name` всегда должен оставаться непустым.

---

# 29. Первый безопасный MCP test

Не начинайте сразу с большой задачи.

Передайте:

```text
task:
Inspect the repository and add a file named antigravity_mcp_test.txt
containing exactly:

antigravity MCP test

verification:
Verify that antigravity_mcp_test.txt exists and contains the expected text.

thinking_level:
medium
```

После выполнения проверить:

```bash
cat antigravity_mcp_test.txt
```

Должно быть:

```text
antigravity MCP test
```

Удалить:

```bash
rm antigravity_mcp_test.txt
```

---

# 30. Получаем абсолютный путь до Python wrapper-а

В wrapper directory:

```bash
cd ~/tools/antigravity-mcp-wrapper
source .venv/bin/activate
which python
```

Например:

```text
/home/alex/tools/antigravity-mcp-wrapper/.venv/bin/python
```

Скопировать.

---

# 31. Получаем абсолютный путь до server.py

```bash
realpath server.py
```

Например:

```text
/home/alex/tools/antigravity-mcp-wrapper/server.py
```

Скопировать.

---

# 32. Получаем абсолютный путь до repository

```bash
cd /ABSOLUTE/PATH/TO/YOUR/PROJECT
pwd
```

Например:

```text
/home/alex/projects/my-project
```

Скопировать.

---

# 33. Настраиваем Codex

Codex MCP servers конфигурируются через:

```text
~/.codex/config.toml
```

В актуальной схеме Codex MCP stdio server поддерживает:

```text
command
args
cwd
env
env_vars
enabled
required
startup_timeout_sec
tool_timeout_sec
enabled_tools
```



Открыть:

```bash
nano ~/.codex/config.toml
```

или ваш редактор.

---

# 34. Добавляем MCP-server в Codex

Добавить:

```toml
[mcp_servers.antigravity_executor]

command = "/ABSOLUTE/PATH/TO/WRAPPER/.venv/bin/python"

args = [
    "/ABSOLUTE/PATH/TO/WRAPPER/server.py"
]

cwd = "/ABSOLUTE/PATH/TO/YOUR/PROJECT"

enabled = true

required = true

startup_timeout_sec = 30

tool_timeout_sec = 900

enabled_tools = [
    "antigravity_execute"
]

env_vars = [
    "GEMINI_API_KEY"
]

[mcp_servers.antigravity_executor.env]
ANTIGRAVITY_TASK_TIMEOUT_SEC = "840"
ANTIGRAVITY_MAX_RESULT_CHARS = "30000"
```

`env_vars` — предпочтительный способ для Codex. Если Codex запускается без
этой переменной, wrapper использует `.env` в своей директории как локальный
fallback; это не заменяет `env_vars` в production-like конфигурации.

---

# 35. Реальный пример config.toml

Например wrapper:

```text
/home/alex/tools/antigravity-mcp-wrapper
```

Repository:

```text
/home/alex/projects/shop-backend
```

Тогда:

```toml
[mcp_servers.antigravity_executor]

command = "/home/alex/tools/antigravity-mcp-wrapper/.venv/bin/python"

args = [
    "/home/alex/tools/antigravity-mcp-wrapper/server.py"
]

cwd = "/home/alex/projects/shop-backend"

enabled = true

required = true

startup_timeout_sec = 30

tool_timeout_sec = 900

enabled_tools = [
    "antigravity_execute"
]

env_vars = [
    "GEMINI_API_KEY"
]

[mcp_servers.antigravity_executor.env]
ANTIGRAVITY_TASK_TIMEOUT_SEC = "840"
ANTIGRAVITY_MAX_RESULT_CHARS = "30000"
```

---

# 36. Почему command должен указывать именно на .venv Python

Не писать:

```toml
command = "python"
```

Потому что Codex может получить другой Python из PATH.

Например:

```text
/usr/bin/python
```

где нет:

```text
google-antigravity
mcp
```

Тогда получите:

```text
ModuleNotFoundError
```

Поэтому используем полный путь:

```text
/home/alex/tools/antigravity-mcp-wrapper/.venv/bin/python
```

---

# 37. Почему cwd указывает на repository

В server.py:

```python
workspace = Path.cwd().resolve()
```

Codex запускает MCP subprocess с:

```toml
cwd = "/home/alex/projects/shop-backend"
```

Следовательно внутри server.py:

```python
Path.cwd()
```

будет:

```text
/home/alex/projects/shop-backend
```

И именно этот путь передаётся Antigravity:

```python
workspaces=[str(workspace)]
```

Это ключевой механизм связи:

```text
Codex cwd
    =
MCP process cwd
    =
Antigravity workspace
    =
repository root
```

---

# 38. Почему tool_timeout_sec = 900

Обычный MCP tool часто выполняется секунды.

Но наш MCP tool запускает целого coding agent.

Он может:

```text
читать repository
искать код
редактировать несколько файлов
запускать npm install
запускать тесты
исправлять тесты
снова запускать тесты
```

Поэтому default MCP timeout может оказаться недостаточным.

Codex поддерживает отдельный:

```toml
tool_timeout_sec
```

для MCP server.

Мы ставим:

```text
900 секунд
```

то есть:

```text
15 минут
```

---

# 39. Почему внутренний timeout = 840

В Codex:

```text
900 секунд
```

В wrapper:

```text
840 секунд
```

То есть Antigravity получает максимум:

```text
14 минут
```

Зачем разница?

`TASK_TIMEOUT_SEC` — это общий бюджет вызова: в него входит и ожидание
`EXECUTION_LOCK`, и фактическое выполнение Antigravity. Поэтому wrapper успевает
сам завершить вызов, поймать timeout и вернуть Codex нормальный ответ:

```json
{
  "status": "error",
  "error_type": "timeout"
}
```

вместо ситуации, когда Codex просто убивает MCP call по собственному timeout.

Схема:

```text
lock wait + Antigravity execution: 840 sec
        ↓
wrapper формирует ошибку
        ↓
Codex timeout: 900 sec
```

---

# 40. Почему вызовы сериализуются внутри wrapper-а

Представим:

```text
Task A:
изменяет auth.py

Task B:
одновременно изменяет auth.py
```

Получим race condition.

В актуальной схеме Codex нет поддерживаемого ключа MCP-конфигурации для
отключения параллельных tool calls. Поэтому сериализацию гарантирует сам
wrapper:

```python
EXECUTION_LOCK = asyncio.Lock()
```

Даже если MCP client отправит вызовы параллельно, критическая секция:

```text
async with EXECUTION_LOCK:
    ...
```

выполнит задачи последовательно и не позволит двум агентам одновременно
изменять один repository.

---

# 41. Почему API key передаётся через env_vars

В Codex config:

```toml
env_vars = [
    "GEMINI_API_KEY"
]
```

Codex умеет передавать выбранные переменные environment stdio MCP process.

Это предпочтительный production-like путь: переменная должна существовать в
environment самого Codex.

То есть сначала:

```bash
export GEMINI_API_KEY="..."
```

а потом из этого же environment запускаем:

```bash
codex
```

Для локального запуска wrapper также поддерживает fallback `.env` рядом с
`server.py`. Поэтому после клонирования wrapper можно положить секрет в
локальный, игнорируемый Git файл `.env`, и запускать smoke/helper без ручного
`export`. Process environment всё равно имеет приоритет над `.env`; `env_vars`
оставляйте в Codex config, чтобы явно прокинуть переменную в MCP process.

Загрузчик написан на стандартной библиотеке Python. Не добавляйте
`python-dotenv` и не передавайте секрет через `env` в TOML — это повышает риск
случайного попадания значения в конфигурацию или диагностику.

---

# 42. Перезапускаем Codex

После изменения:

```text
~/.codex/config.toml
```

полностью закрыть старую Codex session.

Затем:

```bash
export GEMINI_API_KEY="ВАШ_КЛЮЧ"
```

и:

```bash
cd /ABSOLUTE/PATH/TO/YOUR/PROJECT
codex
```

---

# 43. Проверяем MCP servers

В отдельном терминале можно выполнить:

```bash
codex mcp list
```

Нужно увидеть:

```text
antigravity_executor
```

или аналогичную запись.

---

# 44. Проверяем, что Codex видит tool

В Codex спросить:

```text
Какие MCP tools предоставляет antigravity_executor?
Ничего пока не запускай.
```

Codex должен определить:

```text
antigravity_execute
```

---

# 45. Первый настоящий вызов через Codex

Дайте простую задачу:

```text
Используй antigravity_execute.

Попроси Antigravity изучить repository и создать файл
antigravity_codex_test.txt с содержимым:

Codex -> MCP -> Antigravity works

Передай thinking_level: low.

После возврата Antigravity самостоятельно проверь файл.
```

Ожидаемая цепочка:

```text
Codex
↓
antigravity_execute
↓
Antigravity
↓
создаёт файл
↓
возвращает результат
↓
Codex
↓
читает файл
↓
проверяет результат
```

---

# 46. Удаляем test file

После успешной проверки:

```bash
rm antigravity_codex_test.txt
```

---

# 47. Как правильно давать Codex задачи

Хороший вариант:

```text
Нужно реализовать refresh token rotation.

Сначала самостоятельно проанализируй задачу и repository.

После анализа делегируй реализацию через antigravity_execute.

Передай Antigravity:

task:
Реализовать refresh-token rotation.

context:
Auth-код находится в src/auth.
Публичный API менять нельзя.
База данных PostgreSQL.

verification:
Запустить pytest для auth tests и mypy для изменённых модулей.

thinking_level:
high

После завершения Antigravity обязательно:
1. проверь git diff самостоятельно;
2. проверь, что изменения соответствуют задаче;
3. запусти нужные тесты самостоятельно;
4. если найдёшь небольшую ошибку — исправь;
5. если требуется большой rework — снова используй antigravity_execute
   с конкретным feedback.
```

---

# 48. Правильная agent-to-agent модель

Codex должен быть:

```text
planner
reviewer
orchestrator
```

Antigravity:

```text
implementation executor
```

То есть:

```text
Codex:
"что нужно сделать?"

↓

Antigravity:
"реализую"

↓

Codex:
"правильно ли реализовано?"
```

---

# 49. Неправильная модель

Не надо говорить Codex:

```text
Передавай вообще всё Antigravity и никогда ничего сам не проверяй.
```

Иначе получится:

```text
User
↓
Codex proxy
↓
Antigravity
↓
готово
```

и смысл Codex как reviewer теряется.

Правильнее:

```text
User
↓
Codex planning
↓
Antigravity implementation
↓
Codex review
↓
при необходимости Antigravity fix
```

---

# 50. Пример workflow для реальной feature

Пользователь:

```text
Добавь endpoint POST /users/{id}/disable.
```

Codex сначала анализирует:

```text
routes
service
database model
tests
permissions
```

После этого вызывает:

```text
antigravity_execute(
    task="Implement POST /users/{id}/disable ...",
    context="...",
    verification="Run pytest tests/users...",
    thinking_level="medium",
)
```

Antigravity:

```text
читает код
↓
меняет router
↓
меняет service
↓
добавляет test
↓
pytest
↓
видит failure
↓
исправляет
↓
pytest
↓
финальный отчёт
```

Codex получает результат и делает:

```bash
git diff
```

проверяет:

```text
API
security
tests
style
```

После этого либо говорит:

```text
готово
```

либо:

```text
antigravity_execute(
    task="Fix these review issues...",
    context="...",
    thinking_level="low",
)
```

---

# 51. Что возвращает наш MCP tool

Успешный ответ примерно:

```json
{
  "status": "completed",
  "workspace": "/home/alex/projects/shop",
  "duration_seconds": 84.21,
  "thinking_level": "high",
  "result_truncated": false,
  "result": "Implemented refresh token rotation...",
  "next_action": "Codex should inspect git diff and verify independently."
}
```

Ошибка:

```json
{
  "status": "error",
  "error_type": "timeout",
  "workspace": "/home/alex/projects/shop",
  "duration_seconds": 840.02,
  "thinking_level": "high",
  "message": "Execution exceeded 840 seconds."
}
```

Поле `thinking_level` повторяет фактически использованный уровень. Допустимы
`low`, `medium` и `high`; если параметр вызова не передан, wrapper использует
`medium`.

---

# 52. Почему wrapper не пытается вычислять git diff самостоятельно

Мы специально не усложняем wrapper.

Он отвечает только за:

```text
Codex → Antigravity
```

А repository уже общий.

После возврата MCP tool Codex может сам выполнить:

```bash
git status
git diff
git diff --stat
```

Это лучше, чем пытаться прокидывать весь diff через MCP response.

---

# 53. Почему ограничиваем размер результата

В server.py:

```python
MAX_RESULT_CHARS = 30_000
```

Если Antigravity вернёт огромное сообщение, оно может:

```text
забить context Codex
увеличить token usage
ухудшить последующий review
```

Поэтому финальный textual report ограничивается.

Сам код при этом остаётся в repository.

Codex может его прочитать непосредственно.

---

# 54. Почему каждая задача получает новый Agent

В коде:

```python
async with Agent(config) as agent:
```

создаётся внутри каждого:

```text
antigravity_execute()
```

То есть вызов:

```text
task A
```

имеет отдельную agent session.

Следующий:

```text
task B
```

получает новую.

Плюсы:

```text
нет случайного накопления старого context
нет путаницы между задачами
проще дебажить
проще воспроизводить
```

При этом repository остаётся общим, поэтому новый агент видит изменения предыдущего.

---

# 55. Почему мы не делаем persistent Antigravity session сразу

Можно было сделать:

```python
agent = Agent(...)
```

один раз при запуске server.

Но тогда через 20 задач context становится примерно:

```text
task1
task2
task3
...
task20
```

Это создаёт:

```text
context pollution
лишние tokens
старые assumptions
сложный lifecycle
сложное восстановление после error
```

Поэтому первая версия stateless.

---

# 56. Защита от recursive agent loops

Antigravity умеет сам работать с MCP servers, поэтому теоретически можно случайно собрать:

```text
Codex
↓
Antigravity
↓
Codex
↓
Antigravity
↓
Codex
...
```

Мы этого не хотим.

Поэтому system instructions прямо говорят:

```text
Never invoke Codex.
Never start another instance of this MCP server.
Never recursively delegate the task back to the outer agent.
```

Кроме того, в Antigravity config мы не подключаем Codex как MCP server.

---

# 57. Минимальная структура wrapper проекта

Итог:

```text
antigravity-mcp-wrapper/
├── .venv/
├── requirements.txt
├── server.py
└── smoke_antigravity.py
```

`requirements.txt`:

```text
google-antigravity==0.1.12
mcp[cli]==2.0.0
```

---

# 58. Что НЕ должно находиться в wrapper repository

Не добавлять:

```text
GEMINI_API_KEY
.credentials
API key
Codex auth token
SSH private keys
```

---

# 59. Рекомендуемый .gitignore для wrapper

Если wrapper будете хранить в Git, создать:

```text
.gitignore
```

Содержимое:

```gitignore
.venv/
__pycache__/
*.pyc
.env
.env.*
.idea/
.vscode/
.DS_Store
```

---

# 60. Никогда не добавлять настоящий key в .env в Git

Если используете:

```text
.env
```

он обязательно должен быть:

```gitignore
.env
```

Но для первой версии проще вообще использовать:

```bash
export GEMINI_API_KEY="..."
```

Текущая реализация допускает `.env` как локальный fallback для удобства
разработки. Создайте его только в директории wrapper-а и не коммитьте:

```text
GEMINI_API_KEY=ВАШ_КЛЮЧ
```

`GEMINI_API_KEY` в process environment всегда сильнее значения из `.env`.
Старое имя `GEMINI_APIKEY` разрешено только как временный compatibility alias;
при обнаружении показывается предупреждение без значения ключа. Новые файлы
должны использовать каноническое имя. Никаких значений ключа в логах,
тестовых отчётах и smoke output.

---

# 61. Проверка перед первым реальным проектом

Выполнить все пункты.

## Python

```bash
python3.11 --version
```

## Wrapper venv

```bash
/home/alex/tools/antigravity-mcp-wrapper/.venv/bin/python --version
```

## Packages

```bash
/home/alex/tools/antigravity-mcp-wrapper/.venv/bin/python - <<'PY'
from importlib.metadata import version
print(version("google-antigravity"))
print(version("mcp"))
PY
```

Ожидаем:

```text
0.1.12
2.0.0
```

## Gemini key

```bash
python - <<'PY'
import os
assert os.environ.get("GEMINI_API_KEY"), "GEMINI_API_KEY missing"
print("key configured")
PY
```

## Repository

```bash
cd /ABSOLUTE/PATH/TO/YOUR/PROJECT
git status
```

## MCP list

```bash
codex mcp list
```

---

# 62. Troubleshooting: ModuleNotFoundError: google.antigravity

Ошибка:

```text
ModuleNotFoundError: No module named 'google.antigravity'
```

Почти всегда означает, что Codex запускает неправильный Python.

Проверить config:

```toml
command = "/.../antigravity-mcp-wrapper/.venv/bin/python"
```

Не:

```toml
command = "python"
```

Проверить вручную:

```bash
/.../.venv/bin/python - <<'PY'
import google.antigravity
print("OK")
PY
```

---

# 63. Troubleshooting: ModuleNotFoundError: mcp

Ошибка:

```text
ModuleNotFoundError: No module named 'mcp'
```

Активировать wrapper venv:

```bash
cd ~/tools/antigravity-mcp-wrapper
source .venv/bin/activate
```

Установить:

```bash
python -m pip install -r requirements.txt
```

---

# 64. Troubleshooting: FastMCP import error

Если junior скопировал старый пример:

```python
from mcp.server.fastmcp import FastMCP
```

для нашего pinned MCP v2 его нужно заменить.

Правильно:

```python
from mcp.server import MCPServer
```

И:

```python
mcp = MCPServer("Antigravity Coding Executor")
```

---

# 65. Troubleshooting: GEMINI_API_KEY missing

Если Antigravity сообщает отсутствие credentials:

Проверить, что в executor allowlist входит:

```bash
python - <<'PY'
import os
print(bool(os.environ.get("GEMINI_API_KEY")))
PY
```

Если:

```text
False
```

сделать:

```bash
export GEMINI_API_KEY="..."
```

После этого **перезапустить Codex из этого terminal**:

```bash
codex
```

Если ключ хранится в `.env`, каноническое имя должно быть ровно:

```text
GEMINI_API_KEY
```

Старое имя `GEMINI_APIKEY` допускается только как deprecated compatibility
alias, когда `GEMINI_API_KEY` отсутствует; wrapper выдаёт предупреждение и не
печатает значение. Не выводите значение ключа в shell или лог; проверяйте только
наличие канонической переменной (`bool(os.environ.get("GEMINI_API_KEY"))`).
Process environment имеет приоритет над `.env`. После исправления имени или
изменения `.env` перезапустите Codex/MCP process, чтобы loader перечитал
настройки.

---

# 65b. Troubleshooting: quota `429` и временная недоступность `503`

Ошибка `429` обычно означает исчерпанную квоту или rate limit Gemini. На
бесплатном tier это может быть дневной лимит, ограничение запросов в минуту
или временно недоступная конкретная модель. Ошибка `503` (`high demand`,
`overloaded`) означает перегрузку сервиса, а не обязательно неверный ключ.

Проверьте billing/quota в панели провайдера и фактическую модель, затем
повторите небольшой smoke test. Не настраивайте бесконечные retries: wrapper
должен завершать вызов с понятной ошибкой после ограниченного числа попыток;
бесконечный retry создаёт очередь, расходует квоту и скрывает реальную
проблему. Для `503` допустим короткий ограниченный backoff, после чего нужно
сообщить об ошибке и повторить позже вручную. Для `429` сначала ждите сброса
квоты или используйте разрешённый тариф/модель — повтор без изменения условий
обычно не помогает.

---

# 65a. Troubleshooting: `tModel: model is empty`

Ошибка:

```text
tModel: model is empty
```

Проверьте, что builder передаёт имя модели непосредственно в `ModelTarget`, а
не только `GeminiModelOptions` внутри endpoint:

```python
ModelTarget(
    name="gemini-3.7-flash",
    endpoint=GeminiAPIEndpoint(
        options=GeminiModelOptions(
            thinking_level=ThinkingLevel(thinking_level)
        )
    ),
)
```

Для pinned `google-antigravity==0.1.12` ожидаемое имя —
`gemini-3.7-flash`; после изменения перезапустите MCP process и повторите
минимальный smoke test.

---

# 66. Troubleshooting: Codex MCP server starts, но tool отсутствует

Проверить:

```bash
codex mcp list
```

Затем config:

```toml
enabled = true
```

Проверить:

```toml
enabled_tools = [
    "antigravity_execute"
]
```

Проверить syntax server:

```bash
python -m py_compile server.py
```

Проверить через Inspector:

```bash
cd /YOUR/REPO

/PATH/TO/WRAPPER/.venv/bin/mcp \
    dev \
    /PATH/TO/WRAPPER/server.py
```

---

# 67. Troubleshooting: MCP protocol / JSON error

Например Codex сообщает:

```text
invalid JSON
protocol error
unexpected token
```

Первое подозрение:

```python
print(...)
```

в `server.py`.

Найти:

```bash
grep -n "print(" server.py
```

В рабочем `server.py` обычных `print()` быть не должно.

Логи:

```python
logging
```

и только:

```python
stream=sys.stderr
```

---

# 68. Troubleshooting: Antigravity читает файлы, но не изменяет

Проверить наличие:

```python
capabilities=CapabilitiesConfig()
```

Официальная документация указывает, что это включает write capabilities.

---

# 69. Troubleshooting: Antigravity изменяет файлы, но не может запускать тесты

Проверить:

```python
policies=[
    policy.deny_all(),
    *[policy.allow(tool.value) for tool in EXECUTOR_ALLOWED_TOOLS],
]
```

`run_command` остаётся OS trust boundary: allowlist снимает только policy-
ограничение инструмента, но не создаёт sandbox. Для недоверенного кода
запускайте wrapper внутри sandbox/container/VM.

---

# 70. Troubleshooting: Agent работает не в том repository

`cwd` должен указывать именно на корень Git repository. Wrapper проверяет
наличие `.git` непосредственно в этом каталоге; marker может быть каталогом
обычного repository или файлом `gitdir: ...` у linked worktree. Подкаталог
repository не подходит: у него нет собственного `.git` marker.

Если видите ошибку:

```text
Workspace must be the Git repository root with a .git file or directory
```

перейдите в нужный корень и проверьте:

```bash
cd /YOUR/PROJECT
test -e .git
git status
```

Для worktree проверьте, что `.git` — файл, а `git status` выполняется.

Спросить Codex выполнить:

```text
Вызови antigravity_execute с задачей только сообщить workspace path,
не меняя файлы.
```

В результате должен быть:

```json
"workspace": "/правильный/project"
```

Если нет — ошибка в:

```toml
cwd = "..."
```

Ошибки выполнения для клиента намеренно обобщаются: provider exception,
секреты, локальные пути и прочие внутренние детали не возвращаются и не
выводятся целиком в лог.

---

# 71. Troubleshooting: timeout

Если видим:

```json
"error_type": "timeout"
```

первое — определить, нормальна ли длительность задачи.

Для более тяжёлого repository можно увеличить:

```toml
tool_timeout_sec = 1800
```

И:

```toml
[mcp_servers.antigravity_executor.env]
ANTIGRAVITY_TASK_TIMEOUT_SEC = "1740"
```

Сохраняйте примерно:

```text
wrapper timeout
<
Codex timeout
```

Например:

```text
1740 < 1800
```

---

# 72. Troubleshooting: два агента конфликтуют

Сериализацию обеспечивает Python wrapper. Проверить:

```python
EXECUTION_LOCK = asyncio.Lock()
```

и:

```python
async with EXECUTION_LOCK:
```

Ключа Codex config для этого не требуется: каждый вызов ждёт освобождения
`EXECUTION_LOCK` и только затем запускает Antigravity. Общий
`asyncio.wait_for(...)` должен оборачивать весь участок с
`async with EXECUTION_LOCK`, чтобы ожидание очереди также учитывалось в
`TASK_TIMEOUT_SEC`.

---

# 73. Troubleshooting: тесты не находятся

Antigravity `run_command` наследует рабочую среду процесса.

Проверить вручную из repository:

```bash
cd /YOUR/PROJECT
pytest
```

или:

```bash
npm test
```

Если command не существует даже в обычном terminal, проблема не в MCP wrapper.

---

# 74. Troubleshooting Windows

На Windows virtual environment Python обычно:

```text
C:\Users\alex\tools\antigravity-mcp-wrapper\.venv\Scripts\python.exe
```

В TOML удобнее использовать `/`:

```toml
command = "C:/Users/alex/tools/antigravity-mcp-wrapper/.venv/Scripts/python.exe"

args = [
    "C:/Users/alex/tools/antigravity-mcp-wrapper/server.py"
]

cwd = "C:/Users/alex/projects/my-project"
```

Gemini key PowerShell:

```powershell
$env:GEMINI_API_KEY="..."
```

Проверка:

```powershell
python -c "import os; print(bool(os.environ.get('GEMINI_API_KEY')))"
```

---

# 75. Definition of Done

Wrapper считается готовым только если выполнены **ВСЕ** пункты:

- [ ] Python `>=3.10`.
- [ ] Создан отдельный wrapper directory.
- [ ] Создан `.venv`.
- [ ] `google-antigravity` установлен из PyPI.
- [ ] `mcp 2.x` установлен.
- [ ] `from google.antigravity import Agent` работает.
- [ ] `from mcp.server import MCPServer` работает.
- [ ] `GEMINI_API_KEY` передаётся через process environment, предпочтительно
      через Codex `env_vars`.
- [ ] Secure stdlib loader использует process environment перед `.env` рядом с
      wrapper-ом; `python-dotenv` не установлен и не нужен.
- [ ] `.env` остаётся в `.gitignore`, значения ключа нигде не логируются.
- [ ] Новые конфигурации используют точное имя `GEMINI_API_KEY`; alias
      `GEMINI_APIKEY` допускается только как deprecated compatibility fallback
      с предупреждением.
- [ ] `ModelTarget` содержит явное `name="gemini-3.7-flash"` вместе с
      `GeminiAPIEndpoint(options=...)`; runtime не выдаёт `tModel: model is empty`.
- [ ] В `server.py` нет stdout `print()`.
- [ ] MCP server запускается через stdio.
- [ ] MCP Inspector видит `antigravity_execute`.
- [ ] MCP Inspector показывает четвёртый параметр `thinking_level` с enum
      `low | medium | high` и default `medium`.
- [ ] Tool реально может прочитать repository.
- [ ] Tool реально может создать файл.
- [ ] Tool реально может изменить файл.
- [ ] Tool реально может выполнить command.
- [ ] Tool реально может выполнить test.
- [ ] Codex видит `antigravity_executor`.
- [ ] Codex видит `antigravity_execute`.
- [ ] Codex может вызвать tool.
- [ ] Antigravity изменяет именно нужный repository.
- [ ] После tool call Codex видит изменения через `git diff`.
- [ ] Одновременные agent calls сериализуются через `EXECUTION_LOCK`.
- [ ] `get_workspace()` принимает только cwd с `.git` file/directory marker;
      linked worktree с `.git`-файлом поддерживается.
- [ ] Все четыре аргумента tool валидируются до проверки workspace и ожидания
      `EXECUTION_LOCK`.
- [ ] Общий wrapper timeout включает ожидание lock и execution и меньше Codex
      timeout.
- [ ] Client errors sanitised: exception details не возвращаются и не
      логируются целиком.
- [ ] Executor policy deny-by-default и разрешает только `LIST_DIR`,
      `FIND_FILE`, `SEARCH_DIR`, `VIEW_FILE`, `CREATE_FILE`, `EDIT_FILE`,
      `RUN_COMMAND`, `FINISH`.
- [ ] Read-only smoke policy разрешает только `LIST_DIR`, `FIND_FILE`,
      `SEARCH_DIR`, `VIEW_FILE`, `FINISH`; запись и shell отключены.
- [ ] `RUN_COMMAND` документирован как OS trust boundary; sandbox обязателен
      для недоверенных задач.
- [ ] API key не записан в repository.
- [ ] Antigravity не вызывает Codex рекурсивно.
- [ ] После Antigravity Codex самостоятельно review-ит изменения.

---

# 76. Финальный рабочий config.toml — шаблон

Скопировать и заменить три пути.

```toml
[mcp_servers.antigravity_executor]

command = "/ABSOLUTE/PATH/TO/antigravity-mcp-wrapper/.venv/bin/python"

args = [
    "/ABSOLUTE/PATH/TO/antigravity-mcp-wrapper/server.py"
]

cwd = "/ABSOLUTE/PATH/TO/target-repository"

enabled = true

required = true

startup_timeout_sec = 30

tool_timeout_sec = 900

enabled_tools = [
    "antigravity_execute"
]

env_vars = [
    "GEMINI_API_KEY"
]

[mcp_servers.antigravity_executor.env]
ANTIGRAVITY_TASK_TIMEOUT_SEC = "840"
ANTIGRAVITY_MAX_RESULT_CHARS = "30000"
```

---

# 77. Финальная команда запуска

Linux/macOS:

```bash
export GEMINI_API_KEY="ВАШ_GEMINI_API_KEY"

cd /ABSOLUTE/PATH/TO/target-repository

codex
```

Если ключ хранится в локальном `.env` wrapper-а, отдельный `export` не нужен:
loader прочитает его при старте MCP process. Всё равно оставляйте
`env_vars = ["GEMINI_API_KEY"]` в config и не помещайте `.env` в Git.

---

# 78. Финальный test prompt для Codex

После запуска Codex отправить:

```text
Проверь доступный MCP server antigravity_executor.

Используй antigravity_execute для следующей тестовой задачи:

task:
Создай в корне repository файл mcp_integration_test.txt.

Файл должен содержать ровно одну строку:

Codex -> MCP -> Antigravity integration works

verification:
Проверь существование файла и его содержимое.

thinking_level:
medium

После завершения Antigravity:
1. самостоятельно прочитай созданный файл;
2. самостоятельно проверь git diff;
3. сообщи, прошёл ли integration test.

Никаких других файлов не изменяй.
```

После этого вручную:

```bash
cat mcp_integration_test.txt
```

Ожидаем:

```text
Codex -> MCP -> Antigravity integration works
```

Проверить:

```bash
git diff
```

После теста:

```bash
rm mcp_integration_test.txt
```

---

# 79. Финальный production-like prompt

После успешного integration test можно использовать такую модель:

```text
Ты главный coding orchestrator.

Для этой задачи:

1. Сначала самостоятельно исследуй repository.
2. Составь минимальный implementation plan.
3. Определи конкретную задачу для Antigravity.
4. Используй MCP tool antigravity_execute для реализации.
5. Передай Antigravity достаточный context и verification criteria.
6. После возврата Antigravity НЕ доверяй результату автоматически.
7. Самостоятельно проверь git diff.
8. Самостоятельно проверь изменённые файлы.
9. Самостоятельно запусти релевантные tests/lint/typecheck.
10. Если изменения корректны — заверши задачу.
11. Если есть небольшие ошибки — исправь самостоятельно.
12. Если требуется существенный rework — повторно вызови antigravity_execute,
    передав конкретный review feedback.

Не позволяй Antigravity вызывать Codex или делегировать задачу обратно.
```

---

# 80. Итоговая архитектура после выполнения инструкции

```text
┌────────────────────────────────────────────────────────────┐
│                         USER                               │
└─────────────────────────────┬──────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────┐
│                         CODEX                              │
│                                                            │
│  анализирует задачу                                       │
│  исследует repository                                     │
│  формирует implementation request                         │
└─────────────────────────────┬──────────────────────────────┘
                              │
                              │ MCP / stdio
                              │
                              ▼
┌────────────────────────────────────────────────────────────┐
│               ANTIGRAVITY MCP WRAPPER                     │
│                                                            │
│  tool: antigravity_execute                                │
│                                                            │
│  task                                                      │
│  context                                                   │
│  verification                                              │
│  thinking_level: low | medium | high (default: medium)      │
│                                                            │
│  concurrency lock                                          │
│  timeout                                                   │
│  workspace resolution                                      │
│  error handling                                            │
└─────────────────────────────┬──────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────┐
│                GOOGLE ANTIGRAVITY AGENT                    │
│                                                            │
│  inspect                                                   │
│     ↓                                                      │
│  search                                                    │
│     ↓                                                      │
│  edit                                                      │
│     ↓                                                      │
│  run command                                               │
│     ↓                                                      │
│  test                                                      │
│     ↓                                                      │
│  debug                                                     │
│     ↓                                                      │
│  edit                                                      │
│     ↓                                                      │
│  verify                                                    │
└─────────────────────────────┬──────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────┐
│                     SHARED REPOSITORY                      │
└─────────────────────────────┬──────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────┐
│                         CODEX                              │
│                                                            │
│  git diff                                                  │
│  review                                                    │
│  tests                                                     │
│  final decision                                            │
└────────────────────────────────────────────────────────────┘
```

---

# 81. Главное правило архитектуры

Запомнить одной строкой:

```text
Codex думает и контролирует.
Antigravity автономно исполняет.
Repository является общей точкой состояния.
MCP является границей делегирования.
```

Именно такую архитектуру должен собрать разработчик.
