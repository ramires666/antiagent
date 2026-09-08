"""Bounded browser-bridge smoke; --live uses only a synthetic local web page."""

from __future__ import annotations

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
import threading
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def probe(node: str | None, script: str | None, mode: str, live: bool) -> dict:
    env = dict(os.environ)
    env["ANTIAGENT_BROWSER_MODE"] = mode
    args = [str(Path(__file__).with_name("antiagent_browser.py"))]
    if node:
        args.extend(["--node", node])
    if script:
        args.extend(["--script", script])
    params = StdioServerParameters(command=sys.executable, args=args, env=env)
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            if mode == "disabled":
                if names:
                    raise RuntimeError("Disabled bridge exposed tools")
                return {"mode": mode, "tools": 0, "live": False}
            if not {"new_page", "take_snapshot", "close_page"} <= names:
                raise RuntimeError("Browser tools are unavailable")
            if {"evaluate_script", "list_network_requests", "get_network_request"} & names:
                raise RuntimeError("Unexpected sensitive inspection tools exposed")
            for name, arguments in (
                ("evaluate_script", {"function": "() => 1"}),
                ("navigate_page", {"url": "javascript:void(0)"}),
                ("navigate_page", {"url": "https://example.test", "initScript": "void(0)"}),
            ):
                rejected = await session.call_tool(name, arguments)
                if not rejected.is_error:
                    raise RuntimeError("Browser bridge did not enforce its tool policy")
            result = {"mode": mode, "tools": len(names), "live": False}
            if not live:
                return result
            marker = "ANTIAGENT_BROWSER_" + uuid.uuid4().hex

            class Page(BaseHTTPRequestHandler):
                def do_GET(self):
                    body = f"<!doctype html><title>{marker}</title><h1>{marker}</h1>".encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *_):
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            page_id = None
            try:
                opened = await session.call_tool("new_page", {
                    "url": f"http://127.0.0.1:{server.server_port}/", "timeout": 20000,
                })
                if opened.is_error:
                    raise RuntimeError("Browser could not open the smoke page")
                text = "\n".join(getattr(block, "text", "") for block in opened.content)
                # DevTools may include the page title before its URL.
                match = re.search(r"(?m)^(\d+): [^\r\n]*http://127\.0\.0\.1:" + str(server.server_port) + r"/", text)
                if not match:
                    raise RuntimeError("Smoke page ID is unavailable")
                page_id = int(match[1])
                snapshot = await session.call_tool("take_snapshot", {"pageId": page_id})
                text = "\n".join(getattr(block, "text", "") for block in snapshot.content)
                if snapshot.is_error or marker not in text:
                    raise RuntimeError("Smoke marker not found in the browser snapshot")
                result["live"] = True
                result["marker_found"] = True
            finally:
                if page_id is not None:
                    await session.call_tool("close_page", {"pageId": page_id})
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
            return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node")
    parser.add_argument("--script")
    parser.add_argument("--mode", choices=("disabled", "isolated", "user_session"), default="disabled")
    parser.add_argument("--live", action="store_true", help="Open and close one synthetic local page")
    args = parser.parse_args()
    try:
        async def bounded():
            async with asyncio.timeout(50):
                return await probe(args.node, args.script, args.mode, args.live)
        print(json.dumps(asyncio.run(bounded())))
        return 0
    except Exception:
        # Raw browser/tool errors may contain user tab titles or URLs.
        print("Browser smoke failed; check installation, Chrome availability and connection approval.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
