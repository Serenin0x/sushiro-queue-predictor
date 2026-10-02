import base64
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import unittest
from unittest.mock import patch

from sushiwait.auth import describe_authorization


NOW = datetime(2026, 10, 2, 12, 21, 25, tzinfo=timezone.utc)
ISSUED = datetime(2026, 10, 2, 11, 44, 37, tzinfo=timezone.utc)
EXPIRES = datetime(2026, 10, 2, 12, 44, 37, tzinfo=timezone.utc)


def encoded(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def token_with_raw_payload(payload):
    # Synthetic unsigned inspection inputs, never usable production credentials.
    return ".".join((encoded(b'{"alg":"HS256"}'), encoded(payload), encoded(b"synthetic-signature")))


def token_with_claims(claims):
    return token_with_raw_payload(json.dumps(claims, allow_nan=False).encode("utf-8"))


class AuthorizationStatusTests(unittest.TestCase):
    def test_known_declared_hour_and_capture_remaining_are_local_metadata(self):
        token = token_with_claims({"iat": int(ISSUED.timestamp()), "exp": int(EXPIRES.timestamp())})
        status = describe_authorization("Bearer " + token, now=NOW)
        self.assertEqual(status, {
            "configured": True,
            "token_kind": "jwt_like",
            "declared_issued_at": "2026-10-02T11:44:37Z",
            "declared_expires_at": "2026-10-02T12:44:37Z",
            "declared_lifetime_seconds": 3600,
            "remaining_seconds": 1392,
            "expired": False,
            "expiry_source": "unverified_claim",
            "signature_verified": False,
        })

    def test_expiry_is_available_without_issued_claim(self):
        status = describe_authorization(token_with_claims({"exp": int(EXPIRES.timestamp())}), now=NOW)
        self.assertEqual(status["declared_expires_at"], "2026-10-02T12:44:37Z")
        self.assertEqual(status["remaining_seconds"], 1392)
        self.assertFalse(status["expired"])
        self.assertIsNone(status["declared_issued_at"])
        self.assertIsNone(status["declared_lifetime_seconds"])
        self.assertEqual(status["expiry_source"], "unverified_claim")

    def test_issued_without_expiry_does_not_infer_an_hour(self):
        status = describe_authorization(token_with_claims({"iat": int(ISSUED.timestamp())}), now=NOW)
        self.assertEqual(status["declared_issued_at"], "2026-10-02T11:44:37Z")
        for key in ("declared_expires_at", "declared_lifetime_seconds", "remaining_seconds", "expired"):
            self.assertIsNone(status[key])
        self.assertEqual(status["expiry_source"], "unknown")

    def test_reversed_valid_times_are_preserved_without_expiry_conclusion(self):
        status = describe_authorization(token_with_claims({
            "iat": int(EXPIRES.timestamp()), "exp": int(ISSUED.timestamp())}), now=NOW)
        self.assertEqual(status["declared_issued_at"], "2026-10-02T12:44:37Z")
        self.assertEqual(status["declared_expires_at"], "2026-10-02T11:44:37Z")
        for key in ("declared_lifetime_seconds", "remaining_seconds", "expired"):
            self.assertIsNone(status[key])
        self.assertEqual(status["expiry_source"], "invalid_claim")

    def test_invalid_issued_does_not_erase_valid_expiry(self):
        for invalid in (True, False, None, "1760000000", [], {}, 10**50):
            with self.subTest(invalid=invalid):
                status = describe_authorization(token_with_claims({
                    "iat": invalid, "exp": int(EXPIRES.timestamp())}), now=NOW)
                self.assertIsNone(status["declared_issued_at"])
                self.assertIsNone(status["declared_lifetime_seconds"])
                self.assertEqual(status["remaining_seconds"], 1392)
                self.assertFalse(status["expired"])

    def test_invalid_expiry_does_not_erase_valid_issued(self):
        for invalid in (True, False, None, "1760000000", [], {}, 10**50):
            with self.subTest(invalid=invalid):
                status = describe_authorization(token_with_claims({
                    "iat": int(ISSUED.timestamp()), "exp": invalid}), now=NOW)
                self.assertEqual(status["declared_issued_at"], "2026-10-02T11:44:37Z")
                self.assertIsNone(status["declared_expires_at"])
                self.assertIsNone(status["expired"])
                self.assertEqual(status["expiry_source"], "unknown")

    def test_expiry_boundary_and_expired_times_are_clamped(self):
        token = token_with_claims({"exp": int(EXPIRES.timestamp())})
        for current in (EXPIRES, EXPIRES + timedelta(seconds=300)):
            with self.subTest(current=current):
                status = describe_authorization(token, now=current)
                self.assertTrue(status["expired"])
                self.assertEqual(status["remaining_seconds"], 0)
                self.assertFalse(status["signature_verified"])

    def test_fractional_seconds_round_remaining_down(self):
        status = describe_authorization(token_with_claims({
            "iat": NOW.timestamp() - 0.5, "exp": NOW.timestamp() + 1.75}), now=NOW)
        self.assertEqual(status["declared_lifetime_seconds"], 2.25)
        self.assertEqual(status["remaining_seconds"], 1)
        self.assertEqual(status["declared_expires_at"], "2026-10-02T12:21:26.750000Z")
        self.assertFalse(status["expired"])

    def test_equal_times_allow_zero_declared_lifetime(self):
        stamp = int(NOW.timestamp())
        status = describe_authorization(token_with_claims({"iat": stamp, "exp": stamp}), now=NOW)
        self.assertEqual(status["declared_lifetime_seconds"], 0)
        self.assertEqual(status["remaining_seconds"], 0)
        self.assertTrue(status["expired"])

    def test_unusable_clock_keeps_declarations_without_calculating_expiry(self):
        token = token_with_claims({"iat": int(ISSUED.timestamp()), "exp": int(EXPIRES.timestamp())})
        for current in (datetime(2026, 10, 2), "bad-clock", 0):
            with self.subTest(current=current):
                status = describe_authorization(token, now=current)
                self.assertEqual(status["declared_lifetime_seconds"], 3600)
                self.assertEqual(status["declared_expires_at"], "2026-10-02T12:44:37Z")
                self.assertIsNone(status["remaining_seconds"])
                self.assertIsNone(status["expired"])
                self.assertEqual(status["expiry_source"], "unverified_claim")

    def test_missing_and_opaque_credentials_leave_expiry_unknown(self):
        for value, configured, kind in ((None, False, "missing"), ("", False, "missing"),
            ("   ", False, "missing"), ("opaque-synthetic-secret", True, "opaque")):
            with self.subTest(value=value):
                status = describe_authorization(value, now=NOW)
                self.assertEqual(status["configured"], configured)
                self.assertEqual(status["token_kind"], kind)
                self.assertIsNone(status["remaining_seconds"])
                self.assertIsNone(status["expired"])
                self.assertEqual(status["expiry_source"], "unknown")

    def test_malformed_payload_and_transport_injection_are_unknown(self):
        valid = token_with_claims({"exp": int(EXPIRES.timestamp())})
        candidates = ("Bearer", "Bearer ", valid + "\n", valid + "\r\nX: secret",
            "秘密", "Bearer with spaces", "a.b.c", "ab.@@@@.ab", "ab.YQ===.ab",
            token_with_raw_payload(b"not-json"), token_with_raw_payload(b"\xff"),
            token_with_raw_payload(b"[]"), token_with_raw_payload(b"null"))
        for value in candidates:
            with self.subTest(value=value):
                status = describe_authorization(value, now=NOW)
                self.assertIsNone(status["declared_expires_at"])
                self.assertIsNone(status["expired"])
                self.assertFalse(status["signature_verified"])

    def test_duplicate_keys_and_nonfinite_json_reject_entire_payload(self):
        stamp = int(EXPIRES.timestamp())
        for raw in (f'{{"exp":{stamp},"exp":{stamp}}}',
            f'{{"exp":{stamp},"other":{{"key":1,"key":2}}}}',
            f'{{"exp":{stamp},"other":NaN}}',
            f'{{"exp":{stamp},"other":Infinity}}',
            f'{{"exp":{stamp},"other":-Infinity}}',
            f'{{"exp":{stamp},"other":1e999}}'):
            with self.subTest(raw=raw):
                status = describe_authorization(token_with_raw_payload(raw.encode()), now=NOW)
                self.assertIsNone(status["declared_expires_at"])
                self.assertIsNone(status["expired"])
                self.assertEqual(status["expiry_source"], "unknown")

    def test_incorrect_base64_padding_is_not_repaired_into_valid_claims(self):
        parts = token_with_claims({"exp": int(EXPIRES.timestamp())}).split(".")
        required_padding = -len(parts[1]) % 4
        correct = ".".join((parts[0], parts[1] + "=" * required_padding, parts[2]))
        self.assertEqual(describe_authorization(correct, now=NOW)["remaining_seconds"], 1392)
        incorrect_padding = 2 if required_padding == 1 else 1
        malformed = ".".join((parts[0], parts[1] + "=" * incorrect_padding, parts[2]))
        status = describe_authorization(malformed, now=NOW)
        self.assertIsNone(status["declared_expires_at"])
        self.assertIsNone(status["expired"])

    def test_eight_kib_bound_is_checked_before_decoding(self):
        with patch("sushiwait.auth.base64.b64decode", side_effect=AssertionError("must not decode")) as decode:
            status = describe_authorization("a" * 8193, now=NOW)
            decode.assert_not_called()
        self.assertTrue(status["configured"])
        self.assertIsNone(status["expired"])

    def test_inspection_never_prints_or_exposes_other_claims(self):
        marker = "synthetic-sensitive-marker"
        token = token_with_claims({"exp": int(EXPIRES.timestamp()),
            "sub": marker, "aud": marker, "nested": {"value": marker}})
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            status = describe_authorization(token, now=NOW)
        self.assertEqual(output.getvalue(), "")
        for representation in (str(status), repr(status), json.dumps(status)):
            self.assertNotIn(marker, representation)
            self.assertNotIn(token, representation)
        self.assertEqual(set(status), {"configured", "token_kind", "declared_issued_at",
            "declared_expires_at", "declared_lifetime_seconds", "remaining_seconds",
            "expired", "expiry_source", "signature_verified"})


if __name__ == "__main__":
    unittest.main()
