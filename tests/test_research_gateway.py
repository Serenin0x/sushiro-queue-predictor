import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_statistics_gateway import Hub, gateway


class ResearchGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.root.chmod(0o700)
        self.app=gateway.StatisticsGateway(Hub(),research_file=self.root/'study.sqlite')
    def request(self,path,value=None,*,origin=b'https://124.222.57.236',method='POST',raw=None,scheme='https'):
        async def run():
            messages=[]
            async def send(v):messages.append(v)
            async def receive():return {'type':'http.request','body':raw if raw is not None else json.dumps(value).encode()}
            await self.app({'type':'http','scheme':scheme,'path':path,'raw_path':path.encode(),'query_string':b'',
                'method':method,'client':('synthetic',0),'headers':[(b'origin',origin),(b'content-type',b'application/json')]},receive,send)
            return messages
        return asyncio.run(run())
    def submission(self):
        return {'schema_version':1,'action':'submit','record_id':'a'*32,'owner_secret':'b'*64,
            'ticket':{'store_id':'3014','queue_type':'ordinary','number':'4045','issued_at':'2026-10-13T03:00:00Z',
                      'checked_in':False,'entry_mode':'retrospective','prediction_reason':'insufficient_sparse_trend'}}
    def test_unavailable_prediction_saved_private_not_public_get(self):
        v=self.submission();self.assertEqual(self.request('/api/v1/research/tickets',v)[0]['status'],200)
        result=self.request('/api/v1/research/tickets',{'schema_version':1,'action':'list','owner_secret':'b'*64})
        self.assertEqual(json.loads(result[1]['body'])['records'][0]['reported']['number'],'4045')
        other=self.request('/api/v1/research/tickets',{'schema_version':1,'action':'list','owner_secret':'c'*64})
        self.assertEqual(json.loads(other[1]['body'])['records'],[])
        self.assertEqual(self.request('/api/v1/research/tickets',method='GET')[0]['status'],404)
        public=self.request('/wait-reference.json',method='GET')
        self.assertEqual(public[0]['status'],200);self.assertNotIn(b'4045',public[1]['body'])
    def test_origin_size_duplicate_keys_and_credentials_rejected(self):
        for raw in (b'x'*4097,b'{"schema_version":1,"schema_version":1}'):
            self.assertEqual(self.request('/api/v1/research/tickets',raw=raw)[0]['status'],400)
        self.assertEqual(self.request('/api/v1/research/tickets',self.submission(),origin=b'https://example.com')[0]['status'],400)
        v=self.submission();v['api_key']='synthetic';self.assertEqual(self.request('/api/v1/research/tickets',v)[0]['status'],400)
    def test_plaintext_scheme_even_with_forged_https_origin_rejected(self):
        self.assertEqual(self.request('/api/v1/research/tickets',self.submission(),scheme='http')[0]['status'],400)

    def test_plaintext_private_origin_rejected(self):
        self.assertEqual(self.request('/api/v1/research/tickets',self.submission(),origin=b'http://124.222.57.236:18080')[0]['status'],400)

    def test_viewing_lease_has_no_ticket_or_paid_request(self):
        v={'schema_version':1,'client_id':'a'*32,'store_id':'3014','queue_type':'ordinary','mode':'thinking','visible':True}
        self.assertEqual(self.request('/api/v1/prediction/presence',v)[0]['status'],200)
        v['number']='4045';self.assertEqual(self.request('/api/v1/prediction/presence',v)[0]['status'],400)

    def test_delete_restore_and_wrong_owner_through_private_post_only(self):
        self.request('/api/v1/research/tickets',self.submission())
        action={'schema_version':1,'action':'delete','record_id':'a'*32,'owner_secret':'b'*64}
        self.assertEqual(self.request('/api/v1/research/tickets',action,origin=b'https://example.com')[0]['status'],400)
        other=self.request('/api/v1/research/tickets',{**action,'owner_secret':'c'*64})
        self.assertFalse(json.loads(other[1]['body'])['found'])
        deleted=self.request('/api/v1/research/tickets',action)
        self.assertTrue(json.loads(deleted[1]['body'])['deleted'])
        records=self.request('/api/v1/research/tickets',{'schema_version':1,'action':'list','owner_secret':'b'*64})
        self.assertEqual(json.loads(records[1]['body'])['total'],0)
        self.assertEqual(self.request('/api/v1/research/tickets',action,scheme='http')[0]['status'],400)
        self.assertEqual(self.request('/api/v1/research/tickets',{**action,'action':'restore'})[0]['status'],200)
        records=self.request('/api/v1/research/tickets',{'schema_version':1,'action':'list','owner_secret':'b'*64})
        self.assertEqual(json.loads(records[1]['body'])['total'],1)

    def test_forecast_baseline_uses_private_owner_post_and_saved_observations(self):
        from test_ticket_timing import TicketTimingTests
        fixture=TicketTimingTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        class View:
            def dispatch(self,path):return 200,json.dumps(fixture.detail()).encode(),'application/json'
        self.app.official_view=View()
        submit={**self.submission(),'ticket':fixture.ticket}
        action={'schema_version':1,'action':'forecast','record_id':'a'*32,'owner_secret':'b'*64,'forecast':fixture.forecast}
        with patch('sushiwait.ticketresearch.clock',return_value=fixture.now):
            self.assertEqual(self.request('/api/v1/research/tickets',submit)[0]['status'],200)
            response=self.request('/api/v1/research/tickets',action)
            self.assertTrue(json.loads(response[1]['body'])['accepted'])
            wrong=self.request('/api/v1/research/tickets',{**action,'owner_secret':'c'*64})
            self.assertFalse(json.loads(wrong[1]['body'])['found'])
            rows=self.request('/api/v1/research/tickets',{'schema_version':1,'action':'list','owner_secret':'b'*64})
            self.assertIn('algorithm',json.loads(rows[1]['body'])['records'][0]['reported']['first_forecasts'])
        self.assertEqual(self.request('/api/v1/research/tickets',action,scheme='http')[0]['status'],400)
        self.assertEqual(self.request('/api/v1/research/tickets',action,origin=b'https://example.com')[0]['status'],400)
