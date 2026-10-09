import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from sushiwait.dailyfleet import (DailyFleetService, OriginGate, FleetRemoteClient,
    validate_catalog, SUMMARY_KEYS, MAX_STORES)
from sushiwait.dailycontroller import DailyController
from sushiwait.monitorhub import MonitorHub, validate_config, HubError
from sushiwait.remote import RemoteResult
from sushiwait.remoteservice import RemoteASGI, RemoteServiceError
from sushiwait.remotetasks import RemoteTaskError
from test_remote_service import request, wait_for
import test_daily_controller as fixture


def catalog(n=2):
    return {'schema_version':1,'directory_observed_at':fixture.BASE.isoformat(),
        'directory_source':'official_normal_response_provided_by_user',
        'directory_currentness_verified':False,'current_mainland_completeness_verified':False,
        'stores':[{'store_id':str(900001+i),'directory_name':'Synthetic '+str(i),'area':'Synthetic'} for i in range(n)]}


class FleetTests(unittest.TestCase):
    setUp=fixture.DailyControllerTests.setUp
    tearDown=fixture.DailyControllerTests.tearDown

    def fleet(self, **kwargs):
        return DailyFleetService(root=self.root,catalog=catalog(),business_hours=self.hours,
            not_before=fixture.BASE.isoformat(),wall_clock=self.clock.wall,**kwargs)

    def test_catalog_is_explicit_bounded_unique_and_never_currentness_claim(self):
        self.assertEqual(len(validate_catalog(catalog(256))['stores']),256)
        for v in [catalog(257),catalog(0),{**catalog(),'directory_currentness_verified':True},
                  {**catalog(),'current_mainland_completeness_verified':True}]:
            with self.assertRaises(RemoteServiceError):validate_catalog(v)
        v=catalog();v['stores'][1]['store_id']=v['stores'][0]['store_id']
        with self.assertRaises(RemoteServiceError):validate_catalog(v)
        v=catalog();v['stores'][0]['credential']='forbidden'
        with self.assertRaises(RemoteServiceError):validate_catalog(v)

    def test_same_root_lock_blocks_duplicate_and_configuration_change(self):
        self.clock.seconds=-60
        a=self.fleet();b=self.fleet()
        try:
            a.start()
            with self.assertRaises(RemoteServiceError):b.start()
            self.assertTrue(a.status()['worker_alive'])
            self.assertEqual(a.status()['origin_gate']['transport_admissions_this_process'],0)
        finally:a.shutdown()
        c=self.fleet(requests_per_second=6)
        with self.assertRaises(RemoteServiceError):c.start()

    def test_read_routes_add_no_origin_or_writer_start_and_keep_scope(self):
        f=self.fleet()
        self.root.mkdir(mode=0o700)
        for child in f.children.values(): Path(child.config['root']).mkdir(mode=0o700)
        with patch('socket.socket',side_effect=AssertionError('origin forbidden')):
            self.assertEqual(f.daily_batch_index()['days'],{})
            self.assertEqual(f.status('900001')['store_ids'],['900001'])
        for path in ['/api/v1/days','/api/v1/months/2026-10','/api/v1/stores/900001/status',
                     '/api/v1/stores/900001/days/2026-10-09']:
            status,v,_=asyncio.run(request(RemoteASGI(f),path))
            self.assertEqual(status,200);self.assertFalse(v['network_performed_by_read'])
        status,_,_=asyncio.run(request(RemoteASGI(f),'/api/v1/stores/999999/status'))
        self.assertEqual(status,404)
        self.assertFalse(f.started);self.assertEqual(self.opener.calls,[])

    def test_hub_schema3_uses_one_read_for_whole_calendar_and_per_store_status(self):
        f=self.fleet();names=f.names;calls=[]
        def read(w,path):
            calls.append(path)
            return f.status('900002') if path.endswith('/status') else f.daily_batch_index('2026-10')
        h=MonitorHub({'schema_version':3,'mode':'daily_fleet_readonly',
            'workers':[{'endpoint':'http://127.0.0.1:18821','stores':names}]},reader=read)
        self.assertEqual(h.daily_index('2026-10')['store_names'],names)
        self.assertEqual(h.status('900002')['selected_store_id'],'900002')
        self.assertEqual(calls,['/api/v1/months/2026-10','/api/v1/stores/900002/status'])
        self.assertEqual(validate_config(h.config),h.config)
        v=deepcopy(h.config);v['workers'].append(v['workers'][0])
        with self.assertRaises(HubError):validate_config(v)

    def test_global_compact_calendar_preserves_full_individual_summary(self):
        f=self.fleet();self.root.mkdir(mode=0o700)
        for store,child in f.children.items():
            config=child.config
            with DailyController(config=config,now=self.clock.wall()) as c:
                c.tick(wall_clock=self.clock.wall,monotonic_clock=self.clock.mono,sleep=self.clock.sleep,
                    emit=lambda _:None,client_factory=lambda:fixture.RemoteClient(opener=self.opener))
            self.clock.seconds=0
        value=f.daily_batch_index('2026-10')
        self.assertEqual(set(value['days']['2026-10-09']),set(f.names))
        row=value['days']['2026-10-09']['900001']
        self.assertEqual(set(row),set(SUMMARY_KEYS));self.assertEqual(row['successful_pairs'],2)
        self.assertIn('source_freshness',f.daily_detail('900001','2026-10-09')['summary'])
        f.gate.halted=True
        halted=f.daily_batch_index('2026-10')
        self.assertEqual(halted['unavailable_store_ids'],list(f.names))
        self.assertTrue(halted['origin_halted'])
        self.assertEqual(halted['days'],value['days'])

    def test_legacy_must_explicitly_select_only_previously_collected_stores(self):
        for kwargs in [dict(legacy_store_ids=['999999']),dict(legacy_exports_root='/tmp'),
                       dict(legacy_store_ids=['900001'])]:
            with self.assertRaises(RemoteServiceError):self.fleet(**kwargs)

    def test_two_real_workers_commit_separate_pairs_through_shared_gate(self):
        f=self.fleet(opener_factory=lambda:self.opener,requests_per_second=6)
        try:
            f.start()
            wait_for(lambda: all(len(c.view.records[s]['successful'])==2 for s,c in f.children.items()))
            self.assertEqual(len(self.opener.calls),4)
            self.assertEqual(f.status()['origin_gate']['transport_admissions_this_process'],4)
            for store in f.names:
                self.assertEqual(f.daily_detail(store,'2026-10-09')['summary']['successful_pairs'],1)
        finally:f.shutdown()
        self.assertEqual(f.status()['origin_gate']['inflight'],0)

    def test_origin_429_halts_other_writers_and_restart_without_new_requests(self):
        class Denial(fixture.Opener):
            def open(self,request,timeout):
                self.calls.append((request.full_url,self.clock.seconds))
                return fixture.Response(request,429)
        opener=Denial(self.clock)
        f=self.fleet(opener_factory=lambda:opener,requests_per_second=6)
        try:
            f.start();wait_for(lambda:f.gate.halted)
            self.assertEqual(f.status()['service_state'],'failed')
        finally:f.shutdown()
        before=len(opener.calls)
        again=self.fleet(opener_factory=lambda:opener)
        with self.assertRaises(RemoteServiceError):again.start()
        self.assertEqual(len(opener.calls),before)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.root.chmod(0o700)
    def tearDown(self):self.tmp.cleanup()

    def test_concurrent_transports_are_spaced_without_accumulated_burst(self):
        gate=OriginGate(root=self.root,requests_per_second=6,inflight=3)
        starts=[];lock=threading.Lock()
        def run():
            self.assertTrue(gate.enter(lambda:True))
            with lock:starts.append(time.monotonic())
            time.sleep(.01);gate.leave()
        threads=[threading.Thread(target=run) for _ in range(5)]
        for t in threads:t.start()
        for t in threads:t.join(3);self.assertFalse(t.is_alive())
        self.assertEqual(len(starts),5)
        self.assertGreaterEqual(min(b-a for a,b in zip(starts,starts[1:])),1/6-.003)
        self.assertLessEqual(gate.status()['peak_inflight'],3)

    def test_closing_during_pace_wait_adds_no_admission_and_releases_slot(self):
        gate=OriginGate(root=self.root,requests_per_second=6,inflight=1)
        self.assertTrue(gate.enter(lambda:True));gate.leave()
        until=time.monotonic()+.03
        self.assertFalse(gate.enter(lambda:time.monotonic()<until))
        self.assertEqual(gate.status()['transport_admissions_this_process'],1)
        self.assertTrue(gate.slots.acquire(blocking=False));gate.slots.release()

    def test_origin_denial_persists_across_restart_and_never_auto_resumes(self):
        gate=OriginGate(root=self.root,requests_per_second=6)
        self.assertTrue(gate.enter(lambda:True))
        result=RemoteResult('groupqueues',True,False,None,'http_error',429,None,None,None)
        gate.leave(result)
        with self.assertRaises(RemoteTaskError):gate.enter(lambda:True)
        again=OriginGate(root=self.root)
        self.assertTrue(again.status()['origin_halted'])
        with self.assertRaises(RemoteTaskError):again.enter(lambda:True)
        self.assertEqual(json.loads((self.root/'origin-halted.json').read_text())['automatic_resume'],False)

    def test_stop_interrupts_wait_without_transport(self):
        gate=OriginGate(root=self.root)
        gate.stop.set()
        with self.assertRaises(RemoteTaskError):gate.enter(lambda:True)
        self.assertEqual(gate.status()['transport_admissions_this_process'],0)

    def test_invalid_bounds_and_changed_persistent_halt_are_conservative(self):
        for kwargs in [dict(requests_per_second=7),dict(requests_per_second=float('nan')),
                       dict(inflight=9),dict(inflight=True)]:
            with self.assertRaises(RemoteServiceError):OriginGate(root=self.root,**kwargs)


if __name__=='__main__':unittest.main()
