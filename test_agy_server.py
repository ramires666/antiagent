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
from mcp.types import CallToolResult


OUTPUT_FIELDS = {
    "status", "result", "model", "thinking_level", "mode", "usage",
    "conversation_id", "result_truncated", "error_type", "exit_code",
    "retryable", "run_id", "started_at", "finished_at", "duration_seconds",
    "cli_version", "metadata_complete", "usage_available",
    "conversation_id_available",
    "preexisting_dirty", "worktree_changed", "changed_paths",
    "postflight_complete", "requires_review",
}


def result_data(result):
    return result.structured_content if isinstance(result, CallToolResult) else result


_LOCK_WORKER = r'''
import asyncio
import os
import sys
from pathlib import Path

import agy_server


async def main():
    root, lock_directory, marker, mode, hold, timeout = sys.argv[1:]
    agy_server._LOCK_DIRECTORY = Path(lock_directory)
    lock = agy_server.WorkspaceLock(Path(root))
    try:
        await lock.acquire(float(timeout))
    except agy_server.WorkspaceLockTimeout:
        Path(marker).write_text("timeout", encoding="ascii")
        return
    except agy_server.WorkspaceLockError:
        Path(marker).write_text("error", encoding="ascii")
        return
    Path(marker).write_text("acquired", encoding="ascii")
    if mode == "crash":
        os._exit(0)
    try:
        await asyncio.sleep(float(hold))
    finally:
        lock.release()


asyncio.run(main())
'''


