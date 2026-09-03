from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError

import response_diagnostics as diagnostics


class ResponseDiagnosticsTests(unittest.TestCase):
    def test_evidence_is_immutable_bounded_and_json_safe(self):
        evidence = diagnostics.ResponseDiagnostics(
            output_format="stream-json",
            final_event_seen=True,
            last_safe_event_type="TOKEN=event-secret",
            response_id="unsafe response/id TOKEN=secret",
            content_block_count=diagnostics.MAX_DIAGNOSTIC_COUNT + 99,
            malformed_event_count=diagnostics.MAX_DIAGNOSTIC_COUNT + 99,
        )
        self.assertEqual(evidence.last_safe_event_type, "unknown")
        self.assertIsNone(evidence.response_id)
        self.assertEqual(
            evidence.content_block_count, diagnostics.MAX_DIAGNOSTIC_COUNT
        )
        self.assertEqual(
            evidence.malformed_event_count, diagnostics.MAX_DIAGNOSTIC_COUNT
        )
        encoded = json.dumps(evidence.as_dict())
        self.assertNotIn("event-secret", encoded)
        self.assertNotIn("TOKEN=secret", encoded)
        with self.assertRaises(FrozenInstanceError):
            evidence.response_id = "replacement"  # type: ignore[misc]

    def test_evidence_rejects_invalid_scalars_and_accepts_safe_id(self):
        evidence = diagnostics.ResponseDiagnostics(
            output_format="xml",  # type: ignore[arg-type]
            final_event_seen=1,  # type: ignore[arg-type]
            last_safe_event_type=None,
            response_id="resp_01-safe:part",
            content_block_count=True,
            malformed_event_count=-1,
        )
        self.assertIsNone(evidence.output_format)
        self.assertIsNone(evidence.final_event_seen)
        self.assertEqual(evidence.response_id, "resp_01-safe:part")
        self.assertIsNone(evidence.content_block_count)
        self.assertEqual(evidence.malformed_event_count, 0)

    def classify(self, payload, evidence=None, **kwargs):
        return diagnostics.classify_content_result(
            payload,
            evidence or diagnostics.ResponseDiagnostics(output_format="json"),
            **kwargs,
        )

    def test_all_five_codes_are_mutually_exclusive(self):
        cases = (
            (
                "empty_model_response",
                {"response": " \r\n", "usage": {"output_tokens": 0}},
                diagnostics.ResponseDiagnostics(output_format="json"),
                {},
            ),
            (
                "stream_closed_before_final",
                None,
                diagnostics.ResponseDiagnostics(
                    output_format="stream-json",
                    final_event_seen=False,
                    last_safe_event_type="step_update",
                ),
                {},
            ),
            (
                "content_parse_failed",
                {"response": 7},
                diagnostics.ResponseDiagnostics(
                    output_format="stream-json", final_event_seen=True
                ),
                {},
            ),
            (
                "content_filtered",
                {"response": "", "finish_reason": "SAFETY"},
                diagnostics.ResponseDiagnostics(output_format="json"),
                {},
            ),
            (
                "final_block_missing",
                {"usage": {"output_tokens": 0}},
                diagnostics.ResponseDiagnostics(output_format="json"),
                {},
            ),
        )
        observed = []
        for expected, payload, evidence, kwargs in cases:
            with self.subTest(expected=expected):
                result = self.classify(payload, evidence, **kwargs)
                self.assertIsNotNone(result)
                assert result is not None
                self.assertEqual(result.error_type, expected)
                observed.append(result.error_type)
        self.assertEqual(set(observed), set(diagnostics.CONTENT_ERROR_CODES))

    def test_output_tokens_distinguish_empty_from_missing_final_block(self):
        for tokens, expected in (
            (0, "empty_model_response"),
            (0.0, "empty_model_response"),
            (1, "final_block_missing"),
            (42.5, "final_block_missing"),
        ):
            with self.subTest(tokens=tokens):
                result = self.classify({
                    "response": "",
                    "usage": {"output_tokens": tokens},
                })
                assert result is not None
                self.assertEqual(result.error_type, expected)

        explicit = self.classify(
            {"response": "", "usage": {"output_tokens": 0}}, output_tokens=9
        )
        assert explicit is not None
        self.assertEqual(explicit.error_type, "final_block_missing")

    def test_invalid_output_token_values_do_not_claim_generated_content(self):
        for tokens in (True, -1, float("nan"), float("inf"), "9"):
            with self.subTest(tokens=tokens):
                result = self.classify({
                    "response": "", "usage": {"output_tokens": tokens}
                })
                assert result is not None
                self.assertEqual(result.error_type, "empty_model_response")

    def test_stream_terminal_rules_and_valid_final_precedence(self):
        malformed_eof = self.classify(
            None,
            diagnostics.ResponseDiagnostics(
                output_format="stream-json",
                final_event_seen=False,
                last_safe_event_type="init",
                malformed_event_count=1,
            ),
        )
        assert malformed_eof is not None
        self.assertEqual(malformed_eof.error_type, "content_parse_failed")

        explicit_parse = self.classify(
            {"response": ""},
            diagnostics.ResponseDiagnostics(
                output_format="stream-json", final_event_seen=True
            ),
            parse_failed=True,
        )
        assert explicit_parse is not None
        self.assertEqual(explicit_parse.error_type, "content_parse_failed")

        # Malformed intermediate noise does not override a valid final result.
        valid_final = self.classify(
            {"response": "usable"},
            diagnostics.ResponseDiagnostics(
                output_format="stream-json",
                final_event_seen=True,
                last_safe_event_type="result",
                malformed_event_count=3,
            ),
        )
        self.assertIsNone(valid_final)

    def test_filter_requires_no_deliverable_content(self):
        reasons = ("content_filter", "content-filtered", "blocked", "safety")
        for reason in reasons:
            with self.subTest(reason=reason):
                result = self.classify({"response": "", "finishReason": reason})
                assert result is not None
                self.assertEqual(result.error_type, "content_filtered")
        explicit = self.classify({"response": ""}, content_filtered=True)
        assert explicit is not None
        self.assertEqual(explicit.error_type, "content_filtered")
        self.assertIsNone(self.classify(
            {"response": "safe partial content", "content_filtered": True}
        ))
        non_boolean = self.classify(
            {"response": "", "usage": {"output_tokens": 0}},
            content_filtered="true",  # type: ignore[arg-type]
        )
        assert non_boolean is not None
        self.assertEqual(non_boolean.error_type, "empty_model_response")

    def test_retry_hint_is_limited_to_plan_mode(self):
        for code in diagnostics.CONTENT_ERROR_CODES:
            with self.subTest(code=code):
                expected = code in (
                    "stream_closed_before_final", "final_block_missing"
                )
                self.assertEqual(
                    diagnostics.is_retryable_content_error(code, "plan"), expected
                )
                self.assertFalse(
                    diagnostics.is_retryable_content_error(code, "accept-edits")
                )


