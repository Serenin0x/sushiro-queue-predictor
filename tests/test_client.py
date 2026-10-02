"""Offline transport safety tests: no production request or real credential."""

from __future__ import annotations

import contextlib
from email.message import Message
import http.client
import io
import json
import socket
import ssl
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPSHandler, ProxyHandler, build_opener
from urllib.response import addinfourl

from sushiwait.client import SushiroClient, _RejectRedirects
from sushiwait.observations import normalize_snapshot


class FakeResponse:
    def __init__(self, body, *, status=200, headers=None, url=None, read_error=None):
        self.body = body
        self.status = status
        self.headers = headers or {}
        self.url = url
        self.read_error = read_error
        self.read_sizes = []
        self.closed = False

    def getcode(self):
        return self.status

    def geturl(self):
        return self.url

    def read(self, size):
        self.read_sizes.append(size)
        if self.read_error is not None:
            raise self.read_error
        return self.body[:size]

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        if self.response.url is None:
            self.response.url = request.full_url
        return self.response


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def fixture_store(**extra):
    return {"id": 123, "name": "合成测试门店", **extra}


class ClientTests(unittest.TestCase):
    def query(self, body, *, method="store", status=200, headers=None, url=None, limit=2097152):
        response = FakeResponse(body, status=status, headers=headers, url=url)
        opener = FakeOpener(response)
        client = SushiroClient("synthetic-test-token", opener=opener, max_response_bytes=limit)
        result = client.fetch_store("123") if method == "store" else client.fetch_stores()
        return result, response, opener

    def test_fixed_read_only_request_and_query_auth(self):
        for endpoint in ("store", "stores"):
            with self.subTest(endpoint=endpoint):
                body = fixture_store() if endpoint == "store" else [fixture_store()]
                result, response, opener = self.query(encoded(body), method=endpoint)
                self.assertTrue(result.ok)
                request, timeout = opener.calls[0]
                split = urlsplit(request.full_url)
                self.assertEqual((split.scheme, split.netloc), ("https", "crm-cn-prd.sushiro.com.cn"))
                self.assertEqual(request.get_method(), "GET")
                self.assertIsNone(request.data)
                self.assertEqual(timeout, 15)
                self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-test-token")
                self.assertNotIn("synthetic-test-token", request.full_url)
                self.assertIsNone(request.get_header("Content-type"))
                if endpoint == "store":
                    self.assertEqual(split.path, "/wechat/api/2.0/getStoreById")
                    self.assertEqual(parse_qs(split.query), {"storeId": ["123"]})
                else:
                    self.assertEqual(split.path, "/wechat/api/2.0/stores")
                    self.assertEqual(parse_qs(split.query), {"latitude": ["1"], "longitude": ["1"], "numresults": ["10000"]})
                self.assertEqual(len(opener.calls), 1)
                self.assertTrue(response.closed)

    def test_gateway_detail_uses_only_observed_route_and_local_headers(self):
        opener = FakeOpener(FakeResponse(encoded(fixture_store())))
        client = SushiroClient(
            "synthetic-gateway-authorization",
            api_profile="miniapp_gateway",
            app_client="synthetic-app-client",
            app_code="synthetic-app-code",
            user_agent="Synthetic Agent/1.0 (offline test)",
            referer="https://synthetic.invalid/private-reference",
            content_type="application/x-www-form-urlencoded; charset=UTF-8",
            opener=opener,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = client.fetch_store("123")
        self.assertTrue(result.ok)
        request, timeout = opener.calls[0]
        self.assertEqual(
            request.full_url,
            "https://sapi.sushiro.com.cn/gateway/wechat/api/2.0/getStoreById?storeId=123",
        )
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(timeout, 15)
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-gateway-authorization")
        self.assertEqual(request.get_header("X-app-client"), "synthetic-app-client")
        self.assertEqual(request.get_header("X-app-code"), "synthetic-app-code")
        self.assertEqual(request.get_header("User-agent"), "Synthetic Agent/1.0 (offline test)")
        self.assertEqual(request.get_header("Referer"), "https://synthetic.invalid/private-reference")
        self.assertEqual(request.get_header("Content-type"), "application/x-www-form-urlencoded; charset=UTF-8")
        self.assertEqual(client.api_profile, "miniapp_gateway")
        with self.assertRaises(AttributeError):
            client.api_profile = "legacy"
        self.assertEqual(output.getvalue(), "")
        for secret in (
            "synthetic-gateway-authorization", "synthetic-app-client", "synthetic-app-code",
            "Synthetic Agent/1.0", "private-reference", "charset=UTF-8",
        ):
            self.assertNotIn(secret, repr(result))
            self.assertNotIn(secret, repr(client))
            self.assertNotIn(secret, request.full_url)

    def test_gateway_directory_is_unsupported_without_any_request(self):
        opener = FakeOpener()
        client = SushiroClient(None, api_profile="miniapp_gateway", opener=opener)
        result = client.fetch_stores()
        self.assertEqual((result.ok, result.error_code, result.http_status, result.payload), (False, "unsupported_endpoint", None, None))
        self.assertEqual(opener.calls, [])
        self.assertEqual(client._fetch("cancelNetTicket", {}).error_code, "invalid_endpoint")
        self.assertEqual(opener.calls, [])

    def test_gateway_http_failure_has_no_legacy_fallback_or_raw_output(self):
        opener = FakeOpener(error=HTTPError(
            "https://sapi.sushiro.com.cn", 401, "synthetic-private-message",
            {"Authorization": "synthetic-private-header"},
            io.BytesIO(b"synthetic-private-response-body"),
        ))
        client = SushiroClient(None, api_profile="miniapp_gateway", opener=opener)
        result = client.fetch_store("123")
        self.assertEqual((result.error_code, result.http_status, result.payload), ("http_error", 401, None))
        self.assertEqual(len(opener.calls), 1)
        request = opener.calls[0][0]
        self.assertEqual(urlsplit(request.full_url).netloc, "sapi.sushiro.com.cn")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIsNone(request.get_header("X-app-client"))
        self.assertIsNone(request.get_header("X-app-code"))
        self.assertIsNone(request.get_header("User-agent"))
        self.assertIsNone(request.get_header("Referer"))
        self.assertIsNone(request.get_header("Content-type"))
        self.assertNotIn("synthetic-private", repr(result))

    def test_api_profiles_reject_arbitrary_destinations_and_legacy_app_headers(self):
        for profile in ("unknown", "https://other.invalid", "../getStoreById", None, []):
            with self.subTest(profile_type=type(profile).__name__):
                with self.assertRaisesRegex(ValueError, "^invalid_api_profile$"):
                    SushiroClient(None, api_profile=profile)
        for headers in (
            {"app_client": "synthetic-client"}, {"app_code": "synthetic-code"},
            {"user_agent": "Synthetic Agent/1.0"},
            {"referer": "https://synthetic.invalid"},
            {"content_type": "application/x-www-form-urlencoded"},
        ):
            with self.subTest(header=next(iter(headers))):
                with self.assertRaisesRegex(ValueError, "^unsupported_profile_header$"):
                    SushiroClient(None, **headers)

    def test_gateway_app_header_validation_rejects_whitespace_and_controls(self):
        bad_values = ("", "bad\r\nInjected: synthetic", "two tokens", " padded", "tab\tvalue", "秘密", "bad\x00value", "x" * 1025, True)
        for field in ("app_client", "app_code"):
            for value in bad_values:
                with self.subTest(field=field, value_type=type(value).__name__):
                    with self.assertRaisesRegex(ValueError, "^invalid_" + field + "$"):
                        SushiroClient(None, api_profile="miniapp_gateway", **{field: value})

    def test_gateway_header_text_is_bounded_ascii_without_control_characters(self):
        for field, limit in (("user_agent", 2048), ("referer", 2048), ("content_type", 256)):
            for value in ("", " ", "bad\r\nInjected: synthetic", "bad\tvalue", "秘密", "bad\x7fvalue", "x" * (limit + 1), True):
                with self.subTest(field=field, value_type=type(value).__name__):
                    with self.assertRaisesRegex(ValueError, "^invalid_" + field + "$"):
                        SushiroClient(None, api_profile="miniapp_gateway", **{field: value})
        with self.assertRaisesRegex(ValueError, "^invalid_referer$"):
            SushiroClient(None, api_profile="miniapp_gateway", referer="https://synthetic.invalid/two words")

    def test_default_gateway_opener_does_not_invent_user_agent(self):
        class CapturingHTTPSHandler(HTTPSHandler):
            def __init__(self):
                super().__init__()
                self.request = None

            def https_open(self, request):
                self.request = request
                response = addinfourl(io.BytesIO(encoded(fixture_store())), Message(), request.full_url, 200)
                response.msg = "OK"
                return response

        handler = CapturingHTTPSHandler()
        with patch("sushiwait.client.HTTPSHandler", return_value=handler):
            client = SushiroClient(None, api_profile="miniapp_gateway")
            self.assertTrue(client.fetch_store("123").ok)
        self.assertIsNotNone(handler.request)
        self.assertIsNone(handler.request.get_header("User-agent"))
        self.assertIsNone(handler.request.get_header("Referer"))
        self.assertIsNone(handler.request.get_header("Content-type"))

    def test_gateway_redirect_to_legacy_is_blocked_and_not_read(self):
        response = FakeResponse(b"synthetic-private-body", url="https://crm-cn-prd.sushiro.com.cn/wechat/api/2.0/getStoreById?storeId=123")
        opener = FakeOpener(response)
        result = SushiroClient(None, api_profile="miniapp_gateway", opener=opener).fetch_store("123")
        self.assertEqual(result.error_code, "redirect_blocked")
        self.assertEqual(response.read_sizes, [])
        self.assertEqual(len(opener.calls), 1)
        self.assertTrue(response.closed)

    def test_anonymous_diagnostic_omits_authorization(self):
        opener = FakeOpener(error=HTTPError("https://fixed.invalid", 401, "private error", {}, io.BytesIO(b"private body")))
        client = SushiroClient(None, opener=opener)
        result = client.fetch_stores()
        self.assertIsNone(opener.calls[0][0].get_header("Authorization"))
        self.assertFalse(result.ok)
        self.assertEqual((result.error_code, result.http_status, result.payload), ("http_error", 401, None))
        self.assertEqual(len(opener.calls), 1)

    def test_anonymous_success_can_be_sampled_again_by_caller(self):
        opener = FakeOpener(FakeResponse(encoded(fixture_store())))
        client = SushiroClient(None, opener=opener)
        self.assertTrue(client.fetch_store("123").ok)
        self.assertTrue(client.fetch_store("123").ok)
        self.assertEqual(len(opener.calls), 2)
        self.assertTrue(all(request.get_header("Authorization") is None for request, _ in opener.calls))

    def test_tls_verification_failures_have_distinct_safe_error(self):
        for error in (
            ssl.SSLCertVerificationError(1, "private certificate details"),
            URLError(ssl.SSLCertVerificationError(1, "private certificate details")),
        ):
            with self.subTest(error_type=type(error).__name__):
                opener = FakeOpener(error=error)
                result = SushiroClient(None, opener=opener).fetch_stores()
                self.assertEqual((result.error_code, result.http_status, result.payload), ("tls_verification_failed", None, None))
                self.assertNotIn("private certificate details", repr(result))
                self.assertEqual(len(opener.calls), 1)
        for error in (
            ssl.SSLError(1, "private handshake details"),
            URLError(ssl.SSLError(1, "private handshake details")),
        ):
            with self.subTest(error_type=type(error).__name__):
                result = SushiroClient(None, opener=FakeOpener(error=error)).fetch_stores()
                self.assertEqual(result.error_code, "tls_error")
                self.assertIsNone(result.payload)

    def test_ca_configuration_preserves_certificate_and_hostname_checks(self):
        # Mock the path-based load: no certificate file is read by this test.
        context = ssl.create_default_context()
        for ca_file in (None, "synthetic-trust-roots.pem"):
            with self.subTest(custom_roots=ca_file is not None):
                with patch("sushiwait.client.ssl.create_default_context", return_value=context) as factory:
                    client = SushiroClient(None, ca_file=ca_file)
                factory.assert_called_once_with(cafile=ca_file)
                handlers = [handler for handler in client._opener.handlers if isinstance(handler, HTTPSHandler)]
                self.assertEqual(len(handlers), 1)
                self.assertIs(handlers[0]._context, context)
                self.assertTrue(context.check_hostname)
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        with self.assertRaises(TypeError):
            SushiroClient(None, verify=False)

    def test_invalid_ca_config_is_safe_and_has_no_insecure_fallback(self):
        for ca_file in ("", "bad\x00path", 1):
            with self.subTest(path_type=type(ca_file).__name__):
                with self.assertRaisesRegex(ValueError, "^invalid_ca_file$"):
                    SushiroClient(None, ca_file=ca_file)
        with patch("sushiwait.client.ssl.create_default_context", side_effect=FileNotFoundError("private path")) as factory:
            with self.assertRaisesRegex(ValueError, "^invalid_ca_file$"):
                SushiroClient(None, ca_file="synthetic-missing-roots.pem")
        self.assertEqual(factory.call_count, 1)

    def test_authorization_normalizes_bearer_and_rejects_header_injection(self):
        opener = FakeOpener(FakeResponse(encoded(fixture_store())))
        SushiroClient(" bearer synthetic-test-token ", opener=opener).fetch_store("123")
        self.assertEqual(opener.calls[0][0].get_header("Authorization"), "Bearer synthetic-test-token")
        for token in ("bad\r\nInjected: secret", "", "Bearer", "two tokens", "秘密", 7):
            with self.subTest(token_type=type(token).__name__):
                with self.assertRaisesRegex(ValueError, "^invalid_authorization$"):
                    SushiroClient(token)

    def test_invalid_store_ids_and_private_endpoints_never_open(self):
        opener = FakeOpener()
        client = SushiroClient(None, opener=opener)
        for store_id in (None, 123, "", "0", "-1", "../cancelNetTicket", "123&token=secret", "https://other.invalid", "１２３", "9" * 30):
            with self.subTest(value_type=type(store_id).__name__):
                self.assertEqual(client.fetch_store(store_id).error_code, "invalid_store_id")
        self.assertEqual(client._fetch("createNetTicket", {}).error_code, "invalid_endpoint")
        self.assertEqual(opener.calls, [])

    def test_redirects_are_blocked_without_reading_body(self):
        for status, url in ((302, None), (307, None), (200, "https://other.invalid/collect")):
            with self.subTest(status=status):
                result, response, opener = self.query(b"secret-body", status=status, url=url)
                self.assertEqual(result.error_code, "redirect_blocked")
                self.assertIsNone(result.payload)
                self.assertEqual(response.read_sizes, [])
                self.assertEqual(len(opener.calls), 1)

    def test_actual_urllib_redirect_handler_does_not_make_second_request(self):
        class SyntheticHTTPSHandler(HTTPSHandler):
            def __init__(self, status):
                super().__init__()
                self.calls = 0
                self.status = status
                self.body = io.BytesIO(b"sensitive redirect body")

            def https_open(self, request):
                self.calls += 1
                if self.calls > 1:
                    raise AssertionError("redirect destination must not be opened")
                headers = Message()
                headers["Location"] = "https://other.invalid/token"
                response = addinfourl(self.body, headers, request.full_url, self.status)
                response.msg = "redirect"
                return response

        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                handler = SyntheticHTTPSHandler(status)
                opener = build_opener(ProxyHandler({}), handler, _RejectRedirects())
                result = SushiroClient("synthetic-test-token", opener=opener).fetch_stores()
                self.assertEqual((result.error_code, result.http_status), ("redirect_blocked", status))
                self.assertEqual(handler.calls, 1)
                self.assertTrue(handler.body.closed)

    def test_http_failures_have_safe_status_and_no_payload(self):
        for status in (401, 403, 404, 429, 500, 503):
            with self.subTest(status=status):
                result, response, opener = self.query(b"token=synthetic-test-token private-body", status=status)
                self.assertEqual((result.error_code, result.http_status, result.payload), ("http_error", status, None))
                self.assertEqual(response.read_sizes, [])
                self.assertEqual(len(opener.calls), 1)

    def test_size_limit_applies_without_or_despite_content_length(self):
        for headers in ({}, {"Content-Length": "bad"}, {"Content-Length": "2"}):
            with self.subTest(headers=headers):
                result, response, _ = self.query(b"x" * 100, headers=headers, limit=32)
                self.assertEqual(result.error_code, "response_too_large")
                self.assertEqual(response.read_sizes, [33])
        result, response, _ = self.query(b"x", headers={"Content-Length": "33"}, limit=32)
        self.assertEqual(result.error_code, "response_too_large")
        self.assertEqual(response.read_sizes, [])
        raw = encoded(fixture_store())
        result, _, _ = self.query(raw, limit=len(raw))
        self.assertTrue(result.ok)

    def test_network_errors_do_not_leak_details_or_retry(self):
        failures = ((socket.timeout("secret-token"), "timeout"), (URLError(socket.timeout("secret-token")), "timeout"), (URLError("private host secret-token"), "network_error"), (OSError("secret-token"), "network_error"), (http.client.IncompleteRead(b"private body"), "network_error"))
        for error, code in failures:
            with self.subTest(error_type=type(error).__name__):
                opener = FakeOpener(error=error)
                output = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    result = SushiroClient("secret-token", opener=opener).fetch_store("123")
                self.assertEqual(result.error_code, code)
                self.assertIsNone(result.http_status)
                self.assertIsNone(result.payload)
                self.assertNotIn("secret-token", repr(result))
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(len(opener.calls), 1)

    def test_body_read_timeout_keeps_known_status_and_closes_response(self):
        response = FakeResponse(b"", read_error=socket.timeout("private-token"))
        result = SushiroClient(None, opener=FakeOpener(response)).fetch_store("123")
        self.assertEqual((result.error_code, result.http_status), ("timeout", 200))
        self.assertIsNone(result.payload)
        self.assertTrue(response.closed)

    def test_json_errors_and_nonstandard_constants_are_not_empty_success(self):
        bodies = (b"", b"<html>private body</html>", b"\xff", b'{"id":123,"name":"test","wait":NaN}', b'{"id":123,"id":456,"name":"test"}')
        for body in bodies:
            with self.subTest(body_length=len(body)):
                result, _, _ = self.query(body)
                self.assertEqual((result.error_code, result.http_status, result.payload), ("invalid_json", 200, None))

    def test_business_failure_overrides_valid_store_shape(self):
        fields = ({"success": False}, {"success": "true"}, {"error": "private detail"}, {"code": 401}, {"code": "NEW_UNKNOWN_SUCCESS_CODE"}, {"errorCode": 500}, {"error_code": True}, {"errCode": []}, {"code": 200.0})
        for field in fields:
            with self.subTest(field_name=next(iter(field))):
                result, _, _ = self.query(encoded(fixture_store(**field)))
                self.assertEqual((result.error_code, result.payload), ("business_error", None))
        result, _, _ = self.query(encoded({"code": "OK", "data": fixture_store(errorCode=500)}))
        self.assertEqual(result.error_code, "business_error")

    def test_known_business_success_codes_preserve_entire_response(self):
        for code in (None, "", 0, "0", 200, "200", "OK", "SUCCESS"):
            with self.subTest(code=code):
                payload = {"code": code, "success": True, "data": fixture_store(groupQueues={"mixedQueue": ["synthetic-1"]}, unknownField={"kept": True})}
                result, _, _ = self.query(encoded(payload))
                self.assertTrue(result.ok)
                self.assertEqual(result.payload, payload)
                self.assertNotIn("wait", result.payload["data"])

    def test_response_shape_and_store_identity_must_match(self):
        values = ({}, [], None, "message", {"message": "not a store"}, {"id": 123}, {"id": True, "name": "test"}, {"id": 123, "name": " "}, {"id": 123, "storeId": 456, "name": "test"}, {"data": None}, {"data": []})
        for value in values:
            with self.subTest(value_type=type(value).__name__):
                result, _, _ = self.query(encoded(value))
                self.assertEqual((result.error_code, result.payload), ("invalid_response", None))
        result, _, _ = self.query(encoded({"id": 456, "name": "test"}))
        self.assertEqual(result.error_code, "store_id_mismatch")
        result, _, _ = self.query(encoded({"storeId": "123", "name": "test"}))
        self.assertTrue(result.ok)

    def test_bare_identity_wins_over_unknown_data_in_client_and_snapshot(self):
        unknown_data = (
            None,
            [],
            {
                "id": 456,
                "name": "其他合成门店",
                "wait": 999,
                "error": "SYNTHETIC_PRIVATE_NESTED_VALUE",
            },
        )
        for identity in ({"id": 123}, {"storeId": "123"}):
            for nested in unknown_data:
                with self.subTest(identity_key=next(iter(identity)), nested_type=type(nested).__name__):
                    bare = {
                        **identity,
                        "name": "合成测试门店",
                        "wait": 4,
                        "groupQueues": {"mixedQueue": ["SYNTHETIC-001"]},
                        "data": nested,
                    }
                    snapshots = []
                    for payload in (bare, {"code": 200, "data": bare}):
                        result, _, _ = self.query(encoded(payload))
                        self.assertTrue(result.ok)
                        self.assertEqual(result.payload, payload)
                        snapshot = normalize_snapshot(
                            result.payload,
                            "123",
                            request_started_at=result.started_at,
                            received_at=result.received_at,
                            elapsed_ms=result.elapsed_ms,
                            data_origin="synthetic",
                        )
                        self.assertEqual(snapshot["normalized"]["raw_wait"]["value"], 4)
                        self.assertEqual(
                            snapshot["normalized"]["groupQueues"]["groups"]["mixedQueue"]["value"],
                            ["SYNTHETIC-001"],
                        )
                        self.assertNotIn("SYNTHETIC_PRIVATE_NESTED_VALUE", json.dumps(snapshot))
                        snapshots.append(snapshot)
                    self.assertEqual(snapshots[0]["normalized"], snapshots[1]["normalized"])
                    self.assertEqual(snapshots[0]["content_hash"], snapshots[1]["content_hash"])

    def test_nested_valid_identity_cannot_override_wrong_bare_identity(self):
        result, _, _ = self.query(encoded({
            "id": 456, "name": "其他合成门店", "data": fixture_store(),
        }))
        self.assertEqual((result.error_code, result.payload), ("store_id_mismatch", None))
        result, _, _ = self.query(encoded({
            "storeId": None, "name": "未知合成门店", "data": fixture_store(),
        }))
        self.assertEqual((result.error_code, result.payload), ("invalid_response", None))

    def test_directory_wrapping_preservation_and_invalid_elements(self):
        for value in ([fixture_store()], {"code": "OK", "data": [fixture_store()], "sourceTime": "synthetic"}, []):
            with self.subTest(value_type=type(value).__name__):
                result, _, _ = self.query(encoded(value), method="stores")
                self.assertTrue(result.ok)
                self.assertEqual(result.payload, {"data": value} if isinstance(value, list) else value)
        for value in ({}, {"data": None}, [None], ["store"], [{"name": "missing id"}], [fixture_store(), {"id": 5}]):
            with self.subTest(value_type=type(value).__name__):
                result, _, _ = self.query(encoded(value), method="stores")
                self.assertEqual((result.error_code, result.payload), ("invalid_response", None))

    def test_result_timestamps_and_safe_configuration_limits(self):
        result, _, _ = self.query(encoded(fixture_store()))
        self.assertTrue(result.started_at.endswith("Z"))
        self.assertTrue(result.received_at.endswith("Z"))
        self.assertGreaterEqual(result.elapsed_ms, 0)
        self.assertNotIn("合成测试门店", repr(result))
        for timeout in (0, -1, 16, float("inf"), float("nan"), True, "15"):
            with self.subTest(timeout_type=type(timeout).__name__):
                with self.assertRaisesRegex(ValueError, "^invalid_timeout$"):
                    SushiroClient(None, timeout_seconds=timeout)
        for limit in (0, -1, 2097153, True, 2.0):
            with self.subTest(limit_type=type(limit).__name__):
                with self.assertRaisesRegex(ValueError, "^invalid_response_limit$"):
                    SushiroClient(None, max_response_bytes=limit)


if __name__ == "__main__":
    unittest.main()
