"""Actual daily boundary/ledger/projection checks with synthetic data only."""
from copy import deepcopy
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock

from sushiwait.packets import encoded
from tests.test_materialsource import TARGET, PACKAGE

spec = importlib.util.spec_from_file_location('official_daily_service',
    Path(__file__).resolve().parents[1] / 'deploy' / 'official_daily_service.py')
daily = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daily)


def config():
    return {'schema_version': 1, 'first_day': '2026-10-15',
            'source': {'app_id': TARGET, 'source_version': 181, 'package_sha256': PACKAGE},
            'supplier_root': '/home/ubuntu/sushiwait-official-cloud-e0462-20261012',
            'archive_groups': [
                {'name': 'primary', 'store_ids': daily.STORES[:3], 'archive': '/tmp/primary.sqlite'},
                {'name': 'extra', 'store_ids': daily.STORES[3:], 'archive': '/tmp/extra.sqlite'}]}


class DailyServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.network = patch('socket.create_connection', side_effect=AssertionError('no real network')).start()
        self.addCleanup(patch.stopall)

    def test_every_day_has_new_manifest_and_weekend_opening(self):
        values = [daily.make_plan(config(), datetime.fromisoformat(f'2026-10-{d}T12:00:00+08:00'))
                  for d in (15, 16, 17, 18, 19)]
        self.assertEqual(len({v['plan_id'] for v in values}), 5)
        self.assertEqual([v['windows'][0]['start'][11:16] for v in values],
                         ['11:00', '11:00', '10:30', '10:30', '11:00'])
        self.assertEqual([v['ordinary_query_limit'] for v in values], [680, 680, 695, 695, 680])
        self.assertTrue(all(v['cadence'] == 'meal-peaks-v1' and v['renewal_limit'] == 14 for v in values))

    def test_midnight_and_next_year_do_not_end_recurring_schedule(self):
        for when in ('2026-10-16T00:00:00+08:00', '2027-01-02T12:00:00+08:00'):
            value = daily.make_plan(config(), datetime.fromisoformat(when))
            self.assertTrue(daily.campaign.validate_plan(value))
        with self.assertRaises(ValueError): daily.window_for(datetime(2026, 10, 15))

    def test_no_retrospective_trial_or_old_journal_reuse(self):
        with self.assertRaises(ValueError):
            daily.make_plan(config(), datetime.fromisoformat('2026-10-14T12:00:00+08:00'))
        value = daily.make_plan(config(), datetime.fromisoformat('2026-10-15T12:00:00+08:00'))
        with self.assertRaises(daily.campaign.TrialError): daily.campaign.legacy.validate_plan(value)
        self.assertEqual(value['windows'][0]['end'], '2026-10-15T22:00:00+08:00')

    def test_all_five_stores_have_smooth_child_arguments(self):
        value = daily.make_plan(config(), datetime.fromisoformat('2026-10-15T12:00:00+08:00'))
        start, end, rounds = daily.campaign.validate_plan(value)[0]
        args = daily.campaign.legacy.collect_arguments(self.root, value['store_ids'], rounds,
            self.root / 'task.json', False, start, end, interval=600, cadence=value['cadence'])
        self.assertEqual(args.count('--store-id'), 5)
        self.assertEqual(args[args.index('--cadence') + 1], 'meal-peaks-v1')
        self.assertEqual(rounds, 136)

    def test_daily_usage_rejects_same_task_with_wrong_cadence(self):
        value = daily.make_plan(config(), datetime.fromisoformat('2026-10-15T12:00:00+08:00'))
        start = daily.campaign.validate_plan(value)[0][0]
        daily.campaign.task_path(self.root, start, value['cadence']).touch(mode=0o600)
        status = {'store_ids': daily.STORES, 'api_profile': 'miniapp_gateway',
                  'interval_seconds': 600, 'target_rounds': 136}
        with patch.object(daily.campaign.legacy, 'task_status', return_value=status), self.assertRaises(ValueError):
            daily.campaign.query_usage(self.root, value)

    def test_failure_latch_survives_restart_and_tomorrow(self):
        daily.latch(self.root, 'cloud_refresh_failed', '2026-10-15')
        before = (self.root / 'collection-blocked.json').read_bytes()
        self.assertTrue(daily.latched(self.root))
        stopped = threading.Event()
        with patch.object(stopped, 'wait', side_effect=lambda _: stopped.set()), \
             patch.object(daily, 'prepare_day') as prepare:
            daily.run_collection(self.root, config(), stopped)
        prepare.assert_not_called()
        self.assertEqual(before, (self.root / 'collection-blocked.json').read_bytes())
        self.network.assert_not_called()

    def test_repeated_write_cannot_mutate_existing_plan(self):
        path = self.root / 'campaign-plan.json'
        daily.put_once(path, encoded({'a': 1}))
        daily.put_once(path, encoded({'a': 1}))
        with self.assertRaises(ValueError): daily.put_once(path, encoded({'a': 2}))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_projection_groups_use_independent_bound_cursors(self):
        value = daily.make_plan(config(), datetime.fromisoformat('2026-10-15T12:00:00+08:00'))
        root = self.root / value['plan_id']; root.mkdir(mode=0o700)
        scoped = {**value, 'store_ids': daily.STORES[:3], 'ordinary_query_limit': 408}
        extra = {**value, 'store_ids': daily.STORES[3:], 'ordinary_query_limit': 272}
        with daily.projection.Projection(root, self.root / 'a.sqlite', scoped, namespace='public-outbox-primary') as a, \
             daily.projection.Projection(root, self.root / 'b.sqlite', extra, namespace='public-outbox-extra') as b:
            self.assertNotEqual(a.pin, b.pin)
            self.assertNotEqual(a.outbox, b.outbox)
            self.assertEqual(a.scope['store_ids'] + b.scope['store_ids'], daily.STORES)


if __name__ == '__main__':
    unittest.main()
