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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("antigravity-mcp")
mcp = MCPServer("Antigravity Coding Executor")

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
    try:
        value = int(os.environ.get(name, ""))
        if value > 0:
            return value
    except ValueError:
        pass
    if name in os.environ:
        logger.warning("Invalid %s=%r; using %s", name, os.environ[name], default)
    return default


TASK_TIMEOUT_SEC = read_positive_int_env(
    "ANTIGRAVITY_TASK_TIMEOUT_SEC", DEFAULT_TASK_TIMEOUT_SEC
)
MAX_RESULT_CHARS = read_positive_int_env(
    "ANTIGRAVITY_MAX_RESULT_CHARS", DEFAULT_MAX_RESULT_CHARS
)
EXECUTION_LOCK = asyncio.Lock()

SYSTEM_INSTRUCTIONS = """
You are an autonomous coding executor working for OpenAI Codex. Inspect the
repository and its instructions, then implement only the requested task using
the smallest correct change. You may read and modify files and run commands and
tests. Iterate on relevant failures and finish with a concise implementation and
verification report. Never invoke Codex, start this MCP server, delegate back to
the outer agent, push or commit, use destructive repository-wide commands,
discard unrelated changes, or modify files outside the configured workspace.
""".strip()


def get_workspace() -> Path:
    workspace = Path.cwd().resolve()
    git_marker = workspace / ".git"
    if not workspace.is_dir() or not (git_marker.is_dir() or git_marker.is_file()):
        raise RuntimeError("Current working directory is not a Git repository root")
    return workspace


def build_agent_prompt(
    task: str, context: str, verification: str, workspace: Path
) -> str:
    parts = [f"WORKSPACE\n\n{workspace}\n\nTASK\n\n{task.strip()}"]
    if context.strip():
        parts.append(f"ADDITIONAL CONTEXT\n\n{context.strip()}")
    if verification.strip():
        parts.append(f"REQUIRED VERIFICATION\n\n{verification.strip()}")
    parts.append(
        "EXECUTION REQUIREMENT\n\nActually implement the task, run relevant "
        "verification, and iterate on failures. Do not only describe a solution."
    )
    return "\n\n".join(parts)


def truncate_result(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_RESULT_CHARS:
        return text, False
    return (
        text[:MAX_RESULT_CHARS]
        + "\n\n[Antigravity result truncated by MCP wrapper]",
        True,
    )


def build_model_target(
    thinking_level: ThinkingLevelName,
) -> ModelTarget:
    if thinking_level not in ALLOWED_THINKING_LEVELS:
        raise ValueError("thinking_level must be low, medium, or high")
    return ModelTarget(
        endpoint=GeminiAPIEndpoint(
            options=GeminiModelOptions(thinking_level=ThinkingLevel(thinking_level))
        )
    )


async def execute_with_antigravity(
    *, workspace: Path, prompt: str, thinking_level: ThinkingLevelName
) -> str:
    async with EXECUTION_LOCK:
        config = LocalAgentConfig(
            system_instructions=SYSTEM_INSTRUCTIONS,
            capabilities=CapabilitiesConfig(
                enabled_tools=list(EXECUTOR_ALLOWED_TOOLS), enable_subagents=False
            ),
            workspaces=[str(workspace)],
            policies=[
                policy.deny_all(),
                *[policy.allow(tool.value) for tool in EXECUTOR_ALLOWED_TOOLS],
            ],
            model=build_model_target(thinking_level),
        )
        async with Agent(config) as agent:
            response = await agent.chat(prompt)
            return await response.text()


@mcp.tool()
async def antigravity_execute(
    task: str,
    context: str = "",
    verification: str = "",
    thinking_level: ThinkingLevelName = "medium",
) -> dict[str, Any]:
    """Implement a coding task in the current workspace with Antigravity.

    thinking_level selects Gemini reasoning effort: low, medium, or high.
    After completion, Codex must inspect the diff and verify independently.
    """
    started_at = time.monotonic()
    if not isinstance(task, str) or not task.strip():
        return {
            "status": "error",
            "error_type": "invalid_request",
            "message": "task must be a non-empty string",
            "thinking_level": thinking_level,
        }
    if not isinstance(thinking_level, str) or thinking_level not in ALLOWED_THINKING_LEVELS:
        return {
            "status": "error",
            "error_type": "invalid_request",
            "message": "thinking_level must be low, medium, or high",
            "thinking_level": thinking_level,
        }
    if not isinstance(context, str) or not isinstance(verification, str):
        return {
            "status": "error",
            "error_type": "invalid_request",
            "message": "context and verification must be strings",
            "thinking_level": thinking_level,
        }

    workspace: Path | None = None
    try:
        workspace = get_workspace()
        prompt = build_agent_prompt(task, context, verification, workspace)
        logger.info("Received task for workspace=%s thinking=%s", workspace, thinking_level)
        result = await asyncio.wait_for(
            execute_with_antigravity(
                workspace=workspace,
                prompt=prompt,
                thinking_level=thinking_level,
            ),
            timeout=TASK_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        logger.error("Antigravity task timed out after %s seconds", TASK_TIMEOUT_SEC)
        return {
            "status": "error",
            "error_type": "timeout",
            "workspace": str(workspace),
            "duration_seconds": round(time.monotonic() - started_at, 2),
            "thinking_level": thinking_level,
            "message": f"Execution exceeded {TASK_TIMEOUT_SEC} seconds.",
        }
    except Exception as exc:
        logger.error("Antigravity execution failed (%s)", type(exc).__name__)
        response = {
            "status": "error",
            "error_type": type(exc).__name__,
            "duration_seconds": round(time.monotonic() - started_at, 2),
            "thinking_level": thinking_level,
            "message": "Antigravity execution failed.",
        }
        if workspace is not None:
            response["workspace"] = str(workspace)
        return response

    result, was_truncated = truncate_result(result)
    duration = round(time.monotonic() - started_at, 2)
    logger.info("Antigravity task completed in %.2f seconds", duration)
    return {
        "status": "completed",
        "workspace": str(workspace),
        "duration_seconds": duration,
        "thinking_level": thinking_level,
        "result_truncated": was_truncated,
        "result": result,
        "next_action": "Codex should inspect git diff and verify independently.",
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
