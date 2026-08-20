import asyncio
import importlib
import os
import sys
import types
import unittest
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch


class Data:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class ModelTarget(Data):
    def __init__(self, *, endpoint=None, name=None, types=None):
        super().__init__(endpoint=endpoint, name=name, types=types)


class GeminiAPIEndpoint(Data):
    def __init__(self, *, api_key=None, options=None, base_url=None, http_headers=None):
        super().__init__(api_key=api_key, options=options)


class GeminiModelOptions(Data):
    def __init__(self, *, thinking_level=None, service_tier=None):
        super().__init__(thinking_level=thinking_level)


class ThinkingLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class BuiltinTools(str, Enum):
    LIST_DIR = "list_directory"
    FIND_FILE = "find_file"
    SEARCH_DIR = "search_directory"
    VIEW_FILE = "view_file"
    CREATE_FILE = "create_file"
    EDIT_FILE = "edit_file"
    RUN_COMMAND = "run_command"
    FINISH = "finish"


class MCPServer:
    def __init__(self, name):
        self.name = name

    def tool(self):
        return lambda function: function

    def run(self, *, transport):
        raise AssertionError("server must not run during tests")


def install_stubs() -> None:
    google = types.ModuleType("google")
    antigravity = types.ModuleType("google.antigravity")
    hooks = types.ModuleType("google.antigravity.hooks")
    mcp = types.ModuleType("mcp")
    mcp_server = types.ModuleType("mcp.server")
    for name, value in {
        "Agent": Data,
        "BuiltinTools": BuiltinTools,
        "CapabilitiesConfig": Data,
        "GeminiAPIEndpoint": GeminiAPIEndpoint,
        "GeminiModelOptions": GeminiModelOptions,
        "LocalAgentConfig": Data,
        "ModelTarget": ModelTarget,
        "ThinkingLevel": ThinkingLevel,
    }.items():
        setattr(antigravity, name, value)
    hooks.policy = types.SimpleNamespace(
        allow=lambda tool: f"allow:{tool}", deny_all=lambda: "deny_all"
    )
    mcp_server.MCPServer = MCPServer
    sys.modules.update(
        {
            "google": google,
            "google.antigravity": antigravity,
            "google.antigravity.hooks": hooks,
            "mcp": mcp,
            "mcp.server": mcp_server,
        }
    )


install_stubs()
server = importlib.import_module("server")
smoke = importlib.import_module("smoke_antigravity")


class ServerHelpersTest(unittest.TestCase):
    def test_executor_allowlist_is_explicit_and_subagents_are_disabled(self):
        self.assertEqual(
            tuple(tool.value for tool in server.EXECUTOR_ALLOWED_TOOLS),
            (
                "list_directory",
                "find_file",
                "search_directory",
                "view_file",
                "create_file",
                "edit_file",
                "run_command",
                "finish",
            ),
        )
        smoke_tools = tuple(tool.value for tool in smoke.SMOKE_ALLOWED_TOOLS)
        self.assertEqual(
            smoke_tools,
            ("list_directory", "find_file", "search_directory", "view_file", "finish"),
        )
        self.assertNotIn("create_file", smoke_tools)
        self.assertNotIn("edit_file", smoke_tools)
        self.assertNotIn("run_command", smoke_tools)

    def test_helpers_and_thinking_mapping(self):
        for level in ("low", "medium", "high"):
            with self.subTest(level=level):
                target = server.build_model_target(level)
                self.assertEqual(target.endpoint.options.thinking_level, level)

        with self.assertRaisesRegex(ValueError, "thinking_level"):
            server.build_model_target("minimal")

        prompt = server.build_agent_prompt(" task ", " context ", " test ", Path("repo"))
        self.assertIn("TASK\n\ntask", prompt)
        self.assertIn("ADDITIONAL CONTEXT\n\ncontext", prompt)
        self.assertIn("REQUIRED VERIFICATION\n\ntest", prompt)

        with patch.dict(os.environ, {"LIMIT": "7"}):
            self.assertEqual(server.read_positive_int_env("LIMIT", 3), 7)
        with patch.dict(os.environ, {"LIMIT": "bad"}):
            self.assertEqual(server.read_positive_int_env("LIMIT", 3), 3)

        with patch.object(server, "MAX_RESULT_CHARS", 3):
            text, truncated = server.truncate_result("abcd")
            self.assertTrue(truncated)
            self.assertTrue(text.startswith("abc"))

        invalid = asyncio.run(
            server.antigravity_execute("   ", thinking_level="high")
        )
        self.assertEqual(invalid["error_type"], "invalid_request")
        self.assertEqual(invalid["thinking_level"], "high")

    def test_validation_errors_and_lock_timeout(self):
        async def checks():
            with patch.object(server, "get_workspace") as workspace, patch.object(
                server, "execute_with_antigravity", new_callable=AsyncMock
            ) as execute:
                for task, kwargs in (
                    (1, {}),
                    ("task", {"thinking_level": "minimal"}),
                    ("task", {"thinking_level": 1}),
                    ("task", {"context": 1}),
                    ("task", {"verification": 1}),
                ):
                    result = await server.antigravity_execute(task, **kwargs)
                    self.assertEqual(result["error_type"], "invalid_request")
                workspace.assert_not_called()
                execute.assert_not_awaited()

            with patch.object(
                server, "get_workspace", side_effect=RuntimeError("secret detail")
            ), self.assertLogs("antigravity-mcp", level="ERROR") as logs:
                result = await server.antigravity_execute("task")
            self.assertEqual(result["error_type"], "RuntimeError")
            self.assertEqual(result["message"], "Antigravity execution failed.")
            self.assertNotIn("secret detail", " ".join(logs.output))

            lock = asyncio.Lock()
            await lock.acquire()
            agent = AsyncMock()
            try:
                with patch.object(server, "get_workspace", return_value=Path("repo")), patch.object(
                    server, "EXECUTION_LOCK", lock
                ), patch.object(server, "TASK_TIMEOUT_SEC", 0.01), patch.object(
                    server, "Agent", agent
                ):
                    result = await server.antigravity_execute("task")
            finally:
                lock.release()
            self.assertEqual(result["error_type"], "timeout")
            agent.assert_not_called()

        asyncio.run(checks())

    def test_non_git_workspace_is_rejected(self):
        with TemporaryDirectory() as directory, patch.object(
            server.Path, "cwd", return_value=Path(directory)
        ):
            result = asyncio.run(server.antigravity_execute("task"))
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertEqual(result["message"], "Antigravity execution failed.")
        self.assertNotIn(directory, str(result))


if __name__ == "__main__":
    unittest.main()
