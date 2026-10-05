"""One successful normal query -> one complete private context, over loopback.

This receiver does not obtain login codes, sign initialization requests, control
WeChat/Surge, or contact Sushiro. The explicit same-computer adapter supplies a
minimal observation; its historical success is not independent server proof.
"""

from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
import time
from typing import Callable
from urllib.parse import quote

from .capture import (
    CaptureError, CaptureInspection, _check_parent, _inspect_entry, _json,
    _named_identity, _open_parent, write_credentials_from_capture,
)
from .credentials import _identity, read_credentials_file


MAX_OBSERVATION_BYTES = 32 * 1024
MAX_WINDOW_SECONDS = 60
_APP_REFERER = re.compile(r"https://servicewechat\.com/(wx[a-f0-9]{16})/[0-9]+/(?:page-frame\.html|index\.html)\Z")
_HEADER_NAMES = frozenset({"authorization", "x-app-client", "x-app-code", "user-agent", "referer", "content-type"})
_URL_PREFIX = "https://sapi.sushiro.com.cn/gateway/wechat/api/2.0/getStoreById?storeId="
_ERRORS = frozenset({"bridge_invalid_input", "bridge_wrong_app", "bridge_wrong_store",
    "bridge_context_unchanged", "bridge_timeout", "bridge_unavailable", "bridge_session_unsafe"})


class BridgeError(ValueError):
    def __init__(self, code: str, *, diagnostics: dict | None = None):
        self.error_code = code if code in _ERRORS else "bridge_invalid_input"
        self.diagnostics = diagnostics
        super().__init__(self.error_code)


def _app_id(referer: str | None) -> str | None:
    match = _APP_REFERER.fullmatch(referer or "")
    return match.group(1) if match else None


def inspect_observation(body: bytes, *, expected_app: str,
                        store_ids: tuple[str, ...], now: datetime) -> CaptureInspection:
    """Strict, bounded input; other response fields never enter the context."""
    if not isinstance(body, bytes) or len(body) > MAX_OBSERVATION_BYTES:
        raise BridgeError("bridge_invalid_input")
    value = _json(body, "capture_invalid_json")
    if (not isinstance(value, dict) or set(value) != {"schema_version", "request", "response"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1):
        raise BridgeError("bridge_invalid_input")
    request, response = value["request"], value["response"]
    if (not isinstance(request, dict) or set(request) != {"method", "url", "headers"}
            or request["method"] != "GET" or not isinstance(request["url"], str)
            or not request["url"].startswith(_URL_PREFIX)):
        raise BridgeError("bridge_invalid_input")
    store_id = request["url"][len(_URL_PREFIX):]
    if store_id not in store_ids:
        raise BridgeError("bridge_wrong_store")
    headers = request["headers"]
    if not isinstance(headers, list) or len(headers) != len(_HEADER_NAMES):
        raise BridgeError("bridge_invalid_input")
    values = {}
    for header in headers:
        if (not isinstance(header, dict) or set(header) != {"name", "value"}
                or not isinstance(header["name"], str) or not isinstance(header["value"], str)
                or not header["value"].strip()):
            raise BridgeError("bridge_invalid_input")
        key = header["name"].lower()
        if key not in _HEADER_NAMES or key in values:
            raise BridgeError("bridge_invalid_input")
        values[key] = header["value"]
    if _app_id(values["referer"]) != expected_app:
        raise BridgeError("bridge_wrong_app")
    if (not isinstance(response, dict) or set(response) != {"status", "store"}
            or type(response["status"]) is not int or response["status"] != 200
            or not isinstance(response["store"], dict)
            or set(response["store"]) != {"id", "name"}):
        raise BridgeError("bridge_invalid_input")
    timestamp = now.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    entry = _inspect_entry({"request": request, "startedDateTime": timestamp,
        "response": {"status": 200, "content": {"text": json.dumps(response["store"], allow_nan=False)}}}, 0)
    if not entry.matched or entry.error_code or entry.context is None:
        raise CaptureError(entry.error_code or "capture_invalid_context")
    return CaptureInspection(1, 0, (entry,), now)


def _create_session(path: str | Path, value: dict) -> tuple[int, str, tuple]:
    parent_fd = file_fd = None
    created_identity = None
    failed = False
    try:
        parent_fd, name = _open_parent(path, private=True)
        file_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                          | getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=parent_fd)
        created_identity = _identity(os.fstat(file_fd))[:2]
        body = json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8")
        position = 0
        while position < len(body):
            written = os.write(file_fd, body[position:])
            if written <= 0:
                raise OSError
            position += written
        os.fsync(file_fd)
        _check_parent(path, parent_fd, private=True)
        identity = _named_identity(parent_fd, name)
        if identity != _identity(os.fstat(file_fd)):
            raise OSError
        os.close(file_fd)
        file_fd = None
        return parent_fd, name, identity
    except (OSError, ValueError, TypeError):
        failed = True
    finally:
        if failed:
            if file_fd is not None:
                os.close(file_fd)
            if parent_fd is not None:
                if created_identity is not None:
                    try:
                        # A same-user replacement belongs to its new creator.
                        current = _named_identity(parent_fd, name)
                        if current is not None and current[:2] == created_identity:
                            os.unlink(name, dir_fd=parent_fd)
                    except (OSError, CaptureError):
                        pass
                os.close(parent_fd)
    raise BridgeError("bridge_session_unsafe")


