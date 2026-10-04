"""Bounded, read-only queries to fixed mainland store API profiles.

This is a transport and response-validation tool, not a prediction service.
Legacy routing comes from reference-code research. Gateway detail routing was
observed in a normal mini-program request and independently replayed for store
3004 with HTTP 200 on 2026-10-02. Gateway directory routing and the same fixed
directory parameters were observed with HTTP 200 on 2026-10-03. Credential renewal, individual header necessity,
source freshness and anonymous access are not established. No credential is
bundled or discovered automatically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import http.client
import json
import math
import re
import socket
import ssl
import time
from typing import Any, Protocol
from types import MappingProxyType
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from .transport import observe_headers


_READ_ONLY_ENDPOINTS = frozenset({"stores", "getStoreById"})
_MAX_TIMEOUT_SECONDS = 15
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_ID_PATTERN = re.compile(r"[0-9]{1,19}\Z", re.ASCII)
_SUCCESS_CODES = frozenset({"", "0", "200", "OK", "SUCCESS"})
_CODE_FIELDS = ("code", "errorCode", "error_code", "errCode")


@dataclass(frozen=True)
class _ApiProfile:
    origin: str
    base_path: str
    endpoints: frozenset[str]


_API_PROFILES = MappingProxyType({
    "legacy": _ApiProfile(
        "https://crm-cn-prd.sushiro.com.cn",
        "/wechat/api/2.0",
        frozenset({"stores", "getStoreById"}),
    ),
    "miniapp_gateway": _ApiProfile(
        "https://sapi.sushiro.com.cn",
        "/gateway/wechat/api/2.0",
        frozenset({"stores", "getStoreById"}),
    ),
})


@dataclass(frozen=True)
class QueryResult:
    """Safe query outcome; request timestamps are not source-update times.

    Successful JSON objects are preserved. A bare store-list array is wrapped
    in a synthetic ``{"data": array}`` object to keep the payload contract a
    dictionary. Failed responses never expose their body or error details.
    """

    ok: bool
    payload: dict[str, Any] | None = field(repr=False)
    error_code: str | None
    http_status: int | None
    started_at: str
    received_at: str
    elapsed_ms: int
    transport: dict | None = field(default=None, repr=False)


class _Opener(Protocol):
    def open(self, request: Request, *, timeout: float) -> Any: ...


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Returning None makes urllib raise HTTPError without visiting newurl.
        return None


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _positive_id(value: Any) -> int | None:
    if type(value) is int:
        number = value
    elif isinstance(value, str) and _ID_PATTERN.fullmatch(value):
        number = int(value)
    else:
        return None
    return number if 0 < number <= 2**63 - 1 else None


def _store_id(store: dict[str, Any]) -> int | None:
    present = [store[key] for key in ("id", "storeId") if key in store]
    if not present:
        return None
    parsed = [_positive_id(value) for value in present]
    if None in parsed or len(set(parsed)) != 1:
        return None
    return parsed[0]


def _valid_store(store: Any) -> bool:
    return (
        isinstance(store, dict)
        and _store_id(store) is not None
        and isinstance(store.get("name"), str)
        and bool(store["name"].strip())
    )


def _has_business_error(value: dict[str, Any]) -> bool:
    if "success" in value and value["success"] is not True:
        return True
    if value.get("error"):
        return True
    for key in _CODE_FIELDS:
        if key not in value or value[key] is None:
            continue
        code = value[key]
        if type(code) is int:
            normalized = str(code)
        elif isinstance(code, str):
            normalized = code.strip().upper()
        else:
            return True
        if normalized not in _SUCCESS_CODES:
            return True
    return False


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError("invalid_json_constant")


class SushiroClient:
    """Single-attempt queries to an explicitly selected fixed API profile.

    ``authorization`` must be a locally supplied query token or Bearer value.
    ``None`` omits authorization and does not establish that anonymous data
    access is supported. Callers bound sampling and stop after failed queries.
    ``legacy`` preserves the researched default; ``miniapp_gateway`` permits
    only the observed detail query. Gateway app-header, user-agent, referer and
    content-type values are optional, supplied locally and never transferred
    to the legacy origin. Their
    presence in an observed request does not prove the server requires them.
    ``ca_file`` supplies trusted CA certificates while preserving certificate
    and hostname verification. The optional opener is an offline-test seam,
    not a configurable destination.
    """

    def __init__(
        self,
        authorization: str | None,
        timeout_seconds: float = 15,
        max_response_bytes: int = 2097152,
        *,
        opener: _Opener | None = None,
        ca_file: str | None = None,
        api_profile: str = "legacy",
        app_client: str | None = None,
        app_code: str | None = None,
        user_agent: str | None = None,
        referer: str | None = None,
        content_type: str | None = None,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= _MAX_TIMEOUT_SECONDS
        ):
            raise ValueError("invalid_timeout")
        if (
            type(max_response_bytes) is not int
            or not 0 < max_response_bytes <= _MAX_RESPONSE_BYTES
        ):
            raise ValueError("invalid_response_limit")
        if ca_file is not None and (
            not isinstance(ca_file, str) or not ca_file or "\x00" in ca_file
        ):
            raise ValueError("invalid_ca_file")
        if not isinstance(api_profile, str) or api_profile not in _API_PROFILES:
            raise ValueError("invalid_api_profile")
        if api_profile == "legacy" and any(
            value is not None
            for value in (app_client, app_code, user_agent, referer, content_type)
        ):
            raise ValueError("unsupported_profile_header")
        self._api_profile = api_profile
        self._profile = _API_PROFILES[api_profile]
        self._authorization = self._normalize_authorization(authorization)
        self._app_client = self._validate_header(app_client, "invalid_app_client")
        self._app_code = self._validate_header(app_code, "invalid_app_code")
        self._user_agent = self._validate_header(
            user_agent, "invalid_user_agent", max_length=2048, allow_spaces=True
        )
        self._referer = self._validate_header(
            referer, "invalid_referer", max_length=2048
        )
        self._content_type = self._validate_header(
            content_type, "invalid_content_type", max_length=256, allow_spaces=True
        )
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._opener = (
            opener
            if opener is not None
            else self._default_opener(ca_file)
        )

    @property
    def api_profile(self) -> str:
        """Non-secret source identifier used to partition collected history."""
        return self._api_profile

    @staticmethod
    def _validate_header(
        value: str | None,
        error_code: str,
        *,
        max_length: int = 1024,
        allow_spaces: bool = False,
    ) -> str | None:
        if value is None:
            return None
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= max_length
            or not value.strip()
            or any(
                ord(char) < (32 if allow_spaces else 33) or ord(char) > 126
                for char in value
            )
        ):
            raise ValueError(error_code)
        return value

    @staticmethod
    def _default_opener(ca_file: str | None) -> _Opener:
        try:
            context = ssl.create_default_context(cafile=ca_file)
        except (OSError, ValueError):
            # A failed trust-store load must not expose paths or exception text.
            raise ValueError("invalid_ca_file") from None
        opener = build_opener(
            ProxyHandler({}), _RejectRedirects(), HTTPSHandler(context=context)
        )
        # Do not silently invent a user-agent for a gateway request.
        opener.addheaders = []
        return opener

    @staticmethod
    def _normalize_authorization(authorization: str | None) -> str | None:
        if authorization is None:
            return None
        if not isinstance(authorization, str):
            raise ValueError("invalid_authorization")
        if len(authorization) > 8192 or any(
            ord(char) < 32 or ord(char) > 126 for char in authorization
        ):
            raise ValueError("invalid_authorization")
        token = authorization.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if (
            not token
            or token.lower() == "bearer"
            or any(char.isspace() for char in token)
        ):
            raise ValueError("invalid_authorization")
        return "Bearer " + token

    def fetch_stores(self) -> QueryResult:
        return self._fetch(
            "stores", {"latitude": "1", "longitude": "1", "numresults": "10000"}
        )

    def fetch_store(self, store_id: str) -> QueryResult:
        return self._fetch("getStoreById", {"storeId": store_id})

    def _fetch(self, endpoint: str, params: dict[str, Any]) -> QueryResult:
        started_at = _utc_now()
        started_ns = time.monotonic_ns()
        status: int | None = None
        transport = None

        def result(error: str | None, payload: dict[str, Any] | None = None) -> QueryResult:
            return QueryResult(
                ok=error is None,
                payload=payload if error is None else None,
                error_code=error,
                http_status=status,
                started_at=started_at,
                received_at=_utc_now(),
                elapsed_ms=max(0, (time.monotonic_ns() - started_ns) // 1_000_000),
                transport=transport,
            )

        if endpoint not in _READ_ONLY_ENDPOINTS:
            return result("invalid_endpoint")
        if endpoint not in self._profile.endpoints:
            return result("unsupported_endpoint")
        requested_id: int | None = None
        if endpoint == "getStoreById":
            store_id = params.get("storeId")
            if not isinstance(store_id, str) or _positive_id(store_id) is None:
                return result("invalid_store_id")
            requested_id = _positive_id(store_id)
            params = {"storeId": store_id}
        else:
            # Keep even internal calls on the researched read-only parameters.
            params = {"latitude": "1", "longitude": "1", "numresults": "10000"}

        url = (
            f"{self._profile.origin}{self._profile.base_path}/{endpoint}"
            f"?{urlencode(params)}"
        )
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        if self._api_profile == "legacy":
            headers["User-Agent"] = "SushiWait-readonly"
        if self._authorization is not None:
            headers["Authorization"] = self._authorization
        if self._api_profile == "miniapp_gateway":
            if self._app_client is not None:
                headers["X-App-Client"] = self._app_client
            if self._app_code is not None:
                headers["X-App-Code"] = self._app_code
            if self._user_agent is not None:
                headers["User-Agent"] = self._user_agent
            if self._referer is not None:
                headers["Referer"] = self._referer
            if self._content_type is not None:
                headers["Content-Type"] = self._content_type
        request = Request(url, headers=headers, method="GET")
        response = None
        try:
            response = self._opener.open(request, timeout=self._timeout_seconds)
            response_status = response.getcode()
            if type(response_status) is not int or not 100 <= response_status <= 599:
                return result("invalid_response")
            status = response_status
            if 300 <= status < 400 or response.geturl() != url:
                return result("redirect_blocked")
            transport = observe_headers(response.headers)
            if not 200 <= status < 300:
                return result("http_error")
            declared_length = response.headers.get("Content-Length")
            if declared_length is not None:
                try:
                    if int(declared_length) > self._max_response_bytes:
                        return result("response_too_large")
                except (TypeError, ValueError):
                    # Invalid length cannot bypass the bounded read below.
                    pass
            body = response.read(self._max_response_bytes + 1)
            if not isinstance(body, bytes):
                return result("invalid_response")
            if len(body) > self._max_response_bytes:
                return result("response_too_large")
        except HTTPError as error:
            status = (
                error.code
                if type(error.code) is int and 100 <= error.code <= 599
                else None
            )
            if status is not None and not 300 <= status < 400 and error.geturl() == url:
                transport = observe_headers(error.headers)
            try:
                error.close()
            except Exception:
                pass
            return result(
                "redirect_blocked"
                if status is not None and 300 <= status < 400
                else "http_error"
            )
        except (TimeoutError, socket.timeout):
            return result("timeout")
        except ssl.SSLCertVerificationError:
            return result("tls_verification_failed")
        except ssl.SSLError:
            return result("tls_error")
        except URLError as error:
            if isinstance(error.reason, ssl.SSLCertVerificationError):
                return result("tls_verification_failed")
            if isinstance(error.reason, ssl.SSLError):
                return result("tls_error")
            return result(
                "timeout"
                if isinstance(error.reason, (TimeoutError, socket.timeout))
                else "network_error"
            )
        except (OSError, http.client.HTTPException):
            return result("network_error")
        except Exception:
            # No exception text, headers, body or token is returned or logged.
            return result("network_error")
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

        try:
            parsed = json.loads(
                body.decode("utf-8"),
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeError, ValueError, RecursionError):
            return result("invalid_json")
        if isinstance(parsed, dict) and _has_business_error(parsed):
            return result("business_error")

        if endpoint == "stores":
            stores = parsed.get("data") if isinstance(parsed, dict) else parsed
            if not isinstance(stores, list) or not all(_valid_store(store) for store in stores):
                return result("invalid_response")
            if any(_has_business_error(store) for store in stores):
                return result("business_error")
            return result(None, parsed if isinstance(parsed, dict) else {"data": parsed})

        if not isinstance(parsed, dict):
            return result("invalid_response")
        # A store may itself have an unknown data field. Only an outer object
        # without store identity is the researched data-object envelope.
        store = (
            parsed.get("data")
            if "data" in parsed and "id" not in parsed and "storeId" not in parsed
            else parsed
        )
        if isinstance(store, dict) and _has_business_error(store):
            return result("business_error")
        if not _valid_store(store):
            return result("invalid_response")
        if _store_id(store) != requested_id:
            return result("store_id_mismatch")
        return result(None, parsed)
