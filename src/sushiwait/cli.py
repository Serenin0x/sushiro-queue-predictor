"""Bounded read-only sampling, replay and local quality inspection."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any

from . import __version__
from .auth import describe_authorization
from .bridge import BridgeError, receive_context
from .capture import CaptureError, inspect_capture, write_credentials_from_capture
from .client import QueryResult, SushiroClient
from .credentials import CredentialError, CredentialSource, QueryCredentials, read_credentials_file
from .observations import compute_change, normalize_directory, normalize_snapshot
from .outcomes import OutcomeError, OutcomeStore, public_summary, read_episode
from .localpaths import local_data_directory
from .storage import SnapshotStore
from .transport import sanitize_transport

DEFAULT_DB = str(local_data_directory() / "sushiwait.sqlite3")
API_PROFILES = ("legacy", "miniapp_gateway")
EXPIRY_MARGIN_SECONDS = 30


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)


def client_for(
    args: argparse.Namespace, *, credentials: QueryCredentials | None = None
) -> SushiroClient:
    if credentials is not None:
        if credentials.api_profile != args.api_profile:
            raise ValueError("credentials_profile_mismatch")
        return SushiroClient(credentials.authorization, api_profile=args.api_profile,
            app_client=credentials.app_client, app_code=credentials.app_code,
            user_agent=credentials.user_agent, referer=credentials.referer,
            content_type=credentials.content_type,
            ca_file=os.environ.get("SUSHIWAIT_CA_FILE"))
    authorization = app_client = app_code = None
    user_agent = referer = content_type = None
    if not args.anonymous:
        if args.api_profile == "legacy":
            authorization = os.environ.get("SUSHIWAIT_QUERY_AUTHORIZATION")
        else:
            authorization = os.environ.get("SUSHIWAIT_GATEWAY_AUTHORIZATION")
            app_client = os.environ.get("SUSHIWAIT_GATEWAY_APP_CLIENT")
            app_code = os.environ.get("SUSHIWAIT_GATEWAY_APP_CODE")
            user_agent = os.environ.get("SUSHIWAIT_GATEWAY_USER_AGENT")
            referer = os.environ.get("SUSHIWAIT_GATEWAY_REFERER")
            content_type = os.environ.get("SUSHIWAIT_GATEWAY_CONTENT_TYPE")
    return SushiroClient(authorization, api_profile=args.api_profile,
                        app_client=app_client, app_code=app_code,
                        user_agent=user_agent, referer=referer,
                        content_type=content_type,
                        ca_file=os.environ.get("SUSHIWAIT_CA_FILE"))


def _utc_clock() -> datetime:
    return datetime.now(timezone.utc)


class _PreflightStop(ValueError):
    def __init__(self, error_code: str, auth_status: dict | None = None):
        super().__init__(error_code)
        self.error_code = error_code
        self.auth_status = auth_status


def _authorization_stop_code(status: dict) -> str | None:
    """Share the declared-time guard between inspection and query execution."""
    if status["expiry_source"] == "invalid_claim":
        return "auth_claims_invalid"
    if status["expiry_source"] == "unverified_claim":
        if status["expired"] is True:
            return "auth_declared_expired"
        remaining = status["remaining_seconds"]
        if remaining is not None and remaining <= EXPIRY_MARGIN_SECONDS:
            return "auth_expiring"
    return None


def _inspect_private_authorization(args: argparse.Namespace) -> int:
    """Read one explicit private bundle; never touch environment or transport."""
    metadata = {"api_profile": args.api_profile, "credential_source": "private_file",
                "network_performed": False, "server_acceptance": "unverified"}
    try:
        context = read_credentials_file(args.credentials_file, api_profile=args.api_profile)
    except CredentialError as error:
        emit({"ok": False, **metadata, "error_code": error.error_code})
        return 1
    # Sample the clock after file validation, rather than before potentially slow IO.
    checked_at = _utc_clock()
    status = describe_authorization(context.authorization, now=checked_at)
    stop_code = _authorization_stop_code(status)
    guard_state = "unknown"
    if stop_code is not None:
        guard_state = "stop"
    elif status["expiry_source"] == "unverified_claim" and status["remaining_seconds"] is not None:
        guard_state = "no_declared_stop"
    emit({"ok": True, **metadata, "credential_revision": context.revision,
          "checked_at": checked_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
          **status, "authorization_guard": {"state": guard_state,
              "stop_reason": stop_code, "margin_seconds": EXPIRY_MARGIN_SECONDS}})
    # Inspection succeeded even when declarations require queries to stop.
    return 0


class _QuerySession:
    """An immutable context per GET, with bounded local rechecks while waiting."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.source = CredentialSource(args.api_profile,
            credentials_file=getattr(args, "credentials_file", None), anonymous=args.anonymous)
        self._credentials: QueryCredentials | None = None
        self._client_credentials: QueryCredentials | None = None
        self._client: SushiroClient | None = None
        self.auth_status: dict | None = None
        self.observed_revision: int | None = None
        self._observed_authorization: str | None = None
        self.recovery_count = 0

    def _refresh(self) -> None:
        self.auth_status = None
        try:
            credentials = self.source.current()
        except CredentialError as error:
            raise _PreflightStop(error.error_code) from None
        status = describe_authorization(credentials.authorization, now=_utc_clock())
        self.observed_revision = credentials.revision
        self._observed_authorization = credentials.authorization
        self.auth_status = status
        stop_code = _authorization_stop_code(status)
        if stop_code is not None:
            raise _PreflightStop(stop_code, status)
        self._credentials = credentials

    def client(self) -> SushiroClient:
        self._refresh()
        if self._client is None or self._credentials != self._client_credentials:
            try:
                candidate = client_for(self.args, credentials=self._credentials)
            except (ValueError, OSError, TypeError):
                raise _PreflightStop("client_configuration_error", self.auth_status) from None
            self._client = candidate
            self._client_credentials = self._credentials
        return self._client

    def wait_until(self, deadline: float) -> None:
        while True:
            delay = deadline - time.monotonic()
            if delay <= 0:
                return
            self._refresh()
            # Re-read a replaced bundle at the declared protection deadline,
            # even when the next sample is later. No HTTP happens during waits.
            remaining = self.auth_status["remaining_seconds"]
            if remaining is not None:
                delay = min(delay, remaining - EXPIRY_MARGIN_SECONDS)
            time.sleep(min(delay, 60))


