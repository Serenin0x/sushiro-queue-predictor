"""Explicit offline HAR inspection and private gateway-context generation.

Only a user-selected file is read. No network, discovery, login, renewal or
database operation occurs. Captured success is historical evidence, never
proof that a credential is currently accepted by the server. Raw captures,
unknown fields and request values are not part of any public result.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any
from urllib.parse import urlsplit

from .auth import describe_authorization
from .credentials import (
    CredentialError, MAX_CREDENTIAL_FILE_BYTES, QueryCredentials, _context,
    _finite_float, _identity, _private_file, _reject_constant, _unique_object,
    read_credentials_file,
)
from .observations import normalize_snapshot


MAX_CAPTURE_BYTES = 8 * 1024 * 1024
MAX_CAPTURE_ENTRIES = 100
MAX_CONTENT_BYTES = 2 * 1024 * 1024
_PROFILE = "miniapp_gateway"
_HOST = "sapi.sushiro.com.cn"
_PATH = "/gateway/wechat/api/2.0/getStoreById"
_URL = "https://" + _HOST + _PATH
_STORE_ID = re.compile(r"[1-9][0-9]{0,18}\Z", re.ASCII)
_HEADERS = {
    "authorization": "authorization", "x-app-client": "app_client",
    "x-app-code": "app_code", "user-agent": "user_agent",
    "referer": "referer", "content-type": "content_type",
}
_ERRORS = frozenset({
    "capture_unavailable", "capture_unsafe", "capture_unsupported",
    "capture_changed", "capture_too_large", "capture_invalid_json",
    "capture_invalid_format", "capture_invalid_clock", "capture_invalid_request",
    "capture_invalid_timestamp", "capture_http_not_successful",
    "capture_invalid_response", "capture_content_too_large",
    "capture_invalid_context", "capture_entry_not_found",
    "capture_auth_invalid_claim", "capture_auth_expired", "capture_auth_expiring",
    "capture_destination_unsafe", "capture_destination_changed",
    "capture_revision_conflict", "capture_write_failed",
})


class CaptureError(ValueError):
    """A fixed safe category, with no original parsing/filesystem exception."""

    def __init__(self, error_code: str):
        self.error_code = error_code if isinstance(error_code, str) and error_code in _ERRORS else "capture_invalid_format"
        super().__init__(self.error_code)


def _clock(now: datetime | None) -> datetime:
    current = datetime.now(timezone.utc) if now is None else now
    valid = False
    try:
        valid = isinstance(current, datetime) and current.utcoffset() is not None
        if valid:
            current = current.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        valid = False
    if not valid:
        raise CaptureError("capture_invalid_clock")
    return current


def _capture_time(value: Any) -> str | None:
    parsed = None
    if isinstance(value, str) and len(value) <= 80:
        try:
            candidate = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if candidate.utcoffset() is not None:
                parsed = candidate.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        except (ValueError, OverflowError):
            pass
    return parsed


def _expiry_error(status: dict) -> str | None:
    if status["expiry_source"] == "invalid_claim":
        return "capture_auth_invalid_claim"
    if status["expired"] is True:
        return "capture_auth_expired"
    if status["remaining_seconds"] is not None and status["remaining_seconds"] <= 30:
        return "capture_auth_expiring"
    return None


@dataclass(frozen=True)
class CaptureEntry:
    entry_index: int
    captured_at: str | None
    http_status: int | None
    store_id: str | None
    store_name: str | None
    headers_present: tuple[bool, ...]
    matched: bool
    error_code: str | None
    context: QueryCredentials | None = field(default=None, repr=False)

    def _public(self, now: datetime) -> dict:
        status = describe_authorization(self.context.authorization if self.context else None, now=now)
        error = self.error_code or (_expiry_error(status) if self.context else None)
        return {
            "entry_index": self.entry_index, "captured_at": self.captured_at,
            "http_status": self.http_status, "store_id": self.store_id,
            "store_name": self.store_name,
            "headers_present": dict(zip(_HEADERS, self.headers_present)),
            "authorization_status": status, "matched": self.matched,
            "importable": self.matched and self.context is not None and error is None,
            "error_code": error,
        }


@dataclass(frozen=True)
class CaptureInspection:
    entries_total: int
    ignored_entries: int
    entries: tuple[CaptureEntry, ...] = field(repr=False)
    _checked_at: datetime = field(repr=False)

    def public_report(self) -> dict:
        """Return a fresh allowlisted result, never raw input or header values."""
        return {
            "schema_version": 1, "api_profile": _PROFILE, "data_origin": "capture",
            "network_performed": False, "network_verified": False,
            "server_acceptance": "unverified", "entries_total": self.entries_total,
            "ignored_entries": self.ignored_entries,
            "candidates": [entry._public(self._checked_at) for entry in self.entries],
        }


def _open_parent(path: str | Path, *, private: bool) -> tuple[int, str]:
    if os.name != "posix" or not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "geteuid")):
        raise CaptureError("capture_unsupported")
    parent_fd = None
    code = None
    try:
        absolute = Path(os.path.abspath(path))
        if absolute.name in ("", ".", ".."):
            raise CaptureError("capture_destination_unsafe" if private else "capture_unsafe")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        parent_fd = os.open(absolute.anchor, flags)
        for component in absolute.parts[1:-1]:
            next_fd = os.open(component, flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        info = os.fstat(parent_fd)
        if private and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise CaptureError("capture_destination_unsafe")
        return parent_fd, absolute.name
    except CaptureError as error:
        code = error.error_code
    except OSError as error:
        code = ("capture_destination_unsafe" if private else "capture_unsafe") if error.errno in (errno.ELOOP, errno.ENOTDIR) else ("capture_write_failed" if private else "capture_unavailable")
    except (TypeError, ValueError, OverflowError):
        code = "capture_destination_unsafe" if private else "capture_unsafe"
    if parent_fd is not None:
        try:
            os.close(parent_fd)
        except OSError:
            pass
    raise CaptureError(code or "capture_unsafe")


def _check_parent(path: str | Path, parent_fd: int, *, private: bool) -> None:
    """Rewalk the supplied path before using its already-open directory."""
    current_fd = None
    try:
        current_fd, _ = _open_parent(path, private=private)
        old, current = os.fstat(parent_fd), os.fstat(current_fd)
        if (old.st_dev, old.st_ino, old.st_mode, old.st_uid) != (current.st_dev, current.st_ino, current.st_mode, current.st_uid):
            raise CaptureError("capture_destination_changed" if private else "capture_changed")
        if private and (old.st_uid != os.geteuid() or stat.S_IMODE(old.st_mode) != 0o700):
            raise CaptureError("capture_destination_unsafe")
    finally:
        if current_fd is not None:
            os.close(current_fd)


def _read_capture(path: str | Path) -> bytes:
    parent_fd = file_fd = None
    code = None
    try:
        parent_fd, name = _open_parent(path, private=False)
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                          | getattr(os, "O_CLOEXEC", 0), dir_fd=parent_fd)
        before = os.fstat(file_fd)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        for info in (before, named):
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise CaptureError("capture_unsafe")
        if _identity(before) != _identity(named):
            raise CaptureError("capture_changed")
        if before.st_size > MAX_CAPTURE_BYTES:
            raise CaptureError("capture_too_large")
        pieces = []
        remaining = MAX_CAPTURE_BYTES + 1
        while remaining:
            piece = os.read(file_fd, remaining)
            if not piece:
                break
            pieces.append(piece)
            remaining -= len(piece)
        body = b"".join(pieces)
        if len(body) > MAX_CAPTURE_BYTES:
            raise CaptureError("capture_too_large")
        after = os.fstat(file_fd)
        named_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(before) != _identity(after) or _identity(before) != _identity(named_after) or len(body) != before.st_size:
            raise CaptureError("capture_changed")
        _check_parent(path, parent_fd, private=False)
        return body
    except CaptureError as error:
        code = error.error_code
    except OSError as error:
        code = "capture_unsafe" if error.errno in (errno.ELOOP, errno.ENOTDIR) else "capture_unavailable"
    except (TypeError, ValueError, OverflowError):
        code = "capture_invalid_format"
    finally:
        for descriptor in (file_fd, parent_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
    raise CaptureError(code or "capture_invalid_format")


def _json(body: bytes, error_code: str) -> Any:
    failed = False
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_float=_finite_float, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        failed = True
    if failed:
        # Raised outside the handler: JSONDecodeError.doc must not survive.
        raise CaptureError(error_code)
    return value


def _candidate(request: dict) -> bool:
    url = request.get("url")
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        return parsed.hostname == _HOST and parsed.path == _PATH
    except ValueError:
        return False


def _request_id(request: dict) -> str | None:
    url = request.get("url")
    if request.get("method") != "GET" or not isinstance(url, str):
        return None
    prefix = _URL + "?storeId="
    if not url.startswith(prefix):
        return None
    store_id = url[len(prefix):]
    if not _STORE_ID.fullmatch(store_id) or int(store_id) > 2**63 - 1:
        return None
    if "queryString" in request and request["queryString"] != [{"name": "storeId", "value": store_id}]:
        return None
    # The verified GET has no request body. Never import write/body input.
    if request.get("postData") not in (None, {}):
        return None
    return store_id


def _request_context(request: dict) -> tuple[QueryCredentials | None, tuple[bool, ...], str | None]:
    values = {}
    invalid = False
    headers = request.get("headers", [])
    if not isinstance(headers, list):
        headers = []
        invalid = True
    for header in headers:
        if not isinstance(header, dict) or not isinstance(header.get("name"), str):
            invalid = True
            continue
        key = header["name"].lower()
        if key not in _HEADERS:
            continue
        if key in values or not isinstance(header.get("value"), str):
            invalid = True
        values[key] = header.get("value")
    presence = tuple(key in values for key in _HEADERS)
    if invalid or not values.get("authorization"):
        return None, presence, "capture_invalid_context"
    context = None
    try:
        context = _context(_PROFILE, None, values["authorization"],
                           {_HEADERS[key]: value for key, value in values.items() if key != "authorization"})
    except CredentialError:
        pass
    if context is None or context.authorization is None:
        return None, presence, "capture_invalid_context"
    return context, presence, None


def _response_json(content: Any) -> Any:
    if not isinstance(content, dict) or not isinstance(content.get("text"), str):
        raise CaptureError("capture_invalid_response")
    text = content["text"]
    encoding = content.get("encoding")
    body = None
    code = None
    try:
        if encoding == "base64":
            if len(text) > 4 * ((MAX_CONTENT_BYTES + 2) // 3):
                code = "capture_content_too_large"
            else:
                body = base64.b64decode(text.encode("ascii"), validate=True)
                if base64.b64encode(body).decode("ascii") != text:
                    code = "capture_invalid_response"
        elif encoding is None:
            if len(text) > MAX_CONTENT_BYTES:
                code = "capture_content_too_large"
            else:
                body = text.encode("utf-8")
        else:
            code = "capture_invalid_response"
    except (ValueError, UnicodeError, binascii.Error):
        code = "capture_invalid_response"
    if code:
        raise CaptureError(code)
    if body is None or len(body) > MAX_CONTENT_BYTES:
        raise CaptureError("capture_content_too_large")
    return _json(body, "capture_invalid_response")


def _store_identity(payload: Any, store_id: str, captured_at: str) -> str | None:
    name = None
    try:
        # Timing is validation-only. It is not saved as a live observation or
        # represented as source freshness/actual response timing.
        snapshot = normalize_snapshot(payload, store_id,
            request_started_at=captured_at, received_at=captured_at, elapsed_ms=0,
            data_origin="fixture", api_profile=_PROFILE)
        fields = snapshot["normalized"]
        has_id = any(fields[key]["presence"] == "present" for key in ("id", "storeId"))
        raw_name = fields["name"]
        if (has_id and raw_name["presence"] == "present" and isinstance(raw_name["value"], str)
                and raw_name["value"].strip() and len(raw_name["value"]) <= 200
                and not any(ord(char) < 32 or ord(char) == 127 for char in raw_name["value"])):
            name = raw_name["value"]
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        pass
    return name


def _inspect_entry(entry: dict, index: int) -> CaptureEntry:
    request = entry["request"]
    context, presence, context_error = _request_context(request)
    captured_at = _capture_time(entry.get("startedDateTime"))
    response = entry.get("response")
    status = response.get("status") if isinstance(response, dict) else None
    status = status if type(status) is int and 100 <= status <= 599 else None
    store_id = _request_id(request)
    name = None
    error = None
    if store_id is None:
        error = "capture_invalid_request"
    elif captured_at is None:
        error = "capture_invalid_timestamp"
    elif status != 200:
        error = "capture_http_not_successful"
    else:
        try:
            payload = _response_json(response.get("content"))
            name = _store_identity(payload, store_id, captured_at)
        except CaptureError as failure:
            error = failure.error_code
        if error is None and name is None:
            error = "capture_invalid_response"
    matched = error is None
    if matched:
        error = context_error
    # Only response-corresponding identities become public verified identity.
    return CaptureEntry(index, captured_at, status, store_id if matched else None,
                        name, presence, matched, error, context)


def inspect_capture(path: str | Path, *, now: datetime | None = None) -> CaptureInspection:
    """Inspect an explicitly selected bounded HAR; external entries are ignored."""
    checked_at = _clock(now)
    value = _json(_read_capture(path), "capture_invalid_json")
    if not isinstance(value, dict) or not isinstance(value.get("log"), dict):
        raise CaptureError("capture_invalid_format")
    entries = value["log"].get("entries")
    if not isinstance(entries, list):
        raise CaptureError("capture_invalid_format")
    if len(entries) > MAX_CAPTURE_ENTRIES:
        raise CaptureError("capture_too_large")
    selected = []
    for index, entry in enumerate(entries):
        if (not isinstance(entry, dict) or not isinstance(entry.get("request"), dict)
                or not isinstance(entry["request"].get("url"), str)):
            raise CaptureError("capture_invalid_format")
        if _candidate(entry["request"]):
            selected.append(_inspect_entry(entry, index))
    return CaptureInspection(len(entries), len(entries) - len(selected), tuple(selected), checked_at)


def _named_identity(parent_fd: int, name: str) -> tuple | None:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not _private_file(info):
        raise CaptureError("capture_destination_unsafe")
    return _identity(info)


def _write_private(body: bytes, destination: str | Path, revision: int,
                   context: QueryCredentials, now: datetime | None) -> bool:
    """Commit atomically; same-account writers must honor the directory lock.

    POSIX rename has no compare-and-swap. Identity checks and the advisory lock
    prevent cooperating writers from racing; an uncooperative same-user process
    can still race the final check. Once renamed, a directory-fsync failure is
    reported as committed with unconfirmed durability, never as an unwritten
    result. There is no retry or rollback of a committed context.
    """
    parent_fd = temp_fd = None
    temp_name = None
    code = None
    committed = False
    durability = False
    try:
        import fcntl
        parent_fd, name = _open_parent(destination, private=True)
        fcntl.flock(parent_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = _named_identity(parent_fd, name)
        if before is not None:
            old = read_credentials_file(destination, api_profile=_PROFILE)
            if _named_identity(parent_fd, name) != before:
                raise CaptureError("capture_destination_changed")
            if revision <= old.revision:
                raise CaptureError("capture_revision_conflict")
        # Detect known directory-fsync limitations before changing a target.
        os.fsync(parent_fd)
        temp_name = ".sushiwait-context-" + secrets.token_hex(16) + ".tmp"
        temp_fd = os.open(temp_name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                          | getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=parent_fd)
        before_temp = os.fstat(temp_fd)
        if not _private_file(before_temp) or stat.S_IMODE(before_temp.st_mode) != 0o600:
            raise CaptureError("capture_destination_unsafe")
        offset = 0
        while offset < len(body):
            count = os.write(temp_fd, body[offset:])
            if count <= 0:
                raise CaptureError("capture_write_failed")
            offset += count
        os.fsync(temp_fd)
        final_temp = os.fstat(temp_fd)
        os.lseek(temp_fd, 0, os.SEEK_SET)
        read_back = []
        remaining = MAX_CREDENTIAL_FILE_BYTES + 1
        while remaining:
            piece = os.read(temp_fd, remaining)
            if not piece:
                break
            read_back.append(piece)
            remaining -= len(piece)
        named_temp = os.stat(temp_name, dir_fd=parent_fd, follow_symlinks=False)
        if (not _private_file(final_temp) or _identity(final_temp) != _identity(named_temp)
                or _identity(final_temp) != _identity(os.fstat(temp_fd))
                or final_temp.st_dev != before_temp.st_dev or final_temp.st_ino != before_temp.st_ino
                or final_temp.st_size != len(body) or b"".join(read_back) != body):
            raise CaptureError("capture_destination_changed")
        _check_parent(destination, parent_fd, private=True)
        if _named_identity(parent_fd, name) != before:
            raise CaptureError("capture_destination_changed")
        expiry = _expiry_error(describe_authorization(context.authorization, now=_clock(now)))
        if expiry:
            raise CaptureError(expiry)
        os.replace(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        committed = True
        temp_name = None
        try:
            os.fsync(parent_fd)
            durability = True
        except OSError:
            pass
    except CaptureError as error:
        code = error.error_code
    except CredentialError:
        code = "capture_destination_unsafe"
    except (OSError, ImportError, ValueError, TypeError, OverflowError):
        code = "capture_write_failed"
    finally:
        if temp_fd is not None:
            try:
                os.close(temp_fd)
            except OSError:
                pass
        if temp_name is not None and parent_fd is not None:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except OSError:
                pass
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError:
                pass
    if not committed:
        raise CaptureError(code or "capture_write_failed")
    return durability


def write_credentials_from_capture(inspection: CaptureInspection, *, entry_index: int,
                                   destination: str | Path, revision: int,
                                   now: datetime | None = None) -> dict:
    """Select one original entry index and atomically publish its complete context."""
    checked_at = _clock(now)
    if type(entry_index) is not int or entry_index < 0 or not isinstance(inspection, CaptureInspection):
        raise CaptureError("capture_entry_not_found")
    entry = next((candidate for candidate in inspection.entries if candidate.entry_index == entry_index), None)
    if entry is None:
        raise CaptureError("capture_entry_not_found")
    if not entry.matched or entry.error_code or entry.context is None:
        raise CaptureError(entry.error_code or "capture_invalid_context")
    if type(revision) is not int or not 1 <= revision <= 2**63 - 1:
        raise CaptureError("capture_revision_conflict")
    # Revalidate the complete context without environment fallback or a client.
    context = None
    try:
        if entry.context.api_profile == _PROFILE:
            context = _context(_PROFILE, revision, entry.context.authorization,
                               {key: getattr(entry.context, key) for key in _HEADERS.values() if key != "authorization"})
    except CredentialError:
        pass
    if context is None or context.authorization is None:
        raise CaptureError("capture_invalid_context")
    status = describe_authorization(context.authorization, now=checked_at)
    expiry = _expiry_error(status)
    if expiry:
        raise CaptureError(expiry)
    bundle = {"schema_version": 1, "api_profile": _PROFILE, "revision": revision,
              **{key: getattr(context, key) for key in _HEADERS.values()}}
    body = json.dumps(bundle, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_CREDENTIAL_FILE_BYTES:
        raise CaptureError("capture_invalid_context")
    durability = _write_private(body, destination, revision, context, now)
    return {
        "written": True, "committed": True, "durability_confirmed": durability,
        "api_profile": _PROFILE, "entry_index": entry.entry_index,
        "captured_at": entry.captured_at, "store_id": entry.store_id,
        "store_name": entry.store_name, "revision": revision,
        "authorization_status": status, "network_performed": False,
        "network_verified": False, "server_acceptance": "unverified",
    }
