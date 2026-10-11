"""Synthetic fixed-route initialization; no WeChat or SAPI network access."""

import contextlib
from datetime import datetime, timezone
from email.message import Message
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error
from urllib.parse import parse_qs

from sushiwait.initialization import ENDPOINT, MAX_RESPONSE_BYTES, InitializationError, prepare_consumer, initialize_from_yyb
from sushiwait.yyb import ENDPOINT as SUPPLIER_ENDPOINT
from tests.test_credentials import gateway_bundle, write_bundle


CODE = "synthetic-fresh-code"
TOKEN = "synthetic-new-token"
TARGET = "wx0000000000000000"
NOW = datetime(2026, 10, 11, 0, 0, tzinfo=timezone.utc)


def material(code, second):
    return ({"long_token": "synthetic-startup", "js_code": code, "ts": second,
        "nonce": "Abc123", "sign": "a" * 64, "device_id": "synthetic-device",
        "app_version": "synthetic-version"}, {"X-App-Client": "synthetic-client",
        "X-App-Code": "synthetic-date-header", "User-Agent": "Synthetic Runtime",
        "Referer": f"https://servicewechat.com/{TARGET}/181/page-frame.html",
        "Content-Type": "application/json"})


class Response:
    def __init__(self, *, body=None, url=ENDPOINT, content_type="application/json", status=200):
        self.body = body if body is not None else json.dumps({"data": {"auth_token": TOKEN}}).encode()
        self.url, self.status = url, status
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.closed, self.limits = False, []
    def __enter__(self): return self
    def __exit__(self, *args): self.closed = True
    def geturl(self): return self.url
    def read(self, limit):
        self.limits.append(limit)
        return self.body[:limit]


@unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "private POSIX reference")
class InitializationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name).resolve()
        self.parent.chmod(0o700)
        self.context = self.parent / "reference.json"
        self.reference = gateway_bundle()
        self.reference["referer"] = f"https://servicewechat.com/{TARGET}/181/page-frame.html"
        write_bundle(self.context, self.reference)
        self.response = Response()
        self.opener = Mock()
        self.opener.open.return_value = self.response
        self.factory = patch("sushiwait.initialization.urllib.request.build_opener", return_value=self.opener).start()
        self.clock = patch("sushiwait.initialization.datetime").start()
        self.clock.now.return_value = NOW
        self.addCleanup(patch.stopall)
        self.accept = Mock(return_value={"synthetic_only": True, "query_accepted": False})

    def prepared(self, source=material, **kwargs):
        return prepare_consumer(source, self.accept, reference_context_file=self.context, **kwargs)

    def error(self, category, action):
        with self.assertRaises(InitializationError) as raised:
            action()
        error = raised.exception
        self.assertEqual(error.error_code, category)
        self.assertIsNone(error.__context__)
        self.assertIsNone(error.__cause__)
        for secret in (CODE, TOKEN, str(self.context), "synthetic-device"):
            self.assertNotIn(secret, repr(error))

    def test_ready_before_code_single_fixed_request_and_no_reference_heads(self):
        before = self.context.read_bytes()
        consume = self.prepared()
        self.factory.assert_not_called()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = consume(CODE)
        self.assertEqual(result, {"synthetic_only": True, "query_accepted": False})
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(self.context.read_bytes(), before)
        self.opener.open.assert_called_once()
        request = self.opener.open.call_args.args[0]
        self.assertEqual((request.full_url, request.method), (ENDPOINT, "POST"))
        self.assertEqual(json.loads(request.data), material(CODE, int(NOW.timestamp()))[0])
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIsNone(request.get_header("Cookie"))
        self.assertNotIn(self.reference["authorization"], str(request.header_items()))
        self.assertEqual(self.opener.open.call_args.kwargs, {"timeout": 30})
        proxy, redirect = self.factory.call_args.args
        self.assertEqual(proxy.proxies, {})
        self.assertIsNone(redirect.redirect_request(None, None, 302, "", {}, "https://other.invalid"))
        token, heads = self.accept.call_args.args
        self.assertEqual(token, TOKEN)
        self.assertEqual(dict(heads), material(CODE, int(NOW.timestamp()))[1])
        with self.assertRaises(TypeError): heads["Referer"] = "changed"
        self.assertNotIn("js_code", heads)
        self.assertTrue(self.response.closed)
        self.assertEqual(self.response.limits, [MAX_RESPONSE_BYTES + 1])

    def test_missing_hooks_and_invalid_timeout_rejected_before_reference_read(self):
        with patch("sushiwait.initialization.read_credentials_file", side_effect=AssertionError("no read")):
            self.error("initialize_invalid_input", lambda: self.prepared(None))
            self.error("initialize_invalid_input", lambda: prepare_consumer(material, None, reference_context_file=self.context))
            for value in (False, 0, 61, float("nan"), float("inf"), "30"):
                self.error("initialize_invalid_input", lambda: self.prepared(timeout=value))
        self.factory.assert_not_called()

    def test_explicit_form_material_is_encoded_as_form_without_guessing(self):
        def source(code, second):
            body, heads = material(code, second)
            body["ts"] = str(second)
            body["device_id"] = "synthetic device+&"
            heads["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
            return body, heads
        self.prepared(source)(CODE)
        request = self.opener.open.call_args.args[0]
        parsed = parse_qs(request.data.decode(), strict_parsing=True)
        self.assertEqual(parsed["js_code"], [CODE])
        self.assertEqual(parsed["device_id"], ["synthetic device+&"])
        self.assertEqual(parsed["ts"], [str(int(NOW.timestamp()))])
        self.assertIn("application/x-www-form-urlencoded", request.get_header("Content-type"))

    def test_new_token_respects_existing_8192_byte_context_limit(self):
        self.opener.open.return_value = Response(body=json.dumps({"data": {"auth_token": "a" * 8192}}).encode())
        self.prepared()(CODE)
        self.assertEqual(len(self.accept.call_args.args[0]), 8192)
        self.opener.open.return_value = Response(body=json.dumps({"data": {"auth_token": "a" * 8193}}).encode())
        self.error("initialize_response_invalid", lambda: self.prepared()(CODE))

    def test_unsafe_reference_and_wrong_app_do_not_create_consumer(self):
        self.context.chmod(0o644)
        self.error("initialize_reference_invalid", self.prepared)
        self.context.chmod(0o600)
        write_bundle(self.context, {**self.reference, "referer": "https://other.invalid"})
        self.error("initialize_reference_invalid", self.prepared)
        self.factory.assert_not_called()

    def test_material_must_match_fresh_code_time_and_known_target(self):
        cases = [{"js_code": "synthetic-stale-code"}, {"ts": 1}, {"ts": True},
            {"nonce": "x y123"}, {"sign": "bad"}, {"device_id": None}, {"app_version": ""}]
        for updates in cases:
            def altered(code, second):
                body, heads = material(code, second)
                body.update(updates)
                return body, heads
            self.error("initialize_material_invalid", lambda: self.prepared(altered)(CODE))
        for updates in ({"Referer": "https://servicewechat.com/wx1111111111111111/181/page-frame.html"},
                        {"X-App-Code": "bad\nsecret"}, {"Content-Type": "text/plain"},
                        {"Authorization": "synthetic-stale-token"}, {"Cookie": "secret"}):
            def altered(code, second):
                body, heads = material(code, second)
                heads.update(updates)
                return body, heads
            self.error("initialize_material_invalid", lambda: self.prepared(altered)(CODE))
        self.factory.assert_not_called()

    def test_preparation_delay_or_clock_rollback_never_sends_request(self):
        for delta in (6, -1):
            self.clock.now.side_effect = [NOW, datetime.fromtimestamp(NOW.timestamp() + delta, timezone.utc)]
            self.error("initialize_material_expired", lambda: self.prepared()(CODE))
        self.factory.assert_not_called()

    def test_failed_source_and_repeated_consumption_never_replay(self):
        source = Mock(side_effect=RuntimeError(CODE + TOKEN))
        consume = self.prepared(source)
        self.error("initialize_source_failed", lambda: consume(CODE))
        self.error("initialize_used", lambda: consume(CODE))
        source.assert_called_once()
        self.factory.assert_not_called()

    def test_successful_consumption_is_also_single_use(self):
        consume = self.prepared()
        consume(CODE)
        self.error("initialize_used", lambda: consume("synthetic-another-code"))
        self.opener.open.assert_called_once()

    def test_concurrent_call_cannot_duplicate_initialization(self):
        entered, release = threading.Event(), threading.Event()
        def blocking(code, second):
            entered.set()
            if not release.wait(2): raise AssertionError("timeout")
            return material(code, second)
        consume = self.prepared(blocking)
        results = []
        thread = threading.Thread(target=lambda: results.append(consume(CODE)))
        thread.start()
        self.assertTrue(entered.wait(2))
        try:
            self.error("initialize_used", lambda: consume(CODE))
        finally:
            release.set(); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [{"synthetic_only": True, "query_accepted": False}])
        self.opener.open.assert_called_once()

    def test_http_401_403_redirect_and_other_failure_stop_without_body_or_retry(self):
        for status, category in {401: "initialize_auth_rejected", 403: "initialize_access_denied",
                                 302: "initialize_redirect_rejected", 502: "initialize_http_failed"}.items():
            self.opener.open.reset_mock()
            body = io.BytesIO((CODE + TOKEN).encode())
            self.opener.open.side_effect = urllib.error.HTTPError(ENDPOINT, status, CODE, {}, body)
            self.error(category, lambda: self.prepared()(CODE))
            self.assertTrue(body.closed)
            self.opener.open.assert_called_once()
        self.accept.assert_not_called()

    def test_response_origin_content_type_missing_token_or_malformed_body_rejected(self):
        for response, category in [(Response(url="https://other.invalid"), "initialize_redirect_rejected"),
            (Response(content_type="text/html"), "initialize_response_invalid"),
            *[(Response(body=body), "initialize_response_invalid") for body in
              (b'{"data":{}}', b'{"data":{"auth_token":"a","auth_token":"b"}}',
               b'{"data":{"auth_token":NaN}}', b'not-json-secret', b'\xff',
               b'x' * (MAX_RESPONSE_BYTES + 2), b'{"data":{"auth_token":"bad token"}}')]]:
            self.opener.open.return_value = response
            self.error(category, lambda: self.prepared()(CODE))
        self.accept.assert_not_called()

    def test_cleanup_transport_and_downstream_errors_never_expose_secrets(self):
        error = urllib.error.HTTPError(ENDPOINT, 401, CODE, {}, io.BytesIO(TOKEN.encode()))
        error.close = Mock(side_effect=OSError(CODE + TOKEN))
        self.opener.open.side_effect = error
        self.error("initialize_transport_failed", lambda: self.prepared()(CODE))
        self.opener.open.side_effect = RuntimeError(CODE + TOKEN)
        self.error("initialize_transport_failed", lambda: self.prepared()(CODE))
        self.opener.open.side_effect = None
        self.accept.side_effect = RuntimeError(CODE + TOKEN)
        self.error("initialize_consumer_failed", lambda: self.prepared()(CODE))

    def test_invalid_material_object_cannot_leak_source_exception(self):
        class Unsafe:
            def __ne__(self, value): raise RuntimeError(CODE + TOKEN)
        def source(code, second):
            body, heads = material(code, second)
            body["js_code"] = Unsafe()
            return body, heads
        self.error("initialize_material_invalid", lambda: self.prepared(source)(CODE))
        self.factory.assert_not_called()

    def chain_settings(self):
        settings = self.parent / "supplier.json"
        write_bundle(settings, {"schema_version": 1, "ref": "1", "protocol_token": "a" * 64})
        return settings

    def supplier_response(self):
        return Response(url=SUPPLIER_ENDPOINT, body=json.dumps({"code": 0, "data": {
            "account": {"nickname": "synthetic-unneeded-account"},
            "result": {"code": CODE, "errMsg": "login:ok"}}}).encode())

    def test_supplier_to_initialization_chain_binds_code_and_preserves_results(self):
        self.opener.open.side_effect = [self.supplier_response(), self.response]
        source = Mock(side_effect=material)
        result = initialize_from_yyb(source, self.accept, settings_file=self.chain_settings(),
                                     reference_context_file=self.context)
        self.assertEqual(result, {"synthetic_only": True, "query_accepted": False})
        source.assert_called_once_with(CODE, int(NOW.timestamp()))
        self.assertEqual([c.args[0].full_url for c in self.opener.open.call_args_list],
                         [SUPPLIER_ENDPOINT, ENDPOINT])
        self.assertEqual(json.loads(self.opener.open.call_args_list[0].args[0].data)["app_id"], TARGET)
        self.assertEqual(json.loads(self.opener.open.call_args_list[1].args[0].data)["js_code"], CODE)

    def test_supplier_chain_retains_downstream_403_category_without_retry(self):
        body = io.BytesIO(TOKEN.encode())
        self.opener.open.side_effect = [self.supplier_response(),
            urllib.error.HTTPError(ENDPOINT, 403, TOKEN, {}, body)]
        self.error("initialize_access_denied", lambda: initialize_from_yyb(material, self.accept,
            settings_file=self.chain_settings(), reference_context_file=self.context))
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertTrue(body.closed)
        self.accept.assert_not_called()

    def test_supplier_not_contacted_until_consumer_preparation_is_valid(self):
        with patch("sushiwait.initialization.supply_to") as supplier:
            self.error("initialize_invalid_input", lambda: initialize_from_yyb(None, self.accept,
                settings_file=self.chain_settings(), reference_context_file=self.context))
            supplier.assert_not_called()


if __name__ == "__main__":
    unittest.main()
