"""Deterministic stdio fixture; never starts the real Antigravity CLI."""

from __future__ import annotations

import asyncio

import agy_server

CANCEL_SEEN = False


async def _fake_run(argv, cwd, timeout_seconds):
    global CANCEL_SEEN
    prompt = argv[argv.index("-p") + 1]
    if "cancellation state probe" in prompt:
        response = "cancelled-seen" if CANCEL_SEEN else "not-cancelled"
        return 0, '{"status":"SUCCESS","response":"' + response + '"}', False
    if "slow protocol probe" in prompt:
        try:
            delay = 3.0 if "cancellation" in prompt else 0.35
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            CANCEL_SEEN = True
            raise
    return 0, '{"status":"SUCCESS","response":"fixture-ok"}', False


agy_server._resolve_cli = lambda: cwd_cli()
agy_server._run_cli = _fake_run


def cwd_cli():
    # The value is never executed: _run_cli is patched above.
    return __import__("pathlib").Path("fixture-cli")


if __name__ == "__main__":
    agy_server.mcp.run(transport="stdio")
