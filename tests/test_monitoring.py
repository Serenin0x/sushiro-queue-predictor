"""User time semantics and monitoring boundaries, without any external calls."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

from sushiwait.cli import main
from sushiwait.monitoring import MonitoringError, polling_policy


class MonitoringTests(unittest.TestCase):
    def plan(self, **changes):
        return polling_policy(**{"desired_arrival_at": "2026-10-06T19:00:00+08:00",
                                 "as_of": "2026-10-06T18:00:00+08:00", "base_interval": 300,
                                 **changes})

    def test_plus_ten_is_after_arrival_and_not_uncertainty(self):
        result = self.plan(call_offset_minutes=10)
        self.assertEqual(result["target_call_at"], "2026-10-06T11:10:00.000000Z")
        self.assertEqual(result["monitor_horizon_at"], "2026-10-06T11:00:00.000000Z")
        self.assertFalse(result["eta_available"])

    def test_minus_ten_moves_horizon_earlier(self):
        result = self.plan(call_offset_minutes=-10, as_of="2026-10-06T18:40:00+08:00")
        self.assertEqual(result["target_call_at"], "2026-10-06T10:50:00.000000Z")
        self.assertEqual(result["requested_interval_seconds"], 30)

    def test_exact_thirty_minute_boundary_is_sixty_seconds(self):
        self.assertEqual(self.plan(as_of="2026-10-06T18:30:00+08:00")["requested_interval_seconds"], 60)

    def test_just_before_thirty_minute_boundary_is_background(self):
        self.assertEqual(self.plan(as_of="2026-10-06T18:29:59.999999+08:00")["requested_interval_seconds"], 300)

    def test_exact_fifteen_minute_boundary_is_thirty_seconds(self):
        self.assertEqual(self.plan(as_of="2026-10-06T18:45:00+08:00")["requested_interval_seconds"], 30)

    def test_just_before_fifteen_minute_boundary_is_sixty_seconds(self):
        self.assertEqual(self.plan(as_of="2026-10-06T18:44:59.999999+08:00")["requested_interval_seconds"], 60)

    def test_waiting_after_arrival_keeps_thirty_second_target(self):
        result = self.plan(as_of="2026-10-06T19:10:00+08:00")
        self.assertEqual(result["requested_interval_seconds"], 30)
        self.assertEqual(result["next_poll_target_at"], "2026-10-06T11:10:30.000000Z")

    def test_terminal_states_end_target_even_with_acceleration(self):
        for state in ("called", "no_show", "cancelled", "ended"):
            result = self.plan(plan_status=state, accelerated_display_turnover=True)
            self.assertIsNone(result["requested_interval_seconds"])
            self.assertIsNone(result["next_poll_target_at"])
            self.assertFalse(result["reestimate_requested"])

    def test_acceleration_advances_cadence_without_inventing_no_show_rate(self):
        result = self.plan(accelerated_display_turnover=True)
        self.assertEqual(result["requested_interval_seconds"], 30)
        self.assertIsNone(result["true_no_show_rate"])
        self.assertFalse(result["notification_sent"])

    def test_external_earliest_estimate_can_move_horizon_earlier(self):
        result = self.plan(earliest_call_at="2026-10-06T18:10:00+08:00")
        self.assertEqual(result["requested_interval_seconds"], 30)
        self.assertFalse(result["earliest_call_input_verified"])
        self.assertFalse(result["eta_available"])

    def test_later_earliest_estimate_never_delays_horizon(self):
        result = self.plan(earliest_call_at="2026-10-06T20:00:00+08:00")
        self.assertEqual(result["monitor_horizon_at"], "2026-10-06T11:00:00.000000Z")

    def test_equivalent_timezones_and_midnight_offset(self):
        self.assertEqual(self.plan(as_of="2026-10-06T10:30:00Z")["requested_interval_seconds"], 60)
        result = self.plan(desired_arrival_at="2026-10-06T00:05:00+08:00", call_offset_minutes=-10)
        self.assertEqual(result["target_call_at"], "2026-10-05T15:55:00.000000Z")

    def test_missing_timezone_dates_bad_input_and_overflow_are_refused(self):
        for value in ("2026-10-06", "2026-10-06T19:00:00", "not-time", None, "x"*65):
            with self.subTest(value=value), self.assertRaises(MonitoringError):
                self.plan(desired_arrival_at=value)
        with self.assertRaises(MonitoringError):
            self.plan(desired_arrival_at="9999-12-31T23:59:59Z", call_offset_minutes=1440)

    def test_next_target_overflow_is_refused(self):
        with self.assertRaises(MonitoringError):
            self.plan(as_of="9999-12-31T23:59:59Z")

    def test_invalid_interval_offset_state_and_flag_are_refused(self):
        changes = [{"base_interval": v} for v in (0, 59, 3601, True, 60.0)]
        changes += [{"call_offset_minutes": v} for v in (-1441, 1441, True, 10.0)]
        changes += [{"plan_status": v} for v in (None, [], "unknown")]
        changes += [{"accelerated_display_turnover": v} for v in (None, 0, "true")]
        for value in changes:
            with self.subTest(value=value), self.assertRaises(MonitoringError):
                self.plan(**value)

    def test_policy_does_not_call_network_credentials_or_native_controller(self):
        with patch("socket.socket", side_effect=AssertionError("network")), \
                patch("socket.create_connection", side_effect=AssertionError("network")), \
                patch("sushiwait.credentials.read_credentials_file", side_effect=AssertionError("credentials")), \
                patch("sushiwait.surgeguard._command", side_effect=AssertionError("native")):
            result = self.plan()
        for key in ("polling_performed", "scheduler_applied", "notification_sent", "business_operation_performed",
                    "network_performed", "eta_available", "missed_call_prevention_guaranteed"):
            self.assertFalse(result[key])
        self.assertEqual(result["source_freshness"], "unknown")

    def test_cli_is_explicit_offline_policy_and_redacts_bad_input(self):
        args = ["monitor-plan", "--desired-arrival-at", "2026-10-06T19:00:00+08:00",
                "--as-of", "2026-10-06T18:45:00+08:00", "--base-interval", "300"]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(args), 0)
        self.assertEqual(json.loads(output.getvalue())["requested_interval_seconds"], 30)
        args[2] = "synthetic-private-invalid-time"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(args), 1)
        self.assertNotIn("synthetic-private", output.getvalue())


if __name__ == "__main__":
    unittest.main()
