"""Minimal read-only smoke test for the agy_server CLI adapter."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
from pathlib import Path


MARKER = "AGY_SMOKE_READONLY_MARKER"


def git_root() -> Path:
    from agy_server import _git_preflight

    root = _git_preflight().root
    if root is None:
        raise RuntimeError("current directory is not a Git repository root") from None
    return root


def git_status(root: Path) -> object:
    from agy_server import _git_status_snapshot

    snapshot = _git_status_snapshot(root)
    if snapshot is None:
        raise RuntimeError("Git status is unavailable")
    return snapshot


async def run_smoke() -> dict[str, object]:
    # Keep the import inside the smoke path so importing this module has no side effects.
    from agy_server import execute_with_antigravity_cli

    root = git_root()
    prompt = (
        "Read-only smoke test. Do not edit, create, delete, or run commands. "
        f"Inspect only the repository root and return exactly this marker: {MARKER}"
    )
    before = git_status(root)
    result = await execute_with_antigravity_cli(
        workspace=root,
        prompt=prompt,
        thinking_level="low",
        mode="plan",
    )
    after = git_status(root)
    text = result.get("result", "") if isinstance(result, dict) else ""
    completed = isinstance(result, dict) and result.get("status") == "SUCCESS"
    return {
        "status": "ok" if completed and MARKER in text and before == after else "error",
        "marker_found": MARKER in text,
        "git_status_unchanged": before == after,
        "workspace_is_git_root": True,
        "thinking_level": "low",
        "mode": "plan",
    }


def main() -> None:
    try:
        # The adapter may emit SDK diagnostics; never expose them from smoke.
        with contextlib.redirect_stderr(io.StringIO()):
            summary = asyncio.run(run_smoke())
    except Exception as exc:  # safe type-only failure summary
        summary = {
            "status": "error",
            "error_type": type(exc).__name__,
        }
    print(json.dumps(summary, ensure_ascii=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
