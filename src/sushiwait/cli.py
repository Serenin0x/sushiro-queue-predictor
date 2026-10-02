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
from .client import QueryResult, SushiroClient
from .credentials import CredentialError, CredentialSource, QueryCredentials
from .observations import compute_change, normalize_directory, normalize_snapshot
from .storage import SnapshotStore

DEFAULT_DB = "data/local/sushiwait.sqlite3"
API_PROFILES = ("legacy", "miniapp_gateway")
EXPIRY_MARGIN_SECONDS = 30


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, allow_nan=False))


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

    def _refresh(self) -> None:
        self.auth_status = None
        try:
            credentials = self.source.current()
        except CredentialError as error:
            raise _PreflightStop(error.error_code) from None
        status = describe_authorization(credentials.authorization, now=_utc_clock())
        self.auth_status = status
        if status["expiry_source"] == "invalid_claim":
            raise _PreflightStop("auth_claims_invalid", status)
        if status["expiry_source"] == "unverified_claim":
            if status["expired"] is True:
                raise _PreflightStop("auth_declared_expired", status)
            remaining = status["remaining_seconds"]
            if remaining is not None and remaining <= EXPIRY_MARGIN_SECONDS:
                raise _PreflightStop("auth_expiring", status)
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
              "api_profile": api_profile, "failure_phase": "request"})
        return False
    try:
        snapshot = normalize_snapshot(result.payload, store_id,
            request_started_at=result.started_at, received_at=result.received_at,
            elapsed_ms=result.elapsed_ms, data_origin="live", api_profile=api_profile)
    except (ValueError, TypeError, KeyError, OverflowError):
        failure = QueryResult(False, None, "normalization_failed", result.http_status,
                              result.started_at, result.received_at, result.elapsed_ms)
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
    parser = argparse.ArgumentParser(prog="sushiwait", description="SushiWait 只读数据验证工具")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    auth_status = commands.add_parser(
        "auth-status", help="纯本机查看凭证声明到期时间（不联网、不验证签名）"
    )
    auth_status.add_argument("--api-profile", choices=API_PROFILES, default="legacy")
    stores = commands.add_parser("stores", help="查询目录并按名称筛选（一次只读请求）")
    stores.add_argument("--api-profile", choices=API_PROFILES, default="legacy",
                        help="显式选择固定接口；不会自动切换或回退")
    stores.add_argument("--match", action="append", default=[])
    stores.add_argument("--anonymous", action="store_true", help="不用任何本地查询凭证进行诊断")
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
    if args.command in ("snapshot", "collect"):
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
    try:
        if args.command == "auth-status":
            variable = (
                "SUSHIWAIT_QUERY_AUTHORIZATION"
                if args.api_profile == "legacy"
                else "SUSHIWAIT_GATEWAY_AUTHORIZATION"
            )
            status = describe_authorization(os.environ.get(variable))
            emit({"ok": True, "api_profile": args.api_profile, **status})
            return 0
        if args.command == "stores":
            result = client_for(args).fetch_stores()
            if not result.ok:
                emit({"ok": False, "error_code": result.error_code,
                      "http_status": result.http_status, "api_profile": args.api_profile})
                return 1
            stores = normalize_directory(result.payload, api_profile=args.api_profile)
            if args.match:
                stores = [s for s in stores if any(word in str(s["normalized"]["name"]["value"]) for word in args.match)]
            emit({"ok": True, "data_origin": "live", "stores": stores,
                  "received_at": result.received_at, "upstream_freshness": "unknown",
                  "api_profile": args.api_profile})
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
                        try:
                            client = session.client()
                        except _PreflightStop as stop:
                            _record_preflight_stop(db, store_id, args.api_profile, stop)
                            return 1
                        if not observe(client, store_id, db, api_profile=args.api_profile):
                            return 1
                    if i + 1 < args.samples:
                        try:
                            session.wait_until(started + args.interval)
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
