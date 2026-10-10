"""The new display layer uses bounded public routes and packaged resources."""
from pathlib import Path
import re
import shutil
import subprocess
import unittest
from unittest.mock import patch
import test_monitor_hub
from test_statistics_gateway import gateway

ROOT=Path(__file__).resolve().parents[1]

class CalendarFrontendTests(unittest.TestCase):
    def setUp(self):
        self.hub=test_monitor_hub.MonitorHub(test_monitor_hub.config(),now=test_monitor_hub.NOW,
            reader=lambda *_: self.fail('a static asset must not read a worker'))
        self.hub.config['schema_version']=2
        self.app=gateway.StatisticsGateway(self.hub)

    def test_new_entry_all_modules_and_legacy_are_explicit_local_assets(self):
        entry=self.hub.asset('/statistics')[0].decode()
        self.assertIn('src="/calendar.mjs"',entry)
        self.assertIn('href="/statistics-legacy"',entry)
        paths=['/statistics','/statistics-legacy','/calendar.css','/calendar.mjs','/calendar-data.mjs',
               '/calendar-directory.mjs','/calendar-icons.mjs','/statistics-client.mjs']
        for path in paths:
            self.assertTrue(self.app.allowed(path),path)
            code,body,mime=self.hub.dispatch(path,now=test_monitor_hub.NOW)
            self.assertEqual(code,200,path)
            self.assertGreater(len(body),0)
            if path.endswith('.mjs'):self.assertEqual(mime,'text/javascript; charset=utf-8')
        for path in ['/calendar/secret','/calendar.mjs.map','/api/v1/manage','/config.json','/web/calendar.mjs']:
            self.assertFalse(self.app.allowed(path),path)
        self.assertNotIn('<script>',entry)
        self.assertNotIn('<style>',entry)
        self.assertNotIn('即将叫号列表 · 12:30',entry)

    def test_public_gateway_serves_the_entry_and_sanitizes_the_legacy_link(self):
        import asyncio
        async def request(path):
            replies=[]
            async def send(event):replies.append(event)
            async def receive():return {'type':'http.request','body':b''}
            await self.app({'type':'http','method':'GET','path':path,'raw_path':path.encode(),'query_string':b''},receive,send)
            return replies
        for path in ['/statistics','/statistics-legacy','/calendar.mjs','/statistics-client.mjs']:
            replies=asyncio.run(request(path))
            self.assertEqual(replies[0]['status'],200)
            headers=dict(replies[0]['headers'])
            self.assertIn(b"script-src 'self'",headers[b'content-security-policy'])
            self.assertIn(b"connect-src 'self'",headers[b'content-security-policy'])
            self.assertNotIn(b'/monitor',replies[1]['body'])
            if path.endswith('.mjs'):self.assertTrue(headers[b'content-type'].startswith(b'text/javascript'))

    def test_controller_copy_matches_reviewed_integration_package(self):
        self.assertEqual((ROOT/'src/sushiwait/web/statistics-client.mjs').read_bytes(),
            (ROOT/'docs/design/integration/statistics-client.mjs').read_bytes())
        source=(ROOT/'src/sushiwait/web/calendar.mjs').read_text()
        for old in ['window.openai','Tweak','buildSamples','queueArray','storeDayData','北京时间','cdn.jsdelivr']:
            self.assertNotIn(old,source)

    def test_view_data_semantics(self):
        node=shutil.which('node')
        self.assertIsNotNone(node)
        result=subprocess.run([node,str(ROOT/'tests/calendar-data.test.mjs')],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('11 calendar integration cases passed',result.stdout)

    def test_mainland_city_map_covers_the_known_catalog_only(self):
        import json
        catalog=json.loads((ROOT/'config/mainland-store-catalog.json').read_text())
        city_map=(ROOT/'src/sushiwait/web/calendar-directory.mjs').read_text().split('export const cityById = ')[1].split(';')[0]
        city_map=json.loads(city_map)
        self.assertEqual(set(city_map),{str(row['store_id']) for row in catalog['stores']})
        self.assertEqual(len(set(city_map.values())),31)
