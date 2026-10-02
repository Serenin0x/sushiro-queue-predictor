import base64
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import ssl
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

from sushiwait.cli import build_parser, client_for, main
from sushiwait.client import QueryResult
from sushiwait.observations import normalize_snapshot
from sushiwait.storage import SnapshotStore


ROOT = Path(__file__).resolve().parents[1]
FIXTURE1 = ROOT / "examples/fixtures/store-detail-01.synthetic.json"
FIXTURE2 = ROOT / "examples/fixtures/store-detail-02.synthetic.json"


def synthetic():
    f = json.loads(FIXTURE1.read_text())
    return normalize_snapshot(f["payload"], f["store_id"],
        request_started_at=f["observed_at"], received_at=f["observed_at"],
        elapsed_ms=0, data_origin="synthetic")


class StorageCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "observations.sqlite3"

    def run_cli(self, args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(args)
        return code, [json.loads(line) for line in out.getvalue().splitlines()]

    def test_auth_status_selects_only_profile_authorization_and_never_uses_io(self):
        def synthetic_token(expiry):
            def encoded(value):
                return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
            return ".".join((encoded(b'{"alg":"HS256"}'),
                encoded(json.dumps({"exp": expiry, "sub": "synthetic-secret-marker"}).encode()),
                encoded(b"synthetic-signature")))

        configured = {
            "SUSHIWAIT_QUERY_AUTHORIZATION": synthetic_token(123),
            "SUSHIWAIT_GATEWAY_AUTHORIZATION": synthetic_token(4102444800),
            "SUSHIWAIT_CA_FILE": "must-not-load-this-missing-ca.pem",
            "SUSHIWAIT_GATEWAY_APP_CODE": "must-not-read-app-header",
        }
        for args, profile, variable, expired in (
            (["auth-status"], "legacy", "SUSHIWAIT_QUERY_AUTHORIZATION", True),
            (["auth-status", "--api-profile", "miniapp_gateway"],
             "miniapp_gateway", "SUSHIWAIT_GATEWAY_AUTHORIZATION", False),
        ):
            with self.subTest(profile=profile):
                with contextlib.ExitStack() as stack:
                    read = stack.enter_context(patch("sushiwait.cli.os.environ.get", side_effect=configured.get))
                    for target in ("sushiwait.cli.client_for", "sushiwait.cli.SushiroClient",
                        "sushiwait.client.SushiroClient._default_opener", "ssl.create_default_context",
                        "urllib.request.build_opener", "socket.create_connection", "sushiwait.cli.SnapshotStore"):
                        stack.enter_context(patch(target, side_effect=AssertionError("must stay local")))
                    code, rows = self.run_cli(args)
                    credential_reads = [call.args[0] for call in read.call_args_list
                        if call.args[0].startswith("SUSHIWAIT_")]
                    self.assertEqual(credential_reads, [variable])
                self.assertEqual(code, 0)
                self.assertEqual(len(rows), 1)
                self.assertTrue(rows[0]["ok"])
                self.assertEqual(rows[0]["api_profile"], profile)
                self.assertTrue(rows[0]["configured"])
                self.assertEqual(rows[0]["expired"], expired)
                self.assertFalse(rows[0]["signature_verified"])
                self.assertEqual(rows[0]["expiry_source"], "unverified_claim")
                serialized = json.dumps(rows)
                for secret in (*configured.values(), "synthetic-secret-marker"):
                    self.assertNotIn(secret, serialized)
        self.assertFalse(self.db_path.exists())

    def test_auth_status_gateway_missing_does_not_fall_back_or_claim_validity(self):
        configured = {"SUSHIWAIT_QUERY_AUTHORIZATION": "legacy-sensitive-token"}
        with patch("sushiwait.cli.os.environ.get", side_effect=configured.get) as read:
            with patch("sushiwait.cli.SushiroClient", side_effect=AssertionError("must stay offline")):
                code, rows = self.run_cli(["auth-status", "--api-profile", "miniapp_gateway"])
            credential_reads = [call.args[0] for call in read.call_args_list
                if call.args[0].startswith("SUSHIWAIT_")]
            self.assertEqual(credential_reads, ["SUSHIWAIT_GATEWAY_AUTHORIZATION"])
        self.assertEqual(code, 0)
        self.assertFalse(rows[0]["configured"])
        self.assertEqual(rows[0]["token_kind"], "missing")
        self.assertIsNone(rows[0]["expired"])
        self.assertIsNone(rows[0]["remaining_seconds"])
        self.assertEqual(rows[0]["expiry_source"], "unknown")
        self.assertNotIn("legacy-sensitive-token", json.dumps(rows))

    def test_auth_status_malformed_secret_has_no_error_body_or_false_expiry(self):
        marker = "synthetic-sensitive-malformed-marker"
        with patch("sushiwait.cli.os.environ.get", return_value="Bearer " + marker + "\n"):
            code, rows = self.run_cli(["auth-status"])
        self.assertEqual(code, 0)
        self.assertTrue(rows[0]["configured"])
        self.assertIsNone(rows[0]["expired"])
        self.assertEqual(rows[0]["expiry_source"], "unknown")
        self.assertNotIn(marker, json.dumps(rows))
        self.assertNotIn("error_code", rows[0])

    def test_auth_status_unknown_profile_is_rejected_without_reading_credentials(self):
        with patch("sushiwait.cli.os.environ.get", return_value=None) as read:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                self.run_cli(["auth-status", "--api-profile", "unknown"])
            self.assertEqual(error.exception.code, 2)
            self.assertFalse(any(call.args[0].startswith("SUSHIWAIT_")
                for call in read.call_args_list))

    def test_api_profile_is_explicit_for_every_network_command(self):
        for command in ("stores", "snapshot", "collect"):
            arguments = [command]
            if command != "stores":
                arguments += ["--store-id", "123"]
            with self.subTest(command=command):
                self.assertEqual(build_parser().parse_args(arguments).api_profile, "legacy")
                self.assertEqual(build_parser().parse_args(
                    arguments + ["--api-profile", "miniapp_gateway"]
                ).api_profile, "miniapp_gateway")

    def test_unknown_profile_is_rejected_before_client_or_database(self):
        with patch("sushiwait.cli.SushiroClient") as factory:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                self.run_cli(["snapshot", "--store-id", "123", "--db", str(self.db_path),
                              "--api-profile", "unknown"])
            self.assertEqual(error.exception.code, 2)
            factory.assert_not_called()
        self.assertFalse(self.db_path.exists())

    def test_legacy_reads_only_legacy_authorization(self):
        configured = {"SUSHIWAIT_QUERY_AUTHORIZATION": "legacy-test-token",
                      "SUSHIWAIT_GATEWAY_AUTHORIZATION": "gateway-test-token",
                      "SUSHIWAIT_GATEWAY_APP_CLIENT": "gateway-test-client",
                      "SUSHIWAIT_GATEWAY_APP_CODE": "gateway-test-code",
                      "SUSHIWAIT_GATEWAY_USER_AGENT": "Synthetic Agent/1.0",
                      "SUSHIWAIT_GATEWAY_REFERER": "https://synthetic.invalid/reference",
                      "SUSHIWAIT_GATEWAY_CONTENT_TYPE": "application/x-www-form-urlencoded",
                      "SUSHIWAIT_CA_FILE": "test-ca.pem"}
        args = build_parser().parse_args(["stores"])
        with patch("sushiwait.cli.os.environ.get", side_effect=configured.get) as read:
            with patch("sushiwait.cli.SushiroClient") as factory:
                client_for(args)
                factory.assert_called_once_with("legacy-test-token", api_profile="legacy",
                    app_client=None, app_code=None, user_agent=None, referer=None,
                    content_type=None, ca_file="test-ca.pem")
            self.assertEqual([call.args[0] for call in read.call_args_list],
                ["SUSHIWAIT_QUERY_AUTHORIZATION", "SUSHIWAIT_CA_FILE"])

    def test_gateway_credentials_do_not_fall_back_to_legacy(self):
        for configured_gateway in (False, True):
            configured = {"SUSHIWAIT_QUERY_AUTHORIZATION": "legacy-test-token"}
            if configured_gateway:
                configured.update({"SUSHIWAIT_GATEWAY_AUTHORIZATION": "gateway-test-token",
                                   "SUSHIWAIT_GATEWAY_APP_CLIENT": "gateway-test-client",
                                   "SUSHIWAIT_GATEWAY_APP_CODE": "gateway-test-code",
                                   "SUSHIWAIT_GATEWAY_USER_AGENT": "Synthetic Agent/1.0",
                                   "SUSHIWAIT_GATEWAY_REFERER": "https://synthetic.invalid/reference",
                                   "SUSHIWAIT_GATEWAY_CONTENT_TYPE": "application/x-www-form-urlencoded"})
            args = build_parser().parse_args(["stores", "--api-profile", "miniapp_gateway"])
            with self.subTest(configured_gateway=configured_gateway):
                with patch("sushiwait.cli.os.environ.get", side_effect=configured.get) as read:
                    with patch("sushiwait.cli.SushiroClient") as factory:
                        client_for(args)
                        factory.assert_called_once_with(
                            "gateway-test-token" if configured_gateway else None,
                            api_profile="miniapp_gateway",
                            app_client="gateway-test-client" if configured_gateway else None,
                            app_code="gateway-test-code" if configured_gateway else None,
                            user_agent="Synthetic Agent/1.0" if configured_gateway else None,
                            referer="https://synthetic.invalid/reference" if configured_gateway else None,
                            content_type="application/x-www-form-urlencoded" if configured_gateway else None,
                            ca_file=None)
                    self.assertEqual([call.args[0] for call in read.call_args_list],
                        ["SUSHIWAIT_GATEWAY_AUTHORIZATION", "SUSHIWAIT_GATEWAY_APP_CLIENT",
                         "SUSHIWAIT_GATEWAY_APP_CODE", "SUSHIWAIT_GATEWAY_USER_AGENT",
                         "SUSHIWAIT_GATEWAY_REFERER", "SUSHIWAIT_GATEWAY_CONTENT_TYPE",
                         "SUSHIWAIT_CA_FILE"])

    def test_anonymous_never_reads_authorization_or_application_headers(self):
        for profile in ("legacy", "miniapp_gateway"):
            args = build_parser().parse_args(["stores", "--api-profile", profile, "--anonymous"])
            with self.subTest(profile=profile):
                with patch("sushiwait.cli.os.environ.get", return_value="test-ca.pem") as read:
                    with patch("sushiwait.cli.SushiroClient") as factory:
                        client_for(args)
                        factory.assert_called_once_with(None, api_profile=profile,
                            app_client=None, app_code=None, user_agent=None, referer=None,
                            content_type=None, ca_file="test-ca.pem")
                    read.assert_called_once_with("SUSHIWAIT_CA_FILE")

    def test_gateway_directory_is_unsupported_without_network(self):
        with patch("sushiwait.client.SushiroClient._default_opener") as opener_factory:
            opener_factory.return_value.open.side_effect = AssertionError("must stay offline")
            code, rows = self.run_cli(["stores", "--api-profile", "miniapp_gateway", "--anonymous"])
            opener_factory.return_value.open.assert_not_called()
        self.assertEqual(code, 1)
        self.assertEqual(rows[0]["error_code"], "unsupported_endpoint")
        self.assertIsNone(rows[0]["http_status"])

    def test_gateway_collect_stops_on_authentication_failure(self):
        result = QueryResult(False, None, "http_error", 401,
                             "2026-10-02T09:00:00Z", "2026-10-02T09:00:01Z", 1000)
        with patch("sushiwait.cli.os.environ.get", return_value=None):
            with patch("sushiwait.cli.SushiroClient") as factory, patch("sushiwait.cli.time.sleep") as sleep:
                factory.return_value.fetch_store.return_value = result
                code, rows = self.run_cli(["collect", "--api-profile", "miniapp_gateway",
                    "--store-id", "123", "--store-id", "456", "--samples", "3",
                    "--db", str(self.db_path)])
                factory.assert_called_once_with(None, api_profile="miniapp_gateway",
                    app_client=None, app_code=None, user_agent=None, referer=None,
                    content_type=None, ca_file=None)
                factory.return_value.fetch_store.assert_called_once_with("123")
                factory.return_value.fetch_stores.assert_not_called()
                sleep.assert_not_called()
        self.assertEqual(code, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["http_status"], 401)
        self.assertEqual(rows[0]["api_profile"], "miniapp_gateway")
        with SnapshotStore(self.db_path) as db:
            self.assertEqual(db.report()["groups"][0]["api_profile"], "miniapp_gateway")
            self.assertIsNone(db.last("123", data_origin="live", api_profile="legacy"))

    def test_gateway_tls_failure_stops_sampling_and_has_safe_output(self):
        marker = "offline-sensitive-exception-marker"
        with patch("sushiwait.cli.os.environ.get", return_value=None):
            with patch("sushiwait.client.SushiroClient._default_opener") as opener_factory:
                opener_factory.return_value.open.side_effect = URLError(ssl.SSLCertVerificationError(marker))
                with patch("sushiwait.cli.time.sleep") as sleep:
                    code, rows = self.run_cli(["collect", "--api-profile", "miniapp_gateway",
                        "--anonymous", "--store-id", "123", "--store-id", "456",
                        "--samples", "3", "--db", str(self.db_path)])
                    opener_factory.return_value.open.assert_called_once()
                    sleep.assert_not_called()
        self.assertEqual(code, 1)
        self.assertEqual(rows[0]["error_code"], "tls_verification_failed")
        self.assertNotIn(marker, json.dumps(rows))
        with SnapshotStore(self.db_path) as db:
            self.assertEqual(db.report()["groups"][0]["failed_samples"], 1)
            self.assertEqual(db.report()["groups"][0]["api_profile"], "miniapp_gateway")
        self.assertNotIn(marker.encode(), self.db_path.read_bytes())

    def test_replay_is_offline_and_survives_reopening(self):
        with patch("sushiwait.cli.SushiroClient", side_effect=AssertionError("must stay offline")):
            code, rows = self.run_cli(["replay", "--fixture", str(FIXTURE1), "--fixture", str(FIXTURE2), "--db", str(self.db_path)])
        self.assertEqual(code, 0)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["snapshot"]["data_origin"], "synthetic")
        self.assertEqual(rows[1]["snapshot"]["api_profile"], "legacy")
        with SnapshotStore(self.db_path) as db:
            summary = db.report()["groups"][0]
            self.assertEqual(summary["successful_samples"], 2)
            self.assertIsNone(db.last("900001", data_origin="live"))
            self.assertEqual(db.last("900001", data_origin="synthetic")["normalized"]["raw_wait"]["value"], 0)

    def test_cli_successful_live_history_is_partitioned_by_profile(self):
        payload = {"id": 123, "name": "合成门店", "wait": 4}
        result = QueryResult(True, payload, None, 200,
                             "2026-10-02T09:00:00Z", "2026-10-02T09:00:01Z", 1000)
        with patch("sushiwait.cli.os.environ.get", return_value=None):
            with patch("sushiwait.cli.SushiroClient") as factory:
                factory.return_value.fetch_store.return_value = result
                for profile, comparison in (("legacy", "initial"), ("miniapp_gateway", "initial"), ("miniapp_gateway", "comparable")):
                    code, rows = self.run_cli(["snapshot", "--store-id", "123",
                        "--api-profile", profile, "--db", str(self.db_path)])
                    self.assertEqual(code, 0)
                    self.assertEqual(rows[0]["snapshot"]["api_profile"], profile)
                    self.assertEqual(rows[0]["change"]["api_profile"], profile)
                    self.assertEqual(rows[0]["change"]["comparison"], comparison)
        with SnapshotStore(self.db_path) as db:
            groups = {group["api_profile"]: group for group in db.report()["groups"]}
            self.assertEqual(groups["legacy"]["successful_samples"], 1)
            self.assertEqual(groups["miniapp_gateway"]["successful_samples"], 2)
            for profile in groups:
                self.assertEqual(db.last("123", data_origin="live", api_profile=profile)["api_profile"], profile)

    def test_replay_preserves_explicit_profile_without_cross_source_comparison(self):
        gateway_fixture = json.loads(FIXTURE1.read_text())
        gateway_fixture["api_profile"] = "miniapp_gateway"
        gateway_path = Path(self.tmp.name) / "gateway.synthetic.json"
        gateway_path.write_text(json.dumps(gateway_fixture), encoding="utf-8")
        with patch("sushiwait.cli.SushiroClient", side_effect=AssertionError("must stay offline")):
            code, rows = self.run_cli(["replay", "--fixture", str(FIXTURE1),
                "--fixture", str(gateway_path), "--db", str(self.db_path)])
        self.assertEqual(code, 0)
        self.assertEqual([row["snapshot"]["api_profile"] for row in rows], ["legacy", "miniapp_gateway"])
        self.assertEqual([row["change"]["comparison"] for row in rows], ["initial", "initial"])
        with SnapshotStore(self.db_path) as db:
            self.assertEqual({group["api_profile"] for group in db.report()["groups"]}, {"legacy", "miniapp_gateway"})

    def test_invalid_fixture_profile_prevents_partial_replay(self):
        bad_fixture = json.loads(FIXTURE1.read_text())
        bad_fixture["api_profile"] = "not-a-fixed-profile"
        bad_path = Path(self.tmp.name) / "bad-profile.synthetic.json"
        bad_path.write_text(json.dumps(bad_fixture), encoding="utf-8")
        code, rows = self.run_cli(["replay", "--fixture", str(FIXTURE1),
            "--fixture", str(bad_path), "--db", str(self.db_path)])
        self.assertEqual(code, 2)
        self.assertFalse(rows[0]["ok"])
        with SnapshotStore(self.db_path) as db:
            self.assertEqual(db.report()["groups"], [])

    def test_live_and_synthetic_history_are_isolated(self):
        a = synthetic()
        b = dict(a, data_origin="live")
        with SnapshotStore(self.db_path) as db:
            db.save(a)
            db.save(b)
            self.assertEqual(db.last("900001", data_origin="synthetic")["data_origin"], "synthetic")
            self.assertEqual(db.last("900001", data_origin="live")["data_origin"], "live")
            self.assertEqual(len(db.report()["groups"]), 2)

    def test_collect_stops_after_rate_limit_without_retry_or_sleep(self):
        result = QueryResult(False, None, "http_error", 429,
                             "2026-10-02T09:00:00Z", "2026-10-02T09:00:01Z", 1000)
        with patch("sushiwait.cli.SushiroClient") as factory, patch("sushiwait.cli.time.sleep") as sleep:
            factory.return_value.fetch_store.return_value = result
            code, rows = self.run_cli(["collect", "--store-id", "123", "--store-id", "456", "--samples", "3", "--db", str(self.db_path)])
            self.assertEqual(code, 1)
            factory.return_value.fetch_store.assert_called_once_with("123")
            sleep.assert_not_called()
        self.assertEqual(rows[0]["http_status"], 429)
        with SnapshotStore(self.db_path) as db:
            self.assertIsNone(db.last("123", data_origin="live"))
            self.assertEqual(db.report()["groups"][0]["failed_samples"], 1)

    def test_stores_match_uses_normalized_public_name(self):
        result = QueryResult(True, {"data":[{"id":123,"name":"测试中关村大融城店"},{"id":456,"name":"其他测试店"}]}, None, 200,
                             "2026-10-02T09:00:00Z", "2026-10-02T09:00:01Z", 1000)
        with patch("sushiwait.cli.SushiroClient") as factory:
            factory.return_value.fetch_stores.return_value = result
            code, rows = self.run_cli(["stores", "--match", "中关村"])
        self.assertEqual(code, 0)
        self.assertEqual([s["store_id"] for s in rows[0]["stores"]], ["123"])

    def test_oversampling_rejected_before_network(self):
        with patch("sushiwait.cli.SushiroClient") as factory:
            code, _ = self.run_cli(["collect", "--store-id", "123", "--interval", "1", "--db", str(self.db_path)])
            factory.assert_not_called()
        self.assertEqual(code, 2)
        self.assertFalse(self.db_path.exists())

    def test_unmarked_fixture_rejected_without_partial_insert(self):
        bad = Path(self.tmp.name) / "bad.json"
        bad.write_text('{"payload":{"id":1,"name":"private"}}')
        code, _ = self.run_cli(["replay", "--fixture", str(FIXTURE1), "--fixture", str(bad), "--db", str(self.db_path)])
        self.assertEqual(code, 2)
        with SnapshotStore(self.db_path) as db:
            self.assertEqual(db.report()["groups"], [])

    def test_unknown_database_schema_is_not_overwritten(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute("PRAGMA user_version = 99")
        with self.assertRaisesRegex(ValueError, "unsupported_database_schema"):
            SnapshotStore(self.db_path)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 99)

    def test_symlink_database_rejected(self):
        target = Path(self.tmp.name) / "real.db"
        target.write_bytes(b"unchanged")
        self.db_path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "database_symlink_not_allowed"):
            SnapshotStore(self.db_path)
        self.assertEqual(target.read_bytes(), b"unchanged")

    def test_missing_report_database_is_not_created(self):
        code, _ = self.run_cli(["report", "--db", str(self.db_path)])
        self.assertEqual(code, 2)
        self.assertFalse(self.db_path.exists())

    def test_report_is_read_only(self):
        with SnapshotStore(self.db_path) as db:
            db.save(synthetic())
        before = self.db_path.read_bytes()
        with SnapshotStore(self.db_path, read_only=True) as db:
            self.assertEqual(db.report()["groups"][0]["samples"], 1)
            with self.assertRaises(sqlite3.OperationalError):
                db.save(synthetic())
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_unversioned_unrelated_database_is_not_modified(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute("CREATE TABLE unrelated (value TEXT)")
            db.execute("INSERT INTO unrelated VALUES ('preserved')")
        before = self.db_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "unsupported_database_schema"):
            SnapshotStore(self.db_path)
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_leading_zero_ids_share_history_and_are_deduplicated(self):
        a = json.loads(FIXTURE1.read_text())["payload"]
        a["id"] = 12
        result = QueryResult(True, a, None, 200,
                             "2026-10-02T09:00:00Z", "2026-10-02T09:00:01Z", 1000)
        with patch("sushiwait.cli.SushiroClient") as factory:
            factory.return_value.fetch_store.return_value = result
            code, _ = self.run_cli(["snapshot", "--store-id", "0012", "--db", str(self.db_path)])
            self.assertEqual(code, 0)
            factory.return_value.fetch_store.assert_called_once_with("12")
            factory.return_value.fetch_store.reset_mock()
            code, rows = self.run_cli(["collect", "--store-id", "0012", "--store-id", "12", "--samples", "1", "--db", str(self.db_path)])
            self.assertEqual(code, 0)
            factory.return_value.fetch_store.assert_called_once_with("12")
            self.assertEqual(rows[0]["change"]["comparison"], "comparable")
        with SnapshotStore(self.db_path) as db:
            self.assertEqual([x["store_id"] for x in db.report()["groups"]], ["12"])

    def test_invalid_identity_stops_before_local_or_network_changes(self):
        with patch("sushiwait.cli.SushiroClient") as factory:
            code, _ = self.run_cli(["snapshot", "--store-id", "not-a-real-id", "--db", str(self.db_path)])
            factory.assert_not_called()
        self.assertEqual(code, 2)
        self.assertFalse(self.db_path.exists())

    def test_report_orders_normalized_offset_times_in_utc(self):
        first = normalize_snapshot({"id":12,"name":"合成店"}, "12", request_started_at="2026-10-02T18:00:00+08:00", received_at="2026-10-02T18:00:00+08:00", elapsed_ms=0, data_origin="synthetic")
        second = normalize_snapshot({"id":12,"name":"合成店"}, "12", request_started_at="2026-10-02T11:00:00Z", received_at="2026-10-02T11:00:00Z", elapsed_ms=0, data_origin="synthetic")
        with SnapshotStore(self.db_path) as db:
            db.save(second)
            db.save(first)
            report = db.report()["groups"][0]
            self.assertEqual(report["first_received_at"], "2026-10-02T10:00:00.000Z")
            self.assertEqual(report["last_received_at"], "2026-10-02T11:00:00.000Z")


if __name__ == "__main__":
    unittest.main()
