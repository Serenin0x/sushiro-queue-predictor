"""Daily boundaries, archived evidence and restart recovery; no real transport."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sushiwait.dailycontroller import DailyController, daily_config, read_daily_archive
from sushiwait.remotetasks import RemoteTaskError
from sushiwait.remote import RemoteClient
from test_business_hours import rules
from test_remote_window import Opener, Response

BASE = datetime(2026, 10, 9, 3, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.seconds = 0
    def wall(self):
        return BASE + timedelta(seconds=self.seconds)
    def mono(self):
        return self.seconds
    def sleep(self, seconds):
        self.seconds += seconds


class DailyControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()/'single'
        self.clock = Clock()
        self.opener = Opener(self.clock)
        self.hours = rules()
        self.hours['weekday_intervals'] = {str(i): [['11:00', '11:02']] for i in range(1, 8)}
        self.hours['known_statutory_holiday_intervals'] = [['11:00', '11:02']]
        self.utc = patch('sushiwait.remote._utc', side_effect=lambda: self.clock.wall().isoformat())
        self.receipt = patch('sushiwait.remoteintake._clock', side_effect=self.clock.wall)
        self.utc.start(); self.receipt.start()

    def tearDown(self):
        self.receipt.stop(); self.utc.stop(); self.tmp.cleanup()

    def config(self, **kwargs):
        return daily_config(self.root, '900001', self.hours, not_before=BASE.isoformat(), **kwargs)

    def tick(self, controller, **kwargs):
        return controller.tick(wall_clock=self.clock.wall, monotonic_clock=self.clock.mono,
            sleep=self.clock.sleep, emit=lambda _: None,
            client_factory=lambda: RemoteClient(opener=self.opener), **kwargs)

    def starts(self):
        return [at for url, at in self.opener.calls if 'groupqueues?' in url]

    def archive(self, day='2026-10-09', name='result.json'):
        return json.loads(read_daily_archive(self.root/day/name))

    def test_two_days_have_distinct_campaigns_and_previous_bytes_never_change(self):
        cfg = self.config()
        with DailyController(config=cfg, now=self.clock.wall()) as controller:
            result = self.tick(controller)
            self.assertEqual(result['last_finished_date'], '2026-10-09')
            before = {p: p.read_bytes() for p in (self.root/'2026-10-09').rglob('*') if p.is_file()}
            self.clock.seconds = 86400
            self.tick(controller)
        self.assertEqual(self.starts(), [0, 60, 86400, 86460])
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        for day in ['2026-10-09', '2026-10-10']:
            result = self.archive(day)
            self.assertEqual(result['task']['successful_pairs'], 2)
            self.assertEqual(result['task']['maximum_pair_budget'], 6)
            self.assertFalse(result['independent_backup'])
            self.assertEqual(self.archive(day, 'projection.json')['summary']['successful_pairs'], 2)

    def test_closed_hours_and_future_activation_never_create_client_or_day(self):
        self.clock.seconds = -60
        cfg = self.config()
        with DailyController(config=cfg, now=self.clock.wall()) as controller:
            result = controller.tick(wall_clock=self.clock.wall, monotonic_clock=self.clock.mono,
                sleep=self.clock.sleep, emit=lambda _: None,
                client_factory=lambda: self.fail('transport created while closed'))
            self.assertIsNone(result['current_date'])
            self.clock.seconds = 120
            self.tick(controller)
        self.assertEqual(self.opener.calls, [])
        self.assertFalse((self.root/'2026-10-09').exists())

    def test_late_boot_samples_current_instead_of_replaying_missed_minute(self):
        self.clock.seconds = 65
        with DailyController(config=self.config(), now=self.clock.wall()) as controller:
            self.tick(controller)
        self.assertEqual(self.starts(), [65])
        self.assertEqual(self.archive()['task']['created_at'], '2026-10-09T03:01:05.000Z')

    def test_resume_keeps_creation_deadline_budget_and_waits_one_interval(self):
        cfg = self.config()
        with DailyController(config=cfg, now=self.clock.wall()) as controller:
            self.tick(controller, should_stop=lambda: len(self.opener.calls) >= 2)
            saved = deepcopy(controller.value['current'])
        self.clock.seconds = 10
        with DailyController(config=cfg, now=self.clock.wall()) as controller:
            self.assertEqual(controller.value['current'], saved)
            self.tick(controller)
        self.assertEqual(self.starts(), [0, 70])
        result = self.archive()['task']
        self.assertEqual(result['deadline_at'], saved['deadline_at'])
        self.assertEqual(result['maximum_pair_budget'], saved['maximum_pair_budget'])

    def test_resume_after_close_finishes_old_day_without_transport(self):
        cfg = self.config()
        with DailyController(config=cfg, now=self.clock.wall()) as controller:
            self.tick(controller, should_stop=lambda: len(self.opener.calls) >= 2)
        self.clock.seconds = 86400
        with DailyController(config=cfg, now=self.clock.wall()) as controller:
            self.tick(controller)
            self.assertEqual(len(self.opener.calls), 2)
            self.tick(controller)
        self.assertEqual(self.starts(), [0, 86400, 86460])

    def test_seventeen_dates_remain_archived_without_global_dailyview_bound(self):
        with DailyController(config=self.config(), now=self.clock.wall()) as controller:
            for i in range(17):
                self.clock.seconds = 86400*i
                self.tick(controller)
        self.assertEqual(len(list(self.root.glob('*/projection.json'))), 17)
        self.assertEqual(len(self.opener.calls), 68)
        self.assertEqual(self.archive('2026-10-25')['task']['successful_pairs'], 2)

    def test_calendar_gaps_are_recorded_and_not_caught_up(self):
        with DailyController(config=self.config(), now=self.clock.wall()) as controller:
            self.tick(controller)
            self.clock.seconds = 3*86400
            result = self.tick(controller)
        self.assertEqual(result['calendar_dates_not_observed'], 2)
        self.assertEqual(len(self.opener.calls), 8)
        self.assertFalse((self.root/'2026-10-10').exists())

    def test_second_writer_same_namespace_fails_even_while_waiting(self):
        self.clock.seconds = -60
        with DailyController(config=self.config(), now=self.clock.wall()):
            with self.assertRaisesRegex(RemoteTaskError, 'busy'):
                DailyController(config=self.config(), now=self.clock.wall())
        self.assertEqual(self.opener.calls, [])

    def test_missing_controller_cannot_adopt_existing_day_or_reset_budget(self):
        cfg = self.config()
        with DailyController(config=cfg, now=self.clock.wall()) as controller:
            self.tick(controller)
        (self.root/'controller.json').unlink()
        with self.assertRaisesRegex(RemoteTaskError, 'missing_with_existing_state'):
            DailyController(config=cfg, now=self.clock.wall())
        self.assertEqual(len(self.opener.calls), 4)

    def test_config_change_and_clock_rollback_are_rejected(self):
        cfg = self.config()
        with DailyController(config=cfg, now=self.clock.wall()) as controller:
            self.tick(controller)
        for altered in [self.config(daily_pair_cap=1499), cfg]:
            self.clock.seconds = 200 if altered != cfg else 0
            with self.assertRaisesRegex(RemoteTaskError, 'config_conflict|clock_rollback'):
                DailyController(config=altered, now=self.clock.wall())

    def test_auth_failure_halts_and_is_not_retried_tomorrow(self):
        self.opener.open = lambda request, timeout: self._response(request, 401)
        cfg = self.config()
        with DailyController(config=cfg, now=self.clock.wall()) as controller:
            result = self.tick(controller)
            self.assertEqual(result['error_code'], 'daily_controller_day_failed')
        self.clock.seconds = 86400
        with DailyController(config=cfg, now=self.clock.wall()) as controller:
            self.tick(controller)
        self.assertEqual(len(self.opener.calls), 1)
        self.assertFalse((self.root/'2026-10-10').exists())

    def _response(self, request, status):
        self.opener.calls.append((request.full_url, self.clock.seconds))
        return Response(request, status)

    def test_recovered_transient_failure_remains_in_archive(self):
        original = self.opener.open
        def once(request, *, timeout):
            if not self.opener.calls:
                return self._response(request, 503)
            return original(request, timeout=timeout)
        self.opener.open = once
        with DailyController(config=self.config(), now=self.clock.wall()) as controller:
            self.tick(controller)
        result = self.archive()
        self.assertEqual((result['task']['successful_pairs'], result['task']['failed_pairs']), (1, 1))
        self.assertEqual(result['task']['transient_recoveries_used'], 1)
        self.assertEqual(self.archive(name='projection.json')['summary']['failed_pairs'], 1)

    def test_storage_reserve_stops_before_first_request_and_survives_restart(self):
        cfg = self.config()
        with DailyController(config=cfg, now=self.clock.wall()) as controller:
            with patch.object(controller, '_check_storage', side_effect=RemoteTaskError('reserve')):
                with self.assertRaisesRegex(RemoteTaskError, 'storage_or_input_error'):
                    self.tick(controller)
            self.assertEqual(controller.value['state'], 'halted')
        with DailyController(config=cfg, now=self.clock.wall()) as controller:
            self.tick(controller)
        self.assertEqual(self.opener.calls, [])

    def test_storage_pressure_after_commit_does_not_make_second_pair(self):
        with DailyController(config=self.config(), now=self.clock.wall()) as controller:
            with patch.object(controller, '_check_storage', side_effect=[None, RemoteTaskError('reserve')]):
                with self.assertRaises(RemoteTaskError): self.tick(controller)
        self.assertEqual(self.starts(), [0])

    def test_insufficient_declared_daily_cap_halts_without_transport(self):
        with DailyController(config=self.config(daily_pair_cap=5), now=self.clock.wall()) as controller:
            with self.assertRaises(RemoteTaskError): self.tick(controller)
            self.assertEqual(controller.value['state'], 'halted')
        self.assertEqual(self.opener.calls, [])

    def test_archive_completion_after_power_loss_is_deterministic_without_new_requests(self):
        class PowerLoss(BaseException): pass
        cfg = self.config()
        with DailyController(config=cfg, now=self.clock.wall()) as controller:
            original = controller._commit
            def fail_final(value):
                if value['last_finished_date'] is not None:
                    raise PowerLoss
                return original(value)
            with patch.object(controller, '_commit', side_effect=fail_final):
                with self.assertRaises(PowerLoss): self.tick(controller)
        before = {p: p.read_bytes() for p in self.root.glob('*/?*.json')}
        self.clock.seconds = 130
        with DailyController(config=cfg, now=self.clock.wall()) as controller:
            self.tick(controller)
        self.assertEqual(len(self.opener.calls), 4)
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_archive_reader_rejects_public_permissions_and_symlink(self):
        with DailyController(config=self.config(), now=self.clock.wall()) as controller:
            self.tick(controller)
        path = self.root/'2026-10-09/projection.json'
        path.chmod(0o644)
        with self.assertRaises(RemoteTaskError): read_daily_archive(path)
        path.chmod(0o600)
        link = self.root/'link.json'; link.symlink_to(path)
        with self.assertRaises(RemoteTaskError): read_daily_archive(link)

    def test_archive_conflict_after_power_loss_is_not_overwritten(self):
        class PowerLoss(BaseException): pass
        cfg = self.config()
        with DailyController(config=cfg, now=self.clock.wall()) as controller:
            original = controller._commit
            def fail_final(value):
                if value['last_finished_date'] is not None: raise PowerLoss
                return original(value)
            with patch.object(controller, '_commit', side_effect=fail_final):
                with self.assertRaises(PowerLoss): self.tick(controller)
        path = self.root/'2026-10-09/projection.json'; path.write_text('{}')
        self.clock.seconds = 130
        with DailyController(config=cfg, now=self.clock.wall()) as controller:
            with self.assertRaises(RemoteTaskError): self.tick(controller)
        self.assertEqual(path.read_text(), '{}')
        self.assertEqual(len(self.opener.calls), 4)

    def test_break_inside_open_day_waits_and_does_not_query_lunch_gap(self):
        self.hours['weekday_intervals']['5'] = [['11:00', '11:01'], ['11:03', '11:04']]
        with DailyController(config=self.config(), now=self.clock.wall()) as controller:
            self.tick(controller)
        self.assertEqual(self.starts(), [0, 180])

    def test_first_response_crosses_close_and_second_get_is_blocked(self):
        self.clock.seconds = 89
        original = self.opener.open
        def slow(request, *, timeout):
            result = original(request, timeout=timeout)
            self.clock.seconds += 32
            return result
        self.opener.open = slow
        with DailyController(config=self.config(), now=self.clock.wall()) as controller:
            self.tick(controller)
        self.assertEqual(len(self.opener.calls), 1)
        result = self.archive()['task']
        self.assertEqual(result['scheduled_pause_slots'], 1)
        self.assertEqual(result['recorded_http_attempts'], 1)
