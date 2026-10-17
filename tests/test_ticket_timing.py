from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from sushiwait.displayarrival import label_ticket_arrival, ticket_timing
from sushiwait.ticketresearch import TicketResearch
from test_display_replay import archive


class TicketTimingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.root.chmod(0o700)
        self.store=TicketResearch(self.root/'tickets.sqlite');self.addCleanup(self.store.close)
        self.now=datetime(2026,10,14,3,5,tzinfo=timezone.utc)
        self.ticket={'store_id':'3014','queue_type':'ordinary','number':'150','issued_at':'2026-10-14T03:00:00Z',
            'checked_in':False,'entry_mode':'live','prediction_reason':None,'record_kind':'prediction','prediction_mode':'thinking'}
        self.identity='1'*32;self.secret='a'*64
        self.forecast={'mode':'algorithm','requested_mode':'thinking','computed_at':'2026-10-14T03:01:00Z',
            'sample_at':'2026-10-14T03:00:00Z','reference_time':'2026-10-14T03:40:00Z',
            'scenario_earliest':'2026-10-14T03:30:00Z','scenario_latest':'2026-10-14T03:50:00Z','policy':'display_arrival_history_live_v6'}
    def detail(self,values=(100,110)):
        d=archive(values);d['requested_store_id']='3014';d['local_date']='2026-10-14'
        for p in d['points']:
            for key in ('queue_received_at','request_started_at'):p[key]=p[key].replace('2026-10-13','2026-10-14')
            if p['queues']['mixedQueue']:
                n=int(p['queues']['mixedQueue'][0]);p['queues']['mixedQueue']=[str(n),str(n+1),str(n+2)]
        d['generated_at']=d['generated_at'].replace('2026-10-13','2026-10-14')
        return d
    def submit(self,ticket=None):self.store.submit(self.identity,self.secret,ticket or self.ticket,now=self.now)
    def capture(self,forecast=None,**kwargs):
        return self.store.capture_forecast(self.identity,self.secret,forecast or self.forecast,
            read_day=lambda *_:self.detail(),now=self.now,**kwargs)
    def test_first_estimates_frozen_per_mode_and_retained_after_arrival(self):
        self.submit();self.assertTrue(self.capture()['accepted'])
        thinking={**self.forecast,'mode':'thinking','reference_time':'2026-10-14T03:30:00Z'}
        self.assertTrue(self.capture(thinking)['accepted'])
        later={**thinking,'reference_time':'2026-10-14T03:45:00Z'}
        self.assertTrue(self.capture(later)['duplicate'])
        self.store.update_labels(lambda *_:self.detail((100,110,150)),now=self.now+timedelta(minutes=6))
        row=self.store.own_records(self.secret)[0]
        self.assertEqual(row['timing']['status'],'observed')
        self.assertEqual(row['timing']['wait_minutes'],{'lower':10,'upper':10})
        self.assertEqual(row['timing']['prediction_error_minutes'],{'lower':20,'upper':20})
        self.assertEqual(row['reported']['first_forecasts']['thinking']['reference_time'],thinking['reference_time'])
        other=TicketResearch(self.store.path)
        try:self.assertEqual(other.own_records(self.secret)[0]['timing'],row['timing'])
        finally:other.close()
    def test_wrong_owner_deleted_and_record_only_cannot_save_forecast(self):
        self.submit()
        result=self.store.capture_forecast(self.identity,'b'*64,self.forecast,
            read_day=lambda *_:self.fail('another owner must not trigger archive reads'),now=self.now)
        self.assertFalse(result['found'])
        self.store.set_deleted(self.identity,self.secret,now=self.now)
        self.assertFalse(self.capture()['found'])
        self.store.set_deleted(self.identity,self.secret,deleted=False,now=self.now)
        self.store.submit('2'*32,self.secret,{**self.ticket,'record_kind':'record','entry_mode':'retrospective'},now=self.now)
        result=self.store.capture_forecast('2'*32,self.secret,self.forecast,now=self.now)
        self.assertEqual(result['reason'],'record_only')
    def test_no_hindsight_baseline_after_outcome_or_closed_day(self):
        self.submit();self.store.update_labels(lambda *_:self.detail((150,160)),now=self.now)
        self.assertEqual(self.capture()['reason'],'outcome_already_known')
        self.assertNotIn('first_forecasts',self.store.own_records(self.secret)[0]['reported'])
        self.store.submit('2'*32,self.secret,self.ticket,now=self.now)
        result=self.store.capture_forecast('2'*32,self.secret,self.forecast,now=self.now+timedelta(days=1))
        self.assertEqual(result['reason'],'prediction_closed')
    def test_future_invalid_and_stale_baseline_is_not_admitted(self):
        self.submit()
        for changes in ({'computed_at':'2026-10-14T03:06:00Z'}, {'sample_at':'2026-10-14T02:00:00Z'},
                        {'reference_time':'2026-10-14T10:00:00Z'},{'api_key':'synthetic'}, {'mode':'auto'}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):self.capture({**self.forecast,**changes})
    def test_unavailable_archive_does_not_freeze_an_unverified_baseline(self):
        self.submit()
        def missing(*_):raise ValueError('synthetic archive unavailable')
        result=self.store.capture_forecast(self.identity,self.secret,self.forecast,read_day=missing,now=self.now)
        self.assertTrue(result['retryable']);self.assertFalse(result['accepted'])
        self.assertNotIn('first_forecasts',self.store.own_records(self.secret)[0]['reported'])
    def test_inferred_interval_error_is_an_interval(self):
        reported={**self.ticket,'first_forecasts':{'thinking':{**self.forecast,'mode':'thinking','reference_time':'2026-10-14T03:08:00Z'}}}
        d=self.detail((140,145,160));label=label_ticket_arrival(d,store_id='3014',number='150',issued_at=self.ticket['issued_at'])
        timing=ticket_timing(label,reported,now=self.now)
        self.assertEqual(timing['status'],'inferred')
        self.assertEqual(timing['prediction_error_minutes'],{'lower':-2,'upper':3})
    def test_gap_crossing_has_no_call_time_wait_or_error(self):
        for values in ((140,None,160),(160,)):
            d=self.detail(values);label=label_ticket_arrival(d,store_id='3014',number='150',issued_at=self.ticket['issued_at'])
            timing=ticket_timing(label,{**self.ticket,'first_forecasts':{'thinking':self.forecast}},now=self.now)
            self.assertTrue(label['result_available']);self.assertEqual(timing['status'],'missing')
            self.assertIsNone(timing['call_interval']);self.assertIsNone(timing['wait_minutes']);self.assertIsNone(timing['prediction_error_minutes'])
    def test_past_gap_does_not_erase_later_continuous_bracket(self):
        d=self.detail((100,None,140,160));label=label_ticket_arrival(d,store_id='3014',number='150',issued_at=self.ticket['issued_at'])
        self.assertEqual(ticket_timing(label,self.ticket,now=self.now)['status'],'inferred')
    def test_issue_between_healthy_samples_has_a_valid_first_poll_interval(self):
        d=self.detail((140,160));issued='2026-10-14T03:02:00Z'
        label=label_ticket_arrival(d,store_id='3014',number='150',issued_at=issued)
        self.assertEqual(label['arrival_interval']['basis'],'issue_to_first_sample')
        timing=ticket_timing(label,{**self.ticket,'issued_at':issued},now=self.now)
        self.assertEqual(timing['status'],'inferred');self.assertEqual(timing['wait_minutes'],{'lower':0,'upper':3})
    def test_closed_missing_and_record_only_have_no_prediction_error(self):
        label=label_ticket_arrival(self.detail(),store_id='3014',number='150',issued_at=self.ticket['issued_at'])
        self.assertEqual(ticket_timing(label,self.ticket,now=self.now)['status'],'pending')
        self.assertEqual(ticket_timing(label,self.ticket,now=self.now+timedelta(days=1))['status'],'missing')
        direct=label_ticket_arrival(self.detail((150,)),store_id='3014',number='150',issued_at=self.ticket['issued_at'])
        timing=ticket_timing(direct,{**self.ticket,'record_kind':'record','first_forecasts':{'thinking':self.forecast}},now=self.now)
        self.assertEqual(timing['status'],'observed');self.assertIsNone(timing['prediction_error_minutes'])
