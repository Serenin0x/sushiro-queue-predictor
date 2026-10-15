"""Synthetic confirmations only; never contacts WeChat or issues tickets."""
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import urllib.request
import urllib.error
import unittest
from unittest.mock import patch

from sushiwait.packets import encoded
from tests.test_official_daily_service import daily, config

spec = importlib.util.spec_from_file_location('portal',
    Path(__file__).resolve().parents[1] / 'deploy' / 'reauthorization_portal.py')
portal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(portal)
recovery = portal.recovery
NOW = datetime.fromisoformat('2026-10-15T16:00:00+08:00')
KEY = b'k' * 32


class FakeUpstream:
    def __init__(self):
        self.calls, self.logged_in = [], False
        self.state, self.account, self.failure = 'pending', '1', None

    def cookie(self):
        if not self.logged_in:
            raise portal.PortalError('login_required', 401)
        return 'synthetic-session-only'

    def login(self, password):
        self.calls.append(('login', '<discarded>'))
        self.logged_in = password == 'synthetic-owner-password'
        self.cookie()

    def create_link(self):
        self.cookie()
        self.calls.append(('link', 'original-account-1-only'))
        return 'L' * 32

    def request(self, path, data=None, **kwargs):
        self.calls.append((path, data))
        if path.endswith('/image'):
            return b'synthetic-png', 'image/png'
        if path.endswith('/poll'):
            return {'status': self.state}, None
        if path.endswith('/confirm'):
            if self.failure:
                raise portal.PortalError(self.failure, 502)
            return {'id': self.account}, None
        return {'session_id': 'synthetic-qr'}, None


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.plan = daily.make_plan(config(), NOW)
        self.day = self.root / self.plan['plan_id']; self.day.mkdir(mode=0o700)
        recovery.put_once(self.day / 'campaign-plan.json', self.plan)
        self.ledger_dir = self.day / 'controller-state'; self.ledger_dir.mkdir(mode=0o700)
        self.ledger = recovery.campaign.CampaignLedger(self.ledger_dir, self.plan)
        self.ledger.reserve(); self.ledger.accepted(1, 'a' * 64)
        self.ledger.reserve(); self.ledger.halt('cloud_refresh_failed')
        self.diagnostic = self.day / 'refresh-diagnostics'; self.diagnostic.mkdir(mode=0o700)
        recovery.put_once(self.diagnostic / 'attempt-002.json', {
            'phase': 'supplier', 'error_code': 'yyb_reauthorization_required'})
        recovery.latch(self.root, 'cloud_refresh_failed', NOW.date().isoformat(), NOW)
        self.block = recovery.active_block(self.root)
        self.receipt = recovery.signed_receipt({'schema_version': 1, 'receipt_id': '1' * 32,
            'account_ref': '1', 'block_sha256': self.block['sha256'],
            'confirmed_at': (NOW+timedelta(seconds=1)).isoformat()}, KEY)

    def test_signed_receipt_inherits_spent_pending_and_preserves_old_bytes(self):
        before = {p: p.read_bytes() for p in self.ledger_dir.glob('*.json')}
        blocked = (self.root / 'collection-blocked.json').read_bytes()
        self.assertTrue(recovery.prepare_recovery(self.root, self.day, self.plan,
            self.receipt, KEY, NOW+timedelta(seconds=2)))
        ledger_dir, task = recovery.selected(self.day, self.plan)
        successor = recovery.campaign.CampaignLedger(ledger_dir, self.plan)
        self.assertEqual((successor.value['attempts'], successor.value['highest_revision']), (2, 1))
        self.assertFalse(successor.value['pending']); self.assertFalse(successor.value['halted'])
        successor.reserve()
        self.assertEqual(successor.value['attempts'], 3)
        self.assertIsNone(recovery.active_block(self.root))
        self.assertEqual(before, {p: p.read_bytes() for p in self.ledger_dir.glob('*.json')})
        self.assertEqual(blocked, (self.root / 'collection-blocked.json').read_bytes())
        self.assertFalse(recovery.prepare_recovery(self.root, self.day, self.plan,
            self.receipt, KEY, NOW+timedelta(seconds=3)))

    def test_wrong_signature_account_stale_and_other_incident_do_not_clear(self):
        for change in ({'account_ref': '2'}, {'block_sha256': 'b'*64},
                       {'confirmed_at': (NOW-timedelta(seconds=1)).isoformat()}):
            receipt = recovery.signed_receipt({**self.receipt['payload'], **change}, KEY)
            with self.assertRaises(ValueError):
                recovery.prepare_recovery(self.root, self.day, self.plan, receipt, KEY, NOW+timedelta(seconds=2))
        with self.assertRaises(ValueError):
            recovery.validate_receipt(self.receipt, self.block, b'wrong-key', NOW+timedelta(seconds=2))
        with self.assertRaises(ValueError):
            recovery.validate_receipt(self.receipt, self.block, KEY, NOW+timedelta(hours=1))
        self.assertIsNotNone(recovery.active_block(self.root))

    def test_changed_seed_and_path_manifest_are_rejected(self):
        recovery.prepare_recovery(self.root, self.day, self.plan, self.receipt, KEY, NOW+timedelta(seconds=2))
        path = self.day/'controller-state-reauth-001'/'checkpoint-000.json'
        value = recovery.read(path); value['attempts'] = 0
        path.write_bytes(encoded(value))
        with self.assertRaisesRegex(ValueError, 'recovery_seed_changed'):
            recovery.selected(self.day, self.plan)

    def test_second_incident_cannot_consume_first_receipt(self):
        recovery.prepare_recovery(self.root, self.day, self.plan, self.receipt, KEY, NOW+timedelta(seconds=2))
        recovery.latch(self.root, 'cloud_refresh_failed', NOW.date().isoformat(), NOW+timedelta(seconds=5))
        self.assertNotEqual(recovery.active_block(self.root)['sha256'], self.block['sha256'])
        with self.assertRaises(ValueError):
            recovery.prepare_recovery(self.root, self.day, self.plan, self.receipt, KEY, NOW+timedelta(seconds=6))

    def test_ordinary_failed_unknown_and_pending_calls_are_not_refunded(self):
        recovery.prepare_recovery(self.root, self.day, self.plan, self.receipt, KEY, NOW+timedelta(seconds=2))
        original = recovery.campaign.task_path(self.day, recovery.campaign.validate_plan(self.plan)[0][0], self.plan['cadence'])
        original.touch(mode=0o600)
        status = {'store_ids': daily.STORES, 'api_profile': 'miniapp_gateway', 'interval_seconds': 600,
            'cadence_policy': {'id': 'meal-peaks-v1'}, 'target_rounds': 136, 'completed_slots': 10,
            'pending_attempt': True}
        with patch.object(recovery.campaign.legacy, 'task_status', return_value=status):
            _, rounds, resume, _ = recovery.window_budget(self.day, self.plan, NOW)
        self.assertEqual(rounds, (680-11)//5); self.assertFalse(resume)

    def test_after_closing_receipt_selects_next_day_without_backfill(self):
        tomorrow = NOW+timedelta(days=1)
        plan = daily.make_plan(config(), tomorrow)
        day = self.root/plan['plan_id']; day.mkdir(mode=0o700)
        recovery.put_once(day/'campaign-plan.json', plan)
        later = NOW.replace(hour=22, minute=10)
        receipt = recovery.signed_receipt({**self.receipt['payload'], 'confirmed_at': later.isoformat()}, KEY)
        recovery.prepare_recovery(self.root, day, plan, receipt, KEY, later)
        self.assertEqual(recovery.resume_not_before(self.root), tomorrow.replace(hour=11,minute=0))
        self.assertIsNone(recovery.active_block(self.root))

    def test_daily_loop_accepts_receipt_then_runs_original_five_store_scope(self):
        receipt_dir=self.root/'receipts';receipt_dir.mkdir(mode=0o700)
        key_file=self.root/'key';key_file.write_bytes(KEY);key_file.chmod(0o600)
        recovery.put_once(receipt_dir/'owner-confirmation.json',self.receipt)
        recovery.put_once(self.root/'recovery-settings.json',{'schema_version':1,
            'receipt_directory':str(receipt_dir),'signing_key_file':str(key_file)})
        stopped=threading.Event();seen=[]
        def run(day,plan,ledger,signal,**kwargs):
            seen.append((day,plan['store_ids'],ledger.value.copy()))
            self.assertIn('reauth-001',ledger.root.name)
            signal.set();return 0
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None): return cls.fromisoformat((NOW+timedelta(seconds=2)).isoformat())
        with patch.object(daily,'datetime',Clock), \
             patch.object(stopped,'wait',side_effect=AssertionError('unexpected wait')), \
             patch.object(daily,'prepare_day',return_value=(self.day,self.plan)), \
             patch.object(daily.campaign,'run_campaign',side_effect=run):
            daily.run_collection(self.root,config(),stopped)
        self.assertEqual(len(seen),1)
        self.assertEqual(seen[0][1],daily.STORES)
        self.assertEqual(seen[0][2]['attempts'],2)
        self.assertIsNone(recovery.active_block(self.root))


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.root.chmod(0o700)
        self.collection = self.root/'collection'; self.collection.mkdir(mode=0o700)
        (self.root/'receipt-signing.key').write_bytes(b'x'*32); (self.root/'receipt-signing.key').chmod(0o600)
        self.upstream = FakeUpstream(); self.now = NOW.timestamp()
        self.broker = portal.Broker(self.root, self.collection, 'synthetic-owner',
            upstream=self.upstream, clock=lambda: self.now)
        self.addCleanup(self.broker.close)
        self.cap = self.broker.issue(verification=True).split('#')[1]

    def begin(self):
        self.broker.login(self.cap, 'synthetic-owner-password')
        return self.broker.qr(self.cap)

    def test_link_fragment_does_not_generate_qr_or_touch_supplier(self):
        self.assertEqual(len(self.cap), 43)
        self.assertTrue(self.broker.status(self.cap)['login_required'])
        self.assertEqual(self.upstream.calls, [])
        with self.assertRaisesRegex(portal.PortalError, 'reauthorization_not_required'):
            self.broker.issue()

    def test_login_qr_and_success_reuse_then_consume_capability(self):
        value = self.begin(); calls = len(self.upstream.calls)
        self.assertEqual(self.broker.qr(self.cap), value)
        self.assertEqual(len(self.upstream.calls), calls)
        identity = value['image_url'].split('/')[-1]
        self.assertEqual(self.broker.image_ticket(identity), (b'synthetic-png', 'image/png'))
        self.upstream.state = 'authorized'
        self.assertEqual(self.broker.poll(self.cap)['state'], 'authorized')
        self.assertEqual(list((self.root/'receipts').glob('*.json')), [])
        with self.assertRaisesRegex(portal.PortalError, 'authorization_already_completed'):
            self.broker.qr(self.cap)

    def test_wrong_account_never_creates_receipt_or_claims_success(self):
        self.begin(); self.upstream.state='authorized'; self.upstream.account='2'
        with self.assertRaisesRegex(portal.PortalError, 'account_mismatch'):
            self.broker.poll(self.cap)
        self.assertEqual(list((self.root/'receipts').glob('*.json')), [])
        self.assertEqual(self.broker.status(self.cap)['state'], 'confirmation_unknown')

    def test_uncertain_confirmation_never_repeats_after_restart_or_poll(self):
        self.begin(); self.upstream.state='authorized'; self.upstream.failure='upstream_unavailable'
        with self.assertRaises(portal.PortalError): self.broker.poll(self.cap)
        self.upstream.failure=None
        self.assertEqual(self.broker.poll(self.cap)['state'], 'confirmation_unknown')
        self.assertEqual(sum(p.endswith('/confirm') for p,_ in self.upstream.calls), 1)

    def test_expiry_blocks_every_authorization_operation(self):
        self.now += portal.CAP_SECONDS
        for action in (self.broker.status, self.broker.qr, self.broker.poll):
            with self.assertRaisesRegex(portal.PortalError, 'link_expired'): action(self.cap)

    def test_incident_confirmation_writes_bound_signed_receipt(self):
        recovery.latch(self.collection, 'cloud_refresh_failed', NOW.date().isoformat(), NOW)
        with patch.object(recovery, 'requires_scan', return_value=True):
            self.cap=self.broker.issue().split('#')[1]
        self.begin(); self.now+=2; self.upstream.state='authorized'
        self.assertEqual(self.broker.poll(self.cap)['state'], 'authorized')
        receipt = recovery.read(next((self.root/'receipts').glob('*.json')))
        recovery.validate_receipt(receipt, recovery.active_block(self.collection), b'x'*32,
                                  NOW+timedelta(seconds=3))
        self.assertIsNotNone(recovery.active_block(self.collection))

    def test_password_attempts_are_bounded_and_never_persisted(self):
        for _ in range(5):
            with self.assertRaises(portal.PortalError): self.broker.login(self.cap, 'incorrect')
        with self.assertRaisesRegex(portal.PortalError, 'login_rate_limited'):
            self.broker.login(self.cap, 'incorrect')
        self.assertNotIn(b'incorrect', (self.root/'portal.sqlite3').read_bytes())

    def test_http_boundary_origin_internal_key_and_qr_query(self):
        server = portal.ThreadingHTTPServer(('127.0.0.1', 0), portal.handler(self.broker, b'i'*32))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        base='http://127.0.0.1:'+str(server.server_port)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def post(path, body, headers=None):
            request=urllib.request.Request(base+path, data=encoded(body), headers=headers or {}, method='POST')
            try:
                response=opener.open(request)
            except urllib.error.HTTPError as error:
                response=error
            with response: return response.code, json.loads(response.read())
        body={'cap':self.cap}
        self.assertEqual(post('/recover/api/qr', body)[0],403)
        headers={'Origin':portal.ORIGIN,'X-Forwarded-Proto':'https'}
        self.assertEqual(post('/internal/issue', {'verification':True},headers)[0],404)
        self.assertEqual(post('/recover/api/status',body,headers)[0],200)
        image=self.begin()['image_url']
        with opener.open(base+image+'?v=123') as response:
            self.assertEqual(response.read(),b'synthetic-png')
            self.assertEqual(response.headers['Cache-Control'],'no-store')
            self.assertIn("frame-ancestors 'none'",response.headers['Content-Security-Policy'])
        with opener.open(base+image.replace('/image/','/download/')) as response:
            self.assertEqual(response.read(),b'synthetic-png')
            self.assertEqual(response.headers['Content-Disposition'],
                             'attachment; filename="sushiwait-authorization.png"')
        self.now+=181
        with self.assertRaises(urllib.error.HTTPError) as caught: opener.open(base+image)
        self.assertEqual(caught.exception.code,410)
        caught.exception.close()

    def test_console_cookie_expires_in_seven_days_without_saving_password(self):
        upstream=portal.Upstream(self.root,'synthetic-owner')
        cookie='C'*32
        with patch.object(upstream,'request',return_value=({},'yyb_session='+cookie+'; HttpOnly')), \
             patch.object(portal.time,'time',return_value=self.now):
            upstream.login('not-persisted-password')
            self.assertEqual(upstream.cookie(),cookie)
        raw=(self.root/'console-session.private.json').read_bytes()
        self.assertNotIn(b'not-persisted-password',raw)
        self.assertEqual((self.root/'console-session.private.json').stat().st_mode&0o777,0o600)
        with patch.object(portal.time,'time',return_value=self.now+portal.SESSION_SECONDS):
            with self.assertRaisesRegex(portal.PortalError,'login_required'): upstream.cookie()


if __name__ == '__main__': unittest.main()
