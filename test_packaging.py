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
            set(project["tool"]["setuptools"]["py-modules"]),
            {"agy_server", "agent_manager"},
        )
        self.assertIn("mcp[cli]==2.0.0", project["project"]["dependencies"])

    def test_distributed_mcp_configs_have_no_machine_paths_or_fixed_cwd(self):
        for relative_path in (
            Path(".codex/config.toml"),
            Path("codex-host-mcp.example.toml"),
        ):
            with self.subTest(path=str(relative_path)):
                raw = (ROOT / relative_path).read_text(encoding="utf-8")
                with (ROOT / relative_path).open("rb") as stream:
                    config = tomllib.load(stream)
                executor = config["mcp_servers"]["antigravity_cli_executor"]
                expected = (
                    "antiagent-mcp"
                    if relative_path == Path(".codex/config.toml")
                    else "<ABSOLUTE-PIPX-SHIM-PATH>"
                )
                self.assertEqual(executor["command"], expected)
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
                self.assertIn("Get-Command antiagent-mcp -CommandType Application", raw)
                self.assertNotIn(" -- antiagent-mcp\n", raw)


if __name__ == "__main__":
    unittest.main()
