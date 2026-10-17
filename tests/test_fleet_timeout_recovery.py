from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sushiwait.client import QueryResult
from sushiwait.packets import encoded
import test_fleet_isolation as fixture

spec = importlib.util.spec_from_file_location('timeout_recovery',
    Path(__file__).resolve().parents[1]/'deploy/official_timeout_recovery.py')
recovery = importlib.util.module_from_spec(spec); spec.loader.exec_module(recovery)
fleet, daily = fixture.fleet, fixture.daily


class TimeoutRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.root.chmod(0o700)
        self.now = datetime.fromisoformat('2026-10-17T10:30:48+08:00')
        self.plan = daily.make_plan(fixture.FiveCityScopeTests().expanded(),self.now)
        self.day = self.root/self.plan['plan_id']; self.day.mkdir(mode=0o700)
        daily.reauthorization.put_once(self.day/'campaign-plan.json',self.plan)
        (self.day/'controller-state').mkdir(mode=0o700)
        daily.campaign.CampaignLedger(self.day/'controller-state',self.plan)
        self.f = fleet.Fleet(self.day,self.plan,0,self.now); self.addCleanup(self.f.close)

    def failure(self, code='timeout', status=None, phase='request', old=False):
        self.f.reserve('8')
        self.f.db.save_failure('8', QueryResult(False,None,code,status,
            self.now.isoformat(),self.now.isoformat(),15000),
            api_profile='miniapp_gateway',failure_phase=phase)
        if old:
            with patch.object(fleet,'failure_action',return_value='global'):
                with self.assertRaisesRegex(ValueError,'nontransient'):self.f.reconcile()
        else:self.f.reconcile()

    def test_recorded_get_timeout_isolates_store_and_keeps_other_stores_due(self):
        self.failure()
        self.assertEqual(self.f.value['issued'],1)
        self.assertEqual(self.f.value['failed'],1)
        self.assertEqual(self.f.status()['unavailable_store_ids'],['8'])
        self.assertEqual(fleet.instant(self.f.value['stores']['8'][0]),self.now+timedelta(minutes=30))
        self.assertIsNotNone(self.f.due(self.now))
        self.assertNotEqual(self.f.due(self.now),'8')

    def test_timeout_after_success_headers_is_retryable_but_denial_is_not(self):
        for status in [None,200,204]:
            row=(1,'8',self.now.isoformat(),0,json.dumps(dict(error_code='timeout',http_status=status,failure_phase='request')))
            self.assertTrue(fleet.is_transient(row))
        for status in [401,403,429,True,'200']:
            row=(1,'8',self.now.isoformat(),0,json.dumps(dict(error_code='timeout',http_status=status,failure_phase='request')))
            self.assertFalse(fleet.is_transient(row))
        row=(1,'8',self.now.isoformat(),0,json.dumps(dict(error_code='timeout',http_status=None,failure_phase='normalization')))
        self.assertFalse(fleet.is_transient(row))

    def block(self):
        daily.reauthorization.latch(self.root,'daily_collection_failed','2026-10-17',self.now)

    def test_operator_recovery_preserves_original_failure_spend_and_ledger(self):
        self.failure(old=True); self.block()
        before=self.f.path.read_bytes()
        ledger=(self.day/'controller-state/checkpoint-000.json').read_bytes()
        result=recovery.prepare(self.root,daily.reauthorization,self.now+timedelta(hours=4))
        self.assertEqual(result['preserved_attempts'],1)
        self.assertEqual(result['preserved_failures'],1)
        self.assertIsNone(daily.reauthorization.active_block(self.root))
        self.assertIsNone(daily.reauthorization.resume_not_before(self.root))
        self.assertEqual((self.day/'controller-state/checkpoint-000.json').read_bytes(),ledger)
        block=daily.reauthorization.latest_block(self.root)
        _, saved, _, _=recovery.paths(self.root,block)
        self.assertEqual(saved.read_bytes(),before)
        self.assertEqual(self.f.db.db.execute('select count(*) from samples').fetchone()[0],1)
        again=fleet.Fleet(self.day,self.plan,0,self.now+timedelta(hours=4));self.addCleanup(again.close)
        self.assertEqual(again.value['issued'],1)
        self.assertEqual(again.value['stores']['8'][2],1)
        self.assertNotEqual(again.due(self.now+timedelta(hours=4)),'8')
        self.assertTrue(recovery.prepare(self.root,daily.reauthorization,self.now+timedelta(hours=4))['already_prepared'])
        daily.reauthorization.latch(self.root,'cloud_refresh_failed','2026-10-17',self.now+timedelta(hours=5))
        self.assertIsNotNone(daily.reauthorization.active_block(self.root))

    def test_operator_recovery_rejects_denial_without_changing_live_state(self):
        self.failure('http_error',403,old=True); self.block()
        original=self.f.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'not_retryable'):
            recovery.prepare(self.root,daily.reauthorization,self.now)
        self.assertEqual(self.f.path.read_bytes(),original)
        self.assertIsNotNone(daily.reauthorization.active_block(self.root))

    def test_pending_unknown_is_never_cleared(self):
        self.failure(old=True); self.f.reserve('1'); self.block()
        original=self.f.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'accounting'):
            recovery.prepare(self.root,daily.reauthorization,self.now)
        self.assertEqual(self.f.path.read_bytes(),original)
        self.assertIsNotNone(daily.reauthorization.active_block(self.root))

    def test_recovery_cannot_refund_attempts_or_change_other_store(self):
        self.failure(old=True); self.block()
        recovery.prepare(self.root,daily.reauthorization,self.now)
        block=daily.reauthorization.latest_block(self.root)
        _, _, after, _=recovery.paths(self.root,block)
        value=json.loads(after.read_bytes());value['issued']=0
        after.write_bytes(encoded(value))
        with self.assertRaisesRegex(ValueError,'changed'):
            daily.reauthorization.active_block(self.root)

    def test_three_timeouts_stop_one_store_for_day_without_retry_burst(self):
        for _ in range(3):self.failure()
        self.assertEqual(self.f.value['stores']['8'][0],self.plan['windows'][0]['end'])
        self.assertEqual(self.f.value['issued'],3)
        self.assertIsNotNone(self.f.due(self.now+timedelta(hours=1)))
        self.assertNotEqual(self.f.due(self.now+timedelta(hours=1)),'8')