def _record_preflight_stop(
    db: SnapshotStore, store_id: str, api_profile: str, stop: _PreflightStop
) -> None:
    checked_at = _utc_clock().isoformat(timespec="milliseconds").replace("+00:00", "Z")
    db.save_preflight_stop(store_id, stop.error_code, checked_at=checked_at,
                          api_profile=api_profile, auth_status=stop.auth_status)
    output = {"ok": False, "store_id": store_id, "api_profile": api_profile,
              "error_code": stop.error_code, "failure_phase": "preflight",
              "checked_at": checked_at, "http_status": None}
    if stop.auth_status is not None:
        output["auth_status"] = stop.auth_status
    emit(output)


def _await_credentials(session: _QuerySession, stop: _PreflightStop) -> bool:
    """Opt-in bounded pause. A different, higher revision must pass preflight."""
    seconds = getattr(session.args, "wait_for_credentials", 0)
    if not seconds or stop.error_code not in ("auth_declared_expired", "auth_expiring"):
        return False
    stopped_revision = session.observed_revision
    stopped_authorization = session._observed_authorization
    deadline = time.monotonic() + seconds
    metadata = {"api_profile": session.args.api_profile, "network_performed": False}
    emit({"event": "credentials_paused", "timeout_seconds": seconds, **metadata})
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            emit({"event": "credentials_recovery_failed", "error_code": "credentials_wait_timeout", **metadata})
            return False
        time.sleep(min(remaining, 1))
        try:
            session._refresh()
        except _PreflightStop as error:
            if error.error_code in ("auth_declared_expired", "auth_expiring"):
                continue
            emit({"event": "credentials_recovery_failed", "error_code": error.error_code, **metadata})
            return False
        if time.monotonic() >= deadline:
            continue
        if (stopped_revision is not None and session.observed_revision > stopped_revision
                and session._observed_authorization != stopped_authorization):
            session.recovery_count += 1
            emit({"event": "credentials_resumed", "credential_revision": session.observed_revision, **metadata})
            return True


