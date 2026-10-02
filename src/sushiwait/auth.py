"""Inspect locally declared authorization times without verifying credentials.

No network, CA lookup, credential storage or signature verification occurs.
Only bounded JWT-like iat/exp claims can become output; all other claims and
all parsing-error details remain private and unknown expiry stays unknown.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
import json
import math
import re
from typing import Any


_MAX_AUTHORIZATION_BYTES = 8192
_BASE64URL_SEGMENT = re.compile(r"[A-Za-z0-9_-]+={0,2}\Z", re.ASCII)


def _valid_segment(value: str) -> bool:
    if not _BASE64URL_SEGMENT.fullmatch(value):
        return False
    content_length = len(value.rstrip("="))
    remainder = content_length % 4
    if remainder == 1:
        return False
    padding = len(value) - content_length
    return padding == 0 or padding == (-content_length % 4)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_claim")
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite_claim")
    return number


def _reject_constant(value: str) -> None:
    raise ValueError("nonfinite_claim")


def _timestamp(value: Any) -> datetime | None:
    try:
        if type(value) not in (int, float) or not math.isfinite(value):
            return None
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _iso_utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def describe_authorization(
    authorization: str | None, *, now: datetime | None = None
) -> dict[str, Any]:
    """Return safe timing metadata, never a token or arbitrary JWT claims.

    Finite numeric iat and exp are validated independently. Lifetime requires
    both timestamps in order; reversed declarations leave expiry calculations
    unknown. Times are unverified declarations, not proof of server acceptance.
    Missing or malformed claims never receive an assumed TTL.
    The optional clock must be an aware datetime; output timestamps use UTC.
    Remaining seconds are conservatively rounded down and clamped at zero.
    """
    configured = authorization is not None and authorization != ""
    result: dict[str, Any] = {
        "configured": configured,
        "token_kind": "unknown" if configured else "missing",
        "declared_issued_at": None,
        "declared_expires_at": None,
        "declared_lifetime_seconds": None,
        "remaining_seconds": None,
        "expired": None,
        "expiry_source": "unknown",
        "signature_verified": False,
    }
    if not isinstance(authorization, str) or not authorization:
        return result
    if (
        len(authorization) > _MAX_AUTHORIZATION_BYTES
        or any(ord(char) < 32 or ord(char) > 126 for char in authorization)
    ):
        return result
    token = authorization.strip()
    if not token:
        result["configured"] = False
        result["token_kind"] = "missing"
        return result
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token or token.lower() == "bearer" or any(char.isspace() for char in token):
        return result
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        result["token_kind"] = "opaque"
        return result
    result["token_kind"] = "jwt_like"
    if not all(_valid_segment(part) for part in parts):
        return result
    try:
        segment = parts[1].rstrip("=")
        payload_bytes = base64.b64decode(
            segment + "=" * (-len(segment) % 4), altchars=b"-_", validate=True
        )
        claims = json.loads(
            payload_bytes.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_float=_finite_float,
            parse_constant=_reject_constant,
        )
        if not isinstance(claims, dict):
            return result
        issued = _timestamp(claims.get("iat"))
        expires = _timestamp(claims.get("exp"))
        if issued is not None:
            result["declared_issued_at"] = _iso_utc(issued)
        if expires is not None:
            result["declared_expires_at"] = _iso_utc(expires)
        if issued is not None and expires is not None:
            if claims["exp"] < claims["iat"]:
                result["expiry_source"] = "invalid_claim"
                return result
            lifetime = claims["exp"] - claims["iat"]
            if type(lifetime) is float and lifetime.is_integer():
                lifetime = int(lifetime)
            result["declared_lifetime_seconds"] = lifetime
        if expires is None:
            return result
        result["expiry_source"] = "unverified_claim"
        current = datetime.now(timezone.utc) if now is None else now
        if (
            not isinstance(current, datetime)
            or current.tzinfo is None
            or current.utcoffset() is None
        ):
            return result
        current_seconds = current.timestamp()
        if not math.isfinite(current_seconds):
            return result
        result.update({
            "remaining_seconds": max(0, math.floor(claims["exp"] - current_seconds)),
            "expired": current_seconds >= claims["exp"],
        })
    except (
        ValueError, TypeError, OverflowError, OSError, UnicodeError,
        binascii.Error, RecursionError,
    ):
        # Never return original input, other claims or exception details.
        return result
    return result
