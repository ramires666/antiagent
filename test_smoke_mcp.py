from __future__ import annotations

import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import smoke_mcp


def valid_doctor() -> dict[str, object]:
    return {
        "checks_passed": True,
        "cli_available": True,
        "cli_version": "1.1.24",
        "execution_boundary_declared": True,
        "state_writable": True,
        "workspace_status": "ready",
        "auth_probe": "unsupported",
        "network_probe": "not_run",
        "oauth_ready": "unknown",
        "error_type": None,
    }


class DoctorValidationTest(unittest.TestCase):
    def test_accepts_well_formed_healthy_payload(self):
        payload = valid_doctor()

        self.assertIs(smoke_mcp._validate_doctor(payload), payload)

    def test_rejects_each_failed_required_check(self):
        for field in (
            "checks_passed",
            "cli_available",
            "execution_boundary_declared",
            "state_writable",
        ):
            with self.subTest(field=field):
                payload = valid_doctor()
                payload[field] = False
                with self.assertRaisesRegex(smoke_mcp.SmokeError, field):
                    smoke_mcp._validate_doctor(payload)

    def test_rejects_non_ready_workspace_and_malformed_payload(self):
        payload = valid_doctor()
        payload["workspace_status"] = "workspace_not_git"
        with self.assertRaisesRegex(smoke_mcp.SmokeError, "workspace_status"):
            smoke_mcp._validate_doctor(payload)
        with self.assertRaisesRegex(smoke_mcp.SmokeError, "malformed"):
            smoke_mcp._validate_doctor({"checks_passed": True})


class SchemaValidationTest(unittest.TestCase):
    def test_accepts_current_execution_output_fields(self):
        tool = SimpleNamespace(
            name="antigravity_cli_execute",
            output_schema={
                "properties": {
                    field: {} for field in smoke_mcp.EXPECTED_EXECUTION_OUTPUT_FIELDS
                }
            },
        )

        smoke_mcp._validate_loaded_schema([tool])

    def test_rejects_stale_execution_output_fields_with_diagnostic(self):
        fields = smoke_mcp.EXPECTED_EXECUTION_OUTPUT_FIELDS - {"feedback"}
        tool = SimpleNamespace(
            name="antigravity_cli_execute",
            output_schema={"properties": {field: {} for field in fields}},
        )

        with self.assertRaisesRegex(smoke_mcp.SmokeError, "stale.*feedback"):
            smoke_mcp._validate_loaded_schema([tool])


class MainTest(unittest.TestCase):
    def test_health_failure_returns_nonzero_without_traceback(self):
        error = smoke_mcp.SmokeError("doctor check failed: state_writable")
        stderr = io.StringIO()
        with (
            mock.patch("smoke_mcp.resolve_mcp_launcher", return_value=Path("launcher")),
            mock.patch("smoke_mcp.probe", side_effect=error),
            mock.patch("sys.stderr", stderr),
        ):
            result = smoke_mcp.main(["."])

        self.assertEqual(result, 2)
        self.assertIn("state_writable", stderr.getvalue())

    def test_unexpected_failure_is_redacted(self):
        stderr = io.StringIO()
        with (
            mock.patch("smoke_mcp.resolve_mcp_launcher", return_value=Path("launcher")),
            mock.patch(
                "smoke_mcp.probe",
                side_effect=RuntimeError("UNEXPECTED_DIAGNOSTIC_SECRET"),
            ),
            mock.patch("sys.stderr", stderr),
        ):
            result = smoke_mcp.main(["."])

        self.assertEqual(result, 2)
        self.assertIn("failed unexpectedly", stderr.getvalue())
        self.assertNotIn("UNEXPECTED_DIAGNOSTIC_SECRET", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
