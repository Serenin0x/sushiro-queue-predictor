"""Synthetic clocks, explicit private plans and injected query calls."""
from datetime import datetime, timedelta, timezone
import unittest
import contextlib
import io
from copy import deepcopy
from types import SimpleNamespace
from sushiwait.cli import _PreflightStop, main
from pathlib import Path
import tempfile,json
from unittest.mock import patch
from sushiwait import adaptive as m


class AdaptiveScheduleTests(unittest.TestCase):
    def wall(self, minutes=0):
        return (datetime(2020,1,1,tzinfo=timezone.utc)+timedelta(minutes=minutes)).isoformat()
    def plan(self, store='900001', at=60, **changes):
        return {'store_id':store,'desired_arrival_at':self.wall(at),**changes}
    def runner(self, plans, **changes):
        options=dict(allowed_stores=['900001','900002'],base_interval=300,duration_seconds=3600,
                     max_queries=120,wall=self.wall(),monotonic=0)
        options.update(changes)
        return m.AdaptiveSchedule({'schema_version':1,'plans':plans},**options)
    def decision(self,s,minutes=0,mono=None):
        return s.decision(wall=self.wall(minutes),monotonic=minutes*60 if mono is None else mono)
    def mark(self,s,store='900001',minutes=0):
        s.mark_actual_start(store,wall=self.wall(minutes),monotonic=minutes*60)
    def test_same_store_demands_share_one_query(self):
        s=self.runner([self.plan(at=60),self.plan(at=10)])
        self.assertEqual(self.decision(s)['due_stores'],['900001']);self.mark(s)
        self.assertEqual(self.decision(s,mono=29)['due_stores'],[])
        self.assertEqual(self.decision(s,minutes=.5)['due_stores'],['900001'])
    def test_distinct_store_cadences(self):
        s=self.runner([self.plan(at=10),self.plan('900002',60)])
        self.mark(s);self.mark(s,'900002')
        self.assertEqual(self.decision(s,.5)['due_stores'],['900001'])
        self.assertEqual(self.decision(s,.5)['wake_monotonic'],30)
    def test_nearest_boundary_wakes_before_background_poll(self):
        s=self.runner([self.plan(at=31)])
        self.mark(s);self.assertEqual(self.decision(s)['wake_monotonic'],60)
        self.assertEqual(self.decision(s,1)['due_stores'],['900001'])
    def test_15_minute_boundary_shortens_without_duplicate(self):
        s=self.runner([self.plan(at=16)])
        self.mark(s);self.mark(s,minutes=1)
        self.assertEqual(self.decision(s,1)['due_stores'],[])
        self.assertEqual(self.decision(s,1.5)['due_stores'],['900001'])
    def test_offset_uses_earlier_horizon(self):
        s=self.runner([self.plan(at=31,call_offset_minutes=-20)])
        self.mark(s);self.assertEqual(self.decision(s)['wake_monotonic'],30)
    def test_elapsed_recovery_has_no_catchup_burst(self):
        s=self.runner([self.plan(at=10)])
        self.mark(s);s.resumed(wall=self.wall(3),monotonic=180)
        self.assertEqual(self.decision(s,3)['due_stores'],[])
        self.assertEqual(self.decision(s,3.5)['due_stores'],['900001'])
        self.assertEqual(s.starts['900001'][1],0)
    def test_terminals_and_empty_plans_complete(self):
        for plans in [[],[self.plan(plan_status='called')]]:
            self.assertTrue(self.decision(self.runner(plans))['done'])
    def test_duration_and_query_bound_stop(self):
        s=self.runner([self.plan()],duration_seconds=60);self.mark(s)
        self.assertTrue(self.decision(s,1)['done'])
        s=self.runner([self.plan()],max_queries=1);self.mark(s)
        self.assertTrue(self.decision(s)['done'])
    def test_future_or_borrowed_poll_start_not_accepted(self):
        with self.assertRaises(m.ScheduleError):
            m.AdaptiveSchedule({'schema_version':1,'plans':[self.plan()],'last_poll_started_at':{'900001':self.wall()}},allowed_stores=['900001'],base_interval=300,duration_seconds=60,max_queries=1,wall=self.wall(),monotonic=0)
    def test_scope_backwards_clock_and_early_start_refused(self):
        with self.assertRaises(m.ScheduleError):self.runner([self.plan('900003')])
        s=self.runner([self.plan()]);self.mark(s)
        with self.assertRaises(m.ScheduleError):self.mark(s)
        self.decision(s,1)
        with self.assertRaises(m.ScheduleError):self.decision(s,0,mono=61)
    def test_clock_nan_and_bounds_refused(self):
        for changes in [{'monotonic':float('nan')},{'monotonic':True},{'duration_seconds':True},{'max_queries':361}]:
            with self.assertRaises(m.ScheduleError):self.runner([],**changes)
    def test_inputs_unchanged(self):
        plans=[self.plan()];before=deepcopy(plans);s=self.runner(plans);self.mark(s)
        self.assertEqual(plans,before)
    def file_schedule(self, body, mode=0o600):
        with tempfile.TemporaryDirectory() as name:
            folder=Path(name).resolve();folder.chmod(0o700);path=folder/'plan.json';path.write_bytes(body);path.chmod(mode)
            with patch('socket.socket',side_effect=AssertionError('no_network')),patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('no_auth')):
                before=path.read_bytes()
                result=m.schedule_from_file(path,allowed_stores=['900001'],base_interval=300,duration_seconds=120,max_queries=2,wall=self.wall(),monotonic=0)
                self.assertEqual(path.read_bytes(),before)
            return result
    def test_explicit_private_file_is_read_without_query_credentials(self):
        s=self.file_schedule(json.dumps({'schema_version':1,'plans':[self.plan(at=10)]}).encode())
        self.assertEqual(self.decision(s)['due_stores'],['900001'])
    def test_public_private_plan_permissions_are_rejected(self):
        with self.assertRaises(m.ScheduleError):self.file_schedule(b'{}',mode=0o644)
    def test_duplicate_json_or_extra_plan_fields_are_rejected(self):
        for raw in [b'{"schema_version":1,"schema_version":1}',json.dumps({'schema_version':1,'plans':[self.plan(extra='PRIVATE')]}).encode()]:
            with self.assertRaises(m.ScheduleError):self.file_schedule(raw)
    def test_borrowed_last_poll_from_file_is_refused(self):
        raw=json.dumps({'schema_version':1,'plans':[self.plan()],'last_poll_started_at':{'900001':self.wall()}}).encode()
        with self.assertRaises(m.ScheduleError):self.file_schedule(raw)


