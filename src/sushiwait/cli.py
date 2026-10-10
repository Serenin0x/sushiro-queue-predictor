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
from .personal import PersonalError, receive_personal_context, read_personal_context, write_personal_snapshot
from .promotion import PromotionError, promote_context
from .monitoring import MonitoringError, polling_policy
from .shared_monitoring import SharedMonitoringError, read_plan_file, shared_polling_policy
from .window import WindowError, run_window
from .evaluation import EvaluationError, evaluate_document, read_evaluation_document
from .cohort import CohortError, cohort_report, reconstruct_claims
from .intake import IntakeError, OutcomeIntakeStore
from .reviews import ReviewError, OutcomeReviewStore, read_review, draft_review
from .baseline import BaselineError, read_plan as read_baseline_plan, write_baseline
from .backtest import BacktestError, read_backtest_plan, write_backtest
from .features import FeatureError, read_feature_plan, write_feature_dataset
from .fusion import FusionError, read_plan as read_fusion_plan, read_advice, write_fusion
from .deepseek import DeepSeekError, write_deepseek_fusion
from .historyfusion import HistoryFusionError, read_context as read_fusion_context, write_history_fusion
from .tracking import (TrackingError, read_document as read_tracking_document, create_session,
    observe_session, prepare_prediction, calculate_prediction, publish_prediction, end_session, session_status)
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
    personal = commands.add_parser('personal-snapshot',help='查询本人当前普通号单与可选状态历史，仅存私有文件')
    personal.add_argument('--context-file',required=True)
    personal.add_argument('--output-file',required=True)
    personal.add_argument('--include-history',action='store_true')
    context = commands.add_parser('personal-context-surge',help='从正常个人状态请求摘要接入独立私有上下文；不启用调试')
    context.add_argument('--context-file',required=True)
    context.add_argument('--revision',type=int,required=True)
    context.add_argument('--app-id',help='首次接入时绑定本人正常官方小程序的AppID；仅入私有文件')
    context.add_argument('--seconds',type=int,default=35)
    context = commands.add_parser('personal-auth-status',help='离线检查本人只读上下文的声明到期，不发送请求')
    context.add_argument('--context-file',required=True)
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
    remote_signal = commands.add_parser("remote-signal-report", help="只读匿名库的独立响应窗口/日期与展示集合变化；不推断真实过号率")
    remote_signal.add_argument("--db", required=True)
    remote_signal.add_argument("--store-id", required=True)
    remote_signal.add_argument("--as-of", required=True)
    remote_signal.add_argument("--window", type=int, default=120)
    remote_signal.add_argument("--max-gap", type=int, default=90)
    remote_signal.add_argument("--limit", type=int, default=10000)
    remote_signal.add_argument("--availability-basis", choices=("response-completion","local-first-receipt"),
                               default="response-completion", help="响应时刻重建，或新数据的本机首次接收筛选")
    remote_serve = commands.add_parser("remote-serve", help="本机只读号码服务与一份持久匿名任务；固定预算，不提供预测")
    remote_serve.add_argument("--db", required=True)
    remote_serve.add_argument("--task-file", required=True)
    remote_serve.add_argument("--store-id", action="append", required=True)
    remote_serve.add_argument("--interval", type=int, default=60)
    remote_serve.add_argument("--samples", type=int, default=120)
    remote_serve.add_argument("--resume-task", action="store_true")
    remote_serve.add_argument("--stale-after", type=int, help="本机成功响应年龄阈值30–7200秒；不是上游更新频率")
    remote_serve.add_argument("--port", type=int, default=8765, help="1024–65535")
    remote_serve.add_argument("--listen-host",choices=("127.0.0.1","0.0.0.0"),default="127.0.0.1",
                              help="默认仅本机；容器桥接时显式0.0.0.0，并限制宿主发布地址")
    for name in ("remote-window-collect","remote-window-serve"):
        window = commands.add_parser(name,help="持久共享匿名采集窗口；最多72小时，原期限与预算跨重启保持")
        window.add_argument("--db",required=True)
        window.add_argument("--task-file",required=True)
        window.add_argument("--plan-file",required=True,help="明确私有不可变计划；空plans也持续采集背景数据")
        window.add_argument("--plan-updates-file",help="显式私有版本化计划更新；独立0700目录，不能与数据库/任务共用父目录")
        window.add_argument("--business-hours-file",help="显式营业时间JSON；嵌入原任务，闭店不查询，恢复时规则必须一致")
        window.add_argument("--store-id",action="append",required=True)
        window.add_argument("--base-interval",type=int,default=300)
        window.add_argument("--duration",type=int,default=86400)
        window.add_argument("--max-pairs",type=int,default=8640)
        resume_mode=window.add_mutually_exclusive_group()
        resume_mode.add_argument("--resume-task",action="store_true")
        resume_mode.add_argument("--resume-if-present",action="store_true",
                                help="有任务则恢复；任务和数据库均不存在才新建；丢失任务不重置预算")
        if name=="remote-window-serve":
            window.add_argument("--port",type=int,default=8765)
            window.add_argument("--listen-host",choices=("127.0.0.1","0.0.0.0"),default="127.0.0.1")
    window_status = commands.add_parser("remote-window-status",help="只读保存窗口状态；不读计划、不联网、不检查存活")
    window_status.add_argument("--task-file",required=True)
    for name in ("remote-campaign-collect", "remote-campaign-serve"):
        campaign = commands.add_parser(name, help="最多14天的连续采集计划；正常窗口接续，总期限和预算跨重启保持")
        campaign.add_argument("--root", required=True, help="专用空0700状态目录；保留每段窗口，不删除旧库")
        campaign.add_argument("--plan-file", required=True, help="状态目录外的不可变私有计划")
        campaign.add_argument("--plan-updates-file", help="状态目录外独立私有计划更新")
        campaign.add_argument("--business-hours-file",help="显式营业时间JSON；营业外等待，不重置期限或预算")
        campaign.add_argument("--store-id", action="append", required=True)
        campaign.add_argument("--base-interval", type=int, default=300)
        campaign.add_argument("--duration", type=int, default=604800)
        campaign.add_argument("--window-duration", type=int, default=86400)
        campaign.add_argument("--max-pairs", type=int, default=6300)
        campaign.add_argument("--transient-recovery-limit", type=int, choices=range(4), default=0,
                              help="显式允许至多3次瞬时故障后创建新窗口；原期限预算不变，默认首错停止")
        modes = campaign.add_mutually_exclusive_group()
        modes.add_argument("--resume-task", action="store_true")
        modes.add_argument("--resume-if-present", action="store_true")
        if name == "remote-campaign-serve":
            campaign.add_argument("--port", type=int, default=8765)
            campaign.add_argument("--listen-host", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1")
    campaign_status = commands.add_parser("remote-campaign-status", help="只读多日采集检查点；不打开数据库、不联网")
    campaign_status.add_argument("--root", required=True)
    for name in ('remote-daily-collect', 'remote-daily-serve'):
        daily = commands.add_parser(name, help='按营业日建立独立有界任务、归档并等待次日；不续改旧试验')
        daily.add_argument('--root', required=True, help='同店专用0700根目录；与旧试验分开')
        daily.add_argument('--store-id', required=True)
        daily.add_argument('--business-hours-file', required=True)
        daily.add_argument('--not-before', required=True, help='显式接续起始时间；须核对旧同店写者已停止')
        daily.add_argument('--daily-pair-cap', type=int, default=1500)
        if name == 'remote-daily-serve':
            daily.add_argument('--port', type=int, default=18821)
            daily.add_argument('--legacy-exports-root', help='显式旧试采daily-exports私有目录；仅读取')
            daily.add_argument('--legacy-through-date', help='旧导出所属最后日期；须早于新接续本地日期')
    daily_status = commands.add_parser('remote-daily-status', help='只读每日控制器检查点；不启动采集或联网')
    daily_status.add_argument('--root', required=True)
    fleet = commands.add_parser('remote-daily-fleet-serve', help='显式门店目录共享进程、统一限速与单店独立每日归档')
    fleet.add_argument('--root', required=True)
    fleet.add_argument('--catalog-file', required=True)
    fleet.add_argument('--business-hours-file', required=True)
    fleet.add_argument('--not-before', required=True)
    fleet.add_argument('--daily-pair-cap', type=int, default=1500)
    fleet.add_argument('--requests-per-second', type=float, default=5)
    fleet.add_argument('--port', type=int, default=18821)
    fleet.add_argument('--legacy-exports-root')
    fleet.add_argument('--legacy-through-date')
    fleet.add_argument('--legacy-store-id', action='append', default=[])
    archives = commands.add_parser('daily-archive-month', help='只读指定月份的逐日归档；缺失与坏文件分别报告')
    archives.add_argument('--root', required=True)
    archives.add_argument('--store-id', required=True)
    archives.add_argument('--month', required=True)
    archives.add_argument('--legacy-exports-root')
    archives.add_argument('--legacy-through-date')
    hub = commands.add_parser('collection-hub-serve', help='把多个本机采集批次汇入同一观察台；不查询寿司郎、不启动采集')
    hub.add_argument('--config-file', required=True)
    hub.add_argument('--port', type=int, default=51930)
    plans_publish=commands.add_parser("monitor-plans-publish",help="私有计划修订原子发布；不查询、不取号、不发送提醒")
    plans_publish.add_argument("--input",required=True)
    plans_publish.add_argument("--output",required=True)
    plans_publish.add_argument("--store-id",action="append",required=True)
    plans_publish.add_argument("--base-interval",type=int,default=300)
    daily_quality = commands.add_parser('daily-quality-report', help='离线分析已保存日曲线的接收覆盖与断档；不查询来源')
    daily_quality.add_argument('--projection-file', required=True)
    daily_quality.add_argument('--store-id', required=True)
    daily_quality.add_argument('--date', required=True)
    daily_quality.add_argument('--as-of', required=True)
    daily_quality.add_argument('--interval', type=int, default=60)
    daily_quality.add_argument('--max-gap', type=int, default=90)
    window_quality = commands.add_parser("remote-window-quality",help="只读终态窗口的完整结果链、间隔缺口与日期分布；不认证源新鲜度")
    window_quality.add_argument("--db",required=True)
    window_quality.add_argument("--task-file",required=True)
    window_quality.add_argument("--as-of",required=True)
    window_quality.add_argument("--max-gap",type=int,default=360,help="1–7200秒；明确的诊断阈值，不自动认定允许频率")
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
    review_draft = commands.add_parser("outcome-review-draft", help="为已接收的本人结果生成私有未批准审核草稿；不验证事件")
    review_draft.add_argument("--source-db", required=True)
    review_inputs = review_draft.add_mutually_exclusive_group(required=True)
    review_inputs.add_argument("--input")
    review_inputs.add_argument("--synthetic-fixture")
    review_draft.add_argument("--output", required=True)
    review_receive = commands.add_parser("outcome-review-receive", help="保存人工审核声明与首次接收时间；不认证身份或训练标签")
    review_receive.add_argument("--source-db", required=True)
    review_receive.add_argument("--db", required=True)
    review_receive.add_argument("--input", required=True)
    review_cohort = commands.add_parser("outcome-reviewed-cohort", help="只读按结果和审核首次接收回放审核结论；不导出个人数据")
    review_cohort.add_argument("--source-db", required=True)
    review_cohort.add_argument("--db", required=True)
    review_cohort.add_argument("--as-of", required=True)
    review_cohort.add_argument("--data-origin", choices=("self_reported", "synthetic"), required=True)
    review_cohort.add_argument("--api-profile", choices=API_PROFILES, required=True)
    review_cohort.add_argument("--max-revisions", type=int, default=10000)
    baseline = commands.add_parser("baseline-research", help="私有历史区间基线与理想时刻候选搜索；不提供已校准ETA")
    baseline.add_argument("--source-db", required=True)
    baseline.add_argument("--reviews-db", required=True)
    baseline.add_argument("--input", required=True)
    baseline.add_argument("--output", required=True)
    baseline.add_argument("--max-revisions", type=int, default=10000)
    backtest = commands.add_parser("baseline-backtest", help="按当时接收资料重建历史基线并计算误差界；不认证线上预测日志")
    backtest.add_argument("--source-db", required=True)
    backtest.add_argument("--reviews-db", required=True)
    backtest.add_argument("--input", required=True)
    backtest.add_argument("--output", required=True)
    backtest.add_argument("--max-revisions", type=int, default=10000)
    features = commands.add_parser("outcome-feature-export", help="按当时已接收的门店观测对齐私有排队经历；不认证训练标签或ETA")
    features.add_argument("--source-db", required=True)
    features.add_argument("--reviews-db", required=True)
    features.add_argument("--remote-db", required=True)
    features.add_argument("--input", required=True)
    features.add_argument("--output", required=True)
    features.add_argument("--max-revisions", type=int, default=10000)
    features.add_argument("--max-observations", type=int, default=10000)
    fusion = commands.add_parser("fusion-research", help="私有候选分布与公共AI权重融合；不调用模型或提供已校准ETA")
    fusion.add_argument("--input", required=True)
    fusion.add_argument("--advice")
    fusion.add_argument("--output", required=True)
    deepseek = commands.add_parser("deepseek-fusion", help="受限DeepSeek公共权重与私有分布融合；默认不联网，无已校准ETA")
    deepseek.add_argument("--input", required=True)
    deepseek.add_argument("--output", required=True)
    deepseek.add_argument("--budget-file")
    deepseek.add_argument("--api-key-file")
    deepseek.add_argument("--allow-paid-request", action="store_true")
    history = commands.add_parser("history-fusion-research", help="将截至当时可用的私有审核经历转成区间分布；无已校准ETA")
    history.add_argument("--source-db", required=True)
    history.add_argument("--reviews-db", required=True)
    history.add_argument("--input", required=True)
    history.add_argument("--context-file", required=True)
    history.add_argument("--output", required=True)
    history.add_argument("--model-version", default="reviewed-history-intervals-v1")
    history.add_argument("--ai-blend-ppm", type=int, default=0)
    history.add_argument("--max-revisions", type=int, default=10000)
    replay = commands.add_parser('realtime-backtest', help='按历史接收时点公平比较历史、实时和融合误差；不调用供应商')
    for field in ('source-db','reviews-db','remote-db','input','output'):
        replay.add_argument('--'+field, required=True)
    replay.add_argument('--max-revisions', type=int, default=10000)
    replay.add_argument('--max-observations', type=int, default=10000)
    realtime = commands.add_parser('realtime-fusion-research', help='独立经历的实时邻域剩余分布与历史融合；未校准ETA')
    for field in ('source-db','reviews-db','remote-db','input','feature-plan','context-file','output'):
        realtime.add_argument('--'+field, required=True)
    realtime.add_argument('--neighbors', type=int, default=20)
    realtime.add_argument('--minimum-episodes', type=int, default=8)
    realtime.add_argument('--maximum-elapsed-difference-seconds', type=int, default=300)
    realtime.add_argument('--maximum-standardized-distance', type=int, default=3)
    realtime.add_argument('--realtime-weight-ppm', type=int, default=500000)
    realtime.add_argument('--ai-blend-ppm', type=int, default=0)
    realtime.add_argument('--max-revisions', type=int, default=10000)
    realtime.add_argument('--max-observations', type=int, default=10000)
    track = commands.add_parser('ticket-track-init', help='保存手动已有号的私有有界会话；不取号')
    track.add_argument('--ticket-file', required=True)
    track.add_argument('--state-dir', required=True)
    experience = commands.add_parser('ticket-outcome-draft', help='根据本人记录的实际事件生成私有经历草稿；不自动认定叫号')
    experience.add_argument('--state-dir', required=True)
    experience.add_argument('--event-type', required=True, choices=('checked_in','called','seated','no_show','cancelled','observation_ended'))
    experience.add_argument('--event-lower')
    experience.add_argument('--event-upper')
    experience.add_argument('--issued-lower')
    experience.add_argument('--issued-upper')
    experience.add_argument('--previous-file')
    experience.add_argument('--output', required=True)
    bundle = commands.add_parser('experience-bundle-receive', help='把本机下载的经历修订接入私有首次接收库；不自动认证标签')
    bundle.add_argument('--input', required=True)
    bundle.add_argument('--database', required=True)
    bundle.add_argument('--synthetic', action='store_true', help='仅用于明确合成资料')
    track = commands.add_parser('ticket-track-observe', help='绑定已提交公开投影与本人号码；不查询上游')
    track.add_argument('--state-dir', required=True)
    track.add_argument('--view-file', required=True)
    track.add_argument('--context-file', required=True)
    track.add_argument('--as-of', required=True)
    track = commands.add_parser('ticket-track-predict', help='条件剩余计算与当前版本原子提交；研究区间未校准')
    track.add_argument('--state-dir', required=True)
    track.add_argument('--version', required=True, type=int)
    track.add_argument('--source-db')
    track.add_argument('--reviews-db')
    track.add_argument('--fusion-plan-file')
    track.add_argument('--advice-file')
    track.add_argument('--base-interval', type=int, default=300)
    track.add_argument('--model-version', default='reviewed-history-intervals-v1')
    track.add_argument('--ai-blend-ppm', type=int, default=0)
    track = commands.add_parser('ticket-track-end', help='本人声明结束追踪；不取消官方号或制造认证标签')
    track.add_argument('--state-dir', required=True)
    track.add_argument('--status', choices=('called','no_show','cancelled','ended'), required=True)
    track.add_argument('--declared-at', required=True)
    track = commands.add_parser('ticket-track-status', help='查看私有追踪的版本与过期状态；不显示号码')
    track.add_argument('--state-dir', required=True)
    loop = commands.add_parser('ticket-track-run', help='有界自动读取本机采集投影并更新私人追踪；无产品UI或通知')
    loop.add_argument('--state-dir', action='append', required=True)
    loop.add_argument('--store-id', action='append', required=True)
    loop.add_argument('--collector-url')
    loop.add_argument('--duration', type=int, default=3600)
    loop.add_argument('--max-cycles', type=int, default=720)
    loop.add_argument('--read-interval', type=int, default=5)
    loop.add_argument('--base-interval', type=int, default=300)
    loop.add_argument('--source-db')
    loop.add_argument('--reviews-db')
    loop.add_argument('--sealed-remote-db')
    loop.add_argument('--landmark-seconds', action='append', type=int)
    loop.add_argument('--realtime-window-seconds', type=int, default=1800)
    loop.add_argument('--realtime-neighbors', type=int, default=20)
    loop.add_argument('--realtime-minimum-episodes', type=int, default=8)
    loop.add_argument('--model-version', default='reviewed-history-intervals-v1')
    loop.add_argument('--ai-blend-ppm', type=int, default=0)
    loop.add_argument('--plan-output')
    loop.add_argument('--plan-series-id')
    loop.add_argument('--prepare-plans-only', action='store_true')
    loop.add_argument('--allow-paid-request', action='store_true')
    loop.add_argument('--budget-file')
    loop.add_argument('--key-file')
    loop.add_argument('--trend-profile-file', action='append')
    loop.add_argument('--trend-archive-file', action='append')
    trend = commands.add_parser('trend-profile', help='从已封存门店观测建立日期/时段周转参考；不是过号率或ETA')
    trend.add_argument('--remote-db', required=True)
    trend.add_argument('--store-id', required=True)
    trend.add_argument('--as-of', required=True)
    trend.add_argument('--output', required=True)
    trend.add_argument('--window-seconds', type=int, default=1800)
    trend.add_argument('--max-gap-seconds', type=int, default=360)
    trend.add_argument('--minimum-pairs', type=int, default=2)
    trend.add_argument('--minimum-coverage-ppm', type=int, default=500000)
    trend.add_argument('--max-observations', type=int, default=10000)
    trend.add_argument('--availability-basis', choices=('local_first_receipt','sealed_response_reconstruction'),
                       default='local_first_receipt')
    trend = commands.add_parser('trend-score', help='将当前展示变化与相似日期参考比较；不调用模型或上游')
    trend.add_argument('--profile-file', required=True)
    trend.add_argument('--context-file', required=True)
    trend.add_argument('--output', required=True)
    trend.add_argument('--minimum-windows', type=int, default=8)
    trend.add_argument('--minimum-days', type=int, default=2)
    archive = commands.add_parser('trend-archive-add', help='汇入明确封存参考，保存新私有长期档案，不替换旧资料')
    archive.add_argument('--profile-file', action='append', required=True)
    archive.add_argument('--archive-file')
    archive.add_argument('--output', required=True)
    archive = commands.add_parser('trend-archive-select', help='从完整长期参考选择相似日期窗口，不查询上游或输出等待时间')
    archive.add_argument('--archive-file', required=True)
    archive.add_argument('--reference-for', required=True)
    archive.add_argument('--cadence-ms', type=int, default=300000)
    archive.add_argument('--maximum-windows', type=int, default=96)
    archive.add_argument('--maximum-per-day', type=int, default=8)
    archive.add_argument('--minimum-windows', type=int, default=8)
    archive.add_argument('--minimum-days', type=int, default=2)
    archive.add_argument('--output', required=True)
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
    if args.command in ('personal-snapshot','personal-context-surge','personal-auth-status'):
        try:
            if args.command=='personal-snapshot':
                result=write_personal_snapshot(context_file=args.context_file,destination=args.output_file,
                                               include_history=args.include_history)
            elif args.command=='personal-context-surge':
                result=receive_personal_context(context_file=args.context_file,revision=args.revision,
                                                app_id=args.app_id,seconds=args.seconds,on_ready=emit)
            else:
                context=read_personal_context(args.context_file)
                status=describe_authorization(context.authorization,now=_utc_clock())
                result={'ok':True,'purpose':'personal_ticket_read_only','context_revision':context.revision,
                    **status,'network_performed':False,'server_acceptance':'unverified','business_writes':0}
            emit(result);return 0
        except KeyboardInterrupt:
            emit({'ok':False,'error_code':'personal_interrupted','business_writes':0});return 130
        except PersonalError as error:
            emit({'ok':False,'error_code':error.error_code,'committed':error.committed,
                  'eta_available':False,'business_writes':0});return 1
        except (OSError,ValueError,TypeError,KeyError,OverflowError):
            emit({'ok':False,'error_code':'personal_response_invalid','business_writes':0});return 1
    if args.command == 'daily-quality-report':
        from .dailycontroller import read_daily_archive
        from .dailyquality import daily_quality_report
        from .remotetasks import RemoteTaskError, _json
        try:
            projection = _json(read_daily_archive(args.projection_file))
            emit(daily_quality_report(projection, store_id=args.store_id, day=args.date,
                as_of=args.as_of, interval_seconds=args.interval, max_gap_seconds=args.max_gap))
            return 0
        except RemoteTaskError as error:
            emit({'ok': False, 'error_code': str(error), 'network_performed_by_report': False})
            return 2
        except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            emit({'ok': False, 'error_code': 'daily_quality_input_error', 'network_performed_by_report': False})
            return 2
    if args.command == "remote-window-quality":
        from .remote import RemoteStore
        from .remotequality import terminal_checkpoint,window_quality_report
        from .remotetasks import RemoteTaskError
        try:
            if not 1 <= args.max_gap <= 7200:
                raise RemoteTaskError('remote_quality_invalid_gap_bound')
            terminal_checkpoint(args.task_file,as_of=args.as_of)
            with RemoteStore(args.db,read_only=True) as database:
                emit(window_quality_report(database,args.task_file,as_of=args.as_of,max_gap_seconds=args.max_gap))
            return 0
        except RemoteTaskError as error:
            emit({'ok':False,'source':'crm_remote_v1_1','error_code':str(error)});return 2
        except (OSError,ValueError,TypeError,KeyError,OverflowError,sqlite3.Error):
            emit({'ok':False,'source':'crm_remote_v1_1','error_code':'remote_quality_input_or_storage_error'});return 2
    if args.command == "remote-signal-report":
        from .remote import RemoteStore
        from .remotesignals import remote_signal_report
        from .signals import SignalError as RemoteSignalError
        try:
            store_id=canonical_store_id(args.store_id)
            with RemoteStore(args.db,read_only=True) as database:
                emit(remote_signal_report(database,store_id,as_of=args.as_of,window_seconds=args.window,
                    max_gap_seconds=args.max_gap,sample_limit=args.limit,availability_basis=args.availability_basis))
            return 0
        except RemoteSignalError as error:
            emit({'ok':False,'source':'crm_remote_v1_1','error_code':error.error_code});return 2
        except (OSError,ValueError,TypeError,KeyError,OverflowError,sqlite3.Error):
            emit({'ok':False,'source':'crm_remote_v1_1','error_code':'remote_signal_input_or_storage_error'});return 2
    if args.command == 'collection-hub-serve':
        from .monitorhub import read_config as read_hub_config, serve_hub
        try:
            config = read_hub_config(args.config_file)
            serve_hub(config, port=args.port, ready=lambda port: emit({'monitor_hub_ready': True,
                'url': f'http://127.0.0.1:{port}/monitor', 'workers': len(config['workers']),
                'stores': sum(len(w['stores']) for w in config['workers']),
                'upstream_network_performed_by_hub': False, 'collector_started_by_hub': False,
                'eta_available': False}))
            return 0
        except KeyboardInterrupt:
            return 0
        except Exception as error:
            emit({'ok': False, 'error_code': getattr(error, 'error_code', 'monitor_hub_unavailable'),
                'upstream_network_performed_by_hub': False, 'collector_started_by_hub': False,
                'eta_available': False})
            return 1
    if args.command=="monitor-plans-publish":
        from .planupdates import publish_update,PlanUpdateError
        try:
            result=publish_update(args.input,args.output,stores=[canonical_store_id(s) for s in args.store_id],
                base_interval=args.base_interval,clock=_utc_clock)
            emit(result);return 0 if result['ok'] else 1
        except PlanUpdateError as error:
            emit({'ok':False,'committed':error.committed,'error_code':str(error)});return 2
        except (OSError,ValueError,TypeError,KeyError,OverflowError):
            emit({'ok':False,'committed':False,'error_code':'plan_update_input_or_storage_error'});return 2
    if args.command == 'remote-daily-fleet-serve':
        from .dailyfleet import DailyFleetService, read_catalog
        from .businesshours import read_hours
        from .remoteservice import RemoteServiceError, serve_local
        from .remotetasks import RemoteTaskError
        try:
            service = DailyFleetService(root=args.root, catalog=read_catalog(args.catalog_file),
                business_hours=read_hours(args.business_hours_file), not_before=args.not_before,
                daily_pair_cap=args.daily_pair_cap, requests_per_second=args.requests_per_second,
                legacy_exports_root=args.legacy_exports_root, legacy_through_date=args.legacy_through_date,
                legacy_store_ids=args.legacy_store_id)
            serve_local(service, port=args.port, listen_host='127.0.0.1')
            return 1 if service.status()['service_state'] == 'failed' else 0
        except (RemoteTaskError, RemoteServiceError, OSError, ValueError, TypeError, KeyError, OverflowError):
            emit({'ok': False, 'error_code': 'fleet_unavailable_or_unsafe', 'eta_available': False})
            return 2
    if args.command in ('remote-daily-collect', 'remote-daily-serve', 'remote-daily-status', 'daily-archive-month'):
        from .dailycontroller import DailyController, daily_config, daily_controller_status
        from .businesshours import read_hours
        from .remotetasks import RemoteTaskError
        from .remoteservice import RemoteServiceError, serve_local
        try:
            if args.command == 'remote-daily-status':
                emit(daily_controller_status(args.root)); return 0
            store = canonical_store_id(args.store_id)
            if args.command == 'daily-archive-month':
                from .dailyarchive import read_month, legacy_reader_config
                legacy = legacy_reader_config(args.legacy_exports_root, args.legacy_through_date,
                    daily_root=args.root)
                emit(read_month(args.root, store, args.month, now=_utc_clock(), legacy=legacy)); return 0
            hours = read_hours(args.business_hours_file)
            if args.command == 'remote-daily-serve':
                from .dailyservice import DailyCollectorService
                service = DailyCollectorService(root=args.root, store_id=store, business_hours=hours,
                    not_before=args.not_before, daily_pair_cap=args.daily_pair_cap,
                    legacy_exports_root=args.legacy_exports_root, legacy_through_date=args.legacy_through_date)
                serve_local(service, port=args.port, listen_host='127.0.0.1')
                return 1 if service.status()['service_state'] == 'failed' else 0
            import signal
            import threading
            stop = threading.Event(); previous = {}
            try:
                for signum in (signal.SIGINT, signal.SIGTERM):
                    previous[signum] = signal.signal(signum, lambda *_: stop.set())
                config = daily_config(args.root, store, hours, not_before=args.not_before,
                    daily_pair_cap=args.daily_pair_cap)
                with DailyController(config=config, now=_utc_clock()) as controller:
                    emit(controller.status())
                    result = controller.run(wall_clock=_utc_clock, monotonic_clock=time.monotonic,
                        sleep=stop.wait, emit=emit, should_stop=stop.is_set)
                    emit(result); return 1 if result['state'] == 'halted' else 0
            finally:
                for signum, handler in previous.items(): signal.signal(signum, handler)
        except (RemoteTaskError, RemoteServiceError, OSError, ValueError, TypeError, KeyError, OverflowError):
            emit({'ok':False,'error_code':'daily_collection_unavailable_or_unsafe',
                'eta_available':False}); return 2
    if args.command in ("remote-campaign-collect", "remote-campaign-serve", "remote-campaign-status"):
        from .remotecampaign import RemoteCampaign, RemoteCampaignService, campaign_config, remote_campaign_status
        from .remoteservice import serve_local, RemoteServiceError
        from .remotetasks import RemoteTaskError
        from .planupdates import PlanUpdateError
        try:
            if args.command == "remote-campaign-status":
                emit(remote_campaign_status(args.root))
                return 0
            ids = [canonical_store_id(s) for s in args.store_id]
            from .businesshours import read_hours
            hours = read_hours(args.business_hours_file) if args.business_hours_file else None
            if args.command == "remote-campaign-serve":
                service = RemoteCampaignService(root=args.root, plan_file=args.plan_file, store_ids=ids,
                    base_interval=args.base_interval, duration_seconds=args.duration, window_seconds=args.window_duration,
                    max_pairs=args.max_pairs, plan_updates_file=args.plan_updates_file,
                    resume=args.resume_task, resume_if_present=args.resume_if_present,business_hours=hours,
                    transient_recovery_limit=args.transient_recovery_limit)
                serve_local(service, port=args.port, listen_host=args.listen_host)
                return 1 if service.status()['service_state'] == 'failed' else 0
            config = campaign_config(args.root, args.plan_file, ids, args.base_interval, args.duration,
                                     args.window_duration, args.max_pairs, now=_utc_clock(),
                                     plan_updates_file=args.plan_updates_file,business_hours=hours,
                                     transient_recovery_limit=args.transient_recovery_limit)
            with RemoteCampaign(config=config, now=_utc_clock(), resume=args.resume_task,
                                resume_if_present=args.resume_if_present) as campaign:
                result = campaign.collect(wall_clock=_utc_clock, monotonic_clock=time.monotonic,
                                          sleep=time.sleep, emit=emit)
                return 0 if result['ok'] else 1
        except KeyboardInterrupt:
            emit({'ok': False, 'source': 'crm_remote_v1_1', 'error_code': 'interrupted'})
            return 130
        except (RemoteTaskError, RemoteServiceError, PlanUpdateError) as error:
            emit({'ok': False, 'source': 'crm_remote_v1_1', 'error_code': str(error)})
            return 2
        except (OSError, ValueError, TypeError, KeyError, OverflowError, sqlite3.Error):
            emit({'ok': False, 'source': 'crm_remote_v1_1', 'error_code': 'remote_campaign_input_or_storage_error'})
            return 2
    if args.command in ("remote-window-collect","remote-window-serve","remote-window-status"):
        from .remotewindow import (RemoteWindowTask,RemoteWindowService,collect_remote_window,
            remote_window_status,window_config)
        from .remoteservice import serve_local,RemoteServiceError
        from .remote import RemoteClient,RemoteStore
        from .remotetasks import RemoteTaskError
        from .planupdates import PlanUpdateError
        try:
            if args.command=="remote-window-status":emit(remote_window_status(args.task_file));return 0
            ids=[canonical_store_id(s) for s in args.store_id]
            from .businesshours import read_hours
            hours=read_hours(args.business_hours_file) if args.business_hours_file else None
            if args.command=="remote-window-serve":
                service=RemoteWindowService(db=args.db,task_file=args.task_file,plan_file=args.plan_file,store_ids=ids,
                    base_interval=args.base_interval,duration_seconds=args.duration,max_pairs=args.max_pairs,
                    resume=args.resume_task,resume_if_present=args.resume_if_present,plan_updates_file=args.plan_updates_file,
                    business_hours=hours)
                serve_local(service,port=args.port,listen_host=args.listen_host)
                return 1 if service.status()['service_state']=='failed' else 0
            config=window_config(args.db,args.plan_file,ids,args.base_interval,args.duration,args.max_pairs,now=_utc_clock(),
                plan_updates_file=args.plan_updates_file,business_hours=hours)
            with RemoteWindowTask(args.task_file,config=config,resume=args.resume_task,now=_utc_clock(),
                                  resume_if_present=args.resume_if_present) as task:
                task.prepare_database()
                with RemoteStore(args.db,exclusive_create=task.require_new_database) as database:
                    task.bind(database,now=_utc_clock())
                    client=None if task.value['state'] in ('completed','failed') else RemoteClient()
                    summary=collect_remote_window(task,client,wall_clock=_utc_clock,
                        monotonic_clock=time.monotonic,sleep=time.sleep,emit=emit)
                    return 0 if summary['ok'] else 1
        except KeyboardInterrupt:
            emit({'ok':False,'source':'crm_remote_v1_1','error_code':'interrupted'});return 130
        except (RemoteTaskError,RemoteServiceError,PlanUpdateError) as error:
            emit({'ok':False,'source':'crm_remote_v1_1','error_code':str(error)});return 2
        except (OSError,ValueError,TypeError,KeyError,OverflowError,sqlite3.Error):
            emit({'ok':False,'source':'crm_remote_v1_1','error_code':'remote_window_input_or_storage_error'});return 2
    if args.command == "remote-serve":
        from .remoteservice import RemoteQueueService, RemoteServiceError, serve_local
        try:
            ids = [canonical_store_id(s) for s in args.store_id]
            service = RemoteQueueService(db=args.db,task_file=args.task_file,store_ids=ids,
                interval=args.interval,samples=args.samples,resume=args.resume_task,
                stale_after_seconds=args.stale_after)
            serve_local(service,port=args.port,listen_host=args.listen_host)
            return 1 if service.status()['service_state']=='failed' else 0
        except KeyboardInterrupt:return 130
        except RemoteServiceError as error:
            emit({"ok":False,"source":"crm_remote_v1_1","error_code":str(error)});return 2
        except (OSError,ValueError,TypeError,KeyError,OverflowError,sqlite3.Error):
            emit({"ok":False,"source":"crm_remote_v1_1","error_code":"remote_service_input_or_storage_error"});return 2
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
        if args.command in ('trend-profile', 'trend-score'):
            try:
                from .trendprofiles import build_profile, read_profile, write_profile, enrich_context
                from .remote import RemoteStore as TrendRemoteStore
                from .credentials import _read_private_file as read_trend_bytes
                from .outcomes import _json as parse_trend_json
                from .packets import _write_packet as write_trend_packet
                from .intake import _canonical as canonical_trend
                if args.command == 'trend-profile':
                    with TrendRemoteStore(args.remote_db, read_only=True) as remote:
                        profile = build_profile(remote, args.store_id, as_of=args.as_of,
                            window_seconds=args.window_seconds, max_gap_seconds=args.max_gap_seconds,
                            minimum_pairs=args.minimum_pairs, minimum_coverage_ppm=args.minimum_coverage_ppm,
                            max_observations=args.max_observations, availability_basis=args.availability_basis)
                    result = write_profile(profile, args.output)
                else:
                    context, score = enrich_context(read_profile(args.profile_file),
                        parse_trend_json(read_trend_bytes(args.context_file)),
                        minimum_windows=args.minimum_windows, minimum_days=args.minimum_days)
                    saved = write_trend_packet(canonical_trend({'public_context':context, 'trend_score':score}).encode(), args.output)
                    result = {'artifact_written':True,'committed':True,
                        'durability_confirmed':saved['durability_confirmed'],
                        'reference_windows':score['reference_windows'],
                        'trend_available':score['unavailable_reason'] is None,
                        'network_performed':False,'eta_available':False}
                emit({'ok':True, **result}); return 0
            except Exception as error:
                emit({'ok':False,'error_code':getattr(error,'error_code','trend_profile_operation_failed'),
                    'committed':getattr(error,'committed',False),'eta_available':False,'network_performed':False}); return 1
        if args.command in ('trend-archive-add', 'trend-archive-select'):
            try:
                from . import trendarchive as archive_ops
                from . import trendprofiles as archive_profiles
                if args.command == 'trend-archive-add' and not 1 <= len(args.profile_file) <= 16:
                    raise ValueError
                previous = archive_ops.read_archive(args.archive_file) if args.archive_file else None
                if args.command == 'trend-archive-add':
                    profiles = [archive_profiles.read_profile(path) for path in args.profile_file]
                    value = archive_ops.append_profiles(profiles, archive=previous)
                    result = archive_ops.write_archive(value, args.output)
                else:
                    value = archive_ops.select_profile(previous, reference_for=args.reference_for,
                        cadence=[args.cadence_ms,1], maximum_windows=args.maximum_windows,
                        maximum_per_day=args.maximum_per_day, minimum_windows=args.minimum_windows,
                        minimum_days=args.minimum_days)
                    result = archive_profiles.write_profile(value, args.output)
                emit({'ok':True, **result});return 0
            except Exception as error:
                emit({'ok':False,'error_code':getattr(error,'error_code','trend_archive_operation_failed'),
                    'committed':getattr(error,'committed',False),'eta_available':False,
                    'network_performed':False});return 1
        if args.command == 'ticket-track-run':
            from .trackerloop import TrackingCoordinator, RemoteProjectionReader, TrackerLoopError, run_tracking
            coordinator = None
            try:
                if (not 1 <= args.duration <= 21600 or not 1 <= args.max_cycles <= 4320
                        or args.prepare_plans_only and not args.plan_output
                        or not args.prepare_plans_only and not args.collector_url):
                    raise TrackerLoopError('tracker_loop_invalid_configuration')
                def no_read(_):
                    raise TrackerLoopError('tracker_loop_projection_unavailable')
                reader = RemoteProjectionReader(args.collector_url) if args.collector_url else no_read
                coordinator = TrackingCoordinator(args.state_dir, stores=args.store_id, reader=reader,
                    base_interval=args.base_interval, read_interval=args.read_interval,
                    source_db=args.source_db, reviews_db=args.reviews_db,
                    sealed_remote_db=args.sealed_remote_db, landmark_seconds=args.landmark_seconds,
                    realtime_window_seconds=args.realtime_window_seconds,
                    realtime_neighbors=args.realtime_neighbors,realtime_minimum_episodes=args.realtime_minimum_episodes,
                    model_version=args.model_version, ai_blend_ppm=args.ai_blend_ppm,
                    plan_output=args.plan_output, plan_series_id=args.plan_series_id,
                    allow_paid_request=args.allow_paid_request, budget_file=args.budget_file, key_file=args.key_file,
                    trend_profile_files=args.trend_profile_file, trend_archive_files=args.trend_archive_file)
                if args.prepare_plans_only:
                    result = coordinator.publish_demands()
                    emit({'ok': True, 'prepared_plan_revision': result['revision'], **coordinator.summary()})
                else:
                    result = run_tracking(coordinator, duration_seconds=args.duration,
                                          max_cycles=args.max_cycles, emit=emit)
                    emit(result)
                    return 0 if result['ok'] else 1
                return 0
            except KeyboardInterrupt:
                emit({'ok': False, 'error_code': 'tracker_loop_interrupted'}); return 130
            except Exception as error:
                emit({'ok': False, 'error_code': getattr(error, 'error_code', 'tracker_loop_input_or_storage_error'),
                    'official_collection_requests_by_tracker': 0, 'business_writes': 0,
                    'notification_sent': False, 'eta_available': False}); return 1
            finally:
                if coordinator is not None:
                    coordinator.close()
        if args.command.startswith('ticket-track-'):
            try:
                state = Path(args.state_dir).resolve()
                inputs = [Path(p).resolve() for p in (
                    getattr(args, 'ticket_file', None), getattr(args, 'view_file', None),
                    getattr(args, 'context_file', None), getattr(args, 'source_db', None),
                    getattr(args, 'reviews_db', None), getattr(args, 'fusion_plan_file', None),
                    getattr(args, 'advice_file', None)) if p is not None]
                if len(set(inputs)) != len(inputs) or any(p.is_relative_to(state) for p in inputs):
                    raise TrackingError('tracking_invalid_input')
                if args.command == 'ticket-track-init':
                    result = create_session(read_tracking_document(args.ticket_file), directory=args.state_dir)
                elif args.command == 'ticket-track-observe':
                    result = observe_session(directory=args.state_dir, view=read_tracking_document(args.view_file),
                        context=read_tracking_document(args.context_file), as_of=args.as_of)
                elif args.command == 'ticket-track-end':
                    result = end_session(directory=args.state_dir, status=args.status, declared_at=args.declared_at)
                elif args.command == 'ticket-track-status':
                    result = {'ok': True, **session_status(directory=args.state_dir)}
                else:
                    if (bool(args.source_db) != bool(args.reviews_db)
                            or args.source_db and args.fusion_plan_file
                            or args.source_db and (Path(args.source_db).resolve().parent ==
                                Path(args.reviews_db).resolve().parent or
                                state.is_relative_to(Path(args.source_db).resolve().parent) or
                                state.is_relative_to(Path(args.reviews_db).resolve().parent))):
                        raise TrackingError('tracking_invalid_input')
                    preparation = prepare_prediction(directory=args.state_dir, version=args.version)
                    advice, advice_error = None, None
                    if args.advice_file:
                        try:
                            advice = read_advice(args.advice_file)
                        except FusionError:
                            advice_error = 'fusion_invalid_advice'
                    options = {'advice': advice, 'advice_error': advice_error,
                        'fusion_plan': read_fusion_plan(args.fusion_plan_file) if args.fusion_plan_file else None,
                        'base_interval': args.base_interval, 'model_version': args.model_version,
                        'ai_blend_ppm': args.ai_blend_ppm}
                    if args.source_db:
                        with OutcomeIntakeStore(args.source_db, read_only=True) as source, \
                                OutcomeReviewStore(args.reviews_db, read_only=True) as reviews:
                            prediction = calculate_prediction(preparation, source=source, reviews=reviews, **options)
                    else:
                        prediction = calculate_prediction(preparation, **options)
                    result = publish_prediction(directory=args.state_dir, preparation=preparation, result=prediction)
                durable = result.get('durability_confirmed', True)
                emit({**result, **({} if durable else {'error_code': 'tracking_durability_unconfirmed'})})
                return 0 if durable else 1
            except (TrackingError, FusionError, HistoryFusionError, IntakeError, ReviewError,
                    ValueError, OSError, KeyError, TypeError) as error:
                emit({'ok': False, 'error_code': getattr(error, 'error_code', 'tracking_invalid_input'),
                    'committed': getattr(error, 'committed', False), 'durability_confirmed': False,
                    'network_performed': False, 'provider_called': False, 'business_writes': 0,
                    'notification_sent': False, 'verified_training_labels': 0, 'eta_available': False})
                return 1
        if args.command == 'experience-bundle-receive':
            from .experiencebundle import ExperienceBundleError, receive_bundle
            try:
                emit({'ok':True, **receive_bundle(args.input,database=args.database,synthetic=args.synthetic)})
                return 0
            except ExperienceBundleError as error:
                emit({'ok':False,'error_code':error.error_code,'new_revisions_before_failure':error.saved,
                    'failed_append_commit_status':error.commit_status,'network_performed':False,
                    'verified_training_labels':0,'eta_available':False})
                return 1
        if args.command == 'ticket-outcome-draft':
            from .experience import ExperienceError, write_experience
            try:
                previous = read_tracking_document(args.previous_file) if args.previous_file else None
                result = write_experience(directory=args.state_dir, destination=args.output,
                    event_type=args.event_type, lower=args.event_lower, upper=args.event_upper,
                    issued_lower=args.issued_lower, issued_upper=args.issued_upper, previous=previous)
                durable = result['durability_confirmed']
                emit({'ok': durable, **result, **({} if durable else {'error_code':'experience_durability_unconfirmed'})})
                return 0 if durable else 1
            except (ExperienceError, TrackingError, ValueError, OSError) as error:
                emit({'ok': False, 'error_code': getattr(error, 'error_code', 'experience_invalid_input'),
                    'committed': getattr(error, 'committed', False), 'durability_confirmed': False,
                    'network_performed': False, 'provider_called': False, 'business_writes': 0,
                    'notification_sent': False, 'verified_training_labels': 0, 'eta_available': False})
                return 1

        if args.command == 'realtime-backtest':
            from .realtimebacktest import RealtimeBacktestError, read_plan as read_replay_plan, write_realtime_backtest
            from .remote import RemoteStore
            try:
                paths = [Path(p).resolve() for p in (args.source_db,args.reviews_db,args.remote_db,args.input,args.output)]
                if (len(set(paths))!=5 or len({p.parent for p in paths[:3]})!=3
                        or paths[-1].parent in {p.parent for p in paths[:3]}):
                    raise RealtimeBacktestError('realtime_backtest_invalid_input')
                plan = read_replay_plan(args.input)
                with OutcomeIntakeStore(args.source_db,read_only=True) as source, \
                        OutcomeReviewStore(args.reviews_db,read_only=True) as reviews, \
                        RemoteStore(args.remote_db,read_only=True) as remote:
                    result = write_realtime_backtest(source=source,reviews=reviews,remote=remote,
                        plan=plan,destination=args.output,max_revisions=args.max_revisions,max_observations=args.max_observations)
                durable = result['durability_confirmed']
                emit({'ok':durable,**result,**({} if durable else {'error_code':'realtime_backtest_durability_unconfirmed'})})
                return 0 if durable else 1
            except Exception as error:
                emit({'ok':False,'error_code':getattr(error,'error_code','realtime_backtest_failed'),
                    'committed':getattr(error,'committed',False),'durability_confirmed':False,
                    'provider_called':False,'network_performed':False,'verified_training_labels':0,'eta_available':False})
                return 1
        if args.command == 'realtime-fusion-research':
            from .realtimefusion import RealtimeFusionError, write_realtime_fusion
            from .remote import RemoteStore
            try:
                paths = [Path(p).resolve() for p in (args.source_db,args.reviews_db,args.remote_db,
                    args.input,args.feature_plan,args.context_file,args.output)]
                if (len(set(paths))!=7 or len({p.parent for p in paths[:3]})!=3
                        or paths[-1].parent in {p.parent for p in paths[:3]}):
                    raise RealtimeFusionError('realtime_model_invalid_input')
                plan = read_baseline_plan(args.input)
                features_plan = read_feature_plan(args.feature_plan)
                context = read_fusion_context(args.context_file)
                with OutcomeIntakeStore(args.source_db,read_only=True) as source, \
                        OutcomeReviewStore(args.reviews_db,read_only=True) as reviews, \
                        RemoteStore(args.remote_db,read_only=True) as remote:
                    result = write_realtime_fusion(source=source,reviews=reviews,remote=remote,
                        plan=plan,feature_plan=features_plan,context=context,destination=args.output,
                        neighbors=args.neighbors,minimum_episodes=args.minimum_episodes,
                        maximum_elapsed_difference_seconds=args.maximum_elapsed_difference_seconds,
                        maximum_standardized_distance=args.maximum_standardized_distance,
                        realtime_weight_ppm=args.realtime_weight_ppm,ai_blend_ppm=args.ai_blend_ppm,
                        max_revisions=args.max_revisions,max_observations=args.max_observations)
                durable = result['durability_confirmed']
                emit({'ok':durable,**result,**({} if durable else {'error_code':'realtime_model_durability_unconfirmed'})})
                return 0 if durable else 1
            except Exception as error:
                code = getattr(error,'error_code','realtime_model_failed')
                emit({'ok':False,'error_code':code,'committed':getattr(error,'committed',False),
                    'durability_confirmed':False,'network_performed':False,'provider_called':False,
                    'eta_available':False,'verified_training_labels':0});return 1
        if args.command == "history-fusion-research":
            try:
                from .baseline import read_plan as read_history_plan
                paths = [Path(p).resolve() for p in (args.source_db, args.reviews_db, args.input,
                                                   args.context_file, args.output)]
                if (len(set(paths)) != 5 or paths[0].parent == paths[1].parent
                        or paths[4].parent in {p.parent for p in paths[:2]}):
                    raise HistoryFusionError("history_fusion_invalid_input")
                plan = read_history_plan(args.input)
                context = read_fusion_context(args.context_file)
                with OutcomeIntakeStore(args.source_db, read_only=True) as source, \
                        OutcomeReviewStore(args.reviews_db, read_only=True) as reviews:
                    result = write_history_fusion(source=source, reviews=reviews, plan=plan,
                        context=context, destination=args.output, model_version=args.model_version,
                        ai_blend_ppm=args.ai_blend_ppm, max_revisions=args.max_revisions)
                durable = result['durability_confirmed']
                emit({"ok": durable, **result, **({} if durable else {"error_code": "history_fusion_durability_unconfirmed"})})
                return 0 if durable else 1
            except (HistoryFusionError, BaselineError, IntakeError, ReviewError) as error:
                emit({"ok": False, "error_code": error.error_code, "committed": getattr(error, 'committed', False),
                    "durability_confirmed": False, "network_performed": False, "provider_called": False,
                    "verified_training_labels": 0, "eta_available": False})
                return 1
        if args.command == "deepseek-fusion":
            try:
                paths = [Path(p).resolve() for p in
                         (args.input, args.output, args.budget_file, args.api_key_file) if p is not None]
                if len(set(paths)) != len(paths):
                    raise DeepSeekError("deepseek_configuration_conflict")
                plan = read_fusion_plan(args.input)
                result = write_deepseek_fusion(plan, destination=args.output,
                    budget_file=args.budget_file, key_file=args.api_key_file,
                    allow_paid_request=args.allow_paid_request)
                durable = result['durability_confirmed']
                emit({"ok": durable, **result, **({} if durable else {"error_code": "deepseek_durability_unconfirmed"})})
                return 0 if durable else 1
            except (DeepSeekError, FusionError) as error:
                network = getattr(error, 'network_performed', False)
                emit({"ok": False, "error_code": error.error_code,
                    "committed": getattr(error, 'committed', False), "durability_confirmed": False,
                    "network_performed": network, "provider_called": network,
                    "automatic_retry": False, "eta_available": False, "verified_training_labels": 0})
                return 1
        if args.command == "fusion-research":
            try:
                paths = [Path(p).resolve() for p in (args.input, args.output)]
                if args.advice is not None:
                    paths.append(Path(args.advice).resolve())
                if len(set(paths)) != len(paths):
                    raise FusionError("fusion_invalid_input")
                plan = read_fusion_plan(args.input)
                advice, advice_error = None, None
                if args.advice is not None:
                    try:
                        advice = read_advice(args.advice)
                    except FusionError as error:
                        advice_error = error.error_code
                result = write_fusion(plan, destination=args.output, advice=advice, advice_error=advice_error)
                durable = result['durability_confirmed']
                emit({"ok": durable, **result, **({} if durable else {"error_code": "fusion_durability_unconfirmed"})})
                return 0 if durable else 1
            except FusionError as error:
                emit({"ok": False, "error_code": error.error_code, "committed": error.committed,
                      "durability_confirmed": False, "network_performed": False, "provider_called": False,
                      "eta_available": False, "verified_training_labels": 0})
                return 1
        if args.command == "outcome-feature-export":
            from .remote import RemoteStore
            try:
                plan = read_feature_plan(args.input)
                paths = [Path(p).resolve() for p in (args.source_db, args.reviews_db, args.remote_db,
                                                     args.input, args.output)]
                if len(set(paths)) != 5 or len({p.parent for p in paths[:3]}) != 3 or paths[4].parent in {p.parent for p in paths[:3]}:
                    raise FeatureError("features_invalid_input")
                if (type(args.max_revisions) is not int or not 1 <= args.max_revisions <= 10000
                        or type(args.max_observations) is not int or not 1 <= args.max_observations <= 10000):
                    raise FeatureError("features_invalid_plan")
                with OutcomeIntakeStore(args.source_db, read_only=True) as source, \
                        OutcomeReviewStore(args.reviews_db, read_only=True) as reviews, \
                        RemoteStore(args.remote_db, read_only=True) as remote:
                    result = write_feature_dataset(source=source, reviews=reviews, remote=remote, plan=plan,
                        destination=args.output, max_revisions=args.max_revisions, max_observations=args.max_observations)
                durable = result['durability_confirmed']
                emit({"ok": durable, **result, **({} if durable else {"error_code": "features_durability_unconfirmed"})})
                return 0 if durable else 1
            except (FeatureError, IntakeError, ReviewError) as error:
                emit({"ok": False, "error_code": error.error_code, "committed": getattr(error, "committed", False),
                      "durability_confirmed": False, "network_performed": False, "eta_available": False,
                      "verified_training_labels": 0, "model_fitted": False})
                return 1
            except (OSError, ValueError):
                emit({"ok": False, "error_code": "features_remote_invalid", "committed": False,
                      "network_performed": False, "eta_available": False, "verified_training_labels": 0})
                return 1
        if args.command == "baseline-backtest":
            try:
                plan = read_backtest_plan(args.input)
                if not 1 <= args.max_revisions <= 10000:
                    raise BacktestError("backtest_invalid_plan")
                with OutcomeIntakeStore(args.source_db, read_only=True) as source, \
                        OutcomeReviewStore(args.reviews_db, read_only=True) as reviews:
                    result = write_backtest(source=source,reviews=reviews,plan=plan,
                        destination=args.output,max_revisions=args.max_revisions)
                durable = result['durability_confirmed']
                emit({"ok":durable,**result,**({} if durable else {"error_code":"backtest_durability_unconfirmed"})})
                return 0 if durable else 1
            except (BacktestError, IntakeError, ReviewError) as error:
                emit({"ok":False,"error_code":error.error_code,"committed":getattr(error,"committed",False),
                    "durability_confirmed":False,"network_performed":False,"eta_available":False,
                    "verified_training_labels":0,"model_performance_verified":False})
                return 1
        if args.command == "baseline-research":
            try:
                plan = read_baseline_plan(args.input)
                if not 1 <= args.max_revisions <= 10000:
                    raise BaselineError("baseline_invalid_plan")
                with OutcomeIntakeStore(args.source_db, read_only=True) as source, \
                        OutcomeReviewStore(args.reviews_db, read_only=True) as reviews:
                    result = write_baseline(source=source,reviews=reviews,plan=plan,
                        destination=args.output,max_revisions=args.max_revisions)
                durable = result['durability_confirmed']
                emit({"ok":durable,**result,**({} if durable else {"error_code":"baseline_durability_unconfirmed"})})
                return 0 if durable else 1
            except (BaselineError, IntakeError, ReviewError) as error:
                emit({"ok":False,"error_code":error.error_code,
                    "committed":getattr(error,"committed",False),"durability_confirmed":False,
                    "network_performed":False,"authenticity_verified":False,
                    "verified_training_labels":0,"eta_available":False})
                return 1
        if args.command in ("outcome-review-draft", "outcome-review-receive", "outcome-reviewed-cohort"):
            try:
                if args.command == "outcome-review-draft":
                    episode = read_episode(args.synthetic_fixture or args.input,
                                           synthetic=bool(args.synthetic_fixture))
                    with OutcomeIntakeStore(args.source_db, read_only=True) as source:
                        result = draft_review(source, episode, args.output)
                elif args.command == "outcome-review-receive":
                    review = read_review(args.input)
                    with OutcomeIntakeStore(args.source_db, read_only=True) as source, OutcomeReviewStore(args.db) as store:
                        result = store.append(review, source=source)
                else:
                    if not 1 <= args.max_revisions <= 10000:
                        raise ReviewError("review_invalid_scope_or_bounds")
                    with OutcomeIntakeStore(args.source_db, read_only=True) as source, OutcomeReviewStore(args.db, read_only=True) as store:
                        result = store.cohort(source=source, as_of=args.as_of, data_origin=args.data_origin,
                            api_profile=args.api_profile, max_revisions=args.max_revisions)
                durable = result.get("durability_confirmed", True)
                emit({"ok": durable, **result,
                      **({} if durable else {"error_code": "review_durability_unconfirmed"})})
                return 0 if durable else 1
            except (ReviewError, IntakeError, OutcomeError) as error:
                emit({"ok": False, "error_code": error.error_code,
                    "commit_status": getattr(error, "commit_status", "not_started"),
                    "network_performed": False, "authenticity_verified": False, "verified_training_labels": 0})
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
