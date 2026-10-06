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
from .calendar import CalendarError, date_features
from .client import QueryResult, SushiroClient
from .credentials import CredentialError, CredentialSource, QueryCredentials, read_credentials_file
from .observations import compute_change, normalize_directory, normalize_snapshot
from .outcomes import OutcomeError, OutcomeStore, public_summary, read_episode
from .localpaths import local_data_directory
from .storage import SnapshotStore, _report_payload
from .signals import SignalError, signal_report
from .storeview import StoreViewError, store_view, validate_view_scope
from .packets import PacketError, export_packet
from .receipts import ReceiptError, read_packet, packet_summary, archive_packet
from .pending import PendingError, enqueue_packet, pending_status
from .receiver import ReceiverError,read_receiver_token,run_receiver,validate_receiver_limits
from .delivery import DeliveryError,deliver_local,check_confirmation
from .surge import SurgeError, receive_summary
from .surgeguard import GuardError, run_guard
from .promotion import PromotionError, promote_context
from .monitoring import MonitoringError, polling_policy
from .shared_monitoring import SharedMonitoringError, read_plan_file, shared_polling_policy
from .window import WindowError, run_window
from .evaluation import EvaluationError, evaluate_document, read_evaluation_document
from .cohort import CohortError, cohort_report, reconstruct_claims
from .intake import IntakeError, OutcomeIntakeStore
from .transport import sanitize_transport
from .tasks import CollectionTask, TaskError, public_task, task_status
from .adaptive import ScheduleError, schedule_from_file, run_adaptive

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

    def __init__(self, args: argparse.Namespace, *, on_context=None):
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
        self.last_stop_code: str | None = None
        self._on_context = on_context

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
        if self._on_context is not None:
            try:
                self._on_context(credentials, stop_code)
            except CredentialError as error:
                raise _PreflightStop(error.error_code, status) from None
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
    if not seconds or stop.error_code not in ("auth_declared_expired", "auth_expiring", "credentials_refresh_required"):
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
            if error.error_code in ("auth_declared_expired", "auth_expiring", "credentials_refresh_required"):
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
        session.last_stop_code = stop.error_code
        _record_preflight_stop(db, store_id, session.args.api_profile, stop)
        if not _await_credentials(session, stop):
            return None
    # Recheck once at use time; do not restart the pause budget on a race.
    try:
        return session.client()
    except _PreflightStop as stop:
        session.last_stop_code = stop.error_code
        _record_preflight_stop(db, store_id, session.args.api_profile, stop)
        return None


def _recovery_poll_target(deadline: float, store_count: int, interval: int,
                          last_completed: float | None = None) -> float:
    # One store retains its already reserved target. If it is past, query the
    # current state once and anchor subsequent periods to that new start.
    # A completed multi-store round can reuse its reserved target, but never
    # start before a full period after its final observation completed. This
    # bounds every previous query start even if serial response times varied.
    # An explicit restart has no same-process completion bound; keep its
    # conservative full-period policy rather than infer monotonic time.
    if store_count == 1:
        return deadline
    return max(deadline, last_completed + interval) if last_completed is not None else time.monotonic() + interval


def _recovered_round_client(session: _QuerySession, db: SnapshotStore, store_id: str,
                            *, last_completed: float, interval: int) -> SushiroClient | None:
    # The declaration can cross its protection boundary between a finished
    # round wait and the first GET preflight. Apply the same completion bound
    # when that GET itself performed the one permitted recovery wait.
    try:
        session.wait_until(last_completed + interval)
        return session.client()
    except _PreflightStop as stop:
        session.last_stop_code = stop.error_code
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


