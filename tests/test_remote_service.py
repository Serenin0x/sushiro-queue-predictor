"""Worker/API ownership, persistent budgets, stale views and ASGI lifecycle."""
import asyncio
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import io,json,threading,time
from pathlib import Path
import tempfile,unittest
from unittest.mock import patch

from sushiwait.cli import main
from sushiwait.remote import RemoteClient,RemoteStore,QUEUE_NAMES
from sushiwait.remoteservice import LiveRemoteView,RemoteQueueService,RemoteASGI,RemoteServiceError,serve_local
from sushiwait.remotetasks import remote_task_status

BASE=datetime(2026,10,6,12,tzinfo=timezone.utc)

class Response:
    headers={}
    def __init__(self,request,status=200):self.request,self.status=request,status
    def geturl(self):return self.request.full_url
    def getcode(self):return self.status
    def close(self):pass
    def read(self,size):
        value={name:['12','12','13-1'] for name in QUEUE_NAMES} if 'groupqueues?' in self.request.full_url else 0
        return json.dumps(value).encode()[:size]

class Opener:
    def __init__(self,fail=None):self.calls=[];self.fail=fail
    def open(self,request,*,timeout):
        self.calls.append(request.full_url)
        return Response(request,503 if self.fail and self.fail in request.full_url else 200)

def sample(opener=None):
    with patch('sushiwait.remote._utc',return_value=BASE.isoformat()):
        return RemoteClient(opener=opener or Opener()).snapshot('900001')

def wait_for(predicate):
    deadline=time.monotonic()+3
    while not predicate():
        if time.monotonic()>deadline:raise AssertionError('worker_wait_timeout')
        threading.Event().wait(.005)

async def request(app,path,method='GET',query=b'',body=b'',more=False):
    result=[]
    async def receive():return {'type':'http.request','body':body,'more_body':more}
    async def send(event):result.append(event)
    await app({'type':'http','method':method,'path':path,'query_string':query},receive,send)
    return result[0]['status'],json.loads(result[1]['body']),dict(result[0]['headers'])

