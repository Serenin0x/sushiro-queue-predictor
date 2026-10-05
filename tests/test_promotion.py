"""Private-file, atomic-race and OFF-gating checks; no Surge/upstream access."""
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

from sushiwait import promotion
from sushiwait.capture import CaptureError, _write_private
from sushiwait.cli import main
from sushiwait.credentials import read_credentials_file
from sushiwait.surgeguard import GuardError

NOW = datetime.now(timezone.utc)
OFF = {"mitm_enabled": False, "capture_enabled": False, "auto_mitm": False}
REF = "https://servicewechat.com/wx0000000000000000/1/page-frame.html"


def token(marker, *, expires=None):
    claims = {"iat": int(NOW.timestamp()), "exp": int((expires or NOW + timedelta(hours=1)).timestamp()),
              "private": "synthetic-promotion-secret-" + marker}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "Bearer e30." + body + ".c2ln"


def bundle(revision=2, *, marker="new", **changes):
    return {"schema_version": 1, "api_profile": "miniapp_gateway", "revision": revision,
            "authorization": token(marker), "app_client": "synthetic-miniapp",
            "app_code": "synthetic-private-code", "user_agent": "Synthetic Agent/1.0",
            "referer": REF, "content_type": "application/json", **changes}


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.parent = Path(self.folder.name).resolve()
        self.parent.chmod(0o700)
        self.destination, self.staging = self.parent / "current.json", self.parent / "staged.json"
        self.save(self.destination, bundle(1, marker="old"))
        self.save(self.staging, bundle())
        self.before = self.destination.read_bytes()
        self.states = patch.object(promotion, "read_state", return_value=OFF).start()
        self.addCleanup(patch.stopall)
        self.socket = patch("socket.socket", side_effect=AssertionError("unexpected_network")).start()
        self.connect = patch("socket.create_connection", side_effect=AssertionError("unexpected_network")).start()

    def tearDown(self):
        self.assertEqual(self.socket.call_count, 0)
        self.assertEqual(self.connect.call_count, 0)

    def save(self, path, data):
        path.write_text(json.dumps(data))
        path.chmod(0o600)

    def run_promotion(self, **changes):
        return promotion.promote_context(credentials_file=self.destination, staged_file=self.staging,
                                         expected_revision=1, now=NOW, **changes)

    def error(self, code):
        with self.assertRaises(promotion.PromotionError) as caught:
            self.run_promotion()
        self.assertEqual(caught.exception.error_code, code)
        self.assertFalse(caught.exception.committed)
        self.assertEqual(self.destination.read_bytes(), self.before)

    def test_complete_context_is_atomic_private_and_staging_unchanged(self):
        staged = self.staging.read_bytes()
        result = self.run_promotion()
        self.assertEqual(read_credentials_file(self.destination, api_profile="miniapp_gateway").revision, 2)
        self.assertEqual(self.staging.read_bytes(), staged)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)
        self.assertTrue(result["committed"] and result["durability_confirmed"] and result["debug_off_confirmed"])
        self.assertEqual(result["server_acceptance"], "unverified")
        self.assertFalse(result["external_network_performed"])
        self.assertEqual(self.states.call_count, 3)
        for value in (token("new"), token("old"), REF, "synthetic-private-code", str(self.parent)):
            self.assertNotIn(value, json.dumps(result))

    def test_initial_any_switch_on_preserves_main(self):
        for key in OFF:
            with self.subTest(key=key):
                self.states.return_value = {**OFF, key: True}
                self.error("promotion_debug_switch_on")

    def test_unknown_debug_state_preserves_main(self):
        self.states.side_effect = GuardError("surge_guard_unknown_state")
        self.error("promotion_debug_state_unknown")

    def test_unknown_debug_value_or_shape_preserves_main(self):
        for state in ({}, {**OFF, "mitm_enabled": None}, {**OFF, "mitm_enabled": 0}):
            self.states.return_value = state
            self.error("promotion_debug_state_unknown")

    def test_switch_enabled_between_preflight_and_commit_preserves_main(self):
        self.states.side_effect = [OFF, {**OFF, "mitm_enabled": True}]
        self.error("promotion_debug_switch_on")

    def test_invalid_expected_revision_does_not_read_native_state(self):
        for value in (0, -1, True, 1.0, "1", 2**63):
            self.states.reset_mock()
            with self.subTest(value=value), self.assertRaises(promotion.PromotionError):
                promotion.promote_context(credentials_file=self.destination, staged_file=self.staging,
                                          expected_revision=value)
            self.states.assert_not_called()

    def test_same_path_refused_before_native_state(self):
        with self.assertRaisesRegex(promotion.PromotionError, "promotion_path_conflict"):
            promotion.promote_context(credentials_file=self.destination, staged_file=self.destination,
                                      expected_revision=1)
        self.states.assert_not_called()

    def test_baseline_revision_must_match_expected(self):
        self.save(self.destination, bundle(3, marker="other"));self.before = self.destination.read_bytes()
        self.error("promotion_baseline_changed")

    def test_staging_revision_must_be_strictly_new(self):
        self.save(self.staging, bundle(1))
        self.error("promotion_revision_not_new")

    def test_new_revision_with_old_authorization_is_refused(self):
        self.save(self.staging, bundle(2, marker="old"))
        self.error("promotion_authorization_unchanged")

    def test_same_app_is_required_and_normal_version_changes_allowed(self):
        for changes in ({"referer": REF.replace("0000/", "0001/")}, {"app_client": "other-app"},
                        {"referer": "https://unrelated.invalid/page"}):
            self.save(self.staging, bundle(**changes));self.error("promotion_app_mismatch")
        self.save(self.staging, bundle(referer=REF.replace("/1/", "/2/"), app_code="synthetic-next-day-code"))
        self.assertTrue(self.run_promotion()["committed"])

    def test_missing_or_empty_context_is_refused(self):
        for key in ("authorization", "app_code", "user_agent", "referer", "content_type", "app_client"):
            for value in (None, ""):
                self.save(self.staging, bundle(**{key: value}))
                self.error("promotion_private_context_invalid")

    def test_expired_expiring_invalid_and_opaque_declarations_are_refused(self):
        tokens = [token("expiry", expires=NOW), token("expiry", expires=NOW+timedelta(seconds=30)),
                  "Bearer synthetic-opaque-authorization", "Bearer e30.aW52YWxpZA.c2ln"]
        for value in tokens:
            self.save(self.staging, bundle(authorization=value))
            self.error("promotion_auth_guard_stop")

    def test_naive_clock_is_refused(self):
        with self.assertRaisesRegex(promotion.PromotionError, "promotion_invalid_clock"):
            promotion.promote_context(credentials_file=self.destination, staged_file=self.staging,
                                      expected_revision=1, now=datetime(2026, 1, 1))
        self.assertEqual(self.destination.read_bytes(), self.before)

    def test_unsafe_permissions_symlinks_and_hardlinks_are_refused(self):
        self.staging.chmod(0o644);self.error("promotion_private_context_invalid")
        self.staging.chmod(0o600)
        link = self.parent / "linked.json";link.symlink_to(self.staging)
        with self.assertRaisesRegex(promotion.PromotionError, "promotion_private_context_invalid"):
            promotion.promote_context(credentials_file=self.destination, staged_file=link,
                                      expected_revision=1, now=NOW)
        link.unlink();os.link(self.staging, link);self.error("promotion_private_context_invalid")

    def test_malformed_duplicate_and_oversized_staging_is_refused_without_leak(self):
        for body in ('{"secret":"synthetic-private-secret"', '{"schema_version":1,"schema_version":1}',
                     "synthetic-private-secret" * 2000):
            self.staging.write_text(body)
            self.error("promotion_private_context_invalid")

    def test_validated_candidate_is_serialized_instead_of_raw_reopen(self):
        original = promotion._write_private
        def swap_staging(body, *args, **kwargs):
            self.save(self.staging, bundle(3, marker="swapped"))
            return original(body, *args, **kwargs)
        with patch.object(promotion, "_write_private", side_effect=swap_staging):
            self.run_promotion()
        self.assertEqual(read_credentials_file(self.destination, api_profile="miniapp_gateway").authorization, token("new"))

    def test_cooperating_same_revision_baseline_change_is_detected_under_writer_lock(self):
        original = promotion._write_private
        changed = bundle(1, marker="changed-baseline")
        def race(body, *args, **kwargs):
            self.save(self.destination, changed)
            return original(body, *args, **kwargs)
        with patch.object(promotion, "_write_private", side_effect=race), \
                self.assertRaisesRegex(promotion.PromotionError, "promotion_baseline_changed"):
            self.run_promotion()
        self.assertEqual(json.loads(self.destination.read_text()), changed)

    def test_missing_expected_destination_is_not_recreated(self):
        old = read_credentials_file(self.destination, api_profile="miniapp_gateway")
        new = read_credentials_file(self.staging, api_profile="miniapp_gateway")
        self.destination.unlink()
        with self.assertRaisesRegex(CaptureError, "capture_destination_changed"):
            _write_private(self.staging.read_bytes(), self.destination, 2, new, NOW, expected_current=old)
        self.assertFalse(self.destination.exists())

    def test_write_failure_preserves_main_and_does_not_retry(self):
        with patch.object(promotion, "_write_private", side_effect=CaptureError("capture_write_failed")) as write:
            self.error("promotion_write_failed")
        self.assertEqual(write.call_count, 1)

    def test_expiry_rechecked_at_commit_preserves_main(self):
        with patch("sushiwait.capture._clock", return_value=NOW+timedelta(hours=2)):
            self.error("promotion_auth_guard_stop")

    def test_postcommit_readback_change_is_not_hidden_or_rolled_back(self):
        original = promotion._write_private
        def replace_after_commit(*args, **kwargs):
            durable = original(*args, **kwargs)
            self.save(self.destination, bundle(3, marker="subsequent-writer"))
            return durable
        with patch.object(promotion, "_write_private", side_effect=replace_after_commit), \
                self.assertRaises(promotion.PromotionError) as caught:
            self.run_promotion()
        self.assertTrue(caught.exception.committed and caught.exception.durability_confirmed)
        self.assertEqual(read_credentials_file(self.destination, api_profile="miniapp_gateway").revision, 3)

    def test_same_app_with_invalid_staging_path_does_not_copy_environment(self):
        self.staging.unlink()
        with patch.dict(os.environ, {"SUSHIWAIT_AUTHORIZATION": token("environment"),
                                     "SUSHIWAIT_APP_CODE": "synthetic-env-code"}):
            self.error("promotion_private_context_invalid")

    def test_postcommit_off_failure_reports_committed_without_rollback(self):
        self.states.side_effect = [OFF, OFF, {**OFF, "auto_mitm": True}]
        with self.assertRaises(promotion.PromotionError) as caught:
            self.run_promotion()
        self.assertTrue(caught.exception.committed and caught.exception.durability_confirmed)
        self.assertEqual(caught.exception.error_code, "promotion_commit_unconfirmed")
        self.assertEqual(read_credentials_file(self.destination, api_profile="miniapp_gateway").revision, 2)

    def test_committed_fsync_uncertainty_is_not_reported_as_unwritten(self):
        original = promotion._write_private
        def uncertain(*args, **kwargs):
            original(*args, **kwargs);return False
        with patch.object(promotion, "_write_private", side_effect=uncertain):
            result = self.run_promotion()
        self.assertTrue(result["committed"] and result["debug_off_confirmed"])
        self.assertFalse(result["durability_confirmed"])

    def test_cli_success_and_error_only_emit_safe_metadata(self):
        output = io.StringIO()
        args = ["context-promote", "--credentials-file", str(self.destination),
                "--staged-file", str(self.staging), "--expected-revision", "1"]
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(args), 0)
        result = json.loads(output.getvalue());self.assertTrue(result["committed"])
        self.assertNotIn("synthetic-private", output.getvalue());self.assertNotIn(str(self.parent), output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(args), 1)
        result = json.loads(output.getvalue())
        self.assertFalse(result["committed"])
        self.assertEqual(result["error_code"], "promotion_baseline_changed")


if __name__ == "__main__":
    unittest.main()