class AdaptiveDriverTests(unittest.TestCase):
    wall = AdaptiveScheduleTests.wall
    plan = AdaptiveScheduleTests.plan
    runner = AdaptiveScheduleTests.runner
    def run_driver(self, schedule, *, fail=False, stop_wait=False, stop_client=False, client_delay=0):
        clock=[0]; starts=[]; stops=[]; outputs=[]
        def wall():return datetime(2020,1,1,tzinfo=timezone.utc)+timedelta(seconds=clock[0])
        def wait(deadline):
            if stop_wait:raise _PreflightStop('auth_expiring')
            clock[0]=deadline
        def client():
            clock[0]+=client_delay
            if stop_client:raise _PreflightStop('auth_expiring')
            return object()
        def observe(_client,shop,_db,**kwargs):
            starts.append((shop,clock[0]));return not fail
        session=SimpleNamespace(args=SimpleNamespace(api_profile='miniapp_gateway'),client=client,wait_until=wait)
        code=m.run_adaptive(schedule,session,object(),wall_clock=wall,monotonic_clock=lambda:clock[0],observe=observe,record_stop=lambda *a:stops.append(a),emit=outputs.append,preflight_stop=_PreflightStop)
        return code,starts,stops,outputs
    def test_driver_30_second_queries_are_shared_and_bounded(self):
        schedule=self.runner([self.plan(at=10),self.plan(at=12)],duration_seconds=120)
        code,starts,stops,outputs=self.run_driver(schedule)
        self.assertEqual(code,0);self.assertEqual(starts,[('900001',0),('900001',30),('900001',60),('900001',90)])
        self.assertEqual(stops,[]);self.assertEqual(outputs[-1]['queries_started'],4)
    def test_driver_distinct_store_speeds_do_not_force_background_burst(self):
        schedule=self.runner([self.plan(at=10),self.plan('900002',60)],duration_seconds=120)
        _,starts,_,_=self.run_driver(schedule)
        self.assertEqual([at for shop,at in starts if shop=='900002'],[0])
    def test_driver_auth_pause_is_preflight_without_network(self):
        schedule=self.runner([self.plan(at=10)],duration_seconds=120)
        code,starts,stops,_=self.run_driver(schedule,stop_client=True)
        self.assertEqual(code,1);self.assertEqual(starts,[]);self.assertEqual(len(stops),1)
    def test_driver_protection_during_wait_does_not_issue_next_query(self):
        schedule=self.runner([self.plan(at=10)],duration_seconds=120)
        code,starts,stops,_=self.run_driver(schedule,stop_wait=True)
        self.assertEqual(code,1);self.assertEqual(starts,[('900001',0)]);self.assertEqual(len(stops),1)
    def test_driver_duration_after_slow_preflight_stops_before_http(self):
        schedule=self.runner([self.plan(at=10)],duration_seconds=30)
        code,starts,_,_=self.run_driver(schedule,client_delay=30)
        self.assertEqual(code,0);self.assertEqual(starts,[])
    def test_driver_first_failed_get_has_no_retry(self):
        schedule=self.runner([self.plan(at=10)],duration_seconds=120)
        code,starts,_,_=self.run_driver(schedule,fail=True)
        self.assertEqual(code,1);self.assertEqual(starts,[('900001',0)])
    def test_terminal_driver_has_no_auth_or_query(self):
        schedule=self.runner([self.plan(plan_status='ended')],duration_seconds=120)
        code,starts,stops,_=self.run_driver(schedule,stop_client=True)
        self.assertEqual(code,0);self.assertEqual(starts,[]);self.assertEqual(stops,[])
    def test_driver_bound_checks_after_each_store(self):
        schedule=self.runner([self.plan(at=10),self.plan('900002',10)],max_queries=1)
        code,starts,_,outputs=self.run_driver(schedule)
        self.assertEqual(code,0);self.assertEqual(len(starts),1);self.assertEqual(outputs[-1]['queries_started'],1)


