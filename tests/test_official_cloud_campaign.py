"""Finite campaign scheduling and durable limits; no real account or network."""
import contextlib
from copy import deepcopy
from datetime import datetime, timedelta
import importlib.util
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from sushiwait.packets import encoded, _write_packet
from tests.test_materialsource import TARGET, PACKAGE

SPEC = importlib.util.spec_from_file_location('campaign',
    Path(__file__).resolve().parents[1] / 'deploy' / 'official_cloud_campaign.py')
campaign = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(campaign)


def plan():
    return {'schema_version': 2, 'plan_id': 'three-stores-20261015', 'app_id': TARGET,
            'source_version': 181, 'package_sha256': PACKAGE,
            'store_ids': ['3014', '3004', '2009'], 'interval_seconds': 600,
            'ordinary_query_limit': 1404, 'renewal_limit': 98,
            'windows': [{'start': f'2026-10-{day}T{opening}:00+08:00',
                         'end': f'2026-10-{day}T22:00:00+08:00'}
                        for day, opening in ((15, '11:00'), (16, '11:00'), (17, '10:30'),
                                             (18, '10:30'), (19, '11:00'), (20, '11:00'), (21, '11:00'))]}


def weekend():
    value = plan()
    value.update(windows=[value['windows'][2]], ordinary_query_limit=207, renewal_limit=14)
    return value


