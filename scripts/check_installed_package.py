"""Check a wheel install outside the checkout, using only a synthetic fixture."""

import argparse
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

import sushiwait
from sushiwait.cli import main


def check() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--version-file", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--outcome-fixture", type=Path, required=True)
    args = parser.parse_args()
    expected = args.version_file.read_text(encoding="utf-8").strip()
    if sushiwait.__version__ != expected:
        raise SystemExit("installed_version_mismatch")
    if Path(sushiwait.__file__).resolve().is_relative_to(args.source_root.resolve()):
        raise SystemExit("package_imported_from_checkout")
    help_result = subprocess.run([sys.executable, "-I", "-m", "sushiwait", "--help"],
        capture_output=True, text=True, timeout=10, check=True)
    if any(command not in help_result.stdout for command in
            ("capture-import", "context-bridge", "context-surge", "surge-guard", "context-promote", "monitor-plan", "task-status", "outcome-import", "outcome-report", "date-features", "signal-report")):
        raise SystemExit("installed_cli_missing_commands")
    bridge_help = subprocess.run([sys.executable, "-I", "-m", "sushiwait",
                                  "context-bridge", "--help"],
        capture_output=True, text=True, timeout=10, check=True)
    if "--diagnostics" not in bridge_help.stdout:
        raise SystemExit("installed_bridge_diagnostics_missing")
    surge_help = subprocess.run([sys.executable, "-I", "-m", "sushiwait",
                                 "context-surge", "--help"],
        capture_output=True, text=True, timeout=10, check=True)
    if any(option not in surge_help.stdout for option in ("--credentials-file", "--revision", "--seconds")):
        raise SystemExit("installed_surge_intake_missing")
    guard_help = subprocess.run([sys.executable, "-I", "-m", "sushiwait",
                                 "surge-guard", "--help"],
        capture_output=True, text=True, timeout=10, check=True)
    if "--seconds" not in guard_help.stdout:
        raise SystemExit("installed_surge_guard_missing")
    promotion_help = subprocess.run([sys.executable, "-I", "-m", "sushiwait",
                                     "context-promote", "--help"],
        capture_output=True, text=True, timeout=10, check=True)
    if any(option not in promotion_help.stdout for option in
           ("--credentials-file", "--staged-file", "--expected-revision")):
        raise SystemExit("installed_promotion_missing")
    collect_help = subprocess.run([sys.executable, "-I", "-m", "sushiwait", "collect", "--help"],
        capture_output=True, text=True, timeout=10, check=True)
    if any(option not in collect_help.stdout for option in ("--task-file", "--resume-task")):
        raise SystemExit("installed_collection_task_options_missing")
    with tempfile.TemporaryDirectory() as directory:
        database = str(Path(directory).resolve() / "synthetic.sqlite3")
        # Fail immediately if the replay/report smoke path tries any socket I/O.
        with patch("socket.socket", side_effect=AssertionError("unexpected_network")), \
                patch("socket.create_connection", side_effect=AssertionError("unexpected_network")):
            guard_output = io.StringIO()
            with patch("sushiwait.surgeguard._command", side_effect=AssertionError("unexpected_native_command")) as native, \
                    contextlib.redirect_stdout(guard_output):
                if main(["surge-guard", "--seconds", "0"]) != 1:
                    raise SystemExit("installed_guard_invalid_window_accepted")
            if (native.call_count != 0 or
                    json.loads(guard_output.getvalue())["error_code"] != "surge_guard_invalid_window"):
                raise SystemExit("installed_guard_validation_semantics_mismatch")
            promotion_output = io.StringIO()
            with patch("sushiwait.promotion.read_state", side_effect=AssertionError("unexpected_native_command")) as native, \
                    contextlib.redirect_stdout(promotion_output):
                if main(["context-promote", "--credentials-file", "unused-current.json",
                         "--staged-file", "unused-staged.json", "--expected-revision", "0"]) != 1:
                    raise SystemExit("installed_promotion_invalid_revision_accepted")
            if (native.call_count != 0 or
                    json.loads(promotion_output.getvalue())["error_code"] != "promotion_invalid_input"):
                raise SystemExit("installed_promotion_validation_semantics_mismatch")
            monitoring_output = io.StringIO()
            with patch("sushiwait.credentials.read_credentials_file", side_effect=AssertionError("unexpected_credentials")), \
                    patch("sushiwait.surgeguard._command", side_effect=AssertionError("unexpected_native_command")), \
                    contextlib.redirect_stdout(monitoring_output):
                if main(["monitor-plan", "--desired-arrival-at", "2026-10-06T19:00:00+08:00",
                         "--as-of", "2026-10-06T18:40:00+08:00", "--base-interval", "300",
                         "--call-offset-minutes", "-10"]) != 0:
                    raise SystemExit("installed_monitoring_failed")
            monitoring = json.loads(monitoring_output.getvalue())
            if (monitoring["target_call_at"] != "2026-10-06T10:50:00.000000Z"
                    or monitoring["requested_interval_seconds"] != 30 or monitoring["eta_available"]
                    or monitoring["scheduler_applied"] or monitoring["notification_sent"]):
                raise SystemExit("installed_monitoring_semantics_mismatch")
            with contextlib.redirect_stdout(io.StringIO()):
                if main(["replay", "--fixture", str(args.fixture), "--db", database]) != 0:
                    raise SystemExit("installed_replay_failed")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                if main(["report", "--db", database]) != 0:
                    raise SystemExit("installed_report_failed")
            group = json.loads(output.getvalue())["groups"][0]
            from datetime import datetime, timezone
            from sushiwait.tasks import CollectionTask
            from sushiwait.storage import SnapshotStore
            task_path = str(Path(directory).resolve() / "collection.json")
            config = {"db": database, "api_profile": group["api_profile"], "store_ids": [group["store_id"]],
                      "interval": 60, "samples": 1, "wait_for_credentials": 0}
            with CollectionTask(task_path, config=config, resume=False, now=datetime.now(timezone.utc)) as task:
                task.prepare_database()
                with SnapshotStore(database) as db:
                    task.bind(db, now=datetime.now(timezone.utc))
            task_output = io.StringIO()
            with contextlib.redirect_stdout(task_output):
                if main(["task-status", "--task-file", task_path]) != 0:
                    raise SystemExit("installed_task_status_failed")
            signal_output = io.StringIO()
            with contextlib.redirect_stdout(signal_output):
                if main(["signal-report", "--db", database, "--store-id", group["store_id"],
                         "--api-profile", group["api_profile"], "--data-origin", "synthetic",
                         "--as-of", group["last_received_at"]]) != 0:
                    raise SystemExit("installed_signals_failed")
            outcome_database = str(Path(directory).resolve() / "outcomes.sqlite3")
            with contextlib.redirect_stdout(io.StringIO()):
                if main(["outcome-check", "--synthetic-fixture", str(args.outcome_fixture)]) != 0:
                    raise SystemExit("installed_outcome_check_failed")
                if main(["outcome-import", "--synthetic-fixture", str(args.outcome_fixture),
                         "--db", outcome_database]) != 0:
                    raise SystemExit("installed_outcome_import_failed")
            outcome_output = io.StringIO()
            with contextlib.redirect_stdout(outcome_output):
                if main(["outcome-report", "--db", outcome_database]) != 0:
                    raise SystemExit("installed_outcome_report_failed")
            calendar_output = io.StringIO()
            with contextlib.redirect_stdout(calendar_output):
                if main(["date-features", "--at", "2026-10-10T12:00:00+08:00",
                         "--as-of", "2026-10-04T12:00:00Z"]) != 0:
                    raise SystemExit("installed_calendar_failed")
        report = json.loads(output.getvalue())
        if not report["groups"] or any(g["data_origin"] != "synthetic" for g in report["groups"]):
            raise SystemExit("installed_report_origin_mismatch")
        outcome_report = json.loads(outcome_output.getvalue())
        if (outcome_report["origins"] != {"synthetic": 1} or outcome_report["verified_training_labels"] != 0
                or outcome_report["eta_available"]):
            raise SystemExit("installed_outcome_report_semantics_mismatch")
        calendar_report = json.loads(calendar_output.getvalue())
        if (calendar_report["date_type"] != "makeup_workday" or calendar_report["eta_available"]
                or calendar_report["store_open_status"] != "unknown"):
            raise SystemExit("installed_calendar_semantics_mismatch")
        signal_summary = json.loads(signal_output.getvalue())
        if (signal_summary["scan"].get("valid_successful_rows") != 1
                or signal_summary["eta_available"] or signal_summary["true_no_show_rate"] is not None
                or signal_summary["network_performed"]):
            raise SystemExit("installed_signals_semantics_mismatch")
        task_summary = json.loads(task_output.getvalue())
        if (task_summary["task_schema_version"] != 1 or task_summary["completed_slots"] != 0
                or task_summary["network_performed"] or task_summary["eta_available"]):
            raise SystemExit("installed_task_status_semantics_mismatch")
    print(json.dumps({"installed_version": expected, "import_outside_checkout": True,
        "cli_help_ok": True, "bridge_diagnostic_option_ok": True, "surge_intake_help_ok": True,
        "surge_guard_help_ok": True, "guard_invalid_window_ok": True,
        "guard_validation_socket_calls": 0, "guard_validation_native_cli_calls": 0,
        "promotion_help_ok": True, "promotion_invalid_revision_ok": True,
        "promotion_validation_socket_calls": 0, "promotion_validation_native_cli_calls": 0,
        "monitoring_policy_ok": True, "monitoring_policy_socket_calls": 0,
        "monitoring_policy_native_cli_calls": 0, "monitoring_policy_credentials_accessed": False,
        "synthetic_replay_ok": True, "readonly_report_ok": True,
        "replay_report_socket_calls": 0, "synthetic_outcomes_ok": True,
        "outcomes_socket_calls": 0, "verified_training_labels": 0,
        "calendar_package_data_ok": True, "calendar_socket_calls": 0,
        "readonly_signal_report_ok": True, "signal_socket_calls": 0,
        "collection_task_options_ok": True, "private_task_status_ok": True, "task_status_socket_calls": 0}))


if __name__ == "__main__":
    check()
