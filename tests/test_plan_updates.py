"""Whole-plan revisions applied by the actual collector; synthetic transport."""
from copy import deepcopy
from datetime import timedelta
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_remote_window import BASE, Clock, Opener
from sushiwait.cli import main
from sushiwait.credentials import CredentialError
from sushiwait.planupdates import (PlanUpdateError, publish_update, read_update, validate_update, digest)
from sushiwait.remote import RemoteClient, RemoteStore
from sushiwait.remotetasks import RemoteTaskError
from sushiwait.remotewindow import (RemoteWindowTask, PersistentWindowSchedule, RemoteWindowService,
    window_config, remote_window_status, collect_remote_window, _decode_window)

SERIES='14c3094e-64bd-4c15-8d7c-7ea3ca54fba3'


class PlanUpdatesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.parent=Path(self.tmp.name).resolve()
        self.feed_dir=self.parent/'live';self.feed_dir.mkdir(mode=0o700)
        self.feed=self.feed_dir/'accepted.json';self.source=self.parent/'next.json'
        self.db=self.parent/'remote.sqlite3';self.task_file=self.parent/'task.json';self.seed=self.parent/'seed.json'
        self.write(self.seed,{'schema_version':1,'plans':[]})
        self.clock=Clock();self.opener=Opener(self.clock);self.client=RemoteClient(opener=self.opener)
        self.utc=patch('sushiwait.remote._utc',side_effect=lambda:self.clock.wall().isoformat());self.utc.start()
        self.feed_value=self.update(1,[]);self.write(self.feed,self.feed_value)

    def tearDown(self):self.utc.stop();self.tmp.cleanup()
    def write(self,path,value):path.write_text(json.dumps(value));path.chmod(0o600)
    def diner(self,seconds,store='900001',**extra):
        return {'store_id':store,'desired_arrival_at':(BASE+timedelta(seconds=seconds)).isoformat(),**extra}
    def update(self,revision,plans,**extra):
        return {'schema_version':1,'series_id':SERIES,'revision':revision,
            'declared_at':self.clock.wall().isoformat(),'document':{'schema_version':1,'plans':plans},**extra}
    def publish(self,revision,plans,**extra):
        self.feed_value=self.update(revision,plans,**extra);self.write(self.source,self.feed_value)
        return publish_update(self.source,self.feed,stores=['900001','900002'],base_interval=300,clock=self.clock.wall)
    def config(self,*,stores=('900001',),base=300,duration=240,pairs=20):
        return window_config(self.db,self.seed,list(stores),base,duration,pairs,now=self.clock.wall(),plan_updates_file=self.feed)
    def collect(self,config=None,*,resume=False,sleep=None,stop=lambda:False,emit=None):
        events=[];config=config or self.config()
        with RemoteWindowTask(self.task_file,config=config,resume=resume,now=self.clock.wall()) as task:
            task.prepare_database()
            with RemoteStore(self.db) as database:
                task.bind(database,now=self.clock.wall())
                result=collect_remote_window(task,self.client,wall_clock=self.clock.wall,monotonic_clock=self.clock.mono,
                    sleep=sleep or self.clock.sleep,emit=emit or events.append,should_stop=stop)
        return result,events
    def starts(self,store='900001'):
        return [at for url,at in self.opener.calls if 'groupqueues?' in url and url.endswith('storeid='+store)]
    def one_pending(self):
        config=self.config()
        with RemoteWindowTask(self.task_file,config=config,resume=False,now=self.clock.wall()) as task:
            with RemoteStore(self.db) as database:
                task.bind(database,now=self.clock.wall());task.begin('900001',now=self.clock.wall())
                database.append(self.client.snapshot('900001'))
        return config

    def test_running_plan_add_change_and_end_actual_schedule(self):
        def sleep(seconds):
            self.clock.sleep(seconds)
            if self.clock.seconds==10:self.publish(2,[self.diner(1200),self.diner(1200)])
            if self.clock.seconds==70:self.publish(3,[self.diner(600)])
            if self.clock.seconds==100:self.publish(4,[self.diner(600,plan_status='called')])
        result,_=self.collect(sleep=sleep)
        self.assertEqual(self.starts(),[0,60,90]);self.assertEqual(result['accepted_plan_revision'],4)
        self.assertEqual(result['unobserved_plan_revisions'],0);self.assertEqual(result['recorded_http_attempts'],6)
        self.assertEqual(self.clock.seconds,240);self.assertEqual(result['end_reason'],'deadline')
        self.assertEqual(result['maximum_pair_budget'],20);self.assertFalse(result['eta_available'])

    def test_idle_change_wakes_inside_one_second_without_resetting_start(self):
        def sleep(seconds):
            self.clock.sleep(seconds)
            if self.clock.seconds==31:self.publish(2,[self.diner(600)])
        result,_=self.collect(self.config(duration=62),sleep=sleep)
        self.assertEqual(self.starts(),[0,31,61]);self.assertEqual(result['successful_pairs'],3)
        self.assertTrue(all(s<=1 for s in self.clock.sleeps))

    def test_fixed_stores_share_budget_and_one_pair_per_store(self):
        self.publish(2,[self.diner(600),self.diner(600),self.diner(600,store='900002')])
        result,_=self.collect(self.config(stores=('900001','900002'),pairs=3))
        self.assertEqual(self.starts(),[0,30]);self.assertEqual(self.starts('900002'),[0])
        self.assertEqual(result['recorded_http_attempts'],6);self.assertEqual(result['end_reason'],'budget')

    def test_resume_higher_revision_preserves_deadline_budget_and_pending_result(self):
        config=self.one_pending();deadline=remote_window_status(self.task_file)['deadline_at']
        self.clock.seconds=10;self.publish(2,[self.diner(600)])
        result,_=self.collect(config,resume=True)
        self.assertEqual(result['deadline_at'],deadline);self.assertEqual(result['maximum_pair_budget'],20)
        self.assertEqual(self.starts()[:3],[0,40,70]);self.assertEqual(result['uncertain_pair_slots'],0)
        self.assertEqual(result['accepted_plan_revision'],2)

    def test_update_during_pending_pair_is_applied_only_after_reconciliation(self):
        config=self.config()
        with RemoteWindowTask(self.task_file,config=config,resume=False,now=self.clock.wall()) as task:
            with RemoteStore(self.db) as database:
                task.bind(database,now=self.clock.wall());task.begin('900001',now=self.clock.wall())
                self.publish(2,[self.diner(600)]);task.refresh_plans(now=self.clock.wall())
                self.assertEqual(task.value['plan_context']['revision'],1)
                database.append(self.client.snapshot('900001'));task.reconcile(now=self.clock.wall())
                self.assertEqual(task.value['cursor'],1);self.assertEqual(task.value['plan_context']['revision'],1)
                task.refresh_plans(now=self.clock.wall());self.assertEqual(task.value['plan_context']['revision'],2)

    def test_jump_has_no_catchup_or_deadline_extension(self):
        self.publish(2,[self.diner(600)]);self.clock.jump=95
        result,_=self.collect(self.config(duration=160))
        self.assertEqual(self.starts(),[0,95,125,155]);self.assertEqual(result['catch_up_requests'],0)
        self.assertEqual(self.clock.seconds,160)

    def test_unobserved_revisions_are_counted_not_fabricated(self):
        config=self.one_pending();self.clock.seconds=10;self.publish(5,[self.diner(600)])
        result,_=self.collect(config,resume=True)
        self.assertEqual(result['unobserved_plan_revisions'],3)
        self.assertFalse(result['complete_plan_history_verified'])

    def test_completed_task_is_not_revived_or_rewritten_by_new_plan(self):
        config=self.config(pairs=1);self.collect(config);before=self.task_file.read_bytes()
        self.clock.seconds=2;self.publish(2,[self.diner(600)])
        self.collect(config,resume=True);self.assertEqual(self.task_file.read_bytes(),before)
        self.assertEqual(len(self.opener.calls),2)

    def test_failed_task_new_plan_cannot_retry(self):
        config=self.config();self.opener.fail=True;self.collect(config);before=self.task_file.read_bytes()
        self.clock.seconds=2;self.publish(2,[self.diner(600)]);self.opener.fail=False
        self.collect(config,resume=True);self.assertEqual(self.task_file.read_bytes(),before)
        self.assertEqual(len(self.opener.calls),1)

    def test_same_revision_change_stale_series_or_future_stop_without_queries(self):
        config=self.one_pending();before=self.task_file.read_bytes();self.clock.seconds=10
        cases=[self.update(1,[self.diner(600)]),self.update(1,[],series_id='14c3094e-64bd-4c15-8d7c-7ea3ca54fba4'),
               self.update(2,[],declared_at=(BASE+timedelta(seconds=11)).isoformat())]
        for value in cases:
            with self.subTest(value=value):
                self.write(self.feed,value)
                with self.assertRaises(PlanUpdateError):self.collect(config,resume=True)
                self.assertEqual(self.task_file.read_bytes(),before);self.assertEqual(len(self.opener.calls),2)

    def test_revision_rollback_and_declaration_rollback_refuse_resume(self):
        self.clock.seconds=5;self.publish(3,[]);config=self.one_pending()
        before=self.task_file.read_bytes();self.clock.seconds=10
        for value in (self.update(2,[]),self.update(4,[],declared_at=BASE.isoformat())):
            self.write(self.feed,value)
            with self.assertRaises(PlanUpdateError):self.collect(config,resume=True)
            self.assertEqual(self.task_file.read_bytes(),before)

    def test_original_seed_remains_immutable_with_updates_enabled(self):
        config=self.one_pending();self.write(self.seed,{'schema_version':1,'plans':[self.diner(600)]})
        self.clock.seconds=10
        with self.assertRaisesRegex(RemoteTaskError,'plan_changed'):self.collect(config,resume=True)

    def test_status_is_private_plan_free_and_offline(self):
        self.publish(2,[self.diner(600)]);self.one_pending();before=self.task_file.read_bytes()
        with patch('socket.socket',side_effect=AssertionError('socket')) as sock,patch('sushiwait.cli.read_credentials_file') as auth:
            result=remote_window_status(self.task_file)
        body=json.dumps(result)
        for secret in (str(self.feed),SERIES,digest(self.feed_value),self.diner(600)['desired_arrival_at']):self.assertNotIn(secret,body)
        self.assertEqual((sock.call_count,auth.call_count),(0,0));self.assertEqual(before,self.task_file.read_bytes())

    def test_schema3_decoder_preserves_all_old_chain_guards(self):
        self.one_pending();value=json.loads(self.task_file.read_bytes())
        for field, replacement in [('cursor',99),('deadline_at',BASE.isoformat()),('starts',{'900002':BASE.isoformat()}),
                                   ('plan_context',{'revision':1})]:
            bad=deepcopy(value);bad[field]=replacement
            with self.assertRaises(RemoteTaskError):_decode_window(json.dumps(bad).encode())

    def test_invalid_feed_scope_borrowed_starts_and_oversize_reject_before_db(self):
        values=[self.update(2,[self.diner(600,store='900003')]),self.update(2,[])]
        values[1]['document']['last_poll_started_at']={'900001':BASE.isoformat()}
        for value in values:
            self.write(self.feed,value)
            with self.assertRaises(PlanUpdateError):self.config()
            self.assertFalse(self.db.exists())
        self.feed.write_bytes(b' '*16385)
        with self.assertRaises(PlanUpdateError):self.config()

    def test_feed_must_be_separate_directory_from_db_and_task(self):
        with self.assertRaisesRegex(RemoteTaskError,'path_conflict'):
            window_config(self.db,self.seed,['900001'],300,240,20,now=self.clock.wall(),plan_updates_file=self.source)

    def test_atomic_publish_replaces_whole_set_and_preserves_old_on_conflict(self):
        first=self.feed.read_bytes();result=self.publish(2,[self.diner(600)])
        self.assertTrue(result['durability_confirmed']);self.assertEqual(self.feed.stat().st_mode&0o777,0o600)
        accepted=self.feed.read_bytes();self.assertNotEqual(first,accepted)
        with self.assertRaises(PlanUpdateError):self.publish(2,[])
        self.assertEqual(self.feed.read_bytes(),accepted)
        self.publish(3,[]);self.assertEqual(read_update(self.feed,stores=['900001'],base_interval=300)['document']['plans'],[])

    def test_initial_publish_same_revision_and_new_series_conflict(self):
        self.feed.unlink();self.publish(1,[]);before=self.feed.read_bytes()
        with self.assertRaisesRegex(PlanUpdateError,'revision_not_new'):self.publish(1,[])
        with self.assertRaisesRegex(PlanUpdateError,'series_conflict'):
            self.publish(2,[],series_id='14c3094e-64bd-4c15-8d7c-7ea3ca54fba4')
        self.assertEqual(self.feed.read_bytes(),before)

    def test_publication_fsync_failure_reports_committed_without_retry(self):
        self.write(self.source,self.update(2,[]));original=os.fsync;count=[0]
        def fsync(fd):
            count[0]+=1
            if count[0]==3:raise OSError('synthetic')
            return original(fd)
        with patch('sushiwait.planupdates.os.fsync',side_effect=fsync):
            result=publish_update(self.source,self.feed,stores=['900001'],base_interval=300,clock=self.clock.wall)
        self.assertTrue(result['committed']);self.assertFalse(result['durability_confirmed'])
        self.assertFalse(result['ok']);self.assertEqual(read_update(self.feed,stores=['900001'],base_interval=300)['revision'],2)

    def test_output_parent_lock_conflict_leaves_old_unchanged(self):
        import fcntl
        before=self.feed.read_bytes();self.write(self.source,self.update(2,[]));fd=os.open(self.feed_dir,os.O_RDONLY)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(PlanUpdateError):publish_update(self.source,self.feed,stores=['900001'],base_interval=300,clock=self.clock.wall)
        finally:os.close(fd)
        self.assertEqual(self.feed.read_bytes(),before)

    def test_unsafe_input_destination_links_and_duplicate_json_reject(self):
        self.write(self.source,self.update(2,[]));self.source.chmod(0o644)
        with self.assertRaises(PlanUpdateError):publish_update(self.source,self.feed,stores=['900001'],base_interval=300,clock=self.clock.wall)
        self.source.chmod(0o600);self.feed.unlink();self.feed.symlink_to(self.source)
        with self.assertRaises(PlanUpdateError):publish_update(self.source,self.feed,stores=['900001'],base_interval=300,clock=self.clock.wall)
        self.feed.unlink();self.feed.write_text('{"schema_version":1,"schema_version":1}');self.feed.chmod(0o600)
        with self.assertRaises(PlanUpdateError):self.config()

    def test_missing_feed_stops_in_process_before_second_query(self):
        def emit(value):
            if 'id' in value:self.feed.unlink()
        with self.assertRaises(PlanUpdateError):self.collect(emit=emit)
        self.assertEqual(len(self.opener.calls),2)

    def test_cli_publication_returns_safe_metadata_no_network_or_auth(self):
        self.write(self.source,self.update(2,[self.diner(600)]));output=io.StringIO()
        with patch('sushiwait.cli._utc_clock',self.clock.wall),patch('socket.socket') as sock,patch('sushiwait.cli.read_credentials_file') as auth,patch('sys.stdout',output):
            code=main(['monitor-plans-publish','--input',str(self.source),'--output',str(self.feed),'--store-id','900001'])
        self.assertEqual(code,0);result=json.loads(output.getvalue());self.assertEqual(result['revision'],2)
        for secret in (SERIES,str(self.feed),self.diner(600)['desired_arrival_at']):self.assertNotIn(secret,output.getvalue())
        self.assertEqual((sock.call_count,auth.call_count),(0,0))

    def test_service_constructor_and_worker_use_dynamic_task(self):
        # Real worker/thread, synthetic transport and clock; no HTTP server here.
        def wait(seconds,stop_event):
            self.clock.sleep(seconds)
            if self.clock.seconds==10:self.publish(2,[self.diner(600)])
        service=RemoteWindowService(db=self.db,task_file=self.task_file,plan_file=self.seed,plan_updates_file=self.feed,
            store_ids=['900001'],duration_seconds=62,max_pairs=20,client_factory=lambda:self.client,
            wall_clock=self.clock.wall,monotonic_clock=self.clock.mono,wait=wait)
        service.start();service.thread.join(2)
        self.assertFalse(service.thread.is_alive());self.assertEqual(self.starts(),[0,30,60])
        self.assertEqual(service.status()['service_state'],'completed')
        self.assertEqual(remote_window_status(self.task_file)['accepted_plan_revision'],2)

    def test_atomic_rename_read_race_has_one_local_reread_only(self):
        body=self.feed.read_bytes()
        with patch('sushiwait.planupdates._read_private_file',side_effect=[CredentialError('credentials_file_changed'),body]) as reader:
            value=read_update(self.feed,stores=['900001'],base_interval=300,now=self.clock.wall())
        self.assertEqual(reader.call_count,2);self.assertEqual(value['revision'],1)
        with patch('sushiwait.planupdates._read_private_file',side_effect=CredentialError('credentials_file_unavailable')) as reader:
            with self.assertRaises(PlanUpdateError):read_update(self.feed,stores=['900001'],base_interval=300)
        self.assertEqual(reader.call_count,1)

    def test_cli_collect_and_serve_forward_updates_flag(self):
        from sushiwait.remoteservice import RemoteServiceError
        for command in ('remote-window-collect','remote-window-serve'):
            output=io.StringIO()
            with patch('sushiwait.cli._utc_clock',self.clock.wall),patch('sys.stdout',output), \
                    patch('sushiwait.remotewindow.RemoteWindowService',side_effect=RemoteServiceError('synthetic_stop')) as factory, \
                    patch('sushiwait.remotewindow.RemoteWindowTask',side_effect=RemoteTaskError('synthetic_stop')) as task:
                result=main([command,'--db',str(self.db),'--task-file',str(self.task_file),'--plan-file',str(self.seed),
                    '--plan-updates-file',str(self.feed),'--store-id','900001'])
            self.assertEqual(result,2)
            if command.endswith('serve'):self.assertEqual(factory.call_args.kwargs['plan_updates_file'],str(self.feed))
            else:self.assertEqual(task.call_args.kwargs['config']['plan_updates_file'],str(self.feed))

    def test_terminal_quality_accepts_schema3_without_querying(self):
        config=self.config(pairs=1);self.collect(config);self.clock.seconds=1;output=io.StringIO()
        before=self.db.read_bytes()
        with patch('sys.stdout',output),patch('socket.socket') as sock:
            result=main(['remote-window-quality','--db',str(self.db),'--task-file',str(self.task_file),
                '--as-of',self.clock.wall().isoformat()])
        self.assertEqual(result,0);self.assertEqual(sock.call_count,0);self.assertEqual(before,self.db.read_bytes())

    def test_invalid_update_boolean_revision_non_uuid4_and_clock(self):
        for value in (self.update(True,[]),self.update(1,[],series_id='00000000-0000-0000-0000-000000000000'),
                      self.update(1,[],declared_at='2026-10-06T12:00:00')):
            with self.assertRaises(PlanUpdateError):validate_update(value,stores=['900001'],base_interval=300,now=self.clock.wall())

    def test_submillisecond_declaration_preserves_digest_and_order(self):
        self.clock.seconds=.000999;self.publish(2,[]);self.one_pending()
        value=json.loads(self.task_file.read_bytes())
        self.assertEqual(value['plan_context']['document_digest'],digest(self.feed_value))
        self.assertEqual(value['plan_context']['declared_at'],self.feed_value['declared_at'])
        self.clock.seconds=.001
        self.write(self.feed,self.update(3,[],declared_at=(BASE+timedelta(microseconds=998)).isoformat()))
        with self.assertRaisesRegex(PlanUpdateError,'declaration_rollback'):self.collect(self.config(),resume=True)


if __name__=='__main__':unittest.main()