class RemoteViewTests(unittest.TestCase):
    def setUp(self):self.view=LiveRemoteView(['900001'],stale_after_seconds=120)
    def get(self,now=BASE,**kw):
        return self.view.snapshot('900001',now=now,service_state=kw.get('state','running'),worker_alive=kw.get('alive',True))

    def test_unavailable_is_not_empty_queue_or_zero_wait(self):
        value=self.get();self.assertIsNone(value['fields']['groupqueues']['payload'])
        self.assertEqual(value['fields']['groupqueues']['state'],'unavailable')
        self.assertFalse(value['eta_available']);self.assertEqual(value['source_freshness'],'unknown')

    def test_committed_arrays_preserve_order_duplicates_suffixes_and_unknown_count(self):
        record=sample();record['private_secret']='should_disappear';record['queries']['groupqueues']['private_secret']='should_disappear'
        self.view.publish(record);record['queries']['groupqueues']['payload']['queues']['mixedQueue'].clear()
        value=self.get();fields=value['fields']
        self.assertEqual(fields['groupqueues']['payload']['queues']['mixedQueue'],['12','12','13-1'])
        self.assertEqual(fields['storequeuecount']['payload'],{'raw_count':0,'unit':'unknown'})
        self.assertNotIn('should_disappear',json.dumps(value));self.assertEqual(fields['groupqueues']['state'],'recent_response')
        fields['groupqueues']['payload']['queues']['mixedQueue'].clear()
        self.assertEqual(len(self.get()['fields']['groupqueues']['payload']['queues']['mixedQueue']),3)

    def test_failure_keeps_old_queues_explicitly_and_skipped_count(self):
        self.view.publish(sample());self.view.publish(sample(Opener('groupqueues?')))
        fields=self.get()['fields']
        self.assertEqual([x['state'] for x in fields.values()],['last_known_only']*2)
        self.assertEqual(fields['groupqueues']['latest_attempt']['http_status'],503)
        self.assertFalse(fields['storequeuecount']['latest_attempt']['attempted'])

    def test_partial_pair_updates_queues_but_keeps_count_with_own_state(self):
        self.view.publish(sample());self.view.publish(sample(Opener('storequeuecount?')))
        fields=self.get()['fields']
        self.assertEqual(fields['groupqueues']['state'],'recent_response')
        self.assertEqual(fields['storequeuecount']['state'],'last_known_only')

    def test_age_and_worker_stop_do_not_claim_upstream_freshness(self):
        self.view.publish(sample())
        self.assertEqual(self.get(BASE+timedelta(seconds=120))['fields']['groupqueues']['state'],'recent_response')
        self.assertEqual(self.get(BASE+timedelta(seconds=121))['fields']['groupqueues']['state'],'stale_response')
        self.assertEqual(self.get(state='completed',alive=False)['fields']['groupqueues']['state'],'last_known_only')
        self.assertEqual(self.get(BASE-timedelta(seconds=1))['fields']['groupqueues']['state'],'clock_invalid')

    def test_saved_history_remains_history_even_if_worker_started(self):
        self.view.publish(sample(),saved_history=True)
        self.assertEqual(self.get()['fields']['groupqueues']['state'],'saved_history')
        self.view.publish(sample());self.assertEqual(self.get()['fields']['groupqueues']['state'],'recent_response')

    def test_bad_record_does_not_replace_last_good_state(self):
        self.view.publish(sample());before=self.get()
        bad=sample();bad['queries']['groupqueues']['payload']['queues']['mixedQueue']=['<secret>']
        with self.assertRaises(ValueError):self.view.publish(bad)
        self.assertEqual(before,self.get())

    def test_wrong_store_and_clock_order_are_rejected(self):
        self.view.publish(sample());before=self.get();bad=sample();bad['requested_store_id']='900002'
        with self.assertRaises(RemoteServiceError):self.view.publish(bad)
        with patch('sushiwait.remote._utc',return_value=(BASE-timedelta(seconds=1)).isoformat()):
            bad=RemoteClient(opener=Opener()).snapshot('900001')
        with self.assertRaises(RemoteServiceError):self.view.publish(bad)
        self.assertEqual(before,self.get())

    def test_invalid_scope_or_age_bounds_fail(self):
        for ids,age in [([],120),(['900001']*2,120),(['900001'],0),(['900001'],True),(['900001'],7201)]:
            with self.assertRaises(ValueError):LiveRemoteView(ids,stale_after_seconds=age)


class RemoteServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.parent=Path(self.tmp.name).resolve()
        self.db=self.parent/'remote.sqlite3';self.task=self.parent/'task.json';self.services=[]
        self.opener=Opener()
    def tearDown(self):
        for service in self.services:service.shutdown()
        self.tmp.cleanup()
    def service(self,**kwargs):
        value=RemoteQueueService(db=self.db,task_file=self.task,store_ids=['900001'],interval=30,
            client_factory=lambda:RemoteClient(opener=self.opener),**kwargs)
        self.services.append(value);return value

    def test_reads_during_writer_lock_do_not_query_upstream_or_sqlite(self):
        service=self.service(samples=3);service.start()
        wait_for(lambda:service.status()['task']['completed_pair_slots']==1)
        with self.assertRaises(BlockingIOError):RemoteStore(self.db,read_only=True)
        before=len(self.opener.calls)
        for _ in range(10):
            status,value,headers=asyncio.run(request(RemoteASGI(service),'/api/v1/stores/900001/queue'))
            self.assertEqual(status,200);self.assertEqual(headers[b'cache-control'],b'no-store')
            self.assertEqual(value['fields']['groupqueues']['state'],'recent_response')
        self.assertEqual(len(self.opener.calls),before);self.assertEqual(before,2)
        text=json.dumps(service.status())+json.dumps(value)
        self.assertNotIn(str(self.db),text);self.assertNotIn(str(self.task),text)
        service.shutdown();self.assertEqual(service.status()['service_state'],'stopped')
        self.assertEqual(remote_task_status(self.task)['completed_pair_slots'],1)

    def test_completed_resume_never_creates_client_and_retains_history(self):
        service=self.service(samples=1);service.start();wait_for(lambda:service.status()['service_state']=='completed');service.shutdown()
        before=(self.db.read_bytes(),self.task.read_bytes())
        resumed=self.service(samples=1,resume=True)
        resumed.client_factory=lambda:(_ for _ in ()).throw(AssertionError('client_not_expected'))
        resumed.start();wait_for(lambda:resumed.status()['service_state']=='completed');resumed.shutdown()
        self.assertEqual(before,(self.db.read_bytes(),self.task.read_bytes()))
        self.assertEqual(resumed.store_view('900001')['fields']['groupqueues']['state'],'saved_history')
        self.assertEqual(len(self.opener.calls),2)

    def test_failed_query_stops_task_and_readiness(self):
        self.opener.fail='groupqueues?';service=self.service(samples=3);service.start()
        wait_for(lambda:service.status()['service_state']=='failed')
        self.assertEqual(len(self.opener.calls),1)
        code,value,_=asyncio.run(request(RemoteASGI(service),'/health'))
        self.assertEqual(code,503);self.assertEqual(value['task']['failed_pairs'],1)
        self.assertEqual(service.store_view('900001')['fields']['groupqueues']['state'],'unavailable')

    def test_duplicate_task_instance_cannot_launch_another_worker(self):
        first=self.service(samples=3);first.start();wait_for(lambda:first.status()['task']['completed_pair_slots']==1)
        second=self.service(samples=3,resume=True)
        with self.assertRaisesRegex(RemoteServiceError,'remote_task_busy'):second.start()
        self.assertEqual(len(self.opener.calls),2)
        self.assertEqual(first.status()['service_state'],'running')

    def test_shutdown_during_wait_preserves_remaining_budget_then_resume_waits(self):
        service=self.service(samples=3);service.start();wait_for(lambda:service.status()['task']['completed_pair_slots']==1);service.shutdown()
        waits=[]
        def halt(seconds,event):waits.append(seconds);event.set()
        resumed=self.service(samples=3,resume=True,wait=halt);resumed.start()
        wait_for(lambda:resumed.status()['service_state']=='stopped')
        self.assertEqual(len(self.opener.calls),2);self.assertTrue(waits and 29<waits[0]<=30)
        self.assertEqual(remote_task_status(self.task)['completed_pair_slots'],1)
        self.assertEqual(remote_task_status(self.task)['target_pair_slots'],3)

    def test_lifespan_start_and_shutdown_release_resources(self):
        service=self.service(samples=3);app=RemoteASGI(service)
        async def lifecycle():
            inputs=iter([{'type':'lifespan.startup'},{'type':'lifespan.shutdown'}]);events=[]
            async def receive():return next(inputs)
            async def send(event):events.append(event)
            await app({'type':'lifespan'},receive,send);return events
        self.assertEqual([x['type'] for x in asyncio.run(lifecycle())],['lifespan.startup.complete','lifespan.shutdown.complete'])
        self.assertFalse(service.status()['worker_alive'])
        with RemoteStore(self.db,read_only=True):pass

    def test_duplicate_lifespan_does_not_stop_original_worker(self):
        service=self.service(samples=3);service.start();wait_for(lambda:service.status()['task']['completed_pair_slots']==1)
        async def duplicate():
            events=[]
            async def receive():return {'type':'lifespan.startup'}
            async def send(event):events.append(event)
            await RemoteASGI(service)({'type':'lifespan'},receive,send);return events
        self.assertEqual(asyncio.run(duplicate())[0]['type'],'lifespan.startup.failed')
        self.assertTrue(service.status()['worker_alive']);self.assertEqual(len(self.opener.calls),2)

    def test_interrupted_commit_is_reconciled_before_resume_without_requery(self):
        from sushiwait.remotetasks import RemoteTask
        service=self.service(samples=3)
        original=RemoteTask.reconcile
        def interrupted(task,*,now,interrupted=False):
            if not interrupted:raise KeyboardInterrupt
            return original(task,now=now,interrupted=interrupted)
        with patch.object(RemoteTask,'reconcile',new=interrupted):
            service.start();wait_for(lambda:service.status()['service_state']=='failed')
        self.assertTrue(remote_task_status(self.task)['pending_attempt']);service.shutdown()
        waits=[]
        def halt(seconds,event):waits.append(seconds);event.set()
        resumed=self.service(samples=3,resume=True,wait=halt);resumed.start()
        wait_for(lambda:resumed.status()['service_state']=='stopped')
        self.assertEqual(len(self.opener.calls),2);self.assertEqual(remote_task_status(self.task)['completed_pair_slots'],1)
        self.assertFalse(remote_task_status(self.task)['pending_attempt']);self.assertTrue(waits)
        self.assertEqual(resumed.store_view('900001')['fields']['groupqueues']['state'],'saved_history')

    def test_shutdown_lets_inflight_pair_finish_but_stops_next_store(self):
        entered,release=threading.Event(),threading.Event()
        opener=self.opener;original=opener.open
        def blocked(request,*,timeout):
            if 'groupqueues?' in request.full_url:
                entered.set()
                if not release.wait(3):raise AssertionError('release_timeout')
            return original(request,timeout=timeout)
        opener.open=blocked
        service=RemoteQueueService(db=self.db,task_file=self.task,store_ids=['900001','900002'],
            interval=30,samples=1,client_factory=lambda:RemoteClient(opener=opener))
        self.services.append(service);service.start();self.assertTrue(entered.wait(3))
        stopper=threading.Thread(target=service.shutdown);stopper.start()
        self.assertTrue(service.stop_event.wait(3));release.set();stopper.join(3)
        self.assertFalse(stopper.is_alive());self.assertEqual(len(opener.calls),2)
        self.assertEqual(remote_task_status(self.task)['completed_pair_slots'],1)
        self.assertEqual(remote_task_status(self.task)['target_pair_slots'],2)

    def test_startup_failure_is_explicit_and_private_error_not_echoed(self):
        service=self.service(samples=1);service.task_file=self.parent/'missing'/'task.json'
        async def lifecycle():
            events=[]
            async def receive():return {'type':'lifespan.startup'}
            async def send(event):events.append(event)
            await RemoteASGI(service)({'type':'lifespan'},receive,send);return events
        events=asyncio.run(lifecycle());self.assertEqual(events[0]['type'],'lifespan.startup.failed')
        self.assertNotIn(str(self.parent),json.dumps(events));self.assertFalse(service.status()['worker_alive'])

    def test_read_routes_reject_unknown_stores_queries_bodies_and_writes(self):
        app=RemoteASGI(self.service(samples=1))
        for path,method,query,body,more,expected in [
                ('/api/v1/status','GET',b'',b'',False,200),('/health','GET',b'',b'',False,503),
                ('/api/v1/stores/900002/queue','GET',b'',b'',False,404),('/api/v1/stores/900001/queue','POST',b'',b'',False,405),
                ('/api/v1/status','GET',b'url=private',b'',False,400),('/api/v1/status','GET',b'',b'secret',False,400),
                ('/api/v1/status','GET',b'',b'',True,400),('/wrong','GET',b'',b'',False,404)]:
            status,value,_=asyncio.run(request(app,path,method,query,body,more));self.assertEqual(status,expected)
            self.assertNotIn('secret',json.dumps(value));self.assertEqual(self.opener.calls,[])

    def test_cli_bounds_fail_before_network_storage_or_runner(self):
        with patch('socket.socket',side_effect=AssertionError('socket')) as sock, \
                patch('sushiwait.remoteservice.RemoteTask',side_effect=AssertionError('task')) as task, \
                patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('auth')) as auth, \
                patch('sys.stdout',new_callable=io.StringIO) as out:
            for flags in [['--samples','121'],['--interval','29'],['--stale-after','0'],['--port','80']]:
                self.assertEqual(main(['remote-serve','--db',str(self.db),'--task-file',str(self.task),'--store-id','900001',*flags]),2)
        self.assertEqual((sock.call_count,task.call_count,auth.call_count),(0,0,0))
        self.assertNotIn(str(self.db),out.getvalue());self.assertFalse(self.task.exists())

    def test_optional_runner_has_fixed_local_configuration_and_cleanup(self):
        service=self.service(samples=1)
        class Runner:
            def run(self,app,**kw):
                self.kw=kw;self.app=app
        runner=Runner()
        with patch.dict('sys.modules',{'uvicorn':runner}):serve_local(service,port=8765)
        self.assertEqual(runner.kw['host'],'127.0.0.1');self.assertEqual(runner.kw['workers'],1)
        self.assertEqual(runner.kw['lifespan'],'on');self.assertFalse(runner.kw['access_log'])
        self.assertEqual(self.opener.calls,[])

if __name__=='__main__':unittest.main()
