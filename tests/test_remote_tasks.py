"""Crash, durability and scope checks for anonymous checkpoints, offline."""
from datetime import datetime,timedelta,timezone
import hashlib,io,json,os
from pathlib import Path
import sqlite3,subprocess,sys,tempfile,unittest
from unittest.mock import patch

from sushiwait.cli import main
from sushiwait.remote import RemoteClient,RemoteStore,QUEUE_NAMES
from sushiwait.remotetasks import RemoteTask,RemoteTaskError,remote_task_status,task_config

BASE=datetime(2026,10,6,12,tzinfo=timezone.utc)

class Clock:
    def __init__(self):self.seconds=0;self.sleeps=[];self.callback=None
    def wall(self):return BASE+timedelta(seconds=self.seconds)
    def mono(self):return self.seconds
    def sleep(self,seconds):
        self.sleeps.append(seconds);self.seconds+=seconds
        if self.callback:self.callback()

class Response:
    headers={}
    def __init__(self,request,status):self.request=request;self.status=status
    def geturl(self):return self.request.full_url
    def getcode(self):return self.status
    def close(self):pass
    def read(self,size):
        return json.dumps({name:[] for name in QUEUE_NAMES} if 'groupqueues?' in self.request.full_url else 0).encode()[:size]

class Opener:
    def __init__(self,clock):self.clock=clock;self.calls=[];self.interrupt=False;self.status=200
    def open(self,request,*,timeout):
        self.calls.append((request.full_url,self.clock.seconds))
        if self.interrupt:self.interrupt=False;raise KeyboardInterrupt
        return Response(request,self.status)

class RemoteTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.parent=Path(self.tmp.name).resolve()
        self.db=self.parent/'remote.sqlite3';self.task=self.parent/'task.json'
        self.clock=Clock();self.opener=Opener(self.clock)
    def tearDown(self):self.tmp.cleanup()
    def args(self,*,stores=('900001',),samples=3,interval=30,resume=False):
        result=['remote-collect','--db',str(self.db),'--task-file',str(self.task),
            '--interval',str(interval),'--samples',str(samples)]
        for store in stores:result+=['--store-id',store]
        if resume:result+=['--resume-task']
        return result
    def run_cli(self,args):
        client=RemoteClient(opener=self.opener)
        with patch('sushiwait.remote.RemoteClient',return_value=client), \
                patch('sushiwait.cli._utc_clock',side_effect=self.clock.wall), \
                patch('sushiwait.cli.time.monotonic',side_effect=self.clock.mono), \
                patch('sushiwait.cli.time.sleep',side_effect=self.clock.sleep), \
                patch('sushiwait.remote._utc',side_effect=lambda:self.clock.wall().isoformat(timespec='milliseconds').replace('+00:00','Z')), \
                patch('socket.socket',side_effect=AssertionError('network')) as sock, \
                patch('sushiwait.credentials.read_credentials_file',side_effect=AssertionError('auth')) as auth, \
                patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('auth')) as cli_auth, \
                patch('sys.stdout',new_callable=io.StringIO) as output:
            code=main(args)
        self.assertEqual([sock.call_count,auth.call_count,cli_auth.call_count],[0,0,0])
        return code,[json.loads(line) for line in output.getvalue().splitlines()]
    def raw(self):return json.loads(self.task.read_text())
    def pause_after_commit(self,**kwargs):
        original=RemoteTask.reconcile
        def interrupted(task,*,now,interrupted=False):
            if not interrupted:raise KeyboardInterrupt
            return original(task,now=now,interrupted=interrupted)
        with patch.object(RemoteTask,'reconcile',new=interrupted):
            self.assertEqual(self.run_cli(self.args(**kwargs))[0],130)
        self.assertIsNotNone(self.raw()['pending'])

    def test_complete_bounded_task_and_status_have_no_private_paths(self):
        code,events=self.run_cli(self.args(stores=('900001','900002'),samples=2))
        self.assertEqual(code,0);summary=events[-1]['remote_task_summary']
        self.assertEqual((summary['successful_pairs'],summary['recorded_http_attempts']),(4,8))
        self.assertEqual(summary['maximum_request_budget'],8)
        self.assertTrue(summary['all_slots_successful'])
        self.assertEqual([at for url,at in self.opener.calls if 'groupqueues?' in url],[0,0,30,30])
        status=remote_task_status(self.task)
        for private in (str(self.db),str(self.task),self.raw()['records_digest']):
            self.assertNotIn(private,json.dumps(status))
        self.assertEqual(status['process_liveness'],'unknown')
        for p in (self.task,self.task.with_name(self.task.name+'.lock')):self.assertEqual(p.stat().st_mode&0o777,0o600)

    def test_committed_result_before_checkpoint_advances_once_after_resume(self):
        self.pause_after_commit();self.clock.seconds=10
        code,events=self.run_cli(self.args(resume=True))
        self.assertEqual(code,0);self.assertEqual(events[-1]['remote_task_summary']['successful_pairs'],3)
        self.assertEqual([at for url,at in self.opener.calls if 'groupqueues?' in url],[0,40,70])
        self.assertEqual(self.raw()['uncertain'],0)

    def test_unknown_inflight_attempt_is_consumed_not_replayed(self):
        self.opener.interrupt=True
        self.assertEqual(self.run_cli(self.args())[0],130);self.clock.seconds=10
        code,events=self.run_cli(self.args(resume=True));summary=events[-1]['remote_task_summary']
        self.assertEqual(code,1)
        self.assertEqual((summary['successful_pairs'],summary['uncertain_pair_slots']),(2,1))
        self.assertFalse(summary['all_slots_successful'])
        self.assertEqual(summary['unrecorded_http_attempts'],'unknown')
        self.assertEqual([at for url,at in self.opener.calls if 'groupqueues?' in url],[0,40,70])
        self.assertEqual(summary['last_gap']['reason'],'uncertain_attempt')

    def test_partial_multistore_restart_waits_full_period(self):
        self.pause_after_commit(stores=('900001','900002'),samples=2);self.clock.seconds=5
        self.assertEqual(self.run_cli(self.args(stores=('900001','900002'),samples=2,resume=True))[0],0)
        self.assertEqual([at for url,at in self.opener.calls if 'groupqueues?' in url],[0,35,65,65])

    def test_abrupt_subprocess_exit_after_sqlite_commit_recovers_once(self):
        code='''
import sys,os,json
from datetime import datetime,timezone
sys.path.insert(0,sys.argv[1])
from sushiwait import remote
from sushiwait.remotetasks import RemoteTask,task_config
now=datetime(2026,10,6,12,tzinfo=timezone.utc)
remote._utc=lambda:now.isoformat(timespec='milliseconds').replace('+00:00','Z')
class Response:
    headers={}
    def __init__(self,request):self.request=request
    def getcode(self):return 200
    def geturl(self):return self.request.full_url
    def close(self):pass
    def read(self,size):return json.dumps({n:[] for n in remote.QUEUE_NAMES} if 'groupqueues?' in self.request.full_url else 0).encode()
class Opener:
    def open(self,request,*,timeout):return Response(request)
with RemoteTask(sys.argv[2],config=task_config(sys.argv[3],['900001'],30,3),resume=False,now=now) as task:
    task.prepare_database()
    with remote.RemoteStore(sys.argv[3]) as db:
        task.bind(db,now=now);task.begin(now=now)
        db.append(remote.RemoteClient(opener=Opener()).snapshot('900001'))
        os._exit(0)
'''
        result=subprocess.run([sys.executable,'-c',code,str(Path(__file__).resolve().parents[1]/'src'),str(self.task),str(self.db)],
            cwd=self.parent,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr.decode())
        self.assertIsNotNone(self.raw()['pending']);self.clock.seconds=10
        status=self.run_cli(self.args(resume=True))
        self.assertEqual(status[0],0);self.assertEqual(status[1][-1]['remote_task_summary']['successful_pairs'],3)
        self.assertEqual(len(self.opener.calls),4)

    def test_failed_task_cannot_query_again_even_with_resume(self):
        self.opener.status=429
        self.assertEqual(self.run_cli(self.args())[0],1)
        self.assertEqual(len(self.opener.calls),1)
        before=self.db.read_bytes(),self.task.read_bytes();self.clock.seconds=10
        self.assertEqual(self.run_cli(self.args(resume=True))[0],1)
        self.assertEqual(len(self.opener.calls),1)
        self.assertEqual(before,(self.db.read_bytes(),self.task.read_bytes()))

    def test_completed_resume_is_readonly_and_zero_queries(self):
        self.assertEqual(self.run_cli(self.args(samples=1))[0],0)
        before=self.db.read_bytes(),self.task.read_bytes();calls=len(self.opener.calls);self.clock.seconds=10
        self.assertEqual(self.run_cli(self.args(samples=1,resume=True))[0],0)
        self.assertEqual(len(self.opener.calls),calls)
        self.assertEqual(before,(self.db.read_bytes(),self.task.read_bytes()))

    def test_existing_task_requires_explicit_resume(self):
        self.run_cli(self.args(samples=1));before=self.db.read_bytes(),self.task.read_bytes()
        code,events=self.run_cli(self.args(samples=1))
        self.assertEqual(code,2);self.assertEqual(events[-1]['error_code'],'remote_task_exists_use_resume')
        self.assertEqual(before,(self.db.read_bytes(),self.task.read_bytes()))

    def test_missing_resume_task_does_not_create_database(self):
        code,events=self.run_cli(self.args(resume=True))
        self.assertEqual((code,events[-1]['error_code']),(2,'remote_task_missing'))
        self.assertFalse(self.db.exists());self.assertEqual(self.opener.calls,[])

    def test_resume_flag_without_task_is_rejected_before_network(self):
        args=self.args();i=args.index('--task-file');del args[i:i+2];args+=['--resume-task']
        code,events=self.run_cli(args)
        self.assertEqual((code,events[-1]['error_code']),(2,'remote_task_required_for_resume'))
        self.assertFalse(self.db.exists());self.assertEqual(self.opener.calls,[])

    def test_config_changes_are_rejected_without_database_changes(self):
        self.pause_after_commit();before=self.db.read_bytes(),self.task.read_bytes();self.clock.seconds=10
        for values in ({'samples':4},{'interval':60},{'stores':('900002',)}):
            code,events=self.run_cli(self.args(resume=True,**values))
            self.assertEqual((code,events[-1]['error_code']),(2,'remote_task_config_conflict'))
            self.assertEqual(before,(self.db.read_bytes(),self.task.read_bytes()))

    def test_worker_lock_prevents_second_cooperating_worker(self):
        config=task_config(self.db,['900001'],30,3)
        with RemoteTask(self.task,config=config,resume=False,now=BASE):
            with self.assertRaisesRegex(RemoteTaskError,'remote_task_busy'):
                RemoteTask(self.task,config=config,resume=False,now=BASE)

    def test_missing_resumed_database_is_not_recreated(self):
        self.pause_after_commit();self.db.unlink();self.clock.seconds=10
        code,events=self.run_cli(self.args(resume=True))
        self.assertEqual((code,events[-1]['error_code']),(2,'remote_task_database_changed'))
        self.assertFalse(self.db.exists())

    def test_database_replacement_is_rejected(self):
        self.pause_after_commit();copy=self.parent/'copy.sqlite3';copy.write_bytes(self.db.read_bytes());copy.chmod(0o600)
        os.replace(copy,self.db);self.clock.seconds=10
        code,events=self.run_cli(self.args(resume=True))
        self.assertEqual((code,events[-1]['error_code']),(2,'remote_task_database_changed'))

    def test_all_committed_rows_are_checked_not_only_latest(self):
        self.run_cli(self.args());self.clock.seconds=100
        with sqlite3.connect(self.db) as db:
            row=db.execute('SELECT payload_json FROM remote_samples WHERE id=1').fetchone()[0]
            changed=json.loads(row);changed['queries']['storequeuecount']['payload']['raw_count']=9
            db.execute('UPDATE remote_samples SET payload_json=? WHERE id=1',(json.dumps(changed),))
        calls=len(self.opener.calls);code,events=self.run_cli(self.args(resume=True))
        self.assertEqual((code,events[-1]['error_code']),(2,'remote_task_database_changed'))
        self.assertEqual(len(self.opener.calls),calls)

    def test_extra_pending_result_or_wrong_run_is_not_guessed(self):
        self.pause_after_commit();self.clock.seconds=10
        with sqlite3.connect(self.db) as db:db.execute('UPDATE remote_samples SET run_id=?',('11111111-1111-4111-8111-111111111111',))
        code,events=self.run_cli(self.args(resume=True))
        self.assertEqual((code,events[-1]['error_code']),(2,'remote_task_result_conflict'))
        self.assertEqual(len(self.opener.calls),2)

    def test_clock_rollback_stops_before_query(self):
        self.pause_after_commit();self.clock.seconds=-1
        code,events=self.run_cli(self.args(resume=True))
        self.assertEqual((code,events[-1]['error_code']),(2,'remote_task_clock_rollback'))
        self.assertEqual(len(self.opener.calls),2)

    def test_task_replacement_during_wait_stops_before_next_query(self):
        def replace():
            replacement=self.parent/'replacement.json';replacement.write_bytes(self.task.read_bytes());replacement.chmod(0o600)
            os.replace(replacement,self.task)
        self.clock.callback=replace
        code,events=self.run_cli(self.args())
        self.assertEqual((code,events[-1]['error_code']),(2,'remote_task_changed'))
        self.assertEqual(len(self.opener.calls),2)

    def test_precommit_fsync_failure_keeps_checkpoint_and_stops_query(self):
        original=RemoteTask.begin
        def fail(task,*,now):
            before=task.path.read_bytes()
            with patch('sushiwait.remotetasks.os.fsync',side_effect=OSError('private-disk-error')):
                with self.assertRaisesRegex(RemoteTaskError,'remote_task_write_failed'):original(task,now=now)
            self.assertEqual(task.path.read_bytes(),before)
            raise RemoteTaskError('remote_task_write_failed')
        with patch.object(RemoteTask,'begin',new=fail):code,events=self.run_cli(self.args())
        self.assertEqual((code,events[-1]['error_code']),(2,'remote_task_write_failed'))
        self.assertEqual(self.opener.calls,[])

    def test_directory_fsync_failure_reports_actual_pending_commit(self):
        original=RemoteTask.begin;real=os.fsync
        def fail(task,*,now):
            def fsync(fd):
                if fd==task.parent_fd:raise OSError('private-disk-error')
                return real(fd)
            with patch('sushiwait.remotetasks.os.fsync',side_effect=fsync):return original(task,now=now)
        with patch.object(RemoteTask,'begin',new=fail):code,events=self.run_cli(self.args())
        self.assertEqual((code,events[-1]['error_code']),(2,'remote_task_durability_unconfirmed'))
        self.assertIsNotNone(self.raw()['pending']);self.assertEqual(self.opener.calls,[])

    def test_task_status_is_offline_bounded_and_duplicate_keys_rejected(self):
        self.run_cli(self.args(samples=1));before=self.db.read_bytes(),self.task.read_bytes()
        self.assertEqual(self.run_cli(['remote-task-status','--task-file',str(self.task)])[0],0)
        self.assertEqual(before,(self.db.read_bytes(),self.task.read_bytes()))
        self.task.write_text('{"schema_version":1,"schema_version":1}')
        code,events=self.run_cli(['remote-task-status','--task-file',str(self.task)])
        self.assertEqual(code,2);self.assertNotIn(str(self.task),json.dumps(events))

    def test_task_and_database_path_collision_does_not_touch_victim(self):
        victim=self.parent/'victim.json';victim.write_text('private');victim.chmod(0o600)
        args=self.args();args[args.index('--db')+1]=str(victim);args[args.index('--task-file')+1]=str(victim)
        self.assertEqual(self.run_cli(args)[0],2);self.assertEqual(victim.read_text(),'private')


if __name__=='__main__':unittest.main()
