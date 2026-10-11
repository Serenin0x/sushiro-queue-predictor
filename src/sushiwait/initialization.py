"""One-use SAPI initialization consumer with an explicit request source.

This module supplies no official constants, signing implementation, device
identity, version defaults, or date headers. A separately verified source must
prepare the complete request for the fresh code. Successful initialization is
not independent queue-query acceptance.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import json
import re
import threading
from types import MappingProxyType
from typing import TypeVar
import urllib.error
import urllib.request
from urllib.parse import urlencode

from .bridge import _app_id
from .credentials import CredentialError, read_credentials_file
from .yyb import supply_for_target, supply_to


ENDPOINT = "https://sapi.sushiro.com.cn/api/1.3/initialize"
MAX_RESPONSE_BYTES = 64 * 1024
_BODY_KEYS = frozenset({"long_token", "js_code", "ts", "nonce", "sign",
                        "device_id", "app_version"})
_HEAD_KEYS = frozenset({"X-App-Client", "X-App-Code", "User-Agent", "Referer",
                        "Content-Type"})
_ERRORS = frozenset({"initialize_invalid_input", "initialize_reference_invalid",
    "initialize_material_invalid", "initialize_material_expired", "initialize_source_failed", "initialize_used",
    "initialize_transport_failed", "initialize_redirect_rejected",
    "initialize_auth_rejected", "initialize_access_denied", "initialize_http_failed",
    "initialize_response_invalid", "initialize_consumer_failed"})
T = TypeVar("T")


class InitializationError(ValueError):
    """Safe error categories; no request, response, or exception text leaks."""

    def __init__(self, error_code: str):
        self.error_code = error_code if error_code in _ERRORS else "initialize_invalid_input"
        super().__init__(self.error_code)


def _text(value, maximum=4096):
    return (type(value) is str and 0 < len(value) <= maximum
            and all(32 <= ord(c) <= 126 for c in value))


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def _reject_constant(_):
    raise ValueError("nonfinite")


def _token(body):
    failure = False
    value = None
    if type(body) is not bytes or len(body) > MAX_RESPONSE_BYTES:
        raise InitializationError("initialize_response_invalid")
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        failure = True
    if failure:
        raise InitializationError("initialize_response_invalid")
    data = value.get("data") if isinstance(value, dict) else None
    token = data.get("auth_token") if isinstance(data, dict) else None
    if not _text(token, 8192) or any(ord(c) < 33 for c in token):
        raise InitializationError("initialize_response_invalid")
    return token


def _material(value, code, second, target):
    if not isinstance(value, tuple) or len(value) != 2:
        raise InitializationError("initialize_material_invalid")
    body, headers = value
    if type(body) is not dict or type(headers) is not dict:
        raise InitializationError("initialize_material_invalid")
    body, headers = dict(body), dict(headers)
    if set(body) != _BODY_KEYS or set(headers) != _HEAD_KEYS:
        raise InitializationError("initialize_material_invalid")
    ts = body["ts"]
    if (body["js_code"] != code or not _text(body["js_code"])
            or not _text(body["long_token"]) or not _text(body["device_id"])
            or not _text(body["app_version"]) or not _text(body["nonce"], 6)
            or len(body["nonce"]) != 6 or any(ord(c) < 33 for c in body["nonce"])
            or type(body["sign"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", body["sign"]) is None
            or not (type(ts) is int and ts == second
                    or type(ts) is str and ts == str(second))
            or not all(_text(v) for v in headers.values())
            or headers["Content-Type"].lower().split(";", 1)[0].strip() not in {
                "application/json", "application/x-www-form-urlencoded"}
            or _app_id(headers["Referer"]) != target):
        raise InitializationError("initialize_material_invalid")
    return body, headers


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validate_input(prepare_request, accept_session, timeout, expected_app_id,
                    *, required_target=False):
    if (not callable(prepare_request) or not callable(accept_session)
            or type(timeout) not in (int, float) or not 1 <= timeout <= 60
            or required_target and expected_app_id is None
            or expected_app_id is not None and (type(expected_app_id) is not str
                or re.fullmatch(r"wx[0-9a-f]{16}", expected_app_id) is None)):
        raise InitializationError("initialize_invalid_input")


def prepare_consumer(prepare_request: Callable, accept_session: Callable, *,
                     reference_context_file, timeout: float = 30,
                     expected_app_id: str | None = None) -> Callable[[str], T]:
    """Prepare a single-call consumer before requesting any fresh login code.

    prepare_request(code, utc_second) must return (body, five normal headers)
    from a verified request source. No historical request is replayed here.
    accept_session(token, immutable_headers) receives only the new token and
    headers, not the code or initialization body. It must separately check queue
    acceptance. A used consumer cannot retry, even after a local failure.
    """
    _validate_input(prepare_request, accept_session, timeout, expected_app_id)
    reference_failure = False
    target = None
    try:
        reference = read_credentials_file(reference_context_file, api_profile="miniapp_gateway")
        target = _app_id(reference.referer)
    except CredentialError:
        reference_failure = True
    if reference_failure or not target or expected_app_id is not None and target != expected_app_id:
        raise InitializationError("initialize_reference_invalid")
    return _prepare_for_target(prepare_request, accept_session, target, timeout)


def prepare_consumer_for_target(prepare_request: Callable, accept_session: Callable, *,
                                expected_app_id: str, timeout: float = 30) -> Callable[[str], T]:
    """Prepare a target-bound initialization without an old query credential.

    The explicit source still supplies every normal body/header field. No
    constants, signing material, dummy credentials or device defaults are
    invented. Queue acceptance/publication remains the callback's responsibility.
    """
    _validate_input(prepare_request, accept_session, timeout, expected_app_id,
                    required_target=True)
    return _prepare_for_target(prepare_request, accept_session, expected_app_id, timeout)


def _prepare_for_target(prepare_request, accept_session, target, timeout):
    used = False
    lock = threading.Lock()

    def consume(code):
        nonlocal used
        with lock:
            if used:
                raise InitializationError("initialize_used")
            used = True
        if not _text(code) or any(ord(c) < 33 for c in code):
            raise InitializationError("initialize_invalid_input")
        failure = None
        second = int(datetime.now(timezone.utc).timestamp())
        try:
            source = prepare_request(code, second)
        except Exception:
            failure = "initialize_source_failed"
        if failure:
            raise InitializationError(failure)
        try:
            body, headers = _material(source, code, second, target)
        except InitializationError as error:
            failure = error.error_code
        except Exception:
            failure = "initialize_material_invalid"
        if failure:
            raise InitializationError(failure)
        # Local freshness policy, not a claim about the upstream time tolerance.
        if not 0 <= int(datetime.now(timezone.utc).timestamp()) - second <= 5:
            raise InitializationError("initialize_material_expired")
        media_type = headers["Content-Type"].lower().split(";", 1)[0].strip()
        encoded = (json.dumps(body, ensure_ascii=True, separators=(",", ":"))
                   if media_type == "application/json" else urlencode(body))
        request = urllib.request.Request(ENDPOINT, method="POST",
            data=encoded.encode("utf-8"), headers=headers)
        status, response_body = None, None
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
            with opener.open(request, timeout=timeout) as response:
                status = response.status
                if response.geturl() != ENDPOINT:
                    failure = "initialize_redirect_rejected"
                elif response.headers.get_content_type() != "application/json":
                    failure = "initialize_response_invalid"
                elif status == 200:
                    response_body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            status = error.code
            try:
                error.close()
            except Exception:
                failure = "initialize_transport_failed"
        except Exception:
            failure = "initialize_transport_failed"
        if failure:
            raise InitializationError(failure)
        if status != 200:
            failure = {401: "initialize_auth_rejected", 403: "initialize_access_denied"}.get(
                status, "initialize_redirect_rejected" if status in (301, 302, 303, 307, 308)
                else "initialize_http_failed")
            raise InitializationError(failure)
        token = _token(response_body)
        try:
            outcome = accept_session(token, MappingProxyType(headers))
        except Exception:
            failure = "initialize_consumer_failed"
        finally:
            token = None
            response_body = None
        if failure:
            raise InitializationError(failure)
        return outcome

    consume.target_app_id = target
    return consume


def initialize_from_yyb(prepare_request: Callable, accept_session: Callable, *,
                        settings_file, reference_context_file,
                        supplier_timeout: float = 45, initialize_timeout: float = 30,
                        expected_app_id: str | None = None) -> T:
    """One supplier call followed immediately by one prepared initialization.

    Preserve the safe failing layer. Source acceptance, queue-query acceptance
    and renewal still require independent evidence. No retries or direct file
    writes; the explicit accept_session callback may separately publish a
    verified context.
    """
    consume = prepare_consumer(prepare_request, accept_session,
        reference_context_file=reference_context_file, timeout=initialize_timeout,
        expected_app_id=expected_app_id)
    return _initialize(consume, lambda checked: supply_to(checked, settings_file=settings_file,
        reference_context_file=reference_context_file, timeout=supplier_timeout,
        expected_app_id=consume.target_app_id))


def initialize_for_target_from_yyb(prepare_request: Callable, accept_session: Callable, *,
                                  settings_file, expected_app_id: str,
                                  supplier_timeout: float = 45, initialize_timeout: float = 30) -> T:
    """Explicit cold-start supply and initialization, with no reference file.

    A verified source and prepared acceptance callback are still mandatory.
    This does not publish credentials or prove an independent cloud runtime.
    """
    consume = prepare_consumer_for_target(prepare_request, accept_session,
        expected_app_id=expected_app_id, timeout=initialize_timeout)
    return _initialize(consume, lambda checked: supply_for_target(checked,
        settings_file=settings_file, expected_app_id=consume.target_app_id,
        timeout=supplier_timeout))


def _initialize(consume, supplier):
    downstream_error = None

    def checked(code):
        nonlocal downstream_error
        try:
            return consume(code)
        except InitializationError as error:
            downstream_error = error.error_code
            return None

    outcome = supplier(checked)
    if downstream_error is not None:
        raise InitializationError(downstream_error)
    return outcome
