"""Synthetic private-context inspection, with every external IO route guarded."""

import base64
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sushiwait.cli import main
from sushiwait.credentials import read_credentials_file


NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
MARKER = "synthetic-private-status-marker"


def token(claims):
    def encode(value):
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
    return ".".join((encode(b'{"alg":"HS256"}'),
        encode(json.dumps({"sub": MARKER, **claims}).encode()), encode(b"synthetic-signature")))


def bundle(authorization=None, revision=1, profile="miniapp_gateway"):
    value = {"schema_version": 1, "api_profile": profile, "revision": revision,
             "authorization": authorization if authorization is not None else token({
                 "iat": NOW.timestamp() - 60, "exp": NOW.timestamp() + 3600})}
    if profile == "miniapp_gateway":
        value.update({"app_client": MARKER, "app_code": MARKER,
            "user_agent": "Synthetic " + MARKER, "referer": "https://synthetic.invalid/" + MARKER,
            "content_type": "application/json"})
    return value


@unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "POSIX private-file input")
class AuthStatusFileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.parent = Path(tmp.name).resolve()
        self.parent.chmod(0o700)
        self.path = self.parent / "context.json"
        self.write(bundle())

    def write(self, value):
        replacement = self.parent / "replacement.json"
        replacement.write_text(json.dumps(value), encoding="utf-8")
        replacement.chmod(0o600)
        os.replace(replacement, self.path)

    def run_cli(self, *, profile="miniapp_gateway", path=None, clock=NOW):
        output = io.StringIO()
        errors = io.StringIO()
        args = ["auth-status", "--api-profile", profile, "--credentials-file", str(path or self.path)]
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(errors))
            env = stack.enter_context(patch("sushiwait.cli.os.environ.get", return_value=None))
            stack.enter_context(patch("sushiwait.cli._utc_clock", return_value=clock))
            for target in ("sushiwait.cli.client_for", "sushiwait.cli.SushiroClient",
                "sushiwait.client.SushiroClient._default_opener", "ssl.create_default_context",
                "urllib.request.build_opener", "socket.create_connection", "sushiwait.cli.SnapshotStore"):
                stack.enter_context(patch(target, side_effect=AssertionError("must stay local")))
            code = main(args)
            self.assertFalse(any(c.args[0].startswith("SUSHIWAIT_") for c in env.call_args_list))
        self.assertEqual(errors.getvalue(), "")
        rows = output.getvalue().splitlines()
        self.assertEqual(len(rows), 1)
        self.assertNotIn(MARKER, output.getvalue())
        self.assertNotIn(str(self.parent), output.getvalue())
        row = json.loads(rows[0])
        self.assertFalse(row["network_performed"])
        self.assertEqual(row["server_acceptance"], "unverified")
        self.assertEqual(row["credential_source"], "private_file")
        return code, row

    def test_complete_file_reports_revision_and_declared_guard_without_transport(self):
        original = self.path.read_bytes()
        with patch("sushiwait.cli.read_credentials_file", wraps=read_credentials_file) as read:
            code, row = self.run_cli()
            read.assert_called_once_with(str(self.path), api_profile="miniapp_gateway")
        self.assertEqual(code, 0)
        self.assertTrue(row["ok"])
        self.assertEqual(row["credential_revision"], 1)
        self.assertEqual(row["checked_at"], "2026-10-03T12:00:00.000Z")
        self.assertEqual(row["remaining_seconds"], 3600)
        self.assertEqual(row["authorization_guard"], {
            "state": "no_declared_stop", "stop_reason": None, "margin_seconds": 30})
        self.assertFalse(row["signature_verified"])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual({p.name for p in self.parent.iterdir()}, {"context.json"})

    def test_file_time_overrides_all_environment_credentials(self):
        self.write(bundle(authorization=token({"exp": NOW.timestamp() - 1})))
        code, row = self.run_cli()
        self.assertEqual(code, 0)
        self.assertTrue(row["expired"])
        self.assertEqual(row["authorization_guard"]["stop_reason"], "auth_declared_expired")

    def test_zero_thirty_and_thirty_one_second_boundaries_match_query_guard(self):
        for remaining, reason in ((31, None), (30, "auth_expiring"), (0, "auth_declared_expired"),
                                  (-1, "auth_declared_expired")):
            with self.subTest(remaining=remaining):
                self.write(bundle(authorization=token({"exp": NOW.timestamp() + remaining})))
                code, row = self.run_cli()
                self.assertEqual(code, 0)  # Inspection success is not request eligibility.
                self.assertEqual(row["remaining_seconds"], max(0, remaining))
                self.assertEqual(row["authorization_guard"]["stop_reason"], reason)
                self.assertEqual(row["authorization_guard"]["state"],
                    "stop" if reason else "no_declared_stop")

    def test_fractional_remaining_is_conservative_at_protection_boundary(self):
        self.write(bundle(authorization=token({"exp": NOW.timestamp() + 30.9})))
        code, row = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(row["remaining_seconds"], 30)
        self.assertEqual(row["authorization_guard"]["stop_reason"], "auth_expiring")

    def test_opaque_and_missing_expiry_never_infer_validity_or_lifetime(self):
        for auth in (MARKER, token({"iat": NOW.timestamp()}), token({"exp": True}), "a.b.c"):
            with self.subTest(authorization_kind="synthetic"):
                self.write(bundle(authorization=auth))
                code, row = self.run_cli()
                self.assertEqual(code, 0)
                self.assertIsNone(row["remaining_seconds"])
                self.assertIsNone(row["expired"])
                self.assertEqual(row["authorization_guard"]["state"], "unknown")
                self.assertIsNone(row["authorization_guard"]["stop_reason"])

    def test_reversed_declarations_stop_but_inspection_still_succeeds(self):
        self.write(bundle(authorization=token({"iat": NOW.timestamp() + 1, "exp": NOW.timestamp()})))
        code, row = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(row["authorization_guard"]["stop_reason"], "auth_claims_invalid")
        self.assertIsNone(row["remaining_seconds"])

    def test_new_invocation_reads_atomic_revision_replacement(self):
        first_code, first = self.run_cli()
        self.write(bundle(authorization=token({"exp": NOW.timestamp() + 7200}), revision=2))
        next_code, next_row = self.run_cli()
        self.assertEqual((first_code, next_code), (0, 0))
        self.assertEqual((first["credential_revision"], next_row["credential_revision"]), (1, 2))
        self.assertEqual(next_row["remaining_seconds"], 7200)

    def test_clock_is_read_after_complete_file_validation(self):
        self.write(bundle(authorization=token({"exp": NOW.timestamp() + 60})))
        events = []
        def read(*args, **kwargs):
            events.append("file")
            return read_credentials_file(*args, **kwargs)
        def clock():
            self.assertEqual(events, ["file"])
            events.append("clock")
            return NOW + timedelta(seconds=30)
        output = io.StringIO()
        with patch("sushiwait.cli.read_credentials_file", side_effect=read), \
             patch("sushiwait.cli._utc_clock", side_effect=clock), \
             patch("sushiwait.cli.SushiroClient", side_effect=AssertionError("no transport")), \
             contextlib.redirect_stdout(output):
            code = main(["auth-status", "--api-profile", "miniapp_gateway",
                         "--credentials-file", str(self.path)])
        self.assertEqual(code, 0)
        self.assertEqual(events, ["file", "clock"])
        row = json.loads(output.getvalue())
        self.assertEqual(row["authorization_guard"]["stop_reason"], "auth_expiring")

    def test_missing_file_is_safe_error_without_environment_fallback(self):
        code, row = self.run_cli(path=self.parent / (MARKER + ".json"))
        self.assertEqual(code, 1)
        self.assertFalse(row["ok"])
        self.assertEqual(row["error_code"], "credentials_file_unavailable")
        self.assertNotIn("credential_revision", row)

    def test_unsafe_permissions_and_symlink_are_rejected_without_transport(self):
        self.path.chmod(0o644)
        code, row = self.run_cli()
        self.assertEqual((code, row["error_code"]), (1, "credentials_file_unsafe"))
        self.path.chmod(0o600)
        link = self.parent / "link.json"
        link.symlink_to(self.path)
        code, row = self.run_cli(path=link)
        self.assertEqual((code, row["error_code"]), (1, "credentials_file_unsafe"))

    def test_invalid_json_and_headers_never_echo_parsing_context(self):
        self.path.write_text('{"authorization":"' + MARKER, encoding="utf-8")
        code, row = self.run_cli()
        self.assertEqual((code, row["error_code"]), (1, "credentials_file_invalid_json"))
        value = bundle()
        value["user_agent"] = MARKER + "\n"
        self.write(value)
        code, row = self.run_cli()
        self.assertEqual((code, row["error_code"]), (1, "credentials_file_invalid"))

    def test_profile_mismatch_is_not_auto_detected_or_repaired(self):
        code, row = self.run_cli(profile="legacy")
        self.assertEqual((code, row["error_code"]), (1, "credentials_profile_mismatch"))

    def test_explicit_legacy_file_remains_separate_from_gateway(self):
        self.write(bundle(profile="legacy", revision=7))
        code, row = self.run_cli(profile="legacy")
        self.assertEqual(code, 0)
        self.assertEqual((row["api_profile"], row["credential_revision"]), ("legacy", 7))

    def test_unknown_profile_is_rejected_before_file_or_environment_access(self):
        with patch("sushiwait.cli.read_credentials_file", side_effect=AssertionError("no file")), \
             patch("sushiwait.cli.os.environ.get", return_value=None) as env, \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            main(["auth-status", "--api-profile", "unknown", "--credentials-file", str(self.path)])
        self.assertEqual(raised.exception.code, 2)
        self.assertFalse(any(c.args[0].startswith("SUSHIWAIT_") for c in env.call_args_list))


if __name__ == "__main__":
    unittest.main()