def _persistent_collect(args: argparse.Namespace) -> int:
    """Resume an explicitly selected bounded task; never silently replay a slot."""
    config = {"db": os.path.abspath(args.db), "api_profile": args.api_profile,
              "store_ids": args.store_id, "interval": args.interval, "samples": args.samples,
              "wait_for_credentials": args.wait_for_credentials}
    if args.transient_failure_budget:
        config["transient_failure_budget"] = args.transient_failure_budget
    with CollectionTask(args.task_file, config=config, resume=args.resume_task, now=_utc_clock()) as task:
        task.prepare_database()
        with SnapshotStore(args.db, read_only=task.value["state"] == "completed") as db:
            task.bind(db, now=_utc_clock())
            emit({"event": "collection_task_restored" if task.loaded else "collection_task_created",
                  **public_task(task.value)})
            if task.value["state"] == "completed":
                return 0 if task.value["failed"] == task.value["uncertain"] == 0 else 1
            session = _QuerySession(args, on_context=task.check_context)
            last_completed = None
            round_transient_failure = False

            def wait(deadline: float) -> bool:
                try:
                    session.wait_until(deadline)
                    return True
                except _PreflightStop as stop:
                    _record_preflight_stop(db, args.store_id[0], args.api_profile, stop)
                    task.stop(stop.error_code, now=_utc_clock())
                    if not _await_credentials(session, stop):
                        emit({"event": "collection_task_stopped", **public_task(task.value)})
                        return False
                try:
                    session.wait_until(_recovery_poll_target(deadline, len(args.store_id), args.interval, last_completed))
                    return True
                except _PreflightStop as stop:
                    _record_preflight_stop(db, args.store_id[0], args.api_profile, stop)
                    task.stop(stop.error_code, now=_utc_clock())
                    emit({"event": "collection_task_stopped", **public_task(task.value)})
                    return False

            # Every unfinished restarted task waits a full period, including a
            # partially completed round. Downtime cannot create a catch-up burst.
            if task.loaded and task.value["last_attempt_at"] is not None:
                if not wait(time.monotonic() + args.interval):
                    return 1
            count = len(args.store_id)
            started = time.monotonic()
            while task.value["cursor"] < count * args.samples:
                if task.value["cursor"] % count == 0:
                    started = time.monotonic()
                    round_transient_failure = False
                store_id = args.store_id[task.value["cursor"] % count]
                before_recovery = session.recovery_count
                client = _collect_client(session, db, store_id)
                if client is not None and session.recovery_count != before_recovery:
                    if count > 1 and task.value["cursor"] % count == 0 and last_completed is not None:
                        client = _recovered_round_client(session, db, store_id,
                            last_completed=last_completed, interval=args.interval)
                    started = time.monotonic()
                if client is None:
                    task.stop(session.last_stop_code or "client_configuration_error", now=_utc_clock())
                    emit({"event": "collection_task_stopped", **public_task(task.value)})
                    return 1
                task.begin(now=_utc_clock())
                before_id = task.value["pending"]["after_sample_id"]
                ok = observe(client, store_id, db, api_profile=args.api_profile)
                last_completed = time.monotonic()
                task.reconcile(now=_utc_clock())
                if not ok:
                    if (not args.transient_failure_budget
                            or task.value["failed"] > args.transient_failure_budget
                            or not _transient_failure_after(db, store_id, args.api_profile, before_id)):
                        emit({"event": "collection_task_checkpoint", **public_task(task.value)})
                        return 1
                    round_transient_failure = True
                    task.continue_after_transient_failure(now=_utc_clock())
                    emit({"event": "transient_query_failure_recorded", "store_id": store_id,
                          "failed_slots": task.value["failed"],
                          "transient_failure_budget": args.transient_failure_budget,
                          "failed_slot_retried": False})
                emit({"event": "collection_task_checkpoint", **public_task(task.value)})
                if task.value["cursor"] < count * args.samples and task.value["cursor"] % count == 0:
                    deadline = started + args.interval
                    if round_transient_failure:
                        deadline = max(deadline, last_completed + args.interval)
                    if not wait(deadline):
                        return 1
            return 0 if task.value["failed"] == task.value["uncertain"] == 0 else 1


