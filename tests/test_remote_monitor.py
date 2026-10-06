"""Offline integration of shared 60/30-second scheduling and anonymous queries."""
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sushiwait.adaptive import AdaptiveSchedule
from sushiwait.cli import main
from sushiwait.remote import RemoteClient, RemoteStore, QUEUE_NAMES, monitor_remote


class Clock:
    def __init__(self, jump=0):
        self.seconds, self.jump, self.sleeps = 0, jump, []
        self.base = datetime(2026, 10, 6, tzinfo=timezone.utc)
    def wall(self): return self.base + timedelta(seconds=self.seconds)
    def mono(self): return self.seconds
    def sleep(self, duration):
        self.sleeps.append(duration)
        self.seconds += max(duration, self.jump)
        self.jump = 0


class Response:
    def __init__(self, request, status):
        self.request, self.status, self.headers = request, status, {}
    def getcode(self): return self.status
    def geturl(self): return self.request.full_url
    def close(self): pass
    def read(self, size):
        value = ({name: [] for name in QUEUE_NAMES} if 'groupqueues?' in self.request.full_url else 0)
        return json.dumps(value).encode()[:size]


class Opener:
    def __init__(self, clock, *, fail_endpoint=None, latency=0):
        self.clock, self.fail_endpoint, self.latency = clock, fail_endpoint, latency
        self.calls = []
    def open(self, request, *, timeout):
        self.calls.append((self.clock.seconds, request.full_url))
        self.clock.seconds += self.latency
        return Response(request, 429 if self.fail_endpoint and self.fail_endpoint in request.full_url else 200)


def document(clock, arrival_seconds, *, copies=1, stores=('3014',), status='waiting'):
    return {'schema_version': 1, 'plans': [
        {'store_id': store, 'desired_arrival_at': (clock.base + timedelta(seconds=arrival_seconds)).isoformat(),
         'plan_status': status} for store in stores for _ in range(copies)]}


