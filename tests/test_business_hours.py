"""Opening boundaries and real persisted tasks, synthetic transport only."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from sushiwait.businesshours import BusinessHours, read_hours, validate_hours
from sushiwait.remote import RemoteClient, RemoteStore, validate_record
from sushiwait.remotetasks import RemoteTaskError
from sushiwait.remotewindow import RemoteWindowTask, collect_remote_window, window_config
from sushiwait.remotecampaign import RemoteCampaign, campaign_config, remote_campaign_status
import test_remote_window as fixture

DEFAULT=Path(__file__).resolve().parents[1]/'config/default-business-hours.json'
def rules():return read_hours(DEFAULT)
def at(value):return datetime.fromisoformat(value+'+08:00')

class HoursRulesTests(unittest.TestCase):
    def test_weekday_and_exact_boundaries(self):
        hours=BusinessHours(rules())
        for stamp,expected in [('2026-10-09T10:59:59',False),('2026-10-09T11:00:00',True),
            ('2026-10-09T21:59:59',True),('2026-10-09T22:00:00',False)]:
            with self.subTest(stamp=stamp):self.assertEqual(hours.decision('900001',at(stamp))['is_open_window'],expected)
    def test_holiday_starts_at_1030(self):
        self.assertTrue(BusinessHours(rules()).decision('900001',at('2026-10-06T10:30:00'))['is_open_window'])
    def test_makeup_saturday_follows_user_natural_weekday(self):
        r=BusinessHours(rules()).decision('900001',at('2026-10-10T10:30:00'))
        self.assertEqual(r['date_type'],'makeup_workday');self.assertTrue(r['is_open_window'])
    def test_unknown_calendar_year_preserves_unknown(self):
        r=BusinessHours(rules()).decision('900001',at('2027-01-01T10:45:00'))
        self.assertEqual(r['date_type'],'unknown');self.assertFalse(r['is_open_window'])
    def test_store_specific_date_wins_global_date_and_store_week(self):
        r=rules();r['date_overrides']=[{'date':'2026-10-09','store_id':None,'intervals':[]},
            {'date':'2026-10-09','store_id':'900001','intervals':[['12:00','14:00']]}]
        h=BusinessHours(r);self.assertTrue(h.decision('900001',at('2026-10-09T13:00:00'))['is_open_window'])
        self.assertFalse(h.decision('900002',at('2026-10-09T13:00:00'))['is_open_window'])
    def test_multiple_daily_windows_and_lunch_gap(self):
        r=rules();r['weekday_intervals']['5']=[['11:00','14:00'],['17:00','22:00']]
        result=BusinessHours(r).decision('900001',at('2026-10-09T15:00:00'))
        self.assertEqual(result['next_open_at'],'2026-10-09T09:00:00.000Z')
    def test_no_declared_opening_is_not_guessed(self):
        r=rules();r['weekday_intervals']={d:[] for d in '1234567'};r['known_statutory_holiday_intervals']=[]
        self.assertIsNone(BusinessHours(r).decision('900001',at('2026-10-09T13:00:00'))['next_open_at'])
    def test_rejects_overlap_cross_midnight_boolean_schema_and_unknown_fields(self):
        for mutate in [lambda r:r.update(schema_version=True),lambda r:r.update(extra=1),
            lambda r:r['weekday_intervals'].update({'5':[['22:00','11:00']]}),
            lambda r:r['weekday_intervals'].update({'5':[['11:00','14:00'],['13:00','22:00']]}),
            lambda r:r.update(business_hours_verified=True)]:
            r=rules();mutate(r)
            with self.assertRaises(RemoteTaskError):validate_hours(r)
    def test_duplicate_overrides_rejected(self):
        r=rules();r['date_overrides']=[{'date':'2026-10-09','store_id':None,'intervals':[]}]*2
        with self.assertRaises(RemoteTaskError):validate_hours(r)
    def test_aware_clock_required(self):
        with self.assertRaises(RemoteTaskError):BusinessHours(rules()).decision('900001',datetime(2026,10,9))

class HoursCollectionTests(unittest.TestCase):
    setUp=fixture.RemoteWindowTests.setUp
    tearDown=fixture.RemoteWindowTests.tearDown
    def config(self,*,duration=180,pairs=20,hours=None):
        return window_config(self.db,self.plan,['900001'],60,duration,pairs,now=self.clock.wall(),business_hours=hours or rules())
    def collect(self,config,*,resume=False,sleep=None,emit=None):
        events=[]
        with RemoteWindowTask(self.path,config=config,resume=resume,now=self.clock.wall()) as task:
            task.prepare_database()
            with RemoteStore(self.db) as db:
                task.bind(db,now=self.clock.wall())
                result=collect_remote_window(task,self.client,wall_clock=self.clock.wall,monotonic_clock=self.clock.mono,
                    sleep=sleep or self.clock.sleep,emit=emit or events.append)
        return result,events
    def closed_rules(self):
        r=rules();r['date_overrides']=[{'date':'2026-10-06','store_id':None,'intervals':[]}];return r
    def brief_rules(self):
        r=rules();r['weekday_intervals']={d:[['20:00','20:01']] for d in '1234567'}
        r['known_statutory_holiday_intervals']=[['20:00','20:01']];return r
    def test_closed_window_creates_zero_http_and_keeps_budget(self):
        result,_=self.collect(self.config(hours=self.closed_rules()))
        self.assertEqual(len(self.opener.calls),0);self.assertEqual(result['completed_pair_slots'],0)
        self.assertEqual(result['end_reason'],'deadline');self.assertEqual(result['scheduled_pause_slots'],0)
    def test_preopening_wait_then_collects_without_catchup(self):
        r=rules();r['date_overrides']=[{'date':'2026-10-06','store_id':None,'intervals':[['20:01','20:03']]}]
        result,_=self.collect(self.config(hours=r))
        self.assertEqual([t for url,t in self.opener.calls if 'groupqueues?' in url],[60,120])
        self.assertEqual(result['successful_pairs'],2)
    def test_first_response_crosses_close_second_get_blocked(self):
        self.clock.seconds=59;original=self.opener.open
        def slow(request,**kwargs):
            response=original(request,**kwargs);self.clock.seconds+=2;return response
        self.opener.open=slow
        result,events=self.collect(self.config(hours=self.brief_rules()))
        self.assertEqual(len(self.opener.calls),1);self.assertEqual(result['recorded_http_attempts'],1)
        self.assertEqual(result['successful_pairs'],0);self.assertEqual(result['failed_pairs'],0)
        self.assertEqual(result['scheduled_pause_slots'],1)
        record=next(e['record'] for e in events if 'record' in e)
        self.assertEqual(record['queries']['storequeuecount']['error_code'],'business_window_closed')
        self.assertFalse(validate_record(record)['ok']);self.assertIsNone(self.client.request_guard)
    def test_transport_guard_cleanup_even_on_emit_error(self):
        def fail(_):raise RuntimeError('fixture_emit_failure')
        with self.assertRaises(RuntimeError):self.collect(self.config(),emit=fail)
        self.assertIsNone(self.client.request_guard)
    def test_partial_slot_counts_survive_resume_and_cannot_be_forged(self):
        self.clock.seconds=59;original=self.opener.open
        def slow(request,**kwargs):
            response=original(request,**kwargs);self.clock.seconds+=2;return response
        self.opener.open=slow;config=self.config(hours=self.brief_rules());result,_=self.collect(config)
        after=self.path.read_bytes();again,_=self.collect(config,resume=True)
        self.assertEqual(again['scheduled_pause_slots'],1);self.assertEqual(self.path.read_bytes(),after)
        value=json.loads(after);value['scheduled_pauses']=0;value['successful']=1
        self.path.write_text(json.dumps(value));self.path.chmod(0o600)
        with self.assertRaisesRegex(RemoteTaskError,'result_conflict'):self.collect(config,resume=True)
    def test_real_transport_failure_still_terminal_and_not_scheduled_pause(self):
        self.opener.fail=True;result,_=self.collect(self.config())
        self.assertEqual(result['state'],'failed');self.assertEqual(result['failed_pairs'],1)
        self.assertEqual(result['scheduled_pause_slots'],0)
    def test_restart_changed_hours_cannot_reset_deadline_or_budget(self):
        config=self.config(hours=self.closed_rules());self.collect(config)
        changed=deepcopy(config);changed['business_hours']['weekday_intervals']['5']=[['12:00','22:00']]
        with self.assertRaisesRegex(RemoteTaskError,'config_conflict'):self.collect(changed,resume=True)
    def test_duplicate_json_and_symlink_input_rejected(self):
        p=self.parent/'hours.json';p.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaises(RemoteTaskError):read_hours(p)
        p.unlink();p.symlink_to(DEFAULT)
        with self.assertRaises(RemoteTaskError):read_hours(p)
    def test_campaign_cross_day_partial_slot_and_global_budget(self):
        self.clock.seconds=59;original=self.opener.open
        def slow(request,**kwargs):
            response=original(request,**kwargs);self.clock.seconds+=2;return response
        self.opener.open=slow
        root=self.parent/'campaign';root.mkdir(mode=0o700)
        config=campaign_config(root,self.plan,['900001'],60,90000,86400,2,now=self.clock.wall(),business_hours=self.brief_rules())
        def sleep(seconds):
            if 61<=self.clock.seconds<86400:self.clock.seconds=86400
            else:self.clock.sleep(seconds)
        with RemoteCampaign(config=config,now=self.clock.wall()) as task:
            result=task.collect(wall_clock=self.clock.wall,monotonic_clock=self.clock.mono,sleep=sleep,
                emit=lambda _:None,client_factory=lambda:self.client)
        self.assertEqual(result['end_reason'],'budget');self.assertEqual(result['scheduled_pause_slots'],1)
        self.assertEqual(result['successful_pairs'],1);self.assertEqual(result['recorded_http_attempts'],3)
        self.assertEqual(result['completed_pair_slots'],2)
        self.assertEqual(remote_campaign_status(root)['deadline_at'],result['deadline_at'])
        self.assertEqual([t for url,t in self.opener.calls if 'groupqueues?' in url],[59,86400])

if __name__=='__main__':unittest.main()
