import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sushiwait.credentials import (
    CredentialError, CredentialSource, MAX_CREDENTIAL_FILE_BYTES, read_credentials_file,
)


def gateway_bundle(revision=1, authorization="synthetic-query-secret"):
    return {"schema_version": 1, "api_profile": "miniapp_gateway", "revision": revision,
        "authorization": authorization, "app_client": "synthetic-client-secret",
        "app_code": "synthetic-code-secret", "user_agent": "Synthetic Agent/1.0",
        "referer": "https://synthetic.invalid/reference", "content_type": "application/x-www-form-urlencoded"}


def write_bundle(path, value, *, atomic=False):
    destination = path.with_name("replacement.json") if atomic else path
    destination.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")
    destination.chmod(0o600)
    if atomic:
        os.replace(destination, path)


@unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "POSIX private-file input")
class CredentialsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name).resolve()
        self.parent.chmod(0o700)
        self.path = self.parent / "context.json"
        write_bundle(self.path, gateway_bundle())

    def read(self):
        return read_credentials_file(self.path, api_profile="miniapp_gateway")

    def assert_safe_error(self, function, code):
        with self.assertRaises(CredentialError) as raised:
            function()
        self.assertEqual(raised.exception.error_code, code)
        self.assertEqual(str(raised.exception), code)
        self.assertNotIn(str(self.path), repr(raised.exception))
        self.assertNotIn("synthetic-query-secret", repr(raised.exception))

    def test_complete_context_is_validated_without_network_ca_or_output(self):
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(out))
            for target in ("sushiwait.client.SushiroClient.__init__", "ssl.create_default_context",
                           "urllib.request.build_opener", "socket.create_connection"):
                stack.enter_context(patch(target, side_effect=AssertionError("must stay local")))
            context = self.read()
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(context.authorization, "Bearer synthetic-query-secret")
        self.assertEqual(context.revision, 1)
        for secret in ("synthetic-query-secret", "synthetic-client-secret", "synthetic-code-secret",
                       "Synthetic Agent/1.0", "https://synthetic.invalid/reference", "application/x-www-form-urlencoded"):
            self.assertNotIn(secret, repr(context))
        self.assertNotIn(str(self.path), repr(CredentialSource("miniapp_gateway", credentials_file=self.path)))

    def test_legacy_format_accepts_no_gateway_headers_or_arbitrary_destinations(self):
        value = {"schema_version": 1, "api_profile": "legacy", "revision": 1,
                 "authorization": "synthetic-legacy-secret"}
        write_bundle(self.path, value)
        context = read_credentials_file(self.path, api_profile="legacy")
        self.assertEqual(context.authorization, "Bearer synthetic-legacy-secret")
        self.assertIsNone(context.app_client)
        for key, extra in (("app_client", None), ("origin", "https://other.invalid"),
                           ("endpoint", "cancel"), ("ca_file", "private-path"), ("headers", {}), ("log", {})):
            with self.subTest(key=key):
                write_bundle(self.path, {**value, key: extra})
                self.assert_safe_error(lambda: read_credentials_file(self.path, api_profile="legacy"),
                                       "credentials_file_invalid")

    def test_gateway_requires_explicit_complete_headers_and_never_fills_from_env(self):
        value = gateway_bundle()
        for key in ("app_client", "app_code", "user_agent", "referer", "content_type"):
            with self.subTest(key=key):
                incomplete = dict(value)
                del incomplete[key]
                write_bundle(self.path, incomplete)
                with patch("sushiwait.credentials.os.environ.get", side_effect=AssertionError("no environment fallback")):
                    self.assert_safe_error(self.read, "credentials_file_invalid")
        write_bundle(self.path, {**value, **{key: None for key in ("app_client", "app_code", "user_agent", "referer", "content_type")}})
        self.assertIsNone(self.read().app_client)

    def test_profile_mismatch_is_rejected_before_any_client(self):
        self.assert_safe_error(lambda: read_credentials_file(self.path, api_profile="legacy"),
                               "credentials_profile_mismatch")

    def test_file_and_parent_must_be_private_without_auto_chmod(self):
        for mode in (0o644, 0o640, 0o660, 0o700):
            with self.subTest(mode=mode):
                self.path.chmod(mode)
                self.assert_safe_error(self.read, "credentials_file_unsafe")
                self.assertEqual(self.path.stat().st_mode & 0o7777, mode)
        self.path.chmod(0o600)
        self.parent.chmod(0o755)
        self.assert_safe_error(self.read, "credentials_file_unsafe")
        self.assertEqual(self.parent.stat().st_mode & 0o7777, 0o755)

    def test_private_read_only_file_is_supported(self):
        self.path.chmod(0o400)
        self.assertEqual(self.read().revision, 1)

    def test_owner_mismatch_is_rejected(self):
        current_uid = os.geteuid()
        with patch("sushiwait.credentials.os.geteuid", return_value=current_uid + 1):
            self.assert_safe_error(self.read, "credentials_file_unsafe")

    def test_final_and_ancestor_symlinks_are_not_followed(self):
        linked_file = self.parent / "link.json"
        linked_file.symlink_to(self.path)
        self.assert_safe_error(lambda: read_credentials_file(linked_file, api_profile="miniapp_gateway"),
                               "credentials_file_unsafe")
        linked_directory = self.parent / "linked-directory"
        linked_directory.symlink_to(self.parent, target_is_directory=True)
        self.assert_safe_error(lambda: read_credentials_file(linked_directory / self.path.name, api_profile="miniapp_gateway"),
                               "credentials_file_unsafe")

    def test_hardlinks_and_nonregular_files_are_rejected_without_blocking(self):
        other = self.parent / "hardlink.json"
        os.link(self.path, other)
        self.assert_safe_error(self.read, "credentials_file_unsafe")
        other.unlink()
        fifo = self.parent / "fifo"
        os.mkfifo(fifo, 0o600)
        self.assert_safe_error(lambda: read_credentials_file(fifo, api_profile="miniapp_gateway"),
                               "credentials_file_unsafe")

    def test_size_limit_is_enforced_before_content_read(self):
        self.path.write_bytes(b"x" * (MAX_CREDENTIAL_FILE_BYTES + 1))
        with patch("sushiwait.credentials.os.read", side_effect=AssertionError("must not read")) as read:
            self.assert_safe_error(self.read, "credentials_file_too_large")
            read.assert_not_called()

    def test_duplicate_nonfinite_utf8_and_json_errors_are_safe(self):
        for body in (b'{"revision":1,"revision":2}', b'{"other":{"key":1,"key":2}}',
                     b'{"other":NaN}', b'{"other":Infinity}', b'{"other":1e999}',
                     b"\xff", b"not-json"):
            with self.subTest(body=body):
                self.path.write_bytes(body)
                self.assert_safe_error(self.read, "credentials_file_invalid_json")

    def test_decoder_exception_does_not_escape_in_error_context(self):
        self.path.write_bytes(b'{"authorization":"synthetic-sensitive-json-body", broken}')
        with self.assertRaises(CredentialError) as raised:
            self.read()
        self.assertIsNone(raised.exception.__context__)
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn("synthetic-sensitive-json-body", str(raised.exception))

    def test_schema_revision_authorization_and_header_validation(self):
        for key, invalid in (("schema_version", True), ("schema_version", 2), ("revision", True),
            ("revision", 0), ("revision", -1), ("revision", 2**63), ("revision", 1.0),
            ("authorization", None), ("authorization", ""), ("authorization", "a" * 8193),
            ("authorization", "a" * 8186),
            ("authorization", "secret\r\nInjected: private"), ("app_code", "white space"),
            ("app_client", "秘密"), ("user_agent", "control\tvalue"), ("user_agent", "x" * 2049),
            ("referer", "https://synthetic.invalid/with space"), ("content_type", "x" * 257)):
            with self.subTest(key=key, invalid_type=type(invalid)):
                write_bundle(self.path, {**gateway_bundle(), key: invalid})
                self.assert_safe_error(self.read, "credentials_file_invalid")

    def test_atomic_replacement_during_read_is_rejected_without_retry(self):
        real_read = os.read
        replaced = False
        def replace_after_read(descriptor, size):
            nonlocal replaced
            body = real_read(descriptor, size)
            if body and not replaced:
                replaced = True
                write_bundle(self.path, gateway_bundle(2, "replacement-secret"), atomic=True)
            return body
        with patch("sushiwait.credentials.os.read", side_effect=replace_after_read):
            self.assert_safe_error(self.read, "credentials_file_changed")
        self.assertEqual(self.read().revision, 2)

    def test_in_place_mutation_during_read_is_rejected(self):
        real_read = os.read
        mutated = False
        def mutate_after_read(descriptor, size):
            nonlocal mutated
            body = real_read(descriptor, size)
            if body and not mutated:
                mutated = True
                write_bundle(self.path, gateway_bundle(2, "replacement-secret"))
            return body
        with patch("sushiwait.credentials.os.read", side_effect=mutate_after_read):
            self.assert_safe_error(self.read, "credentials_file_changed")

    def test_revisions_do_not_roll_back_or_change_under_same_revision(self):
        source = CredentialSource("miniapp_gateway", credentials_file=self.path)
        first = source.current()
        self.assertEqual(source.current(), first)
        write_bundle(self.path, gateway_bundle(1, "changed-same-revision"), atomic=True)
        self.assert_safe_error(source.current, "credentials_revision_conflict")
        write_bundle(self.path, {**gateway_bundle(2, "replacement-secret"), "app_code": None}, atomic=True)
        second = source.current()
        self.assertEqual(second.revision, 2)
        self.assertIsNone(second.app_code)
        write_bundle(self.path, gateway_bundle(1), atomic=True)
        self.assert_safe_error(source.current, "credentials_revision_rollback")

    def test_environment_context_is_isolated_and_kept_until_restart(self):
        configured = {"SUSHIWAIT_QUERY_AUTHORIZATION": "legacy-synthetic-secret",
                      "SUSHIWAIT_GATEWAY_AUTHORIZATION": "gateway-synthetic-secret"}
        source = CredentialSource("legacy")
        with patch("sushiwait.credentials.os.environ.get", side_effect=configured.get) as read:
            first = source.current()
            configured["SUSHIWAIT_QUERY_AUTHORIZATION"] = "new-legacy-synthetic-secret"
            self.assertEqual(source.current(), first)
            read.assert_called_once_with("SUSHIWAIT_QUERY_AUTHORIZATION")
        self.assertEqual(first.authorization, "Bearer legacy-synthetic-secret")
        with patch("sushiwait.credentials.os.environ.get", side_effect=AssertionError("anonymous reads no credentials")):
            self.assertIsNone(CredentialSource("miniapp_gateway", anonymous=True).current().authorization)

    def test_missing_file_does_not_expose_path_and_unsupported_platform_is_closed(self):
        self.path.unlink()
        self.assert_safe_error(self.read, "credentials_file_unavailable")
        with self.assertRaises(CredentialError) as raised:
            self.read()
        self.assertIsNone(raised.exception.__context__)
        self.assertIsNone(raised.exception.__cause__)
        with patch("sushiwait.credentials.os.name", "nt"):
            self.assert_safe_error(self.read, "credentials_file_unsupported")


if __name__ == "__main__":
    unittest.main()