class RemoteMonitorTests(unittest.TestCase):
    def run_case(self, *, arrival=1860, copies=1, stores=('3014',), status='waiting',
                 base=3600, duration=600, maximum=3, jump=0, fail=None, latency=0):
        clock = Clock(jump)
        schedule = AdaptiveSchedule(document(clock, arrival, copies=copies, stores=stores, status=status),
            allowed_stores=list(stores), base_interval=base, duration_seconds=duration,
            max_queries=maximum, wall=clock.wall().isoformat(), monotonic=clock.mono())
        opener = Opener(clock, fail_endpoint=fail, latency=latency)
        events = []
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder).resolve() / 'remote.sqlite3'
            with patch('socket.socket', side_effect=AssertionError('network')), \
                    patch('sushiwait.cli.read_credentials_file', side_effect=AssertionError('credentials')), \
                    RemoteStore(path) as database:
                result = monitor_remote(schedule, RemoteClient(opener=opener), database,
                    wall_clock=clock.wall, monotonic_clock=clock.mono,
                    sleep=clock.sleep, emit=events.append)
                report = database.report(stores[0])
        return clock, opener, result, events, report

    def test_thirty_minute_boundary_rechecks_before_base_interval_and_coalesces(self):
        _, opener, summary, events, report = self.run_case(copies=2, maximum=2)
        self.assertEqual([at for at,url in opener.calls if 'groupqueues?' in url], [0, 60])
        self.assertEqual(summary['requests_attempted'], 4)
        self.assertEqual(report['successful_pairs'], 2)
        self.assertNotIn('desired_arrival_at', json.dumps(events))
        self.assertFalse(summary['personal_plan_details_in_output'])

    def test_fifteen_minute_boundary_reduces_period_to_thirty_seconds(self):
        _, opener, summary, _, _ = self.run_case(arrival=960, copies=2)
        self.assertEqual([at for at,url in opener.calls if 'groupqueues?' in url], [0, 60, 90])
        self.assertEqual(summary['pairs_started'], 3)

    def test_missed_slots_query_current_once_without_backlog_burst(self):
        _, opener, summary, _, _ = self.run_case(jump=300)
        self.assertEqual([at for at,url in opener.calls if 'groupqueues?' in url], [0, 300, 360])
        self.assertEqual(summary['requests_attempted'], 6)
        self.assertEqual(summary['retries'], 0)

    def test_queue_failure_stops_without_count_or_other_store(self):
        _, opener, summary, events, report = self.run_case(stores=('3004','3014'), fail='groupqueues')
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(summary['pairs_started'], 1)
        self.assertTrue(summary['stopped_on_failure'])
        self.assertEqual(report['failed_pairs'], 1)
        self.assertEqual(events[0]['record']['queries']['storequeuecount']['error_code'], 'preceding_query_failed')

    def test_count_failure_keeps_partial_queue_and_stops(self):
        _, opener, summary, events, _ = self.run_case(stores=('3004','3014'), fail='storequeuecount')
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(summary['successful_pairs'], 0)
        self.assertTrue(events[0]['record']['queries']['groupqueues']['ok'])
        self.assertIsNone(events[0]['record']['queries']['storequeuecount']['payload'])

    def test_pair_budget_is_shared_across_stores_and_bounds_both_gets(self):
        _, opener, summary, _, _ = self.run_case(stores=('2009','3004','3014'), maximum=2)
        self.assertEqual(len(opener.calls), 4)
        self.assertEqual(summary['maximum_pair_budget'], 2)
        self.assertEqual(summary['maximum_request_budget'], 4)
        self.assertFalse(any('storeid=3014' in url for _,url in opener.calls))

    def test_deadline_stops_new_pairs_but_retains_inflight_pair(self):
        clock, opener, summary, _, _ = self.run_case(duration=30, latency=20, maximum=3)
        self.assertEqual(clock.seconds, 40)
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(summary['pairs_started'], 1)
        self.assertEqual(summary['successful_pairs'], 1)

    def test_terminal_plans_end_without_queries_or_invented_capabilities(self):
        clock, opener, summary, events, _ = self.run_case(status='ended')
        self.assertEqual(opener.calls, [])
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(summary['pairs_started'], 0)
        for key in ('eta_available','notification_sent','business_operation_performed',
                    'missed_call_prevention_guaranteed','upstream_frequency_verified'):
            self.assertIs(summary[key], False)
        self.assertEqual(events, [{'remote_monitor_summary': summary}])

    def test_cli_bad_budget_does_not_read_plans_credentials_or_open_database(self):
        with patch('socket.socket', side_effect=AssertionError('network')) as network, \
                patch('sushiwait.cli.read_credentials_file', side_effect=AssertionError('credentials')) as credentials, \
                patch('sushiwait.cli.schedule_from_file', side_effect=AssertionError('plan')) as plan, \
                patch('sushiwait.remote.RemoteStore', side_effect=AssertionError('database')) as database, \
                patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(main(['remote-monitor','--db','unused','--store-id','3014',
                '--plan-file','unused','--max-pairs','361']), 2)
        self.assertEqual([x.call_count for x in (network,credentials,plan,database)], [0]*4)

    def test_cli_out_of_scope_private_plan_stops_before_query_or_database(self):
        self.invalid_file(store='3004')

    def test_cli_public_mode_plan_stops_before_query_or_database(self):
        self.invalid_file(mode=0o644)

    def invalid_file(self, *, store='3014', mode=0o600):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder).resolve() / 'plan.json'
            data = document(Clock(), 1860, stores=(store,))
            path.write_text(json.dumps(data));path.chmod(mode)
            with patch('socket.socket', side_effect=AssertionError('network')) as network, \
                    patch('sushiwait.cli.read_credentials_file', side_effect=AssertionError('credentials')) as credentials, \
                    patch('sushiwait.remote.RemoteClient', side_effect=AssertionError('client')) as client, \
                    patch('sushiwait.remote.RemoteStore', side_effect=AssertionError('database')) as database, \
                    patch('sys.stdout', new_callable=io.StringIO) as out:
                self.assertEqual(main(['remote-monitor','--db','unused','--store-id','3014',
                    '--plan-file',str(path)]), 2)
            self.assertEqual([x.call_count for x in (network,credentials,client,database)], [0]*4)
            self.assertNotIn(data['plans'][0]['desired_arrival_at'], out.getvalue())
            self.assertNotIn(str(path), out.getvalue())

    def test_cli_terminal_private_plan_applies_scheduler_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            parent=Path(folder).resolve();path=parent/'plan.json'
            data=document(Clock(), 1860, status='ended')
            path.write_text(json.dumps(data));path.chmod(0o600)
            with patch('socket.socket', side_effect=AssertionError('network')) as network, \
                    patch('sushiwait.cli.read_credentials_file', side_effect=AssertionError('credentials')) as credentials, \
                    patch('sys.stdout', new_callable=io.StringIO) as out:
                self.assertEqual(main(['remote-monitor','--db',str(parent/'remote.sqlite3'),
                    '--store-id','3014','--plan-file',str(path)]), 0)
            summary=json.loads(out.getvalue())['remote_monitor_summary']
            self.assertEqual(summary['requests_attempted'],0)
            self.assertEqual((network.call_count,credentials.call_count),(0,0))
            self.assertNotIn('desired_arrival_at',out.getvalue())

    def test_cli_active_shared_plan_writes_one_pair_without_private_plan_details(self):
        with tempfile.TemporaryDirectory() as folder:
            parent=Path(folder).resolve();path=parent/'plan.json'
            arrival=(datetime.now(timezone.utc)+timedelta(minutes=16)).isoformat()
            data={'schema_version':1,'plans':[{'store_id':'3014','desired_arrival_at':arrival}]*2}
            path.write_text(json.dumps(data));path.chmod(0o600)
            opener=Opener(Clock())
            with patch('socket.socket',side_effect=AssertionError('network')) as network, \
                    patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('credentials')) as credentials, \
                    patch('sushiwait.remote.RemoteClient',return_value=RemoteClient(opener=opener)), \
                    patch('sys.stdout',new_callable=io.StringIO) as out:
                self.assertEqual(main(['remote-monitor','--db',str(parent/'remote.sqlite3'),
                    '--store-id','3014','--plan-file',str(path),'--max-pairs','1']),0)
            self.assertEqual((network.call_count,credentials.call_count),(0,0))
            self.assertEqual(len(opener.calls),2)
            self.assertNotIn(arrival,out.getvalue())
            with RemoteStore(parent/'remote.sqlite3',read_only=True) as database:
                self.assertEqual(database.report('3014')['successful_pairs'],1)


if __name__=='__main__':
    unittest.main()
