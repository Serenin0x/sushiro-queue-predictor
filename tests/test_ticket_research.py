from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from sushiwait.ticketresearch import TicketResearch
from test_display_replay import archive


class TicketResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.root.chmod(0o700)
        self.store=TicketResearch(self.root/'research.sqlite');self.addCleanup(self.store.close)
        self.now=datetime(2026,10,14,3,tzinfo=timezone.utc)
        self.ticket={'store_id':'3014','queue_type':'ordinary','number':'120','issued_at':'2026-10-13T03:00:00Z',
                     'checked_in':None,'entry_mode':'retrospective','prediction_reason':'insufficient_sparse_trend'}
        self.secret='a'*64

    def test_unavailable_prediction_still_saved_and_owner_scoped(self):
        result=self.store.submit('1'*32,self.secret,self.ticket,now=self.now)
        self.assertTrue(result['saved'])
        own=self.store.own_records(self.secret)
        self.assertEqual(own[0]['reported']['prediction_reason'],'insufficient_sparse_trend')
        self.assertEqual(self.store.own_records('b'*64),[])
        self.assertEqual(self.store.path.stat().st_mode&0o777,0o600)

    def test_duplicate_is_idempotent_but_other_owner_or_ticket_is_rejected(self):
        self.store.submit('1'*32,self.secret,self.ticket,now=self.now)
        self.assertTrue(self.store.submit('1'*32,self.secret,self.ticket,now=self.now)['duplicate'])
        with self.assertRaises(ValueError):self.store.submit('1'*32,'b'*64,self.ticket,now=self.now)
        with self.assertRaises(ValueError):self.store.submit('1'*32,self.secret,{**self.ticket,'number':'130'},now=self.now)
        self.assertEqual(len(self.store.own_records(self.secret)),1)

    def test_future_invalid_and_credential_fields_not_admitted(self):
        for changes in ({'issued_at':'2027-01-01T00:00:00Z'},{'number':'0'},{'store_id':'-1'},
                        {'checked_in':'yes'},{'api_key':'synthetic-key'}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                self.store.submit('1'*32,self.secret,{**self.ticket,**changes},now=self.now)

    def test_lease_expiry_hiding_and_algorithm_mode_stop_demand(self):
        value={'client_id':'2'*32,'store_id':'3014','queue_type':'ordinary','mode':'auto','visible':True}
        self.store.presence(value,now=self.now)
        self.assertEqual(self.store.demand(now=self.now),{'3014':{'ordinary':{'nonthinking'}}})
        self.assertEqual(self.store.demand(now=self.now+timedelta(seconds=91)),{})
        self.store.presence({**value,'mode':'thinking'},now=self.now)
        self.assertEqual(self.store.demand(now=self.now)['3014']['ordinary'],{'thinking'})
        self.store.presence({**value,'visible':False},now=self.now)
        self.assertEqual(self.store.demand(now=self.now),{})
        self.store.presence({**value,'mode':'algorithm'},now=self.now)
        self.assertEqual(self.store.demand(now=self.now),{})

    def test_retroactive_ticket_matches_saved_day_and_freezes_first_arrival(self):
        self.store.submit('1'*32,self.secret,self.ticket,now=self.now)
        detail=archive();detail['requested_store_id']='3014'
        self.assertEqual(self.store.update_labels(lambda *args:detail,now=self.now),1)
        first=self.store.own_records(self.secret)[0]['result']
        self.assertTrue(first['result_available']);self.assertEqual(first['wait_minutes'],{'lower':5,'upper':10})
        self.assertEqual(self.store.update_labels(lambda *args:self.fail('completed result immutable'),now=self.now),0)
        self.assertEqual(self.store.own_records(self.secret)[0]['result'],first)
        self.assertEqual(self.store.public_wait_summary(now=self.now)['stores'],{})

    def test_missing_historical_archive_remains_recorded(self):
        self.store.submit('1'*32,self.secret,{**self.ticket,'issued_at':'2020-01-01T00:00:00Z'},now=self.now)
        def missing(*args):raise FileNotFoundError
        self.store.update_labels(missing,now=self.now)
        self.assertEqual(self.store.own_records(self.secret)[0]['result']['state'],'archive_unavailable')

    def test_anonymous_summary_contains_no_ticket_owner_or_issue_time(self):
        import json
        for i in range(6):
            ticket={**self.ticket,'number':str(120+(i%3)*10),'issued_at':('2026-10-12' if i<3 else '2026-10-13')+'T03:00:00Z'}
            self.store.submit(f'{i:032x}',self.secret,ticket,now=self.now)
        def read(store,day):
            d=archive();d['requested_store_id']=store;d['local_date']=day
            for point in d['points']:
                for key in ('queue_received_at','request_started_at'):point[key]=point[key].replace('2026-10-13',day)
            d['generated_at']=d['generated_at'].replace('2026-10-13',day);return d
        self.store.update_labels(read,now=self.now)
        summary=self.store.public_wait_summary(now=self.now)
        self.assertEqual(summary['stores']['3014']['ordinary']['sample_count'],6)
        for private in ('issued_at','number','owner_hash',self.secret):self.assertNotIn(private,json.dumps(summary))
