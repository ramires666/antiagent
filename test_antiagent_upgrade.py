from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