def _collect_client(session: _QuerySession, db: SnapshotStore, store_id: str) -> SushiroClient | None:
    try:
        return session.client()
    except _PreflightStop as stop:
        _record_preflight_stop(db, store_id, session.args.api_profile, stop)
        if not _await_credentials(session, stop):
            return None
    # Recheck once at use time; do not restart the pause budget on a race.
    try:
        return session.client()
    except _PreflightStop as stop:
        _record_preflight_stop(db, store_id, session.args.api_profile, stop)
        return None


def observe(
    client: SushiroClient,
    store_id: str,
    db: SnapshotStore,
    *,
    api_profile: str = "legacy",
) -> bool:
    result = client.fetch_store(store_id)
    if not result.ok:
        db.save_failure(store_id, result, api_profile=api_profile, failure_phase="request")
        emit({"ok": False, "store_id": store_id, "error_code": result.error_code,
              "http_status": result.http_status, "received_at": result.received_at,
              "api_profile": api_profile, "failure_phase": "request",
              "transport": sanitize_transport(result.transport)})
        return False
    try:
        snapshot = normalize_snapshot(result.payload, store_id,
            request_started_at=result.started_at, received_at=result.received_at,
            elapsed_ms=result.elapsed_ms, data_origin="live", api_profile=api_profile,
            transport=result.transport)
    except (ValueError, TypeError, KeyError, OverflowError):
        failure = QueryResult(False, None, "normalization_failed", result.http_status,
                              result.started_at, result.received_at, result.elapsed_ms,
                              transport=result.transport)
        db.save_failure(store_id, failure, api_profile=api_profile, failure_phase="normalization")
        emit({"ok": False, "store_id": store_id, "error_code": failure.error_code,
              "http_status": failure.http_status, "received_at": failure.received_at,
              "api_profile": api_profile, "failure_phase": "normalization"})
        return False
    previous = db.last(store_id, data_origin="live", api_profile=api_profile)
    db.save(snapshot)
    emit({"ok": True, "snapshot": snapshot, "change": compute_change(previous, snapshot)})
    return True


