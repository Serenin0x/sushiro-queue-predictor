"""Finite multi-window collection: real files, synthetic source and clocks."""
from copy import deepcopy
from datetime import timedelta
import asyncio
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_remote_window as fixture
from test_remote_service import request, wait_for
from test_plan_updates import SERIES
from sushiwait.cli import main
from sushiwait.remote import RemoteStore
from sushiwait.remotetasks import RemoteTaskError
from sushiwait.remotewindow import RemoteWindowTask
from sushiwait.remoteservice import RemoteASGI
from sushiwait.remotecampaign import (
    MAX_CAMPAIGN_DURATION, RemoteCampaign, RemoteCampaignService,
    campaign_config, remote_campaign_status,
)


class CampaignTests(unittest.TestCase):
    def setUp(self):
        fixture.RemoteWindowTests.setUp(self)
        self.root = self.parent / 'campaign'
        self.root.mkdir(mode=0o700)

    tearDown = fixture.RemoteWindowTests.tearDown
    starts = fixture.RemoteWindowTests.starts
    document = fixture.RemoteWindowTests.document
    diner = fixture.RemoteWindowTests.diner

    def config(self, *, stores=('900001',), base=60, duration=150, window=45, pairs=20, feed=None):
        return campaign_config(self.root, self.plan, list(stores), base, duration, window, pairs,
                               now=self.clock.wall(), plan_updates_file=feed)

    def collect(self, config=None, *, resume=False, auto=False, stop=lambda: False, sleep=None, factory=None):
        events = []
        with RemoteCampaign(config=config or self.config(), now=self.clock.wall(),
                            resume=resume, resume_if_present=auto) as campaign:
            result = campaign.collect(wall_clock=self.clock.wall, monotonic_clock=self.clock.mono,
                sleep=sleep or self.clock.sleep, emit=events.append, should_stop=stop,
                client_factory=factory or (lambda: self.client))
        return result, events

    def files(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def update(self, feed, revision, plans=None):
        value = {'schema_version': 1, 'series_id': SERIES, 'revision': revision,
                 'declared_at': self.clock.wall().isoformat(),
                 'document': {'schema_version': 1, 'plans': plans or []}}
        feed.write_text(json.dumps(value))
        feed.chmod(0o600)
        return value

    def feed(self):
        directory = self.parent / 'feed'
        directory.mkdir(mode=0o700)
        feed = directory / 'current.json'
        self.update(feed, 1)
        return feed

    def test_non_aligned_windows_keep_actual_store_cadence_and_global_deadline(self):
        result, events = self.collect()
        self.assertTrue(result['ok'])
        self.assertEqual(self.starts(), [0, 60, 120])
        self.assertEqual(self.clock.seconds, 150)
        self.assertEqual((result['windows_created'], result['windows_archived']), (3, 3))
        self.assertEqual(result['recorded_http_attempts'], 6)
        self.assertEqual([e['window_index'] for e in events if 'record' in e], [1, 2, 3])
        self.assertEqual(result['end_reason'], 'deadline')

    def test_multi_store_background_remains_one_pair_per_store_per_period(self):
        result, _ = self.collect(self.config(stores=('900001', '900002', '900003')))
        for store in ('900001', '900002', '900003'):
            self.assertEqual(self.starts(store), [0, 60, 120])
        self.assertEqual(result['recorded_http_attempts'], 18)

    def test_global_budget_is_not_replenished_at_rollover(self):
        result, _ = self.collect(self.config(pairs=2))
        self.assertEqual(self.starts(), [0, 60])
        self.assertEqual(result['end_reason'], 'budget')
        self.assertEqual(result['completed_pair_slots'], 2)
        self.assertEqual(result['windows_created'], 2)
        self.assertEqual(len(self.opener.calls), 4)

    def test_partial_stop_and_restart_preserve_deadline_budget_and_full_resume_wait(self):
        config = self.config(duration=300, window=90)
        first, _ = self.collect(config, stop=lambda: self.clock.seconds >= 65)
        original = first['deadline_at']
        self.assertEqual(first['state'], 'active')
        self.clock.seconds = 100
        final, _ = self.collect(config, resume=True)
        self.assertEqual(self.starts(), [0, 60, 160, 220, 280])
        self.assertEqual(final['deadline_at'], original)
        self.assertEqual(final['maximum_pair_budget'], 20)
        self.assertEqual(final['catch_up_requests'], 0)

    def test_expired_restart_reconciles_and_closes_without_new_client(self):
        config = self.config()
        self.collect(config, stop=lambda: self.clock.seconds >= 1)
        self.clock.seconds = 200
        with patch('socket.socket', side_effect=AssertionError('socket')) as network:
            result, _ = self.collect(config, resume=True, factory=lambda: (_ for _ in ()).throw(AssertionError('client')))
        self.assertEqual(result['end_reason'], 'deadline')
        self.assertEqual(self.starts(), [0])
        self.assertEqual(network.call_count, 0)

    def test_failed_pair_is_archived_then_campaign_stops_permanently(self):
        config = self.config()
        self.opener.fail = True
        result, _ = self.collect(config)
        before = self.files()
        self.assertEqual((result['state'], result['end_reason']), ('failed', 'query_failed'))
        self.assertEqual(result['windows_created'], 1)
        self.opener.fail = False
        self.clock.seconds = 10
        self.collect(config, resume=True, factory=lambda: (_ for _ in ()).throw(AssertionError('client')))
        self.assertEqual(self.files(), before)
        self.assertEqual(len(self.opener.calls), 1)

    def test_unknown_inflight_pair_is_charged_and_stops_before_any_retry(self):
        config = self.config()
        class InterruptedClient:
            def snapshot(inner, store):
                raise RuntimeError('simulated_crash')
        with self.assertRaisesRegex(RuntimeError, 'simulated_crash'):
            self.collect(config, factory=InterruptedClient)
        self.clock.seconds = 10
        result, _ = self.collect(config, resume=True, factory=lambda: (_ for _ in ()).throw(AssertionError('client')))
        self.assertEqual(result['uncertain_pair_slots'], 1)
        self.assertEqual(result['completed_pair_slots'], 1)
        self.assertEqual((result['state'], result['end_reason']), ('failed', 'uncertain_attempt'))
        self.assertEqual(result['unrecorded_http_attempts'], 'unknown')
        self.assertEqual(result['windows_archived'], 1)
        self.assertEqual(self.opener.calls, [])
        before = self.files()
        self.collect(config, resume=True)
        self.assertEqual(self.files(), before)

    def test_saved_inflight_result_is_reconciled_once_without_replay(self):
        config = self.config(duration=180, window=180)
        with patch.object(RemoteWindowTask, 'reconcile', side_effect=RuntimeError('after_save')):
            with self.assertRaisesRegex(RuntimeError, 'after_save'):
                self.collect(config)
        self.clock.seconds = 10
        result, _ = self.collect(config, resume=True)
        self.assertEqual(self.starts(), [0, 70, 130])
        self.assertEqual(result['successful_pairs'], 3)
        self.assertEqual(result['uncertain_pair_slots'], 0)

    def test_crash_after_archiving_failure_cannot_advance_to_a_new_window(self):
        config = self.config()
        self.opener.fail = True
        with patch.object(RemoteCampaign, '_finish', side_effect=RuntimeError('between_checkpoints')):
            with self.assertRaises(RuntimeError):
                self.collect(config)
        self.opener.fail = False
        self.clock.seconds = 10
        result, _ = self.collect(config, resume=True)
        self.assertEqual(result['end_reason'], 'query_failed')
        self.assertEqual(len(self.opener.calls), 1)
        self.assertEqual(result['windows_created'], 1)

    def test_terminal_status_and_cli_do_not_open_databases_or_network(self):
        config = self.config()
        self.collect(config)
        before = self.files()
        with patch('socket.socket', side_effect=AssertionError('network')) as network, \
             patch('sushiwait.remote.RemoteClient', side_effect=AssertionError('client')) as client, \
             patch('sushiwait.cli.read_credentials_file', side_effect=AssertionError('auth')) as auth, \
             patch('sys.stdout', new_callable=io.StringIO) as output, \
             patch('sushiwait.cli._utc_clock', side_effect=self.clock.wall):
            self.assertEqual(main(['remote-campaign-status', '--root', str(self.root)]), 0)
            self.assertEqual(main(['remote-campaign-collect', '--root', str(self.root), '--plan-file', str(self.plan),
                '--store-id', '900001', '--base-interval', '60', '--duration', '150',
                '--window-duration', '45', '--max-pairs', '20', '--resume-if-present']), 0)
        self.assertEqual((network.call_count, client.call_count, auth.call_count), (0, 0, 0))
        self.assertNotIn(str(self.root), output.getvalue())
        self.assertEqual(self.files(), before)
        self.assertEqual(len(self.opener.calls), 6)

    def test_manifest_missing_never_adopts_or_overwrites_previous_windows(self):
        config = self.config()
        self.collect(config, stop=lambda: self.clock.seconds >= 1)
        (self.root / 'campaign.json').unlink()
        before = self.files()
        with self.assertRaisesRegex(RemoteTaskError, 'orphan_or_foreign'):
            self.collect(config, auto=True)
        self.assertEqual(self.files(), before)
        self.assertEqual(len(self.opener.calls), 2)

    def test_missing_active_task_does_not_reset_window_or_budget(self):
        config = self.config()
        self.collect(config, stop=lambda: self.clock.seconds >= 1)
        (self.root / 'window-01' / 'task.json').unlink()
        before = self.files()
        with self.assertRaisesRegex(RemoteTaskError, 'missing'):
            self.collect(config, auto=True)
        self.assertEqual(self.files(), before)
        self.assertEqual(len(self.opener.calls), 2)

    def test_archive_database_change_is_rejected_before_next_query(self):
        config = self.config()
        self.collect(config)
        database = self.root / 'window-01' / 'remote.sqlite3'
        with database.open('ab') as stream:
            stream.write(b'changed')
        before = len(self.opener.calls)
        with self.assertRaisesRegex(RemoteTaskError, 'archive_changed'):
            self.collect(config, resume=True)
        self.assertEqual(len(self.opener.calls), before)

    def test_copying_archive_to_a_different_inode_is_rejected(self):
        config = self.config()
        self.collect(config)
        database = self.root / 'window-01' / 'remote.sqlite3'
        copy = database.with_suffix('.copy')
        copy.write_bytes(database.read_bytes())
        copy.chmod(0o600)
        copy.replace(database)
        with self.assertRaisesRegex(RemoteTaskError, 'identity_changed'):
            self.collect(config, resume=True)

    def test_window_symlink_and_foreign_state_are_rejected(self):
        foreign = self.root / 'window-01'
        foreign.symlink_to(self.parent, target_is_directory=True)
        with self.assertRaisesRegex(RemoteTaskError, 'orphan_or_foreign'):
            self.collect(auto=True)
        self.assertEqual(self.opener.calls, [])

    def test_one_campaign_writer_only(self):
        config = self.config()
        with RemoteCampaign(config=config, now=self.clock.wall()):
            with self.assertRaisesRegex(RemoteTaskError, 'busy'):
                RemoteCampaign(config=config, now=self.clock.wall(), resume=True)
        self.assertEqual(self.opener.calls, [])

    def test_configuration_cannot_change_across_restart(self):
        config = self.config()
        self.collect(config, stop=lambda: self.clock.seconds >= 1)
        changed = deepcopy(config)
        changed['max_pairs'] += 1
        with self.assertRaisesRegex(RemoteTaskError, 'config_conflict'):
            self.collect(changed, resume=True)

    def test_sleep_jump_keeps_gap_and_never_creates_missed_windows(self):
        config = self.config(duration=300, window=90)
        self.clock.jump = 200
        result, _ = self.collect(config)
        self.assertEqual(self.starts(), [0, 200, 260])
        self.assertEqual(result['windows_created'], 2)
        self.assertEqual(result['catch_up_requests'], 0)

    def test_wall_rollback_is_rejected_before_any_query(self):
        config = self.config()
        self.collect(config, stop=lambda: self.clock.seconds >= 1)
        self.clock.seconds = -1
        with self.assertRaisesRegex(RemoteTaskError, 'clock_rollback'):
            self.collect(config, resume=True)
        self.assertEqual(self.starts(), [0])

    def test_restart_barrier_spans_several_short_windows_without_resetting_it(self):
        config = self.config(duration=240, window=45)
        self.collect(config, stop=lambda: self.clock.seconds >= 1)
        self.clock.seconds = 50
        result, _ = self.collect(config, resume=True)
        self.assertEqual(self.starts(), [0, 110, 170, 230])
        self.assertEqual(result['end_reason'], 'deadline')

    def test_wall_forward_jump_at_rollover_cannot_bypass_monotonic_cadence(self):
        offset = [0]
        self.clock.wall = lambda: fixture.BASE + timedelta(seconds=self.clock.seconds + offset[0])
        def sleep(seconds):
            self.clock.sleep(seconds)
            if self.clock.seconds == 45:
                offset[0] = 100
        self.collect(self.config(duration=300, window=45), sleep=sleep)
        self.assertEqual(self.starts(), [0, 60, 120, 180])

    def test_crash_before_child_publication_keeps_orphan_and_makes_no_requests(self):
        config = self.config()
        with patch.object(RemoteCampaign, '_publish_child', side_effect=RuntimeError('before_publication')):
            with self.assertRaises(RuntimeError):
                self.collect(config)
        before = self.files()
        with self.assertRaisesRegex(RemoteTaskError, 'orphan_or_foreign'):
            self.collect(config, resume=True)
        self.assertEqual(before, self.files())
        self.assertEqual(self.opener.calls, [])

    def test_archived_checkpoint_edit_is_rejected_before_any_request(self):
        config = self.config()
        self.collect(config)
        path = self.root / 'window-01' / 'task.json'
        body = json.loads(path.read_text())
        body['updated_at'] = (fixture.BASE + timedelta(seconds=46)).isoformat()
        path.write_text(json.dumps(body))
        path.chmod(0o600)
        before = len(self.opener.calls)
        with self.assertRaises(RemoteTaskError):
            self.collect(config, resume=True)
        self.assertEqual(len(self.opener.calls), before)

    def test_child_budget_end_does_not_open_another_window_with_new_allowance(self):
        with patch('sushiwait.remotecampaign.MAX_PAIRS', 2):
            config = self.config(duration=300, window=300, pairs=5)
            result, _ = self.collect(config)
        self.assertEqual((result['state'], result['end_reason']), ('failed', 'window_budget'))
        self.assertEqual(result['completed_pair_slots'], 2)
        self.assertEqual(result['windows_created'], 1)

    def test_shared_30_second_plan_survives_window_boundaries(self):
        self.document([self.diner(seconds=600), self.diner(seconds=600)])
        result, _ = self.collect(self.config(base=300))
        self.assertEqual(self.starts(), [0, 30, 60, 90, 120])
        self.assertEqual(result['recorded_http_attempts'], 10)

    def test_hot_feed_is_retained_and_archives_ignore_later_feed_changes(self):
        feed = self.feed()
        config = self.config(base=300, feed=feed)
        def sleep(seconds):
            self.clock.sleep(seconds)
            if self.clock.seconds == 10:
                self.update(feed, 2, [self.diner(seconds=600)])
        result, _ = self.collect(config, sleep=sleep)
        self.assertEqual(self.starts(), [0, 30, 60, 90, 120])
        self.assertEqual(result['accepted_plan_revision'], 2)
        before = self.files()
        self.clock.seconds = 160
        self.update(feed, 3)
        final, _ = self.collect(config, resume=True)
        self.assertEqual(final['accepted_plan_revision'], 2)
        self.assertEqual(self.files(), before)

    def test_lower_feed_revision_is_rejected_between_windows(self):
        feed = self.feed()
        self.update(feed, 2)
        config = self.config(feed=feed)
        def sleep(seconds):
            self.clock.sleep(seconds)
            if self.clock.seconds == 45:
                self.update(feed, 1)
        with self.assertRaisesRegex(ValueError, 'rollback'):
            self.collect(config, sleep=sleep)
        self.assertEqual(self.starts(), [0])

    def test_bounded_duration_scope_and_window_count_are_validated(self):
        for kwargs in ({'duration': MAX_CAMPAIGN_DURATION + 1}, {'duration': 450, 'window': 30},
                       {'stores': ('900001', '900002', '900003', '900004')}, {'pairs': 0}, {'base': 30}):
            with self.subTest(kwargs=kwargs), self.assertRaises(RemoteTaskError):
                self.config(**kwargs)
        self.assertEqual(self.opener.calls, [])

    def test_private_permissions_and_unknown_source_truth_are_retained(self):
        result, _ = self.collect()
        for path in self.root.rglob('*'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)
        self.assertFalse(result['eta_available'])
        self.assertEqual(result['verified_training_labels'], 0)
        self.assertEqual(result['source_freshness'], 'unknown')

    def test_service_and_asgi_share_collection_across_multiple_windows(self):
        service = RemoteCampaignService(root=self.root, plan_file=self.plan, store_ids=['900001'],
            base_interval=60, duration_seconds=150, window_seconds=45, max_pairs=20,
            client_factory=lambda: self.client, wall_clock=self.clock.wall, monotonic_clock=self.clock.mono,
            wait=lambda seconds, event: self.clock.sleep(seconds))
        service.start()
        wait_for(lambda: not service.status()['worker_alive'])
        self.assertEqual(service.status()['service_state'], 'completed')
        before = len(self.opener.calls)
        app = RemoteASGI(service)
        for _ in range(5):
            status, body, _ = asyncio.run(request(app, '/api/v1/stores/900001/queue'))
            self.assertEqual(status, 200)
            self.assertEqual(body['fields']['groupqueues']['state'], 'last_known_only')
        status, _, _ = asyncio.run(request(app, '/health'))
        self.assertEqual(status, 503)
        self.assertEqual(len(self.opener.calls), before)
        self.assertEqual(service.status()['task']['windows_archived'], 3)
        service.shutdown()


if __name__ == '__main__':
    unittest.main()
