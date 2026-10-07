"""Automatic private tracking, shared leases and concurrent late-model guards."""
import asyncio
import contextlib
from copy import deepcopy
from datetime import timedelta
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_tracking as fixture
import test_deepseek as model_fixture
import test_remote_service as service_fixture
import test_remote_window as window_fixture
from sushiwait.cli import main
from sushiwait.remote import RemoteClient, RemoteStore
from sushiwait.remoteservice import LiveRemoteView, RemoteASGI, RemoteQueueService
from sushiwait.remotewindow import RemoteWindowTask, window_config, collect_remote_window
from sushiwait.shared_monitoring import shared_polling_policy, SharedMonitoringError
from sushiwait.tracking import create_session, end_session, TrackingSession, session_status
from sushiwait.trackerloop import (TrackingCoordinator, RemoteProjectionReader,
    TrackerLoopError, run_tracking)

BASE, stamp = fixture.BASE, fixture.stamp


class TrackerLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(); self.root.chmod(0o700)
        self.state = self.root/'state'; self.state.mkdir(mode=0o700)
        self.ticket = fixture.ticket(desired_arrival_at=None)
        create_session(self.ticket, directory=self.state, now=BASE)
        self.now, self.mono, self.reads = BASE+timedelta(seconds=1), 1, []
        self.view = LiveRemoteView(['900001'], stale_after_seconds=360)
        self.view.publish(fixture.record(0, ('13', '14')))
        self.feed_dir = self.root/'feed'; self.feed_dir.mkdir(mode=0o700)
        self.feed = self.feed_dir/'plans.json'; self.series = str(uuid4())

    def reader(self, store):
        self.reads.append(store)
        return self.view.tracking_projection(store, now=self.now, service_state='running', worker_alive=True)

    def coordinator(self, **kw):
        options = dict(stores=['900001'], reader=self.reader, clock=lambda: self.now, monotonic=lambda: self.mono)
        options.update(kw)
        result = TrackingCoordinator(options.pop('directories', [self.state]), **options)
        self.addCleanup(result.close)
        return result

    def finish(self, coordinator):
        for future in list(coordinator.futures.values()):
            future.result(timeout=3)
        coordinator._reap()

    def publish(self, second, labels=('14', '15'), fail=None):
        self.now, self.mono = BASE+timedelta(seconds=second+1), second+1
        self.view.publish(fixture.record(second, labels, fail))

    def prediction(self, version=1, directory=None):
        directory = self.state if directory is None else directory
        with TrackingSession(directory) as session:
            return session._read(f'prediction-{version:04d}.json')

    def candidate(self, preparation):
        return fixture.TrackingTests.candidate(self, preparation, fast=True)

    def configure_model(self):
        self.ledger = self.root/'ledger'; self.ledger.mkdir(mode=0o700)
        self.keys = self.root/'keys'; self.keys.mkdir(mode=0o700)
        self.budget_file, self.key_file = self.ledger/'budget.json', self.keys/'key.json'
        budget = model_fixture.budget(); budget.update(created_at=stamp(-1), deadline_at=stamp(3600))
        self.budget_file.write_text(json.dumps(budget)); self.budget_file.chmod(0o600)
        self.key_file.write_text(json.dumps({'schema_version': 1, 'purpose': 'deepseek_official',
                                           'api_key': 'sk-SyntheticOnlyKey'})); self.key_file.chmod(0o600)
        self.view = LiveRemoteView(['900001'], stale_after_seconds=360)
        self.view.publish(fixture.record(-30, ('9', '10')))
        self.view.publish(fixture.record(0, ('13', '14')))

    def response(self):
        return json.dumps(model_fixture.response()).encode()

    def test_automatic_cycle_commits_observation_and_missing_prediction_private(self):
        c = self.coordinator(); first = c.cycle(); self.finish(c)
        self.assertEqual(first['local_projection_reads'], 1)
        self.assertEqual(c.counts['observations_committed'], 1)
        self.assertEqual(c.counts['predictions_committed'], 1)
        self.assertEqual(self.prediction()['unavailable_reason'], 'history_not_supplied')
        for p in self.state.iterdir(): self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertFalse(c.summary()['eta_available'])

    def test_same_store_people_share_one_reader_and_one_scheduler_target(self):
        other = self.root/'second'; other.mkdir(mode=0o700)
        create_session(fixture.ticket(number='88', desired_arrival_at=None), directory=other, now=BASE)
        c = self.coordinator(directories=[self.state, other], plan_output=self.feed, plan_series_id=self.series)
        c.cycle(); self.finish(c)
        self.assertEqual(self.reads, ['900001'])
        policy = shared_polling_policy(json.loads(self.feed.read_bytes())['document'], as_of=stamp(1), base_interval=300)
        self.assertEqual(policy['store_count'], 1)
        self.assertEqual(policy['stores'][0]['plan_count'], 2)
        self.assertEqual(policy['stores'][0]['requested_interval_seconds'], 30)
        self.assertEqual(policy['stores'][0]['requested_queries_per_due'], 1)
        self.assertNotIn(self.ticket['episode_id'], self.feed.read_text())
        self.assertNotIn('number', self.feed.read_text())

    def test_repeated_read_does_not_create_source_update_or_private_version(self):
        c = self.coordinator(); c.cycle(); self.finish(c)
        self.now = BASE+timedelta(seconds=5); c.cycle(); self.finish(c)
        self.assertEqual(c.counts['observations_committed'], 1)
        self.assertEqual(c.counts['predictions_committed'], 1)
        self.assertEqual(c.counts['local_projection_reads'], 2)

    def test_elapsed_tick_advances_private_version_without_claiming_new_source(self):
        c = self.coordinator(candidate_builder=self.candidate)
        c.cycle(); self.finish(c)
        first = self.prediction()
        self.now = BASE+timedelta(seconds=31); c.cycle(); self.finish(c)
        second = self.prediction(2)
        self.assertEqual(first['fusion_plan']['public_context']['observation_revision'],
                         second['fusion_plan']['public_context']['observation_revision'])
        self.assertEqual(first['fusion_plan']['public_context']['latest_queue_received_at'],
                         second['fusion_plan']['public_context']['latest_queue_received_at'])
        self.assertEqual(first['research_call_time_envelopes'], second['research_call_time_envelopes'])
        self.assertEqual(first['fusion']['wait_quantile_envelopes_us']['p50']['lower_us']-
                         second['fusion']['wait_quantile_envelopes_us']['p50']['lower_us'], 30_000_000)

    def test_new_queue_response_updates_before_old_poll_interval(self):
        c = self.coordinator(); c.cycle(); self.finish(c)
        self.publish(6); c.cycle(); self.finish(c)
        self.assertEqual(c.counts['observations_committed'], 2)
        self.assertEqual(self.prediction(2)['display']['state'], 'previously_seen_not_currently_displayed')
        self.assertFalse(self.prediction(2)['display']['no_show_verified'])

    def test_failure_and_stale_display_do_not_reuse_success_as_current(self):
        c = self.coordinator(candidate_builder=self.candidate); c.cycle(); self.finish(c)
        self.publish(10, fail='groupqueues'); c.cycle(); self.finish(c)
        self.assertEqual(self.prediction(2)['unavailable_reason'], 'current_display_evidence_unavailable')
        self.assertIsNone(self.prediction(2)['research_call_time_envelopes'])
        self.assertEqual(self.prediction(2)['polling_request']['requested_interval_seconds'], 30)

    def test_unknown_issue_time_is_not_manufactured_by_loop(self):
        other = self.root/'unknown'; other.mkdir(mode=0o700)
        create_session(fixture.ticket(issued_at=None, desired_arrival_at=None), directory=other, now=BASE)
        c = self.coordinator(directories=[other]); c.cycle(); self.finish(c)
        self.assertEqual(self.prediction(directory=other)['unavailable_reason'], 'issued_time_unknown')

    def test_slow_calculation_does_not_block_new_observation_and_late_result_rejected(self):
        began, release = threading.Event(), threading.Event()
        def build(prep):
            if prep['receipt']['tracking_version'] == 1:
                began.set(); self.assertTrue(release.wait(3))
            return self.candidate(prep)
        c = self.coordinator(candidate_builder=build); self.addCleanup(release.set)
        c.cycle(); self.assertTrue(began.wait(2))
        self.publish(10); c.cycle()
        self.assertEqual(session_status(directory=self.state, now=self.now)['tracking_version'], 2)
        release.set(); self.finish(c)
        self.assertFalse((self.state/'prediction-0001.json').exists())
        self.assertEqual(c.counts['superseded_jobs'], 1)
        c.cycle(); self.finish(c)
        self.assertTrue((self.state/'prediction-0002.json').exists())

    def test_caller_end_while_computation_pending_rejects_result_and_releases_demand(self):
        began, release = threading.Event(), threading.Event()
        def build(prep):
            began.set(); self.assertTrue(release.wait(3)); return self.candidate(prep)
        c = self.coordinator(candidate_builder=build, plan_output=self.feed, plan_series_id=self.series)
        self.addCleanup(release.set); c.cycle(); self.assertTrue(began.wait(2))
        end_session(directory=self.state, status='ended', declared_at=stamp(1), now=self.now)
        c.publish_demands(); release.set(); self.finish(c)
        self.assertFalse((self.state/'prediction-0001.json').exists())
        doc = json.loads(self.feed.read_bytes())['document']
        self.assertIsNone(shared_polling_policy(doc, as_of=stamp(1), base_interval=300)['stores'][0]['requested_interval_seconds'])

    def test_model_actual_mock_wire_changes_numerical_private_prediction(self):
        self.configure_model(); calls = []
        def transmit(body, key, timeout):
            calls.append(json.loads(body)); return self.response()
        c = self.coordinator(candidate_builder=self.candidate, allow_paid_request=True,
            budget_file=self.budget_file, key_file=self.key_file, transport=transmit)
        c.cycle(); self.finish(c)
        value = self.prediction()
        self.assertEqual(len(calls), 1)
        self.assertTrue(value['fusion']['ai_numerical_influence_applied'])
        self.assertEqual(value['fusion']['wait_quantile_envelopes_us']['p50']['lower_us'], 99_000_000)
        wire = json.dumps(calls[0])
        for personal in (self.ticket['episode_id'], self.ticket['issued_at'], 'intervals', 'checked_in'):
            self.assertNotIn(personal, wire)
        self.assertEqual(value['provider_adapter_summary']['transport_basis'], 'injected_transport')
        self.assertFalse(value['provider_adapter_summary']['billing_verified'])
        self.assertEqual(c.counts['provider_request_attempts'], 1)
        self.assertEqual(c.counts['provider_network_calls'], 0)

    def test_late_mock_provider_reply_rejected_newer_local_version_no_refund(self):
        self.configure_model(); began, release, calls = threading.Event(), threading.Event(), []
        def transmit(body, key, timeout):
            calls.append(body); began.set(); self.assertTrue(release.wait(3)); return self.response()
        c = self.coordinator(candidate_builder=self.candidate, allow_paid_request=True,
            budget_file=self.budget_file, key_file=self.key_file, transport=transmit)
        self.addCleanup(release.set); c.cycle(); self.assertTrue(began.wait(2))
        self.assertTrue((self.ledger/'deepseek-call-1.json').exists())
        self.publish(10); c.cycle(); release.set(); self.finish(c)
        self.assertFalse((self.state/'prediction-0001.json').exists())
        self.assertEqual(c.counts['superseded_jobs'], 1)
        self.assertEqual(c.counts['provider_request_attempts'], 1)
        self.assertEqual(len(calls), 1)
        self.assertTrue((self.ledger/'deepseek-call-1.json').exists())

    def test_clock_only_update_does_not_repeat_provider_post(self):
        self.configure_model(); calls = []
        def transmit(body, key, timeout): calls.append(body); return self.response()
        c = self.coordinator(candidate_builder=self.candidate, allow_paid_request=True,
            budget_file=self.budget_file, key_file=self.key_file, transport=transmit)
        c.cycle(); self.finish(c)
        self.now = BASE+timedelta(seconds=31); c.cycle(); self.finish(c)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.prediction(2)['fusion']['fallback_reason'], 'deepseek_duplicate_observation')
        self.assertFalse(self.prediction(2)['fusion']['advice_accepted'])

    def test_disabled_model_does_not_open_key_or_budget(self):
        c = self.coordinator(candidate_builder=self.candidate)
        with patch('sushiwait.deepseek._key', side_effect=AssertionError('key')), \
                patch('sushiwait.deepseek._Ledger', side_effect=AssertionError('ledger')):
            c.cycle(); self.finish(c)
        self.assertEqual(c.counts['provider_request_attempts'], 0)

    def test_plan_series_cannot_replace_another_operators_feed(self):
        c = self.coordinator(plan_output=self.feed, plan_series_id=self.series); c.publish_demands()
        before = self.feed.read_bytes()
        other = self.coordinator(plan_output=self.feed, plan_series_id=str(uuid4()))
        with self.assertRaises(TrackerLoopError): other.publish_demands()
        self.assertEqual(self.feed.read_bytes(), before)

    def test_lease_renews_locally_without_making_new_upstream_or_observation(self):
        c = self.coordinator(plan_output=self.feed, plan_series_id=self.series)
        c.publish_demands(); self.assertEqual(c.counts['plan_revisions_published'], 1)
        self.now = BASE+timedelta(seconds=10); self.assertFalse(c.publish_demands()['committed'])
        self.now = BASE+timedelta(seconds=31); self.assertTrue(c.publish_demands()['committed'])
        self.assertEqual(self.reads, []); self.assertEqual(c.counts['observations_committed'], 0)
        self.assertEqual(c.counts['plan_revisions_published'], 2)

    def test_unknown_arrival_expired_lease_returns_to_background_and_deadline_ends(self):
        c = self.coordinator(plan_output=self.feed, plan_series_id=self.series)
        c.cycle(); self.finish(c)
        doc = json.loads(self.feed.read_bytes())['document']
        self.assertIsNone(doc['plans'][0]['desired_arrival_at'])
        self.assertEqual(shared_polling_policy(doc, as_of=stamp(60), base_interval=300)['stores'][0]['requested_interval_seconds'], 30)
        self.assertEqual(shared_polling_policy(doc, as_of=stamp(61), base_interval=300)['stores'][0]['requested_interval_seconds'], 300)
        self.assertIsNone(shared_polling_policy(doc, as_of=stamp(7200), base_interval=300)['stores'][0]['requested_interval_seconds'])

    def test_lease_validation_rejects_forged_long_lived_or_partial_demand(self):
        base = {'store_id': '900001', 'desired_arrival_at': None,
            'polling_request': {'interval_seconds': 30, 'expires_at': stamp(60)}}
        for change in ({'polling_request': None}, {'polling_request': {'interval_seconds': True, 'expires_at': stamp(60)}},
                {'polling_request': {'interval_seconds': 45, 'expires_at': stamp(60)}},
                {'polling_request': {'interval_seconds': 30, 'expires_at': stamp(301)}},
                {'plan_expires_at': None}, {'call_offset_minutes': 10}):
            with self.subTest(change=change), self.assertRaises(SharedMonitoringError):
                shared_polling_policy({'schema_version': 1, 'plans': [{**base, **change}]}, as_of=stamp(0), base_interval=300)

    def test_real_window_scheduler_applies_shared_lease_and_preserves_budget_deadline(self):
        other = self.root/'second'; other.mkdir(mode=0o700)
        create_session(fixture.ticket(number='13', desired_arrival_at=None), directory=other, now=BASE)
        c = self.coordinator(directories=[self.state, other], plan_output=self.feed, plan_series_id=self.series)
        c.cycle(); self.finish(c)
        work = self.root/'worker'; work.mkdir(mode=0o700)
        seed = self.root/'initial.json'; seed.write_text(json.dumps({'schema_version': 1, 'plans': []})); seed.chmod(0o600)
        class Clock:
            seconds = 1
            def wall(self): return BASE+timedelta(seconds=self.seconds)
            def mono(self): return self.seconds
            def sleep(self, seconds): self.seconds += seconds
        clock = Clock(); opener = window_fixture.Opener(clock)
        config = window_config(work/'remote.sqlite3', seed, ['900001'], 300, 360, 4,
                               now=clock.wall(), plan_updates_file=self.feed)
        with patch('sushiwait.remote._utc', side_effect=lambda: clock.wall().isoformat()), \
                RemoteWindowTask(work/'task.json', config=config, resume=False, now=clock.wall()) as task:
            task.prepare_database()
            with RemoteStore(work/'remote.sqlite3') as database:
                task.bind(database, now=clock.wall())
                result = collect_remote_window(task, RemoteClient(opener=opener), wall_clock=clock.wall,
                    monotonic_clock=clock.mono, sleep=clock.sleep, emit=lambda _: None, should_stop=lambda: False)
        starts = [at for url, at in opener.calls if 'groupqueues?' in url]
        self.assertEqual(starts, [1, 31, 331])
        self.assertEqual(result['recorded_http_attempts'], 6)
        self.assertEqual(result['maximum_pair_budget'], 4)
        self.assertEqual(fixture._time(result['deadline_at']), fixture._time(stamp(361)))
        self.assertEqual(result['end_reason'], 'deadline')

    def test_bad_or_old_projection_is_rejected_without_creating_a_version(self):
        good = self.reader('900001')
        for bad in (None, {}, {**good, 'requested_store_id': '900002'},
                    {**good, 'generated_at': stamp(-40)}, {**good, 'upstream_snapshot_atomic': True}):
            c = self.coordinator(reader=lambda _, v=bad: v)
            with self.assertRaises(TrackerLoopError): c.cycle()
            self.assertFalse((self.state/'observation-0001.json').exists())

    def test_configuration_caps_duplicate_or_out_of_scope_sessions(self):
        for options in ({'directories': [self.state]*2}, {'stores': ['900002']}, {'read_interval': 1},
                        {'allow_paid_request': True}, {'directories': [self.state]*17}):
            with self.subTest(options=options), self.assertRaises(TrackerLoopError): self.coordinator(**options)

    def test_ended_sessions_do_not_read_or_create_new_forecasts(self):
        end_session(directory=self.state, status='ended', declared_at=stamp(0), now=self.now)
        c = self.coordinator(); result = c.cycle()
        self.assertEqual(result['active_sessions'], 0)
        self.assertEqual(self.reads, [])

    def test_private_update_cap_survives_coordinator_reopen(self):
        other = self.root/'bounded'; other.mkdir(mode=0o700)
        create_session(fixture.ticket(max_updates=1, desired_arrival_at=None), directory=other, now=BASE)
        c = self.coordinator(directories=[other]); c.cycle(); self.finish(c); c.close()
        self.publish(10)
        reopened = self.coordinator(directories=[other]); result = reopened.cycle()
        self.assertEqual(result['active_sessions'], 0)
        self.assertFalse((other/'observation-0002.json').exists())

    def test_bounded_runner_stops_after_exact_read_budget_no_catchup(self):
        c = self.coordinator()
        class Stop:
            def is_set(self): return False
            def wait(self, seconds):
                self.test.mono += 400
                self.test.now = BASE+timedelta(seconds=self.test.mono)
            test = self
        result = run_tracking(c, duration_seconds=60, max_cycles=4, stop=Stop())
        self.assertEqual(result['cycles'], 1)
        self.assertEqual(len(self.reads), 1)
        self.assertTrue(c.closed)

    def test_invalid_local_url_never_opens_socket(self):
        with patch('socket.socket', side_effect=AssertionError('network')):
            for url in ('https://127.0.0.1:1', 'http://localhost:1', 'http://example.com:80',
                        'http://127.0.0.1', 'http://user@127.0.0.1:1', 'http://127.0.0.1:1/a',
                        'http://127.0.0.1:1?x=1', 'http://127.0.0.1:01'):
                with self.subTest(url=url), self.assertRaises(TrackerLoopError): RemoteProjectionReader(url)

    def test_projection_route_is_read_only_atomic_local_and_no_extra_queries(self):
        test = self
        class Service:
            view = test.view
            wall_clock = lambda _: test.now
            status = lambda _: {'service_state': 'running', 'worker_alive': True}
            tracking_projection = RemoteQueueService.tracking_projection
        async def check():
            with patch('sqlite3.connect', side_effect=AssertionError('database')), \
                    patch('socket.socket', side_effect=AssertionError('network')):
                return await service_fixture.request(RemoteASGI(Service()),
                    '/api/v1/stores/900001/tracking-projection')
        status, frame, _ = asyncio.run(check())
        self.assertEqual(status, 200); self.assertTrue(frame['local_projection_atomic'])
        self.assertFalse(frame['upstream_snapshot_atomic'])
        self.assertEqual(frame['generated_at'], frame['history']['generated_at'])

    def test_cli_prepares_dedicated_feed_without_network_and_redacts_ticket(self):
        output = io.StringIO()
        with patch('socket.socket', side_effect=AssertionError('network')), contextlib.redirect_stdout(output), \
                patch('sushiwait.trackerloop._now', side_effect=lambda: self.now):
            status = main(['ticket-track-run', '--state-dir', str(self.state), '--store-id', '900001',
                '--prepare-plans-only', '--plan-output', str(self.feed), '--plan-series-id', self.series])
        self.assertEqual(status, 0)
        value = json.loads(output.getvalue()); self.assertEqual(value['local_projection_reads'], 0)
        self.assertEqual(value['prepared_plan_revision'], 1)
        for private in (str(self.state), self.ticket['episode_id'], self.series): self.assertNotIn(private, output.getvalue())

    def test_local_reader_uses_fixed_get_no_identity_headers_and_rejects_redirect(self):
        frame = self.reader('900001'); calls = []
        class Response:
            status = 200
            def getheader(self, key, default): return 'application/json; charset=utf-8'
            def read(self, limit): calls.append(('read', limit)); return json.dumps(frame).encode()
        class Connection:
            def __init__(self, host, port, timeout): calls.append(('connect', host, port, timeout))
            def request(self, *args, **kw): calls.append(('request', args, kw))
            def getresponse(self): return Response()
            def close(self): calls.append(('closed',))
        with patch('sushiwait.trackerloop.http.client.HTTPConnection', Connection):
            value = RemoteProjectionReader('http://127.0.0.1:12345')('900001')
            self.assertEqual(value['requested_store_id'], '900001')
            self.assertEqual(calls[1], ('request', ('GET', '/api/v1/stores/900001/tracking-projection'), {}))
            Response.status = 302
            with self.assertRaises(TrackerLoopError): RemoteProjectionReader('http://127.0.0.1:12345')('900001')
        self.assertEqual(calls[0], ('connect', '127.0.0.1', 12345, 5))
        self.assertEqual(calls[-1], ('closed',))

    def test_projection_lock_keeps_an_actual_concurrent_writer_out_of_both_copies(self):
        began, attempting, done = threading.Event(), threading.Event(), threading.Event()
        original = self.view.snapshot
        def publisher():
            self.assertTrue(began.wait(2)); attempting.set()
            self.view.publish(fixture.record(5, ('22',))); done.set()
        thread = threading.Thread(target=publisher); thread.start()
        def snapshot(*args, **kw):
            began.set(); self.assertTrue(attempting.wait(2))
            self.assertFalse(done.wait(.02)); return original(*args, **kw)
        with patch.object(self.view, 'snapshot', snapshot): frame = self.reader('900001')
        thread.join(2); self.assertFalse(thread.is_alive())
        self.assertEqual(frame['view']['fields']['groupqueues']['received_at'],
                         frame['history']['points'][-1]['queries']['groupqueues']['received_at'])
        self.assertEqual(frame['view']['fields']['groupqueues']['payload']['queues']['storeQueue'], ['13', '14'])
        self.assertTrue(done.is_set())

    def test_desired_time_bands_work_without_fabricated_number_position(self):
        other = self.root/'timed'; other.mkdir(mode=0o700)
        create_session(fixture.ticket(number='99', desired_arrival_at=stamp(1961)), directory=other, now=BASE)
        c = self.coordinator(directories=[other], plan_output=self.feed, plan_series_id=self.series)
        for at, interval in ((1, 300), (161, 60), (1061, 30)):
            self.now = BASE+timedelta(seconds=at)
            c.cycle(); self.finish(c)
            doc = json.loads(self.feed.read_bytes())['document']
            self.assertEqual(shared_polling_policy(doc, as_of=stamp(at), base_interval=300)
                             ['stores'][0]['requested_interval_seconds'], interval)
        self.assertIsNone(self.prediction(3, directory=other)['display']['exact_front_tables'])

    def test_close_prevents_pending_job_publication_without_ending_official_ticket(self):
        began, release = threading.Event(), threading.Event()
        def build(prep): began.set(); self.assertTrue(release.wait(3)); return self.candidate(prep)
        c = self.coordinator(candidate_builder=build); self.addCleanup(release.set)
        c.cycle(); self.assertTrue(began.wait(2))
        closer = threading.Thread(target=c.close); closer.start()
        deadline = time.monotonic()+2
        while not c.closed and time.monotonic() < deadline:
            threading.Event().wait(.005)
        # Obtain the same gate: close sets its closed flag before waiting on AI.
        with c.gate: self.assertTrue(c.closed)
        release.set(); closer.join(2); self.assertFalse(closer.is_alive())
        self.assertFalse((self.state/'prediction-0001.json').exists())
        self.assertFalse((self.state/'terminal.json').exists())

    def test_cli_runs_actual_automatic_loop_with_injected_local_reader(self):
        original = TrackingCoordinator
        def construct(*args, **kw):
            kw.update(clock=lambda: self.now, monotonic=lambda: self.mono)
            return original(*args, **kw)
        class Stop:
            test = self
            def is_set(self): return False
            def wait(self, seconds):
                # The CLI runner remains real; only time and its reader are
                # injected. Give the genuine compute thread a chance to commit.
                self.test.finish(self.test.running)
                self.test.now += timedelta(seconds=seconds); self.test.mono += seconds
        def construct_saved(*args, **kw):
            self.running = construct(*args, **kw); return self.running
        real_run = run_tracking
        def run(c, **kw): return real_run(c, stop=Stop(), **kw)
        output = io.StringIO()
        with patch('sushiwait.trackerloop.TrackingCoordinator', side_effect=construct_saved), \
                patch('sushiwait.trackerloop.RemoteProjectionReader', return_value=self.reader), \
                patch('sushiwait.trackerloop.run_tracking', side_effect=run), contextlib.redirect_stdout(output):
            code = main(['ticket-track-run', '--state-dir', str(self.state), '--store-id', '900001',
                '--collector-url', 'http://127.0.0.1:12345', '--duration', '60', '--max-cycles', '2'])
        self.assertEqual(code, 0); values = [json.loads(l) for l in output.getvalue().splitlines()]
        self.assertEqual(values[-1]['cycles'], 2)
        self.assertEqual(values[-1]['local_projection_reads'], 2)
        self.assertEqual(values[-1]['predictions_committed'], 1)
        for private in (str(self.state), self.ticket['episode_id'], self.ticket['issued_at']):
            self.assertNotIn(private, output.getvalue())

    def test_invalid_monotonic_clock_rejected_before_any_projection_read(self):
        for value in (float('nan'), float('inf'), True, -1):
            c = self.coordinator(monotonic=lambda v=value: v)
            with self.subTest(value=value), self.assertRaises(TrackerLoopError):
                run_tracking(c, duration_seconds=60, max_cycles=1)
            self.assertTrue(c.closed)
        self.assertEqual(self.reads, [])

    def test_cli_failed_final_summary_is_nonzero_exit(self):
        output = io.StringIO()
        with patch('sushiwait.trackerloop.RemoteProjectionReader', return_value=self.reader), \
                patch('sushiwait.trackerloop.run_tracking', return_value={'ok':False,'failed_jobs':1}), \
                contextlib.redirect_stdout(output):
            code = main(['ticket-track-run','--state-dir',str(self.state),'--store-id','900001',
                         '--collector-url','http://127.0.0.1:12345'])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output.getvalue())['ok'])


if __name__ == '__main__':
    unittest.main()
