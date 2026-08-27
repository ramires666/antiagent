from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPError


ROOT = Path(__file__).resolve().parent
SERVER = ROOT / "agy_server.py"
FIXTURE = ROOT / "_mcp_protocol_fixture.py"
TOOL_NAME = "antigravity_cli_execute"
INPUT_FIELDS = {
    "task",
    "context",
    "verification",
    "thinking_level",
    "mode",
    "working_directory",
    "acknowledge_review",
}
OUTPUT_FIELDS = {
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
}


@unittest.skipUnless(os.name == "nt", "Windows STDIO integration test")
class MCPProtocolTest(unittest.TestCase):
    @staticmethod
    def _server_parameters(cwd: Path, *, trust_repo: bool) -> StdioServerParameters:
        fake_cli = Path(os.environ["SYSTEMROOT"]) / "System32" / "where.exe"
        env = {"ANTIGRAVITY_CLI_PATH": str(fake_cli)}
        if trust_repo:
            # The managed test sandbox runs under a different SID than the checkout.
            env.update({
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": str(ROOT),
            })
        return StdioServerParameters(
            command=sys.executable,
            args=[str(FIXTURE)],
            cwd=str(cwd),
            env=env,
        )

    def test_real_stdio_protocol_and_session_survival(self):
        async def scenario():
            params = self._server_parameters(ROOT, trust_repo=True)
            with open(os.devnull, "w", encoding="utf-8") as errlog:
                async with stdio_client(params, errlog=errlog) as (read, write):
                    async with ClientSession(
                        read, write, read_timeout_seconds=5.0
                    ) as session:
                        initialized = await session.initialize()
                        self.assertEqual(
                            initialized.server_info.name,
                            "Antigravity CLI Executor",
                        )
                        self.assertIsNotNone(initialized.capabilities.tools)

                        listed = await session.list_tools()
                        self.assertEqual([tool.name for tool in listed.tools], [TOOL_NAME])
                        tool = listed.tools[0]
                        schema = tool.input_schema
                        self.assertEqual(set(schema["properties"]), INPUT_FIELDS)
                        self.assertEqual(schema["required"], ["task"])
                        self.assertEqual(schema["properties"]["task"]["type"], "string")
                        self.assertEqual(schema["properties"]["context"]["default"], "")
                        self.assertEqual(schema["properties"]["verification"]["default"], "")
                        self.assertEqual(
                            schema["properties"]["working_directory"]["default"], ""
                        )
                        self.assertEqual(
                            schema["properties"]["thinking_level"]["enum"],
                            ["low", "medium", "high"],
                        )
                        self.assertEqual(
                            schema["properties"]["thinking_level"]["default"],
                            "medium",
                        )
                        self.assertEqual(
                            schema["properties"]["mode"]["enum"],
                            ["plan", "accept-edits"],
                        )
                        self.assertEqual(
                            schema["properties"]["mode"]["default"],
                            "plan",
                        )
                        self.assertFalse(
                            schema["properties"]["acknowledge_review"]["default"]
                        )
                        self.assertIsNotNone(tool.output_schema)
                        assert tool.output_schema is not None
                        self.assertEqual(
                            set(tool.output_schema.get("properties", {})),
                            OUTPUT_FIELDS,
                        )
                        self.assertEqual(
                            set(tool.output_schema.get("required", [])),
                            OUTPUT_FIELDS,
                        )
                        self.assertFalse(
                            tool.output_schema.get("additionalProperties", True)
                        )
                        output_properties = tool.output_schema["properties"]
                        self.assertEqual(
                            output_properties["status"]["enum"],
                            ["SUCCESS", "ERROR"],
                        )
                        self.assertEqual(
                            output_properties["thinking_level"]["anyOf"][0]["enum"],
                            ["low", "medium", "high"],
                        )
                        self.assertEqual(
                            output_properties["mode"]["anyOf"][0]["enum"],
                            ["plan", "accept-edits"],
                        )

                        progress_events = []

                        async def on_progress(progress, total, message):
                            progress_events.append((progress, total, message))

                        arguments = {"task": "protocol probe", "mode": "plan"}
                        valid = await session.call_tool(
                            TOOL_NAME,
                            arguments,
                            progress_callback=on_progress,
                        )
                        self.assertFalse(valid.is_error)
                        self.assertEqual(valid.structured_content["status"], "SUCCESS")
                        self.assertEqual(
                            set(valid.structured_content), OUTPUT_FIELDS
                        )
                        self.assertEqual(
                            valid.structured_content["result"],
                            "fixture-ok",
                        )
                        self.assertEqual(
                            [event[:2] for event in progress_events],
                            [(1.0, None), (2.0, None)],
                        )
                        run_id = valid.structured_content["run_id"]
                        self.assertEqual(
                            [event[2] for event in progress_events],
                            [
                                f"run_id={run_id} state=queued",
                                f"run_id={run_id} state=running",
                            ],
                        )
                        self.assertEqual(valid.structured_content["cli_version"], "1.1.22")
                        self.assertTrue(valid.structured_content["metadata_complete"])

                        default_mode = await session.call_tool(
                            TOOL_NAME, {"task": "default mode protocol probe"}
                        )
                        self.assertFalse(default_mode.is_error)
                        self.assertEqual(
                            default_mode.structured_content["result"],
                            "default-mode-plan",
                        )

                        unknown = await session.call_tool("missing", {})
                        self.assertTrue(unknown.is_error)
                        self.assertEqual(unknown.content[0].text, "Invalid tool arguments")
                        self.assertIsNone(unknown.structured_content)

                        missing = await session.call_tool(TOOL_NAME, {})
                        self.assertTrue(missing.is_error)
                        self.assertEqual(missing.content[0].text, "Invalid tool arguments")
                        self.assertIsNone(missing.structured_content)

                        missing_secret = await session.call_tool(
                            TOOL_NAME, {"context": {"secret": "PROTOCOL_SENTINEL_SECRET"}}
                        )
                        self.assertTrue(missing_secret.is_error)
                        self.assertEqual(
                            missing_secret.content[0].text, "Invalid tool arguments"
                        )
                        self.assertIsNone(missing_secret.structured_content)
                        self.assertNotIn(
                            "PROTOCOL_SENTINEL_SECRET", repr(missing_secret.content)
                        )

                        wrong_type = await session.call_tool(
                            TOOL_NAME, {"task": 123}
                        )
                        self.assertTrue(wrong_type.is_error)
                        self.assertIn("non-empty string", wrong_type.content[0].text)
                        self.assertEqual(
                            set(wrong_type.structured_content), OUTPUT_FIELDS
                        )

                        wrong_context = await session.call_tool(
                            TOOL_NAME, {"task": "x", "context": 123}
                        )
                        self.assertTrue(wrong_context.is_error)
                        self.assertIn("context", wrong_context.content[0].text)
                        self.assertEqual(
                            set(wrong_context.structured_content), OUTPUT_FIELDS
                        )

                        secret = "PROTOCOL_SENTINEL_SECRET"
                        for redacted_arguments in (
                            {"task": "x", "context": {"secret": secret}},
                            {"task": "x", "thinking_level": secret},
                            {"task": "x", "working_directory": {"secret": secret}},
                            {"task": "x", "acknowledge_review": {"secret": secret}},
                        ):
                            with self.subTest(arguments=redacted_arguments):
                                redacted = await session.call_tool(
                                    TOOL_NAME, redacted_arguments
                                )
                                self.assertTrue(redacted.is_error)
                                wire_text = repr(redacted.content) + repr(
                                    redacted.structured_content
                                )
                                self.assertNotIn(secret, wire_text)

                        invalid_enum = await session.call_tool(
                            TOOL_NAME,
                            {"task": "x", "thinking_level": "minimal"},
                        )
                        self.assertTrue(invalid_enum.is_error)
                        self.assertIn("low", invalid_enum.content[0].text)
                        self.assertIn("medium", invalid_enum.content[0].text)
                        self.assertIn("high", invalid_enum.content[0].text)
                        self.assertEqual(
                            set(invalid_enum.structured_content), OUTPUT_FIELDS
                        )

                        invalid_mode = await session.call_tool(
                            TOOL_NAME,
                            {"task": "x", "mode": "unrestricted"},
                        )
                        self.assertTrue(invalid_mode.is_error)
                        self.assertIn("plan", invalid_mode.content[0].text)
                        self.assertIn("accept-edits", invalid_mode.content[0].text)
                        self.assertEqual(
                            set(invalid_mode.structured_content), OUTPUT_FIELDS
                        )

                        repeated = await session.call_tool(TOOL_NAME, arguments)
                        self.assertFalse(repeated.is_error)
                        self.assertEqual(repeated.structured_content["result"], "fixture-ok")
                        self.assertNotEqual(
                            repeated.structured_content["run_id"],
                            valid.structured_content["run_id"],
                        )

    def test_read_timeout_does_not_corrupt_session(self):
        async def scenario():
            params = self._server_parameters(ROOT, trust_repo=True)
            with open(os.devnull, "w", encoding="utf-8") as errlog:
                async with stdio_client(params, errlog=errlog) as (read, write):
                    async with ClientSession(
                        read, write, read_timeout_seconds=5.0
                    ) as session:
                        await session.initialize()
                        with self.assertRaises(MCPError):
                            await session.call_tool(
                                TOOL_NAME,
                                {"task": "slow protocol probe", "mode": "plan"},
                                read_timeout_seconds=0.1,
                            )
                        await asyncio.sleep(0.4)
                        result = await session.call_tool(
                            TOOL_NAME, {"task": "fast protocol probe", "mode": "plan"}
                        )
                        self.assertFalse(result.is_error)
                        self.assertEqual(result.structured_content["result"], "fixture-ok")

        asyncio.run(scenario())

    def test_real_stdio_accepts_explicit_working_directory(self):
        async def scenario():
            params = self._server_parameters(ROOT.parent, trust_repo=True)
            with open(os.devnull, "w", encoding="utf-8") as errlog:
                async with stdio_client(params, errlog=errlog) as (read, write):
                    async with ClientSession(
                        read, write, read_timeout_seconds=5.0
                    ) as session:
                        await session.initialize()
                        result = await session.call_tool(
                            TOOL_NAME,
                            {
                                "task": "explicit workspace protocol probe",
                                "mode": "plan",
                                "working_directory": str(ROOT),
                            },
                        )
                        self.assertFalse(result.is_error)
                        self.assertEqual(result.structured_content["status"], "SUCCESS")
                        self.assertEqual(result.structured_content["result"], "fixture-ok")

        asyncio.run(scenario())

    def test_cancelled_call_does_not_corrupt_same_session(self):
        async def scenario():
            params = self._server_parameters(ROOT, trust_repo=True)
            started = asyncio.Event()

            async def on_progress(_progress, _total, message):
                if message.endswith("state=running"):
                    started.set()

            with open(os.devnull, "w", encoding="utf-8") as errlog:
                async with stdio_client(params, errlog=errlog) as (read, write):
                    async with ClientSession(
                        read, write, read_timeout_seconds=5.0
                    ) as session:
                        await session.initialize()
                        pending = asyncio.create_task(
                            session.call_tool(
                                TOOL_NAME,
                                {"task": "slow protocol probe", "mode": "plan"},
                                progress_callback=on_progress,
                            )
                        )
                        await asyncio.wait_for(started.wait(), timeout=2)
                        pending.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await pending
                        started_at = asyncio.get_running_loop().time()
                        result = await session.call_tool(
                            TOOL_NAME,
                            {"task": "cancellation state probe", "mode": "plan"},
                        )
                        elapsed = asyncio.get_running_loop().time() - started_at
                        self.assertFalse(result.is_error)
                        self.assertEqual(
                            result.structured_content["result"], "cancelled-seen"
                        )
                        self.assertLess(elapsed, 1.5)

        asyncio.run(scenario())

    def test_real_stdio_rejects_non_git_cwd(self):
        async def scenario():
            with tempfile.TemporaryDirectory(prefix="agy-mcp-non-git-") as directory:
                params = self._server_parameters(Path(directory), trust_repo=False)
                with open(os.devnull, "w", encoding="utf-8") as errlog:
                    async with stdio_client(params, errlog=errlog) as (read, write):
                        async with ClientSession(
                            read, write, read_timeout_seconds=5.0
                        ) as session:
                            await session.initialize()
                            result = await session.call_tool(
                                TOOL_NAME,
                                {"task": "protocol probe", "mode": "plan"},
                            )
                            self.assertTrue(result.is_error)
                            self.assertEqual(result.structured_content["status"], "ERROR")
                            self.assertEqual(
                                result.structured_content["result"],
                                "current working directory must be a Git root",
                            )

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
