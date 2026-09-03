from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class PackagingTest(unittest.TestCase):
    def test_console_script_is_portable(self):
        with (ROOT / "pyproject.toml").open("rb") as stream:
            project = tomllib.load(stream)

        self.assertEqual(
            project["project"]["scripts"]["antiagent-mcp"],
            "agy_server:main",
        )
        self.assertEqual(
            project["project"]["scripts"]["antiagent-codex-install"],
            "antiagent_setup:main",
        )
        self.assertEqual(
            set(project["tool"]["setuptools"]["py-modules"]),
            {
                "agy_server", "agent_manager", "response_diagnostics",
                "runtime_identity", "antiagent_setup", "antiagent_upgrade",
            },
        )
        self.assertIn("mcp[cli]==2.0.0", project["project"]["dependencies"])

    def test_project_config_does_not_override_user_launcher(self):
        raw = (ROOT / ".codex/config.toml").read_text(encoding="utf-8")
        with (ROOT / ".codex/config.toml").open("rb") as stream:
            config = tomllib.load(stream)
        self.assertNotIn("mcp_servers", config)
        self.assertNotIn("command", config)
        self.assertNotRegex(raw, r"(?i)\b[a-z]:[\\/]")

    def test_host_config_template_has_no_machine_paths_or_fixed_cwd(self):
        relative_path = Path("codex-host-mcp.example.toml")
        raw = (ROOT / relative_path).read_text(encoding="utf-8")
        with (ROOT / relative_path).open("rb") as stream:
            config = tomllib.load(stream)
        executor = config["mcp_servers"]["antigravity_cli_executor"]
        self.assertEqual(executor["command"], "<ABSOLUTE-PIPX-SHIM-PATH>")
        self.assertNotIn("args", executor)
        self.assertNotIn("cwd", executor)
        self.assertNotRegex(raw, r"(?i)\b[a-z]:[\\/]")
        self.assertNotIn("ANTIAGENT_STATE_DIR", executor.get("env", {}))

    def test_user_level_registration_uses_resolved_absolute_launcher(self):
        for relative_path in (
            Path("README.md"),
            Path("HOST_SIDE_DEPLOYMENT.md"),
            Path("Инструкция_ Antigravity CLI OAuth Executor для Codex.md"),
        ):
            with self.subTest(path=str(relative_path)):
                raw = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn("python.exe -m antiagent_setup", raw)
                self.assertNotIn(" -- antiagent-mcp\n", raw)

    def test_docs_do_not_recommend_unsafe_raw_force_upgrade(self):
        for relative_path in (
            Path("README.md"),
            Path("HOST_SIDE_DEPLOYMENT.md"),
            Path("КАК_ПОЛЬЗОВАТЬСЯ.md"),
            Path("Инструкция_ Antigravity CLI OAuth Executor для Codex.md"),
        ):
            with self.subTest(path=str(relative_path)):
                raw = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertNotIn("pipx install --force", raw)


if __name__ == "__main__":
    unittest.main()
