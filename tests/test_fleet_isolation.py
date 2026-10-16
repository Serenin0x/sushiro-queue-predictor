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
    def test_504_isolates_only_one_store_preserving_spend(self):
        self.record('3025')
        self.assertEqual(self.f.status()['unavailable_store_ids'],['3025'])
        self.assertNotEqual(self.f.due(self.now),'3025')
        self.assertEqual(self.f.value['issued'],1)
        self.assertEqual(self.f.value['failed'],1)
        self.assertEqual(fleet.instant(self.f.value['stores']['3025'][0]),self.now+timedelta(minutes=30))
        restored=fleet.Fleet(self.root,self.plan,0,self.now);self.addCleanup(restored.close)
        self.assertEqual(restored.value,self.f.value)
    def test_403_is_global_and_never_retried(self):
        with self.assertRaisesRegex(ValueError,'nontransient'):
            self.record('3025',403)
        self.assertEqual(self.f.value['failed'],1)
        self.assertIsNone(self.f.value['pending'])
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
