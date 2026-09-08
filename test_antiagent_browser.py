from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import antiagent_browser as browser


class BrowserArgvTest(unittest.TestCase):
    def _files(self):
        directory = Path(tempfile.mkdtemp())
        node = directory / ("node.exe" if os.name == "nt" else "node")
        script = directory / "chrome-devtools-mcp.js"
        node.touch()
        script.touch()
        return node, script

    def test_browser_argv_rejects_disabled_and_invalid_modes(self):
        node, script = self._files()
        for mode in ("disabled", "bogus", None):
            with self.subTest(mode=mode), self.assertRaises(browser.BrowserError):
                browser.browser_argv(mode, str(node), str(script))

    def test_browser_argv_uses_mode_specific_flag_and_validates_native_node(self):
        node, script = self._files()
        isolated = browser.browser_argv("isolated", str(node), str(script))
        session = browser.browser_argv("user_session", str(node), str(script))
        self.assertEqual(isolated[-1], "--isolated")
        self.assertEqual(session[-1], "--autoConnect")
        self.assertEqual(isolated[:2], [str(node.resolve()), str(script.resolve())])

        shell_node = node.with_suffix(".cmd")
        shell_node.touch()
        with self.assertRaisesRegex(browser.BrowserError, "native executable"):
            browser.browser_argv("isolated", str(shell_node), str(script))

        with self.assertRaises(browser.BrowserError):
            browser.browser_argv("isolated", "relative-node", str(script))
        with self.assertRaises(browser.BrowserError):
            browser.browser_argv("isolated", str(node), str(script.with_name("missing.js")))


class BrowserMainTest(unittest.TestCase):
    def _files(self):
        directory = Path(tempfile.mkdtemp())
        node = directory / ("node.exe" if os.name == "nt" else "node")
        script = directory / "chrome-devtools-mcp.js"
        node.touch()
        script.touch()
        return node, script

    def test_disabled_mode_runs_empty_server_without_browser_preflight(self):
        server = MagicMock()
        with patch.dict(os.environ, {browser.MODE_ENV: "disabled"}, clear=True), patch(
            "antiagent_browser.MCPServer", return_value=server
        ) as constructor, patch("antiagent_browser.browser_argv") as argv:
            self.assertEqual(browser.main([]), 0)
        constructor.assert_called_once_with(browser.SERVER_NAME)
        server.run.assert_called_once_with()
        argv.assert_not_called()

    def test_invalid_mode_is_rejected(self):
        with patch.dict(os.environ, {browser.MODE_ENV: "invalid"}, clear=True), patch(
            "antiagent_browser.MCPServer"
        ) as server:
            self.assertEqual(browser.main([]), 1)
        server.assert_not_called()

    def test_registration_config_isolated_python_module_command(self):
        node, script = self._files()
        config = browser.registration_config(str(node), str(script))
        command = config["mcpServers"][browser.SERVER_NAME]
        self.assertEqual(command["command"], str(Path(os.sys.executable).resolve()))
        self.assertEqual(command["args"][:2], ["-I", "-m"])
        self.assertEqual(command["args"][2], "antiagent_browser")
        self.assertNotIn("--isolated", command["args"])
        self.assertNotIn(browser.MODE_ENV, json.dumps(config))

    def test_isolated_does_not_lock_but_user_session_does(self):
        node, script = self._files()
        with patch.dict(os.environ, {browser.MODE_ENV: "isolated"}, clear=True), patch(
            "antiagent_browser.browser_argv", return_value=["node", "script"]
        ), patch("antiagent_browser._run_browser", return_value=3), patch(
            "antiagent_browser.user_session_lock"
        ) as lock:
            self.assertEqual(browser.main(["--node", str(node), "--script", str(script)]), 3)
        lock.assert_not_called()

        @contextmanager
        def session():
            yield

        with patch.dict(os.environ, {browser.MODE_ENV: "user_session"}, clear=True), patch(
            "antiagent_browser.browser_argv", return_value=["node", "script"]
        ), patch("antiagent_browser._run_browser", return_value=4), patch(
            "antiagent_browser.user_session_lock", side_effect=session
        ) as lock:
            self.assertEqual(browser.main(["--node", str(node), "--script", str(script)]), 4)
        lock.assert_called_once_with()


class BrowserLockTest(unittest.TestCase):
    def test_user_session_lock_is_mutually_exclusive_and_releases(self):
        with tempfile.TemporaryDirectory() as home, patch.object(Path, "home", return_value=Path(home)):
            first = browser.user_session_lock()
            first.__enter__()
            try:
                with self.assertRaisesRegex(browser.BrowserError, "busy"):
                    with browser.user_session_lock():
                        pass
            finally:
                first.__exit__(None, None, None)
            with browser.user_session_lock():
                pass


class BrowserPolicyTest(unittest.TestCase):
    def test_browser_environment_filters_debug_values_and_sets_safe_defaults(self):
        inherited = {
            "DEBUG": "secret",
            "debug_extra": "secret",
            "NODE_OPTIONS": "--inspect",
            "Node_Debug": "verbose",
            "SAFE_SETTING": "kept",
        }
        with patch.dict(os.environ, inherited, clear=True):
            env = browser._browser_environment()
        self.assertEqual(env["SAFE_SETTING"], "kept")
        self.assertNotIn("DEBUG", env)
        self.assertNotIn("debug_extra", env)
        self.assertNotIn("NODE_OPTIONS", env)
        self.assertNotIn("Node_Debug", env)
        self.assertEqual(env["CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS"], "1")
        self.assertEqual(env["CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS"], "1")

    def test_browser_policy_blocks_evaluation_network_and_unsafe_navigation(self):
        self.assertTrue(browser._allowed_call("navigate_page", {"url": "https://example.test"}))
        self.assertTrue(browser._allowed_call("click", {"uid": "1"}))
        for name, args in (
            ("evaluate_script", {"function": "() => document.cookie"}),
            ("get_network_request", {}),
            ("navigate_page", {"url": "file:///secret.txt"}),
            ("navigate_page", {"url": "javascript:alert(1)"}),
            ("navigate_page", {"url": "https://example.test", "initScript": "evil"}),
        ):
            with self.subTest(name=name, args=args):
                self.assertFalse(browser._allowed_call(name, args))


if __name__ == "__main__":
    unittest.main()
