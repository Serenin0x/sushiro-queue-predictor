import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from test_statistics_gateway import gateway, Hub
from test_display_replay import archive
from sushiwait.analyticsai import read_state, write_state

class View:
    names={'3014':'合成门店'}
    def dispatch(self,path):
        if '/months/' in path:return 200,json.dumps({'source':'sapi_miniapp_gateway','days':{'2026-10-13':{'3014':{}}}}).encode(),'application/json'
        d=archive();d['requested_store_id']='3014';return 200,json.dumps(d).encode(),'application/json'

class AnalyticsGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.root.chmod(0o700)
        self.app=gateway.StatisticsGateway(Hub(),official_view=View(),model_advice_file=self.root/'public-advice.json',research_file=self.root/'tickets.sqlite')
        self.selection={'from':'2026-10-13','to':'2026-10-13','source':'sapi_miniapp_gateway','queue':'ordinary','store_id':''}
    def request(self,value,*,origin='https://124.222.57.236',scheme='https'):
        async def run():
            messages=[]
            async def receive():return {'type':'http.request','body':json.dumps({'schema_version':1,**value}).encode()}
            async def send(msg):messages.append(msg)
            await self.app({'type':'http','method':'POST','path':'/api/v1/analytics','scheme':scheme,'headers':[(b'origin',origin.encode()),(b'content-type',b'application/json')],'query_string':b''},receive,send)
            return messages[0]['status'],json.loads(messages[1]['body'])
        return asyncio.run(run())
    def test_report_and_model_queue_only_receive_server_derived_public_metrics(self):
        code,report=self.request({'action':'report','selection':self.selection});self.assertEqual(code,200)
        self.assertEqual(report['stores'][0]['observations'],20)
        code,result=self.request({'action':'explain','request_id':'c'*32,'selection':self.selection,'report_id':report['report_id'],'visible':True});self.assertEqual(code,200)
        job=read_state(self.root/'analytics-job.json');self.assertEqual(job['context']['scope'],'public_aggregates_only')
        self.assertNotIn('issued_at',json.dumps(job));self.assertNotIn('owner_secret',json.dumps(job))
        self.assertEqual(self.request({'action':'explain','request_id':'c'*32,'selection':self.selection,'report_id':'z'*64,'visible':True})[0],400)
        self.assertEqual(self.request({'action':'explain','request_id':'c'*32,'selection':self.selection,'report_id':report['report_id'],'visible':False})[1]['state'],'released')
    def test_cross_origin_http_and_user_prompt_rejected(self):
        for kwargs in ({'origin':'https://foreign.example'},{'scheme':'http'}):self.assertEqual(self.request({'action':'report','selection':self.selection},**kwargs)[0],400)
        self.assertEqual(self.request({'action':'report','selection':self.selection,'prompt':'send personal data'})[0],400)
    def test_new_route_and_assets_are_allowed_without_reopening_management(self):
        for path in ('/analytics-ui.js','/analytics.css','/analytics-advice.json'):self.assertTrue(self.app.allowed(path))
        self.assertTrue(self.app.supports_query('/statistics-legacy',b'section=intelligence&date=2026-10-16'))
        self.assertFalse(self.app.allowed('/analytics-job.json'))

    def test_new_intent_after_completion_and_old_cancellation_cannot_replace_it(self):
        _,report=self.request({'action':'report','selection':self.selection})
        value={'action':'explain','selection':self.selection,'report_id':report['report_id'],'visible':True,'request_id':'1'*32}
        self.assertEqual(self.request(value)[1]['state'],'queued')
        self.assertEqual(self.request({**value,'request_id':'2'*32})[1]['state'],'busy')
        write_state(self.root/'analytics-result.json',{'request_id':'1'*32,'state':'ready'})
        self.assertEqual(self.request({**value,'request_id':'2'*32})[1]['state'],'queued')
        self.request({**value,'visible':False})
        self.assertEqual(read_state(self.root/'analytics-job.json')['request_id'],'2'*32)
    def test_fresh_request_reads_archive_again(self):
        from unittest.mock import patch
        with patch.object(self.app.official_view,'dispatch',wraps=self.app.official_view.dispatch) as read:
            self.request({'action':'report','selection':self.selection});n=read.call_count
            self.request({'action':'report','selection':self.selection});self.assertEqual(read.call_count,n)
            self.request({'action':'report','selection':self.selection,'fresh':True});self.assertGreater(read.call_count,n)

    def test_health_safe_reasons_and_closed_during_business_is_abnormal(self):
        at=datetime(2026,10,16,4,tzinfo=timezone.utc)
        path=self.root/'health.json';self.app.collection_status_file=path
        write_state(path,{'state':'closed','at':at.isoformat(),'error_code':'SECRET_DIAGNOSTIC'})
        value=self.app.collection_health(now=at)
        self.assertEqual(value['reason_code'],'collection_not_started')
        self.assertEqual(value['state'],'needs_attention');self.assertNotIn('SECRET',json.dumps(value))
        write_state(path,{'state':'needs_attention','at':at.isoformat(),'error_code':'cloud_refresh_failed'})
        self.assertEqual(self.app.collection_health(now=at)['reason_code'],'cloud_refresh_failed')
