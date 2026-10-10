"""Synthetic official-source projections; no real auth, upstream or UI calls."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from sushiwait.observations import normalize_snapshot
from sushiwait.officialstats import project_packet, OfficialStatisticsError, SOURCE
from sushiwait.packets import build_packet
from sushiwait.receipts import ReceiptError

ROOT = Path(__file__).resolve().parents[1]
HOURS = json.loads((ROOT/'config/default-business-hours.json').read_bytes())
AS_OF = '2026-10-10T12:00:00Z'


def packet(stamp='2026-10-10T03:00:00Z', *, payload=None, profile='miniapp_gateway'):
    payload = payload if payload is not None else {'id':900001,'name':'Synthetic official store',
        'groupQueuesCount':4,'groupQueues':{'mixedQueue':['0012','0014','0012','0016'],
        'reservationQueue':[],'counterQueue':[],'boothQueue':['0014']}}
    normalized = normalize_snapshot(payload, '900001', request_started_at=stamp,
        received_at=stamp, elapsed_ms=0, data_origin='synthetic', api_profile=profile)
    row = {'id':1,'run_id':'764c40ca-8f27-41b7-8026-28d046d526cb','store_id':'900001',
           'api_profile':profile,'data_origin':'synthetic','received_at':stamp,'ok':1,
           'payload_json':json.dumps(normalized)}
    return build_packet([row],as_of=AS_OF,store_ids=['900001'],api_profile=profile,data_origin='synthetic')


class OfficialStatisticsTests(unittest.TestCase):
    def project(self, value=None, **changes):
        with patch('socket.socket',side_effect=AssertionError('no upstream')) as sockets:
            result = project_packet(packet() if value is None else value,
                **{'as_of':AS_OF,'hours':HOURS,'store_names':{'900001':'Synthetic store'},**changes})
            self.assertEqual(sockets.call_count,0)
        return result

    def day(self, result=None):
        return (result or self.project())['days'][('900001','2026-10-10')]

    def test_single_get_semantics_and_partial_coverage(self):
        value=self.day();summary=value['summary']
        self.assertEqual(value['source'],SOURCE)
        self.assertEqual(summary['successful_pairs'],1)
        self.assertEqual(summary['recorded_http_attempts'],1)
        self.assertEqual(summary['successful_detail_responses'],1)
        self.assertEqual(summary['expected_background_slots_full_day'],690)
        self.assertEqual(summary['observed_background_slots'],1)
        self.assertLess(summary['observed_slot_fraction_so_far'],1)
        self.assertEqual(value['selection_scope'],'bounded_public_packet_not_full_day')
        self.assertFalse(value['network_performed_by_read'] or value['eta_available'])
        self.assertFalse(value['full_source_arrays_persisted'])
        self.assertTrue(value['full_source_arrays_in_input_packet'])
        self.assertIsNone(summary['actual_called_count'])
        self.assertIsNone(summary['no_show_rate'])

    def test_first_three_keep_order_duplicates_and_full_packet_is_preserved(self):
        original=packet();result=self.project(original);point=self.day(result)['points'][0]
        self.assertEqual(point['queues']['mixedQueue'],['0012','0014','0012'])
        self.assertEqual(point['display_sizes']['mixedQueue'],4)
        self.assertEqual(result['source_packet'],original)
        self.assertIsNot(result['source_packet'],original)
        self.assertEqual(point['call_reference_labels']['mixedQueue'],'0012')
        self.assertFalse(self.day(result)['first_label_is_confirmed_call'])
        self.assertIsNone(point['removed_labels'])

    def test_missing_queue_is_not_a_successful_empty_queue(self):
        payload={'id':900001,'name':'Synthetic store','groupQueues':{'reservationQueue':[]}}
        point=self.day(self.project(packet(payload=payload)))['points'][0]
        self.assertIsNone(point['queues'])
        self.assertEqual(point['queue_field_presence']['mixedQueue'],'missing')
        self.assertIsNone(point['observed_queue_fields']['mixedQueue'])
        self.assertEqual(point['observed_queue_fields']['reservationQueue'],[])
        self.assertIsNone(point['count_raw'])
        self.assertIsNone(point['count_received_at'])

    def test_invalid_count_is_unknown_and_not_zero(self):
        p={'id':900001,'name':'Synthetic store','groupQueuesCount':-1,
           'groupQueues':{'mixedQueue':[],'reservationQueue':[]}}
        point=self.day(self.project(packet(payload=p)))['points'][0]
        self.assertIsNone(point['count_raw'])
        self.assertEqual(point['count_field']['presence'],'invalid')
        self.assertIsNone(point['count_field']['value'])

    def test_china_midnight_groups_by_local_start_date(self):
        value=packet(stamp='2026-10-09T16:00:00Z')
        result=self.project(value)
        self.assertIn(('900001','2026-10-10'),result['days'])
        self.assertEqual(self.day(result)['summary']['observed_background_slots'],0)

    def test_legacy_source_and_future_read_are_rejected(self):
        with self.assertRaisesRegex(OfficialStatisticsError,'official_stats_profile_mismatch'):
            self.project(packet(profile='legacy'))
        with self.assertRaises(ReceiptError):
            self.project(as_of='2026-10-09T00:00:00Z')

    def test_invalid_cadence_and_names_do_not_create_misleading_scopes(self):
        for interval in (True,0,59,3601):
            with self.subTest(interval=interval),self.assertRaises(OfficialStatisticsError):
                self.project(base_interval=interval)
        for names in ({'900002':'Other'}, {'900001':'bad\nname'}, {'900001':''}):
            with self.subTest(names=names),self.assertRaises(OfficialStatisticsError):
                self.project(store_names=names)

    def test_duplicate_or_tampered_packet_is_rejected_before_projection(self):
        for duplicate in (True,False):
            value=packet()
            if duplicate:value['records'].append(deepcopy(value['records'][0]))
            else:value['records'][0]['display']['groupQueuesCount']['value']=100
            with self.assertRaises(ReceiptError):self.project(value)

    def test_existing_browser_adapter_accepts_only_explicit_official_cohort(self):
        value=self.project()
        data=json.dumps({'month':value['months']['2026-10'],'day':self.day(value)})
        script = """
