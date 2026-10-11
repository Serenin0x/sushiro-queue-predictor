"""Verify one new SAPI context, then publish the whole private revision.

No signing source, supplier, retries, daemon, or old-token queries are provided.
This callback is prepared before requesting a fresh one-use login code. A failed
query leaves the old file untouched; successful querying is distinguished from
an atomic commit and its durability confirmation.
"""

from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timezone
import json
import re
import threading

from .auth import describe_authorization
from .bridge import _app_id
from .capture import CaptureError, _write_private, _expiry_error
from .client import SushiroClient
from .credentials import CredentialError, MAX_CREDENTIAL_FILE_BYTES, _context, read_credentials_file
from .observations import normalize_snapshot
from .initialization import initialize_from_yyb


_FIELDS = ("authorization", "app_client", "app_code", "user_agent", "referer", "content_type")
_HEADS = {"X-App-Client": "app_client", "X-App-Code": "app_code",
          "User-Agent": "user_agent", "Referer": "referer", "Content-Type": "content_type"}


class SessionPreparationError(ValueError):
    def __init__(self, error_code):
        self.error_code = error_code if error_code in {
            "session_invalid_input", "session_reference_invalid", "session_baseline_changed"} else "session_invalid_input"
        super().__init__(self.error_code)


def prepare_verifier(*, credentials_file, expected_revision, store_id, timeout=15):
    """Return a single-use accept_session callback for initialization.

    A complete private baseline and explicit store are required before obtaining
    code. The new six-field context comes only from the new token and supplied
    normal headers. No expired baseline headers are borrowed. Requires a known
    expiry declaration with more than 30s remaining for this publication trial;
    this is not an upstream TTL guarantee or signature verification.
    """
    if (type(expected_revision) is not int or not 1 <= expected_revision < 2**63 - 1
            or type(store_id) is not str or re.fullmatch(r"[1-9][0-9]{0,18}", store_id) is None
            or int(store_id) > 2**63 - 1 or type(timeout) not in (int, float)
            or not 1 <= timeout <= 60):
        raise SessionPreparationError("session_invalid_input")
    failure = None
    try:
        previous = read_credentials_file(credentials_file, api_profile="miniapp_gateway")
    except CredentialError:
        failure = "session_reference_invalid"
    if failure:
        raise SessionPreparationError(failure)
    target = _app_id(previous.referer)
    if target is None or any(getattr(previous, key) is None for key in _FIELDS):
        raise SessionPreparationError("session_reference_invalid")
    if previous.revision != expected_revision:
        raise SessionPreparationError("session_baseline_changed")
    used, lock = False, threading.Lock()

    def accept(token, supplied_headers):
        nonlocal used
        result = {"event": "session_verification", "ok": False, "store_id": store_id,
                  "network_attempted": False, "query_accepted": False,
                  "committed": False, "durability_confirmed": False}

        def rejected(code):
            result["error_code"] = code
            return result

        with lock:
            if used:
                return rejected("session_verifier_used")
            used = True
        failure = False
        candidate = None
        try:
            if not isinstance(supplied_headers, Mapping):
                raise ValueError("headers")
            heads = dict(supplied_headers)
            if set(heads) != set(_HEADS):
                raise ValueError("heads")
            candidate = _context("miniapp_gateway", expected_revision + 1, token,
                                 {field: heads[name] for name, field in _HEADS.items()})
        except Exception:
            failure = True
        if failure or candidate is None or any(getattr(candidate, key) is None for key in _FIELDS):
            return rejected("session_material_invalid")
        if _app_id(candidate.referer) != target or candidate.app_client != previous.app_client:
            return rejected("session_app_mismatch")
        if candidate.authorization == previous.authorization:
            return rejected("session_authorization_unchanged")
        status = describe_authorization(candidate.authorization, now=datetime.now(timezone.utc))
        result["authorization_status"] = status
        if (status["expiry_source"] != "unverified_claim" or status["remaining_seconds"] is None
                or _expiry_error(status)):
            return rejected("session_auth_guard_stop")
        body = json.dumps({"schema_version": 1, **asdict(candidate)}, ensure_ascii=True,
                          allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_CREDENTIAL_FILE_BYTES:
            return rejected("session_material_invalid")
        # Catch a cooperating writer's update before making an external query.
        try:
            current = read_credentials_file(credentials_file, api_profile="miniapp_gateway")
        except CredentialError:
            return rejected("session_baseline_changed")
        if current != previous:
            return rejected("session_baseline_changed")
        failure = False
        try:
            client = SushiroClient(candidate.authorization, timeout_seconds=timeout,
                api_profile="miniapp_gateway", **{key: getattr(candidate, key) for key in _HEADS.values()})
        except Exception:
            failure = True
        if failure:
            return rejected("session_query_setup_failed")
        result["network_attempted"] = True
        try:
            query = client.fetch_store(store_id)
        except Exception:
            return rejected("session_query_failed")
        result.update(http_status=query.http_status, started_at=query.started_at,
                      received_at=query.received_at, elapsed_ms=query.elapsed_ms)
        if not query.ok or query.http_status != 200:
            return rejected({401: "session_query_auth_rejected", 403: "session_query_access_denied"}.get(
                query.http_status, "session_query_failed"))
        try:
            snapshot = normalize_snapshot(query.payload, store_id, request_started_at=query.started_at,
                received_at=query.received_at, elapsed_ms=query.elapsed_ms, data_origin="live",
                api_profile="miniapp_gateway", transport=query.transport)
        except Exception:
            return rejected("session_snapshot_invalid")
        result.update(query_accepted=True, snapshot=snapshot)
        # Reuse the established guarded atomic writer, without inventing a HAR
        # observation, capture timestamp or native-client success claim.
        failure = None
        try:
            durable = _write_private(body, credentials_file, candidate.revision,
                                     candidate, None, expected_current=previous)
        except CaptureError as error:
            failure = ("session_baseline_changed" if error.error_code == "capture_destination_changed"
                       else "session_auth_guard_stop" if error.error_code in {
                           "capture_auth_expired", "capture_auth_expiring", "capture_auth_invalid_claim"}
                       else "session_commit_failed")
        except Exception:
            failure = "session_commit_failed"
        if failure:
            return rejected(failure)
        result.update(committed=True, durability_confirmed=durable, revision=candidate.revision)
        try:
            confirmed = read_credentials_file(credentials_file, api_profile="miniapp_gateway") == candidate
        except CredentialError:
            confirmed = False
        if not confirmed:
            return rejected("session_commit_unconfirmed")
        if not durable:
            return rejected("session_durability_unconfirmed")
        result.update(ok=True, error_code=None)
        return result

    accept.target_app_id = target
    return accept


def renew_from_yyb(prepare_request, *, settings_file, credentials_file,
                   expected_revision, store_id, supplier_timeout=45,
                   initialize_timeout=30, query_timeout=15):
    """One explicit supply → initialization → query → atomic revision attempt.

    Requires a separately verified normal request source. No source defaults,
    background scheduling, retry or cloud upload are provided. The result must
    be checked for ok: initialization alone does not imply query acceptance or
    a durable commit. Bind all three stages to the same prepared target app.
    """
    accept = prepare_verifier(credentials_file=credentials_file,
        expected_revision=expected_revision, store_id=store_id, timeout=query_timeout)
    return initialize_from_yyb(prepare_request, accept, settings_file=settings_file,
        reference_context_file=credentials_file, supplier_timeout=supplier_timeout,
        initialize_timeout=initialize_timeout, expected_app_id=accept.target_app_id)
