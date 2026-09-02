from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import antiagent_setup


class ResolveLauncherTest(unittest.TestCase):
    def test_finds_pipx_launcher_without_path_entry(self):
        executable_name = "antiagent-mcp.exe" if os.name == "nt" else "antiagent-mcp"
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            launcher = profile / ".local" / "bin" / executable_name
            launcher.parent.mkdir(parents=True)
            launcher.touch()

            resolved = antiagent_setup.resolve_mcp_launcher(
                environ={"USERPROFILE": str(profile), "PATH": ""},
                argv0="python",
            )

        self.assertEqual(resolved, launcher.resolve())
        self.assertTrue(resolved.is_absolute())

    def test_explicit_missing_launcher_is_rejected(self):
        with self.assertRaisesRegex(antiagent_setup.SetupError, "does not exist"):
            antiagent_setup.resolve_mcp_launcher(
                "missing-antiagent-mcp.exe",
                environ={"PATH": ""},
                argv0="python",
            )


class ResolveCodexTest(unittest.TestCase):
    def test_finds_codex_under_local_app_data(self):
        with tempfile.TemporaryDirectory() as directory:
            local_app_data = Path(directory)
            codex = local_app_data / "OpenAI" / "Codex" / "bin" / "build-id" / "codex.exe"
            codex.parent.mkdir(parents=True)
            codex.touch()

            resolved = antiagent_setup.resolve_codex_cli(
                environ={"LOCALAPPDATA": str(local_app_data), "PATH": ""}
            )

        self.assertEqual(resolved, codex.resolve())


class RegistrationTest(unittest.TestCase):
    def test_registration_uses_absolute_launcher_and_host_boundary(self):
        codex = Path("C:/tools/codex.exe")
        launcher = Path("C:/tools/antiagent-mcp.exe")

        remove_command, add_command, get_command = antiagent_setup.registration_commands(
            codex, launcher
        )

        self.assertEqual(remove_command[-1], antiagent_setup.SERVER_NAME)
        self.assertEqual(add_command[-1], str(launcher))
        self.assertIn(antiagent_setup.BOUNDARY_ENV, add_command)
        self.assertEqual(get_command[-1], antiagent_setup.SERVER_NAME)

    def test_register_replaces_existing_registration_and_verifies_path(self):
        codex = Path("C:/tools/codex.exe")
        launcher = Path("C:/tools/antiagent-mcp.exe")
        completed = [
            mock.Mock(returncode=0, stdout="Removed", stderr=""),
            mock.Mock(returncode=0, stdout="Added", stderr=""),
            mock.Mock(returncode=0, stdout=f"command: {launcher}", stderr=""),
        ]

        with mock.patch("antiagent_setup.subprocess.run", side_effect=completed) as run:
            antiagent_setup.register(codex, launcher)

        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args_list[1].args[0][-1], str(launcher))

    def test_register_stops_when_add_fails(self):
        codex = Path("C:/tools/codex.exe")
        launcher = Path("C:/tools/antiagent-mcp.exe")
        completed = [
            mock.Mock(returncode=0, stdout="Removed", stderr=""),
            mock.Mock(returncode=1, stdout="", stderr="failed"),
        ]

        with mock.patch("antiagent_setup.subprocess.run", side_effect=completed):
            with self.assertRaisesRegex(antiagent_setup.SetupError, "Unable to add"):
                antiagent_setup.register(codex, launcher)


if __name__ == "__main__":
    unittest.main()