import assert from 'node:assert/strict';
import {validateMonth,validateDay,createStatisticsClient,createStatisticsController} from './src/sushiwait/web/statistics-client.mjs';
let body='';for await(const chunk of process.stdin)body+=chunk;
const {month,day}=JSON.parse(body);const source='sapi_miniapp_gateway';
assert.throws(()=>validateMonth(month,'2026-10'));
assert.throws(()=>validateDay(day,'900001','2026-10-10'));
validateMonth(month,'2026-10',{source});validateDay(day,'900001','2026-10-10',{source});
const calls=[];const client=createStatisticsClient({source,fetchImpl:async path=>{
 calls.push(path);return {ok:true,text:async()=>JSON.stringify(path.includes('/months/')?month:day)};
}});
const index=await client.readMonth('2026-10');
await client.readDay('900001','2026-10-10',index.configured_store_ids);
assert.deepEqual(calls,['/api/v1/official/months/2026-10','/api/v1/official/stores/900001/days/2026-10-10']);
assert.throws(()=>createStatisticsClient({source:'auto'}));
assert.throws(()=>validateMonth({...month,api_profile:'legacy'},'2026-10',{source}));
assert.throws(()=>validateDay({...day,summary:{...day.summary,success_semantics:'two_queries'}},'900001','2026-10-10',{source}));
const controller=createStatisticsController({client:{readMonth:async()=>month,readDay:async()=>({...day,source:'crm_remote_v1_1'})}});
await controller.select({month:'2026-10',date:'2026-10-10',storeId:'900001'});
assert.equal(controller.snapshot().detail,null);
assert.equal(controller.snapshot().error.code,'day_scope_mismatch');
console.log('explicit official adapter passed');
"""
        node=shutil.which('node');self.assertIsNotNone(node)
        result=subprocess.run([node,'--input-type=module','-e',script],cwd=ROOT,
                              input=data,text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('explicit official adapter passed',result.stdout)
