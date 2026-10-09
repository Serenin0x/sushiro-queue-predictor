"""Transient recovery keeps failed archives, shared budgets and business gates."""
import json
import ssl
import unittest

from sushiwait.remote import RemoteStore
from sushiwait.remotecampaign import campaign_config
from sushiwait.remotetasks import RemoteTaskError
import test_remote_campaign as campaign_fixture
import test_remote_window as fixture
from test_business_hours import rules


class RecoveryTests(unittest.TestCase):
    setUp = campaign_fixture.CampaignTests.setUp
    tearDown = campaign_fixture.CampaignTests.tearDown
    collect = campaign_fixture.CampaignTests.collect
    starts = campaign_fixture.CampaignTests.starts
    files = campaign_fixture.CampaignTests.files
    def config(self, *, duration=300, pairs=20, limit=3, hours=None):
        return campaign_config(self.root, self.plan, ['900001'], 60, duration,
            duration, pairs, now=self.clock.wall(), business_hours=hours,
            transient_recovery_limit=limit)

    def opener_with_failures(self, failures, status=503, error=None):
        original = self.opener.open
        remaining = [failures]
        def open_(request, *, timeout):
            if remaining[0]:
                remaining[0] -= 1
                self.opener.calls.append((request.full_url, self.clock.seconds))
                if error is not None:
                    raise error
                return fixture.Response(request, status)
            return original(request, timeout=timeout)
        self.opener.open = open_

    def test_single_transient_failure_creates_new_window_without_erasing_failure(self):
        self.opener_with_failures(1)
        result, _ = self.collect(self.config())
        self.assertEqual(self.starts(), [0, 60, 120, 180, 240])
        self.assertEqual((result['state'], result['failed_pairs'], result['successful_pairs']), ('completed', 1, 4))
        self.assertEqual(result['recorded_http_attempts'], 9)
        self.assertEqual(result['transient_recoveries_used'], 1)
        self.assertFalse(result['ok'])  # A completed imperfect day is not all-success.
        manifest = json.loads((self.root/'campaign.json').read_text())
        self.assertEqual(manifest['windows'][0]['summary']['state'], 'failed')
        self.assertIsNotNone(manifest['windows'][0]['database_digest'])
        with RemoteStore(self.root/'window-01'/'remote.sqlite3', read_only=True) as db:
            self.assertEqual(db.db.execute('SELECT count(*) FROM remote_samples WHERE ok=0').fetchone()[0], 1)

    def test_repeated_failure_backoff_and_limit_do_not_replenish_budget(self):
        self.opener_with_failures(10)
        result, _ = self.collect(self.config(duration=1000))
        self.assertEqual(self.starts(), [0, 60, 180, 420])
        self.assertEqual((result['state'], result['end_reason']), ('failed', 'query_failed'))
        self.assertEqual(result['completed_pair_slots'], 4)
        self.assertEqual(result['recorded_http_attempts'], 4)
        self.assertEqual(result['maximum_pair_budget'], 20)
        before=self.files()
        self.clock.seconds=500
        self.collect(self.config(duration=1000), resume=True)
        self.assertEqual(self.files(),before)
        self.assertEqual(len(self.opener.calls),4)

    def test_auth_rate_limit_schema_and_certificate_errors_are_not_retried(self):
        for status,error in [(401,None),(403,None),(429,None),(500,None),
                             (None,ssl.SSLCertVerificationError('certificate'))]:
            with self.subTest(status=status,error=type(error).__name__):
                # Each case gets a separate fixture and immutable state.
                self.tearDown();self.setUp();self.opener_with_failures(1,status=status,error=error)
                result,_=self.collect(self.config())
                self.assertEqual(result['end_reason'],'query_failed')
                self.assertEqual(self.starts(),[0])
        self.tearDown();self.setUp()
        class Bad(fixture.Response):
            def read(self,size):return b'{}'
        self.opener.open=lambda request,timeout:Bad(request,200)
        result,_=self.collect(self.config())
        self.assertEqual(result['end_reason'],'query_failed')
        self.assertEqual(result['windows_created'],1)

    def test_timeout_is_recoverable_but_failure_never_replayed(self):
        self.opener_with_failures(1,error=TimeoutError())
        result,_=self.collect(self.config(duration=150))
        self.assertEqual(self.starts(),[0,60,120])
        self.assertEqual(result['failed_pairs'],1)
        self.assertEqual(result['recorded_http_attempts'],5)

    def test_deadline_and_exhausted_budget_prevent_recovery_queries(self):
        self.opener_with_failures(1)
        result,_=self.collect(self.config(duration=30))
        self.assertEqual(self.starts(),[0]);self.assertEqual(result['end_reason'],'deadline')
        self.tearDown();self.setUp();self.opener_with_failures(1)
        result,_=self.collect(self.config(pairs=1))
        self.assertEqual(self.starts(),[0]);self.assertEqual(result['end_reason'],'budget')

    def test_restart_during_backoff_keeps_original_deadline_and_failed_bytes(self):
        self.opener_with_failures(1)
        config=self.config()
        result,_=self.collect(config,stop=lambda:self.clock.seconds>=20)
        self.assertEqual(result['state'],'active')
        before={p:p.read_bytes() for p in (self.root/'window-01').iterdir() if p.is_file()}
        deadline=result['deadline_at'];self.clock.seconds=30
        final,_=self.collect(config,resume=True)
        self.assertEqual(self.starts(),[0,90,150,210,270])
        self.assertEqual(final['deadline_at'],deadline)
        self.assertEqual({p:p.read_bytes() for p in before},before)

    def test_recovery_after_closing_waits_until_next_business_window(self):
        # Fixture BASE is local 20:00, close in 30 seconds, next day 11:00.
        hours=rules();hours['weekday_intervals']={day:[['11:00','20:01']] for day in '1234567'}
        hours['known_statutory_holiday_intervals']=[['11:00','20:01']]
        self.clock.seconds=30;self.opener_with_failures(1)
        result,_=self.collect(self.config(duration=16*3600,hours=hours),stop=lambda:len(self.opener.calls)>=3)
        self.assertEqual(self.starts(),[30,15*3600])
        self.assertEqual(result['failed_pairs'],1)

    def test_recovery_policy_is_immutable_and_invalid_limits_fail_before_queries(self):
        config=self.config();self.collect(config,stop=lambda:self.clock.seconds>=1)
        changed=dict(config);changed['transient_recovery_limit']=2
        with self.assertRaisesRegex(RemoteTaskError,'config_conflict'):
            self.collect(changed,resume=True)
        for limit in [-1,4,True]:
            with self.assertRaises(RemoteTaskError):self.config(limit=limit)

    def test_changed_failed_archive_is_rejected_before_a_new_query(self):
        self.opener_with_failures(1);config=self.config()
        self.collect(config,stop=lambda:self.clock.seconds>=20)
        path=self.root/'window-01'/'remote.sqlite3'
        with path.open('ab') as out:out.write(b'changed')
        before=len(self.opener.calls);self.clock.seconds=30
        with self.assertRaisesRegex(RemoteTaskError,'archive_changed'):
            self.collect(config,resume=True)
        self.assertEqual(len(self.opener.calls),before)

    def test_unknown_inflight_result_still_stops_without_retry(self):
        config=self.config()
        class Interrupted:
            def snapshot(self, store):raise RuntimeError('crash')
        with self.assertRaisesRegex(RuntimeError,'crash'):
            self.collect(config,factory=Interrupted)
        self.clock.seconds=10
        result,_=self.collect(config,resume=True,factory=lambda:(_ for _ in ()).throw(AssertionError('query')))
        self.assertEqual(result['end_reason'],'uncertain_attempt')
        self.assertEqual(result['completed_pair_slots'],1)
        self.assertEqual(self.opener.calls,[])
