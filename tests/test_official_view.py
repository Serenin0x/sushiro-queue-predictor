"""Durable archive / read-only gateway integration, entirely synthetic."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sushiwait.observations import normalize_snapshot
from sushiwait.officialview import OfficialStatisticsView, OfficialViewError, read_config
from sushiwait.packets import build_packet
from sushiwait.receipts import PacketArchive
import test_statistics_gateway as gateway_tests

gateway = gateway_tests.gateway
Hub = gateway_tests.Hub

ROOT = Path(__file__).resolve().parents[1]
HOURS = json.loads((ROOT/'config/default-business-hours.json').read_bytes())
AT = '2026-10-10T12:00:00Z'
NOW = datetime.fromisoformat(AT.replace('Z','+00:00'))
MONTH = '/api/v1/official/months/2026-10'
DAY = '/api/v1/official/stores/900001/days/2026-10-10'


def make_packet(rows, *, store='900001', profile='miniapp_gateway', origin='synthetic'):
    result = []
    for identity, started, received in rows:
        snapshot = normalize_snapshot({'id':int(store),'name':'Synthetic store',
            'groupQueues':{'mixedQueue':[str(identity),str(identity+2),str(identity+4),'9999'],
                           'reservationQueue':[]}}, store,
            request_started_at=started,received_at=received,elapsed_ms=0,
            data_origin=origin,api_profile=profile)
        result.append({'id':identity,'run_id':'764c40ca-8f27-41b7-8026-28d046d526cb',
            'store_id':store,'api_profile':profile,'data_origin':origin,
            'received_at':received,'ok':1,'payload_json':json.dumps(snapshot)})
    return build_packet(result,as_of=AT,store_ids=[store],api_profile=profile,data_origin=origin)


class OfficialViewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.root.chmod(0o700);self.db=self.root/'public.sqlite3'
        self.first=make_packet([(1,'2026-10-10T03:00:00Z','2026-10-10T03:00:00.1Z')])
        self.append(self.first)
        self.view=self.create()

    def tearDown(self):
        self.temp.cleanup()

    def append(self, packet):
        with PacketArchive(self.db) as archive:
            return archive.append(packet,as_of=AT)

    def create(self, **changes):
        return OfficialStatisticsView(self.db, **{'store_names':{'900001':'Synthetic store'},
            'data_origin':'synthetic','hours':HOURS,**changes})

    def read(self, path=DAY):
        with patch('socket.socket',side_effect=AssertionError('no network')) as sockets, \
                patch('sushiwait.officialview._read_private_file',side_effect=AssertionError('no credentials')) as private:
            value=self.view.read(path,now=NOW)
            self.assertEqual(sockets.call_count+private.call_count,0)
            return value

    def test_durable_incremental_batches_and_idempotent_duplicates(self):
        value=self.read();self.assertEqual(value['summary']['observations'],1)
        receipt=self.append(self.first);self.assertEqual(receipt['duplicate_records'],1)
        self.append(make_packet([(2,'2026-10-10T03:01:00Z','2026-10-10T03:01:00.1Z')]))
        restarted=self.create().read(DAY,now=NOW)
        self.assertEqual(restarted['summary']['observations'],2)
        self.assertEqual(restarted['summary']['recorded_http_attempts'],2)
        self.assertEqual(restarted['summary']['observed_background_slots'],2)
        self.assertEqual(restarted['points'][1]['queues']['mixedQueue'],['2','4','6'])
        self.assertEqual(restarted['points'][1]['display_sizes']['mixedQueue'],4)
        self.assertTrue(restarted['full_source_arrays_persisted'])
        self.assertEqual(restarted['collection_state'],'not_proven_by_archive')
        self.assertFalse(restarted['network_performed_by_read'] or restarted['eta_available'])
        self.assertIsNone(restarted['points'][1]['interval_seconds'])

    def test_http_reads_leave_database_bytes_and_files_unchanged(self):
        before=hashlib.sha256(self.db.read_bytes()).hexdigest();files=set(self.root.iterdir())
        self.read(MONTH);self.read(DAY)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(),before)
        self.assertEqual(set(self.root.iterdir()),files)

    def test_empty_scope_is_unknown_without_zero_wait(self):
        day=self.read('/api/v1/official/stores/900001/days/2026-10-09')
        self.assertIsNone(day['summary']);self.assertEqual(day['points'],[])
        self.assertFalse(day['full_source_arrays_persisted'])
        month=self.read('/api/v1/official/months/2026-09')
        self.assertEqual(month['days'],{});self.assertEqual(month['configured_store_ids'],['900001'])

    def test_scope_isolates_legacy_other_origin_and_other_store(self):
        rows=[(2,'2026-10-10T03:01:00Z','2026-10-10T03:01:00Z')]
        for change in ({'profile':'legacy'},{'origin':'fixture'},{'store':'900002'}):
            self.append(make_packet(rows,**change))
        self.assertEqual(self.read()['summary']['observations'],1)
        self.assertFalse(self.view.allowed('/api/v1/official/stores/900002/days/2026-10-10'))

    def test_china_date_uses_request_start_and_keeps_subsecond_midnight(self):
        self.append(make_packet([(2,'2026-10-09T15:59:59.9Z','2026-10-09T16:00:00.1Z'),
            (3,'2026-10-09T16:00:00.000001Z','2026-10-09T16:00:00.1Z')]))
        month=self.read(MONTH)
        self.assertEqual(month['days']['2026-10-09']['900001']['observations'],1)
        self.assertEqual(month['days']['2026-10-10']['900001']['observations'],2)
        self.assertEqual(self.read()['points'][0]['queues']['mixedQueue'][0],'3')

    def test_archive_corruption_returns_503_without_old_success_or_fallback(self):
        self.read()
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE archive_records SET record_sha256=?",['0'*64])
        status,body,_=self.view.dispatch(DAY)
        self.assertEqual(status,503);value=json.loads(body)
        self.assertEqual(value['source'],'sapi_miniapp_gateway')
        self.assertNotIn('points',value);self.assertFalse(value['network_performed_by_read'])

    def test_missing_permissions_symlink_and_wrong_schema_are_rejected(self):
        missing=self.root/'missing.sqlite3';view=OfficialStatisticsView(missing,
            store_names=self.view.names,data_origin='synthetic',hours=HOURS)
        self.assertEqual(view.dispatch(DAY)[0],503);self.assertFalse(missing.exists())
        self.db.chmod(0o644);self.assertEqual(self.view.dispatch(DAY)[0],503);self.db.chmod(0o600)
        link=self.root/'linked.sqlite3';link.symlink_to(self.db)
        view=OfficialStatisticsView(link,store_names=self.view.names,data_origin='synthetic',hours=HOURS)
        self.assertEqual(view.dispatch(DAY)[0],503)
        with sqlite3.connect(self.db) as db:db.execute('PRAGMA user_version=2')
        self.assertEqual(self.view.dispatch(DAY)[0],503)

    def test_database_replacement_during_read_fails_closed(self):
        original=self.view._day
        def replace(*args):
            result=original(*args)
            saved=self.root/'old.sqlite3';self.db.rename(saved)
            self.db.write_bytes(saved.read_bytes());self.db.chmod(0o600)
            return result
        with patch.object(self.view,'_day',side_effect=replace):
            with self.assertRaisesRegex(OfficialViewError,'official_view_database_changed'):
                self.read()

    def test_selected_day_over_1000_is_explicit_error_and_never_silent_truncation(self):
        rows=[(i,'2026-10-10T03:01:00Z','2026-10-10T03:01:00Z') for i in range(2,1002)]
        self.append(make_packet(rows))
        status,body,_=self.view.dispatch(DAY)
        self.assertEqual(status,503)
        self.assertEqual(json.loads(body)['error_code'],'official_view_day_exceeds_bound')
        self.assertEqual(self.view.dispatch(MONTH)[0],503)

    def test_config_and_routes_are_explicit_not_auto_source_selection(self):
        config={'schema_version':1,'mode':'official_packet_archive_readonly',
            'database_file':str(self.db),'store_names':self.view.names,
            'data_origin':'synthetic','hours':HOURS,'base_interval':60}
        path=self.root/'view.json';path.write_text(json.dumps(config));path.chmod(0o600)
        self.assertEqual(read_config(path).read(DAY,now=NOW)['summary']['observations'],1)
        config['authorization']='not-a-credential';path.write_text(json.dumps(config))
        with self.assertRaisesRegex(OfficialViewError,'official_view_invalid_config'):read_config(path)
        for route in ['/api/v1/months/2026-10','/api/v1/official/months/2026-13',
                '/api/v1/official/stores/900001/days/2026-02-30',DAY+'?refresh=1',
                '/api/v1/official/status','/api/v1/official/packet-archive']:
            self.assertFalse(self.view.allowed(route));self.assertEqual(self.view.dispatch(route)[0],404)
        for changes in ({'store_names':{}},{'base_interval':True},{'data_origin':'auto'}):
            with self.assertRaises(OfficialViewError):self.create(**changes)

    def test_browser_accepts_durable_month_day_and_empty_day_contracts(self):
        data=json.dumps({'month':self.read(MONTH),'day':self.read(),
            'empty':self.read('/api/v1/official/stores/900001/days/2026-10-09')})
        script="""
