import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_statistics_gateway import Hub,gateway
from sushiwait.screenshot import MAX_BYTES

class ScreenshotGatewayTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name);root.chmod(0o700)
  self.app=gateway.StatisticsGateway(Hub(),research_file=root/'study.sqlite',ocr_enabled=True)
 def request(self,raw=b'synthetic',mime=b'image/png',scheme='https',origin=b'https://124.222.57.236'):
  async def run():
   messages=[]
   async def send(v):messages.append(v)
   async def receive():return {'type':'http.request','body':raw}
   await self.app({'type':'http','scheme':scheme,'path':'/api/v1/research/screenshots/recognize','raw_path':b'/api/v1/research/screenshots/recognize','method':'POST','query_string':b'','client':('synthetic',0),'headers':[(b'origin',origin),(b'content-type',mime)]},receive,send)
   return messages
  return asyncio.run(run())
 def test_secure_pixels_return_fields_without_saving_or_external_calls(self):
  value={'schema_version':1,'fields':{'store_id':'900001','number':'321','issued_at':'2026-09-08T04:45:19Z'},'auto_accept':True}
  with patch('sushiwait.screenshot.recognize',return_value=value) as recognized:
   response=self.request();self.assertEqual(response[0]['status'],200);self.assertEqual(json.loads(response[1]['body']),value);self.assertEqual(recognized.call_args.args[0],b'synthetic')
  self.assertFalse(self.app.research_file.exists(),'recognizing must not silently create a study ticket')
 def test_wrong_scheme_origin_type_and_large_body_rejected_before_engine(self):
  with patch('sushiwait.screenshot.recognize') as recognized:
   for opts in ({'scheme':'http'},{'origin':b'https://example.com'},{'mime':b'image/svg+xml'},{'raw':b'x'*(MAX_BYTES+1)}):
    self.assertEqual(self.request(**opts)[0]['status'],400)
   recognized.assert_not_called()
 def test_request_budget_bounds_cpu_work(self):
  with patch('sushiwait.screenshot.recognize',return_value={'schema_version':1}):
   for _ in range(20):self.assertEqual(self.request()[0]['status'],200)
   self.assertEqual(self.request()[0]['status'],429)