class AdaptiveCliTests(unittest.TestCase):
    def invoke(self, document, *, extra=(), hardlink=False, client=None, observe=None):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name).resolve()
            folder.chmod(0o700)
            path, database, credentials = folder/'plans.json', folder/'samples.sqlite3', folder/'credentials.json'
            path.write_text(json.dumps(document))
            path.chmod(0o600)
            if hardlink:
                import os
                os.link(path, database)
            initial = path.read_bytes()
            output = io.StringIO()
            clock = [0]
            def wall():
                return datetime(2020,1,1,tzinfo=timezone.utc)+timedelta(seconds=clock[0])
            def wait(deadline):
                clock[0] = deadline
            session = SimpleNamespace(args=SimpleNamespace(api_profile='miniapp_gateway'),
                client=client or (lambda: object()), wait_until=wait)
            with patch('socket.socket', side_effect=AssertionError('network')), \
                    patch('sushiwait.credentials.read_credentials_file', side_effect=AssertionError('credentials')) as auth, \
                    patch('sushiwait.cli._utc_clock', side_effect=wall), \
                    patch('sushiwait.cli.time.monotonic', side_effect=lambda:clock[0]), \
                    patch('sushiwait.cli._QuerySession', return_value=session), \
                    patch('sushiwait.cli.observe', side_effect=observe or AssertionError('unexpected query')), \
                    contextlib.redirect_stdout(output):
                result = main(['monitor-collect', '--plans-file', str(path), '--db', str(database),
                    '--credentials-file', str(credentials), '--api-profile', 'miniapp_gateway',
                    '--store-id', '900001', '--duration-seconds', '90', *extra])
            self.assertEqual(path.read_bytes(), initial)
            self.assertEqual(auth.call_count, 0)
            text = output.getvalue()
            self.assertNotIn(str(path), text)
            self.assertNotIn('PRIVATE_VALUE', text)
            return result, [json.loads(line) for line in text.splitlines()], database.exists()

    def plan(self, **changes):
        return {'schema_version':1,'plans':[{'store_id':'900001',
            'desired_arrival_at':'2020-01-01T00:10:00Z',**changes}]}

    def test_cli_merges_two_plans_and_drives_bounded_query_callbacks(self):
        document = self.plan()
        document['plans'] *= 2
        starts = []
        code, output, exists = self.invoke(document, observe=lambda _c,s,_db,**kw: starts.append(s) or True)
        self.assertEqual(code, 0)
        self.assertEqual(starts, ['900001']*3)
        self.assertTrue(exists)
        self.assertEqual(output[-1]['successful_queries'], 3)
        self.assertFalse(output[-1]['eta_available'])

    def test_cli_terminal_plan_creates_no_database_or_auth_request(self):
        code, output, exists = self.invoke(self.plan(plan_status='ended'))
        self.assertEqual(code, 0)
        self.assertFalse(exists)
        self.assertEqual(output[-1]['queries_started'], 0)

    def test_cli_invalid_plan_scope_and_bounds_touch_no_database(self):
        for document, extra in [(self.plan(store_id='900002'), ()),
                (self.plan(private='PRIVATE_VALUE'), ()), (self.plan(), ('--duration-seconds','29'))]:
            code, output, exists = self.invoke(document, extra=extra)
            self.assertEqual(code, 1)
            self.assertFalse(exists)
            self.assertIn('error_code', output[-1])

    def test_cli_hardlinked_input_database_is_rejected_without_overwrite(self):
        code, output, _ = self.invoke(self.plan(), hardlink=True)
        self.assertEqual(code, 1)
        self.assertEqual(output[-1]['error_code'], 'schedule_path_conflict')

    def test_cli_failed_query_does_not_retry(self):
        calls = []
        code, _, _ = self.invoke(self.plan(), observe=lambda *_a,**_kw: calls.append(1) or False)
        self.assertEqual(code, 1)
        self.assertEqual(calls, [1])


if __name__=='__main__':unittest.main()
