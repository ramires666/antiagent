from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from antiagent_setup import SetupError, resolve_mcp_launcher


EXPECTED_TOOLS = {
    "antigravity_agent_spawn",
    "antigravity_agent_list",
    "antigravity_agent_status",
    "antigravity_agent_wait",
    "antigravity_agent_followup",
    "antigravity_agent_interrupt",
    "antigravity_doctor",
    "antigravity_cli_execute",
}

EXPECTED_EXECUTION_OUTPUT_FIELDS = {
    "status",
    "result",
    "model",
    "thinking_level",
    "mode",
    "usage",
    "conversation_id",
    "result_truncated",
    "error_type",
    "exit_code",
    "retryable",
    "run_id",
    "started_at",
    "finished_at",
    "duration_seconds",
    "cli_version",
    "metadata_complete",
    "usage_available",
    "conversation_id_available",
    "preexisting_dirty",
    "worktree_changed",
    "changed_paths",
    "postflight_complete",
    "requires_review",
    "payload_mode",
    "file_scope_enforced",
    "shell_denied",
    "feedback",
}

EXPECTED_DOCTOR_FIELDS = {
    "checks_passed",
    "cli_available",
    "cli_version",
    "execution_boundary_declared",
    "state_writable",
    "workspace_status",
    "auth_probe",
    "network_probe",
    "oauth_ready",
    "error_type",
}


class SmokeError(RuntimeError):
    """Raised when the installed MCP does not satisfy the current contract."""


def _validate_loaded_schema(tools: Sequence[Any]) -> None:
    execute = next(
        (tool for tool in tools if tool.name == "antigravity_cli_execute"), None
    )
    schema = None if execute is None else execute.output_schema
    properties = schema.get("properties") if isinstance(schema, Mapping) else None
    if not isinstance(properties, Mapping):
        raise SmokeError("installed MCP has no usable execution output schema")
    actual = set(properties)
    if actual != EXPECTED_EXECUTION_OUTPUT_FIELDS:
        missing = sorted(EXPECTED_EXECUTION_OUTPUT_FIELDS - actual)
        unexpected = sorted(actual - EXPECTED_EXECUTION_OUTPUT_FIELDS)
        details = []
        if missing:
            details.append(f"missing={','.join(missing)}")
        if unexpected:
            details.append(f"unexpected={','.join(unexpected)}")
        suffix = f" ({'; '.join(details)})" if details else ""
        raise SmokeError(
            "installed MCP schema is stale; run py -m antiagent_upgrade, "
            f"restart Codex completely, and retry{suffix}"
        )


def _validate_doctor(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != EXPECTED_DOCTOR_FIELDS:
        raise SmokeError("installed MCP doctor returned a malformed payload")
    for field in (
        "checks_passed",
        "cli_available",
        "execution_boundary_declared",
        "state_writable",
    ):
        if payload[field] is not True:
            raise SmokeError(f"installed MCP doctor check failed: {field}")
    if payload["workspace_status"] != "ready":
        raise SmokeError(
            "installed MCP doctor check failed: "
            f"workspace_status={payload['workspace_status']!r}"
        )
    if payload["error_type"] is not None:
        raise SmokeError(
            "installed MCP doctor returned an error: "
            f"{payload['error_type']!r}"
        )
    if payload["auth_probe"] != "unsupported":
        raise SmokeError("installed MCP doctor returned an invalid auth_probe")
    if payload["network_probe"] != "not_run":
        raise SmokeError("installed MCP doctor returned an invalid network_probe")
    if payload["oauth_ready"] != "unknown":
        raise SmokeError("installed MCP doctor returned an invalid oauth_ready")
    if payload["cli_version"] is not None and not isinstance(
        payload["cli_version"], str
    ):
        raise SmokeError("installed MCP doctor returned an invalid cli_version")
    return payload


async def probe(workspace: Path, launcher: Path) -> dict[str, object]:
    environment = dict(os.environ)
    environment["ANTIAGENT_EXECUTION_BOUNDARY"] = "host"
    parameters = StdioServerParameters(
        command=str(launcher),
        cwd=str(workspace),
        env=environment,
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=10.0) as session:
            await session.initialize()
            listed = await session.list_tools()
            doctor = await session.call_tool("antigravity_doctor", {})
    tool_names = {tool.name for tool in listed.tools}
    if tool_names != EXPECTED_TOOLS:
        raise SmokeError("installed MCP exposed an unexpected tool set")
    _validate_loaded_schema(listed.tools)
    if doctor.is_error:
        raise SmokeError("installed MCP doctor returned an error")
    doctor_payload = _validate_doctor(doctor.structured_content)
    return {
        "tools": sorted(tool_names),
        "doctor": doctor_payload,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only smoke test for the installed antiagent-mcp command."
    )
    parser.add_argument(
        "workspace",
        nargs="?",
        default=".",
        help="Exact Git-root to use as the inherited MCP working directory.",
    )
    parser.add_argument(
        "--launcher",
        help="Optional absolute antiagent-mcp path; otherwise resolve the pipx launcher.",
    )
    args = parser.parse_args(argv)
    try:
        workspace = Path(args.workspace).resolve()
        launcher = resolve_mcp_launcher(args.launcher)
        result = asyncio.run(probe(workspace, launcher))
    except (SmokeError, SetupError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    except Exception:
        print("ERROR: installed MCP smoke failed unexpectedly", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
