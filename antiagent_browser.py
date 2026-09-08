"""Opt-in stdio launcher for a locally installed Chrome DevTools MCP.

No passwords, cookies, profile contents, or remote debugging endpoints enter
this interface. The parent executor selects one mode in its child environment.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager, nullcontext
import json
import os
from pathlib import Path
import stat
import sys
from typing import Iterator, Sequence
from urllib.parse import urlsplit

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server import MCPServer
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent


MODE_ENV = "ANTIAGENT_BROWSER_MODE"
MODES = ("disabled", "isolated", "user_session")
SERVER_NAME = "antiagent_browser"
ALLOWED_TOOLS = frozenset({
    "new_page", "list_pages", "select_page", "close_page", "navigate_page",
    "take_snapshot", "take_screenshot", "click", "fill", "fill_form",
    "press_key", "hover", "drag", "handle_dialog", "wait_for", "resize_page",
})


def _allowed_call(name: str, args: dict) -> bool:
    if name not in ALLOWED_TOOLS:
        return False
    # No injected scripts, file transfers, or navigation to local files/code.
    if any(key in args for key in ("initScript", "script", "filePath", "filePaths")):
        return False
    if "url" in args:
        value = args["url"]
        if not isinstance(value, str):
            return False
        try:
            url = urlsplit(value)
            if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
                return False
        except ValueError:
            return False
    return True


class BrowserError(RuntimeError):
    """Safe diagnostic which contains no browser or process output."""


def _executable_file(value: str | None, label: str) -> Path:
    if not value or not Path(value).is_absolute():
        raise BrowserError(f"{label} must be an absolute file path")
    try:
        path = Path(value).resolve(strict=True)
        if not path.is_file():
            raise OSError()
    except (OSError, RuntimeError, ValueError):
        raise BrowserError(f"{label} is unavailable") from None
    return path


def browser_argv(mode: str, node: str | None, script: str | None) -> list[str]:
    if mode not in ("isolated", "user_session"):
        raise BrowserError("Browser mode must be isolated or user_session")
    node_path = _executable_file(node, "Node executable")
    script_path = _executable_file(script, "Chrome DevTools MCP script")
    if node_path.suffix.lower() in (".cmd", ".bat", ".ps1"):
        raise BrowserError("Node must be a native executable, not a shell script")
    return [
        str(node_path), str(script_path),
        "--no-usage-statistics", "--no-performance-crux",
        "--no-category-network", "--no-category-performance",
        "--isolated" if mode == "isolated" else "--autoConnect",
    ]


def registration_config(node: str | None, script: str | None) -> dict:
    # Validate without starting Node or looking at any browser profile.
    argv = browser_argv("isolated", node, script)
    return {"mcpServers": {SERVER_NAME: {
        "command": str(Path(sys.executable).resolve()),
        "args": ["-I", "-m", "antiagent_browser", "--node", argv[0],
                 "--script", argv[1]],
    }}}


@contextmanager
def user_session_lock() -> Iterator[None]:
    """One cooperating browser bridge per OS user, across all workspaces.

    A fixed per-user location deliberately ignores workspace/state overrides.
    The OS releases the lock on exit, including abnormal process termination.
    """
    directory = Path.home() / ".antiagent-browser"
    if directory.is_symlink() or (
        hasattr(directory, "is_junction") and directory.is_junction()
    ):
        raise BrowserError("Browser lock directory must not be a link")
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / "user-session.lock"
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise BrowserError("Browser lock must not be a link")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    locked = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise BrowserError("Browser lock must be a regular file")
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise BrowserError("User browser session is busy; wait for its owner to finish") from None
        yield
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _browser_environment() -> dict[str, str]:
    env = dict(os.environ)
    # Never inherit debugging that can spill page contents to disk or stderr.
    for key in list(env):
        if key.upper().startswith("DEBUG") or key.upper() in ("NODE_OPTIONS", "NODE_DEBUG"):
            env.pop(key)
    env["CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS"] = "1"
    env["CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS"] = "1"
    return env


async def _serve_browser(argv: list[str]) -> None:
    params = StdioServerParameters(command=argv[0], args=argv[1:], env=_browser_environment())
    with open(os.devnull, "w") as errors:
        async with stdio_client(params, errlog=errors) as (reader, writer):
            async with ClientSession(reader, writer) as upstream:
                async with asyncio.timeout(20):
                    await upstream.initialize()
                    listed = await upstream.list_tools()
                exposed = []
                for tool in listed.tools:
                    if tool.name not in ALLOWED_TOOLS:
                        continue
                    schema = dict(tool.input_schema)
                    schema["properties"] = {key: value for key, value in schema.get("properties", {}).items()
                                            if key not in ("initScript", "script", "filePath", "filePaths")}
                    exposed.append(tool.model_copy(update={"input_schema": schema}))
                names = {tool.name for tool in exposed}

                async def list_tools(_ctx, _params):
                    return ListToolsResult(tools=exposed)

                async def call_tool(_ctx, request):
                    args = request.arguments or {}
                    if request.name not in names or not _allowed_call(request.name, args):
                        return CallToolResult(is_error=True, content=[TextContent(type="text", text="Browser operation is not permitted by this adapter")])
                    try:
                        async with asyncio.timeout(45):
                            return await upstream.call_tool(request.name, args)
                    except Exception:
                        return CallToolResult(is_error=True, content=[TextContent(type="text", text="Browser operation failed or timed out")])

                server = Server(SERVER_NAME, on_list_tools=list_tools, on_call_tool=call_tool)
                async with stdio_server() as (incoming, outgoing):
                    await server.run(incoming, outgoing, server.create_initialization_options())


def _run_browser(argv: list[str]) -> int:
    asyncio.run(_serve_browser(argv))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", help="Absolute path to the installed Node executable")
    parser.add_argument("--script", help="Absolute path to chrome-devtools-mcp/build/src/bin/chrome-devtools-mcp.js")
    parser.add_argument("--print-config", action="store_true", help="Print registration JSON; change nothing")
    args = parser.parse_args(argv)
    try:
        if args.print_config:
            print(json.dumps(registration_config(args.node, args.script), indent=2))
            return 0
        mode = os.environ.get(MODE_ENV, "disabled")
        if mode not in MODES:
            raise BrowserError("Invalid browser mode")
        if mode == "disabled":
            # A valid empty MCP server avoids failed startup in ordinary coding
            # runs, and does not even resolve a browser/Node installation.
            MCPServer(SERVER_NAME).run()
            return 0
        command = browser_argv(mode, args.node, args.script)
        with user_session_lock() if mode == "user_session" else nullcontext():
            return _run_browser(command)
    except Exception as exc:
        message = str(exc) if isinstance(exc, BrowserError) else "Browser bridge could not start"
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
