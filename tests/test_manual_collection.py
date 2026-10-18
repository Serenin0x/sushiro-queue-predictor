"""Manual requests use real private queues and the existing synthetic fleet ledger."""
import asyncio
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sushiwait.manualcollection import ManualCollection, ManualError, COOKIE, SESSION_SECONDS, verify_owner
import test_fleet_isolation as fleet_fixture
from test_statistics_gateway import gateway, Hub
fleet, daily = fleet_fixture.fleet, fleet_fixture.daily


class ManualCollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.root.chmod(0o700)
        self.now=datetime.fromisoformat('2026-10-18T12:00:00+08:00')
        self.plan=daily.make_plan(fleet_fixture.FiveCityScopeTests().expanded(),self.now)
        folder=self.root/'day';folder.mkdir(mode=0o700)
        self.f=fleet.Fleet(folder,self.plan,0,self.now);self.addCleanup(self.f.close)
        self.q=ManualCollection(self.root/'manual.sqlite',self.plan['store_ids'],clock=lambda:self.now.timestamp())
        self.net=patch('socket.create_connection',side_effect=AssertionError('no real source calls')).start()
        self.addCleanup(patch.stopall)

    def submit(self, store=None, identity='a'*32, **kwargs):
        return self.q.submit(identity,store,self.now,**kwargs)

    def record(self,store,ok=True):
        self.f.reserve(store)
        payload={} if ok else {'http_status':504,'error_code':'http_error','failure_phase':'request'}
        self.f.db._insert(store,'live','miniapp_gateway',self.now.isoformat(),ok,payload)
        self.f.reconcile()

    def test_all_scope_excludes_isolation_but_is_not_a_city_filter(self):
        excluded=self.plan['store_ids'][:2];job=self.submit(excluded=excluded)
        self.assertEqual(job['total'],81)
        self.assertFalse(set(excluded)&{t['store_id'] for t in job['targets']})
        self.assertEqual(self.f.value['issued'],0);self.net.assert_not_called()

    def test_single_store_and_invalid_scope_cannot_expand_fleet(self):
        store=self.plan['store_ids'][-1];self.assertEqual(self.submit(store)['total'],1)
        for identity,target in [(None,store),('bad',store),(1,store),('b'*32,'999999')]:
            with self.assertRaises(ManualError):self.q.submit(identity,target,self.now)

    def test_idempotency_merge_and_conflict_do_not_add_requests(self):
        first=self.submit();self.assertEqual(self.submit()['request_id'],first['request_id'])
        self.assertEqual(self.submit(self.plan['store_ids'][0],'b'*32)['request_id'],first['request_id'])
        with self.assertRaises(ManualError):self.submit(self.plan['store_ids'][0])
        self.assertEqual(self.f.value['issued'],0)

    def test_second_different_single_job_waits_for_active_job(self):
        self.submit(self.plan['store_ids'][0])
        with self.assertRaisesRegex(ManualError,'job_running'):self.submit(self.plan['store_ids'][1],'b'*32)

    def test_new_query_has_original_reservation_budget_and_archive_row(self):
        store=self.plan['store_ids'][-1];self.submit(store)
        self.assertEqual(self.q.next_store(self.f,self.now),store)
        self.record(store);self.assertIsNone(self.q.next_store(self.f,self.now))
        result=self.q.status();self.assertEqual((result['state'],result['successful']),('completed',1))
        self.assertEqual(self.f.value['issued'],1);self.assertIsNone(self.f.value['pending'])
        self.assertEqual(self.f.db.db.execute('SELECT count(*) FROM samples').fetchone()[0],1)
        self.assertGreater(fleet.instant(self.f.value['stores'][store][0]),self.now)

    def test_automatic_sample_after_submission_satisfies_manual_target_once(self):
        store=self.plan['store_ids'][0];self.submit(store);self.record(store)
        self.assertIsNone(self.q.next_store(self.f,self.now));self.assertEqual(self.q.status()['successful'],1)
        self.assertEqual(self.f.value['issued'],1)

    def test_old_sample_cannot_count_as_a_new_manual_observation(self):
        store=self.plan['store_ids'][0];self.record(store);self.now+=timedelta(seconds=1);self.submit(store)
        self.assertEqual(self.q.next_store(self.f,self.now),store);self.assertEqual(self.q.status()['successful'],0)

    def test_progress_recovers_after_restart_without_second_query(self):
        store=self.plan['store_ids'][0];self.submit(store);self.record(store)
        restored=ManualCollection(self.q.path,self.plan['store_ids'],clock=lambda:self.now.timestamp())
        self.assertIsNone(restored.next_store(self.f,self.now));self.assertEqual(restored.status()['successful'],1)
        self.assertEqual(self.f.value['issued'],1)

    def test_failure_is_recorded_and_other_targets_continue(self):
        stores=self.plan['store_ids'];self.submit();self.record(stores[0],False)
        target=self.q.next_store(self.f,self.now)
        self.assertNotEqual(target,stores[0]);self.assertEqual(self.q.status()['failed'],1)
        self.assertEqual(self.f.value['stores'][stores[0]][2],1);self.assertIsNotNone(target)

    def test_newly_isolated_store_is_skipped_and_never_reenabled(self):
        store=self.plan['store_ids'][0];self.submit(store);self.f.value['stores'][store][2]=1
        self.assertIsNone(self.q.next_store(self.f,self.now));self.assertEqual(self.q.status()['skipped'],1)
        self.assertEqual(self.f.value['stores'][store][2],1)

    def test_shared_backoff_and_existing_budget_are_authoritative(self):
        store=self.plan['store_ids'][0];self.submit(store)
        self.f.value['rate_limit_until']=(self.now+timedelta(minutes=15)).isoformat()
        self.assertIsNone(self.q.next_store(self.f,self.now));self.assertEqual(self.q.status()['pending'],1)
        self.f.value['issued']=self.plan['ordinary_query_limit']
        self.assertIsNone(self.q.next_store(self.f,self.now));self.assertEqual(self.q.status()['targets'][0]['error_code'],'query_budget_exhausted')

    def test_close_expiry_and_stop_cannot_replay_tomorrow(self):
        self.submit(self.plan['store_ids'][0]);self.now+=timedelta(hours=1)
        self.assertIsNone(self.q.next_store(self.f,self.now));self.assertEqual(self.q.status()['error_code'],'manual_expired')
        self.assertIsNone(self.q.next_store(self.f,self.now+timedelta(days=1)))
        self.submit(self.plan['store_ids'][0],'b'*32);self.q.stop_active('business_closed',self.now)
        self.assertEqual(self.q.status()['targets'][0]['error_code'],'business_closed')

    def test_session_expiry_logout_and_password_not_persisted(self):
        token=self.q.login('synthetic-peer','only-a-test-password',lambda p:p=='only-a-test-password')
        self.q.authenticate(token);self.assertNotIn(b'only-a-test-password',self.q.path.read_bytes())
        self.assertNotIn(token.encode(),self.q.path.read_bytes());self.q.logout(token)
        with self.assertRaises(ManualError):self.q.authenticate(token)
        token=self.q.login('synthetic-peer','synthetic',lambda _:True);self.now+=timedelta(seconds=SESSION_SECONDS)
        with self.assertRaises(ManualError):self.q.authenticate(token)

    def test_login_throttle_reserves_even_failed_outcomes(self):
        verify=Mock(return_value=False)
        for _ in range(5):
            with self.assertRaisesRegex(ManualError,'login_failed'):self.q.login('synthetic-peer','bad',verify)
        with self.assertRaisesRegex(ManualError,'login_limited'):self.q.login('synthetic-peer','bad',verify)
        self.assertEqual(verify.call_count,5)

    def test_manual_query_preserves_future_automatic_capacity(self):
        store=self.plan['store_ids'][0];self.submit(store)
        rounds=self.plan['ordinary_query_limit']//len(self.plan['store_ids'])
        self.f.value['stores'][store][1]=rounds-1;self.f.value['issued']=rounds-1
        self.assertIsNone(self.q.next_store(self.f,self.now))
        self.assertEqual(self.q.status()['error_code'],'awaiting_automatic_slot')
        self.assertIsNotNone(self.f.due(self.now),'automatic sampling must still run')
        self.assertEqual(self.f.value['issued'],rounds-1)

    def test_a_dead_collector_does_not_leave_a_job_running_forever(self):
        self.submit();self.now+=timedelta(minutes=31)
        self.assertEqual(self.q.status()['state'],'completed');self.assertEqual(self.q.status()['error_code'],'manual_expired')

    def test_unsafe_or_unrelated_database_is_not_adopted(self):
        import sqlite3
        other=self.root/'other.sqlite'
        with sqlite3.connect(other) as db:db.execute('CREATE TABLE unrelated (id INTEGER)')
        other.chmod(0o600)
        with self.assertRaisesRegex(ValueError,'database_schema'):ManualCollection(other,self.plan['store_ids'])
        other.chmod(0o644)
        with self.assertRaisesRegex(ValueError,'database_unsafe'):ManualCollection(other,self.plan['store_ids'])

    def test_owner_password_check_has_one_fixed_loopback_route(self):
        response=Mock();response.read.return_value=b'{"code":0}';response.headers={'Set-Cookie':'yyb_session='+('x'*32)}
        opener=Mock();opener.open.return_value.__enter__=Mock(return_value=response);opener.open.return_value.__exit__=Mock(return_value=False)
        with patch('sushiwait.manualcollection.urllib.request.build_opener',return_value=opener):
            self.assertTrue(verify_owner('synthetic-owner','synthetic-only'))
        request=opener.open.call_args.args[0]
        self.assertEqual(request.full_url,'http://127.0.0.1:18081/login')
        self.assertEqual(json.loads(request.data)['username'],'synthetic-owner')

    def test_source_run_uses_same_observer_and_paces_manual_request(self):
        store=self.plan['store_ids'][-1];self.submit(store)
        config=self.root/'manual-collection.json';config.write_text(json.dumps({'schema_version':1,'queue_file':str(self.q.path),'owner_username':'synthetic-owner'}));config.chmod(0o600)
        day=self.root/'runner';day.mkdir(mode=0o700);stopped=Mock();stopped.is_set.side_effect=[False,True]
        campaign=SimpleNamespace(validate_plan=daily.campaign.validate_plan,legacy=Mock())
        context=SimpleNamespace(revision=1,authorization=None);campaign.legacy.Controller.return_value.context.return_value=(context,'synthetic-digest')
        service=SimpleNamespace(campaign=campaign,reauthorization=SimpleNamespace(task_usage=lambda *_a,**_k:(0,0)),publish_status=Mock(),CHINA=daily.CHINA)
        observed=[]
        def observe(_client,target,db,**_kwargs):
            observed.append(target);db._insert(target,'live','miniapp_gateway',self.now.isoformat(),True,{})
        with patch.object(fleet,'datetime') as clock,patch.object(fleet.cli,'observe',side_effect=observe),patch.object(fleet.cli,'client_for'),patch.object(fleet.cli,'describe_authorization'),patch.object(fleet.cli,'_authorization_stop_code',return_value=None):
            clock.now.return_value=self.now;clock.fromisoformat.side_effect=datetime.fromisoformat;fleet.run(service,self.root,day,self.plan,Mock(),stopped)
        self.assertEqual(observed,[store]);self.assertEqual(self.q.status()['successful'],1)
        self.assertTrue(any(0<=c.args[0]<=1.4 for c in stopped.wait.call_args_list))


class ManualGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name).resolve();root.chmod(0o700)
        config=root/'settings.json';config.write_text(json.dumps({'schema_version':1,'queue_file':str(root/'queue.sqlite'),'owner_username':'synthetic-owner'}));config.chmod(0o600)
        self.config=config
        self.key=root/'internal.key';self.key.write_bytes(b'k'*32);self.key.chmod(0o600)
        self.app=gateway.StatisticsGateway(Hub(),manual_collection_config=config)
        self.app.collection_health=lambda:{'state':'running','unavailable_store_ids':[]}

    def request(self,value,*,origin='https://124.222.57.236',scheme='https',cookie='',method='POST',query=b''):
        async def run():
            replies=[]
            async def receive():return {'type':'http.request','body':json.dumps(value).encode()}
            async def send(message):replies.append(message)
            await self.app({'type':'http','method':method,'path':'/api/v1/collection/manual','scheme':scheme,'query_string':query,'headers':[(b'origin',origin.encode()),(b'content-type',b'application/json'),(b'cookie',cookie.encode())],'client':('synthetic-peer',1)},receive,send)
            return replies[0],json.loads(replies[1]['body']) if replies[1]['body'].startswith(b'{') else None
        return asyncio.run(run())

    def login(self,remember=False):
        with patch('sushiwait.manualcollection.verify_owner',return_value=True):
            response,_=self.request({'schema_version':1,'action':'login','password':'synthetic','remember':remember})
        self.assertEqual(response['status'],200)
        raw=dict(response['headers'])[b'set-cookie'].decode();cookie=SimpleCookie();cookie.load(raw)
        self.assertIn('Secure',raw);self.assertIn('HttpOnly',raw);self.assertIn('SameSite=Strict',raw)
        self.assertEqual('Max-Age' in raw,remember)
        return f'{COOKIE}={cookie[COOKIE].value}'

    def test_login_is_required_and_get_never_enqueues(self):
        value={'schema_version':1,'action':'request','request_id':'a'*32,'store_id':None}
        self.assertEqual(self.request(value)[0]['status'],401)
        self.assertEqual(self.request(value,method='GET')[0]['status'],404)
        self.assertIsNone(self.app.manual.status())

    def test_wrong_origin_http_query_and_unknown_actions_are_rejected(self):
        value={'schema_version':1,'action':'status'};cookie=self.login()
        for kwargs in [dict(origin='https://other.invalid'),dict(scheme='http'),dict(query=b'ignored=1')]:
            self.assertEqual(self.request(value,cookie=cookie,**kwargs)[0]['status'],400)
        self.assertEqual(self.request({**value,'action':'anything'},cookie=cookie)[0]['status'],400)

    def test_authenticated_request_returns_progress_without_observing_source(self):
        cookie=self.login(True)
        with patch('socket.create_connection',side_effect=AssertionError('no source HTTP')):
            response,value=self.request({'schema_version':1,'action':'request','request_id':'a'*32,'store_id':'3014'},cookie=cookie)
        self.assertEqual(response['status'],200);self.assertEqual(value['job']['total'],1)
        self.assertEqual(self.request({'schema_version':1,'action':'status'},cookie=cookie)[1]['job']['request_id'],'a'*32)

    def test_closed_or_stale_collector_cannot_accept_queries(self):
        cookie=self.login();self.app.collection_health=lambda:{'state':'closed'}
        response,value=self.request({'schema_version':1,'action':'request','request_id':'a'*32,'store_id':None},cookie=cookie)
        self.assertEqual(response['status'],409);self.assertEqual(value['error_code'],'manual_business_closed')
        self.assertIsNone(self.app.manual.status())

    def test_logout_revokes_only_the_manual_session(self):
        cookie=self.login();response,_=self.request({'schema_version':1,'action':'logout'},cookie=cookie)
        self.assertEqual(response['status'],200);self.assertIn(b'Max-Age=0',dict(response['headers'])[b'set-cookie'])
        self.assertEqual(self.request({'schema_version':1,'action':'status'},cookie=cookie)[0]['status'],401)