def load_fixture(path: str) -> dict:
    p = Path(path)
    if p.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("fixture_too_large")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("data_origin") != "synthetic":
        raise ValueError("fixture_must_be_explicitly_synthetic")
    if not isinstance(data.get("payload"), dict) or not isinstance(data.get("store_id"), str):
        raise ValueError("invalid_fixture")
    if data.get("api_profile", "legacy") not in API_PROFILES:
        raise ValueError("invalid_fixture_api_profile")
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sushiwait", description="SUSHIWAIT 数据接入与结果记录工具")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("outcome-check", "离线校验本人结果记录，只输出安全统计"),
                           ("outcome-import", "校验并追加私有结果修订，不验证真实性")):
        command = commands.add_parser(name, help=help_text)
        inputs = command.add_mutually_exclusive_group(required=True)
        inputs.add_argument("--input", help="本人明确指定的0600结果JSON，父目录0700")
        inputs.add_argument("--synthetic-fixture", help="只接受明确标记的合成结果，不算真实标签")
        if name == "outcome-import":
            command.add_argument("--db", required=True, help="独立私有结果库，不使用公共快照库")
    outcomes = commands.add_parser("outcome-report", help="只读汇总私有结果，不输出号码、身份或行程时间")
    outcomes.add_argument("--db", required=True)
    outcomes.add_argument("--limit", type=int, default=1000, help="最近1–10000个episode的最新修订")
    auth_status = commands.add_parser(
        "auth-status", help="纯本机查看凭证声明到期时间（不联网、不验证签名）"
    )
    auth_status.add_argument("--api-profile", choices=API_PROFILES, default="legacy")
    auth_status.add_argument("--credentials-file", help="检查显式私有整组；不读取环境、不联网")
    capture_check = commands.add_parser(
        "capture-check", help="离线核对本人指定的正常查询 HAR，仅输出安全元数据"
    )
    capture_check.add_argument("--har", required=True, help="明确指定本机已授权捕获文件")
    capture_import = commands.add_parser(
        "capture-import", help="从指定正常请求生成本机私有上下文（不联网、不续期）"
    )
    capture_import.add_argument("--har", required=True)
    capture_import.add_argument("--entry-index", required=True, type=int,
                                help="capture-check 中的原始 HAR 条目下标，从 0 开始")
    capture_import.add_argument("--output", required=True, help="私有查询上下文的目标文件")
    capture_import.add_argument("--revision", required=True, type=int,
                                help="完整上下文修订号，更新既有文件时必须递增")
    bridge = commands.add_parser("context-bridge", help="最多60秒接收同电脑正常查询上下文；不登录或续期")
    bridge.add_argument("--credentials-file", required=True, help="现有gateway完整私有上下文")
    bridge.add_argument("--session-file", required=True, help="新建0600短时接入配置，结束自动清理")
    bridge.add_argument("--revision", required=True, type=int)
    bridge.add_argument("--store-id", action="append", required=True, help="仅接受明确指定的1–3店")
    bridge.add_argument("--seconds", type=int, default=60, help="接收窗口1–60秒")
    stores = commands.add_parser("stores", help="查询目录并按名称筛选（一次只读请求）")
    stores.add_argument("--api-profile", choices=API_PROFILES, default="legacy",
                        help="显式选择固定接口；不会自动切换或回退")
    stores.add_argument("--match", action="append", default=[])
    stores_auth = stores.add_mutually_exclusive_group()
    stores_auth.add_argument("--anonymous", action="store_true", help="不用任何本地查询凭证进行诊断")
    stores_auth.add_argument("--credentials-file", help="对应接口的完整私有查询上下文")
    snapshot = commands.add_parser("snapshot", help="采集单店一份规范化快照")
    snapshot.add_argument("--api-profile", choices=API_PROFILES, default="legacy")
    snapshot.add_argument("--store-id", required=True)
    snapshot.add_argument("--db", default=DEFAULT_DB)
    snapshot_auth = snapshot.add_mutually_exclusive_group()
    snapshot_auth.add_argument("--anonymous", action="store_true")
    snapshot_auth.add_argument("--credentials-file", help="完整私有查询上下文；不与环境凭证混用")
    collect = commands.add_parser("collect", help="少量门店有界采样；首个查询失败即停止")
    collect.add_argument("--api-profile", choices=API_PROFILES, default="legacy")
    collect.add_argument("--store-id", action="append", required=True)
    collect.add_argument("--interval", type=int, default=60)
    collect.add_argument("--samples", type=int, default=3, help="每店轮数，1–120")
    collect.add_argument("--db", default=DEFAULT_DB)
    collect.add_argument("--wait-for-credentials", type=int, default=0,
                         help="私有文件模式到期后暂停等正常新上下文，0–600秒；默认停止")
    collect_auth = collect.add_mutually_exclusive_group()
    collect_auth.add_argument("--anonymous", action="store_true")
    collect_auth.add_argument("--credentials-file", help="每次查询前重读完整私有上下文；整组原子更新")
    replay = commands.add_parser("replay", help="离线导入明确标记的合成快照（不联网）")
    replay.add_argument("--fixture", action="append", required=True)
    replay.add_argument("--db", default=DEFAULT_DB)
    report = commands.add_parser("report", help="查看本地采样数量、失败数和时间范围")
    report.add_argument("--db", default=DEFAULT_DB)
    return parser


