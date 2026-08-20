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

    def test_dangerous_flag_is_absent_in_both_modes(self):
        _, plan_argv = self._execute(mode="plan")
        _, edit_argv = self._execute(mode="accept-edits")
        self.assertNotIn("--dangerously-skip-permissions", plan_argv)
        self.assertNotIn("--dangerously-skip-permissions", edit_argv)

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

    def test_nonfinite_usage_values_are_filtered(self):
        payload = {
            "status": "SUCCESS",
            "response": "ok",
            "usage": {
                "input_tokens": float("nan"),
                "output_tokens": float("inf"),
                "thinking_tokens": float("-inf"),
                "total_tokens": 7,
            },
        }
        result, _ = self._execute(stdout=payload)
        self.assertEqual(result["usage"], {"total_tokens": 7})

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

    def test_empty_result_from_output_overflow_is_safe_error(self):
        with patch("agy_server._git_root", return_value=Path("C:/repo")), patch(
            "agy_server._resolve_cli", return_value=Path("C:/bin/agy.exe")
        ), patch(
            "agy_server._run_cli", new=AsyncMock(return_value=(-9, "", False))
        ):
            result = asyncio.run(server.antigravity_cli_execute("x", mode="plan"))
        self.assertEqual(result, {
            "status": "ERROR",
            "result": "Antigravity CLI returned invalid JSON",
            "model": "gemini-3.7-flash-medium",
            "thinking_level": "medium",
            "mode": "plan",
            "usage": {},
            "conversation_id": None,
            "result_truncated": False,
        })

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

    def test_windows_job_factory_and_close_on_normal_completion(self):
        async def scenario():
            class FakeProcess:
                pid = 41
                returncode = 0
                stdout = asyncio.StreamReader()
                stderr = asyncio.StreamReader()

                async def wait(self):
                    return self.returncode

            process = FakeProcess()
            process.stdout.feed_data(b"ok")
            process.stdout.feed_eof()
            process.stderr.feed_eof()
            job = object()

            async def create(*args, **kwargs):
                return process

            with patch(
                "agy_server.asyncio.create_subprocess_exec", new=create
            ), patch(
                "agy_server._create_windows_job", return_value=job
            ) as create_job, patch("agy_server._close_windows_job") as close_job:
                result = await server._run_cli(
                    ["agy"], Path("C:/repo"), timeout_seconds=1
                )
            return job, create_job, close_job, result

        job, create_job, close_job, result = asyncio.run(scenario())
        create_job.assert_called_once_with(41)
        close_job.assert_called_once_with(job)
        self.assertEqual(result, (0, "ok", False))

    def test_timeout_calls_process_tree_cleanup(self):
        async def scenario():
            loop = asyncio.get_running_loop()
            released = asyncio.Event()
            events = []

            class FakeProcess:
                pid = 42
                returncode = None
                stdout = asyncio.StreamReader()
                stderr = asyncio.StreamReader()

                async def wait(self):
                    await released.wait()
                    events.append("wait_complete")
                    return self.returncode

                def finish(self):
                    self.returncode = -9
                    self.stdout.feed_eof()
                    self.stderr.feed_eof()
                    released.set()

                def kill(self):
                    self.finish()

            process = FakeProcess()
            job = object()

            def cleanup(_process):
                events.append("tree_kill")
                loop.call_soon_threadsafe(process.finish)

            def close_job(handle):
                if handle is job:
                    events.append("job_close")

            async def create(*args, **kwargs):
                return process

            with patch(
                "agy_server.asyncio.create_subprocess_exec", new=create
            ), patch(
                "agy_server._create_windows_job", return_value=job
            ) as create_job, patch(
                "agy_server._close_windows_job", side_effect=close_job
            ) as close, patch(
                "agy_server._kill_process_tree", side_effect=cleanup
            ) as kill:
                result = await server._run_cli(
                    ["agy"], Path("C:/repo"), timeout_seconds=0.01
                )
            return process, job, create_job, close, kill, events, result

        process, job, create_job, close, kill, events, (
            code, output, timed_out
        ) = asyncio.run(scenario())
        create_job.assert_called_once_with(42)
        self.assertEqual(sum(call.args == (job,) for call in close.call_args_list), 1)
        kill.assert_called_once_with(process)
        self.assertLess(events.index("job_close"), events.index("tree_kill"))
        self.assertLess(events.index("job_close"), events.index("wait_complete"))
        self.assertTrue(timed_out)
        self.assertEqual(code, -9)
        self.assertEqual(output, "")

    def test_taskkill_nonzero_falls_back_to_process_kill(self):
        class FakeProcess:
            pid = 77
            returncode = None
            killed = False

            def kill(self):
                self.killed = True

        process = FakeProcess()
        completed = type("Completed", (), {"returncode": 1})()
        with patch("agy_server.os.name", "nt"), patch(
            "agy_server.subprocess.run", return_value=completed
        ) as run:
            server._kill_process_tree(process)
        self.assertTrue(process.killed)
        self.assertEqual(
            run.call_args.args[0], ["taskkill", "/PID", "77", "/T", "/F"]
        )
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_bounded_output_excess_kills_process(self):
        async def scenario(stream_name, limit):
            loop = asyncio.get_running_loop()
            released = asyncio.Event()

            class FakeProcess:
                pid = 88
                returncode = None
                stdout = asyncio.StreamReader()
                stderr = asyncio.StreamReader()

                async def wait(self):
                    await released.wait()
                    return self.returncode

                def finish(self):
                    self.returncode = -9
                    self.stdout.feed_eof()
                    self.stderr.feed_eof()
                    released.set()

                def kill(self):
                    self.finish()

            process = FakeProcess()
            getattr(process, stream_name).feed_data(b"x" * (limit + 1))

            def cleanup(_process):
                loop.call_soon_threadsafe(process.finish)

            async def create(*args, **kwargs):
                return process

            with patch("agy_server.asyncio.create_subprocess_exec", new=create), patch(
                "agy_server._kill_process_tree", side_effect=cleanup
            ) as kill:
                result = await server._run_cli(
                    ["agy"], Path("C:/repo"), timeout_seconds=1
                )
            return process, kill, result

        for stream_name, limit in (
            ("stdout", server.MAX_STDOUT_CHARS),
            ("stderr", server.MAX_STDERR_CHARS),
        ):
            with self.subTest(stream=stream_name):
                process, kill, (code, output, timed_out) = asyncio.run(
                    scenario(stream_name, limit)
                )
                kill.assert_called_once_with(process)
                self.assertEqual(code, -9)
                self.assertEqual(output, "")
                self.assertFalse(timed_out)

    def test_mcp_schema_has_mode_and_thinking_enums(self):
        tools = asyncio.run(server.mcp.list_tools())
        schema = next(tool.input_schema for tool in tools if tool.name == "antigravity_cli_execute")
        self.assertEqual(schema["properties"]["thinking_level"]["enum"], ["low", "medium", "high"])
        self.assertEqual(schema["properties"]["thinking_level"]["default"], "medium")
        self.assertEqual(schema["properties"]["mode"]["enum"], ["plan", "accept-edits"])
        self.assertEqual(schema["properties"]["mode"]["default"], "accept-edits")


if __name__ == "__main__":
    unittest.main()
