from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from antiagent_setup import resolve_mcp_launcher


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
            tool_names = {tool.name for tool in listed.tools}
            if tool_names != EXPECTED_TOOLS:
                raise RuntimeError("installed MCP exposed an unexpected tool set")
            doctor = await session.call_tool("antigravity_doctor", {})
            if doctor.is_error:
                raise RuntimeError("installed MCP doctor returned an error")
            return {
                "tools": sorted(tool_names),
                "doctor": doctor.structured_content,
            }


def main() -> None:
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
    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()
    launcher = resolve_mcp_launcher(args.launcher)
    print(json.dumps(asyncio.run(probe(workspace, launcher)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
