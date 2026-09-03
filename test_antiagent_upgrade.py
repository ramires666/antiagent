from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import antiagent_upgrade


def completed(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class WindowsProcessGuardTest(unittest.TestCase):
    def test_active_mcp_process_blocks_upgrade_before_pipx(self):
        scanner = mock.Mock(return_value=[("antiagent-mcp.exe", 4321)])

        with self.assertRaisesRegex(antiagent_upgrade.UpgradeError, "PID 4321"):
            antiagent_upgrade.ensure_upgrade_is_safe(platform_name="nt", scanner=scanner)

        scanner.assert_called_once()

    def test_no_matching_process_allows_upgrade(self):
        scanner = mock.Mock(return_value=[("codex.exe", 1234)])

        antiagent_upgrade.ensure_upgrade_is_safe(platform_name="nt", scanner=scanner)

        scanner.assert_called_once()

    def test_process_probe_failure_is_fail_closed(self):
        scanner = mock.Mock(side_effect=OSError("blocked"))

        with self.assertRaisesRegex(antiagent_upgrade.UpgradeError, "Unable to verify"):
            antiagent_upgrade.ensure_upgrade_is_safe(platform_name="nt", scanner=scanner)

    def test_unexpected_process_probe_failure_is_fail_closed(self):
        scanner = mock.Mock(side_effect=RuntimeError("unexpected"))

        with self.assertRaisesRegex(antiagent_upgrade.UpgradeError, "Unable to verify"):
            antiagent_upgrade.ensure_upgrade_is_safe(platform_name="nt", scanner=scanner)


class ProjectRootValidationTest(unittest.TestCase):
    @staticmethod
    def _write_project(root: Path, *, name: str = "antiagent-mcp") -> None:
        (root / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\n', encoding="utf-8"
        )
        for filename in (
            "antiagent_setup.py", "agy_server.py", "agent_manager.py",
            "response_diagnostics.py", "runtime_identity.py",
        ):
            (root / filename).touch()

    def test_accepts_complete_antiagent_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_project(root)

            resolved = antiagent_upgrade._project_root(str(root))

        self.assertEqual(resolved, root.resolve())

    def test_rejects_unrelated_project_before_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_project(root, name="unrelated")

            with self.assertRaisesRegex(
                antiagent_upgrade.UpgradeError, "antiagent-mcp"
            ):
                antiagent_upgrade._project_root(str(root))

    def test_rejects_missing_required_source_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_project(root)
            (root / "agy_server.py").unlink()

            with self.assertRaisesRegex(antiagent_upgrade.UpgradeError, "agy_server.py"):
                antiagent_upgrade._project_root(str(root))


class UpgradeWorkflowTest(unittest.TestCase):
    def test_upgrade_runs_pipx_then_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyproject.toml").touch()
            (root / "antiagent_setup.py").touch()
            runner = mock.Mock(side_effect=[completed(), completed()])
            with mock.patch("antiagent_upgrade.ensure_upgrade_is_safe") as guard:
                antiagent_upgrade.upgrade(root, runner=runner)

        guard.assert_called_once_with(scanner=antiagent_upgrade._windows_processes)
        install = runner.call_args_list[0]
        self.assertEqual(install.args[0][:5], [sys.executable, "-m", "pipx", "install", "--force"])
        self.assertEqual(runner.call_args_list[1].args[0][1], str(root / "antiagent_setup.py"))

    def test_guard_failure_prevents_any_mutation(self):
        runner = mock.Mock()
        with mock.patch(
            "antiagent_upgrade.ensure_upgrade_is_safe",
            side_effect=antiagent_upgrade.UpgradeError("active"),
        ):
            with self.assertRaises(antiagent_upgrade.UpgradeError):
                antiagent_upgrade.upgrade(Path.cwd(), runner=runner)
        runner.assert_not_called()


class MainTest(unittest.TestCase):
    def test_no_register_success_message_is_accurate(self):
        stdout = io.StringIO()
        with (
            mock.patch("antiagent_upgrade._project_root", return_value=Path.cwd()),
            mock.patch("antiagent_upgrade.upgrade") as upgrade,
            mock.patch("sys.stdout", stdout),
        ):
            result = antiagent_upgrade.main(["--no-register"])

        self.assertEqual(result, 0)
        upgrade.assert_called_once_with(Path.cwd(), dry_run=False, register=False)
        self.assertIn("registration was not changed", stdout.getvalue())
        self.assertNotIn("upgraded and registered", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
