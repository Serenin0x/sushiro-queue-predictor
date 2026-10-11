"""Synthetic supplier responses only; no account or external code request."""

import contextlib
from email.message import Message
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

from sushiwait.yyb import ENDPOINT, MAX_RESPONSE_BYTES, YYBCodeError, supply_for_target, supply_to
from tests.test_credentials import gateway_bundle, write_bundle


TOKEN = "a" * 64
TARGET = "wx0000000000000000"
CODE = "synthetic-new-code-secret"


def success():
    return {"code": 0, "msg": "success", "data": {
        "openid": "synthetic-account-secret",
        "account": {"nickname": "synthetic-nickname-secret"},
        "result": {"code": CODE, "errMsg": "login:ok"},
    }}


class Response:
    def __init__(self, value=None, *, body=None, url=ENDPOINT, content_type="application/json"):
        self.body = body if body is not None else json.dumps(success() if value is None else value).encode()
        self.url, self.status = url, 200
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.closed, self.limits = False, []

    def __enter__(self): return self
    def __exit__(self, *args): self.closed = True
    def geturl(self): return self.url
    def read(self, limit):
        self.limits.append(limit)
        return self.body[:limit]


@unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "POSIX private supplier files")
class YYBTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name).resolve()
        self.parent.chmod(0o700)
        self.settings = self.parent / "settings.json"
        self.context = self.parent / "context.json"
        self.settings_value = {"schema_version": 1, "ref": "1", "protocol_token": TOKEN}
        self.reference = gateway_bundle()
        self.reference["referer"] = f"https://servicewechat.com/{TARGET}/181/page-frame.html"
        write_bundle(self.settings, self.settings_value)
        write_bundle(self.context, self.reference)
        self.response = Response()
        self.opener = Mock()
        self.opener.open.return_value = self.response
        self.factory = patch("sushiwait.yyb.urllib.request.build_opener", return_value=self.opener).start()
        self.addCleanup(patch.stopall)
        self.consumer = Mock(return_value={"initialized": False, "synthetic_only": True})

    def call(self, consumer=None, **kwargs):
        return supply_to(self.consumer if consumer is None else consumer,
            settings_file=self.settings, reference_context_file=self.context, **kwargs)

    def error(self, code, function=None):
        with self.assertRaises(YYBCodeError) as raised:
            (function or self.call)()
        error = raised.exception
        self.assertEqual(error.error_code, code)
        self.assertIsNone(error.__context__)
        self.assertIsNone(error.__cause__)
        for secret in (TOKEN, CODE, "synthetic-account-secret", "synthetic-nickname-secret", str(self.settings)):
            self.assertNotIn(secret, repr(error))

    def test_one_prepared_consumer_receives_only_code_and_no_files_or_output_change(self):
        before = {p.name: p.read_bytes() for p in self.parent.iterdir()}
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            result = self.call()
        self.assertEqual(result, {"initialized": False, "synthetic_only": True})
        self.consumer.assert_called_once_with(CODE)
        self.assertEqual(out.getvalue(), "")
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data), {"ref": "1", "app_id": TARGET})
        self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertNotIn(self.reference["authorization"], str(request.header_items()))
        self.assertNotIn("Cookie", dict(request.header_items()))
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertEqual(self.opener.open.call_args.kwargs, {"timeout": 45})
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.parent.iterdir()})
        self.assertTrue(self.response.closed)
        self.assertEqual(self.response.limits, [MAX_RESPONSE_BYTES + 1])
        proxy, redirect = self.factory.call_args.args
        self.assertEqual(proxy.proxies, {})
        self.assertIsNone(redirect.redirect_request(None, None, 302, "", {}, "https://other.invalid"))

    def test_no_consumer_or_invalid_timeout_stops_before_private_reads_and_network(self):
        with patch("sushiwait.yyb._read_private_file", side_effect=AssertionError("no read")):
            self.error("yyb_consumer_missing", lambda: supply_to(None,
                settings_file=self.settings, reference_context_file=self.context))
            for value in (False, 0, 61, float("nan"), float("inf"), "45"):
                self.error("yyb_invalid_input", lambda: self.call(timeout=value))
        self.factory.assert_not_called()

    def test_private_settings_reject_unsafe_files_duplicates_and_arbitrary_destination(self):
        self.settings.chmod(0o644)
        self.error("yyb_private_config_invalid")
        self.settings.chmod(0o600)
        variants = [
            {**self.settings_value, "url": "https://other.invalid"},
            {**self.settings_value, "schema_version": True},
            {**self.settings_value, "protocol_token": "bad\nsecret"},
            *[{**self.settings_value, "ref": v} for v in (True, 1, "0", "01", "１", "1\n", str(2**63))],
        ]
        for value in variants:
            with self.subTest(value=value):
                write_bundle(self.settings, value)
                self.error("yyb_private_config_invalid")
        self.settings.write_bytes(b'{"schema_version":1,"schema_version":1}')
        self.error("yyb_private_config_invalid")
        self.factory.assert_not_called()

    def test_reference_must_be_complete_private_gateway_context_with_known_app_identity(self):
        for value in ("https://other.invalid", None, f"https://servicewechat.com/{TARGET}/181/page-frame.html?extra=1"):
            write_bundle(self.context, {**self.reference, "referer": value})
            self.error("yyb_target_reference_invalid")
        write_bundle(self.context, {**self.reference, "app_code": None})
        self.error("yyb_target_reference_invalid")
        write_bundle(self.context, self.reference)
        self.context.chmod(0o644)
        self.error("yyb_target_reference_invalid")
        self.factory.assert_not_called()

    def test_expected_target_prevents_reference_change_between_preparation_and_supply(self):
        self.error("yyb_target_reference_invalid", lambda: self.call(
            expected_app_id="wx1111111111111111"))
        for target in (True, "wxnot-an-app", "wx0000000000000000\n"):
            self.error("yyb_invalid_input", lambda: self.call(expected_app_id=target))
        self.factory.assert_not_called()
        self.assertEqual(self.call(expected_app_id=TARGET),
                         {"initialized": False, "synthetic_only": True})

    def test_wrapped_success_and_supplier_status_required_without_code_shape_assumptions(self):
        cases = [
            {"result": {"code": CODE, "errMsg": "login:ok"}},
            {**success(), "code": False},
            {**success(), "code": 1},
            {"code": 0, "data": {"result": {"code": CODE, "errMsg": "login:fail"}}},
            *[{"code": 0, "data": {"result": {"code": v, "errMsg": "login:ok"}}}
              for v in (None, 1, "", "bad code", "x\nsecret", "x" * 4097)],
        ]
        for value in cases:
            with self.subTest(value=value):
                self.opener.open.return_value = Response(value)
                self.error("yyb_invalid_response")
        self.consumer.assert_not_called()

    def test_duplicate_invalid_and_oversized_json_do_not_reach_consumer_or_leak(self):
        for body in (b'{"code":0,"code":0}', b'{"code":NaN}', b'not-json-secret', b'\xff',
                     b"x" * (MAX_RESPONSE_BYTES + 2)):
            self.opener.open.return_value = Response(body=body)
            self.error("yyb_invalid_response")
        self.consumer.assert_not_called()

    def test_http_categories_redirect_and_reauth_never_retry_or_read_remote_message(self):
        statuses = {401: "yyb_local_auth_rejected", 409: "yyb_reauthorization_required",
            502: "yyb_protocol_failed", 403: "yyb_http_failed", 302: "yyb_redirect_rejected"}
        for status, category in statuses.items():
            self.opener.open.reset_mock()
            body = io.BytesIO((CODE + TOKEN).encode())
            self.opener.open.side_effect = urllib.error.HTTPError(ENDPOINT, status, CODE, {}, body)
            self.error(category)
            self.assertTrue(body.closed)
            self.opener.open.assert_called_once()
        self.consumer.assert_not_called()

    def test_changed_response_origin_and_wrong_content_type_are_rejected(self):
        self.opener.open.return_value = Response(url="http://127.0.0.1:18082/wxapp/getCode")
        self.error("yyb_redirect_rejected")
        self.opener.open.return_value = Response(content_type="text/html")
        self.error("yyb_invalid_response")
        self.consumer.assert_not_called()

    def test_failed_http_error_cleanup_cannot_expose_remote_secret_exception(self):
        error = urllib.error.HTTPError(ENDPOINT, 401, CODE, {}, io.BytesIO(TOKEN.encode()))
        error.close = Mock(side_effect=OSError(CODE + TOKEN))
        self.opener.open.side_effect = error
        self.error("yyb_transport_failed")
        self.consumer.assert_not_called()
        self.opener.open.assert_called_once()

    def test_transport_and_consumer_exceptions_discard_raw_secret_context(self):
        self.opener.open.side_effect = OSError(CODE + TOKEN)
        self.error("yyb_transport_failed")
        self.consumer.assert_not_called()
        self.opener.open.side_effect = None
        self.consumer.side_effect = RuntimeError(CODE + TOKEN)
        self.error("yyb_consumer_failed")
        self.consumer.assert_called_once_with(CODE)
        self.assertEqual(self.opener.open.call_count, 2)


@unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "POSIX private supplier files")
class TargetSupplierTests(unittest.TestCase):
    error = YYBTests.error

    def setUp(self):
        YYBTests.setUp(self)
        self.context.unlink()
        self.settings_value = {"schema_version": 2, "ref": "1",
                               "protocol_token": TOKEN, "app_id": TARGET}
        write_bundle(self.settings, self.settings_value)

    def call(self, consumer=None, **kwargs):
        options = {"expected_app_id": TARGET, **kwargs}
        return supply_for_target(self.consumer if consumer is None else consumer,
                                 settings_file=self.settings, **options)

    def test_no_reference_credential_read_or_file_created(self):
        before = self.settings.read_bytes()
        with patch("sushiwait.yyb.read_credentials_file", side_effect=AssertionError("no reference read")):
            result = self.call()
        self.assertEqual(result, {"initialized": False, "synthetic_only": True})
        self.consumer.assert_called_once_with(CODE)
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(json.loads(request.data), {"ref": "1", "app_id": TARGET})
        self.assertEqual(list(self.parent.iterdir()), [self.settings])
        self.assertEqual(self.settings.read_bytes(), before)
        self.opener.open.assert_called_once()

    def test_caller_target_is_required_and_valid_before_private_read(self):
        with patch("sushiwait.yyb._read_private_file", side_effect=AssertionError("no settings read")):
            for target in (None, True, "wxwrong", TARGET + "\n"):
                self.error("yyb_invalid_input", lambda: self.call(expected_app_id=target))
            self.error("yyb_consumer_missing", lambda: supply_for_target(None,
                       settings_file=self.settings, expected_app_id=TARGET))
            for timeout in (False, 0, 61, float("nan"), float("inf")):
                self.error("yyb_invalid_input", lambda: self.call(timeout=timeout))
        self.factory.assert_not_called()

    def test_private_target_and_caller_pin_must_match_before_network(self):
        self.error("yyb_target_mismatch", lambda: self.call(expected_app_id="wx1111111111111111"))
        self.factory.assert_not_called()
        self.consumer.assert_not_called()

    def test_strict_schema_does_not_accept_old_config_or_destination_override(self):
        for value in (
            {**self.settings_value, "schema_version": 1},
            {**self.settings_value, "schema_version": True},
            {**self.settings_value, "url": "https://other.invalid"},
            {key: val for key, val in self.settings_value.items() if key != "app_id"},
            *[{**self.settings_value, "app_id": val} for val in (None, True, TARGET + "\n")],
            {**self.settings_value, "ref": "0"},
            {**self.settings_value, "protocol_token": "b" * 63},
        ):
            write_bundle(self.settings, value)
            self.error("yyb_private_config_invalid")
        write_bundle(self.settings, self.settings_value)
        self.settings.chmod(0o644)
        self.error("yyb_private_config_invalid")
        self.factory.assert_not_called()

    def test_remote_reauthorization_stops_once_without_callback_or_secret_error(self):
        body = io.BytesIO((CODE + TOKEN).encode())
        self.opener.open.side_effect = urllib.error.HTTPError(ENDPOINT, 409, CODE, {}, body)
        self.error("yyb_reauthorization_required")
        self.assertTrue(body.closed)
        self.opener.open.assert_called_once()
        self.consumer.assert_not_called()

    def test_consumer_exception_is_sanitized_and_not_retried(self):
        self.consumer.side_effect = RuntimeError(CODE + TOKEN)
        self.error("yyb_consumer_failed")
        self.opener.open.assert_called_once()
        self.consumer.assert_called_once_with(CODE)
