"""Persisted deadlines/budgets and shared real-start schedules, synthetic clocks."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import io,json,os,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

from sushiwait.remote import RemoteClient,RemoteStore,QUEUE_NAMES
from sushiwait.remotetasks import RemoteTaskError
from sushiwait.remotewindow import (MAX_DURATION,MAX_PAIRS,RemoteWindowTask,PersistentWindowSchedule,
    collect_remote_window,window_config,remote_window_status,window_status)
from sushiwait.cli import main

BASE=datetime(2026,10,6,12,tzinfo=timezone.utc)
class Clock:
    def __init__(self):self.seconds=0;self.sleeps=[];self.jump=0
    def wall(self):return BASE+timedelta(seconds=self.seconds)
    def mono(self):return self.seconds
    def sleep(self,seconds):self.sleeps.append(seconds);self.seconds+=max(seconds,self.jump);self.jump=0
class Response:
    headers={}
    def __init__(self,request,status):self.request,self.status=request,status
    def geturl(self):return self.request.full_url
    def getcode(self):return self.status
    def close(self):pass
    def read(self,size):return json.dumps({k:[] for k in QUEUE_NAMES} if 'groupqueues?' in self.request.full_url else 0).encode()[:size]
class Opener:
    def __init__(self,clock):self.clock=clock;self.calls=[];self.fail=False
    def open(self,request,*,timeout):self.calls.append((request.full_url,self.clock.seconds));return Response(request,503 if self.fail else 200)

class RemoteWindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.parent=Path(self.tmp.name).resolve()
        self.db=self.parent/'remote.sqlite3';self.path=self.parent/'task.json';self.plan=self.parent/'plans.json'
        self.plan.write_text(json.dumps({'schema_version':1,'plans':[]}));self.plan.chmod(0o600)
        self.clock=Clock();self.opener=Opener(self.clock);self.client=RemoteClient(opener=self.opener)
        self.utc=patch('sushiwait.remote._utc',side_effect=lambda:self.clock.wall().isoformat());self.utc.start()
    def tearDown(self):self.utc.stop();self.tmp.cleanup()
    def config(self,stores=('900001',),base=60,duration=180,pairs=20):
        return window_config(self.db,self.plan,list(stores),base,duration,pairs,now=self.clock.wall())
    def document(self,plans):
        self.plan.write_text(json.dumps({'schema_version':1,'plans':plans}));self.plan.chmod(0o600)
    def diner(self,seconds=960,store='900001',**kw):
        return {'store_id':store,'desired_arrival_at':(BASE+timedelta(seconds=seconds)).isoformat(),**kw}
    def collect(self,config=None,resume=False,stop=lambda:False):
        config=config or self.config();events=[]
        with RemoteWindowTask(self.path,config=config,resume=resume,now=self.clock.wall()) as task:
            task.prepare_database()
            with RemoteStore(self.db) as database:
                task.bind(database,now=self.clock.wall())
                result=collect_remote_window(task,self.client,wall_clock=self.clock.wall,monotonic_clock=self.clock.mono,
                    sleep=self.clock.sleep,emit=events.append,should_stop=stop)
        return result,events
    def starts(self,store='900001'):
        return [at for url,at in self.opener.calls if 'groupqueues?' in url and url.endswith('storeid='+store)]
    def pending(self,config=None,*,saved=True):
        config=config or self.config()
        with RemoteWindowTask(self.path,config=config,resume=False,now=self.clock.wall()) as task:
            task.prepare_database()
            with RemoteStore(self.db) as database:
                task.bind(database,now=self.clock.wall());task.begin('900001',now=self.clock.wall())
                if saved:database.append(self.client.snapshot('900001'))
        return config

    def test_background_empty_plans_records_until_exact_deadline(self):
        result,_=self.collect();self.assertTrue(result['ok']);self.assertEqual(self.starts(),[0,60,120])
        self.assertEqual(result['end_reason'],'deadline');self.assertEqual(result['recorded_http_attempts'],6)
        self.assertEqual(self.clock.seconds,180)
    def test_two_same_store_plans_coalesce_60_then_30_seconds(self):
        self.document([self.diner(),self.diner()]);result,_=self.collect()
        self.assertEqual(self.starts(),[0,60,90,120,150]);self.assertEqual(result['successful_pairs'],5)
    def test_unplanned_store_keeps_background_cadence(self):
        self.document([self.diner(seconds=600)])
        result,_=self.collect(self.config(stores=('900001','900002'),base=120,duration=150))
        self.assertEqual(self.starts(),[0,30,60,90,120]);self.assertEqual(self.starts('900002'),[0,120])
        self.assertEqual(result['successful_pairs'],7)
    def test_terminal_plan_returns_to_background_collection(self):
        self.document([self.diner(seconds=600,plan_status='called')]);self.collect()
        self.assertEqual(self.starts(),[0,60,120])
    def test_nearest_window_boundary_wakes_before_background_target(self):
        self.document([self.diner(seconds=1860)])
        self.collect(self.config(base=300,duration=121));self.assertEqual(self.starts(),[0,60,120])
        self.assertEqual(self.clock.sleeps[0],60)
    def test_budget_is_shared_across_stores_without_exceeding(self):
        result,_=self.collect(self.config(stores=('900001','900002'),pairs=3))
        self.assertEqual(result['successful_pairs'],3);self.assertEqual(result['end_reason'],'budget')
        self.assertEqual(len(self.opener.calls),6);self.assertEqual(self.starts('900002'),[0])
    def test_resume_saved_pending_once_waits_and_keeps_original_deadline(self):
        config=self.pending();original=remote_window_status(self.path)['deadline_at'];self.clock.seconds=10
        result,_=self.collect(config,resume=True)
        self.assertEqual(self.starts(),[0,70,130]);self.assertEqual(result['successful_pairs'],3)
        self.assertEqual(result['deadline_at'],original);self.assertEqual(self.clock.seconds,180)
        self.assertEqual(result['uncertain_pair_slots'],0)
    def test_unknown_pending_consumes_one_slot_without_replay(self):
        config=self.pending(self.config(pairs=3),saved=False);self.clock.seconds=10
        result,_=self.collect(config,resume=True)
        self.assertEqual(self.starts(),[70,130]);self.assertFalse(result['ok'])
        self.assertEqual(result['uncertain_pair_slots'],1);self.assertEqual(result['completed_pair_slots'],3)
        self.assertEqual(result['last_gap']['reason'],'uncertain_attempt')
        self.assertEqual(result['unrecorded_http_attempts'],'unknown')
    def test_expired_restart_reconciles_then_makes_no_requests(self):
        config=self.pending();self.clock.seconds=200;before=len(self.opener.calls)
        result,_=self.collect(config,resume=True)
        self.assertEqual(len(self.opener.calls),before);self.assertEqual(result['successful_pairs'],1)
        self.assertEqual(result['end_reason'],'deadline')
    def test_completed_resume_does_not_mutate_files(self):
        config=self.config(pairs=1);self.collect(config);before=(self.db.read_bytes(),self.path.read_bytes())
        self.clock.seconds=5;result,_=self.collect(config,resume=True)
        self.assertTrue(result['ok']);self.assertEqual(before,(self.db.read_bytes(),self.path.read_bytes()))
        self.assertEqual(len(self.opener.calls),2)
    def test_fail_stop_is_persisted_and_cannot_be_retried(self):
        config=self.config();self.opener.fail=True;result,_=self.collect(config)
        self.assertEqual(result['state'],'failed');self.assertEqual(len(self.opener.calls),1)
        self.clock.seconds=30;self.opener.fail=False;self.collect(config,resume=True)
        self.assertEqual(len(self.opener.calls),1)
    def test_jump_has_one_current_sample_no_catchup_burst(self):
        self.clock.jump=130;self.collect(self.config(duration=250))
        self.assertEqual(self.starts(),[0,130,190]);self.assertEqual(self.clock.seconds,250)
    def test_resume_at_15_minute_boundary_applies_current_shorter_interval(self):
        self.document([self.diner()]);config=self.pending();self.clock.seconds=55
        self.collect(config,resume=True)
        self.assertEqual(self.starts(),[0,85,115,145,175])
    def test_clock_forward_jump_does_not_bypass_monotonic_spacing(self):
        config=self.config(duration=3600)
        with RemoteWindowTask(self.path,config=config,resume=False,now=self.clock.wall()) as task:
            with RemoteStore(self.db) as database:
                task.bind(database,now=self.clock.wall());schedule=PersistentWindowSchedule(task,wall=self.clock.wall(),monotonic=0)
                schedule.mark('900001',wall=self.clock.wall(),monotonic=0);database.append(self.client.snapshot('900001'));task.reconcile(now=self.clock.wall())
                decision=schedule.decision(wall=BASE+timedelta(seconds=600),monotonic=1)
                self.assertEqual(decision['due_stores'],[]);self.assertEqual(decision['wake_monotonic'],60)
                with self.assertRaises(RemoteTaskError):schedule.decision(wall=BASE+timedelta(seconds=599),monotonic=2)
    def test_monotonic_duration_stops_even_if_wall_clock_is_slow(self):
        config=self.config(duration=30)
        with RemoteWindowTask(self.path,config=config,resume=False,now=self.clock.wall()) as task:
            with RemoteStore(self.db) as database:
                task.bind(database,now=self.clock.wall());schedule=PersistentWindowSchedule(task,wall=self.clock.wall(),monotonic=0)
                self.assertTrue(schedule.decision(wall=BASE+timedelta(seconds=29),monotonic=30)['done'])
                self.assertEqual(task.value['end_reason'],'monotonic_duration')
    def test_changed_plan_digest_or_configuration_refuses_resume(self):
        config=self.pending();self.document([self.diner()]);self.clock.seconds=1
        with self.assertRaisesRegex(RemoteTaskError,'plan_changed'):
            RemoteWindowTask(self.path,config=config,resume=True,now=self.clock.wall())
        new=self.config()
        with self.assertRaisesRegex(RemoteTaskError,'config_conflict'):
            RemoteWindowTask(self.path,config=new,resume=True,now=self.clock.wall())
        self.assertEqual(len(self.opener.calls),2)
    def test_old_committed_record_mutation_is_detected_on_restart(self):
        config=self.config(pairs=2);self.collect(config)
        database=sqlite3.connect(self.db);database.execute("UPDATE remote_samples SET run_id='00000000-0000-0000-0000-000000000001' WHERE id=1");database.commit();database.close()
        self.clock.seconds=61
        with self.assertRaisesRegex(RemoteTaskError,'database_changed'):self.collect(config,resume=True)
    def test_external_sqlite_commit_is_detected_before_next_http(self):
        config=self.config()
        with RemoteWindowTask(self.path,config=config,resume=False,now=self.clock.wall()) as task:
            with RemoteStore(self.db) as database:
                task.bind(database,now=self.clock.wall());task.begin('900001',now=self.clock.wall())
                database.append(self.client.snapshot('900001'));task.reconcile(now=self.clock.wall())
                other=sqlite3.connect(self.db);other.execute("UPDATE remote_samples SET run_id='00000000-0000-0000-0000-000000000001'");other.commit();other.close()
                self.clock.seconds=60
                with self.assertRaisesRegex(RemoteTaskError,'database_changed'):task.begin('900001',now=self.clock.wall())
    def test_in_process_plan_change_stops_before_next_query(self):
        config=self.config()
        with RemoteWindowTask(self.path,config=config,resume=False,now=self.clock.wall()) as task:
            with RemoteStore(self.db) as database:
                task.bind(database,now=self.clock.wall());self.document([self.diner()]);self.clock.seconds=60
                with self.assertRaisesRegex(RemoteTaskError,'plan_changed'):task.begin('900001',now=self.clock.wall())
                self.assertEqual(self.opener.calls,[])
    def test_wrong_pending_run_or_extra_tail_refuses_recovery(self):
        config=self.pending();database=sqlite3.connect(self.db)
        database.execute("UPDATE remote_samples SET run_id='00000000-0000-0000-0000-000000000001'");database.commit();database.close()
        self.clock.seconds=1
        with self.assertRaisesRegex(RemoteTaskError,'result_conflict'):self.collect(config,resume=True)
    def test_database_file_replacement_is_not_recovered(self):
        config=self.pending();original=self.db.read_bytes();self.db.unlink();self.db.write_bytes(original);self.db.chmod(0o600)
        self.clock.seconds=1
        with self.assertRaisesRegex(RemoteTaskError,'database_changed'):self.collect(config,resume=True)
    def test_status_has_no_plan_times_paths_or_hashes_and_does_not_query(self):
        self.document([self.diner()]);config=self.pending();before=(self.db.read_bytes(),self.path.read_bytes(),self.plan.read_bytes())
        with patch('socket.socket',side_effect=AssertionError('socket')) as sock,patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('auth')) as auth:
            value=remote_window_status(self.path)
        text=json.dumps(value)
        for forbidden in (str(self.plan),str(self.db),config['plan_digest'],self.diner()['desired_arrival_at']):self.assertNotIn(forbidden,text)
        self.assertEqual((sock.call_count,auth.call_count),(0,0));self.assertEqual(before,(self.db.read_bytes(),self.path.read_bytes(),self.plan.read_bytes()))
    def test_72_hour_config_and_budget_boundary_are_explicit(self):
        config=self.config(duration=MAX_DURATION,pairs=MAX_PAIRS)
        with RemoteWindowTask(self.path,config=config,resume=False,now=self.clock.wall()) as task:
            self.assertEqual((_decode_delta(task.value),task.value['config']['max_pairs']),(259200,25920))
        for kw in [{'duration':MAX_DURATION+1},{'pairs':MAX_PAIRS+1},{'base':59},{'duration':True}]:
            with self.assertRaises(ValueError):self.config(**kw)
    def test_borrowed_poll_start_or_out_of_scope_plans_fail_before_storage(self):
        self.document([self.diner(store='900002')])
        with self.assertRaises(ValueError):self.config()
        self.plan.write_text(json.dumps({'schema_version':1,'plans':[],'last_poll_started_at':{'900001':BASE.isoformat()}}))
        with self.assertRaises(ValueError):self.config()
        self.assertFalse(self.db.exists());self.assertFalse(self.path.exists())
    def test_task_plan_path_conflict_refuses_write(self):
        config=self.config();before=self.plan.read_bytes()
        with self.assertRaisesRegex(RemoteTaskError,'path_conflict'):
            RemoteWindowTask(self.plan,config=config,resume=False,now=self.clock.wall())
        self.assertEqual(before,self.plan.read_bytes())
    def test_cli_with_fake_transport_uses_deadline_and_offline_status(self):
        with patch('sushiwait.remote.RemoteClient',return_value=self.client),patch('sushiwait.cli._utc_clock',side_effect=self.clock.wall), \
                patch('sushiwait.cli.time.monotonic',side_effect=self.clock.mono),patch('sushiwait.cli.time.sleep',side_effect=self.clock.sleep), \
                patch('socket.socket',side_effect=AssertionError('socket')) as sock,patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('auth')) as auth, \
                patch('sys.stdout',new_callable=io.StringIO) as out:
            code=main(['remote-window-collect','--db',str(self.db),'--task-file',str(self.path),'--plan-file',str(self.plan),
                '--store-id','900001','--duration','121','--base-interval','60','--max-pairs','10'])
            self.assertEqual(code,0);self.assertEqual(main(['remote-window-status','--task-file',str(self.path)]),0)
        self.assertEqual(self.starts(),[0,60,120]);self.assertEqual((sock.call_count,auth.call_count),(0,0))
        self.assertNotIn(str(self.plan),out.getvalue())
        before=self.db.read_bytes(),self.path.read_bytes()
        with patch('sushiwait.remote.RemoteClient',side_effect=AssertionError('terminal_client')) as factory, \
                patch('sushiwait.cli._utc_clock',side_effect=self.clock.wall),patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(main(['remote-window-collect','--db',str(self.db),'--task-file',str(self.path),
                '--plan-file',str(self.plan),'--store-id','900001','--duration','121','--base-interval','60',
                '--max-pairs','10','--resume-task']),0)
        self.assertEqual(factory.call_count,0);self.assertEqual(before,(self.db.read_bytes(),self.path.read_bytes()))

    def test_window_service_reuses_http_projection_and_persistent_checkpoint(self):
        import asyncio,threading,time
        from sushiwait.remotewindow import RemoteWindowService
        from sushiwait.remoteservice import RemoteASGI
        self.document([self.diner(),self.diner()]);waited=threading.Event()
        def stop_wait(seconds,event):waited.set();event.set()
        service=RemoteWindowService(db=self.db,task_file=self.path,plan_file=self.plan,store_ids=['900001'],
            base_interval=300,duration_seconds=86400,max_pairs=8640,wall_clock=self.clock.wall,
            monotonic_clock=self.clock.mono,client_factory=lambda:self.client,wait=stop_wait)
        try:
            service.start();self.assertTrue(waited.wait(3));service.shutdown()
            async def read():
                events=[]
                async def receive():return {'type':'http.request','body':b''}
                async def send(event):events.append(event)
                await RemoteASGI(service)({'type':'http','method':'GET','path':'/api/v1/stores/900001/queue','query_string':b''},receive,send)
                return events
            events=asyncio.run(read());self.assertEqual(events[0]['status'],200)
            self.assertEqual(json.loads(events[1]['body'])['fields']['groupqueues']['state'],'last_known_only')
            self.assertEqual(service.status()['task']['task_schema_version'],2)
            self.assertEqual(service.status()['task']['maximum_pair_budget'],8640)
            self.assertEqual(len(self.opener.calls),2)
        finally:service.shutdown()

    def test_cli_invalid_window_bounds_do_not_touch_storage_or_network(self):
        with patch('socket.socket',side_effect=AssertionError('socket')) as sock, \
                patch('sushiwait.remote.RemoteStore',side_effect=AssertionError('database')) as db, \
                patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('auth')) as auth, \
                patch('sys.stdout',new_callable=io.StringIO) as out:
            for flags in [['--duration','259201'],['--max-pairs','25921'],['--base-interval','59']]:
                code=main(['remote-window-collect','--db',str(self.db),'--task-file',str(self.path),'--plan-file',str(self.plan),'--store-id','900001',*flags])
                self.assertEqual(code,2)
        self.assertEqual((sock.call_count,db.call_count,auth.call_count),(0,0,0));self.assertFalse(self.path.exists())
        self.assertNotIn(str(self.plan),out.getvalue())

def _decode_delta(value):
    return (datetime.fromisoformat(value['deadline_at'].replace('Z','+00:00'))-datetime.fromisoformat(value['created_at'].replace('Z','+00:00'))).total_seconds()

if __name__=='__main__':unittest.main()