class ProactiveAuthorizationGatewayTests(unittest.TestCase):
    login=ManualGatewayTests.login
    request=ManualGatewayTests.request
    def setUp(self):
        ManualGatewayTests.setUp(self)
        self.app=gateway.StatisticsGateway(Hub(),manual_collection_config=self.config,reauthorization_key_file=self.key)
        self.app.collection_health=lambda:{'state':'closed'}

    def test_closed_owner_can_issue_link_without_queue_or_source_calls(self):
        cookie=self.login();value={'schema_version':1,'action':'reauthorize'}
        with patch('sushiwait.authorizationlink.issue_link',return_value='/recover/#'+'A'*43) as issue:
            self.assertEqual(self.request(value)[0]['status'],401);issue.assert_not_called()
            response,body=self.request(value,cookie=cookie)
            self.assertEqual(response['status'],200);self.assertEqual(body['path'],'/recover/#'+'A'*43)
            issue.assert_called_once_with(self.key,'https://124.222.57.236')
        self.assertIsNone(self.app.manual.status())

    def test_issue_cannot_override_scope_and_remains_origin_protected(self):
        cookie=self.login();value={'schema_version':1,'action':'reauthorize'}
        with patch('sushiwait.authorizationlink.issue_link') as issue:
            for kwargs in [dict(origin='https://other.invalid'),dict(scheme='http'),dict(query=b'x=1'),dict(method='GET')]:
                self.assertNotEqual(self.request(value,cookie=cookie,**kwargs)[0]['status'],200)
            for extra in [{'account':'2'},{'verification':False},{'url':'https://other.invalid'}]:
                self.assertEqual(self.request({**value,**extra},cookie=cookie)[0]['status'],400)
            issue.assert_not_called()

    def test_existing_fault_is_not_cleared_and_private_errors_are_not_exposed(self):
        cookie=self.login()
        with patch('sushiwait.authorizationlink.issue_link',side_effect=ManualError('reauthorization_blocked',409)):
            response,body=self.request({'schema_version':1,'action':'reauthorize'},cookie=cookie)
        self.assertEqual(response['status'],409);self.assertEqual(body,{'error_code':'reauthorization_blocked'})
        self.assertIsNone(self.app.manual.status())

    def test_reauthorization_requires_configured_owner_authentication(self):
        with self.assertRaises(ValueError):gateway.StatisticsGateway(Hub(),reauthorization_key_file=self.key)