def receive_context(*, credentials_file: str | Path, session_file: str | Path,
                    revision: int, store_ids: tuple[str, ...], seconds: int = 60,
                    on_ready: Callable[[dict], None] | None = None,
                    diagnostics: bool = False) -> dict:
    """Explicit one-shot listener: IPv4 loopback, <=60s, no outgoing requests."""
    if (type(diagnostics) is not bool
            or type(seconds) is not int or not 1 <= seconds <= MAX_WINDOW_SECONDS
            or type(revision) is not int or not 1 <= revision <= 2**63 - 1
            or not 1 <= len(store_ids) <= 3 or len(set(store_ids)) != len(store_ids)
            or any(not isinstance(s, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", s)
                   or int(s) > 2**63 - 1 for s in store_ids)
            or os.path.abspath(credentials_file) == os.path.abspath(session_file)):
        raise BridgeError("bridge_invalid_input")
    previous = read_credentials_file(credentials_file, api_profile="miniapp_gateway")
    expected_app = _app_id(previous.referer)
    if expected_app is None:
        raise BridgeError("bridge_wrong_app")
    if revision <= previous.revision:
        raise CaptureError("capture_revision_conflict")
    secret = secrets.token_hex(32)
    deadline = time.monotonic() + seconds
    result = None
    parent_fd = None
    cleanup_confirmed = True
    diagnostic_counts = {"local_connections": 0, "authenticated_deliveries": 0,
                         "validated_observations": 0,
                         "rejected_observations": 0, "last_rejection": None}

    class Server(HTTPServer):
        allow_reuse_address = False

        def get_request(self):
            connection, address = super().get_request()
            diagnostic_counts["local_connections"] += 1
            connection.settimeout(max(0.001, min(1, deadline - time.monotonic())))
            return connection, address

        def handle_error(self, request, client_address):
            # Never let request bytes, headers or parser exceptions reach logs.
            pass

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):
            pass

        def send_error(self, code, message=None, explain=None):
            self._reply(code, {"ok": False})

        def _reply(self, status, value):
            body = json.dumps(value, allow_nan=False).encode("utf-8")
            self.send_response_only(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            nonlocal result
            authorizations = self.headers.get_all("Authorization", [])
            authorized = (len(authorizations) == 1 and authorizations[0].isascii() and
                          secrets.compare_digest(authorizations[0], "Bearer " + secret))
            if (self.path != "/v1/context" or time.monotonic() >= deadline
                    or self.headers.get_all("Host", []) != [f"127.0.0.1:{self.server.server_port}"]
                    or not authorized
                    or self.headers.get("Origin") is not None
                    or self.headers.get("Transfer-Encoding") is not None
                    or self.headers.get("Expect") is not None):
                self._reply(403, {"ok": False})
                return
            diagnostic_counts["authenticated_deliveries"] += 1
            lengths = self.headers.get_all("Content-Length", [])
            types = self.headers.get_all("Content-Type", [])
            if (len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,6}", lengths[0])
                    or not 1 <= int(lengths[0]) <= MAX_OBSERVATION_BYTES
                    or types != ["application/json"]):
                self._reply(400, {"ok": False})
                return
            failure = None
            try:
                body = self.rfile.read(int(lengths[0]))
                if len(body) != int(lengths[0]) or time.monotonic() >= deadline:
                    raise BridgeError("bridge_timeout")
                now = datetime.now(timezone.utc)
                inspection = inspect_observation(body, expected_app=expected_app,
                                                 store_ids=store_ids, now=now)
                diagnostic_counts["validated_observations"] += 1
                if inspection.entries[0].context.authorization == previous.authorization:
                    raise BridgeError("bridge_context_unchanged")
                if time.monotonic() >= deadline:
                    raise BridgeError("bridge_timeout")
                metadata = write_credentials_from_capture(inspection, entry_index=0,
                    destination=credentials_file, revision=revision)
                # This is local receipt time, not upstream query/source time.
                metadata["relay_received_at"] = metadata.pop("captured_at")
                metadata.pop("entry_index")
                result = {**metadata, "credential_source": "normal_client_bridge",
                          "local_transport": "ipv4_loopback", "network_performed": True,
                          "external_network_performed": False}
            except (CaptureError, BridgeError) as error:
                failure = error.error_code
            except (OSError, ValueError, TypeError, KeyError, OverflowError):
                failure = "bridge_invalid_input"
            if failure is not None:
                diagnostic_counts["rejected_observations"] += 1
                diagnostic_counts["last_rejection"] = failure
            self._reply(200 if result else 422, {"ok": result is not None,
                **({"error_code": failure} if failure else {})})

    server = None
    try:
        server = Server(("127.0.0.1", 0), Handler)
        server.timeout = 0.25
        setting = {"schema_version": 1,
            "url": f"http://127.0.0.1:{server.server_port}/v1/context",
            "token": secret, "expires_ms": int((time.time() + seconds) * 1000),
            "app_id": expected_app, "store_ids": list(store_ids)}
        # A comma-free argument for Surge's comma-separated script declaration.
        setting["adapter_argument"] = quote(json.dumps(setting, separators=(",", ":")), safe="")
        parent_fd, name, identity = _create_session(session_file, setting)
        if on_ready:
            on_ready({"event": "bridge_ready", "window_seconds": seconds,
                      "local_transport": "ipv4_loopback", "external_network_performed": False})
        while result is None and time.monotonic() < deadline:
            server.handle_request()
        if result is None:
            raise BridgeError("bridge_timeout",
                              diagnostics=dict(diagnostic_counts) if diagnostics else None)
    except (socket.error, OverflowError):
        raise BridgeError("bridge_unavailable") from None
    finally:
        if server is not None:
            server.server_close()
        if parent_fd is not None:
            try:
                _check_parent(session_file, parent_fd, private=True)
                if _named_identity(parent_fd, name) != identity:
                    cleanup_confirmed = False
                else:
                    os.unlink(name, dir_fd=parent_fd)
            except (OSError, CaptureError):
                cleanup_confirmed = False
            os.close(parent_fd)
    return {**result, "session_cleanup_confirmed": cleanup_confirmed,
            **({"diagnostics": dict(diagnostic_counts)} if diagnostics else {})}