import {validateMonth,validateDay} from './src/sushiwait/web/statistics-client.mjs';
let input='';for await(const chunk of process.stdin)input+=chunk;
const {month,day,empty}=JSON.parse(input);const source='sapi_miniapp_gateway';
validateMonth(month,'2026-10',{source});validateDay(day,'900001','2026-10-10',{source});
validateDay(empty,'900001','2026-10-09',{source});
"""
        result=subprocess.run([shutil.which('node'),'--input-type=module','-e',script],
            cwd=ROOT,input=data,text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)


class OfficialGatewayTests(unittest.TestCase):
    request = gateway_tests.StatisticsGatewayTests.request

    def setUp(self):
        self.hub=Hub()
        self.view=unittest.mock.Mock()
        self.view.allowed.side_effect=lambda path:path==DAY or path==MONTH
        self.view.dispatch.return_value=(200,b'{"source":"sapi_miniapp_gateway"}','application/json')
        self.app=gateway.StatisticsGateway(self.hub,official_view=self.view)

    def test_disabled_official_route_is_404_and_never_uses_crm(self):
        self.app=gateway.StatisticsGateway(self.hub)
        self.assertEqual(self.request(DAY)[0]['status'],404)
        self.assertEqual(self.hub.calls,[]);self.view.dispatch.assert_not_called()

    def test_explicit_official_routes_are_independent_and_fail_without_fallback(self):
        self.assertEqual(self.request(DAY)[0]['status'],200)
        self.assertEqual(self.request(MONTH)[0]['status'],200)
        self.assertEqual(self.hub.calls,[])
        self.app.cache.clear();self.view.dispatch.return_value=(503,b'{"error_code":"archive_unavailable"}','application/json')
        self.assertEqual(self.request(DAY)[0]['status'],503)
        self.assertEqual(self.hub.calls,[])

    def test_shared_gateway_deadline_and_read_only_boundaries_are_preserved(self):
        for method in ('POST','DELETE','PUT'):
            self.assertEqual(self.request(DAY,method=method)[0]['status'],405)
        self.assertEqual(self.request(DAY,query=b'update=1')[0]['status'],400)
        self.assertEqual(self.request(DAY,raw=DAY.encode().replace(b'900001',b'%39%30%30%30%30%31'))[0]['status'],400)
        self.app.deadline=NOW
        self.assertEqual(self.request(DAY)[0]['status'],503)
        self.view.dispatch.assert_not_called();self.assertEqual(self.hub.calls,[])
