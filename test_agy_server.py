from __future__ import annotations

import asyncio
import json
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import agy_server as server


class AgyServerTest(unittest.TestCase):
    def test_model_mapping_and_git_root(self):
        self.assertEqual([server._model_for(x) for x in ("low", "medium", "high")], [
            "gemini-3.7-flash-low",
            "gemini-3.7-flash-medium",
            "gemini-3.7-flash-high",
        ])
        with patch("agy_server.Path.cwd", return_value=Path("C:/repo")), patch(
            "agy_server.subprocess.run",
            return_value=type("Completed", (), {"stdout": "C:/repo\n"})(),
        ) as run:
            self.assertEqual(server._git_root(), Path("C:/repo"))
        self.assertEqual(run.call_args.args[0], ["git", "rev-parse", "--show-toplevel"])
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_validation_happens_before_cli_resolution(self):
        with patch("agy_server._resolve_cli") as resolve:
            empty = asyncio.run(server.antigravity_cli_execute("  "))
            bad_level = asyncio.run(server.antigravity_cli_execute("x", thinking_level="bad"))
            bad_mode = asyncio.run(server.antigravity_cli_execute("x", mode="yolo"))
        resolve.assert_not_called()
        self.assertEqual(empty["status"], "ERROR")
        self.assertEqual(bad_level["status"], "ERROR")
        self.assertEqual(bad_mode["status"], "ERROR")

    def _execute(self, *, level="low", mode="plan", stdout=None, returncode=0):
        captured = {}

        async def fake_run(argv, cwd, timeout_seconds):
            captured["argv"] = argv
            captured["cwd"] = cwd
            captured["timeout_seconds"] = timeout_seconds
            return returncode, json.dumps(stdout if stdout is not None else {
                "status": "SUCCESS", "response": "marker",
            }), False

        with patch("agy_server._git_root", return_value=Path("C:/repo")), patch(
            "agy_server._resolve_cli", return_value=Path("C:/bin/agy.exe")
        ), patch("agy_server._run_cli", new=fake_run):
            result = asyncio.run(server.antigravity_cli_execute(
                "inspect only", thinking_level=level, mode=mode
            ))
        return result, captured["argv"]

    def test_argv_has_sandbox_no_shell_and_low_medium_high(self):
        for level in ("low", "medium", "high"):
            result, argv = self._execute(level=level)
            self.assertEqual(result["status"], "SUCCESS")
            self.assertEqual(argv[argv.index("--effort") + 1], level)
            self.assertIn("--sandbox", argv)
            self.assertIn("--disable-slash-commands", argv)
            self.assertNotIn("--dangerously-skip-permissions", argv)
            self.assertNotIn("--shell", argv)

    def test_accept_edits_adds_dangerous_flag_but_plan_does_not(self):
        _, plan_argv = self._execute(mode="plan")
        _, edit_argv = self._execute(mode="accept-edits")
        self.assertNotIn("--dangerously-skip-permissions", plan_argv)
        self.assertIn("--dangerously-skip-permissions", edit_argv)

    def test_child_environment_strips_api_credentials(self):
        with patch.dict(os.environ, {
            "GEMINI_API_KEY": "secret", "GOOGLE_API_KEY": "secret2",
            "GOOGLE_APPLICATION_CREDENTIALS": "file", "ANTIGRAVITY_API_KEY": "secret3",
            "SAFE_SETTING": "yes", "MY_SERVICE_TOKEN": "secret4",
            "AUTH_HEADER": "secret5", "CI_JOB_TOKEN": "secret6",
            "DB_PASSWORD": "secret7", "ORDINARY_VALUE": "keep",
        }, clear=True):
            env = server._child_environment()
        for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "ANTIGRAVITY_API_KEY"):
            self.assertNotIn(key, env)
        self.assertEqual(env["SAFE_SETTING"], "yes")
        self.assertEqual(env["ORDINARY_VALUE"], "keep")
        self.assertNotIn("MY_SERVICE_TOKEN", env)
        self.assertNotIn("AUTH_HEADER", env)
        self.assertNotIn("CI_JOB_TOKEN", env)
        self.assertNotIn("DB_PASSWORD", env)

    def test_success_normalization_usage_id_and_truncation(self):
        payload = {
            "status": "SUCCESS", "response": "x" * (server.MAX_RESULT_CHARS + 20),
            "conversation_id": "safe-id_1", "usage": {
                "input_tokens": 2, "output_tokens": 3, "total_tokens": 5,
                "secret": "discard",
            },
        }
        result, _ = self._execute(stdout=payload)
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(len(result["result"]), server.MAX_RESULT_CHARS)
        self.assertTrue(result["result_truncated"])
        self.assertEqual(result["thinking_level"], "low")
        self.assertEqual(result["model"], "gemini-3.7-flash-low")
        self.assertEqual(result["mode"], "plan")
        self.assertEqual(result["conversation_id"], "safe-id_1")
        self.assertEqual(result["usage"], {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5})
        self.assertNotIn("secret", json.dumps(result))

    def test_malformed_and_nonzero_output_are_generic(self):
        with patch("agy_server._git_root", return_value=Path("C:/repo")), patch(
            "agy_server._resolve_cli", return_value=Path("C:/bin/agy.exe")
        ), patch("agy_server._run_cli", new=AsyncMock(return_value=(0, "TOKEN=secret", False))):
            malformed = asyncio.run(server.antigravity_cli_execute("x", mode="plan"))
        self.assertEqual(malformed["result"], "Antigravity CLI returned invalid JSON")
        with patch("agy_server._git_root", return_value=Path("C:/repo")), patch(
            "agy_server._resolve_cli", return_value=Path("C:/bin/agy.exe")
        ), patch("agy_server._run_cli", new=AsyncMock(return_value=(1, '{"status":"FAIL","response":"secret"}', False))):
            failed = asyncio.run(server.antigravity_cli_execute("x", mode="plan"))
        self.assertEqual(failed["result"], "Antigravity CLI task failed")
        self.assertNotIn("secret", json.dumps(failed))

    def test_prompt_size_validation_happens_before_git_or_cli(self):
        with patch("agy_server._git_root") as git_root, patch(
            "agy_server.execute_with_antigravity_cli"
        ) as execute:
            result = asyncio.run(server.antigravity_cli_execute(
                "x", context="c" * server.MAX_PROMPT_CHARS
            ))
        self.assertEqual(result["result"], "task context is too large")
        git_root.assert_not_called()
        execute.assert_not_called()

    def test_prompt_at_maximum_size_is_accepted(self):
        base_prompt = server._prompt("x", "", "")
        context = "c" * (server.MAX_PROMPT_CHARS - len(base_prompt))
        expected = {"status": "SUCCESS", "result": "ok"}
        with patch("agy_server._git_root", return_value=Path("C:/repo")), patch(
            "agy_server._resolve_cli", return_value=Path("C:/bin/agy.exe")
        ), patch("agy_server.execute_with_antigravity_cli", return_value=expected) as execute:
            result = asyncio.run(server.antigravity_cli_execute("x", context=context))
        self.assertEqual(result, expected)
        execute.assert_called_once()

    def test_timeout_calls_process_tree_cleanup(self):
        released = asyncio.Event()

        class FakeProcess:
            pid = 42
            returncode = None

            async def communicate(self):
                await released.wait()
                return b"", b"secret stderr"

            async def wait(self):
                self.returncode = -9

            def kill(self):
                self.returncode = -9

        process = FakeProcess()

        def cleanup(_process):
            released.set()
            process.returncode = -9

        async def create(*args, **kwargs):
            return process

        with patch("agy_server.asyncio.create_subprocess_exec", new=create), patch(
            "agy_server._timeout_seconds", return_value=1
        ), patch("agy_server._kill_process_tree", side_effect=cleanup) as kill:
            code, output, timed_out = asyncio.run(server._run_cli(["agy"], Path("C:/repo")))
        kill.assert_called_once_with(process)
        self.assertTrue(timed_out)
        self.assertEqual(output, "")

    def test_mcp_schema_has_mode_and_thinking_enums(self):
        tools = asyncio.run(server.mcp.list_tools())
        schema = next(tool.input_schema for tool in tools if tool.name == "antigravity_cli_execute")
        self.assertEqual(schema["properties"]["thinking_level"]["enum"], ["low", "medium", "high"])
        self.assertEqual(schema["properties"]["thinking_level"]["default"], "medium")
        self.assertEqual(schema["properties"]["mode"]["enum"], ["plan", "accept-edits"])
        self.assertEqual(schema["properties"]["mode"]["default"], "accept-edits")


if __name__ == "__main__":
    unittest.main()
