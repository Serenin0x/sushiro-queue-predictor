"""Synthetic local captures only; no public raw HAR fixtures or live calls."""

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

from sushiwait.capture import (
    CaptureError, MAX_CAPTURE_BYTES, MAX_CAPTURE_ENTRIES, MAX_CONTENT_BYTES,
    inspect_capture, write_credentials_from_capture,
)
from sushiwait.credentials import read_credentials_file


NOW = datetime(2026, 10, 3, 10, tzinfo=timezone.utc)
URL = "https://sapi.sushiro.com.cn/gateway/wechat/api/2.0/getStoreById?storeId=900001"
AUTH = "synthetic-capture-authorization-secret"
HEADERS = {
    "authorization": "Bearer " + AUTH,
    "x-app-client": "synthetic-capture-client-secret",
    "x-app-code": "synthetic-capture-code-secret",
    "user-agent": "Synthetic Capture Agent secret/1.0",
    "referer": "https://synthetic.invalid/capture-reference-secret",
    "content-type": "application/synthetic-capture-secret",
}


def token(*, exp=None, iat=None):
    claims = {"private_account": "synthetic-private-account-secret"}
    if exp is not None:
        claims["exp"] = exp
    if iat is not None:
        claims["iat"] = iat
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "e30." + payload + ".c2ln"


def entry(*, authorization=None, when=None, body=None):
    headers = dict(HEADERS)
    if authorization is not None:
        headers["authorization"] = authorization
    return {
        "startedDateTime": (when or NOW).isoformat(),
        "request": {"method": "GET", "url": URL,
                    "queryString": [{"name": "storeId", "value": "900001"}],
                    "headers": [{"name": key, "value": value} for key, value in headers.items()]},
        "response": {"status": 200, "content": {"text": json.dumps(body or {
            "id": 900001, "name": "合成门店", "wait": 80,
            "groupQueues": {"boothQueue": ["synthetic-only-number"]},
            "private_unknown": "synthetic-response-secret",
        })}},
    }


@unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "POSIX explicit file input")
class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name).resolve()
        self.parent.chmod(0o700)
        self.har = self.parent / "synthetic-source.har"
        self.destination = self.parent / "context.json"
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name in ("sushiwait.client.SushiroClient.__init__", "ssl.create_default_context",
                     "urllib.request.build_opener", "socket.create_connection"):
            self.stack.enter_context(patch(name, side_effect=AssertionError("must be offline")))

    def source(self, entries=None):
        self.har.write_text(json.dumps({"log": {"entries": [entry()] if entries is None else entries}},
                                       ensure_ascii=True), encoding="utf-8")
        self.har.chmod(0o644)  # Normal user export, deliberately not a private context.
        return self.har

    def inspect(self, entries=None, *, now=NOW):
        return inspect_capture(self.source(entries), now=now)

    def write(self, inspection, *, index=0, revision=1, now=NOW):
        return write_credentials_from_capture(inspection, entry_index=index,
                destination=self.destination, revision=revision, now=now)

    def assert_error(self, function, code):
        with self.assertRaises(CaptureError) as raised:
            function()
        error = raised.exception
        self.assertEqual(error.error_code, code)
        self.assertEqual(str(error), code)
        self.assertIsNone(error.__context__)
        self.assertIsNone(error.__cause__)
        for secret in (*HEADERS.values(), str(self.har), str(self.destination)):
            self.assertNotIn(secret, repr(error))
        return error

    def test_public_evidence_and_complete_context_stay_offline_and_secretless(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            inspection = self.inspect()
            report = inspection.public_report()
            metadata = self.write(inspection)
        self.assertEqual(out.getvalue(), "")
        self.assertFalse(report["network_performed"])
        self.assertFalse(report["network_verified"])
        self.assertEqual(report["server_acceptance"], "unverified")
        candidate = report["candidates"][0]
        self.assertEqual((candidate["store_id"], candidate["store_name"]), ("900001", "合成门店"))
        self.assertTrue(candidate["matched"])
        self.assertTrue(candidate["importable"])
        self.assertTrue(all(candidate["headers_present"].values()))
        self.assertEqual(candidate["authorization_status"]["expiry_source"], "unknown")
        public = json.dumps([report, metadata], ensure_ascii=False) + repr(inspection) + repr(inspection.entries)
        for secret in (*HEADERS.values(), AUTH, "synthetic-response-secret", "synthetic-only-number",
                       "private_unknown", str(self.har), URL):
            self.assertNotIn(secret, public)
        context = read_credentials_file(self.destination, api_profile="miniapp_gateway")
        self.assertEqual(context.authorization, "Bearer " + AUTH)
        for key, field in (("x-app-client", "app_client"), ("x-app-code", "app_code"),
                           ("user-agent", "user_agent"), ("referer", "referer"), ("content-type", "content_type")):
            self.assertEqual(getattr(context, field), HEADERS[key])
        self.assertEqual(self.destination.stat().st_mode & 0o7777, 0o600)
        self.assertTrue(metadata["written"] and metadata["committed"] and metadata["durability_confirmed"])
        self.assertEqual(self.har.stat().st_mode & 0o7777, 0o644)

    def test_original_indices_and_capture_times_select_exact_refresh(self):
        external = {"request": {"url": "https://private.invalid/secret?token=external-secret"}}
        first = entry(authorization="synthetic-first-refresh-secret", when=NOW - timedelta(minutes=1))
        last = entry(authorization="synthetic-last-refresh-secret", when=NOW,
                     body={"id": 900001, "name": "另一份合成门店"})
        inspection = self.inspect([external, first, external, last])
        report = inspection.public_report()
        self.assertEqual((report["entries_total"], report["ignored_entries"]), (4, 2))
        self.assertEqual([item["entry_index"] for item in report["candidates"]], [1, 3])
        self.assertNotEqual(report["candidates"][0]["captured_at"], report["candidates"][1]["captured_at"])
        metadata = self.write(inspection, index=3)
        self.assertEqual(metadata["store_name"], "另一份合成门店")
        self.assertEqual(read_credentials_file(self.destination, api_profile="miniapp_gateway").authorization,
                         "Bearer synthetic-last-refresh-secret")
        self.assertNotIn("external-secret", json.dumps(report))
        self.assert_error(lambda: self.write(inspection, index=0, revision=2), "capture_entry_not_found")

    def test_foreign_hosts_and_routes_are_only_counted(self):
        urls = ["https://sapi.sushiro.com.cn.evil.invalid/gateway/wechat/api/2.0/getStoreById?storeId=900001",
                "https://crm-cn-prd.sushiro.com.cn/wechat/api/2.0/getStoreById?storeId=900001",
                "https://sapi.sushiro.com.cn/gateway/wechat/api/2.0/stores?secret=external-secret",
                "https://sapi.sushiro.com.cn/private/write?secret=external-secret"]
        entries = [{"request": {"url": url, "postData": {"text": "external-body-secret"}}} for url in urls]
        report = self.inspect(entries).public_report()
        self.assertEqual(report["ignored_entries"], len(urls))
        self.assertEqual(report["candidates"], [])
        self.assertNotIn("external", json.dumps(report))

    def test_fixed_request_rejects_host_ambiguity_and_noncanonical_query(self):
        variants = [URL.replace("https:", "http:"), URL.replace("//sapi", "//user:private-secret@sapi"),
                    URL.replace(".cn/", ".cn:443/"), URL + "#private-secret", URL + "#",
                    URL + "&storeId=900001", URL + "&extra=private-secret", URL + "&",
                    URL.replace("900001", "0900001"), URL.replace("900001", "%39%30%30%30%30%31"),
                    URL.replace("900001", "0"), URL.replace("900001", str(2**63))]
        for url in variants:
            with self.subTest(url=url):
                candidate = entry()
                candidate["request"]["url"] = url
                report = self.inspect([candidate]).public_report()["candidates"][0]
                self.assertEqual(report["error_code"], "capture_invalid_request")
                self.assertFalse(report["matched"])
                self.assertIsNone(report["store_id"])
                self.assertNotIn("private-secret", json.dumps(report))

    def test_get_only_with_consistent_har_query_and_no_request_body(self):
        changes = [{"method": "POST"}, {"queryString": []}, {"queryString": None},
                   {"queryString": [{"name": "storeId", "value": "900002"}]},
                   {"queryString": [{"name": "storeId", "value": "900001"}] * 2},
                   {"postData": {"text": "private-request-body"}}]
        for change in changes:
            with self.subTest(change=change):
                candidate = entry()
                candidate["request"].update(change)
                report = self.inspect([candidate]).public_report()["candidates"][0]
                self.assertEqual(report["error_code"], "capture_invalid_request")
                self.assertFalse(report["importable"])
        candidate = entry()
        del candidate["request"]["queryString"]
        self.assertTrue(self.inspect([candidate]).public_report()["candidates"][0]["importable"])

    def test_store_identity_and_business_success_are_required(self):
        bodies = [{"id": 900002, "name": "合成门店"}, {"name": "合成门店"},
                  {"id": True, "name": "合成门店"}, {"id": 900001, "name": ""},
                  {"id": 900001, "name": "\nprivate-control"},
                  {"id": 900001, "name": "合成门店", "storeId": 900002},
                  {"id": 900001, "name": "合成门店", "code": "UNKNOWN"},
                  {"id": 900001, "name": "合成门店", "success": False}, []]
        for body in bodies:
            with self.subTest(body=body):
                candidate = entry()
                candidate["response"]["content"]["text"] = json.dumps(body)
                inspection = self.inspect([candidate])
                report = inspection.public_report()["candidates"][0]
                self.assertEqual(report["error_code"], "capture_invalid_response")
                self.assertIsNone(report["store_id"])
                self.assertIsNone(report["store_name"])
                self.assert_error(lambda: self.write(inspection), "capture_invalid_response")

    def test_reviewed_envelope_and_bare_store_with_unknown_data_are_accepted(self):
        for body in ({"data": {"storeId": "900001", "name": "合成门店"}, "code": 200},
                     {"id": 900001, "name": "合成门店", "data": {"private": "nested-secret"}}):
            with self.subTest(body=body):
                report = self.inspect([entry(body=body)]).public_report()["candidates"][0]
                self.assertTrue(report["matched"])
                self.assertTrue(report["importable"])
                self.assertNotIn("nested-secret", json.dumps(report))

    def test_http_failure_never_imports_or_claims_verified_identity(self):
        for status in (401, 302, 500, True, "200", None):
            with self.subTest(status=status):
                candidate = entry()
                candidate["response"]["status"] = status
                candidate["response"]["content"]["text"] = "private-error-body"
                inspection = self.inspect([candidate])
                report = inspection.public_report()["candidates"][0]
                self.assertEqual(report["error_code"], "capture_http_not_successful")
                self.assertFalse(report["importable"])
                self.assertIsNone(report["store_name"])
                self.assert_error(lambda: self.write(inspection), "capture_http_not_successful")

    def test_utc_capture_time_required_and_offset_is_normalized(self):
        for timestamp in (None, "invalid-private-time", "2026-10-03T10:00:00", 123):
            with self.subTest(timestamp=timestamp):
                candidate = entry()
                candidate["startedDateTime"] = timestamp
                report = self.inspect([candidate]).public_report()["candidates"][0]
                self.assertEqual(report["error_code"], "capture_invalid_timestamp")
                self.assertIsNone(report["captured_at"])
        candidate = entry()
        candidate["startedDateTime"] = "2026-10-03T18:00:00+08:00"
        self.assertEqual(self.inspect([candidate]).public_report()["candidates"][0]["captured_at"],
                         "2026-10-03T10:00:00.000Z")
        self.assert_error(lambda: inspect_capture(self.har, now=datetime(2026, 10, 3)), "capture_invalid_clock")

    def test_six_known_headers_only_case_insensitive_duplicates_rejected(self):
        candidate = entry()
        candidate["request"]["headers"].append({"name": "Cookie", "value": "private-cookie-secret"})
        self.assertTrue(self.inspect([candidate]).public_report()["candidates"][0]["importable"])
        for key in HEADERS:
            with self.subTest(key=key):
                candidate = entry()
                candidate["request"]["headers"].append({"name": key.upper(), "value": HEADERS[key]})
                report = self.inspect([candidate]).public_report()["candidates"][0]
                self.assertTrue(report["matched"])
                self.assertEqual(report["error_code"], "capture_invalid_context")
                self.assertFalse(report["importable"])

    def test_invalid_or_missing_authorization_cannot_generate_context(self):
        for authorization in ("", "Bearer", "synthetic\nheader-injection", "synthetic 非ASCII", "x" * 8193):
            with self.subTest(authorization_kind=len(authorization)):
                inspection = self.inspect([entry(authorization=authorization)])
                report = inspection.public_report()["candidates"][0]
                self.assertEqual(report["error_code"], "capture_invalid_context")
                self.assert_error(lambda: self.write(inspection), "capture_invalid_context")
        candidate = entry()
        candidate["request"]["headers"] = []
        self.assertFalse(self.inspect([candidate]).public_report()["candidates"][0]["importable"])

    def test_missing_optional_headers_are_explicit_null_and_never_mix_environment(self):
        candidate = entry()
        candidate["request"]["headers"] = [{"name": "authorization", "value": AUTH}]
        with patch("sushiwait.credentials.os.environ.get", side_effect=AssertionError("no env reads")):
            metadata = self.write(self.inspect([candidate]))
            context = read_credentials_file(self.destination, api_profile="miniapp_gateway")
        self.assertEqual(context.authorization, "Bearer " + AUTH)
        for field in ("app_client", "app_code", "user_agent", "referer", "content_type"):
            self.assertIsNone(getattr(context, field))
        stored = json.loads(self.destination.read_text())
        self.assertEqual(set(stored), {"schema_version", "api_profile", "revision", "authorization",
                                      "app_client", "app_code", "user_agent", "referer", "content_type"})
        self.assertFalse(metadata["network_performed"])

    def test_strict_har_json_rejects_duplicates_nonfinite_utf8_and_invalid_types(self):
        bodies = [b'{"log":{"entries":[]},"log":{"private":"secret"}}',
                  b'{"log":{"entries":[]},"secret":NaN}', b'{"log":{"entries":[]},"secret":1e999}',
                  b'{"log":{"entries":[]},"secret":"\xff"}', b'{"log":{"entries":[]', b'{"secret":"private"}']
        for body in bodies:
            with self.subTest(body_kind=len(body)):
                self.har.write_bytes(body)
                code = "capture_invalid_format" if body == bodies[-1] else "capture_invalid_json"
                self.assert_error(lambda: inspect_capture(self.har, now=NOW), code)
        for entries in (None, {}, [None], [{"request": []}], [{"request": {"url": 1}}]):
            self.har.write_text(json.dumps({"log": {"entries": entries}}))
            self.assert_error(lambda: inspect_capture(self.har, now=NOW), "capture_invalid_format")

    def test_entry_and_file_bounds_are_enforced_before_unbounded_processing(self):
        self.source([{"request": {"url": "https://ignored.invalid"}}] * (MAX_CAPTURE_ENTRIES + 1))
        self.assert_error(lambda: inspect_capture(self.har, now=NOW), "capture_too_large")
        with self.har.open("wb") as source:
            source.truncate(MAX_CAPTURE_BYTES + 1)
        with patch("sushiwait.capture.os.read", side_effect=AssertionError("large file must not be read")):
            self.assert_error(lambda: inspect_capture(self.har, now=NOW), "capture_too_large")

    def test_text_and_base64_responses_are_bounded_and_strict(self):
        candidate = entry()
        plain = candidate["response"]["content"]["text"].encode()
        candidate["response"]["content"] = {"text": base64.b64encode(plain).decode(), "encoding": "base64"}
        self.assertTrue(self.inspect([candidate]).public_report()["candidates"][0]["importable"])
        for content in ({"text": "invalid %% private-secret", "encoding": "base64"},
                        {"text": "e30=\n", "encoding": "base64"},
                        {"text": "e30====", "encoding": "base64"},
                        {"text": "{}", "encoding": "gzip"}, {"text": []},
                        {"text": '{"id":900001,"id":900002,"name":"secret"}'},
                        {"text": '{"id":900001,"name":"合成门店","secret":Infinity}'}):
            with self.subTest(content_kind=content.get("encoding")):
                candidate["response"]["content"] = content
                report = self.inspect([candidate]).public_report()["candidates"][0]
                self.assertEqual(report["error_code"], "capture_invalid_response")
        for content in ({"text": "x" * (MAX_CONTENT_BYTES + 1)},
                        {"text": "中" * (MAX_CONTENT_BYTES // 3 + 1)},
                        {"text": base64.b64encode(b"x" * (MAX_CONTENT_BYTES + 1)).decode(), "encoding": "base64"}):
            candidate["response"]["content"] = content
            report = self.inspect([candidate]).public_report()["candidates"][0]
            self.assertEqual(report["error_code"], "capture_content_too_large")

    def test_source_file_type_owner_links_and_symlinks_are_rejected(self):
        self.source()
        linked = self.parent / "source-link.har"
        linked.symlink_to(self.har)
        self.assert_error(lambda: inspect_capture(linked, now=NOW), "capture_unsafe")
        hardlink = self.parent / "source-hardlink.har"
        os.link(self.har, hardlink)
        self.assert_error(lambda: inspect_capture(self.har, now=NOW), "capture_unsafe")
        hardlink.unlink()
        with patch("sushiwait.capture.os.geteuid", return_value=os.geteuid() + 1):
            self.assert_error(lambda: inspect_capture(self.har, now=NOW), "capture_unsafe")
        directory = self.parent / "not-a-har"
        directory.mkdir()
        self.assert_error(lambda: inspect_capture(directory, now=NOW), "capture_unsafe")
        fifo = self.parent / "source-fifo"
        os.mkfifo(fifo)
        self.assert_error(lambda: inspect_capture(fifo, now=NOW), "capture_unsafe")
        ancestor = self.parent / "linked-directory"
        ancestor.symlink_to(self.parent, target_is_directory=True)
        self.assert_error(lambda: inspect_capture(ancestor / self.har.name, now=NOW), "capture_unsafe")

    def test_source_replacement_and_in_place_mutation_during_read_are_detected(self):
        original_read = os.read
        for replacement in (True, False):
            with self.subTest(replacement=replacement):
                self.source()
                changed = False
                def read_and_change(fd, count):
                    nonlocal changed
                    result = original_read(fd, count)
                    if not changed:
                        changed = True
                        if replacement:
                            other = self.parent / "other-source.har"
                            other.write_bytes(b"{}"); os.replace(other, self.har)
                        else:
                            self.har.write_bytes(b"{}");
                    return result
                with patch("sushiwait.capture.os.read", side_effect=read_and_change):
                    self.assert_error(lambda: inspect_capture(self.har, now=NOW), "capture_changed")

    def test_unavailable_and_json_errors_have_no_secret_exception_chain(self):
        self.assert_error(lambda: inspect_capture(self.har, now=NOW), "capture_unavailable")
        self.har.write_text('{"private":"' + AUTH + '" invalid}', encoding="utf-8")
        self.assert_error(lambda: inspect_capture(self.har, now=NOW), "capture_invalid_json")
        self.assertEqual(CaptureError(AUTH).error_code, "capture_invalid_format")

    def test_source_parent_replacement_during_read_is_detected(self):
        nested = self.parent / "source-directory"
        nested.mkdir()
        self.har = nested / "synthetic.har"
        self.source()
        original_read = os.read
        changed = False
        def read_and_replace_parent(fd, count):
            nonlocal changed
            result = original_read(fd, count)
            if not changed:
                changed = True
                nested.rename(self.parent / "moved-source-directory")
                nested.mkdir()
                self.har.write_bytes(result)
            return result
        with patch("sushiwait.capture.os.read", side_effect=read_and_replace_parent):
            self.assert_error(lambda: inspect_capture(self.har, now=NOW), "capture_changed")

    def test_current_expiry_is_checked_again_at_generation_with_zero_save(self):
        expires = NOW.timestamp() + 100
        authorization = token(iat=NOW.timestamp() - 3600, exp=expires)
        inspection = self.inspect([entry(authorization=authorization)])
        self.assertTrue(inspection.public_report()["candidates"][0]["importable"])
        with patch("sushiwait.capture._write_private", side_effect=AssertionError("must not write")):
            self.assert_error(lambda: self.write(inspection, now=NOW + timedelta(seconds=70)), "capture_auth_expiring")
            self.assert_error(lambda: self.write(inspection, now=NOW + timedelta(seconds=100)), "capture_auth_expired")
        self.assertFalse(self.destination.exists())
        report = self.inspect([entry(authorization=authorization)], now=NOW + timedelta(seconds=100)).public_report()
        self.assertEqual(report["candidates"][0]["authorization_status"]["remaining_seconds"], 0)
        self.assertFalse(report["candidates"][0]["importable"])
        self.assertNotIn(authorization, json.dumps(report))
        self.assertNotIn("private_account", json.dumps(report))

    def test_invalid_claim_order_stops_and_unknown_expiry_is_not_invented(self):
        reversed_token = token(iat=NOW.timestamp() + 100, exp=NOW.timestamp() + 50)
        inspection = self.inspect([entry(authorization=reversed_token)])
        self.assert_error(lambda: self.write(inspection), "capture_auth_invalid_claim")
        for authorization in (AUTH, token(iat=NOW.timestamp()), "e30.invalid%%.c2ln"):
            with self.subTest(kind=authorization[:3]):
                inspection = self.inspect([entry(authorization=authorization)])
                status = inspection.public_report()["candidates"][0]["authorization_status"]
                self.assertIsNone(status["remaining_seconds"])
                self.assertIsNone(status["expired"])
                self.assertEqual(status["expiry_source"], "unknown")
                self.assertTrue(self.write(inspection, revision=1 if not self.destination.exists() else 2)["committed"])
                self.destination.unlink()

    def test_exp_without_iat_still_protects_expiring_context(self):
        inspection = self.inspect([entry(authorization=token(exp=NOW.timestamp() + 30))])
        status = inspection.public_report()["candidates"][0]["authorization_status"]
        self.assertIsNone(status["declared_issued_at"])
        self.assertIsNone(status["declared_lifetime_seconds"])
        self.assertEqual(status["remaining_seconds"], 30)
        self.assert_error(lambda: self.write(inspection), "capture_auth_expiring")

    def test_private_destination_and_all_ancestor_links_are_enforced_without_chmod(self):
        inspection = self.inspect()
        self.parent.chmod(0o755)
        self.assert_error(lambda: self.write(inspection), "capture_destination_unsafe")
        self.assertEqual(self.parent.stat().st_mode & 0o7777, 0o755)
        self.parent.chmod(0o700)
        linked = self.parent / "linked-parent"
        linked.symlink_to(self.parent, target_is_directory=True)
        self.assert_error(lambda: write_credentials_from_capture(inspection, entry_index=0,
                          destination=linked / "context.json", revision=1, now=NOW), "capture_destination_unsafe")
        self.assertFalse(self.destination.exists())

    def test_existing_target_links_permissions_and_profile_are_not_overwritten(self):
        inspection = self.inspect()
        self.write(inspection)
        original = self.destination.read_bytes()
        for mode in (0o644, 0o700):
            self.destination.chmod(mode)
            self.assert_error(lambda: self.write(inspection, revision=2), "capture_destination_unsafe")
            self.assertEqual(self.destination.read_bytes(), original)
        self.destination.chmod(0o600)
        hardlink = self.parent / "hardlink-context"
        os.link(self.destination, hardlink)
        self.assert_error(lambda: self.write(inspection, revision=2), "capture_destination_unsafe")
        hardlink.unlink()
        self.destination.unlink()
        self.destination.symlink_to(self.har)
        self.assert_error(lambda: self.write(inspection, revision=2), "capture_destination_unsafe")
        self.destination.unlink()
        legacy = {"schema_version": 1, "api_profile": "legacy", "revision": 1, "authorization": AUTH}
        self.destination.write_text(json.dumps(legacy)); self.destination.chmod(0o600)
        original = self.destination.read_bytes()
        self.assert_error(lambda: self.write(inspection, revision=2), "capture_destination_unsafe")
        self.assertEqual(self.destination.read_bytes(), original)

    def test_replacement_requires_higher_revision_and_changes_the_complete_group(self):
        first = self.inspect()
        self.write(first, revision=4)
        original = self.destination.read_bytes()
        for revision in (4, 3, 0, True, 2**63):
            with self.subTest(revision=revision):
                self.assert_error(lambda: self.write(first, revision=revision), "capture_revision_conflict")
                self.assertEqual(self.destination.read_bytes(), original)
        candidate = entry(authorization="new-synthetic-query-secret")
        candidate["request"]["headers"] = candidate["request"]["headers"][:1]
        self.destination.chmod(0o400)
        self.write(self.inspect([candidate]), revision=5)
        context = read_credentials_file(self.destination, api_profile="miniapp_gateway")
        self.assertEqual(context.revision, 5)
        self.assertEqual(context.authorization, "Bearer new-synthetic-query-secret")
        self.assertIsNone(context.app_client)
        self.assertIsNone(context.content_type)
        self.assertEqual(self.destination.stat().st_mode & 0o7777, 0o600)

    def test_invalid_selection_never_writes(self):
        inspection = self.inspect()
        for index in (-1, 1, True, "0"):
            with self.subTest(index=index):
                self.assert_error(lambda: self.write(inspection, index=index), "capture_entry_not_found")
        self.assertFalse(self.destination.exists())

    def test_write_and_rename_failures_preserve_old_file_and_remove_temporary(self):
        inspection = self.inspect()
        self.write(inspection)
        original = self.destination.read_bytes()
        for operation in ("os.write", "os.replace"):
            with self.subTest(operation=operation):
                with patch("sushiwait.capture." + operation, side_effect=OSError("private-filesystem-secret")):
                    self.assert_error(lambda: self.write(inspection, revision=2), "capture_write_failed")
                self.assertEqual(self.destination.read_bytes(), original)
                self.assertEqual(list(self.parent.glob(".sushiwait-context-*.tmp")), [])

    def test_target_race_and_temporary_replacement_are_detected_before_commit(self):
        inspection = self.inspect()
        self.write(inspection)
        original_write = os.write
        original = self.destination.read_bytes()
        for target_race in (True, False):
            with self.subTest(target_race=target_race):
                self.destination.write_bytes(original); self.destination.chmod(0o600)
                changed = False
                replacement = b'{"race":"synthetic-only"}'
                def write_and_race(fd, body):
                    nonlocal changed
                    result = original_write(fd, body)
                    if not changed:
                        changed = True
                        if target_race:
                            self.destination.write_bytes(replacement)
                        else:
                            temporary = next(self.parent.glob(".sushiwait-context-*.tmp"))
                            temporary.unlink()
                            temporary.write_bytes(replacement); temporary.chmod(0o600)
                    return result
                with patch("sushiwait.capture.os.write", side_effect=write_and_race):
                    self.assert_error(lambda: self.write(inspection, revision=2), "capture_destination_changed")
                self.assertEqual(self.destination.read_bytes(), replacement if target_race else original)
                self.assertEqual(list(self.parent.glob(".sushiwait-context-*.tmp")), [])

    def test_temporary_read_back_detects_same_size_in_place_corruption(self):
        inspection = self.inspect()
        self.write(inspection)
        original = self.destination.read_bytes()
        original_write = os.write
        def write_and_corrupt(fd, body):
            result = original_write(fd, body)
            temporary = next(self.parent.glob(".sushiwait-context-*.tmp"))
            temporary.write_bytes(b"}" + body[1:])
            return result
        with patch("sushiwait.capture.os.write", side_effect=write_and_corrupt):
            self.assert_error(lambda: self.write(inspection, revision=2), "capture_destination_changed")
        self.assertEqual(self.destination.read_bytes(), original)
        self.assertEqual(list(self.parent.glob(".sushiwait-context-*.tmp")), [])

    def test_destination_parent_replacement_during_write_is_detected(self):
        inspection = self.inspect()
        nested = self.parent / "private-destination"
        nested.mkdir(mode=0o700)
        self.destination = nested / "context.json"
        self.write(inspection)
        original = self.destination.read_bytes()
        moved = self.parent / "moved-private-destination"
        original_write = os.write
        def write_and_replace_parent(fd, body):
            result = original_write(fd, body)
            nested.rename(moved)
            nested.mkdir(mode=0o700)
            self.destination.write_bytes(original)
            self.destination.chmod(0o600)
            return result
        with patch("sushiwait.capture.os.write", side_effect=write_and_replace_parent):
            self.assert_error(lambda: self.write(inspection, revision=2), "capture_destination_changed")
        self.assertEqual((moved / "context.json").read_bytes(), original)
        self.assertEqual(self.destination.read_bytes(), original)
        self.assertEqual(list(moved.glob(".sushiwait-context-*.tmp")), [])

    def test_directory_lock_refuses_competing_cooperative_writer(self):
        import fcntl
        inspection = self.inspect()
        descriptor = os.open(self.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_error(lambda: self.write(inspection), "capture_write_failed")
            self.assertFalse(self.destination.exists())
        finally:
            os.close(descriptor)

    def test_fsync_failure_before_commit_preserves_old_but_after_commit_is_reported(self):
        inspection = self.inspect()
        self.write(inspection)
        original = self.destination.read_bytes()
        original_fsync = os.fsync
        for failure_call in (1, 2, 3):
            with self.subTest(failure_call=failure_call):
                count = 0
                def fsync_or_fail(fd):
                    nonlocal count
                    count += 1
                    if count == failure_call:
                        raise OSError("synthetic-durability-failure")
                    return original_fsync(fd)
                with patch("sushiwait.capture.os.fsync", side_effect=fsync_or_fail):
                    if failure_call < 3:
                        self.assert_error(lambda: self.write(inspection, revision=2), "capture_write_failed")
                    else:
                        metadata = self.write(inspection, revision=2)
                        self.assertTrue(metadata["written"] and metadata["committed"])
                        self.assertFalse(metadata["durability_confirmed"])
                if failure_call < 3:
                    self.assertEqual(self.destination.read_bytes(), original)
                else:
                    self.assertEqual(read_credentials_file(self.destination, api_profile="miniapp_gateway").revision, 2)
                self.assertEqual(list(self.parent.glob(".sushiwait-context-*.tmp")), [])

    def test_commit_time_guard_removes_temp_and_preserves_previous_context(self):
        self.write(self.inspect())
        original = self.destination.read_bytes()
        authorization = token(exp=NOW.timestamp() + 31)
        inspection = self.inspect([entry(authorization=authorization)])
        with patch("sushiwait.capture._clock", side_effect=[NOW, NOW + timedelta(seconds=1)]):
            self.assert_error(lambda: self.write(inspection, revision=2), "capture_auth_expiring")
        self.assertEqual(self.destination.read_bytes(), original)
        self.assertEqual(list(self.parent.glob(".sushiwait-context-*.tmp")), [])

    def test_real_cli_import_uses_advancing_clock_at_commit_and_preserves_old_context(self):
        from sushiwait.cli import main
        import stat

        self.write(self.inspect())
        original = self.destination.read_bytes()
        authorization = token(exp=NOW.timestamp() + 31)
        self.source([entry(authorization=authorization)])
        phase = {"temporary_fsynced": False, "dynamic_checks": 0}
        original_fsync = os.fsync

        def progressing_clock(now=None):
            # A supplied timestamp remains fixed, which reproduces the bug if
            # CLI passes its report timestamp into the real writer again.
            if now is not None:
                return now
            phase["dynamic_checks"] += 1
            return NOW + timedelta(seconds=1 if phase["temporary_fsynced"] else 0)

        def fsync_and_advance(fd):
            original_fsync(fd)
            if stat.S_ISREG(os.fstat(fd).st_mode):
                phase["temporary_fsynced"] = True

        output = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(output))
            stack.enter_context(patch("sushiwait.cli._utc_clock", return_value=NOW))
            stack.enter_context(patch("sushiwait.capture._clock", side_effect=progressing_clock))
            stack.enter_context(patch("sushiwait.capture.os.fsync", side_effect=fsync_and_advance))
            stack.enter_context(patch("sushiwait.capture.os.replace", side_effect=AssertionError("must not commit")))
            stack.enter_context(patch("sushiwait.cli.SnapshotStore", side_effect=AssertionError("no database")))
            result = main(["capture-import", "--har", str(self.har), "--entry-index", "0",
                           "--output", str(self.destination), "--revision", "2"])
        self.assertTrue(phase["temporary_fsynced"])
        self.assertGreaterEqual(phase["dynamic_checks"], 2)
        self.assertEqual(result, 1)
        report = json.loads(output.getvalue())
        self.assertEqual(report["error_code"], "capture_auth_expiring")
        self.assertFalse(report["network_performed"])
        self.assertEqual(self.destination.read_bytes(), original)
        self.assertEqual(list(self.parent.glob(".sushiwait-context-*.tmp")), [])
        for secret in (*HEADERS.values(), authorization, str(self.har), str(self.destination)):
            self.assertNotIn(secret, output.getvalue())

    def test_serialized_context_size_is_bounded_before_any_target_operation(self):
        inspection = self.inspect([entry(authorization="\\" * 8185)])
        self.assertTrue(inspection.public_report()["candidates"][0]["importable"])
        with patch("sushiwait.capture._open_parent", side_effect=AssertionError("must not touch target")):
            self.assert_error(lambda: self.write(inspection), "capture_invalid_context")
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
