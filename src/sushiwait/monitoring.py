"""Offline per-plan polling targets; no prediction, scheduling or business writes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math


class MonitoringError(ValueError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


def _time(value: str) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise MonitoringError("monitor_invalid_time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise MonitoringError("monitor_invalid_time") from None


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def polling_policy(*, desired_arrival_at: str, as_of: str, base_interval: int,
                   call_offset_minutes: int = 0, plan_status: str = "waiting",
                   earliest_call_at: str | None = None,
                   accelerated_display_turnover: bool = False) -> dict:
    """User preference is target_call = arrival + offset, not a prediction error.

    earliest_call_at is an explicit, unverified external estimate. It may only
    move polling earlier; this function neither computes nor certifies it.
    Time bands specify requested targets, never upstream freshness or coverage.
    """
    if (type(base_interval) is not int or not 60 <= base_interval <= 3600
            or type(call_offset_minutes) is not int or not -1440 <= call_offset_minutes <= 1440
            or type(accelerated_display_turnover) is not bool
            or not isinstance(plan_status, str)
            or plan_status not in {"waiting", "called", "no_show", "cancelled", "ended"}):
        raise MonitoringError("monitor_invalid_input")
    arrival, current = _time(desired_arrival_at), _time(as_of)
    try:
        target = arrival + timedelta(minutes=call_offset_minutes)
    except OverflowError:
        raise MonitoringError("monitor_invalid_time") from None
    earliest = _time(earliest_call_at) if earliest_call_at is not None else None
    horizon = min(value for value in (arrival, target, earliest) if value is not None)
    remaining = (horizon - current).total_seconds()
    if not math.isfinite(remaining):
        raise MonitoringError("monitor_invalid_time")
    interval = None
    if plan_status != "waiting":
        reason = "plan_terminal"
    elif accelerated_display_turnover:
        interval, reason = 30, "display_acceleration_input"
    elif remaining <= 15 * 60:
        interval, reason = 30, "within_15_minutes_or_overdue"
    elif remaining <= 30 * 60:
        interval, reason = 60, "within_30_minutes"
    else:
        interval, reason = base_interval, "configured_background"
    try:
        next_target = _stamp(current + timedelta(seconds=interval)) if interval else None
    except OverflowError:
        raise MonitoringError("monitor_invalid_time") from None
    return {"policy_schema_version": 1, "as_of": _stamp(current),
            "desired_arrival_at": _stamp(arrival), "target_call_at": _stamp(target),
            "call_offset_minutes": call_offset_minutes, "plan_status": plan_status,
            "monitor_horizon_at": _stamp(horizon), "horizon_remaining_seconds": remaining,
            "earliest_call_input_present": earliest is not None,
            "earliest_call_input_verified": False if earliest is not None else None,
            "accelerated_display_turnover_input": accelerated_display_turnover,
            "requested_interval_seconds": interval, "next_poll_target_at": next_target,
            "reason": reason, "reestimate_requested": interval == 30,
            "polling_performed": False, "scheduler_applied": False,
            "notification_sent": False, "business_operation_performed": False,
            "source_freshness": "unknown", "upstream_frequency_verified": False,
            "eta_available": False, "true_no_show_rate": None,
            "missed_call_prevention_guaranteed": False, "network_performed": False}
