from datetime import datetime,timedelta
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from sushiwait.client import QueryResult
from sushiwait.storage import SnapshotStore
from sushiwait.packets import encoded
from test_five_city_scope import FiveCityScopeTests,daily

spec=importlib.util.spec_from_file_location('fleet_collect',Path(__file__).resolve().parents[1]/'deploy/official_fleet_collect.py')
fleet=importlib.util.module_from_spec(spec);spec.loader.exec_module(fleet)
class FleetIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.root.chmod(0o700)
        self.now=datetime.fromisoformat('2026-10-16T11:05:00+08:00')
        self.plan=daily.make_plan(FiveCityScopeTests().expanded(),self.now)
        self.f=fleet.Fleet(self.root,self.plan,0,self.now);self.addCleanup(self.f.close)
    def record(self,store,code=504):
        self.f.reserve(store)
        result=QueryResult(False,None,'http_error',code,self.now.isoformat(),self.now.isoformat(),1)
        self.f.db.save_failure(store,result,api_profile='miniapp_gateway',failure_phase='request')
        self.f.reconcile()
    def test_calendar_success_uses_two_minute_peak_and_no_catchup(self):
        at=datetime.fromisoformat('2026-10-18T12:00:00+08:00')
        plan=daily.make_plan(FiveCityScopeTests().expanded(),at)
        path=self.root/'calendar';path.mkdir(mode=0o700)
        with_new=fleet.Fleet(path,plan,0,at);self.addCleanup(with_new.close)
        with_new.reserve('3025');with_new.db._insert('3025','live','miniapp_gateway',at.isoformat(),True,{})
        with_new.reconcile()
        self.assertEqual(fleet.instant(with_new.value['stores']['3025'][0]),at+timedelta(seconds=120))
        for elapsed,expected in [(0,1.4),(.5,.9),(2,0)]:
            self.assertAlmostEqual(fleet.request_delay(plan['cadence'],elapsed),expected)
        self.assertEqual(fleet.request_delay('meal-peaks-v1',.5),2.1)

    def test_504_isolates_only_one_store_preserving_spend(self):
        self.record('3025')
        self.assertEqual(self.f.status()['unavailable_store_ids'],['3025'])
        self.assertNotEqual(self.f.due(self.now),'3025')
        self.assertEqual(self.f.value['issued'],1)
        self.assertEqual(self.f.value['failed'],1)
        self.assertEqual(fleet.instant(self.f.value['stores']['3025'][0]),self.now+timedelta(minutes=30))
        restored=fleet.Fleet(self.root,self.plan,0,self.now);self.addCleanup(restored.close)
        self.assertEqual(restored.value,self.f.value)
    def test_one_403_isolates_store_for_day_without_stopping_other_stores(self):
        self.record('3025',403)
        self.assertEqual(self.f.value['failed'],1)
        self.assertIsNone(self.f.value['pending'])
        self.assertEqual(self.f.value['stores']['3025'][0],self.plan['windows'][0]['end'])
        self.assertIsNotNone(self.f.due(self.now))
        self.assertNotEqual(self.f.due(self.now),'3025')

    def test_three_distinct_consecutive_denials_stop_shared_authorization(self):
        self.record('3025',403);self.record('3026',401)
        with self.assertRaisesRegex(ValueError,'shared_authorization'):
            self.record('3032',403)
        self.assertEqual(self.f.value['issued'],3)
        self.assertEqual(self.f.value['failed'],3)
        self.assertIsNone(self.f.value['pending'])
        with self.assertRaisesRegex(ValueError,'shared_authorization'):
            fleet.Fleet(self.root,self.plan,0,self.now)

    def test_success_between_denials_prevents_shared_circuit(self):
        self.record('3025',403)
        self.f.reserve('3026')
        self.f.db._insert('3026','live','miniapp_gateway',self.now.isoformat(),True,{})
        self.f.reconcile()
        self.record('3032',403)
        self.assertIsNotNone(self.f.due(self.now))

    def test_old_or_same_store_denials_are_not_shared_credential_evidence(self):
        self.record('3025',403);self.record('3025',403)
        self.now+=timedelta(minutes=6)
        self.record('3026',403)
        self.assertIsNotNone(self.f.due(self.now))

    def test_request_and_data_failures_have_local_policies(self):
        for code,status,phase,action in [
            ('http_error',404,'request','day'),('http_error',500,'request','cooldown'),
            ('network_error',None,'request','cooldown'),('invalid_json',200,'request','cooldown'),
            ('business_error',200,'request','cooldown'),
            ('normalization_failed',200,'normalization','cooldown'),
            ('tls_verification_failed',None,'request','day'),
            ('store_id_mismatch',200,'request','day'),('redirect_blocked',302,'request','day'),
            ('invalid_store_id',None,'request','global'),('timeout',None,'preflight','global')]:
            row=(1,'3025',self.now.isoformat(),0,json.dumps(dict(error_code=code,http_status=status,failure_phase=phase)))
            self.assertEqual(fleet.failure_action(row),action)

    def test_writer_continues_other_stores_after_one_denial(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        import threading
        stop=threading.Event(); visited=[]; now=self.now
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return now.astimezone(tz)
        controller=Mock();controller.context.return_value=(SimpleNamespace(revision=1,authorization='synthetic'), '0'*64)
        def observe(client,store,db,**kwargs):
            visited.append(store)
            if len(visited)==1:
                db.save_failure(store,QueryResult(False,None,'http_error',403,now.isoformat(),now.isoformat(),1),api_profile='miniapp_gateway',failure_phase='request')
            else:db._insert(store,'live','miniapp_gateway',now.isoformat(),True,{})
            if len(visited)==3:stop.set()
        service=SimpleNamespace(campaign=daily.campaign,reauthorization=daily.reauthorization,publish_status=Mock(),CHINA=daily.CHINA)
        with patch.object(fleet,'datetime',Clock),patch.object(daily.campaign.legacy,'Controller',return_value=controller),\
             patch.object(daily.campaign.legacy,'prepare_material_source',return_value=object()),\
             patch.object(fleet.cli,'describe_authorization',return_value={}),\
             patch.object(fleet.cli,'_authorization_stop_code',return_value=None),\
             patch.object(fleet.cli,'client_for',return_value=object()),patch.object(fleet.cli,'observe',side_effect=observe),\
             patch.object(stop,'wait',return_value=False):
            fleet.run(service,self.root,self.root,self.plan,Mock(),stop)
        self.assertEqual(len(set(visited)),3)
        state=json.loads(self.f.path.read_bytes())
        self.assertEqual((state['issued'],state['successful'],state['failed']),(3,2,1))
        self.assertEqual(state['stores'][visited[0]][0],self.plan['windows'][0]['end'])
    def test_uncertain_reservation_is_not_reissued_after_restart(self):
        self.f.reserve('3025')
        with self.assertRaisesRegex(ValueError,'outcome_unknown'):
            fleet.Fleet(self.root,self.plan,0,self.now)
        self.assertEqual(json.loads(self.f.path.read_bytes())['issued'],1)
    def test_success_clears_isolation_and_schedules_from_actual_receive(self):
        self.record('3025');r=(2,'3025',(self.now+timedelta(minutes=30)).isoformat(),1,'{}')
        self.f.value['issued']+=1;self.f.finish(r)
        self.assertEqual(self.f.status()['unavailable_store_ids'],[])
        self.assertEqual(fleet.instant(self.f.value['stores']['3025'][0]),self.now+timedelta(minutes=33))
    def test_three_failures_pause_until_close_and_do_not_block_others(self):
        for _ in range(3):self.record('3025')
        self.assertEqual(self.f.value['stores']['3025'][0],self.plan['windows'][0]['end'])
        self.assertNotEqual(self.f.due(self.now+timedelta(hours=1)),'3025')
    def test_seed_requires_exact_legacy_accounting(self):
        path=self.root/'empty';path.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError,'inherited_usage'):
            fleet.Fleet(path,self.plan,60,self.now)
    def test_budget_is_not_reset_and_due_returns_none_at_limit(self):
        self.f.value['issued']=self.plan['ordinary_query_limit']
        self.assertIsNone(self.f.due(self.now))
    def test_fleet_usage_included_in_owner_reauthorization(self):
        day=self.root/self.plan['plan_id'];day.mkdir(mode=0o700)
        p=day/'fleet-state.json';p.write_bytes(encoded({'issued':60,'plan_sha256':self.f.value['plan_sha256']}));p.chmod(0o600)
        self.assertEqual(daily.reauthorization.task_usage(day,self.plan)[0],60)
    def test_operator_clearance_bound_to_original_task_and_one_transient_row(self):
        from unittest.mock import Mock
        import hashlib
        day=self.root/self.plan['plan_id'];day.mkdir(mode=0o700)
        task=day/'collection-2026-10-16-smooth.json';task.write_bytes(b'{}');task.chmod(0o600)
        with SnapshotStore(day/'observations.sqlite') as db:
            db.save_failure('3025',QueryResult(False,None,'http_error',504,self.now.isoformat(),self.now.isoformat(),1),api_profile='miniapp_gateway',failure_phase='request')
        daily.reauthorization.latch(self.root,'cloud_reader_failed','2026-10-16',self.now)
        block=daily.reauthorization.latest_block(self.root)
        directory=self.root/'isolation-clearances';directory.mkdir(mode=0o700)
        receipt={'schema_version':1,'policy':fleet.POLICY,'block_sha256':block['sha256'],
                 'task_file':str(task.relative_to(self.root)),'task_sha256':hashlib.sha256(b'{}').hexdigest(),'failure_id':1}
        p=directory/(block['sha256']+'.json');p.write_bytes(encoded(receipt));p.chmod(0o600)
        status={'pending_attempt':False,'uncertain_slots':0,'failed_slots':1}
        with patch.object(daily.reauthorization.campaign.legacy,'task_status',return_value=status):
            self.assertIsNone(daily.reauthorization.active_block(self.root))
            self.assertIsNone(daily.reauthorization.resume_not_before(self.root))
            with SnapshotStore(day/'observations.sqlite') as db:
                db.save_failure('3025',QueryResult(False,None,'http_error',504,self.now.isoformat(),self.now.isoformat(),1),api_profile='miniapp_gateway',failure_phase='request')
            self.assertIsNone(daily.reauthorization.active_block(self.root))
            daily.reauthorization.latch(self.root,'cloud_refresh_failed','2026-10-16',self.now+timedelta(seconds=1))
            self.assertIsNotNone(daily.reauthorization.active_block(self.root))
    def test_429_respects_global_backoff_and_preserves_other_store_due_times(self):
        self.record('3025',429)
        self.assertIsNone(self.f.due(self.now+timedelta(minutes=14)))
        self.assertIsNotNone(self.f.due(self.now+timedelta(minutes=16)))
        self.assertNotEqual(self.f.due(self.now+timedelta(minutes=16)),'3025')
        self.assertEqual(fleet.instant(self.f.value['rate_limit_until']),self.now+timedelta(minutes=15))
        self.record('3025',429)
        self.assertEqual(fleet.instant(self.f.value['rate_limit_until']),self.now+timedelta(minutes=30))
