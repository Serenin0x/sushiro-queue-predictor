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
from sushiwait.client import QueryResult
from sushiwait.storage import SnapshotStore


BASE = datetime(2026, 10, 3, 4, 0, tzinfo=timezone.utc)


def jwt(exp=None, iat=None):
    claims = {}
    if exp is not None:
        claims["exp"] = int(exp.timestamp())
    if iat is not None:
        claims["iat"] = int(iat.timestamp())
    claims["sub"] = "synthetic-private-claim"
    encoded = lambda value: base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
    return ".".join((encoded(b'{"alg":"HS256"}'), encoded(json.dumps(claims).encode()), encoded(b"synthetic-signature")))


def bundle(revision=1, authorization="synthetic-old-query-secret", **overrides):
    return {"schema_version": 1, "api_profile": "miniapp_gateway", "revision": revision,
        "authorization": authorization, "app_client": "synthetic-old-client-secret",
        "app_code": "synthetic-old-code-secret", "user_agent": "Synthetic Agent/1.0",
        "referer": "https://synthetic.invalid/reference", "content_type": "application/x-www-form-urlencoded",
        **overrides}


def success(store_id, elapsed=5):
    return QueryResult(True, {"id": int(store_id), "name": "合成店", "wait": 1,
        "groupQueues": {"boothQueue": ["100-2"], "mixedQueue": ["100"],
                        "counterQueue": [], "reservationQueue": []}}, None, 200,
        "2026-10-03T04:00:00.000Z", "2026-10-03T04:00:00.005Z", elapsed)


class FakeClock:
    def __init__(self):
        self.seconds = 0
        self.sleeps = []
        self.on_sleep = None

    def now(self):
        return BASE + timedelta(seconds=self.seconds)

    def monotonic(self):
        return self.seconds

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.seconds += seconds
        if self.on_sleep is not None:
            self.on_sleep(seconds)


@unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "POSIX private-file input")
class CollectionCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name).resolve()
        self.parent.chmod(0o700)
        self.path = self.parent / "credentials.json"
        self.db = self.parent / "observations.sqlite3"
        self.clock = FakeClock()
        self.write(bundle())

    def write(self, value):
        replacement = self.parent / "replacement.json"
        replacement.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")
        replacement.chmod(0o600)
        os.replace(replacement, self.path)

    def arguments(self, command="collect", *, samples=2, interval=60, ids=("123",)):
        args = [command, "--api-profile", "miniapp_gateway", "--credentials-file", str(self.path), "--db", str(self.db)]
        for store_id in ids:
            args.extend(("--store-id", store_id))
        if command == "collect":
            args.extend(("--samples", str(samples), "--interval", str(interval)))
        return args

    def run_cli(self, args, factory):
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("sushiwait.cli._utc_clock", side_effect=self.clock.now))
            stack.enter_context(patch("sushiwait.cli.time.monotonic", side_effect=self.clock.monotonic))
            stack.enter_context(patch("sushiwait.cli.time.sleep", side_effect=self.clock.sleep))
            client_factory = stack.enter_context(patch("sushiwait.cli.SushiroClient", side_effect=factory))
            # Header validators remain the real pure methods in credentials.py.
            stack.enter_context(patch("socket.create_connection", side_effect=AssertionError("no network")))
            stack.enter_context(contextlib.redirect_stdout(out))
            code = main(args)
        return code, [json.loads(line) for line in out.getvalue().splitlines()], client_factory

    def factory(self, callback=None):
        calls = []
        contexts = []
        class FakeClient:
            def __init__(client, authorization, options):
                client.authorization = authorization
                client.options = options

            def fetch_store(client, store_id):
                calls.append((store_id, client.authorization, client.options))
                return callback(store_id, len(calls)) if callback is not None else success(store_id)

        def create(authorization, **options):
            contexts.append((authorization, options))
            return FakeClient(authorization, options)
        return create, calls, contexts

    def test_two_stores_use_reloaded_whole_bundle_without_environment_header_mix(self):
        def response(store_id, index):
            if index == 1:
                self.write(bundle(2, "synthetic-new-query-secret", app_client="synthetic-new-client-secret",
                                  app_code=None, user_agent=None, referer=None, content_type=None))
            return success(store_id)
        create, calls, contexts = self.factory(response)
        with patch("sushiwait.cli.os.environ.get", return_value=None) as read:
            code, rows, client_factory = self.run_cli(self.arguments(samples=1, ids=("123", "456")), create)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], "Bearer synthetic-old-query-secret")
        self.assertEqual(calls[1][1], "Bearer synthetic-new-query-secret")
        self.assertEqual(calls[1][2]["app_client"], "synthetic-new-client-secret")
        for key in ("app_code", "user_agent", "referer", "content_type"):
            self.assertIsNone(calls[1][2][key])
        credential_reads = [call.args[0] for call in read.call_args_list if call.args[0].startswith("SUSHIWAIT_")]
        self.assertEqual(credential_reads, ["SUSHIWAIT_CA_FILE", "SUSHIWAIT_CA_FILE"])
        self.assertEqual(client_factory.call_count, 2)
        serialized = json.dumps(rows)
        for marker in ("synthetic-old-query-secret", "synthetic-new-query-secret", "synthetic-new-client-secret", str(self.path)):
            self.assertNotIn(marker, serialized)

    def test_unchanged_bundle_keeps_client_but_file_is_read_before_each_get(self):
        create, calls, contexts = self.factory()
        from sushiwait.credentials import read_credentials_file
        with patch("sushiwait.credentials.read_credentials_file", wraps=read_credentials_file) as read:
            code, rows, client_factory = self.run_cli(self.arguments(samples=1, ids=("123", "456", "789")), create)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 3)
        self.assertEqual(read.call_count, 3)
        self.assertEqual(client_factory.call_count, 1)

    def test_declared_expiry_guard_is_zero_request_zero_opener_for_snapshot(self):
        for remaining, expected in ((30, "auth_expiring"), (0, "auth_declared_expired"), (-1, "auth_declared_expired")):
            with self.subTest(remaining=remaining):
                self.write(bundle(authorization=jwt(BASE + timedelta(seconds=remaining))))
                code, rows, factory = self.run_cli(self.arguments("snapshot"), lambda *a, **k: self.fail("must not build client"))
                self.assertEqual(code, 1)
                factory.assert_not_called()
                self.assertEqual(rows[0]["error_code"], expected)
                self.assertEqual(rows[0]["failure_phase"], "preflight")
                self.assertEqual(rows[0]["checked_at"], "2026-10-03T04:00:00.000Z")
                self.assertIsNone(rows[0]["http_status"])
                self.assertNotIn("received_at", rows[0])
                self.assertNotIn("synthetic-private-claim", json.dumps(rows))
        with SnapshotStore(self.db, read_only=True) as db:
            saved = [json.loads(row[0]) for row in db.db.execute("SELECT payload_json FROM samples")]
        self.assertEqual(len(saved), 3)
        for row in saved:
            self.assertEqual(row["timing"], {"checked_at": "2026-10-03T04:00:00.000Z"})
            self.assertEqual(row["failure_phase"], "preflight")

    def test_wait_wakes_at_protection_deadline_and_stops_without_second_get(self):
        self.write(bundle(authorization=jwt(BASE + timedelta(seconds=70))))
        create, calls, contexts = self.factory()
        code, rows, _ = self.run_cli(self.arguments(interval=60), create)
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.clock.sleeps, [40])
        self.assertEqual(rows[-1]["error_code"], "auth_expiring")
        self.assertEqual(rows[-1]["checked_at"], "2026-10-03T04:00:40.000Z")

    def test_fresh_bundle_at_protection_wake_continues_until_next_scheduled_get(self):
        self.write(bundle(authorization=jwt(BASE + timedelta(seconds=70))))
        def update(seconds):
            if self.clock.seconds == 40:
                self.write(bundle(2, jwt(BASE + timedelta(hours=2)), app_code="synthetic-new-code-secret"))
        self.clock.on_sleep = update
        create, calls, contexts = self.factory()
        code, rows, factory = self.run_cli(self.arguments(interval=60), create)
        self.assertEqual(code, 0)
        self.assertEqual(self.clock.sleeps, [40, 20])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][2]["app_code"], "synthetic-new-code-secret")
        self.assertEqual(factory.call_count, 2)
        self.assertTrue(all(row["ok"] for row in rows))

    def test_time_passage_between_stores_stops_before_second_get(self):
        self.write(bundle(authorization=jwt(BASE + timedelta(seconds=40))))
        def response(store_id, index):
            self.clock.seconds += 15
            return success(store_id)
        create, calls, contexts = self.factory(response)
        code, rows, _ = self.run_cli(self.arguments(samples=1, ids=("123", "456")), create)
        self.assertEqual(code, 1)
        self.assertEqual([call[0] for call in calls], ["123"])
        self.assertEqual(rows[-1]["store_id"], "456")
        self.assertEqual(rows[-1]["error_code"], "auth_expiring")

    def test_opaque_and_missing_expiry_do_not_infer_lifetime_or_block_bounded_query(self):
        for authorization in ("synthetic-opaque-secret", jwt(iat=BASE)):
            with self.subTest(kind=authorization.count(".")):
                self.write(bundle(authorization=authorization))
                create, calls, contexts = self.factory()
                code, rows, _ = self.run_cli(self.arguments(samples=1), create)
                self.assertEqual(code, 0)
                self.assertEqual(len(calls), 1)

    def test_reversed_claims_stop_without_request(self):
        self.write(bundle(authorization=jwt(BASE, BASE + timedelta(seconds=60))))
        code, rows, factory = self.run_cli(self.arguments(samples=1), lambda *a, **k: self.fail("must not build client"))
        factory.assert_not_called()
        self.assertEqual(code, 1)
        self.assertEqual(rows[0]["error_code"], "auth_claims_invalid")
        self.assertIsNone(rows[0]["auth_status"]["expired"])

    def test_changed_same_revision_and_rollback_stop_without_using_old_or_candidate(self):
        for initial_revision, next_revision, expected in ((1, 1, "credentials_revision_conflict"),
            (2, 1, "credentials_revision_rollback"), (1, 0, "credentials_file_invalid")):
            with self.subTest(next_revision=next_revision):
                self.write(bundle(initial_revision))
                def response(store_id, index):
                    self.write(bundle(next_revision, "synthetic-updated-secret"))
                    return success(store_id)
                create, calls, contexts = self.factory(response)
                code, rows, _ = self.run_cli(self.arguments(samples=1, ids=("123", "456")), create)
                self.assertEqual(code, 1)
                self.assertEqual(len(calls), 1)
                self.assertEqual(rows[-1]["error_code"], expected)
                self.assertEqual(rows[-1]["failure_phase"], "preflight")

    def test_unsafe_file_stops_before_environment_client_or_opener(self):
        self.path.chmod(0o644)
        with patch("sushiwait.cli.os.environ.get", return_value=None) as read:
            code, rows, factory = self.run_cli(self.arguments("snapshot"), lambda *a, **k: self.fail("must not build client"))
        self.assertEqual(code, 1)
        factory.assert_not_called()
        self.assertEqual(rows[0]["error_code"], "credentials_file_unsafe")
        self.assertNotIn("auth_status", rows[0])
        self.assertFalse(any(call.args[0].startswith("SUSHIWAIT_") for call in read.call_args_list))
        for secret in (str(self.path), "synthetic-old-query-secret", "synthetic-old-code-secret"):
            self.assertNotIn(secret, json.dumps(rows))
            self.assertNotIn(secret.encode(), self.db.read_bytes())

    def test_environment_expiry_guard_is_local_without_new_file_option(self):
        authorization = jwt(BASE + timedelta(seconds=20))
        configured = {"SUSHIWAIT_GATEWAY_AUTHORIZATION": authorization}
        args = ["snapshot", "--api-profile", "miniapp_gateway", "--store-id", "123", "--db", str(self.db)]
        with patch("sushiwait.cli.os.environ.get", side_effect=configured.get) as read:
            code, rows, factory = self.run_cli(args, lambda *a, **k: self.fail("must not build client"))
        self.assertEqual(code, 1)
        factory.assert_not_called()
        self.assertEqual(rows[0]["error_code"], "auth_expiring")
        self.assertNotIn(authorization, json.dumps(rows))
        credential_reads = [call.args[0] for call in read.call_args_list if call.args[0].startswith("SUSHIWAIT_")]
        self.assertNotIn("SUSHIWAIT_QUERY_AUTHORIZATION", credential_reads)
        self.assertNotIn("SUSHIWAIT_CA_FILE", credential_reads)

    def test_first_actual_401_does_not_wait_reload_or_retry(self):
        def response(store_id, index):
            self.write(bundle(2, "synthetic-updated-secret"))
            return QueryResult(False, None, "http_error", 401,
                "2026-10-03T04:00:00.000Z", "2026-10-03T04:00:00.005Z", 5)
        create, calls, contexts = self.factory(response)
        code, rows, factory = self.run_cli(self.arguments(ids=("123", "456")), create)
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(rows[0]["failure_phase"], "request")
        self.assertEqual(rows[0]["http_status"], 401)

    def test_invalid_replacement_stops_and_does_not_reuse_cached_credentials(self):
        def response(store_id, index):
            self.path.write_text('{"secret":"must-not-output"}', encoding="utf-8")
            return success(store_id)
        create, calls, contexts = self.factory(response)
        code, rows, factory = self.run_cli(self.arguments(samples=1, ids=("123", "456")), create)
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(rows[-1]["error_code"], "credentials_profile_mismatch")
        self.assertNotIn("must-not-output", json.dumps(rows))
        self.assertIsNone(rows[-1]["http_status"])

    def test_normalization_failure_retains_actual_request_timings_and_stops(self):
        create, calls, contexts = self.factory()
        with patch("sushiwait.cli.normalize_snapshot", side_effect=ValueError("synthetic-sensitive-parser-detail")):
            code, rows, _ = self.run_cli(self.arguments(ids=("123", "456")), create)
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(rows[0]["error_code"], "normalization_failed")
        self.assertEqual(rows[0]["failure_phase"], "normalization")
        self.assertNotIn("synthetic-sensitive-parser-detail", json.dumps(rows))
        with SnapshotStore(self.db, read_only=True) as db:
            saved = json.loads(db.db.execute("SELECT payload_json FROM samples").fetchone()[0])
        self.assertEqual(saved["timing"]["request_started_at"], "2026-10-03T04:00:00.000Z")
        self.assertEqual(saved["http_status"], 200)
        self.assertEqual(saved["failure_phase"], "normalization")

    def test_restarting_with_updated_bundle_continues_same_database_history(self):
        self.write(bundle(authorization=jwt(BASE)))
        code, rows, _ = self.run_cli(self.arguments("snapshot"), lambda *a, **k: self.fail("expired"))
        self.assertEqual(code, 1)
        self.write(bundle(2, "synthetic-restarted-query-secret"))
        create, calls, contexts = self.factory()
        for expected_comparison in ("initial", "comparable"):
            code, rows, _ = self.run_cli(self.arguments("snapshot"), create)
            self.assertEqual(code, 0)
            self.assertEqual(rows[0]["change"]["comparison"], expected_comparison)
        with SnapshotStore(self.db, read_only=True) as db:
            self.assertEqual(db.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 3)
            self.assertIsNotNone(db.last("123", data_origin="live", api_profile="miniapp_gateway"))

    def test_credentials_file_and_anonymous_are_mutually_exclusive_before_io(self):
        with patch("sushiwait.cli.SushiroClient") as client:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                main(self.arguments("snapshot") + ["--anonymous"])
            self.assertEqual(raised.exception.code, 2)
            client.assert_not_called()
        self.assertFalse(self.db.exists())


if __name__ == "__main__":
    unittest.main()