class SafeReviewSuffixTests(unittest.TestCase):
    def test_strips_ansi_controls_and_bounds_suffix(self):
        result = diagnostics.safe_review_suffix(
            "prefix\x1b[31mRED\x1b[0m\x00\r\nfinished", max_chars=12
        )
        self.assertEqual(result, "RED finished")
        self.assertNotIn("\x1b", result or "")
        self.assertNotIn("\x00", result or "")
        self.assertLessEqual(len(result or ""), 12)

    def test_redacts_private_keys_bearer_jwt_assignments_and_entropy(self):
        private_material = "MIIEv" + "A1b2C3d4" * 8
        bearer = "bearer-secret-A1b2C3d4E5f6G7h8"
        jwt = "abcdefgh.ABCDEFGHIJKL012345.zyxwvuts98765432"
        password = "PasswordValue-9482"
        high_entropy = "QWxhZGRpbjpPcGVuU2VzYW1lMTIzNDU2Nzg5MFhZWg=="
        source = (
            "review "
            "-----BEGIN " + "PRIVATE KEY-----\n"
            f"{private_material}\n"
            "-----END " + "PRIVATE KEY----- "
            f"Authorization: Bearer {bearer} token={jwt} "
            f"password='{password}' id={high_entropy} done"
        )
        result = diagnostics.safe_review_suffix(source)
        self.assertIsNotNone(result)
        serialized = result or ""
        for secret in (private_material, bearer, jwt, password, high_entropy):
            self.assertNotIn(secret, serialized)
        self.assertIn("REDACTED", serialized)
        self.assertTrue(serialized.endswith("done"))

    def test_redacts_known_secret_prefixes_and_prefixed_assignments(self):
        secrets = (
            "sk" + "-abcdefghijklmnop123456",
            "ghp" + "_abcdefghijklmnop123456",
            "AI" + "zaSyAabcdefghijklmnopqrstuvwx",
            "AK" + "IAABCDEFGHIJKLMNOP",
            "xo" + "xb-1234567890-abcdefghijklmnop",
            "database-password-value",
        )
        source = " ".join((
            *secrets[:-1],
            f"DB_PASSWORD={secrets[-1]}",
            "complete",
        ))
        result = diagnostics.safe_review_suffix(source)
        self.assertIsNotNone(result)
        for secret in secrets:
            self.assertNotIn(secret, result or "")
        self.assertTrue((result or "").endswith("complete"))

    def test_fail_closed_for_uncertain_dangerous_content(self):
        unsafe = (
            "-----BEGIN " + "PRIVATE KEY----- incomplete",
            '{"tool_args":{"path":"private"}}',
            '{"prompt":"raw user payload"}',
            '{"user_prompt":"raw secret"}',
            '{"system":"raw secret"}',
            '{"messages":[{"role":"user","content":"raw secret"}]}',
            "<thinking>hidden chain</thinking>",
            "<user>raw secret</user>",
            "<system>raw secret</system>",
            "<assistant>structured envelope</assistant>",
            "<tool>raw result</tool>",
            "role: user raw request",
            "https://alice:secret@example.test/path",
            "X" * 2050,
            "safe " * (diagnostics.MAX_REVIEW_SCAN_CHARS // 5 + 1),
        )
        for value in unsafe:
            with self.subTest(value=value[:30]):
                self.assertIsNone(diagnostics.safe_review_suffix(value))

    def test_rejects_invalid_input_and_does_not_leak_in_repr(self):
        for value in (None, b"bytes", ""):
            with self.subTest(value=value):
                self.assertIsNone(diagnostics.safe_review_suffix(value))
        self.assertIsNone(diagnostics.safe_review_suffix("ok", max_chars=0))
        self.assertIsNone(diagnostics.safe_review_suffix(
            "ok", max_chars=diagnostics.MAX_REVIEW_SUFFIX_CHARS + 1
        ))


if __name__ == "__main__":
    unittest.main()
