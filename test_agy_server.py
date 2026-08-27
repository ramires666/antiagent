from __future__ import annotations

import asyncio
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from ctypes import wintypes
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
            "agy_server._resolve_executable", return_value=Path("C:/Git/cmd/git.exe")
        ), patch(
            "agy_server.subprocess.run",
            return_value=type("Completed", (), {"stdout": "C:/repo\n"})(),
        ) as run:
            self.assertEqual(server._git_root(), Path("C:/repo"))
        self.assertEqual(
            run.call_args.args[0],
            [str(Path("C:/Git/cmd/git.exe")), "rev-parse", "--show-toplevel"],
        )
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

    def _execute(
        self, *, level="low", mode="plan", stdout=None, returncode=0,
        timed_out=False,
    ):
        captured = {}

        async def fake_run(argv, cwd, timeout_seconds):
            captured["argv"] = argv
            captured["cwd"] = cwd
            captured["timeout_seconds"] = timeout_seconds
            return returncode, json.dumps(stdout if stdout is not None else {
                "status": "SUCCESS", "response": "marker",
            }), timed_out

        with patch("agy_server._git_root", return_value=Path("C:/repo")), patch(
            "agy_server._resolve_cli", return_value=Path("C:/bin/agy.exe")
        ), patch("agy_server._run_cli", new=fake_run):
            result = asyncio.run(server.antigravity_cli_execute(
                "inspect only", thinking_level=level, mode=mode
            ))
        return result, captured["argv"]

    def test_timeout_configuration(self):
        cases = (
            (None, server.DEFAULT_TIMEOUT_SECONDS),
            ("", server.DEFAULT_TIMEOUT_SECONDS),
            ("invalid", server.DEFAULT_TIMEOUT_SECONDS),
            ("0", server.DEFAULT_TIMEOUT_SECONDS),
            ("-1", server.DEFAULT_TIMEOUT_SECONDS),
            ("17", 17),
            (str(server.MAX_TIMEOUT_SECONDS + 1), server.MAX_TIMEOUT_SECONDS),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw), patch.dict(os.environ, {}, clear=False):
                if raw is None:
                    os.environ.pop("ANTIGRAVITY_CLI_TIMEOUT_SECONDS", None)
                else:
                    os.environ["ANTIGRAVITY_CLI_TIMEOUT_SECONDS"] = raw
                self.assertEqual(server._timeout_seconds(), expected)

    def test_cli_resolution_priority_and_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            configured = base / "configured.exe"
            discovered = base / "discovered.exe"
            fallback = base / "agy" / "bin" / "agy.exe"
            configured.touch()
            discovered.touch()
            fallback.parent.mkdir(parents=True)
            fallback.touch()

            with patch.dict(os.environ, {
                "ANTIGRAVITY_CLI_PATH": str(configured),
                "LOCALAPPDATA": str(base),
            }, clear=True), patch(
                "agy_server._resolve_executable", return_value=discovered
            ) as resolve:
                self.assertEqual(server._resolve_cli(), configured.resolve())
            resolve.assert_not_called()

            with patch.dict(os.environ, {
                "ANTIGRAVITY_CLI_PATH": str(base / "missing.exe"),
                "LOCALAPPDATA": str(base / "missing-appdata"),
            }, clear=True), patch(
                "agy_server._resolve_executable", return_value=discovered
            ) as resolve:
                self.assertEqual(server._resolve_cli(), discovered.resolve())
            resolve.assert_called_once_with("agy")

            with patch.dict(os.environ, {"LOCALAPPDATA": str(base)}, clear=True), patch(
                "agy_server._resolve_executable", return_value=discovered
            ) as resolve:
                self.assertEqual(server._resolve_cli(), fallback.resolve())
            resolve.assert_not_called()

            with patch.dict(os.environ, {}, clear=True), patch(
                "agy_server._resolve_executable", return_value=None
            ):
                self.assertIsNone(server._resolve_cli())

    def test_relative_configured_cli_path_is_not_used(self):
        with patch.dict(
            os.environ,
            {"ANTIGRAVITY_CLI_PATH": "agy.exe", "LOCALAPPDATA": ""},
            clear=True,
        ), patch("agy_server._resolve_executable", return_value=None) as resolve:
            self.assertIsNone(server._resolve_cli())
        resolve.assert_called_once_with("agy")

    def test_windows_executable_resolution_skips_current_directory_and_relative_path(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            safe = base / "safe"
            safe.mkdir()
            (base / "agy.exe").touch()
            expected = safe / "agy.exe"
            expected.touch()
            with patch("agy_server.os.name", "nt"), patch.dict(
                os.environ,
                {"PATH": f"{os.pathsep}{safe};relative", "PATHEXT": ".EXE"},
                clear=False,
            ), patch("agy_server.Path.cwd", return_value=base):
                resolved = server._resolve_executable("agy")
            self.assertEqual(resolved, expected.resolve())
            self.assertTrue(resolved.is_absolute())

    def test_git_failures_and_non_root_are_rejected(self):
        for error in (OSError("missing"), subprocess.TimeoutExpired("git", 5)):
            with self.subTest(error=type(error).__name__), patch(
                "agy_server.Path.cwd", return_value=Path("C:/repo")
            ), patch("agy_server.subprocess.run", side_effect=error):
                self.assertIsNone(server._git_root())

        completed = type("Completed", (), {"stdout": "C:/other\n"})()
        with patch("agy_server.Path.cwd", return_value=Path("C:/repo")), patch(
            "agy_server.subprocess.run", return_value=completed
        ):
            self.assertIsNone(server._git_root())

        with patch("agy_server._git_root", return_value=None), patch(
            "agy_server.execute_with_antigravity_cli", new=AsyncMock()
        ) as execute:
            result = asyncio.run(server.antigravity_cli_execute("x"))
        self.assertEqual(result["result"], "current working directory must be a Git root")
        execute.assert_not_awaited()

    def test_explicit_git_root_absolute_relative_and_rejections(self):
        process_cwd = Path.cwd().resolve()
        with tempfile.TemporaryDirectory(
            prefix="agy-workspace-", dir=process_cwd
        ) as directory:
            base = Path(directory).resolve()
            repo = base / "repo"
            nested = repo / "nested"
            non_git = base / "non-git"
            file_path = base / "file.txt"
            nested.mkdir(parents=True)
            non_git.mkdir()
            file_path.write_text("not a directory", encoding="utf-8")

            def git_root(*_args, cwd, **_kwargs):
                candidate = Path(cwd).resolve()
                self.assertTrue(candidate.is_dir())
                if candidate == non_git:
                    raise subprocess.CalledProcessError(128, "git")
                root = repo if candidate == nested else candidate
                return type("Completed", (), {"stdout": f"{root}\n"})()

            with patch("agy_server.subprocess.run", side_effect=git_root):
                self.assertEqual(server._git_root(str(repo)), repo)
                self.assertEqual(
                    server._git_root(str(repo.relative_to(process_cwd))), repo
                )
                for value in (
                    str(base / "missing"), str(file_path), str(nested), str(non_git)
                ):
                    with self.subTest(working_directory=value):
                        self.assertIsNone(server._git_root(value))

    def test_explicit_canonical_path_outside_process_cwd_is_rejected_before_git(self):
        process_cwd = Path.cwd().resolve()
        with tempfile.TemporaryDirectory(prefix="agy-outside-") as directory:
            outside = Path(directory).resolve()
            self.assertFalse(outside.is_relative_to(process_cwd))
            with patch("agy_server.subprocess.run") as git:
                self.assertIsNone(server._git_root(str(outside)))
            git.assert_not_called()

    def test_handler_rejects_explicit_invalid_working_directory_before_cli(self):
        working_directory = str(Path.cwd() / "missing-workspace")
        with patch("agy_server._git_root", return_value=None) as git_root, patch(
            "agy_server.execute_with_antigravity_cli", new=AsyncMock()
        ) as execute, patch("agy_server._resolve_cli") as resolve_cli:
            result = asyncio.run(server.antigravity_cli_execute(
                "x", working_directory=working_directory
            ))
        git_root.assert_called_once_with(working_directory)
        execute.assert_not_awaited()
        resolve_cli.assert_not_called()
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(
            result["result"], "working_directory must be an existing Git root"
        )

    def test_empty_git_stdout_is_rejected(self):
        for stdout in ("", " \t\r\n"):
            completed = type("Completed", (), {"stdout": stdout})()
            with self.subTest(stdout=repr(stdout)), patch(
                "agy_server.Path.cwd", return_value=Path("C:/repo")
            ), patch("agy_server.subprocess.run", return_value=completed):
                self.assertIsNone(server._git_root())

    def test_non_string_inputs_and_prompt_formatting(self):
        cases = (
            ({"task": 1}, "task must be a non-empty string"),
            ({"task": "x", "context": 1}, "context and verification must be strings"),
            ({"task": "x", "verification": []}, "context and verification must be strings"),
        )
        for arguments, expected in cases:
            with self.subTest(arguments=arguments), patch("agy_server._git_root") as root:
                result = asyncio.run(server.antigravity_cli_execute(**arguments))
            self.assertEqual(result["result"], expected)
            root.assert_not_called()

        with patch("agy_server._git_root") as root, patch(
            "agy_server.execute_with_antigravity_cli", new=AsyncMock()
        ) as execute:
            result = asyncio.run(server.antigravity_cli_execute(
                "x", working_directory=123
            ))
        self.assertEqual(result["status"], "ERROR")
        root.assert_not_called()
        execute.assert_not_awaited()

        expected_prompt = (
            "You are a coding subagent operating in the current Git repository.\n"
            "Complete the requested task, make the smallest safe changes, and run "
            "the requested verification. Do not disclose credentials or secrets. "
            "Do not use MCP, plugins, subagents, network access, destructive commands, "
            "git commit, or git push.\n\n"
            "TASK:\ntask\n\nCONTEXT:\ncontext\n\nVERIFICATION:\nverify"
        )
        self.assertEqual(server._prompt("task", "context", "verify"), expected_prompt)
        with patch("agy_server._git_root", return_value=Path("C:/repo")), patch(
            "agy_server.execute_with_antigravity_cli",
            new=AsyncMock(return_value={"status": "SUCCESS"}),
        ) as execute:
            asyncio.run(server.antigravity_cli_execute(
                " task ", context=" context ", verification=" verify ", mode="plan"
            ))
        execute.assert_awaited_once()
        self.assertEqual({
            key: execute.await_args.kwargs[key]
            for key in ("workspace", "prompt", "thinking_level", "mode")
        }, {
            "workspace": Path("C:/repo"), "prompt": expected_prompt,
            "thinking_level": "medium", "mode": "plan",
        })

    def test_exact_argv_and_subprocess_contract(self):
        cli = Path("C:/bin/agy.exe")
        workspace = Path("C:/repo")
        expected_argv = [
            str(cli), "-p", "PROMPT", "--mode", "plan", "--model",
            "gemini-3.7-flash-high", "--effort", "high", "--output-format",
            "json", "--print-timeout", "5s", "--sandbox",
            "--disable-slash-commands", "--add-dir", str(workspace),
        ]
        self.assertEqual(
            server._build_argv(cli, workspace, "PROMPT", "high", "plan", 10),
            expected_argv,
        )
        short_argv = server._build_argv(cli, workspace, "PROMPT", "high", "plan", 3)
        self.assertEqual(short_argv[short_argv.index("--print-timeout") + 1], "1s")

        async def scenario():
            captured = {}

            class FakeProcess:
                pid = 91
                returncode = 0
                stdout = asyncio.StreamReader()
                stderr = asyncio.StreamReader()

                async def wait(self):
                    return self.returncode

            process = FakeProcess()
            process.stdout.feed_data(b"stdout")
            process.stdout.feed_eof()
            process.stderr.feed_eof()

            async def create(*args, **kwargs):
                captured["args"] = args
                captured["kwargs"] = kwargs
                return process

            with patch("agy_server._child_environment", return_value={"SAFE": "yes"}), patch(
                "agy_server.asyncio.create_subprocess_exec", new=create
            ), patch("agy_server._create_windows_job", return_value=None) as create_job:
                result = await server._run_cli(expected_argv, workspace, 2)
            return captured, create_job, result

        captured, create_job, result = asyncio.run(scenario())
        expected_kwargs = {
            "cwd": str(workspace), "env": {"SAFE": "yes"},
            "stdin": asyncio.subprocess.DEVNULL,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if os.name == "nt":
            expected_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            expected_kwargs["start_new_session"] = True
        self.assertEqual(captured, {"args": tuple(expected_argv), "kwargs": expected_kwargs})
        create_job.assert_called_once_with(91)
        self.assertEqual(result, (0, "stdout", False))

    def test_subprocess_spawn_failures_are_safe(self):
        for error in (OSError("no executable"), ValueError("bad argv")):
            with self.subTest(error=type(error).__name__), patch(
                "agy_server.asyncio.create_subprocess_exec", new=AsyncMock(side_effect=error)
            ), patch("agy_server._create_windows_job") as create_job:
                result = asyncio.run(server._run_cli(["agy"], Path("C:/repo"), 1))
            self.assertEqual(result, (None, "", False))
            create_job.assert_not_called()

    def test_windows_command_line_budget_is_checked_before_spawn(self):
        argv = ["C:/bin/agy.exe", "-p", chr(0x1F600) * 20_000]
        with patch("agy_server.os.name", "nt"), patch(
            "agy_server.asyncio.create_subprocess_exec", new=AsyncMock()
        ) as create, patch("agy_server._create_windows_job") as create_job:
            result = asyncio.run(server._run_cli(argv, Path("C:/repo"), 1))
        self.assertEqual(result, (None, "", False))
        create.assert_not_awaited()
        create_job.assert_not_called()

        argv = ["C:/bin/agy.exe", "x" * server.MAX_WINDOWS_COMMAND_LINE_UNITS]
        with patch("agy_server.os.name", "nt"), patch(
            "agy_server.asyncio.create_subprocess_exec", new=AsyncMock()
        ) as create:
            result = asyncio.run(server._run_cli(argv, Path("C:/repo"), 1))
        self.assertEqual(result, (None, "", False))
        create.assert_not_awaited()

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

    def test_invalid_conversation_ids_and_usage_are_discarded(self):
        for value in (None, 123, "", "has space", "x" * 129, "bad/segment"):
            with self.subTest(conversation_id=value):
                self.assertIsNone(server._conversation_id({"conversation_id": value}))
        for value in (None, [], "tokens", 7):
            with self.subTest(usage=value):
                self.assertEqual(server._usage({"usage": value}), {})
        self.assertEqual(server._usage({"usage": {
            "input_tokens": True,
            "output_tokens": "2",
            "thinking_tokens": None,
            "cache_read_tokens": 3,
            "unknown": 99,
        }}), {"cache_read_tokens": 3})
        self.assertEqual(server._usage({"usage": {
            "input_tokens": -1,
            "output_tokens": -0.5,
            "total_tokens": 0,
        }}), {"total_tokens": 0})

    def test_timeout_and_start_failure_results(self):
        cases = (
            ({"returncode": 0, "timed_out": True}, "Antigravity CLI timed out"),
            ({"returncode": None, "timed_out": False}, "Antigravity CLI could not be started"),
        )
        for options, expected in cases:
            with self.subTest(options=options):
                result, _ = self._execute(**options)
                self.assertEqual(result["status"], "ERROR")
                self.assertEqual(result["result"], expected)

    def test_cli_unavailable_and_defensive_stdout_limit(self):
        with patch("agy_server._resolve_cli", return_value=None), patch(
            "agy_server._run_cli", new=AsyncMock()
        ) as run:
            unavailable = asyncio.run(server.execute_with_antigravity_cli(
                workspace=Path("C:/repo"), prompt="x",
                thinking_level="medium", mode="plan",
            ))
        self.assertEqual(
            unavailable["result"], "Antigravity CLI is not installed or unavailable"
        )
        run.assert_not_awaited()

        with patch("agy_server._resolve_cli", return_value=Path("C:/bin/agy.exe")), patch(
            "agy_server._run_cli",
            new=AsyncMock(return_value=(0, "x" * (server.MAX_STDOUT_CHARS + 1), False)),
        ):
            oversized = asyncio.run(server.execute_with_antigravity_cli(
                workspace=Path("C:/repo"), prompt="x",
                thinking_level="medium", mode="plan",
            ))
        self.assertEqual(oversized["result"], "Antigravity CLI response was too large")

    def test_outer_timeout_releases_execution_lock(self):
        async def scenario():
            calls = 0

            async def run(*_args):
                nonlocal calls
                calls += 1
                if calls == 1:
                    await asyncio.Event().wait()
                return 0, '{"status":"SUCCESS","response":"after timeout"}', False

            with patch("agy_server._resolve_cli", return_value=Path("C:/bin/agy.exe")), patch(
                "agy_server._timeout_seconds", return_value=0.01
            ), patch("agy_server._run_cli", new=run), patch(
                "agy_server.EXECUTION_LOCK", asyncio.Lock()
            ):
                first = await server.execute_with_antigravity_cli(
                    workspace=Path("C:/repo"), prompt="x",
                    thinking_level="low", mode="plan",
                )
                second = await server.execute_with_antigravity_cli(
                    workspace=Path("C:/repo"), prompt="x",
                    thinking_level="low", mode="plan",
                )
            return calls, first, second

        calls, first, second = asyncio.run(scenario())
        self.assertEqual(calls, 2)
        self.assertEqual(first["result"], "Antigravity CLI timed out")
        self.assertEqual(second["result"], "after timeout")

    def test_timeout_while_waiting_for_lock_then_next_call_succeeds(self):
        async def scenario():
            lock = asyncio.Lock()
            await lock.acquire()
            run = AsyncMock(return_value=(
                0, '{"status":"SUCCESS","response":"after release"}', False
            ))
            with patch("agy_server._resolve_cli", return_value=Path("C:/bin/agy.exe")), patch(
                "agy_server._timeout_seconds", return_value=0.01
            ), patch("agy_server._run_cli", new=run), patch(
                "agy_server.EXECUTION_LOCK", lock
            ):
                try:
                    blocked = await server.execute_with_antigravity_cli(
                        workspace=Path("C:/repo"), prompt="x",
                        thinking_level="low", mode="plan",
                    )
                    calls_while_locked = run.await_count
                finally:
                    lock.release()
                recovered = await server.execute_with_antigravity_cli(
                    workspace=Path("C:/repo"), prompt="x",
                    thinking_level="low", mode="plan",
                )
            return blocked, calls_while_locked, recovered, run.await_count

        blocked, calls_while_locked, recovered, total_calls = asyncio.run(scenario())
        self.assertEqual(blocked["result"], "Antigravity CLI timed out")
        self.assertEqual(calls_while_locked, 0)
        self.assertEqual(recovered["result"], "after release")
        self.assertEqual(total_calls, 1)

    def test_heartbeat_emits_and_is_reaped_on_completion_and_timeout(self):
        async def heartbeat_emits():
            real_sleep = asyncio.sleep
            messages = []
            emitted = asyncio.Event()
            sleeps = 0

            async def sleep(_seconds):
                nonlocal sleeps
                sleeps += 1
                if sleeps == 1:
                    await real_sleep(0)
                    return
                await asyncio.Event().wait()

            async def progress(message):
                messages.append(message)
                emitted.set()

            with patch("agy_server.asyncio.sleep", new=sleep):
                task = asyncio.create_task(server._progress_heartbeat(progress))
                await asyncio.wait_for(emitted.wait(), 1)
                task.cancel()
                await task
            return messages, task.done()

        messages, done = asyncio.run(heartbeat_emits())
        self.assertEqual(messages, ["Antigravity CLI request is still in progress"])
        self.assertTrue(done)

        async def lifecycle(timeout):
            started = asyncio.Event()
            reaped = asyncio.Event()

            async def heartbeat(_progress):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    reaped.set()

            async def run(*_args):
                if timeout:
                    await asyncio.Event().wait()
                await started.wait()
                return 0, '{"status":"SUCCESS","response":"ok"}', False

            async def progress(_message):
                pass

            with patch("agy_server._resolve_cli", return_value=Path("C:/bin/agy.exe")), patch(
                "agy_server._timeout_seconds", return_value=0.01 if timeout else 1
            ), patch("agy_server._progress_heartbeat", new=heartbeat), patch(
                "agy_server._run_cli", new=run
            ), patch("agy_server.EXECUTION_LOCK", asyncio.Lock()):
                result = await server.execute_with_antigravity_cli(
                    workspace=Path("C:/repo"), prompt="x", thinking_level="low",
                    mode="plan", progress=progress,
                )
            return result, started.is_set(), reaped.is_set()

        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                result, started, reaped = asyncio.run(lifecycle(timeout))
                self.assertTrue(started)
                self.assertTrue(reaped)
                self.assertEqual(
                    result["result"], "Antigravity CLI timed out" if timeout else "ok"
                )

    def test_json_shape_status_and_response_validation(self):
        cases = (
            ([], "Antigravity CLI returned an invalid response"),
            ("scalar", "Antigravity CLI returned an invalid response"),
            ({"status": "FAIL", "response": "private"}, "Antigravity CLI task failed"),
            ({"response": "missing status"}, "Antigravity CLI task failed"),
            ({"status": "SUCCESS", "response": 7}, "Antigravity CLI returned an invalid response"),
        )
        for payload, expected in cases:
            with self.subTest(payload=payload):
                result, _ = self._execute(stdout=payload)
                self.assertEqual(result["status"], "ERROR")
                self.assertEqual(result["result"], expected)
                self.assertNotIn("private", json.dumps(result))
        missing_response, _ = self._execute(stdout={"status": "SUCCESS"})
        self.assertEqual(missing_response["status"], "SUCCESS")
        self.assertEqual(missing_response["result"], "")

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

    def test_stderr_and_logs_do_not_disclose_output(self):
        async def scenario():
            class FakeProcess:
                pid = 92
                returncode = 0
                stdout = asyncio.StreamReader()
                stderr = asyncio.StreamReader()

                async def wait(self):
                    return self.returncode

            process = FakeProcess()
            process.stdout.feed_data(b"public")
            process.stdout.feed_eof()
            process.stderr.feed_data(b"STDERR_SECRET")
            process.stderr.feed_eof()

            async def create(*_args, **_kwargs):
                return process

            with patch("agy_server.asyncio.create_subprocess_exec", new=create), patch(
                "agy_server._create_windows_job", return_value=None
            ):
                return await server._run_cli(["agy"], Path("C:/repo"), 1)

        self.assertEqual(asyncio.run(scenario()), (0, "public", False))
        with self.assertLogs(server.logger, level="WARNING") as logs:
            result, _ = self._execute(stdout={
                "status": "FAIL", "response": "PAYLOAD_SECRET"
            })
        self.assertEqual(result["result"], "Antigravity CLI task failed")
        self.assertNotIn("PAYLOAD_SECRET", "\n".join(logs.output))

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

    @unittest.skipUnless(os.name == "nt", "Windows process-tree regression")
    def test_actual_windows_descendant_is_gone_after_timeout(self):
        parent_pid = None
        child_pid = None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        def pid_exists(pid):
            ctypes.set_last_error(0)
            handle = open_process(0x1000, False, pid)
            if not handle:
                error = ctypes.get_last_error()
                if error == 87:
                    return False
                self.fail(f"OpenProcess failed for exact PID {pid}: Windows error {error}")
            try:
                exit_code = wintypes.DWORD()
                if not get_exit_code(handle, ctypes.byref(exit_code)):
                    self.fail(
                        f"GetExitCodeProcess failed for exact PID {pid}: "
                        f"Windows error {ctypes.get_last_error()}"
                    )
                return exit_code.value == 259
            finally:
                close_handle(handle)

        try:
            with tempfile.TemporaryDirectory(prefix="agy-tree-test-") as directory:
                pid_file = Path(directory) / "pids.txt"
                child_code = "import time; time.sleep(60)"
                parent_code = (
                    "import os,pathlib,subprocess,sys,time;"
                    f"pid_file=pathlib.Path({str(pid_file)!r});"
                    "pid_file.write_text(str(os.getpid()),encoding='ascii');"
                    f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
                    "handle=pid_file.open('a',encoding='ascii');"
                    "handle.write(' '+str(child.pid));handle.close();"
                    "time.sleep(60)"
                )
                code, output, timed_out = asyncio.run(server._run_cli(
                    [sys.executable, "-c", parent_code],
                    Path(directory),
                    timeout_seconds=1,
                ))
                if pid_file.is_file():
                    pids = pid_file.read_text(encoding="ascii").split()
                    if pids and pids[0].isdecimal():
                        parent_pid = int(pids[0])
                    if len(pids) >= 2 and pids[1].isdecimal():
                        child_pid = int(pids[1])
                self.assertTrue(timed_out)
                self.assertEqual(output, "")
                self.assertIsInstance(parent_pid, int)
                self.assertIsInstance(child_pid, int)
                self.assertNotEqual(parent_pid, child_pid)
                deadline = time.monotonic() + 5
                remaining = {parent_pid, child_pid}
                while remaining and time.monotonic() < deadline:
                    remaining = {pid for pid in remaining if pid_exists(pid)}
                    if not remaining:
                        break
                    time.sleep(0.05)
                remaining = {pid for pid in remaining if pid_exists(pid)}
                self.assertEqual(
                    remaining,
                    set(),
                    f"test-created PIDs survived wrapper timeout: {sorted(remaining)}",
                )
        finally:
            for pid in (parent_pid, child_pid):
                if isinstance(pid, int) and pid > 0:
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True,
                        timeout=10,
                        check=False,
                        shell=False,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )

    def test_windows_job_setup_failures_close_owned_handles(self):
        class ApiFunction:
            def __init__(self, result=None, error=False):
                self.result = result
                self.error = error
                self.calls = []

            def __call__(self, *args):
                self.calls.append(args)
                if self.error:
                    raise OSError("api failure")
                return self.result

        def make_api(stage):
            job = object()
            process = object()
            api = type("FakeKernel32", (), {})()
            api.CreateJobObjectW = ApiFunction(None if stage == "create" else job)
            api.SetInformationJobObject = ApiFunction(
                True, error=stage == "set_exception"
            )
            if stage == "set":
                api.SetInformationJobObject.result = False
            api.OpenProcess = ApiFunction(None if stage == "open" else process)
            api.AssignProcessToJobObject = ApiFunction(stage != "assign")
            api.CloseHandle = ApiFunction()
            return api, job, process

        for stage, expected_closed in (
            ("create", 0),
            ("set", 1),
            ("set_exception", 1),
            ("open", 1),
            ("assign", 2),
        ):
            with self.subTest(stage=stage):
                api, job, process = make_api(stage)
                with patch("agy_server.os.name", "nt"), patch(
                    "agy_server.ctypes.WinDLL", return_value=api, create=True
                ):
                    result = server._create_windows_job(123)
                self.assertIsNone(result)
                self.assertEqual(len(api.CloseHandle.calls), expected_closed)
                if stage == "assign":
                    self.assertEqual(
                        [call[0] for call in api.CloseHandle.calls], [job, process]
                    )

        api, job, process = make_api("success")
        with patch("agy_server.os.name", "nt"), patch(
            "agy_server.ctypes.WinDLL", return_value=api, create=True
        ):
            self.assertIs(server._create_windows_job(123), job)
        self.assertEqual([call[0] for call in api.CloseHandle.calls], [process])

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

    def test_run_cli_caller_cancellation_reaps_and_reraises(self):
        async def scenario():
            loop = asyncio.get_running_loop()
            started = asyncio.Event()
            released = asyncio.Event()
            collected = asyncio.Event()
            waited = asyncio.Event()

            class FakeProcess:
                pid = 43
                returncode = None

                async def wait(self):
                    await released.wait()
                    waited.set()
                    return self.returncode

            process = FakeProcess()
            job = object()

            async def collect(_process):
                started.set()
                try:
                    await released.wait()
                    return b"", False, b""
                finally:
                    collected.set()

            async def create(*_args, **_kwargs):
                return process

            def kill(_process):
                process.returncode = -9
                loop.call_soon_threadsafe(released.set)

            with patch("agy_server.asyncio.create_subprocess_exec", new=create), patch(
                "agy_server._create_windows_job", return_value=job
            ), patch("agy_server._collect_output", new=collect), patch(
                "agy_server._kill_process_tree", side_effect=kill
            ) as kill_tree, patch("agy_server._close_windows_job") as close_job:
                task = asyncio.create_task(server._run_cli(
                    ["agy"], Path("C:/repo"), timeout_seconds=10
                ))
                await started.wait()
                task.cancel()
                cancelled = False
                try:
                    await task
                except asyncio.CancelledError:
                    cancelled = True
            return cancelled, collected.is_set(), waited.is_set(), job, kill_tree, close_job

        cancelled, collected, waited, job, kill_tree, close_job = asyncio.run(scenario())
        self.assertTrue(cancelled)
        self.assertTrue(collected)
        self.assertTrue(waited)
        kill_tree.assert_called_once()
        self.assertEqual(sum(call.args == (job,) for call in close_job.call_args_list), 1)

    def test_unexpected_read_error_cleans_up_and_returns_safely(self):
        async def scenario():
            loop = asyncio.get_running_loop()
            released = asyncio.Event()

            class BrokenStream:
                async def read(self, _size):
                    raise RuntimeError("READ_SECRET")

            class FakeProcess:
                pid = 44
                returncode = None
                stdout = BrokenStream()
                stderr = asyncio.StreamReader()

                async def wait(self):
                    await released.wait()
                    return self.returncode

            process = FakeProcess()
            process.stderr.feed_eof()
            job = object()

            async def create(*_args, **_kwargs):
                return process

            def kill(_process):
                process.returncode = -9
                loop.call_soon_threadsafe(released.set)

            with patch("agy_server.asyncio.create_subprocess_exec", new=create), patch(
                "agy_server._create_windows_job", return_value=job
            ), patch("agy_server._kill_process_tree", side_effect=kill) as kill_tree, patch(
                "agy_server._close_windows_job"
            ) as close_job:
                result = await server._run_cli(["agy"], Path("C:/repo"), 1)
            return result, job, kill_tree, close_job

        result, job, kill_tree, close_job = asyncio.run(scenario())
        self.assertEqual(result, (-9, "", False))
        kill_tree.assert_called_once()
        self.assertEqual(sum(call.args == (job,) for call in close_job.call_args_list), 1)

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
            "agy_server._resolve_executable", return_value=Path("C:/Windows/System32/taskkill.exe")
        ), patch(
            "agy_server.subprocess.run", return_value=completed
        ) as run:
            server._kill_process_tree(process)
        self.assertTrue(process.killed)
        taskkill = Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"
        self.assertEqual(
            run.call_args.args[0],
            [str(taskkill), "/PID", "77", "/T", "/F"],
        )
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_taskkill_success_and_oserror_cleanup(self):
        class FakeProcess:
            pid = 78
            returncode = None
            killed = False

            def kill(self):
                self.killed = True

        successful = FakeProcess()
        completed = type("Completed", (), {"returncode": 0})()
        with patch("agy_server.os.name", "nt"), patch(
            "agy_server._resolve_executable", return_value=Path("C:/Windows/System32/taskkill.exe")
        ), patch(
            "agy_server.subprocess.run", return_value=completed
        ):
            server._kill_process_tree(successful)
        self.assertFalse(successful.killed)

        fallback = FakeProcess()
        with patch("agy_server.os.name", "nt"), patch(
            "agy_server._resolve_executable", return_value=Path("C:/Windows/System32/taskkill.exe")
        ), patch(
            "agy_server.subprocess.run", side_effect=OSError("taskkill unavailable")
        ):
            server._kill_process_tree(fallback)
        self.assertTrue(fallback.killed)

        class GoneProcess(FakeProcess):
            def kill(self):
                raise ProcessLookupError

        with patch("agy_server.os.name", "nt"), patch(
            "agy_server._resolve_executable", return_value=Path("C:/Windows/System32/taskkill.exe")
        ), patch(
            "agy_server.subprocess.run", side_effect=OSError
        ):
            server._kill_process_tree(GoneProcess())
        with patch("agy_server.os.name", "posix"), patch("agy_server.ctypes.WinDLL") as windll:
            server._close_windows_job(object())
        windll.assert_not_called()

    def test_taskkill_uses_absolute_systemroot_path(self):
        with tempfile.TemporaryDirectory() as directory:
            system_root = Path(directory)
            taskkill = system_root / "System32" / "taskkill.exe"
            taskkill.parent.mkdir()
            taskkill.touch()
            process = type("Process", (), {"pid": 81, "returncode": None})()
            completed = type("Completed", (), {"returncode": 0})()
            with patch("agy_server.os.name", "nt"), patch.dict(
                os.environ, {"SystemRoot": str(system_root)}, clear=False
            ), patch("agy_server.subprocess.run", return_value=completed) as run:
                server._kill_process_tree(process)
            self.assertEqual(
                run.call_args.args[0],
                [str(taskkill.resolve()), "/PID", "81", "/T", "/F"],
            )

    def test_taskkill_timeout_falls_back_to_process_kill(self):
        class FakeProcess:
            pid = 79
            returncode = None
            killed = False

            def kill(self):
                self.killed = True

        process = FakeProcess()
        with patch("agy_server.os.name", "nt"), patch(
            "agy_server._resolve_executable", return_value=Path("C:/Windows/System32/taskkill.exe")
        ), patch(
            "agy_server.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["taskkill"], 5),
        ) as run:
            server._kill_process_tree(process)
        self.assertTrue(process.killed)
        run.assert_called_once()

    def test_posix_killpg_success_and_fallback(self):
        class FakeProcess:
            pid = 80
            returncode = None
            killed = False

            def kill(self):
                self.killed = True

        process = FakeProcess()
        with patch("agy_server.os.name", "posix"), patch(
            "agy_server.os.killpg", create=True
        ) as killpg, patch("agy_server.signal.SIGKILL", 9, create=True):
            server._kill_process_tree(process)
        killpg.assert_called_once_with(80, 9)
        self.assertFalse(process.killed)

        fallback = FakeProcess()
        with patch("agy_server.os.name", "posix"), patch(
            "agy_server.os.killpg", create=True, side_effect=OSError("no process group")
        ), patch("agy_server.signal.SIGKILL", 9, create=True):
            server._kill_process_tree(fallback)
        self.assertTrue(fallback.killed)

        class DeniedProcess(FakeProcess):
            def kill(self):
                raise OSError("access denied")

        with patch("agy_server.os.name", "posix"), patch(
            "agy_server.os.killpg", create=True, side_effect=OSError("no process group")
        ), patch("agy_server.signal.SIGKILL", 9, create=True):
            server._kill_process_tree(DeniedProcess())

    def test_finish_killed_process_swallows_reader_and_wait_failures_and_closes_transport(self):
        class Transport:
            closed = False

            def close(self):
                self.closed = True

        class FakeProcess:
            returncode = None

            def __init__(self, transport):
                self._transport = transport

            async def wait(self):
                raise asyncio.TimeoutError

        async def failing_communication():
            raise RuntimeError("reader failure")

        async def scenario():
            transport = Transport()
            process = FakeProcess(transport)
            communication = asyncio.create_task(failing_communication())
            await server._finish_killed_process(process, communication)
            return transport, communication

        transport, communication = asyncio.run(scenario())
        self.assertTrue(communication.done())
        self.assertTrue(transport.closed)

    def test_read_bounded_none_exact_and_excess(self):
        async def scenario(data, limit):
            stream = asyncio.StreamReader()
            stream.feed_data(data)
            stream.feed_eof()
            return await server._read_bounded(stream, limit)

        self.assertEqual(asyncio.run(server._read_bounded(None, 3)), (b"", False))
        self.assertEqual(asyncio.run(scenario(b"abc", 3)), (b"abc", False))
        self.assertEqual(asyncio.run(scenario(b"abcd", 3)), (b"", True))

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
        self.assertNotIn("ctx", schema["properties"])
        self.assertEqual(schema["properties"]["thinking_level"]["enum"], ["low", "medium", "high"])
        self.assertEqual(schema["properties"]["thinking_level"]["default"], "medium")
        self.assertEqual(schema["properties"]["mode"]["enum"], ["plan", "accept-edits"])
        self.assertEqual(schema["properties"]["mode"]["default"], "accept-edits")

    def test_context_progress_is_monotonic(self):
        class FakeContext:
            calls = []

            async def report_progress(self, *args):
                self.calls.append(args)

        async def execute(**kwargs):
            for message in ("queued", "running", "done"):
                await kwargs["progress"](message)
            return {"status": "SUCCESS"}

        ctx = FakeContext()
        with patch("agy_server._git_root", return_value=Path("C:/repo")), patch(
            "agy_server.execute_with_antigravity_cli", new=execute
        ):
            result = asyncio.run(server.antigravity_cli_execute("x", ctx=ctx))
        self.assertEqual(result, {"status": "SUCCESS"})
        self.assertEqual(ctx.calls, [
            (1, None, "queued"),
            (2, None, "running"),
            (3, None, "done"),
        ])

    def test_concurrent_context_progress_is_serialized(self):
        class FakeContext:
            def __init__(self):
                self.active = 0
                self.max_active = 0
                self.calls = []
                self.first_entered = asyncio.Event()
                self.release_first = asyncio.Event()

            async def report_progress(self, progress, total, message):
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                self.calls.append((progress, total, message))
                try:
                    if message == "first":
                        self.first_entered.set()
                        await self.release_first.wait()
                finally:
                    self.active -= 1

        async def scenario():
            ctx = FakeContext()

            async def execute(**kwargs):
                first = asyncio.create_task(kwargs["progress"]("first"))
                await ctx.first_entered.wait()
                second = asyncio.create_task(kwargs["progress"]("second"))
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                ctx.release_first.set()
                await asyncio.gather(first, second)
                return {"status": "SUCCESS"}

            with patch("agy_server._git_root", return_value=Path("C:/repo")), patch(
                "agy_server.execute_with_antigravity_cli", new=execute
            ):
                result = await server.antigravity_cli_execute("x", ctx=ctx)
            return result, ctx

        result, ctx = asyncio.run(scenario())
        self.assertEqual(result, {"status": "SUCCESS"})
        self.assertEqual(ctx.max_active, 1)
        self.assertEqual(ctx.calls, [
            (1, None, "first"),
            (2, None, "second"),
        ])

    def test_progress_callback_failure_is_generic_and_nonfatal(self):
        async def progress(_message):
            raise RuntimeError("PROGRESS_CALLBACK_SECRET")

        with patch("agy_server._resolve_cli", return_value=Path("C:/bin/agy.exe")), patch(
            "agy_server._run_cli", new=AsyncMock(return_value=(
                0, '{"status":"SUCCESS","response":"ok"}', False
            ))
        ), self.assertLogs(server.logger, level="DEBUG") as logs:
            result = asyncio.run(server.execute_with_antigravity_cli(
                workspace=Path("C:/repo"), prompt="x", thinking_level="low",
                mode="plan", progress=progress,
            ))
        self.assertEqual(result["result"], "ok")
        self.assertIn("MCP progress notification failed", "\n".join(logs.output))
        self.assertNotIn("PROGRESS_CALLBACK_SECRET", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
