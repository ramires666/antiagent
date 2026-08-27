"""Deterministic stdio fixture; never starts the real Antigravity CLI."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import agy_server
from agent_manager import AgentStore

CANCEL_SEEN = False
FIXTURE_STATE = tempfile.TemporaryDirectory(prefix="antiagent-mcp-fixture-")
agy_server._AGENT_STORE = AgentStore(
    Path(FIXTURE_STATE.name) / "agents.sqlite3", owner_id="protocol-fixture"
)


async def _fake_run(argv, cwd, timeout_seconds):
    global CANCEL_SEEN
    prompt = argv[argv.index("-p") + 1]
    if "cancellation state probe" in prompt:
        response = "cancelled-seen" if CANCEL_SEEN else "not-cancelled"
        return 0, _success(response), False
    if "default mode protocol probe" in prompt:
        mode = argv[argv.index("--mode") + 1]
        return 0, _success("default-mode-" + mode), False
    if "slow protocol probe" in prompt:
        try:
            delay = 3.0 if "cancellation" in prompt else 0.35
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            CANCEL_SEEN = True
            raise
    return 0, _success("fixture-ok"), False


def _success(response):
    return (
        '{"status":"SUCCESS","response":"' + response
        + '","usage":{"total_tokens":1},'
        '"conversation_id":"123e4567-e89b-12d3-a456-426614174000"}'
    )


agy_server._resolve_cli = lambda: cwd_cli()
agy_server._run_cli = _fake_run
agy_server._probe_cli_version = lambda _cli: "1.1.22"


def cwd_cli():
    # The value is never executed: _run_cli is patched above.
    return __import__("pathlib").Path("fixture-cli")


if __name__ == "__main__":
    agy_server.mcp.run(transport="stdio")