def history(rounds=69, completed=207, *, pending=False, failed=0, unknown=0, state='completed'):
    return {'store_ids': plan()['store_ids'], 'api_profile': 'miniapp_gateway',
            'interval_seconds': 600, 'target_rounds': rounds, 'target_slots': rounds * 3,
            'completed_slots': completed, 'pending_attempt': pending,
            'successful_slots': completed - failed - unknown, 'failed_slots': failed,
            'uncertain_slots': unknown, 'state': state, 'stop_reason': None}


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.network = patch('socket.create_connection', side_effect=AssertionError('no network')).start()
        self.addCleanup(patch.stopall)
        self.output = io.StringIO()
        self.addCleanup(lambda: self.network.assert_not_called())

    def ledger(self, value):
        state = self.root / 'controller-state'
        state.mkdir(mode=0o700, exist_ok=True)
        return campaign.CampaignLedger(state, value)

    def test_exact_seven_day_capacity_and_weekend_half_open_grid(self):
        value = plan()
        windows = campaign.validate_plan(value)
        self.assertEqual([r for _, _, r in windows], [66, 66, 69, 69, 66, 66, 66])
        self.assertEqual(sum(r * 3 for _, _, r in windows), 1404)
        self.assertEqual(value['ordinary_query_limit'] + value['renewal_limit'], 1502)
        start, end, count = windows[2]
        self.assertEqual((start + timedelta(seconds=600 * (count - 1))).astimezone(campaign.CHINA).hour, 21)
        self.assertLess(start + timedelta(seconds=600 * (count - 1)), end)
        self.assertEqual(start + timedelta(seconds=600 * count), end)

    def test_invalid_limits_dates_types_and_stores_cannot_expand_campaign(self):
        for change in ({'schema_version': True}, {'interval_seconds': 300}, {'renewal_limit': 99},
                       {'ordinary_query_limit': 1405}, {'ordinary_query_limit': True},
                       {'store_ids': ['9999']}, {'store_ids': ['3014', '3014']},
                       {'plan_id': '../old-trial'}, {'source_version': 182}, {'approved': True}):
            with self.subTest(change=change), self.assertRaises(campaign.TrialError):
                campaign.validate_plan({**plan(), **change})
        for start in ('2026-10-22T11:00:00+08:00', '2026-10-15T11:00:00',
                      '2026-10-15T08:00:00+08:00'):
            value = plan()
            value['windows'][0]['start'] = start
            with self.subTest(start=start), self.assertRaises(campaign.TrialError):
                campaign.validate_plan(value)

    def test_duplicate_window_and_span_beyond_seven_days_are_invalid(self):
        value = plan()
        value['windows'][1] = deepcopy(value['windows'][0])
        with self.assertRaises(campaign.TrialError): campaign.validate_plan(value)
        value = plan()
        value['windows'][-1] = {'start': '2026-10-22T11:00:00+08:00', 'end': '2026-10-22T22:00:00+08:00'}
        with self.assertRaises(campaign.TrialError): campaign.validate_plan(value)

    def test_legacy_date_and_budget_guards_remain_unchanged(self):
        with self.assertRaises(campaign.TrialError): campaign.legacy.validate_plan(plan())
        self.assertEqual(campaign.legacy.Ledger.checkpoint_limit, 128)

    def test_all_98_attempts_and_seven_finishes_fit_persistent_checkpoints(self):
        value = plan()
        ledger = self.ledger(value)
        for revision in range(1, 99):
            ledger.reserve()
            ledger.accepted(revision, 'a' * 64)
        for index in range(7): ledger.finish(index)
        restored = campaign.CampaignLedger(ledger.root, value)
        self.assertEqual(restored.value['attempts'], 98)
        self.assertEqual(restored.value['highest_revision'], 98)
        self.assertEqual(restored.value['finished_windows'], list(range(7)))
        self.assertEqual(restored.index, 204)
        with self.assertRaisesRegex(campaign.TrialError, 'cloud_refresh_limit'): restored.reserve()

    def test_changed_plan_cannot_reset_existing_durable_ledger(self):
        value = plan()
        ledger = self.ledger(value)
        for change in ({'plan_id': 'another-plan'}, {'renewal_limit': 97},
                       {'ordinary_query_limit': 1403}, {'store_ids': ['3014', '2009', '3004']}):
            with self.subTest(change=change), self.assertRaisesRegex(campaign.TrialError, 'cloud_ledger_invalid'):
                campaign.CampaignLedger(ledger.root, {**value, **change})

    def test_crashed_refresh_reservation_never_becomes_a_free_retry(self):
        value = plan()
        ledger = self.ledger(value)
        ledger.reserve()
        with self.assertRaisesRegex(campaign.TrialError, 'cloud_refresh_outcome_unknown'):
            campaign.CampaignLedger(ledger.root, value)

    def test_unknown_failed_and_pending_ordinary_calls_consume_budget(self):
        value = plan()
        start = campaign.validate_plan(value)[0][0]
        campaign.task_path(self.root, start).touch(mode=0o600)
        with patch.object(campaign.legacy, 'task_status', return_value=history(66, 3, pending=True, failed=1, unknown=1)):
            used, _ = campaign.query_usage(self.root, value)
        self.assertEqual(used, 4)

    def test_new_day_uses_remaining_global_budget_and_resume_keeps_target(self):
        value = plan()
        value['ordinary_query_limit'] = 204
        first, next_start = [w[0] for w in campaign.validate_plan(value)[:2]]
        campaign.task_path(self.root, first).touch(mode=0o600)
        with patch.object(campaign.legacy, 'task_status', return_value=history(66, 198)):
            _, rounds, resume, _ = campaign.window_budget(self.root, value, next_start)
        self.assertEqual((rounds, resume), (2, False))
        campaign.task_path(self.root, next_start).touch(mode=0o600)
        with patch.object(campaign.legacy, 'task_status', side_effect=[history(66, 198), history(2, 3, state='running')]):
            _, rounds, resume, _ = campaign.window_budget(self.root, value, next_start)
        self.assertEqual((rounds, resume), (2, True))

    def test_incompatible_task_cannot_be_skipped_to_refund_quota(self):
        value = weekend()
        start = campaign.validate_plan(value)[0][0]
        campaign.task_path(self.root, start).touch(mode=0o600)
        for change in ({'target_rounds': 70}, {'interval_seconds': 300}, {'store_ids': ['9999']}):
            with patch.object(campaign.legacy, 'task_status', return_value={**history(), **change}), \
                    self.subTest(change=change), self.assertRaisesRegex(campaign.TrialError, 'cloud_task_budget_invalid'):
                campaign.query_usage(self.root, value)

    def test_full_budget_stops_before_private_source_or_supplier_or_child(self):
        value = plan()
        value['ordinary_query_limit'] = 198
        windows = campaign.validate_plan(value)
        campaign.task_path(self.root, windows[0][0]).touch(mode=0o600)
        ledger = self.ledger(value)
        ledger.finish(0)
        source, popen = Mock(), Mock()
        with patch.object(campaign.legacy, 'task_status', return_value=history(66, 198)), \
                contextlib.redirect_stdout(self.output):
            result = campaign.run_campaign(self.root, value, ledger, threading.Event(),
                    clock=lambda: windows[1][0], popen=popen, prepare=source)
        self.assertEqual(result, 0)
        self.assertIn('cloud_ordinary_budget_exhausted', self.output.getvalue())
        source.assert_not_called(); popen.assert_not_called()

    def test_interrupted_offhours_wait_reads_no_source_or_context(self):
        value = weekend()
        start = campaign.validate_plan(value)[0][0]
        stop = threading.Event()
        source, popen = Mock(), Mock()
        with patch.object(stop, 'wait', side_effect=lambda _: stop.set()), contextlib.redirect_stdout(self.output):
            result = campaign.run_campaign(self.root, value, self.ledger(value), stop,
                                          clock=lambda: start-timedelta(hours=1), popen=popen, prepare=source)
        self.assertEqual(result, 0)
        source.assert_not_called(); popen.assert_not_called()

    def test_missed_day_has_no_backfill_or_private_source_read(self):
        value = weekend()
        end = campaign.validate_plan(value)[0][1]
        ledger = self.ledger(value)
        source, popen = Mock(), Mock()
        with contextlib.redirect_stdout(self.output):
            result = campaign.run_campaign(self.root, value, ledger, threading.Event(),
                                          clock=lambda: end, popen=popen, prepare=source)
        self.assertEqual(result, 0)
        self.assertEqual(ledger.value['finished_windows'], [0])
        self.assertIn('"backfill": false', self.output.getvalue())
        source.assert_not_called(); popen.assert_not_called()

    def test_weekend_runtime_launches_69_rounds_then_persists_finished_window(self):
        value = weekend()
        start = campaign.validate_plan(value)[0][0]
        task = campaign.task_path(self.root, start)
        child = Mock(pid=12345, returncode=0)
        child.poll.return_value = 0
        def launch(*_, **__):
            task.touch(mode=0o600)
            return child
        popen, source = Mock(side_effect=launch), Mock()
        ledger = self.ledger(value)
        with patch.object(campaign.legacy, 'Controller') as controller, \
                patch.object(campaign.legacy, 'task_status', return_value=history()), contextlib.redirect_stdout(self.output):
            self.assertEqual(campaign.run_campaign(self.root, value, ledger, threading.Event(),
                            clock=lambda: start, popen=popen, prepare=source), 0)
        args = popen.call_args.args[0]
        self.assertEqual(args[args.index('--samples')+1], '69')
        self.assertEqual(args[args.index('--interval')+1], '600')
        self.assertIn('-I', args)
        controller.return_value.ensure_refresh.assert_called_once()
        self.assertEqual(campaign.CampaignLedger(ledger.root, value).value['finished_windows'], [0])

    def test_uncertain_resume_stops_before_supplier_or_new_reader(self):
        value = weekend()
        start = campaign.validate_plan(value)[0][0]
        campaign.task_path(self.root, start).touch(mode=0o600)
        source, popen = Mock(), Mock()
        with patch.object(campaign.legacy, 'task_status', return_value=history(completed=3, pending=True, state='halted')), \
                self.assertRaisesRegex(campaign.TrialError, 'cloud_reader_failed'):
            campaign.run_campaign(self.root, value, self.ledger(value), threading.Event(),
                                  clock=lambda: start, popen=popen, prepare=source)
        source.assert_not_called(); popen.assert_not_called()

    def test_unexpected_early_child_exit_cannot_mark_day_finished(self):
        value = weekend()
        start = campaign.validate_plan(value)[0][0]
        task = campaign.task_path(self.root, start)
        child = Mock(pid=12345, returncode=0)
        child.poll.return_value = 0
        def launch(*_, **__):
            task.touch(mode=0o600)
            return child
        ledger = self.ledger(value)
        with patch.object(campaign.legacy, 'Controller'), \
                patch.object(campaign.legacy, 'task_status', return_value=history(completed=3, state='running')), \
                contextlib.redirect_stdout(self.output), self.assertRaisesRegex(campaign.TrialError, 'cloud_reader_failed'):
            campaign.run_campaign(self.root, value, ledger, threading.Event(),
                                  clock=lambda: start, popen=Mock(side_effect=launch), prepare=Mock())
        self.assertEqual(ledger.value['finished_windows'], [])

    def test_main_rejects_old_trial_root_before_source_preparation(self):
        value = weekend()
        root = self.root / value['plan_id']
        root.mkdir(mode=0o700)
        _write_packet(encoded(value), root/'campaign-plan.json')
        (root/'trial-plan.json').touch(mode=0o600)
        with patch.object(campaign.sys, 'argv', ['campaign', str(root)]), \
                patch.object(campaign.legacy, 'prepare_material_source') as source, contextlib.redirect_stdout(self.output):
            self.assertEqual(campaign.main(), 1)
        source.assert_not_called()
        self.assertIn('cloud_campaign_root_invalid', self.output.getvalue())


if __name__ == '__main__':
    unittest.main()
