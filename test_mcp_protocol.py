from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parent
SERVER = ROOT / "agy_server.py"
TOOL_NAME = "antigravity_cli_execute"
INPUT_FIELDS = {"task", "context", "verification", "thinking_level", "mode"}
OUTPUT_FIELDS = {
    "status",
    "result",
    "model",
    "thinking_level",
    "mode",
    "usage",
    "conversation_id",
    "result_truncated",
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
            args=[str(SERVER)],
            cwd=str(cwd),
            env=env,
        )

    def test_real_stdio_protocol_and_session_survival(self):
        async def scenario():
            params = self._server_parameters(ROOT, trust_repo=True)
            with open(os.devnull, "w", encoding="utf-8") as errlog:
                async with stdio_client(params, errlog=errlog) as (read, write):
                    async with ClientSession(
                        read, write, read_timeout_seconds=5
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
                            "accept-edits",
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

                        arguments = {"task": "protocol probe", "mode": "plan"}
                        valid = await session.call_tool(TOOL_NAME, arguments)
                        self.assertFalse(valid.is_error)
                        self.assertEqual(valid.structured_content["status"], "ERROR")
                        self.assertEqual(
                            set(valid.structured_content), OUTPUT_FIELDS
                        )
                        self.assertEqual(
                            valid.structured_content["result"],
                            "Antigravity CLI returned invalid JSON",
                        )

                        unknown = await session.call_tool("missing", {})
                        self.assertTrue(unknown.is_error)
                        self.assertIn("Unknown tool", unknown.content[0].text)

                        missing = await session.call_tool(TOOL_NAME, {})
                        self.assertTrue(missing.is_error)
                        self.assertIn("Field required", missing.content[0].text)

                        wrong_type = await session.call_tool(
                            TOOL_NAME, {"task": 123}
                        )
                        self.assertTrue(wrong_type.is_error)
                        self.assertIn("valid string", wrong_type.content[0].text)

                        invalid_enum = await session.call_tool(
                            TOOL_NAME,
                            {"task": "x", "thinking_level": "minimal"},
                        )
                        self.assertTrue(invalid_enum.is_error)
                        self.assertIn("low", invalid_enum.content[0].text)
                        self.assertIn("medium", invalid_enum.content[0].text)
                        self.assertIn("high", invalid_enum.content[0].text)

                        repeated = await session.call_tool(TOOL_NAME, arguments)
                        self.assertFalse(repeated.is_error)
                        self.assertEqual(
                            repeated.structured_content,
                            valid.structured_content,
                        )

        asyncio.run(scenario())

    def test_real_stdio_rejects_non_git_cwd(self):
        async def scenario():
            with tempfile.TemporaryDirectory(prefix="agy-mcp-non-git-") as directory:
                params = self._server_parameters(Path(directory), trust_repo=False)
                with open(os.devnull, "w", encoding="utf-8") as errlog:
                    async with stdio_client(params, errlog=errlog) as (read, write):
                        async with ClientSession(
                            read, write, read_timeout_seconds=5
                        ) as session:
                            await session.initialize()
                            result = await session.call_tool(
                                TOOL_NAME,
                                {"task": "protocol probe", "mode": "plan"},
                            )
                            self.assertFalse(result.is_error)
                            self.assertEqual(result.structured_content["status"], "ERROR")
                            self.assertEqual(
                                result.structured_content["result"],
                                "current working directory must be a Git root",
                            )

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
