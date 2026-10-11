"""Only synthetic sessions, mocked queries and temporary private contexts."""

import base64
import contextlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sushiwait.capture import CaptureError
from sushiwait.client import QueryResult
from sushiwait.credentials import CredentialError, read_credentials_file
from sushiwait.sessionverification import SessionPreparationError, prepare_verifier, renew_from_yyb
from sushiwait.initialization import InitializationError
from sushiwait.yyb import ENDPOINT as SUPPLIER_ENDPOINT
from tests.test_credentials import gateway_bundle, write_bundle
from tests.test_initialization import Response, material, CODE


NOW = datetime.now(timezone.utc)
REF = "https://servicewechat.com/wx0000000000000000/181/page-frame.html"


def token(marker, seconds=3600):
    payload = {"iat": int(NOW.timestamp()) - 1, "exp": int(NOW.timestamp()) + seconds,
               "private": "synthetic-session-secret-" + marker}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return "e30." + encoded + ".c2ln"


def success(*, payload=None):
    at = NOW.isoformat()
    return QueryResult(True, {"id": "3014", "name": "Synthetic Store",
        "groupQueues": {"mixedQueue": ["100", "101", "102"]}} if payload is None else payload,
        None, 200, at, at, 0)


@unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "private POSIX contexts")
class SessionVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name).resolve()
        self.parent.chmod(0o700)
        self.path = self.parent / "context.json"
        self.previous = gateway_bundle(authorization=token("old"))
        self.previous["referer"] = REF
        write_bundle(self.path, self.previous)
        self.before = self.path.read_bytes()
        self.heads = {"X-App-Client": self.previous["app_client"], "X-App-Code": "synthetic-current-date",
            "User-Agent": "Synthetic Agent/2.0", "Referer": REF, "Content-Type": "application/json"}
        self.client = Mock()
        self.client.fetch_store.return_value = success()
        self.factory = patch("sushiwait.sessionverification.SushiroClient", return_value=self.client).start()
        # Credential validation still uses the actual client static validators.
        self.addCleanup(patch.stopall)
        self.socket = patch("socket.create_connection", side_effect=AssertionError("no network")).start()

    def prepared(self, **changes):
        return prepare_verifier(credentials_file=self.path, expected_revision=1, store_id="3014", **changes)

    def check_failure(self, result, code, *, unchanged=True):
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], code)
        if unchanged:
            self.assertFalse(result["committed"])
            self.assertEqual(self.path.read_bytes(), self.before)
        dumped = json.dumps(result)
        for value in (token("old"), token("new"), REF, self.heads["X-App-Code"], str(self.path)):
            self.assertNotIn(value, dumped)

    def test_one_successful_query_precedes_complete_atomic_revision_publish(self):
        accept = self.prepared()
        self.factory.assert_not_called()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = accept(token("new"), self.heads)
        self.assertEqual(output.getvalue(), "")
        self.assertTrue(result["ok"] and result["query_accepted"] and result["committed"] and result["durability_confirmed"])
        self.assertEqual(result["revision"], 2)
        self.client.fetch_store.assert_called_once_with("3014")
        saved = read_credentials_file(self.path, api_profile="miniapp_gateway")
        self.assertEqual(saved.authorization, "Bearer " + token("new"))
        self.assertEqual(saved.app_code, self.heads["X-App-Code"])
        self.assertEqual(saved.user_agent, self.heads["User-Agent"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.check_failure(accept(token("new"), self.heads), "session_verifier_used", unchanged=False)
        self.assertEqual(self.client.fetch_store.call_count, 1)

    def test_invalid_plan_rejected_before_private_file_or_network(self):
        with patch("sushiwait.sessionverification.read_credentials_file", side_effect=AssertionError("no read")):
            for revision, store in ((False, "3014"), (2**63-1, "3014"), (1, "0"), (1, "03014"), (1, "１"), (1, str(2**63))):
                with self.assertRaises(SessionPreparationError) as caught:
                    prepare_verifier(credentials_file=self.path, expected_revision=revision, store_id=store)
                self.assertEqual(caught.exception.error_code, "session_invalid_input")
            for timeout in (False, 0, 61, float("nan"), "15"):
                with self.assertRaises(SessionPreparationError): self.prepared(timeout=timeout)
        self.factory.assert_not_called()

    def test_bad_reference_and_revision_are_preparation_failures(self):
        self.path.chmod(0o644)
        with self.assertRaises(SessionPreparationError) as caught: self.prepared()
        self.assertEqual(caught.exception.error_code, "session_reference_invalid")
        self.path.chmod(0o600)
        write_bundle(self.path, {**self.previous, "revision": 2})
        with self.assertRaises(SessionPreparationError) as caught: self.prepared()
        self.assertEqual(caught.exception.error_code, "session_baseline_changed")
        self.factory.assert_not_called()

    def test_missing_wrong_or_unchanged_material_never_queries(self):
        for code, heads, expected in [(token("new"), {}, "session_material_invalid"),
            (None, self.heads, "session_material_invalid"),
            (token("new"), {**self.heads, "X-App-Client": "wrong"}, "session_app_mismatch"),
            (token("new"), {**self.heads, "Referer": REF.replace("wx0000000000000000", "wx1111111111111111")}, "session_app_mismatch"),
            (token("old"), self.heads, "session_authorization_unchanged")]:
            self.check_failure(self.prepared()(code, heads), expected)
        self.factory.assert_not_called()

    def test_unknown_expired_or_expiring_declaration_never_queries(self):
        for value in ("synthetic-opaque-token", token("new", 0), token("new", 30)):
            result = self.prepared()(value, self.heads)
            self.check_failure(result, "session_auth_guard_stop")
            self.assertFalse(result["network_attempted"])
        self.factory.assert_not_called()

    def test_baseline_changed_after_prepare_stops_before_network(self):
        accept = self.prepared()
        changed = {**self.previous, "revision": 2}
        write_bundle(self.path, changed, atomic=True)
        before_changed = self.path.read_bytes()
        result = accept(token("new"), self.heads)
        self.assertEqual(result["error_code"], "session_baseline_changed")
        self.assertEqual(self.path.read_bytes(), before_changed)
        self.factory.assert_not_called()

    def test_http_rejection_preserves_old_context_without_retries(self):
        for status, expected in ((401, "session_query_auth_rejected"), (403, "session_query_access_denied"), (429, "session_query_failed")):
            self.client.fetch_store.reset_mock()
            self.client.fetch_store.return_value = QueryResult(False, None, "http_error", status, NOW.isoformat(), NOW.isoformat(), 0)
            result = self.prepared()(token("new"), self.heads)
            self.check_failure(result, expected)
            self.assertTrue(result["network_attempted"])
            self.assertFalse(result["query_accepted"])
            self.client.fetch_store.assert_called_once()

    def test_wrong_store_snapshot_never_commits(self):
        self.client.fetch_store.return_value = success(payload={"id": "3004", "name": "wrong"})
        self.check_failure(self.prepared()(token("new"), self.heads), "session_snapshot_invalid")

    def test_race_after_query_keeps_peer_revision_and_keeps_successful_snapshot(self):
        def peer_update(_):
            write_bundle(self.path, {**self.previous, "revision": 3}, atomic=True)
            return success()
        self.client.fetch_store.side_effect = peer_update
        result = self.prepared()(token("new"), self.heads)
        self.assertTrue(result["query_accepted"])
        self.assertIn("snapshot", result)
        self.assertFalse(result["committed"])
        self.assertEqual(result["error_code"], "session_baseline_changed")
        self.assertEqual(read_credentials_file(self.path, api_profile="miniapp_gateway").revision, 3)

    def test_expiry_during_query_is_rechecked_before_commit(self):
        with patch("sushiwait.capture._clock", return_value=NOW + timedelta(seconds=3571)):
            result = self.prepared()(token("new"), self.heads)
        self.check_failure(result, "session_auth_guard_stop")
        self.assertTrue(result["query_accepted"])
        self.assertIn("snapshot", result)

    def test_commit_and_post_commit_uncertainty_are_distinct(self):
        with patch("sushiwait.sessionverification._write_private", side_effect=CaptureError("capture_write_failed")):
            result = self.prepared()(token("new"), self.heads)
        self.check_failure(result, "session_commit_failed")
        self.assertTrue(result["query_accepted"])
        with patch("sushiwait.sessionverification._write_private", return_value=False), patch(
                "sushiwait.sessionverification.read_credentials_file", side_effect=[
                    read_credentials_file(self.path, api_profile="miniapp_gateway"),
                    read_credentials_file(self.path, api_profile="miniapp_gateway"),
                    CredentialError("credentials_file_changed")]):
            result = self.prepared()(token("new"), self.heads)
        self.assertTrue(result["committed"])
        self.assertFalse(result["durability_confirmed"])
        self.assertEqual(result["error_code"], "session_commit_unconfirmed")

    def test_transport_and_source_exception_text_is_never_returned(self):
        self.factory.side_effect = RuntimeError(token("new"))
        self.check_failure(self.prepared()(token("new"), self.heads), "session_query_setup_failed")
        self.factory.side_effect = None
        self.client.fetch_store.side_effect = RuntimeError(token("new"))
        self.check_failure(self.prepared()(token("new"), self.heads), "session_query_failed")
        self.socket.assert_not_called()

    def integration(self):
        settings = self.parent / "supplier.json"
        settings.write_text(json.dumps({"schema_version": 1, "ref": "1", "protocol_token": "a" * 64}))
        settings.chmod(0o600)
        supplier, initialize = Mock(), Mock()
        supplier.open.return_value = Response(url=SUPPLIER_ENDPOINT, body=json.dumps({
            "code": 0, "data": {"result": {"code": CODE, "errMsg": "login:ok"}}}).encode())
        initialize.open.return_value = Response(body=json.dumps({"data": {"auth_token": token("new")}}).encode())
        def source(code, second):
            body, heads = material(code, second)
            heads["X-App-Client"] = self.previous["app_client"]
            return body, heads
        return settings, supplier, initialize, source

    def test_complete_original_chain_commits_and_existing_reader_hot_loads(self):
        from sushiwait.credentials import CredentialSource
        settings, supplier, initialize, source = self.integration()
        reader = CredentialSource(credentials_file=self.path, api_profile="miniapp_gateway")
        self.assertEqual(reader.current().revision, 1)
        with patch("urllib.request.build_opener", side_effect=[supplier, initialize]) as transport:
            result = renew_from_yyb(source, settings_file=settings, credentials_file=self.path,
                                   expected_revision=1, store_id="3014")
        self.assertTrue(result["ok"])
        self.assertEqual(transport.call_count, 2)
        supplier.open.assert_called_once()
        initialize.open.assert_called_once()
        self.client.fetch_store.assert_called_once_with("3014")
        # This is the same existing reader object, not a restarted process.
        refreshed = reader.current()
        self.assertEqual(refreshed.revision, 2)
        self.assertEqual(refreshed.authorization, "Bearer " + token("new"))
        self.socket.assert_not_called()

    def test_complete_chain_does_not_call_initialization_success_query_success(self):
        settings, supplier, initialize, source = self.integration()
        self.client.fetch_store.return_value = QueryResult(False, None, "http_error", 403,
            NOW.isoformat(), NOW.isoformat(), 0)
        with patch("urllib.request.build_opener", side_effect=[supplier, initialize]):
            result = renew_from_yyb(source, settings_file=settings, credentials_file=self.path,
                                   expected_revision=1, store_id="3014")
        self.check_failure(result, "session_query_access_denied")
        self.assertTrue(result["network_attempted"])
        supplier.open.assert_called_once()
        initialize.open.assert_called_once()
        self.client.fetch_store.assert_called_once_with("3014")

    def test_target_change_between_verifier_and_initializer_stops_before_supply(self):
        settings, supplier, initialize, source = self.integration()
        different = replace(read_credentials_file(self.path, api_profile="miniapp_gateway"),
                            referer=REF.replace("wx0000000000000000", "wx1111111111111111"))
        with patch("sushiwait.initialization.read_credentials_file", return_value=different), \
                patch("urllib.request.build_opener") as transport:
            with self.assertRaises(InitializationError) as caught:
                renew_from_yyb(source, settings_file=settings, credentials_file=self.path,
                              expected_revision=1, store_id="3014")
        self.assertEqual(caught.exception.error_code, "initialize_reference_invalid")
        transport.assert_not_called()
        self.client.fetch_store.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.before)


if __name__ == "__main__": unittest.main()
