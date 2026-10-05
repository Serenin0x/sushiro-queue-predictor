"""Explicit, bounded intake from an existing local Surge Mac query summary.

Only the observed fixed directory GET supplies a complete query context. No
request body, response body, initialization, UI control or external query is
performed here. An observed HTTP 200 is not independent server acceptance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import time
from urllib.parse import parse_qsl, urlsplit
from typing import Callable

from .auth import describe_authorization
from .capture import _clock, _expiry_error, _write_private
from .credentials import CredentialError, QueryCredentials, _context, _unique_object, _reject_constant, read_credentials_file

_CLI = "/Applications/Surge.app/Contents/Applications/surge-cli"
_MAX_BYTES = 4 * 1024 * 1024
_MAX_RECENT_ROWS = 200
_MAC_EPOCH = 978307200
_PATH = "/gateway/wechat/api/2.0/stores"
_QUERY = {"latitude": "1", "longitude": "1", "numresults": "10000"}
_HEADERS = {"authorization": "authorization", "x-app-client": "app_client",
            "x-app-code": "app_code", "user-agent": "user_agent",
            "referer": "referer", "content-type": "content_type"}
_ERRORS = frozenset({"surge_unsupported_environment", "surge_unavailable", "surge_summary_read_failed",
    "surge_summary_too_large", "surge_summary_invalid", "surge_invalid_input", "surge_window_timeout",
    "surge_candidate_not_found", "surge_context_incomplete", "surge_context_invalid",
    "surge_app_mismatch", "surge_context_unchanged", "surge_context_ambiguous",
    "surge_auth_guard_stop", "surge_current_context_changed"})


class SurgeError(ValueError):
    def __init__(self, error_code: str):
        self.error_code = error_code if isinstance(error_code, str) and error_code in _ERRORS else "surge_invalid_input"
        super().__init__(self.error_code)


@dataclass(frozen=True)
class SummaryCandidate:
    context: QueryCredentials = field(repr=False)
    received_at: datetime


def _directory_url(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 512 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        return False
    try:
        url = urlsplit(value)
        pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True)
        return (url.scheme == "https" and url.netloc == "sapi.sushiro.com.cn"
                and url.path == _PATH and not url.fragment and len(pairs) == 3
                and dict(pairs) == _QUERY)
    except ValueError:
        return False


def _app(referer: str | None) -> str | None:
    match = re.fullmatch(r"https://servicewechat\.com/(wx[a-f0-9]{16})/[0-9]+/(?:page-frame\.html|index\.html)", referer or "")
    return match[1] if match else None


def inspect_summary(body: bytes, *, previous: QueryCredentials, since: datetime,
                    now: datetime) -> SummaryCandidate:
    """Parse only recent records in the observed Mac 6.4.3 summary format.

    Support verified Unix timestamps plus a Mac-reference representation;
    exactly one must fit the bounded current window. No source time is inferred.
    """
    since, now = _clock(since), _clock(now)
    if now < since or (now - since).total_seconds() > 60 or previous.api_profile != "miniapp_gateway" or _app(previous.referer) is None:
        raise SurgeError("surge_invalid_input")
    if not isinstance(body, bytes) or len(body) > _MAX_BYTES:
        raise SurgeError("surge_summary_too_large")
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        value = None
    if not isinstance(value, dict) or not isinstance(value.get("recent-requests"), list):
        raise SurgeError("surge_summary_invalid")
    rows = value["recent-requests"]
    if len(rows) > _MAX_RECENT_ROWS:
        raise SurgeError("surge_summary_too_large")
    candidates = []
    failure = "surge_candidate_not_found"
    for row in rows:
        if not isinstance(row, dict) or not _directory_url(row.get("URL")):
            continue
        date = row.get("completedDate")
        if type(date) not in (int, float) or not math.isfinite(date):
            continue
        times = []
        for offset in (0, _MAC_EPOCH):
            try:
                candidate_time = datetime.fromtimestamp(date + offset, tz=timezone.utc)
            except (ValueError, OverflowError, OSError):
                continue
            if since <= candidate_time <= now:
                times.append(candidate_time)
        if len(times) != 1:
            continue
        received = times[0]
        response, request = row.get("responseHeader"), row.get("requestHeader")
        if (row.get("method") != "GET" or row.get("completed") is not True
                or row.get("failed") is not False or row.get("streamHasRequestBody") is not False
                or not isinstance(response, str)
                or len(response) > 32768
                or not re.match(r"^HTTP/[0-9.]+ 200(?:\s|$)", response)
                or not isinstance(request, str) or len(request) > 32768):
            continue
        lines = request.splitlines()
        first = re.fullmatch(r"GET (\S+) HTTP/[0-9.]+", lines[0] if lines else "")
        target = first[1] if first else ""
        if target.startswith("/"):
            target = "https://sapi.sushiro.com.cn" + target
        if not _directory_url(target):
            continue
        values = {}
        invalid = False
        for line in lines[1:]:
            if not line:
                break
            name, separator, header = line.partition(":")
            key = name.lower()
            if key in _HEADERS:
                if not separator or key in values or line.startswith((" ", "\t")):
                    invalid = True
                values[key] = header.strip(" \t")
            elif line.startswith((" ", "\t")):
                invalid = True
        if invalid:
            failure = "surge_context_invalid"
            continue
        if set(values) != set(_HEADERS):
            failure = "surge_context_incomplete"
            continue
        try:
            context = _context("miniapp_gateway", None, values["authorization"],
                               {_HEADERS[k]: v for k, v in values.items() if k != "authorization"})
        except CredentialError:
            failure = "surge_context_invalid"
            continue
        if _app(context.referer) != _app(previous.referer):
            failure = "surge_app_mismatch"
            continue
        if context.authorization == previous.authorization:
            failure = "surge_context_unchanged"
            continue
        status = describe_authorization(context.authorization, now=now)
        if (status["expiry_source"] != "unverified_claim"
                or status["remaining_seconds"] is None or _expiry_error(status)):
            failure = "surge_auth_guard_stop"
            continue
        candidates.append(SummaryCandidate(context, received))
    if not candidates:
        raise SurgeError(failure)
    latest = max(candidate.received_at for candidate in candidates)
    selected = [candidate for candidate in candidates if candidate.received_at == latest]
    if any(candidate.context != selected[0].context for candidate in selected):
        raise SurgeError("surge_context_ambiguous")
    return selected[0]


def _read_summary(timeout: float) -> bytes:
    """Bound stdout in memory; never store or expose raw CLI output."""
    if sys.platform != "darwin":
        raise SurgeError("surge_unsupported_environment")
    if not os.path.isfile(_CLI) or not os.access(_CLI, os.X_OK):
        raise SurgeError("surge_unavailable")
    process = None
    chunks = []
    selector = selectors.DefaultSelector()
    try:
        process = subprocess.Popen([_CLI, "dump", "request", "--raw"], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        size = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise SurgeError("surge_summary_read_failed")
            part = os.read(process.stdout.fileno(), min(65536, _MAX_BYTES + 1 - size))
            if not part:
                break
            size += len(part)
            if size > _MAX_BYTES:
                raise SurgeError("surge_summary_too_large")
            chunks.append(part)
        if process.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
            raise SurgeError("surge_summary_read_failed")
        return b"".join(chunks)
    except SurgeError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        raise SurgeError("surge_summary_read_failed") from None
    finally:
        selector.close()
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.wait()
            if process.stdout is not None:
                process.stdout.close()


def receive_summary(*, credentials_file: str | Path, revision: int, seconds: int = 60,
                    on_ready: Callable[[dict], None] | None = None) -> dict:
    """Observe one new normal directory context; never enable MitM or query SAPI."""
    if type(seconds) is not int or not 1 <= seconds <= 60 or type(revision) is not int or not 1 <= revision <= 2**63 - 1:
        raise SurgeError("surge_invalid_input")
    previous = read_credentials_file(credentials_file, api_profile="miniapp_gateway")
    if revision <= previous.revision or _app(previous.referer) is None:
        raise SurgeError("surge_invalid_input")
    if sys.platform != "darwin":
        raise SurgeError("surge_unsupported_environment")
    since = _clock(None)
    deadline = time.monotonic() + seconds
    if on_ready:
        on_ready({"event": "surge_ready", "window_seconds": seconds,
                  "external_network_performed": False, "raw_logging": False})
    last = "surge_candidate_not_found"
    while time.monotonic() < deadline:
        if read_credentials_file(credentials_file, api_profile="miniapp_gateway") != previous:
            raise SurgeError("surge_current_context_changed")
        body = _read_summary(min(5, max(0.001, deadline - time.monotonic())))
        try:
            candidate = inspect_summary(body, previous=previous, since=since, now=_clock(None))
        except SurgeError as error:
            if error.error_code not in {"surge_candidate_not_found", "surge_context_unchanged"}:
                raise
            last = error.error_code
            time.sleep(min(1, max(0, deadline - time.monotonic())))
            continue
        if time.monotonic() >= deadline:
            break
        if read_credentials_file(credentials_file, api_profile="miniapp_gateway") != previous:
            raise SurgeError("surge_current_context_changed")
        context = _context("miniapp_gateway", revision, candidate.context.authorization,
            {key: getattr(candidate.context, key) for key in _HEADERS.values() if key != "authorization"})
        bundle = {"schema_version": 1, "api_profile": "miniapp_gateway", "revision": revision,
                  **{key: getattr(context, key) for key in _HEADERS.values()}}
        body = json.dumps(bundle, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
        if len(body) > 16384:
            raise SurgeError("surge_context_invalid")
        durability = _write_private(body, credentials_file, revision, context, None)
        return {"committed": True, "durability_confirmed": durability, "revision": revision,
                "api_profile": "miniapp_gateway", "credential_source": "normal_directory_query_summary",
                "observed_http_status": 200, "response_body_inspected": False,
                "query_response_received_at": candidate.received_at.isoformat().replace("+00:00", "Z"),
                "authorization_status": describe_authorization(context.authorization),
                "network_performed": False, "external_network_performed": False,
                "server_acceptance": "unverified", "network_verified": False}
    # Fixed disposition only; no request, identity or original exception escapes.
    raise SurgeError("surge_context_unchanged" if last == "surge_context_unchanged" else "surge_window_timeout")
