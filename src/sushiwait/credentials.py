"""Bounded local query context input; never discovers or renews credentials.

Files contain one complete, explicitly versioned profile context. They must be
privately owned regular files in a private directory and replaced atomically.
No URL, CA configuration, arbitrary header, HAR or login input is supported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import errno
import json
import math
import os
from pathlib import Path
import stat
from typing import Any

from .client import SushiroClient


MAX_CREDENTIAL_FILE_BYTES = 16 * 1024
_PROFILES = frozenset({"legacy", "miniapp_gateway"})
_CONTEXT_FIELDS = ("app_client", "app_code", "user_agent", "referer", "content_type")
_FILE_ERRORS = frozenset({
    "credentials_file_unavailable", "credentials_file_unsafe",
    "credentials_file_unsupported", "credentials_file_too_large",
    "credentials_file_changed", "credentials_file_invalid_json",
    "credentials_file_invalid", "credentials_profile_mismatch",
    "credentials_revision_rollback", "credentials_revision_conflict",
    "credentials_invalid",
})


class CredentialError(ValueError):
    """Only a fixed safe category may escape input processing."""

    def __init__(self, error_code: str):
        self.error_code = error_code if error_code in _FILE_ERRORS else "credentials_file_invalid"
        super().__init__(self.error_code)


@dataclass(frozen=True)
class QueryCredentials:
    api_profile: str
    revision: int | None = None
    authorization: str | None = field(default=None, repr=False)
    app_client: str | None = field(default=None, repr=False)
    app_code: str | None = field(default=None, repr=False)
    user_agent: str | None = field(default=None, repr=False)
    referer: str | None = field(default=None, repr=False)
    content_type: str | None = field(default=None, repr=False)


def _context(profile: str, revision: int | None, authorization: Any, values: dict) -> QueryCredentials:
    try:
        # These are pure validators: no client, opener or trust-store is made.
        authorization = SushiroClient._normalize_authorization(authorization)
        if authorization is not None and len(authorization) > 8192:
            raise ValueError("invalid_authorization")
        headers = {
            "app_client": SushiroClient._validate_header(values.get("app_client"), "invalid_app_client"),
            "app_code": SushiroClient._validate_header(values.get("app_code"), "invalid_app_code"),
            "user_agent": SushiroClient._validate_header(values.get("user_agent"), "invalid_user_agent",
                                                         max_length=2048, allow_spaces=True),
            "referer": SushiroClient._validate_header(values.get("referer"), "invalid_referer", max_length=2048),
            "content_type": SushiroClient._validate_header(values.get("content_type"), "invalid_content_type",
                                                           max_length=256, allow_spaces=True),
        }
    except ValueError:
        raise CredentialError("credentials_invalid") from None
    return QueryCredentials(profile, revision, authorization, **headers)


def _private_file(info: os.stat_result) -> bool:
    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) in (0o400, 0o600) and info.st_nlink == 1)


def _identity(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_private_file(path: str | Path) -> bytes:
    if os.name != "posix" or not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "geteuid")):
        raise CredentialError("credentials_file_unsupported")
    parent_fd = file_fd = None
    failure_code = None
    try:
        absolute = Path(os.path.abspath(path))
        if absolute.name in ("", ".", ".."):
            raise CredentialError("credentials_file_unsafe")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        parent_fd = os.open(absolute.anchor, directory_flags)
        # Walk via directory handles so no ancestor symlink can be followed.
        for component in absolute.parts[1:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        parent = os.fstat(parent_fd)
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
                or stat.S_IMODE(parent.st_mode) not in (0o500, 0o700)):
            raise CredentialError("credentials_file_unsafe")
        file_fd = os.open(absolute.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                          | getattr(os, "O_CLOEXEC", 0), dir_fd=parent_fd)
        before = os.fstat(file_fd)
        named_before = os.stat(absolute.name, dir_fd=parent_fd, follow_symlinks=False)
        if not _private_file(before) or not _private_file(named_before):
            raise CredentialError("credentials_file_unsafe")
        if _identity(before) != _identity(named_before):
            raise CredentialError("credentials_file_changed")
        if before.st_size > MAX_CREDENTIAL_FILE_BYTES:
            raise CredentialError("credentials_file_too_large")
        pieces = []
        remaining = MAX_CREDENTIAL_FILE_BYTES + 1
        while remaining:
            piece = os.read(file_fd, remaining)
            if not piece:
                break
            pieces.append(piece)
            remaining -= len(piece)
        body = b"".join(pieces)
        if len(body) > MAX_CREDENTIAL_FILE_BYTES:
            raise CredentialError("credentials_file_too_large")
        after = os.fstat(file_fd)
        named_after = os.stat(absolute.name, dir_fd=parent_fd, follow_symlinks=False)
        if (_identity(before) != _identity(after) or _identity(before) != _identity(named_after)
                or len(body) != before.st_size):
            raise CredentialError("credentials_file_changed")
        return body
    except CredentialError as error:
        failure_code = error.error_code
    except OSError as error:
        # ELOOP/ENOTDIR can be a symlink or another unsafe path component.
        failure_code = "credentials_file_unsafe" if error.errno in (errno.ELOOP, errno.ENOTDIR) else "credentials_file_unavailable"
    except (ValueError, TypeError, OverflowError):
        failure_code = "credentials_file_invalid"
    finally:
        for descriptor in (file_fd, parent_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
    # Do not attach filesystem exceptions containing private paths.
    raise CredentialError(failure_code or "credentials_file_invalid")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite")
    return number


def _reject_constant(value: str) -> None:
    raise ValueError("nonfinite")


def read_credentials_file(path: str | Path, *, api_profile: str) -> QueryCredentials:
    """Read a complete private context without falling back to environment values."""
    if not isinstance(api_profile, str) or api_profile not in _PROFILES:
        raise CredentialError("credentials_profile_mismatch")
    body = _read_private_file(path)
    parse_failed = False
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_float=_finite_float, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        parse_failed = True
    if parse_failed:
        # JSONDecodeError.doc can contain the complete secret context. Raising
        # after its handler keeps that object out of our public exception chain.
        raise CredentialError("credentials_file_invalid_json")
    if not isinstance(value, dict):
        raise CredentialError("credentials_file_invalid")
    if value.get("api_profile") != api_profile:
        raise CredentialError("credentials_profile_mismatch")
    expected = {"schema_version", "api_profile", "revision", "authorization"}
    if api_profile == "miniapp_gateway":
        expected.update(_CONTEXT_FIELDS)
    if set(value) != expected:
        raise CredentialError("credentials_file_invalid")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise CredentialError("credentials_file_invalid")
    if type(value["revision"]) is not int or not 1 <= value["revision"] <= 2**63 - 1:
        raise CredentialError("credentials_file_invalid")
    if not isinstance(value["authorization"], str):
        raise CredentialError("credentials_file_invalid")
    try:
        return _context(api_profile, value["revision"], value["authorization"], value)
    except CredentialError:
        raise CredentialError("credentials_file_invalid") from None


class CredentialSource:
    """Reload files per check; environment mode retains its initial context.

    Revision ordering is enforced within this process, not across restarts.
    A revision change means configuration changed, not proven server renewal.
    """

    def __init__(self, api_profile: str, *, credentials_file: str | Path | None = None,
                 anonymous: bool = False):
        if not isinstance(api_profile, str) or api_profile not in _PROFILES:
            raise CredentialError("credentials_profile_mismatch")
        if credentials_file is not None and anonymous:
            raise CredentialError("credentials_file_invalid")
        self.api_profile = api_profile
        self._path = credentials_file
        self._anonymous = anonymous
        self._current: QueryCredentials | None = None

    def current(self) -> QueryCredentials:
        if self._path is not None:
            candidate = read_credentials_file(self._path, api_profile=self.api_profile)
            if self._current is not None:
                if candidate.revision < self._current.revision:
                    raise CredentialError("credentials_revision_rollback")
                if candidate.revision == self._current.revision and candidate != self._current:
                    raise CredentialError("credentials_revision_conflict")
            self._current = candidate
        elif self._current is None:
            authorization = None
            values = {}
            if not self._anonymous:
                if self.api_profile == "legacy":
                    authorization = os.environ.get("SUSHIWAIT_QUERY_AUTHORIZATION")
                else:
                    authorization = os.environ.get("SUSHIWAIT_GATEWAY_AUTHORIZATION")
                    for key in _CONTEXT_FIELDS:
                        values[key] = os.environ.get("SUSHIWAIT_GATEWAY_" + key.upper())
            self._current = _context(self.api_profile, None, authorization, values)
        return self._current
