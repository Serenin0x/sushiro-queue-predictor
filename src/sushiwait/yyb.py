"""Experimental loopback-only code supplier, not a Sushiro login adapter.

An explicit consumer must be ready before requesting a one-use code. The target
comes from a private reference or an explicitly registered, caller-bound target.
No account discovery,
credential promotion, initialization signing, retry, or background job occurs.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Callable, TypeVar
import urllib.error
import urllib.request

from .bridge import _app_id
from .credentials import CredentialError, _read_private_file, read_credentials_file


ENDPOINT = "http://127.0.0.1:18081/wxapp/getCode"
MAX_RESPONSE_BYTES = 16 * 1024
T = TypeVar("T")
_ERRORS = frozenset({
    "yyb_consumer_missing", "yyb_invalid_input", "yyb_private_config_invalid",
    "yyb_target_reference_invalid", "yyb_target_mismatch", "yyb_transport_failed", "yyb_redirect_rejected",
    "yyb_local_auth_rejected", "yyb_reauthorization_required", "yyb_protocol_failed",
    "yyb_http_failed", "yyb_invalid_response", "yyb_consumer_failed",
})


class YYBCodeError(ValueError):
    """Fixed categories only; remote message, account and code never escape."""

    def __init__(self, error_code: str):
        self.error_code = error_code if error_code in _ERRORS else "yyb_invalid_input"
        super().__init__(self.error_code)


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise YYBCodeError("yyb_invalid_response")
        value[key] = item
    return value


def _reject_constant(_):
    raise ValueError("nonfinite")


def _decode(body: bytes, error_code: str):
    failed = False
    value = None
    if type(body) is not bytes or len(body) > MAX_RESPONSE_BYTES:
        raise YYBCodeError(error_code)
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        failed = True
    if failed:
        raise YYBCodeError(error_code)
    return value


def _code(body: bytes) -> str:
    value = _decode(body, "yyb_invalid_response")
    if not isinstance(value, dict) or type(value.get("code")) is not int or value["code"] != 0:
        raise YYBCodeError("yyb_invalid_response")
    data = value.get("data")
    result = data.get("result") if isinstance(data, dict) else None
    code = result.get("code") if isinstance(result, dict) else None
    if (not isinstance(result, dict) or result.get("errMsg") != "login:ok"
            or type(code) is not str or not 1 <= len(code) <= 4096
            or any(ord(char) < 33 or ord(char) > 126 for char in code)):
        raise YYBCodeError("yyb_invalid_response")
    # login:ok is a supplier status, not Sushiro acceptance or verified AppID binding.
    return code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validate_input(consumer, timeout, expected_app_id, *, required_target=False):
    if not callable(consumer):
        raise YYBCodeError("yyb_consumer_missing")
    if type(timeout) not in (int, float) or not 1 <= timeout <= 60:
        raise YYBCodeError("yyb_invalid_input")
    if (required_target and expected_app_id is None
            or expected_app_id is not None and (type(expected_app_id) is not str
            or re.fullmatch(r"wx[0-9a-f]{16}", expected_app_id) is None)):
        raise YYBCodeError("yyb_invalid_input")


def _settings(settings_file, schema):
    failure = None
    try:
        raw = _read_private_file(settings_file)
    except CredentialError:
        failure = "yyb_private_config_invalid"
    if failure:
        raise YYBCodeError(failure)
    settings = _decode(raw, "yyb_private_config_invalid")
    keys = {"schema_version", "protocol_token", "ref"}
    if schema == 2:
        keys.add("app_id")
    if (not isinstance(settings, dict) or set(settings) != keys
            or type(settings["schema_version"]) is not int or settings["schema_version"] != schema
            or type(settings["protocol_token"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", settings["protocol_token"]) is None
            or type(settings["ref"]) is not str
            or re.fullmatch(r"[1-9][0-9]{0,18}", settings["ref"]) is None
            or int(settings["ref"]) > 2**63 - 1):
        raise YYBCodeError("yyb_private_config_invalid")
    if schema == 2 and (type(settings["app_id"]) is not str
            or re.fullmatch(r"wx[0-9a-f]{16}", settings["app_id"]) is None):
        raise YYBCodeError("yyb_private_config_invalid")
    return settings


def supply_to(consumer: Callable[[str], T], *, settings_file: str | Path,
              reference_context_file: str | Path, timeout: float = 45,
              expected_app_id: str | None = None) -> T:
    """One explicit code request, immediately passed to a prepared consumer.

    Settings: schema_version=1, protocol_token=64 lower-case hexadecimal chars,
    ref=explicit positive decimal account ID. Both files use existing private
    file protections. An expired reference can establish the known target only;
    none of its query headers are sent. This function never contacts Sushiro.
    """
    _validate_input(consumer, timeout, expected_app_id)
    settings = _settings(settings_file, 1)
    failure = None
    try:
        reference = read_credentials_file(reference_context_file, api_profile="miniapp_gateway")
    except CredentialError:
        failure = "yyb_target_reference_invalid"
    if failure:
        raise YYBCodeError(failure)
    target = _app_id(reference.referer)
    if (target is None or expected_app_id is not None and target != expected_app_id
            or any(getattr(reference, name) is None for name in (
            "authorization", "app_client", "app_code", "user_agent", "referer", "content_type"))):
        raise YYBCodeError("yyb_target_reference_invalid")
    return _supply(consumer, settings, target, timeout)


def supply_for_target(consumer: Callable[[str], T], *, settings_file: str | Path,
                      expected_app_id: str, timeout: float = 45) -> T:
    """Cold-start supply without importing an old Sushiro query credential.

    Private schema 2 settings contain exactly schema_version, protocol_token,
    ref and app_id. The caller must independently pin the same explicit app_id.
    Account selection is explicit; no account enumeration or target discovery.
    This only prepares the code supply step, not cloud initialization or login.
    """
    _validate_input(consumer, timeout, expected_app_id, required_target=True)
    settings = _settings(settings_file, 2)
    if settings["app_id"] != expected_app_id:
        raise YYBCodeError("yyb_target_mismatch")
    return _supply(consumer, settings, expected_app_id, timeout)


def _supply(consumer, settings, target, timeout):
    failure = None

    request = urllib.request.Request(ENDPOINT, method="POST",
        data=json.dumps({"ref": settings["ref"], "app_id": target}).encode("utf-8"),
        headers={"Authorization": "Bearer " + settings["protocol_token"],
                 "Content-Type": "application/json", "Accept": "application/json"})
    status = None
    body = None
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            if response.geturl() != ENDPOINT:
                failure = "yyb_redirect_rejected"
            elif response.headers.get_content_type() != "application/json":
                failure = "yyb_invalid_response"
            elif status == 200:
                body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        status = error.code
        try:
            error.close()
        except Exception:
            failure = "yyb_transport_failed"
    except Exception:
        failure = "yyb_transport_failed"
    if failure:
        raise YYBCodeError(failure)
    if status != 200:
        categories = {401: "yyb_local_auth_rejected", 409: "yyb_reauthorization_required",
                      502: "yyb_protocol_failed"}
        raise YYBCodeError("yyb_redirect_rejected" if status in (301, 302, 303, 307, 308)
                           else categories.get(status, "yyb_http_failed"))
    code = _code(body)
    try:
        outcome = consumer(code)
    except Exception:
        failure = "yyb_consumer_failed"
    finally:
        code = None
        body = None
    if failure:
        raise YYBCodeError(failure)
    return outcome
