"""Explicit private staging promotion, after local debug is confirmed OFF."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path

from .auth import describe_authorization
from .capture import CaptureError, _clock, _expiry_error, _write_private
from .credentials import CredentialError, read_credentials_file
from .surge import _app
from .surgeguard import GuardError, read_state


class PromotionError(ValueError):
    def __init__(self, error_code: str, *, committed: bool = False,
                 durability_confirmed: bool = False):
        super().__init__(error_code)
        self.error_code = error_code
        self.committed = committed
        self.durability_confirmed = durability_confirmed


def _off() -> None:
    try:
        state = read_state()
    except GuardError:
        raise PromotionError("promotion_debug_state_unknown") from None
    if (set(state) != {"mitm_enabled", "capture_enabled", "auto_mitm"}
            or any(type(value) is not bool for value in state.values())):
        raise PromotionError("promotion_debug_state_unknown")
    if any(state.values()):
        raise PromotionError("promotion_debug_switch_on")


def promote_context(*, credentials_file: str | Path, staged_file: str | Path,
                    expected_revision: int, now: datetime | None = None) -> dict:
    """Preserve staging and never query, enable debug, generate auth or roll back.

    OFF checks and the directory writer lock coordinate cooperating processes.
    They do not guarantee another operator cannot enable debug after a check.
    Independent server acceptance remains a separate collector observation.
    """
    if type(expected_revision) is not int or not 1 <= expected_revision < 2**63:
        raise PromotionError("promotion_invalid_input")
    try:
        same_path = os.path.abspath(credentials_file) == os.path.abspath(staged_file)
    except (TypeError, ValueError, OSError):
        raise PromotionError("promotion_invalid_input") from None
    if same_path:
        raise PromotionError("promotion_path_conflict")
    _off()
    try:
        previous = read_credentials_file(credentials_file, api_profile="miniapp_gateway")
        candidate = read_credentials_file(staged_file, api_profile="miniapp_gateway")
    except CredentialError:
        raise PromotionError("promotion_private_context_invalid") from None
    fields = ("authorization", "app_client", "app_code", "user_agent", "referer", "content_type")
    if any(not isinstance(getattr(context, key), str) or not getattr(context, key)
           for context in (previous, candidate) for key in fields):
        raise PromotionError("promotion_private_context_invalid")
    if previous.revision != expected_revision:
        raise PromotionError("promotion_baseline_changed")
    if candidate.revision <= expected_revision:
        raise PromotionError("promotion_revision_not_new")
    if candidate.authorization == previous.authorization:
        raise PromotionError("promotion_authorization_unchanged")
    if (_app(previous.referer) is None or _app(candidate.referer) != _app(previous.referer)
            or candidate.app_client != previous.app_client):
        raise PromotionError("promotion_app_mismatch")
    try:
        status = describe_authorization(candidate.authorization, now=_clock(now))
    except CaptureError:
        raise PromotionError("promotion_invalid_clock") from None
    if (status["expiry_source"] != "unverified_claim" or status["remaining_seconds"] is None
            or _expiry_error(status)):
        raise PromotionError("promotion_auth_guard_stop")
    # Serialize this validated whole context, never reopen raw staging to copy it.
    body = json.dumps({"schema_version": 1, **asdict(candidate)}, ensure_ascii=True,
                      allow_nan=False, separators=(",", ":")).encode()
    if len(body) > 16384:
        raise PromotionError("promotion_private_context_invalid")
    _off()
    try:
        durable = _write_private(body, credentials_file, candidate.revision, candidate,
                                 now, expected_current=previous)
    except CaptureError as error:
        if error.error_code == "capture_destination_changed":
            code = "promotion_baseline_changed"
        elif error.error_code in {"capture_auth_expired", "capture_auth_expiring", "capture_auth_invalid_claim"}:
            code = "promotion_auth_guard_stop"
        else:
            code = "promotion_write_failed"
        raise PromotionError(code) from None
    try:
        if read_credentials_file(credentials_file, api_profile="miniapp_gateway") != candidate:
            raise PromotionError("promotion_commit_unconfirmed")
        _off()
    except (CredentialError, PromotionError):
        # The rename happened: report that fact and never retry or restore old auth.
        raise PromotionError("promotion_commit_unconfirmed", committed=True,
                             durability_confirmed=durable) from None
    return {"event": "private_context_promoted", "committed": True,
            "durability_confirmed": durable, "revision": candidate.revision,
            "debug_off_confirmed": True, "staging_preserved": True,
            "server_acceptance": "unverified", "network_performed": False,
            "external_network_performed": False, "client_updated": False,
            "hostnames_restored": False}