def canonical_store_id(value: str) -> str:
    if not value.isascii() or not value.isdecimal() or not 1 <= len(value) <= 19:
        raise ValueError("invalid_store_id")
    number = int(value)
    if not 0 < number <= 2**63 - 1:
        raise ValueError("invalid_store_id")
    return str(number)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in ("snapshot", "collect", "context-bridge"):
        try:
            if args.command == "snapshot":
                args.store_id = canonical_store_id(args.store_id)
            else:
                args.store_id = list(dict.fromkeys(canonical_store_id(s) for s in args.store_id))
        except ValueError:
            emit({"ok": False, "error_code": "invalid_store_id"})
            return 2
    if args.command == "collect":
        ids = args.store_id
        if not 1 <= len(ids) <= 3 or not 30 <= args.interval <= 3600 or not 1 <= args.samples <= 120:
            emit({"ok": False, "error_code": "invalid_sampling_bounds",
                  "detail": "验证阶段限 1–3 店、30–3600 秒周期、1–120 轮"})
            return 2
        if (not 0 <= args.wait_for_credentials <= 600
                or args.wait_for_credentials and args.credentials_file is None):
            emit({"ok": False, "error_code": "invalid_credential_wait_bounds"})
            return 2
    try:
        if args.command in ("outcome-check", "outcome-import", "outcome-report"):
            try:
                if args.command == "outcome-report":
                    with OutcomeStore(args.db, read_only=True) as outcomes:
                        emit({"ok": True, **outcomes.report(limit=args.limit)})
                else:
                    episode = read_episode(args.input or args.synthetic_fixture,
                        synthetic=bool(args.synthetic_fixture))
                    result = public_summary(episode)
                    if args.command == "outcome-import":
                        with OutcomeStore(args.db) as outcomes:
                            result.update(outcomes.append(episode, now=_utc_clock()))
                    emit({"ok": True, **result})
                return 0
            except OutcomeError as error:
                emit({"ok": False, "error_code": error.error_code, "network_performed": False})
                return 1
            except (CaptureError, OSError, ImportError, sqlite3.Error):
                emit({"ok": False, "error_code": "outcome_database_error", "network_performed": False})
                return 1
        if args.command == "context-bridge":
            try:
                result = receive_context(credentials_file=args.credentials_file,
                    session_file=args.session_file, revision=args.revision,
                    store_ids=tuple(args.store_id), seconds=args.seconds, on_ready=emit)
                emit({"ok": True, **result})
                return 0
            except (BridgeError, CaptureError, CredentialError) as error:
                emit({"ok": False, "error_code": error.error_code,
                      "external_network_performed": False})
                return 1
        if args.command in ("capture-check", "capture-import"):
            try:
                inspection = inspect_capture(args.har, now=_utc_clock())
                if args.command == "capture-check":
                    emit(inspection.public_report())
                else:
                    metadata = write_credentials_from_capture(
                        inspection, entry_index=args.entry_index,
                        destination=args.output, revision=args.revision
                    )
                    emit(metadata)
                return 0
            except CaptureError as error:
                emit({"ok": False, "api_profile": "miniapp_gateway",
                      "error_code": error.error_code, "network_performed": False})
                return 1
        if args.command == "auth-status":
            if args.credentials_file is not None:
                return _inspect_private_authorization(args)
            variable = (
                "SUSHIWAIT_QUERY_AUTHORIZATION"
                if args.api_profile == "legacy"
                else "SUSHIWAIT_GATEWAY_AUTHORIZATION"
            )
            status = describe_authorization(os.environ.get(variable))
            emit({"ok": True, "api_profile": args.api_profile, **status})
            return 0
        if args.command == "stores":
            try:
                session = _QuerySession(args)
                client = session.client()
            except _PreflightStop as stop:
                output = {"ok": False, "api_profile": args.api_profile,
                          "error_code": stop.error_code, "failure_phase": "preflight",
                          "checked_at": _utc_clock().isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                          "http_status": None}
                if stop.auth_status is not None:
                    output["auth_status"] = stop.auth_status
                emit(output)
                return 1
            result = client.fetch_stores()
            if not result.ok:
                emit({"ok": False, "error_code": result.error_code,
                      "http_status": result.http_status, "api_profile": args.api_profile,
                      "transport": sanitize_transport(result.transport)})
                return 1
            stores = normalize_directory(result.payload, api_profile=args.api_profile)
            if args.match:
                stores = [s for s in stores if any(word in str(s["normalized"]["name"]["value"]) for word in args.match)]
            emit({"ok": True, "data_origin": "live", "stores": stores,
                  "received_at": result.received_at, "upstream_freshness": "unknown",
                  "api_profile": args.api_profile, "transport": result.transport})
            return 0
        if args.command == "report":
            if not Path(args.db).is_file():
                emit({"ok": False, "error_code": "database_missing"})
                return 2
            with SnapshotStore(args.db, read_only=True) as db:
                emit(db.report())
            return 0
        with SnapshotStore(args.db) as db:
            if args.command == "snapshot":
                try:
                    session = _QuerySession(args)
                    client = session.client()
                except _PreflightStop as stop:
                    _record_preflight_stop(db, args.store_id, args.api_profile, stop)
                    return 1
                return 0 if observe(client, args.store_id, db, api_profile=args.api_profile) else 1
            if args.command == "collect":
                session = _QuerySession(args)
                for i in range(args.samples):
                    started = time.monotonic()
                    for store_id in ids:
                        before_recovery = session.recovery_count
                        client = _collect_client(session, db, store_id)
                        if client is None:
                            return 1
                        if session.recovery_count != before_recovery:
                            started = time.monotonic()
                        if not observe(client, store_id, db, api_profile=args.api_profile):
                            return 1
                    if i + 1 < args.samples:
                        try:
                            session.wait_until(started + args.interval)
                        except _PreflightStop as stop:
                            _record_preflight_stop(db, ids[0], args.api_profile, stop)
                            if not _await_credentials(session, stop):
                                return 1
                            try:
                                # No catch-up burst after a pause: a full interval.
                                session.wait_until(time.monotonic() + args.interval)
                            except _PreflightStop as stop:
                                _record_preflight_stop(db, ids[0], args.api_profile, stop)
                                return 1
                return 0
            if args.command == "replay":
                # Prevalidate every input before the first database insertion.
                fixtures = [load_fixture(path) for path in args.fixture]
                snapshots = []
                for item in fixtures:
                    received = item.get("observed_at")
                    if not isinstance(received, str):
                        raise ValueError("fixture_observed_at_required")
                    datetime.fromisoformat(received.replace("Z", "+00:00"))
                    snapshots.append(normalize_snapshot(item["payload"], item["store_id"],
                        request_started_at=received, received_at=received, elapsed_ms=0,
                        data_origin="synthetic", api_profile=item.get("api_profile", "legacy")))
                for snapshot in snapshots:
                    previous = db.last(snapshot["store_id"], data_origin="synthetic",
                                       api_profile=snapshot["api_profile"])
                    db.save(snapshot)
                    emit({"ok": True, "snapshot": snapshot, "change": compute_change(previous, snapshot)})
                return 0
    except KeyboardInterrupt:
        emit({"ok": False, "error_code": "interrupted"})
        return 130
    except (OSError, ValueError, TypeError, KeyError, OverflowError, sqlite3.Error):
        # Raw input/transport errors may contain credentials; never print them.
        emit({"ok": False, "error_code": "local_input_or_storage_error"})
        return 2
    return 2