class AgyServerTest(unittest.TestCase):
    @staticmethod
    def _start_lock_worker(root, lock_directory, marker, *, mode="hold", hold=0, timeout=1):
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                _LOCK_WORKER,
                str(root),
                str(lock_directory),
                str(marker),
                mode,
                str(hold),
                str(timeout),
            ],
            cwd=str(Path(__file__).resolve().parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _wait_for_lock_marker(self, process, marker, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if marker.exists():
                value = marker.read_text(encoding="ascii")
                if value:
                    return value
            if process.poll() is not None:
                _, stderr = process.communicate()
                self.fail(f"lock worker exited before marker: {stderr}")
            time.sleep(0.02)
        self.fail("lock worker did not signal readiness")

    @staticmethod
    def _stop_lock_worker(process):
        if process.poll() is None:
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        if process.stderr is not None:
            process.stderr.close()

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

    def test_cli_run_result_preserves_legacy_positional_flags(self):
        result = server.CliRunResult(1, "output", False, True, True)
        self.assertTrue(result.command_line_too_long)
        self.assertTrue(result.output_limit)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result, (1, "output", False))

    def test_validation_happens_before_cli_resolution(self):
        with patch("agy_server._resolve_cli") as resolve:
            empty = asyncio.run(server.antigravity_cli_execute("  "))
            bad_level = asyncio.run(server.antigravity_cli_execute("x", thinking_level="bad"))
            bad_mode = asyncio.run(server.antigravity_cli_execute("x", mode="yolo"))
        resolve.assert_not_called()
        self.assertTrue(empty.is_error)
        self.assertTrue(bad_level.is_error)
        self.assertTrue(bad_mode.is_error)
        self.assertEqual(result_data(empty)["status"], "ERROR")
        self.assertEqual(result_data(bad_level)["status"], "ERROR")
        self.assertEqual(result_data(bad_mode)["status"], "ERROR")

    def test_preflight_errors_are_typed_and_have_run_metadata(self):
        errors = (
            "path_not_found",
            "path_outside_allowed_root",
            "workspace_not_git",
            "workspace_not_root",
            "git_trust_denied",
            "git_unavailable",
        )
        for error_type in errors:
            with self.subTest(error_type=error_type), patch(
                "agy_server._git_preflight",
                return_value=server.GitPreflight(None, error_type),
            ), patch(
                "agy_server.execute_with_antigravity_cli", new=AsyncMock()
            ) as execute:
                result = asyncio.run(server.antigravity_cli_execute("x"))
            data = result_data(result)
            self.assertEqual(data["error_type"], error_type)
            self.assertRegex(data["run_id"], r"^[0-9a-f]{32}$")
            self.assertTrue(data["started_at"].endswith("Z"))
            self.assertTrue(data["finished_at"].endswith("Z"))
            self.assertGreaterEqual(data["duration_seconds"], 0)
            execute.assert_not_awaited()

    def test_success_reports_cli_version_and_metadata_completeness(self):
        payload = json.dumps({
            "status": "SUCCESS",
            "response": "ok",
            "usage": {"total_tokens": 3},
            "conversation_id": "conversation-1",
        })
        with patch(
            "agy_server._resolve_cli", return_value=Path("C:/bin/agy.exe")
        ), patch(
            "agy_server._probe_cli_version", return_value="1.1.22"
        ), patch(
            "agy_server._run_cli", new=AsyncMock(return_value=(0, payload, False))
        ):
            data = asyncio.run(server.execute_with_antigravity_cli(
                workspace=Path("C:/repo"), prompt="x",
                thinking_level="medium", mode="plan",
            ))
        self.assertEqual(data["status"], "SUCCESS")
        self.assertIsNone(data["error_type"])
        self.assertEqual(data["exit_code"], 0)
        self.assertEqual(data["cli_version"], "1.1.22")
        self.assertTrue(data["usage_available"])
        self.assertTrue(data["conversation_id_available"])
        self.assertTrue(data["metadata_complete"])

    def test_cli_version_probe_is_bounded_and_uses_sanitized_environment(self):
        completed = type("Completed", (), {"returncode": 0, "stdout": "1.1.22\n"})()
        with patch.dict(os.environ, {"TEST_API_KEY": "secret"}, clear=False), patch(
            "agy_server.subprocess.run", return_value=completed
        ) as run:
            self.assertEqual(server._probe_cli_version(Path("C:/bin/agy.exe")), "1.1.22")
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["cwd"], str(Path("C:/bin")))
        self.assertEqual(kwargs["timeout"], 5)
        self.assertFalse(kwargs["shell"])
        self.assertNotIn("TEST_API_KEY", kwargs["env"])

    def _execute(
        self, *, level="low", mode="plan", stdout=None, returncode=0,
        timed_out=False, stderr="",
    ):
        captured = {}

        async def fake_run(argv, cwd, timeout_seconds):
            captured["argv"] = argv
            captured["cwd"] = cwd
            captured["timeout_seconds"] = timeout_seconds
            return server.CliRunResult(
                returncode,
                json.dumps(stdout if stdout is not None else {
                    "status": "SUCCESS", "response": "marker",
                }),
                timed_out,
                stderr=stderr,
            )

        with patch(
            "agy_server._git_preflight",
            return_value=server.GitPreflight(Path("C:/repo"), None),
        ), patch(
            "agy_server._resolve_cli", return_value=Path("C:/bin/agy.exe")
        ), patch("agy_server._run_cli", new=fake_run), patch(
            "agy_server._git_status_snapshot",
            return_value=server.GitStatusSnapshot({}),
        ):
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

        with patch(
            "agy_server._git_preflight",
            return_value=server.GitPreflight(None, "workspace_not_git"),
        ), patch(
            "agy_server.execute_with_antigravity_cli", new=AsyncMock()
        ) as execute:
            result = asyncio.run(server.antigravity_cli_execute("x"))
        self.assertEqual(result_data(result)["result"], "current working directory must be a Git root")
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

    def test_git_status_parser_handles_rename_and_rejects_unbounded_paths(self):
        snapshot = server._parse_git_status(
            b" M changed.py\0R  renamed.py\0old.py\0"
        )
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.entries, {
            "changed.py": " M",
            "renamed.py": "R ",
            "old.py": "R ",
        })
        self.assertIsNone(server._parse_git_status(b"bad\0"))
        self.assertIsNone(server._parse_git_status(b" M truncated.py"))

    def test_build_argv_adds_only_validated_conversation_id(self):
        conversation_id = "123e4567-e89b-12d3-a456-426614174000"
        argv = server._build_argv(
            Path("C:/bin/agy.exe"), Path("C:/repo"), "PROMPT", "low", "plan", 10,
            conversation_id,
        )
        self.assertEqual(argv[-2:], ["--conversation", conversation_id])
        self.assertNotIn("--conversation", server._build_argv(
            Path("C:/bin/agy.exe"), Path("C:/repo"), "PROMPT", "low", "plan", 10
        ))

    def test_handler_rejects_non_uuid_conversation_id_before_git(self):
        with patch("agy_server._git_preflight") as preflight, patch(
            "agy_server._resolve_cli"
        ) as resolve_cli:
            result = asyncio.run(server.antigravity_cli_execute(
                "x", conversation_id="conversation-1"
            ))
        data = result_data(result)
        self.assertEqual(data["error_type"], "invalid_request")
        self.assertIn("conversation_id", data["result"])
        preflight.assert_not_called()
        resolve_cli.assert_not_called()

    def test_handler_passes_valid_conversation_id_to_executor(self):
        conversation_id = "123e4567-e89b-12d3-a456-426614174000"
        expected = {"status": "SUCCESS"}
        with patch(
            "agy_server._git_preflight",
            return_value=server.GitPreflight(Path("C:/repo"), None),
        ), patch(
            "agy_server.execute_with_antigravity_cli", new=AsyncMock(return_value=expected)
        ) as execute:
            result = asyncio.run(server.antigravity_cli_execute(
                "x", conversation_id=conversation_id
            ))
        self.assertEqual(result, expected)
        self.assertEqual(execute.await_args.kwargs["conversation_id"], conversation_id)

    def test_git_status_snapshot_rejects_oversized_output_without_exposing_it(self):
        def run(*_args, stdout, **kwargs):
            self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
            stdout.write(b"x" * (server.MAX_GIT_STATUS_BYTES + 1))
            return type("Completed", (), {"returncode": 0})()

        with patch("agy_server._resolve_executable", return_value=Path("C:/git.exe")), patch(
            "agy_server.subprocess.run", side_effect=run
        ):
            self.assertIsNone(server._git_status_snapshot(Path("C:/repo")))

    def test_postflight_detects_another_edit_to_preexisting_dirty_file(self):
        with tempfile.TemporaryDirectory(prefix="agy-dirty-") as directory:
            base = Path(directory)
            workspace = base / "repo"
            workspace.mkdir()
            tracked = workspace / "tracked.txt"
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            tracked.write_text("base-value\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=workspace, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "initial"],
                cwd=workspace,
                check=True,
            )
            tracked.write_text("dirty-one\n", encoding="utf-8")

            async def run(*_args):
                time.sleep(0.01)
                tracked.write_text("dirty-two\n", encoding="utf-8")
                return 0, '{"status":"SUCCESS","response":"ok"}', False

            with patch("agy_server._LOCK_DIRECTORY", base / "locks"), patch(
                "agy_server._resolve_cli", return_value=Path("C:/bin/agy.exe")
            ), patch("agy_server._probe_cli_version", return_value="1.1.22"), patch(
                "agy_server._run_cli", new=run
            ):
                result = asyncio.run(server.execute_with_antigravity_cli(
                    workspace=workspace, prompt="x", thinking_level="low",
                    mode="accept-edits",
                ))
            self.assertTrue(result["preexisting_dirty"])
            self.assertTrue(result["worktree_changed"])
            self.assertEqual(result["changed_paths"], ["tracked.txt"])

    def test_unknown_postflight_requires_review(self):
        with tempfile.TemporaryDirectory(prefix="agy-review-") as directory:
            workspace = Path(directory)
            statuses = iter((server.GitStatusSnapshot({}), None))
            with patch("agy_server._LOCK_DIRECTORY", workspace / "locks"), patch(
                "agy_server._resolve_cli", return_value=Path("C:/bin/agy.exe")
            ), patch("agy_server._probe_cli_version", return_value="1.1.22"), patch(
                "agy_server._git_status_snapshot", side_effect=lambda _root: next(statuses)
            ), patch(
                "agy_server._run_cli",
                new=AsyncMock(return_value=(0, '{"status":"SUCCESS","response":"ok"}', False)),
            ):
                result = asyncio.run(server.execute_with_antigravity_cli(
                    workspace=workspace, prompt="x", thinking_level="low",
                    mode="accept-edits",
                ))
            self.assertFalse(result["postflight_complete"])
            self.assertTrue(result["requires_review"])
            self.assertTrue(any((workspace / "locks").glob("*.review")))

    def test_accept_edits_postflight_records_changes_and_clears_marker(self):
        with tempfile.TemporaryDirectory(prefix="agy-review-") as directory:
            workspace = Path(directory)
            lock_directory = workspace / "locks"
            statuses = iter((
                server.GitStatusSnapshot({"preexisting.py": " M"}),
                server.GitStatusSnapshot({"edited.py": "??"}),
            ))
            with patch("agy_server._LOCK_DIRECTORY", lock_directory), patch(
                "agy_server._resolve_cli", return_value=Path("C:/bin/agy.exe")
            ), patch("agy_server._probe_cli_version", return_value="1.1.22"), patch(
                "agy_server._git_status_snapshot", side_effect=lambda _root: next(statuses)
            ), patch(
                "agy_server._run_cli",
                new=AsyncMock(return_value=(
                    0, '{"status":"SUCCESS","response":"ok"}', False
                )),
            ):
                result = asyncio.run(server.execute_with_antigravity_cli(
                    workspace=workspace,
                    prompt="x",
                    thinking_level="low",
                    mode="accept-edits",
                ))
            self.assertTrue(result["preexisting_dirty"])
            self.assertTrue(result["worktree_changed"])
            self.assertEqual(result["changed_paths"], ["edited.py", "preexisting.py"])
            self.assertTrue(result["postflight_complete"])
            self.assertFalse(result["requires_review"])
            self.assertFalse(server._review_marker_path(workspace).exists())

    def test_failed_accept_edits_requires_review_before_next_edit(self):
        with tempfile.TemporaryDirectory(prefix="agy-review-") as directory:
            workspace = Path(directory)
            lock_directory = workspace / "locks"
            statuses = iter((
                server.GitStatusSnapshot({}),
                server.GitStatusSnapshot({"partial.py": " M"}),
                server.GitStatusSnapshot({"partial.py": " M"}),
            ))
            run = AsyncMock(return_value=(1, '{"status":"FAIL"}', False))
            with patch("agy_server._LOCK_DIRECTORY", lock_directory), patch(
                "agy_server._resolve_cli", return_value=Path("C:/bin/agy.exe")
            ), patch("agy_server._probe_cli_version", return_value="1.1.22"), patch(
                "agy_server._git_status_snapshot", side_effect=lambda _root: next(statuses)
            ), patch("agy_server._run_cli", new=run):
                first = asyncio.run(server.execute_with_antigravity_cli(
                    workspace=workspace, prompt="x", thinking_level="low",
                    mode="accept-edits",
                ))
                second = asyncio.run(server.execute_with_antigravity_cli(
                    workspace=workspace, prompt="x", thinking_level="low",
                    mode="accept-edits",
                ))
            self.assertTrue(first["requires_review"])
            self.assertEqual(first["changed_paths"], ["partial.py"])
            self.assertEqual(second["error_type"], "review_required")
            self.assertTrue(second["requires_review"])
            run.assert_awaited_once()

    def test_acknowledge_review_allows_next_edit_and_clears_marker(self):
        with tempfile.TemporaryDirectory(prefix="agy-review-") as directory:
            workspace = Path(directory)
            lock_directory = workspace / "locks"
            statuses = iter((
                server.GitStatusSnapshot({}),
                server.GitStatusSnapshot({"partial.py": " M"}),
                server.GitStatusSnapshot({"partial.py": " M"}),
                server.GitStatusSnapshot({"partial.py": " M"}),
            ))
            run = AsyncMock(side_effect=(
                (1, '{"status":"FAIL"}', False),
                (0, '{"status":"SUCCESS","response":"ok"}', False),
            ))
            with patch("agy_server._LOCK_DIRECTORY", lock_directory), patch(
                "agy_server._resolve_cli", return_value=Path("C:/bin/agy.exe")
            ), patch("agy_server._probe_cli_version", return_value="1.1.22"), patch(
                "agy_server._git_status_snapshot", side_effect=lambda _root: next(statuses)
            ), patch("agy_server._run_cli", new=run):
                first = asyncio.run(server.execute_with_antigravity_cli(
                    workspace=workspace, prompt="x", thinking_level="low",
                    mode="accept-edits",
                ))
                second = asyncio.run(server.execute_with_antigravity_cli(
                    workspace=workspace, prompt="x", thinking_level="low",
                    mode="accept-edits", acknowledge_review=True,
                ))
            self.assertTrue(first["requires_review"])
            self.assertEqual(second["status"], "SUCCESS")
            self.assertFalse(second["requires_review"])
            self.assertFalse(server._review_marker_path(workspace).exists())
            self.assertEqual(run.await_count, 2)

    def test_cancelled_accept_edit_runs_postflight_and_leaves_marker(self):
        with tempfile.TemporaryDirectory(prefix="agy-review-") as directory:
            workspace = Path(directory)
            lock_directory = workspace / "locks"
            statuses = iter((
                server.GitStatusSnapshot({}),
                server.GitStatusSnapshot({"partial.py": " M"}),
            ))
            run = AsyncMock(side_effect=asyncio.CancelledError)
            with patch("agy_server._LOCK_DIRECTORY", lock_directory), patch(
                "agy_server._resolve_cli", return_value=Path("C:/bin/agy.exe")
            ), patch("agy_server._probe_cli_version", return_value="1.1.22"), patch(
                "agy_server._git_status_snapshot", side_effect=lambda _root: next(statuses)
            ), patch("agy_server._run_cli", new=run) as cli:
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(server.execute_with_antigravity_cli(
                        workspace=workspace, prompt="x", thinking_level="low",
                        mode="accept-edits",
                    ))
            self.assertEqual(cli.await_count, 1)
            self.assertTrue(any(lock_directory.glob("*.review")))

    def test_cli_output_limit_is_typed_error(self):
        with patch("agy_server._resolve_cli", return_value=Path("C:/bin/agy.exe")), patch(
            "agy_server._probe_cli_version", return_value="1.1.22"
        ), patch(
            "agy_server._run_cli",
            new=AsyncMock(return_value=server.CliRunResult(0, "", False, output_limit=True)),
        ):
            result = asyncio.run(server.execute_with_antigravity_cli(
                workspace=Path("C:/repo"), prompt="x", thinking_level="low", mode="plan"
            ))
        self.assertEqual(result["error_type"], "output_limit")
        self.assertEqual(result["exit_code"], 0)

    def test_handler_rejects_explicit_invalid_working_directory_before_cli(self):
        working_directory = str(Path.cwd() / "missing-workspace")
        with patch(
            "agy_server._git_preflight",
            return_value=server.GitPreflight(None, "path_not_found"),
        ) as git_preflight, patch(
            "agy_server.execute_with_antigravity_cli", new=AsyncMock()
        ) as execute, patch("agy_server._resolve_cli") as resolve_cli:
            result = asyncio.run(server.antigravity_cli_execute(
                "x", working_directory=working_directory
            ))
        git_preflight.assert_called_once_with(working_directory)
        execute.assert_not_awaited()
        resolve_cli.assert_not_called()
        self.assertEqual(result_data(result)["status"], "ERROR")
        self.assertEqual(
            result_data(result)["result"], "working_directory must be an existing Git root"
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
            with self.subTest(arguments=arguments), patch(
                "agy_server._git_preflight"
            ) as preflight:
                result = asyncio.run(server.antigravity_cli_execute(**arguments))
            self.assertEqual(result_data(result)["result"], expected)
            preflight.assert_not_called()

        with patch("agy_server._git_preflight") as preflight, patch(
            "agy_server.execute_with_antigravity_cli", new=AsyncMock()
        ) as execute:
            result = asyncio.run(server.antigravity_cli_execute(
                "x", working_directory=123
            ))
        self.assertEqual(result_data(result)["status"], "ERROR")
        preflight.assert_not_called()
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
        with patch(
            "agy_server._git_preflight",
            return_value=server.GitPreflight(Path("C:/repo"), None),
        ), patch(
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

    def test_runtime_error_result_is_mcp_error_without_input_echo(self):
        secret = "UNIT_SENTINEL_SECRET"
        for arguments in (
            {"task": "x", "context": {"secret": secret}},
            {"task": "x", "thinking_level": secret},
            {"task": "x", "working_directory": {"secret": secret}},
        ):
            with self.subTest(arguments=arguments):
                result = asyncio.run(server.antigravity_cli_execute(**arguments))
                self.assertIsInstance(result, CallToolResult)
                self.assertTrue(result.is_error)
                self.assertEqual(set(result.structured_content), OUTPUT_FIELDS)
                self.assertNotIn(secret, repr(result.content))
                self.assertNotIn(secret, repr(result.structured_content))

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
            "status": "SUCCESS", "response": "H" * server.MAX_RESULT_CHARS + "T" * 20,
            "conversation_id": "safe-id_1", "usage": {
                "input_tokens": 2, "output_tokens": 3, "total_tokens": 5,
                "secret": "discard",
            },
        }
        result, _ = self._execute(stdout=payload)
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(len(result["result"]), server.MAX_RESULT_CHARS)
        self.assertTrue(result["result_truncated"])
        self.assertIn(server.TRUNCATION_MARKER, result["result"])
        self.assertTrue(result["result"].startswith("H"))
        self.assertTrue(result["result"].endswith("T"))
        self.assertEqual(result["thinking_level"], "low")
        self.assertEqual(result["model"], "gemini-3.7-flash-low")
        self.assertEqual(result["mode"], "plan")
        self.assertEqual(result["conversation_id"], "safe-id_1")
        self.assertEqual(result["usage"], {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5})
        self.assertNotIn("secret", json.dumps(result))
        with patch.object(server, "MAX_RESULT_CHARS", len(server.TRUNCATION_MARKER) + 1):
            edge = server._empty_result("SUCCESS", "abcdef" * 20, "low", "plan")
        self.assertEqual(len(edge["result"]), len(server.TRUNCATION_MARKER) + 1)
        self.assertEqual(edge["result"], "a" + server.TRUNCATION_MARKER)

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
                self.assertEqual(result_data(result)["status"], "ERROR")
                self.assertEqual(result_data(result)["result"], expected)

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

    def test_workspace_lock_is_canonical_nonblocking_and_persistent(self):
        async def scenario(lock_directory):
            with patch("agy_server._LOCK_DIRECTORY", lock_directory):
                root = Path(lock_directory) / "repo"
                root.mkdir(parents=True)
                first = server.WorkspaceLock(root)
                equivalent = server.WorkspaceLock(root / ".")
                self.assertEqual(first.path, equivalent.path)
                await first.acquire(0)
                self.assertTrue(first.path.exists())
                with self.assertRaises(server.WorkspaceLockBusy):
                    await equivalent.acquire(0)
                first.release()
                await equivalent.acquire(0.2)
                equivalent.release()
                self.assertTrue(first.path.exists())

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "locks"))

    def test_workspace_lock_cancellation_does_not_leave_acquired_lock(self):
        async def scenario(lock_directory):
            with patch("agy_server._LOCK_DIRECTORY", lock_directory):
                root = Path(lock_directory) / "repo"
                root.mkdir(parents=True)
                holder = server.WorkspaceLock(root)
                waiter = server.WorkspaceLock(root)
                await holder.acquire(0)
                task = asyncio.create_task(waiter.acquire(5))
                await asyncio.sleep(0.06)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                holder.release()
                await waiter.acquire(0.2)
                waiter.release()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "locks"))

    def test_workspace_lock_multiprocess_contention_and_crash_release(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            lock_directory = base / "locks"
            root = base / "repo"
            root.mkdir()
            owner_marker = base / "owner"
            owner = self._start_lock_worker(
                root, lock_directory, owner_marker, hold=3, timeout=3
            )
            contender = None
            crash = None
            recovery = None
            try:
                self.assertEqual(self._wait_for_lock_marker(owner, owner_marker), "acquired")
                contender_marker = base / "contender"
                contender = self._start_lock_worker(
                    root, lock_directory, contender_marker, timeout=0.3
                )
                self.assertEqual(
                    self._wait_for_lock_marker(contender, contender_marker), "timeout"
                )
                self.assertIsNone(owner.poll())
                self.assertEqual(owner.wait(timeout=4), 0)
            finally:
                if contender is not None:
                    self._stop_lock_worker(contender)
                self._stop_lock_worker(owner)

            crash_marker = base / "crash"
            crash = self._start_lock_worker(
                root, lock_directory, crash_marker, mode="crash", timeout=2
            )
            try:
                self.assertEqual(
                    self._wait_for_lock_marker(crash, crash_marker), "acquired"
                )
                self.assertEqual(crash.wait(timeout=3), 0)

                recovery_marker = base / "recovery"
                recovery = self._start_lock_worker(
                    root, lock_directory, recovery_marker, hold=0.01, timeout=1
                )
                self.assertEqual(
                    self._wait_for_lock_marker(recovery, recovery_marker), "acquired"
                )
                self.assertEqual(recovery.wait(timeout=3), 0)
            finally:
                for process in (crash, recovery):
                    if process is not None:
                        self._stop_lock_worker(process)

    def test_workspace_lock_multiprocess_distinct_roots_run_in_parallel(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            lock_directory = base / "locks"
            root_one = base / "repo-one"
            root_two = base / "repo-two"
            root_one.mkdir()
            root_two.mkdir()
            first_marker = base / "first"
            second_marker = base / "second"
            first = self._start_lock_worker(
                root_one, lock_directory, first_marker, hold=3, timeout=3
            )
            second = None
            try:
                self.assertEqual(self._wait_for_lock_marker(first, first_marker), "acquired")
                second = self._start_lock_worker(
                    root_two, lock_directory, second_marker, hold=0.01, timeout=1
                )
                self.assertEqual(
                    self._wait_for_lock_marker(second, second_marker), "acquired"
                )
                self.assertIsNone(first.poll())
                self.assertEqual(first.wait(timeout=4), 0)
                self.assertEqual(second.wait(timeout=3), 0)
                self.assertNotEqual(
                    server._workspace_lock_path(root_one),
                    server._workspace_lock_path(root_two),
                )
            finally:
                self._stop_lock_worker(first)
                if second is not None:
                    self._stop_lock_worker(second)

    def test_workspace_lock_path_failure_is_safe_result(self):
        async def run(*_args):
            self.fail("CLI must not run when workspace lock setup fails")

        with tempfile.TemporaryDirectory() as directory:
            bad_directory = Path(directory) / "not-a-directory"
            bad_directory.write_text("file", encoding="ascii")
            with patch("agy_server._LOCK_DIRECTORY", bad_directory), patch(
                "agy_server._resolve_cli", return_value=Path("C:/bin/agy.exe")
            ), patch("agy_server._timeout_seconds", return_value=0.1), patch(
                "agy_server._run_cli", new=run
            ):
                result = asyncio.run(server.execute_with_antigravity_cli(
                    workspace=Path("C:/repo"), prompt="x",
                    thinking_level="low", mode="plan",
                ))
        self.assertEqual(result["result"], "Workspace lock could not be acquired")

    def test_outer_timeout_releases_execution_lock(self):
        async def scenario():
            calls = 0

            async def run(*_args):
                nonlocal calls
                calls += 1
                if calls == 1:
                    await asyncio.sleep(_args[-1])
                    return 0, "", True
                return 0, '{"status":"SUCCESS","response":"after timeout"}', False

            with patch("agy_server._resolve_cli", return_value=Path("C:/bin/agy.exe")), patch(
                "agy_server._timeout_seconds", return_value=0.01
            ), patch("agy_server._run_cli", new=run):
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
            with tempfile.TemporaryDirectory() as directory:
                run = AsyncMock(return_value=(
                    0, '{"status":"SUCCESS","response":"after release"}', False
                ))
                with patch("agy_server._LOCK_DIRECTORY", Path(directory)), patch(
                    "agy_server._resolve_cli", return_value=Path("C:/bin/agy.exe")
                ), patch("agy_server._timeout_seconds", return_value=0.01), patch(
                    "agy_server._run_cli", new=run
                ):
                    lock = server.WorkspaceLock(Path("C:/repo"))
                    await lock.acquire(0)
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
        self.assertEqual(blocked["result"], "Workspace lock timed out")
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
                task = asyncio.create_task(server._progress_heartbeat(progress, "run-1"))
                await asyncio.wait_for(emitted.wait(), 1)
                task.cancel()
                await task
            return messages, task.done()

        messages, done = asyncio.run(heartbeat_emits())
        self.assertEqual(messages, ["run_id=run-1 state=running"])
        self.assertTrue(done)

        async def lifecycle(timeout):
            started = asyncio.Event()
            reaped = asyncio.Event()

            async def heartbeat(_progress, _run_id):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    reaped.set()

            async def run(*_args):
                if timeout:
                    await asyncio.sleep(_args[-1])
                    return 0, "", True
                await started.wait()
                return 0, '{"status":"SUCCESS","response":"ok"}', False

            async def progress(_message):
                pass

            with patch("agy_server._resolve_cli", return_value=Path("C:/bin/agy.exe")), patch(
                "agy_server._timeout_seconds", return_value=0.01 if timeout else 1
            ), patch("agy_server._progress_heartbeat", new=heartbeat), patch(
                "agy_server._run_cli", new=run
            ):
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
                self.assertEqual(result_data(result)["status"], "ERROR")
                self.assertEqual(result_data(result)["result"], expected)
                self.assertNotIn("private", json.dumps(result_data(result)))
        missing_response, _ = self._execute(stdout={"status": "SUCCESS"})
        missing_data = result_data(missing_response)
        self.assertEqual(missing_data["status"], "ERROR")
        self.assertEqual(missing_data["error_type"], "no_content")
        self.assertEqual(missing_data["result"], "Antigravity CLI returned no content")
        whitespace_response, _ = self._execute(stdout={
            "status": "SUCCESS", "response": " \r\n\t"
        })
        self.assertEqual(result_data(whitespace_response)["error_type"], "no_content")

    def test_malformed_and_nonzero_output_are_generic(self):
        with patch(
            "agy_server._git_preflight",
            return_value=server.GitPreflight(Path("C:/repo"), None),
        ), patch(
            "agy_server._resolve_cli", return_value=Path("C:/bin/agy.exe")
        ), patch("agy_server._run_cli", new=AsyncMock(return_value=(0, "TOKEN=secret", False))):
            malformed = asyncio.run(server.antigravity_cli_execute("x", mode="plan"))
        self.assertEqual(result_data(malformed)["result"], "Antigravity CLI returned invalid JSON")
        with patch(
            "agy_server._git_preflight",
            return_value=server.GitPreflight(Path("C:/repo"), None),
        ), patch(
            "agy_server._resolve_cli", return_value=Path("C:/bin/agy.exe")
        ), patch("agy_server._run_cli", new=AsyncMock(return_value=(1, '{"status":"FAIL","response":"secret"}', False))):
            failed = asyncio.run(server.antigravity_cli_execute("x", mode="plan"))
        self.assertEqual(result_data(failed)["result"], "Antigravity CLI task failed")
        self.assertNotIn("secret", json.dumps(result_data(failed)))

    def test_cli_failure_classification_precedence_and_redaction(self):
        cases = (
            (
                "profile",
                "Failed to resolve GeminiDir .gemini: Access is denied PROFILE_SECRET",
                "profile_unreadable",
                "Antigravity profile is not readable by the executor",
                False,
            ),
            (
                "profile-writer",
                "summary store recreate failed: unable to open database file WRITER_SECRET",
                "profile_not_writable",
                "Antigravity profile state is not writable by the executor",
                False,
            ),
            (
                "network-before-oauth",
                "connectex: socket in a way forbidden by its access permissions\n"
                "Print mode: triggering interactive OAuth\nNETWORK_SECRET",
                "network_denied",
                "Antigravity network access was denied",
                True,
            ),
            (
                "profile-before-network",
                "profile directory access is denied\nconnectex: network denied PROFILE_NETWORK_SECRET",
                "profile_unreadable",
                "Antigravity profile is not readable by the executor",
                False,
            ),
            (
                "network-before-permission",
                "network denied\nsoft-denying tool confirmation NETWORK_PERMISSION_SECRET",
                "network_denied",
                "Antigravity network access was denied",
                False,
            ),
            (
                "permission-before-oauth",
                "soft-denying tool confirmation\nauth timed out PERMISSION_OAUTH_SECRET",
                "permission_denied",
                "Antigravity tool permission was denied",
                True,
            ),
            (
                "oauth-before-auth",
                "You are not logged into Antigravity\nPrint mode: auth timed out OAUTH_SECRET",
                "oauth_timeout",
                "Antigravity OAuth did not complete",
                True,
            ),
            (
                "auth",
                "You are not logged into Antigravity AUTH_SECRET",
                "auth_missing",
                "Antigravity authentication is unavailable",
                False,
            ),
            (
                "permission",
                'Print mode: soft-denying tool confirmation "ListDir" PERMISSION_SECRET',
                "permission_denied",
                "Antigravity tool permission was denied",
                False,
            ),
            (
                "policy-before-profile",
                "approval-review policy denied payload; .gemini Access is denied POLICY_SECRET",
                "policy_denied",
                "Antigravity payload was denied by policy",
                False,
            ),
        )
        for name, stderr, expected_type, expected_message, timed_out in cases:
            with self.subTest(name=name), self.assertLogs(
                server.logger, level="WARNING"
            ) as logs:
                result, _ = self._execute(
                    stdout={"status": "FAIL", "error": "safe"},
                    returncode=1,
                    timed_out=timed_out,
                    stderr=stderr,
                )
            data = result_data(result)
            self.assertEqual(data["status"], "ERROR")
            self.assertEqual(data["error_type"], expected_type)
            self.assertEqual(data["result"], expected_message)
            self.assertFalse(data["retryable"])
            secret = stderr.split()[-1]
            self.assertNotIn(secret, json.dumps(data))
            self.assertNotIn(secret, "\n".join(logs.output))

        unknown, _ = self._execute(
            stdout={"status": "FAIL"}, returncode=1,
            stderr="unrecognized internal failure UNKNOWN_SECRET",
        )
        self.assertEqual(result_data(unknown)["error_type"], "cli_error")
        self.assertNotIn("UNKNOWN_SECRET", json.dumps(result_data(unknown)))

        stale_auth, _ = self._execute(
            stdout={"status": "FAIL", "error": "backend failure"},
            returncode=1,
            stderr=(
                "You are not logged into Antigravity\n"
                "ChainedAuth: authenticated via keyring STALE_AUTH_SECRET"
            ),
        )
        self.assertEqual(result_data(stale_auth)["error_type"], "cli_error")
        self.assertNotIn("STALE_AUTH_SECRET", json.dumps(result_data(stale_auth)))

        benign_response, _ = self._execute(
            stdout={"status": "FAIL", "response": "user text says policy denied"},
            returncode=1,
            stderr="unrecognized BENIGN_RESPONSE_SECRET",
        )
        self.assertEqual(result_data(benign_response)["error_type"], "cli_error")

        unknown_timeout, _ = self._execute(
            stdout={"status": "FAIL"}, returncode=1, timed_out=True,
            stderr="unrecognized timeout detail TIMEOUT_SECRET",
        )
        self.assertEqual(result_data(unknown_timeout)["error_type"], "timeout")

    def test_classified_stderr_is_not_persisted_in_agent_store(self):
        secret = "PERSISTED_STDERR_SECRET"
        result, _ = self._execute(
            stdout={"status": "FAIL"}, returncode=1,
            stderr=f"network denied {secret}",
        )
        data = result_data(result)
        self.assertEqual(data["error_type"], "network_denied")
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "agents.sqlite3"
            store = server.AgentStore(database, owner_id="stderr-redaction-test")
            snapshot = store.create(Path(directory), "low", "plan")
            store.finish(snapshot.agent_id, "failed", output=data)
            stored = store.get(snapshot.agent_id)
            store.close()
            self.assertIsNotNone(stored)
            self.assertNotIn(secret, json.dumps(stored.output))
            self.assertNotIn(secret.encode(), database.read_bytes())

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

        cli_result = asyncio.run(scenario())
        self.assertEqual(cli_result, (0, "public", False))
        self.assertEqual(cli_result.stderr, "STDERR_SECRET")
        with self.assertLogs(server.logger, level="WARNING") as logs:
            result, _ = self._execute(stdout={
                "status": "FAIL", "response": "PAYLOAD_SECRET"
            })
        self.assertEqual(result_data(result)["result"], "Antigravity CLI task failed")
        self.assertNotIn("PAYLOAD_SECRET", "\n".join(logs.output))

    def test_empty_result_from_output_overflow_is_safe_error(self):
        with patch(
            "agy_server._git_preflight",
            return_value=server.GitPreflight(Path("C:/repo"), None),
        ), patch(
            "agy_server._resolve_cli", return_value=Path("C:/bin/agy.exe")
        ), patch(
            "agy_server._run_cli", new=AsyncMock(return_value=(-9, "", False))
        ):
            result = asyncio.run(server.antigravity_cli_execute("x", mode="plan"))
        data = result_data(result)
        self.assertEqual(data["status"], "ERROR")
        self.assertEqual(data["result"], "Antigravity CLI task failed")
        self.assertEqual(data["error_type"], "cli_error")
        self.assertEqual(data["exit_code"], -9)
        self.assertFalse(data["retryable"])
        self.assertEqual(set(data), OUTPUT_FIELDS)

    def test_prompt_size_validation_happens_before_git_or_cli(self):
        with patch("agy_server._git_preflight") as git_preflight, patch(
            "agy_server.execute_with_antigravity_cli"
        ) as execute:
            result = asyncio.run(server.antigravity_cli_execute(
                "x", context="c" * server.MAX_PROMPT_CHARS
            ))
        self.assertEqual(result_data(result)["result"], "task context is too large")
        git_preflight.assert_not_called()
        execute.assert_not_called()

    def test_prompt_at_maximum_size_is_accepted(self):
        base_prompt = server._prompt("x", "", "")
        context = "c" * (server.MAX_PROMPT_CHARS - len(base_prompt))
        expected = {"status": "SUCCESS", "result": "ok"}
        with patch(
            "agy_server._git_preflight",
            return_value=server.GitPreflight(Path("C:/repo"), None),
        ), patch(
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
        self.assertEqual(schema["properties"]["mode"]["default"], "plan")

    def test_managed_agent_spawn_wait_followup_list_and_persistence(self):
        conversation_id = "123e4567-e89b-12d3-a456-426614174000"

        async def scenario(store):
            calls = []

            async def execute(**kwargs):
                calls.append(kwargs)
                return {
                    "status": "SUCCESS",
                    "result": "managed-ok",
                    "conversation_id": conversation_id,
                }

            workspace = Path.cwd().resolve()
            with patch.object(server, "_AGENT_STORE", store), patch(
                "agy_server._git_preflight",
                return_value=server.GitPreflight(workspace, None),
            ), patch(
                "agy_server.execute_with_antigravity_cli", new=execute
            ):
                spawned = await server.antigravity_agent_spawn(
                    "managed task", thinking_level="low", mode="accept-edits"
                )
                agent_id = spawned["agent"]["agent_id"]
                self.assertEqual(spawned["agent"]["status"], "queued")
                waited = await server.antigravity_agent_wait(agent_id, 2)
                self.assertEqual(waited["agent"]["status"], "completed")
                self.assertEqual(waited["agent"]["output"]["result"], "managed-ok")

                followup = await server.antigravity_agent_followup(agent_id, "continue")
                child_id = followup["agent"]["agent_id"]
                child = await server.antigravity_agent_wait(child_id, 2)
                self.assertEqual(child["agent"]["parent_agent_id"], agent_id)
                self.assertEqual(calls[0]["mode"], "accept-edits")
                self.assertEqual(calls[1]["mode"], "plan")
                self.assertEqual(calls[1]["conversation_id"], conversation_id)

                listed = await server.antigravity_agent_list(limit=10)
                self.assertEqual(
                    {item["agent_id"] for item in listed["agents"]},
                    {agent_id, child_id},
                )
                return agent_id

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agents.sqlite3"
            store = server.AgentStore(path, owner_id="lifecycle-test")
            agent_id = asyncio.run(scenario(store))
            store.close()
            restored = server.AgentStore(path, owner_id="lifecycle-test")
            self.assertEqual(restored.get(agent_id).status, "completed")
            restored.close()
        self.assertFalse(server._AGENT_TASKS)

    def test_managed_agent_wait_timeout_and_interrupt(self):
        async def scenario(store):
            started = asyncio.Event()
            cancelled = asyncio.Event()

            async def execute(**_kwargs):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

            workspace = Path.cwd().resolve()
            with patch.object(server, "_AGENT_STORE", store), patch(
                "agy_server._git_preflight",
                return_value=server.GitPreflight(workspace, None),
            ), patch(
                "agy_server.execute_with_antigravity_cli", new=execute
            ):
                spawned = await server.antigravity_agent_spawn("slow managed task")
                agent_id = spawned["agent"]["agent_id"]
                await asyncio.wait_for(started.wait(), timeout=1)
                timed_out = await server.antigravity_agent_wait(agent_id, 0.01)
                self.assertTrue(timed_out["wait_timed_out"])
                interrupted = await server.antigravity_agent_interrupt(agent_id)
                self.assertEqual(interrupted["agent"]["status"], "interrupted")
                self.assertTrue(interrupted["agent"]["cancel_requested"])
                self.assertTrue(cancelled.is_set())
                repeated = await server.antigravity_agent_interrupt(agent_id)
                self.assertEqual(repeated["agent"]["status"], "interrupted")

        with tempfile.TemporaryDirectory() as directory:
            store = server.AgentStore(
                Path(directory) / "agents.sqlite3", owner_id="interrupt-test"
            )
            asyncio.run(scenario(store))
            store.close()
        self.assertFalse(server._AGENT_TASKS)

    def test_managed_agent_immediate_and_cross_store_interrupt(self):
        async def scenario(primary, peer):
            cancelled = asyncio.Event()

            async def execute(**_kwargs):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

            workspace = Path.cwd().resolve()
            with patch.object(server, "_AGENT_STORE", primary), patch(
                "agy_server._git_preflight",
                return_value=server.GitPreflight(workspace, None),
            ), patch(
                "agy_server.execute_with_antigravity_cli", new=execute
            ):
                immediate = await server.antigravity_agent_spawn("cancel immediately")
                immediate_id = immediate["agent"]["agent_id"]
                stopped = await server.antigravity_agent_interrupt(immediate_id)
                self.assertEqual(stopped["agent"]["status"], "interrupted")

                spawned = await server.antigravity_agent_spawn("cross-store cancel")
                agent_id = spawned["agent"]["agent_id"]
                for _ in range(20):
                    if primary.get(agent_id).status == "running":
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(primary.get(agent_id).status, "running")
                peer.request_cancel(agent_id)
                waited = await server.antigravity_agent_wait(agent_id, 2)
                self.assertEqual(waited["agent"]["status"], "interrupted")
                self.assertTrue(cancelled.is_set())

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agents.sqlite3"
            primary = server.AgentStore(path, owner_id="cross-store-test")
            peer = server.AgentStore(path, owner_id="cross-store-test")
            asyncio.run(scenario(primary, peer))
            peer.close()
            primary.close()
        self.assertFalse(server._AGENT_TASKS)

    def test_managed_agent_validation_and_workspace_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            store = server.AgentStore(
                Path(directory) / "agents.sqlite3", owner_id="validation-test"
            )
            with patch.object(server, "_AGENT_STORE", store), patch(
                "agy_server._git_preflight"
            ) as preflight:
                invalid = asyncio.run(server.antigravity_agent_spawn(""))
                invalid_id = asyncio.run(server.antigravity_agent_status("bad"))
            self.assertTrue(result_data(invalid).get("error_type") == "invalid_request")
            self.assertTrue(
                result_data(invalid_id).get("error_type") == "invalid_request"
            )
            preflight.assert_not_called()

            outside = store.create(
                workspace=Path(directory), thinking_level="low", mode="plan"
            )
            with patch.object(server, "_AGENT_STORE", store):
                hidden = asyncio.run(server.antigravity_agent_status(outside.agent_id))
            self.assertEqual(result_data(hidden)["error_type"], "agent_not_found")
            store.finish(outside.agent_id, "interrupted")
            store.close()

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
        with patch(
            "agy_server._git_preflight",
            return_value=server.GitPreflight(Path("C:/repo"), None),
        ), patch(
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

            with patch(
                "agy_server._git_preflight",
                return_value=server.GitPreflight(Path("C:/repo"), None),
            ), patch(
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
