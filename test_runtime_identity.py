from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

import runtime_identity as runtime


def identity(**changes) -> runtime.RuntimeIdentity:
    base = runtime.RuntimeIdentity(
        package_name="antiagent-mcp",
        package_version="0.3.0",
        schema_revision="7",
        mcp_process_pid=123,
        mcp_process_started_at="2026-09-03T00:00:00.000Z",
        cli_state="ready",
        cli_executable=str(Path.cwd() / "agy.exe"),
        cli_binary_identity=runtime.BinaryIdentity(1, 2, 3, 4, 5),
        cli_version="1.1.25",
    )
    return replace(base, **changes)


class RuntimeIdentityTest(unittest.TestCase):
    def tearDown(self):
        runtime._reset_process_runtime_guard_for_tests()

    def test_identity_models_are_immutable(self):
        captured = identity()
        with self.assertRaises(FrozenInstanceError):
            captured.cli_version = "changed"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            captured.cli_binary_identity.size_bytes = 9  # type: ignore[union-attr,misc]

    def test_capture_resolves_absolute_path_and_all_runtime_fields(self):
        with tempfile.TemporaryDirectory(prefix="runtime-identity-") as directory:
            executable = Path(directory) / "agy.exe"
            executable.write_bytes(b"binary")
            probes = runtime.RuntimeProbes(
                resolve_cli=lambda: executable,
                probe_cli_version=lambda path: " 1.1.25 ",
                probe_package_version=lambda: " 0.3.0 ",
                probe_mcp_pid=lambda: 456,
                probe_mcp_started_at=lambda: "2026-09-03T01:02:03.000Z",
            )
            captured = runtime.capture_runtime_identity(
                probes, schema_revision="8"
            )
        self.assertEqual(captured.cli_state, "ready")
        self.assertEqual(captured.cli_executable, str(executable.resolve()))
        self.assertEqual(captured.cli_version, "1.1.25")
        self.assertEqual(captured.package_version, "0.3.0")
        self.assertEqual(captured.schema_revision, "8")
        self.assertEqual(captured.mcp_process_pid, 456)
        self.assertEqual(
            captured.mcp_process_started_at, "2026-09-03T01:02:03.000Z"
        )
        self.assertEqual(captured.cli_binary_identity.size_bytes, 6)

    def test_capture_does_not_open_or_hash_executable_contents(self):
        with tempfile.TemporaryDirectory(prefix="runtime-stat-only-") as directory:
            executable = Path(directory) / "agy.exe"
            executable.write_bytes(b"x" * 1024)
            probes = runtime.RuntimeProbes(
                resolve_cli=lambda: executable,
                probe_cli_version=lambda path: "1.1.25",
                probe_package_version=lambda: "0.3.0",
            )
            with patch("builtins.open", side_effect=AssertionError("unexpected read")):
                captured = runtime.capture_runtime_identity(probes)
        self.assertEqual(captured.cli_state, "ready")

    def test_stable_identity_has_no_drift(self):
        before = identity()
        snapshot = runtime.compare_runtime_identities(before, before)
        self.assertFalse(snapshot.stale)
        self.assertIsNone(snapshot.error_type)
        self.assertIsNone(snapshot.safe_reason)

    def test_each_non_cli_drift_has_a_specific_reason(self):
        cases = {
            "mcp_process_drift": replace(identity(), mcp_process_pid=124),
            "package_version_drift": replace(identity(), package_version="0.4.0"),
            "schema_revision_drift": replace(identity(), schema_revision="8"),
        }
        for reason, observed in cases.items():
            with self.subTest(reason=reason):
                snapshot = runtime.compare_runtime_identities(identity(), observed)
                self.assertEqual(snapshot.drift_reasons, (reason,))
                self.assertEqual(snapshot.error_type, "stale_runtime_snapshot")

    def test_each_cli_drift_has_a_specific_reason(self):
        base = identity()
        cases = {
            "cli_availability_drift": replace(
                base,
                cli_state="missing",
                cli_executable=None,
                cli_binary_identity=None,
                cli_version=None,
            ),
            "cli_path_drift": replace(
                base, cli_executable=str(Path.cwd() / "other" / "agy.exe")
            ),
            "cli_binary_drift": replace(
                base, cli_binary_identity=runtime.BinaryIdentity(1, 9, 3, 4, 5)
            ),
            "cli_version_drift": replace(base, cli_version="1.1.26"),
        }
        for reason, observed in cases.items():
            with self.subTest(reason=reason):
                snapshot = runtime.compare_runtime_identities(base, observed)
                self.assertEqual(snapshot.drift_reasons, (reason,))

    def test_missing_file_is_an_availability_drift(self):
        with tempfile.TemporaryDirectory(prefix="runtime-missing-") as directory:
            executable = Path(directory) / "agy.exe"
            executable.write_bytes(b"first")
            probes = runtime.RuntimeProbes(
                resolve_cli=lambda: executable,
                probe_cli_version=lambda path: "1.1.25",
                probe_package_version=lambda: "0.3.0",
            )
            before = runtime.capture_runtime_identity(probes)
            executable.unlink()
            after = runtime.capture_runtime_identity(probes)
        self.assertEqual(after.cli_state, "missing")
        self.assertEqual(
            runtime.compare_runtime_identities(before, after).drift_reasons,
            ("cli_availability_drift",),
        )

    def test_replaced_file_at_same_path_is_binary_drift(self):
        with tempfile.TemporaryDirectory(prefix="runtime-replaced-") as directory:
            executable = Path(directory) / "agy.exe"
            executable.write_bytes(b"first")
            probes = runtime.RuntimeProbes(
                resolve_cli=lambda: executable,
                probe_cli_version=lambda path: "1.1.25",
                probe_package_version=lambda: "0.3.0",
            )
            before = runtime.capture_runtime_identity(probes)
            executable.write_bytes(b"replacement-is-larger")
            after = runtime.capture_runtime_identity(probes)
        self.assertEqual(
            runtime.compare_runtime_identities(before, after).drift_reasons,
            ("cli_binary_drift",),
        )

    def test_probe_failures_are_safe_and_do_not_leak_exception_text(self):
        secret = "RUNTIME_SECRET_VALUE"

        def fail():
            raise OSError(secret)

        captured = runtime.capture_runtime_identity(
            runtime.RuntimeProbes(
                resolve_cli=fail,
                probe_cli_version=lambda path: secret,
                probe_package_version=fail,
            )
        )
        self.assertEqual(captured.cli_state, "unreadable")
        self.assertIsNone(captured.package_version)
        diagnostic = runtime.compare_runtime_identities(identity(), captured)
        self.assertNotIn(secret, diagnostic.safe_reason or "")
        self.assertNotIn(str(Path.cwd()), diagnostic.safe_reason or "")

    def test_cli_version_probe_runs_only_for_readable_regular_file(self):
        calls = []
        probes = runtime.RuntimeProbes(
            resolve_cli=lambda: Path("missing-antiagent-cli"),
            probe_cli_version=lambda path: calls.append(path) or "1.1.25",
            probe_package_version=lambda: "0.3.0",
        )
        captured = runtime.capture_runtime_identity(probes)
        self.assertEqual(captured.cli_state, "missing")
        self.assertEqual(calls, [])

    def test_guard_keeps_first_identity_and_detects_later_drift(self):
        guard = runtime.RuntimeSnapshotGuard()
        first = guard.observe_identity(identity())
        later = guard.observe_identity(identity(cli_version="1.1.26"))
        self.assertFalse(first.stale)
        self.assertEqual(later.drift_reasons, ("cli_version_drift",))
        self.assertEqual(guard.baseline(), identity())

    def test_guard_is_thread_safe_under_concurrent_observation(self):
        guard = runtime.RuntimeSnapshotGuard()
        captured = identity()
        calls = 0
        calls_lock = threading.Lock()

        def factory():
            nonlocal calls
            time.sleep(0.001)
            with calls_lock:
                calls += 1
            return captured

        with ThreadPoolExecutor(max_workers=16) as executor:
            snapshots = list(executor.map(lambda _: guard.observe(factory), range(64)))
        self.assertEqual(calls, 64)
        self.assertTrue(all(not snapshot.stale for snapshot in snapshots))
        self.assertEqual(guard.baseline(), captured)

    def test_process_guard_reset_is_private_and_test_only(self):
        first = runtime.guard_process_runtime(lambda: identity())
        self.assertFalse(first.stale)
        self.assertEqual(runtime.process_runtime_baseline(), identity())
        runtime._reset_process_runtime_guard_for_tests()
        self.assertIsNone(runtime.process_runtime_baseline())
        second_identity = identity(cli_version="2.0.0")
        second = runtime.guard_process_runtime(lambda: second_identity)
        self.assertFalse(second.stale)
        self.assertEqual(runtime.process_runtime_baseline(), second_identity)

    def test_invalid_schema_and_package_names_fail_without_probing(self):
        called = []
        probes = runtime.RuntimeProbes(
            resolve_cli=lambda: called.append(True),
            probe_cli_version=lambda path: "1.1.25",
        )
        for kwargs in ({"schema_revision": ""}, {"package_name": " "}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                runtime.capture_runtime_identity(probes, **kwargs)
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