def _transient_failure_after(db: SnapshotStore, store_id: str, profile: str, after_id: int) -> bool:
    """Recognize only this exact failed attempt, never an older saved failure."""
    rows = db.db.execute(
        "SELECT ok,CASE WHEN length(CAST(payload_json AS BLOB))<=65536 THEN payload_json ELSE NULL END "
        "FROM samples WHERE id>? AND run_id=? AND store_id=? AND data_origin='live' "
        "AND api_profile=? ORDER BY id LIMIT 2", (after_id, db.run_id, store_id, profile)).fetchall()
    if len(rows) != 1 or rows[0][0] != 0:
        return False
    value = _report_payload(rows[0][1])
    return bool(value and value.get("store_id") == store_id and value.get("api_profile") == profile
        and value.get("data_origin") == "live" and value.get("failure_phase") == "request"
        and value.get("error_code") == "http_error" and type(value.get("http_status")) is int
        and value["http_status"] in (502, 503, 504))


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
    for name in ("remote-snapshot", "remote-collect", "remote-report", "remote-monitor"):
        command = commands.add_parser(name, help="独立匿名CRM排队来源；不使用微信凭证，不提供预测")
        command.add_argument("--db", required=True, help="独立私有库；父目录已存在且0700，不使用既有快照库")
        command.add_argument("--store-id", required=True, **({"action": "append"} if name in ("remote-collect", "remote-monitor") else {}))
        if name == "remote-collect":
            command.add_argument("--interval", type=int, default=60)
            command.add_argument("--samples", type=int, default=1)
            command.add_argument("--task-file", help="明确私有任务文件；保存匿名成对查询进度，不重发未知槽位")
            command.add_argument("--resume-task", action="store_true", help="显式恢复相同有界任务；不恢复失败任务或补采缺口")
        if name == "remote-report":
            command.add_argument("--limit", type=int, default=1000)
        if name == "remote-monitor":
            command.add_argument("--plan-file", required=True, help="明确0600私人计划文件，父目录0700；不输出个人时间")
            command.add_argument("--base-interval", type=int, default=300, help="窗口外周期60–3600秒；30/15分钟前请求60/30秒")
            command.add_argument("--duration", type=int, default=3600, help="30–3600秒，限制新成对查询开始；在途两请求可随后完成")
            command.add_argument("--max-pairs", type=int, default=120, help="1–360成对查询，每对最多两次HTTP；首错停止")
    remote_status = commands.add_parser("remote-task-status", help="只读匿名任务摘要；不联网、不检查进程存活")
    remote_status.add_argument("--task-file", required=True)
    for name, help_text in (("outcome-check", "离线校验本人结果记录，只输出安全统计"),
                           ("outcome-import", "校验并追加私有结果修订，不验证真实性"),
                           ("outcome-receive", "将新结果修订接入独立私有首次接收库；不认证训练标签")):
        command = commands.add_parser(name, help=help_text)
        inputs = command.add_mutually_exclusive_group(required=True)
        inputs.add_argument("--input", help="本人明确指定的0600结果JSON，父目录0700")
        inputs.add_argument("--synthetic-fixture", help="只接受明确标记的合成结果，不算真实标签")
        if name in ("outcome-import", "outcome-receive"):
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
    bridge.add_argument("--diagnostics", action="store_true",
                        help="只报告本机有效投递计数和固定拒绝原因，不输出请求或凭证")
    surge = commands.add_parser("context-surge", help="最多60秒观察电脑Surge中的正常目录查询，无需HAR；不启用解密或登录")
    surge.add_argument("--credentials-file", required=True, help="现有gateway完整私有上下文")
    surge.add_argument("--revision", required=True, type=int, help="正常新上下文的递增修订号")
    surge.add_argument("--seconds", type=int, default=60, help="观察窗口1–60秒；须另行及时关闭临时解密")
    guard = commands.add_parser("surge-guard", help="独立1–45秒仅关闭本机Surge解密；不启用、不更新凭证或恢复域名")
    guard.add_argument("--seconds", type=int, default=40,
                       help="准备后1–45秒发出关闭并核验；须在另一个进程运行，初始三开关须关闭")
    window = commands.add_parser("context-window", help="协调私有暂存接入与独立限时关闭；不启用解密、不改主配置")
    window.add_argument("--credentials-file", required=True, help="现有完整主上下文；只读并协调锁定")
    window.add_argument("--staged-file", required=True, help="另一私有目录中的相同上下文副本")
    window.add_argument("--revision", required=True, type=int)
    window.add_argument("--seconds", type=int, default=40, help="5–40秒独立关闭上限")
    window.add_argument("--collector-paused", action="store_true", help="确认相关采集均已暂停或未运行；工具不认证此状态")
    promotion = commands.add_parser("context-promote", help="调试关闭后显式原子提交完整私有暂存上下文；不查询或登录")
    promotion.add_argument("--credentials-file", required=True, help="当前gateway私有上下文")
    promotion.add_argument("--staged-file", required=True, help="完整新gateway私有暂存上下文")
    promotion.add_argument("--expected-revision", required=True, type=int, help="预计当前版本；不符则保留原文件")
    monitor = commands.add_parser("monitor-plan", help="离线核对用餐偏移与60/30秒刷新目标；不采集、预测或通知")
    monitor.add_argument("--desired-arrival-at", required=True, help="带时区的理想到店时间")
    monitor.add_argument("--as-of", required=True, help="带时区的策略判断时刻")
    monitor.add_argument("--base-interval", required=True, type=int, help="窗口外目标周期60–3600秒；不代表上游允许频率")
    monitor.add_argument("--call-offset-minutes", type=int, default=0, help="-1440至1440；+10表示到店后10分钟叫号，-10表示提前10分钟")
    monitor.add_argument("--plan-status", choices=("waiting", "called", "no_show", "cancelled", "ended"), default="waiting")
    monitor.add_argument("--earliest-call-at", help="外部提供的最早叫号估计；本工具不认证或计算预测")
    monitor.add_argument("--accelerated-display-turnover", action="store_true", help="外部展示集合加速输入；不当真实过号率")
    shared = commands.add_parser("monitor-stores", help="离线合并同店刷新需求和时间边界；不启动调度或查询")
    shared.add_argument("--plans-file", required=True, help="明确的私有16KiB计划文件；不读取凭证")
    shared.add_argument("--as-of", required=True, help="带时区的判断时刻")
    shared.add_argument("--base-interval", required=True, type=int, help="窗口外目标周期60–3600秒")
    adaptive = commands.add_parser("monitor-collect", help="按私有计划合并同店60/30秒只读查询；有界运行、首错停止")
    adaptive.add_argument("--plans-file", required=True, help="明确的私有16KiB计划；不接受外部last_poll_started_at")
    adaptive.add_argument("--credentials-file", required=True, help="完整私有查询上下文；每次GET前及等待重读")
    adaptive.add_argument("--api-profile", choices=API_PROFILES, required=True)
    adaptive.add_argument("--store-id", action="append", required=True, help="明确允许的1–3店，与计划相符")
    adaptive.add_argument("--db", required=True)
    adaptive.add_argument("--base-interval", type=int, default=300)
    adaptive.add_argument("--duration-seconds", type=int, default=300, help="运行30–3600秒；截止后不开始新查询")
    adaptive.add_argument("--max-queries", type=int, default=120, help="全店合计1–360次；不补发错过的周期")
    adaptive.set_defaults(anonymous=False)
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
    collect = commands.add_parser("collect", help="少量门店有界采样；默认首错停止，可显式容忍有限5xx")
    collect.add_argument("--api-profile", choices=API_PROFILES, default="legacy")
    collect.add_argument("--store-id", action="append", required=True)
    collect.add_argument("--interval", type=int, default=60)
    collect.add_argument("--samples", type=int, default=3, help="每店轮数，1–120")
    collect.add_argument("--db", default=DEFAULT_DB)
    collect.add_argument("--wait-for-credentials", type=int, default=0,
                         help="私有文件模式到期后暂停等正常新上下文，0–600秒；默认停止")
    collect.add_argument("--task-file", help="私有有界任务进度；保存槽位/对账并跨重启检查凭证版本")
    collect.add_argument("--resume-task", action="store_true", help="显式恢复已有task-file；不重发未知或失败槽位")
    collect.add_argument("--transient-failure-budget", type=int, default=0,
                        help="可选1–10次502/503/504失败后继续后续槽位；0保持首错停止，不重试失败槽位")
    collect_auth = collect.add_mutually_exclusive_group()
    collect_auth.add_argument("--anonymous", action="store_true")
    collect_auth.add_argument("--credentials-file", help="每次查询前重读完整私有上下文；整组原子更新")
    replay = commands.add_parser("replay", help="离线导入明确标记的合成快照（不联网）")
    replay.add_argument("--fixture", action="append", required=True)
    replay.add_argument("--db", default=DEFAULT_DB)
    calendar = commands.add_parser("date-features", help="离线日期分类；明确预测当时，不推门店营业或等待时间")
    calendar.add_argument("--at", required=True, help="目标时刻，带显式时区和秒")
    calendar.add_argument("--as-of", required=True, help="使用信息的当时，带显式时区和秒")
    evaluation = commands.add_parser("interval-evaluate", help="离线核对区间叫号记录的误差与覆盖界；不预测或认证标签")
    evaluation.add_argument("--input", required=True, help="明确的私有16KiB JSON记录；仅输出聚合算术")
    cohort = commands.add_parser("outcome-cohort", help="只读核对历史结果声明与完整修订链；不认证训练标签")
    cohort.add_argument("--db", required=True)
    cohort.add_argument("--as-of", required=True, help="重建当时，含时区和秒；依据记录声明而非接收证据")
    cohort.add_argument("--data-origin", choices=("synthetic", "self_reported"), required=True)
    cohort.add_argument("--api-profile", choices=API_PROFILES, required=True)
    cohort.add_argument("--max-revisions", type=int, default=10000)
    intake_cohort = commands.add_parser("outcome-received-cohort", help="只读按本机首次接收时间回放结果修订；不认证标签或时钟")
    intake_cohort.add_argument("--db", required=True)
    intake_cohort.add_argument("--as-of", required=True, help="带时区和秒的回放时刻，不是调用者recorded_at声明")
    intake_cohort.add_argument("--data-origin", choices=("synthetic", "self_reported"), required=True)
    intake_cohort.add_argument("--api-profile", choices=API_PROFILES, required=True)
    intake_cohort.add_argument("--max-revisions", type=int, default=10000)
    report = commands.add_parser("report", help="查看本地采样数量、失败数和时间范围")
    report.add_argument("--db", default=DEFAULT_DB)
    task_report = commands.add_parser("task-status", help="只读私有采集任务的安全摘要；不查门店或凭证")
    task_report.add_argument("--task-file", required=True)
    signals = commands.add_parser("signal-report", help="只读比较展示集合的时间窗口；不推真实过号率")
    signals.add_argument("--db", required=True)
    signals.add_argument("--store-id", required=True)
    signals.add_argument("--api-profile", choices=API_PROFILES, required=True)
    signals.add_argument("--data-origin", choices=("live", "fixture", "synthetic"), required=True)
    signals.add_argument("--as-of", required=True, help="分析当时，必须有秒和显式时区")
    signals.add_argument("--window-seconds", type=int, default=120)
    signals.add_argument("--max-gap-seconds", type=int, default=90)
    signals.add_argument("--sample-limit", type=int, default=10000)
    view = commands.add_parser("store-view", help="只读本机门店展示号及刷新状态；不查询、预测或认定叫号")
    view.add_argument("--db", required=True)
    view.add_argument("--store-id", required=True)
    view.add_argument("--api-profile", choices=API_PROFILES, required=True)
    view.add_argument("--data-origin", choices=("live", "fixture", "synthetic"), required=True)
    view.add_argument("--as-of", required=True, help="展示当时，含秒和显式时区")
    view.add_argument("--max-age-seconds", type=int, default=90, help="1–3600秒；只是本机响应年龄阈值")
    view.add_argument("--sample-limit", type=int, default=1000, help="最近1–10000条同范围记录")
    view.add_argument("--task-file", help="可选同库私有任务；联合展示多店已保存停采状态，不检查进程存活")
    packet = commands.add_parser("packet-export", help="只读导出门店公共字段到私有文件；不上传或读取凭证")
    packet.add_argument("--db", required=True, help="明确的私有DB2，文件0600或0400/父目录0700")
    packet.add_argument("--output", required=True, help="新的0600文件，已有0700目录；拒绝覆盖")
    packet.add_argument("--store-id", action="append", required=True, help="明确1–3店的规范ID，不合并重复输入")
    packet.add_argument("--api-profile", choices=API_PROFILES, required=True)
    packet.add_argument("--data-origin", choices=("live", "fixture", "synthetic"), required=True)
    packet.add_argument("--as-of", required=True, help="带秒与时区的资料截止时间；选中未来记录时整页拒绝")
    packet.add_argument("--after-id", type=int, default=0, help="本机同库同范围的已保存游标，不代表服务端确认")
    packet.add_argument("--limit", type=int, default=1000, help="本次按ID升序选择1–1000条")
    for name, description in (("packet-check", "校验私有公共字段包的格式/校验值；不认证来源或联网"),
                              ("packet-archive", "事务归档公共字段包，重复不新增、冲突整批停止；不启动服务器")):
        receipt = commands.add_parser(name, help=description)
        receipt.add_argument("--input", required=True, help="明确私有0600或0400 JSON/0700目录，最多4MiB")
        if name == "packet-archive":
            receipt.add_argument("--db", required=True, help="独立私有归档库schema1，不能用快照/个人结果库")
    for name, description in (("packet-enqueue", "保存公共字段包到有界私有待确认目录；不上传或删除"),
                              ("pending-status", "只读校验私有待确认目录并输出聚合数量；不上传")):
        pending = commands.add_parser(name, help=description)
        pending.add_argument("--directory", required=True, help="本人已有0700专用目录，最多128包")
        if name == "packet-enqueue":
            pending.add_argument("--input", required=True, help="明确私有公共字段包，不读取查询凭证")
    receiver = commands.add_parser("packet-receiver", help="有界本机回环接收服务；独立凭证、验证后归档，不部署远端")
    receiver.add_argument("--db", required=True, help="本人0700目录中的独立归档库")
    receiver.add_argument("--receiver-token-file", required=True, help="独立私有接收端口令文件，不能使用寿司郎凭证")
    receiver.add_argument("--port", type=int, default=0, help="127.0.0.1端口；0为随机空闲端口")
    receiver.add_argument("--seconds", type=int, default=30, help="1–60秒，独立截止关闭连接")
    receiver.add_argument("--max-requests", type=int, default=10, help="最多1–100个连接，包含拒绝的连接")
    deliver = commands.add_parser("packet-deliver-local", help="仅投递127.0.0.1并保存核对过的签名回执；不删除原包")
    deliver.add_argument("--input", required=True, help="明确的私有公共字段包")
    deliver.add_argument("--receiver-token-file", required=True, help="独立私有接收端口令；不能使用查询凭证")
    deliver.add_argument("--port", type=int, required=True, help="明确的127.0.0.1端口，1–65535")
    deliver.add_argument("--confirmation", required=True, help="0700目录中的新0600确认文件，拒绝覆盖")
    confirm = commands.add_parser("receipt-check", help="离线复核历史签名回执；不验证当前接收库或来源")
    confirm.add_argument("--input", required=True, help="原私有公共字段包")
    confirm.add_argument("--receiver-token-file", required=True, help="原独立私有接收端口令")
    confirm.add_argument("--confirmation", required=True, help="私有历史确认文件")
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
    if args.command in ("remote-snapshot", "remote-collect", "remote-report", "remote-monitor", "remote-task-status"):
        from .remote import RemoteClient, RemoteStore, collect_remote, monitor_remote
        from .remotetasks import RemoteTask, RemoteTaskError, collect_remote_task, remote_task_status, task_config
        try:
            if args.command == "remote-task-status":
                emit(remote_task_status(args.task_file))
                return 0
            ids = list(dict.fromkeys(canonical_store_id(s) for s in args.store_id)) if args.command in ("remote-collect", "remote-monitor") else [canonical_store_id(args.store_id)]
            if args.command == "remote-collect" and (not 1 <= len(ids) <= 3 or not 30 <= args.interval <= 3600 or not 1 <= args.samples <= 120):
                raise ValueError("invalid_sampling_bounds")
            if args.command == "remote-collect" and args.resume_task and not args.task_file:
                raise RemoteTaskError("remote_task_required_for_resume")
            if args.command == "remote-collect" and args.task_file:
                config = task_config(args.db, ids, args.interval, args.samples)
                with RemoteTask(args.task_file, config=config, resume=args.resume_task, now=_utc_clock()) as task:
                    task.prepare_database()
                    with RemoteStore(args.db) as database:
                        task.bind(database, now=_utc_clock())
                        summary = collect_remote_task(task, RemoteClient(), wall_clock=_utc_clock,
                            monotonic_clock=time.monotonic, sleep=time.sleep, emit=emit)
                        return 0 if summary['ok'] else 1
            if args.command == "remote-report" and not 1 <= args.limit <= 10000:
                raise ValueError("invalid_report_bounds")
            if args.command == "remote-monitor":
                if (not 1 <= len(ids) <= 3 or not 60 <= args.base_interval <= 3600
                        or not 30 <= args.duration <= 3600 or not 1 <= args.max_pairs <= 360):
                    raise ValueError("invalid_monitor_bounds")
                schedule = schedule_from_file(args.plan_file, allowed_stores=ids,
                    base_interval=args.base_interval, duration_seconds=args.duration,
                    max_queries=args.max_pairs, wall=datetime.now(timezone.utc).isoformat(),
                    monotonic=time.monotonic())
            with RemoteStore(args.db, read_only=args.command == "remote-report") as database:
                if args.command == "remote-report":
                    emit(database.report(ids[0], limit=args.limit))
                    return 0
                client = RemoteClient()
                if args.command == "remote-snapshot":
                    record = client.snapshot(ids[0])
                    emit({"id": database.append(record), "record": record})
                    return 0 if record["ok"] else 1
                if args.command == "remote-monitor":
                    summary = monitor_remote(schedule, client, database,
                        wall_clock=lambda: datetime.now(timezone.utc), monotonic_clock=time.monotonic,
                        sleep=time.sleep, emit=emit)
                    return 0 if summary['ok'] else 1
                summary = collect_remote(client, database, ids, interval=args.interval, samples=args.samples, emit=emit)
                emit({"collection_summary": summary})
                return 0 if summary["ok"] else 1
        except KeyboardInterrupt:
            emit({"ok": False, "source": "crm_remote_v1_1", "error_code": "interrupted"})
            return 130
        except RemoteTaskError as error:
            emit({"ok": False, "source": "crm_remote_v1_1", "error_code": str(error)})
            return 2
        except (OSError, ValueError, TypeError, KeyError, OverflowError, sqlite3.Error):
            emit({"ok": False, "source": "crm_remote_v1_1", "error_code": "remote_input_or_storage_error"})
            return 2
    if args.command in ("snapshot", "collect", "context-bridge", "signal-report", "store-view", "monitor-collect"):
        try:
            if args.command in ("snapshot", "signal-report", "store-view"):
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
        if (not 0 <= args.transient_failure_budget <= 10
                or args.transient_failure_budget and args.credentials_file is None):
            emit({"ok": False, "error_code": "invalid_transient_failure_budget"})
            return 2
        if (args.resume_task and not args.task_file or args.task_file and not args.credentials_file):
            emit({"ok": False, "error_code": "invalid_collection_task_options"})
            return 2
        if args.task_file:
            paths = [os.path.abspath(p) for p in (args.task_file, args.db, args.credentials_file)]
            if len(set(paths)) != 3 or paths[1] == paths[0] + ".lock" or paths[2] == paths[0] + ".lock":
                emit({"ok": False, "error_code": "collection_task_path_conflict"})
                return 2
    try:
        if args.command == "monitor-collect":
            try:
                paths = [Path(p).resolve() for p in (args.plans_file, args.credentials_file, args.db)]
                if (len(set(paths)) != 3 or any(
                        a.exists() and b.exists() and os.path.samefile(a, b)
                        for i, a in enumerate(paths) for b in paths[i + 1:])):
                    raise ScheduleError("schedule_path_conflict")
                schedule = schedule_from_file(args.plans_file, allowed_stores=args.store_id,
                    base_interval=args.base_interval, duration_seconds=args.duration_seconds,
                    max_queries=args.max_queries, wall=_utc_clock().isoformat(), monotonic=time.monotonic())
                session = _QuerySession(args)
                if schedule.decision(wall=_utc_clock().isoformat(), monotonic=time.monotonic())["done"]:
                    return run_adaptive(schedule, session, None, wall_clock=_utc_clock,
                        monotonic_clock=time.monotonic, observe=observe,
                        record_stop=_record_preflight_stop, emit=emit, preflight_stop=_PreflightStop)
                with SnapshotStore(args.db) as db:
                    return run_adaptive(schedule, session, db, wall_clock=_utc_clock,
                        monotonic_clock=time.monotonic, observe=observe,
                        record_stop=_record_preflight_stop, emit=emit, preflight_stop=_PreflightStop)
            except ScheduleError as error:
                emit({"ok": False, "error_code": error.error_code,
                      "eta_available": False, "business_operation_performed": False})
                return 1
        if args.command == "monitor-plan":
            try:
                result = polling_policy(desired_arrival_at=args.desired_arrival_at,
                    as_of=args.as_of, base_interval=args.base_interval,
                    call_offset_minutes=args.call_offset_minutes, plan_status=args.plan_status,
                    earliest_call_at=args.earliest_call_at,
                    accelerated_display_turnover=args.accelerated_display_turnover)
                emit({"ok": True, **result})
                return 0
            except MonitoringError as error:
                emit({"ok": False, "error_code": error.error_code, "network_performed": False})
                return 1
        if args.command == "monitor-stores":
            try:
                result = shared_polling_policy(read_plan_file(args.plans_file), as_of=args.as_of,
                                               base_interval=args.base_interval)
                emit({"ok": True, **result})
                return 0
            except SharedMonitoringError as error:
                emit({"ok": False, "error_code": error.error_code, "network_performed": False})
                return 1
        if args.command == "outcome-received-cohort":
            try:
                if not 1 <= args.max_revisions <= 10000:
                    raise IntakeError("outcome_intake_invalid_scope_or_bounds")
                with OutcomeIntakeStore(args.db, read_only=True) as store:
                    result = store.cohort(as_of=args.as_of, data_origin=args.data_origin,
                        api_profile=args.api_profile, max_revisions=args.max_revisions)
                emit({"ok": True, **result})
                return 0
            except IntakeError as error:
                emit({"ok": False, "error_code": error.error_code, "network_performed": False})
                return 1
        if args.command == "outcome-cohort":
            try:
                if not 1 <= args.max_revisions <= 10000:
                    raise CohortError("cohort_invalid_bounds")
                reconstruct_claims([], as_of=args.as_of, data_origin=args.data_origin,
                                   api_profile=args.api_profile)
                with OutcomeStore(args.db, read_only=True) as store:
                    result = cohort_report(store, as_of=args.as_of, data_origin=args.data_origin,
                        api_profile=args.api_profile, max_revisions=args.max_revisions)
                emit({"ok": True, **result})
                return 0
            except (CohortError, OutcomeError) as error:
                emit({"ok": False, "error_code": error.error_code, "network_performed": False})
                return 1
        if args.command == "interval-evaluate":
            try:
                result = evaluate_document(read_evaluation_document(args.input))
                emit({"ok": True, **result})
                return 0
            except EvaluationError as error:
                emit({"ok": False, "error_code": error.error_code, "network_performed": False})
                return 1
        if args.command == "context-window":
            try:
                result = run_window(credentials_file=args.credentials_file, staged_file=args.staged_file,
                                    revision=args.revision, seconds=args.seconds,
                                    collector_paused=args.collector_paused, on_ready=emit)
                emit({"ok": result["durability_confirmed"], **result})
                return 0 if result["durability_confirmed"] else 1
            except WindowError as error:
                emit({"ok": False, "error_code": error.error_code,
                      "staging_changed": error.staging_changed,
                      "cleanup_confirmed": error.cleanup_confirmed,
                      "main_context_updated": False, "external_network_performed": False})
                return 1
        if args.command == "context-promote":
            try:
                result = promote_context(credentials_file=args.credentials_file,
                                         staged_file=args.staged_file,
                                         expected_revision=args.expected_revision)
                emit({"ok": result["durability_confirmed"], **result})
                return 0 if result["durability_confirmed"] else 1
            except PromotionError as error:
                emit({"ok": False, "error_code": error.error_code,
                      "committed": error.committed,
                      "durability_confirmed": error.durability_confirmed,
                      "external_network_performed": False})
                return 1
        if args.command == "surge-guard":
            try:
                result = run_guard(seconds=args.seconds, on_ready=emit)
                emit({"ok": True, **result})
                return 0
            except GuardError as error:
                emit({"ok": False, "error_code": error.error_code,
                      "cleanup_attempted": error.cleanup_attempted,
                      "cleanup_confirmed": error.cleanup_confirmed,
                      "external_network_performed": False, "credentials_accessed": False})
                return 1
        if args.command == "task-status":
            try:
                emit({"ok": True, **task_status(args.task_file), "network_performed": False})
                return 0
            except TaskError as error:
                emit({"ok": False, "error_code": error.error_code, "network_performed": False})
                return 1
        if args.command in ("packet-deliver-local", "receipt-check"):
            try:
                result = (deliver_local(args.input,args.receiver_token_file,args.confirmation,port=args.port,clock=_utc_clock)
                          if args.command == "packet-deliver-local"
                          else check_confirmation(args.input,args.confirmation,args.receiver_token_file,as_of=_utc_clock().isoformat()))
                durable = result.get("durability_confirmed",True)
                emit({"ok":durable,**result})
                return 0 if durable else 1
            except DeliveryError as error:
                emit({"ok":False,"error_code":error.error_code,
                      "confirmation_committed":error.confirmation_committed,
                      "durability_confirmed":False,"receiver_commit_status":error.receiver_commit_status,
                      "query_credentials_accessed":False,"remote_deployment_verified":False})
                return 1
        if args.command == "packet-receiver":
            try:
                validate_receiver_limits(port=args.port,seconds=args.seconds,max_requests=args.max_requests)
                if os.path.abspath(args.db)==os.path.abspath(args.receiver_token_file):
                    raise ReceiverError("receiver_path_conflict")
                token=read_receiver_token(args.receiver_token_file)
                result=run_receiver(args.db,token,port=args.port,seconds=args.seconds,
                                    max_requests=args.max_requests,on_ready=emit)
                emit({"ok":True,**result})
                return 0
            except ReceiverError as error:
                emit({"ok":False,"error_code":error.error_code,
                      "query_credentials_accessed":False,"outbound_network_performed":False,
                      "remote_deployment_verified":False})
                return 1
        if args.command in ("packet-enqueue", "pending-status"):
            try:
                at = _utc_clock().isoformat()
                result = (enqueue_packet(args.input,args.directory,as_of=at)
                          if args.command == "packet-enqueue"
                          else pending_status(args.directory,as_of=at))
                emit({"ok":result.get("durability_confirmed",True), **result})
                return 0 if result.get("durability_confirmed",True) else 1
            except PendingError as error:
                emit({"ok":False,"error_code":error.error_code,"committed":error.committed,
                      "durability_confirmed":False,"server_received":False,
                      "network_performed":False,"credentials_accessed":False})
                return 1
        if args.command in ("packet-check", "packet-archive"):
            try:
                now = _utc_clock()
                if args.command == "packet-check":
                    result = packet_summary(read_packet(args.input, as_of=now.isoformat()))
                    result["validation_only"] = True
                else:
                    result = archive_packet(args.input, args.db, now=now)
                emit({"ok":True, **result})
                return 0
            except ReceiptError as error:
                emit({"ok":False, "error_code":error.error_code,
                      "local_archive_commit_status":error.commit_status,
                      "durability_confirmed":False,"network_performed":False,
                      "credentials_accessed":False,"server_received":False})
                return 1
        if args.command == "packet-export":
            try:
                result = export_packet(args.db, args.output, store_ids=args.store_id,
                    api_profile=args.api_profile, data_origin=args.data_origin,
                    as_of=args.as_of, after_id=args.after_id, limit=args.limit)
                emit({"ok":result["durability_confirmed"], **result,
                      **({} if result["durability_confirmed"] else
                         {"error_code":"packet_durability_unconfirmed"})})
                return 0 if result["durability_confirmed"] else 1
            except PacketError as error:
                emit({"ok":False, "error_code":error.error_code, "committed":error.committed,
                      "durability_confirmed":False, "network_performed":False,
                      "credentials_accessed":False, "server_received":False})
                return 1
        if args.command == "collect" and args.task_file:
            try:
                return _persistent_collect(args)
            except (TaskError, CaptureError) as error:
                emit({"ok": False, "error_code": error.error_code if isinstance(error, TaskError)
                      else "collection_task_unavailable_or_unsafe"})
                return 1
        if args.command == "store-view":
            try:
                validate_view_scope(args.store_id, data_origin=args.data_origin,
                    api_profile=args.api_profile, as_of=args.as_of,
                    max_age_seconds=args.max_age_seconds, sample_limit=args.sample_limit)
                with SnapshotStore(args.db, read_only=True) as db:
                    emit({"ok": True, **store_view(db, args.store_id,
                        data_origin=args.data_origin, api_profile=args.api_profile,
                        as_of=args.as_of, max_age_seconds=args.max_age_seconds,
                        sample_limit=args.sample_limit, task_file=args.task_file)})
                return 0
            except (StoreViewError, TaskError) as error:
                emit({"ok": False, "error_code": error.error_code, "network_performed": False})
                return 1
            except (OSError, ValueError, sqlite3.Error):
                emit({"ok": False, "error_code": "store_view_database_error", "network_performed": False})
                return 1
        if args.command == "signal-report":
            try:
                with SnapshotStore(args.db, read_only=True) as db:
                    emit({"ok": True, **signal_report(db, args.store_id,
                        data_origin=args.data_origin, api_profile=args.api_profile,
                        as_of=args.as_of, window_seconds=args.window_seconds,
                        max_gap_seconds=args.max_gap_seconds, sample_limit=args.sample_limit)})
                return 0
            except (SignalError, CalendarError) as error:
                emit({"ok": False, "error_code": error.error_code, "network_performed": False})
                return 1
            except (OSError, sqlite3.Error):
                emit({"ok": False, "error_code": "signal_database_error", "network_performed": False})
                return 1
        if args.command == "date-features":
            try:
                emit({"ok": True, **date_features(args.at, as_of=args.as_of)})
                return 0
            except CalendarError as error:
                emit({"ok": False, "error_code": error.error_code, "network_performed": False})
                return 1
        if args.command in ("outcome-check", "outcome-import", "outcome-report", "outcome-receive"):
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
                    elif args.command == "outcome-receive":
                        with OutcomeIntakeStore(args.db) as outcomes:
                            result.update(outcomes.append(episode))
                        result.update(availability_basis="local_first_receipt_time",
                                      independent_time_attestation=False)
                    emit({"ok": True, **result})
                return 0
            except (OutcomeError, IntakeError) as error:
                emit({"ok": False, "error_code": error.error_code, "network_performed": False,
                      **({"commit_status": error.commit_status} if isinstance(error, IntakeError) else {})})
                return 1
            except (CaptureError, OSError, ImportError, sqlite3.Error):
                emit({"ok": False, "error_code": "outcome_database_error", "network_performed": False})
                return 1
        if args.command == "context-surge":
            try:
                result = receive_summary(credentials_file=args.credentials_file,
                    revision=args.revision, seconds=args.seconds, on_ready=emit)
                emit({"ok": True, **result})
                return 0
            except (SurgeError, CaptureError, CredentialError) as error:
                emit({"ok": False, "error_code": error.error_code,
                      "network_performed": False, "external_network_performed": False})
                return 1
        if args.command == "context-bridge":
            try:
                result = receive_context(credentials_file=args.credentials_file,
                    session_file=args.session_file, revision=args.revision,
                    store_ids=tuple(args.store_id), seconds=args.seconds, on_ready=emit,
                    diagnostics=args.diagnostics)
                emit({"ok": True, **result})
                return 0
            except (BridgeError, CaptureError, CredentialError) as error:
                emit({"ok": False, "error_code": error.error_code,
                      "external_network_performed": False,
                      **({"diagnostics": error.diagnostics}
                         if isinstance(error, BridgeError) and error.diagnostics is not None else {})})
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
                last_completed = None
                failed = 0
                for i in range(args.samples):
                    started = time.monotonic()
                    round_transient_failure = False
                    for store_index, store_id in enumerate(ids):
                        before_recovery = session.recovery_count
                        client = _collect_client(session, db, store_id)
                        if client is not None and session.recovery_count != before_recovery:
                            if len(ids) > 1 and store_index == 0 and last_completed is not None:
                                client = _recovered_round_client(session, db, store_id,
                                    last_completed=last_completed, interval=args.interval)
                            started = time.monotonic()
                        if client is None:
                            return 1
                        before_id = (db.db.execute("SELECT COALESCE(MAX(id),0) FROM samples").fetchone()[0]
                                     if args.transient_failure_budget else None)
                        ok = observe(client, store_id, db, api_profile=args.api_profile)
                        last_completed = time.monotonic()
                        if not ok:
                            failed += 1
                            if (not args.transient_failure_budget
                                    or failed > args.transient_failure_budget
                                    or not _transient_failure_after(db, store_id, args.api_profile, before_id)):
                                return 1
                            round_transient_failure = True
                            emit({"event": "transient_query_failure_recorded", "store_id": store_id,
                                  "failed_slots": failed,
                                  "transient_failure_budget": args.transient_failure_budget,
                                  "failed_slot_retried": False})
                    if i + 1 < args.samples:
                        deadline = started + args.interval
                        if round_transient_failure:
                            deadline = max(deadline, last_completed + args.interval)
                        try:
                            session.wait_until(deadline)
                        except _PreflightStop as stop:
                            _record_preflight_stop(db, ids[0], args.api_profile, stop)
                            if not _await_credentials(session, stop):
                                return 1
                            try:
                                session.wait_until(_recovery_poll_target(deadline, len(ids), args.interval, last_completed))
                            except _PreflightStop as stop:
                                _record_preflight_stop(db, ids[0], args.api_profile, stop)
                                return 1
                return 1 if failed else 0
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
