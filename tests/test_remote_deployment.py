"""Restart selection must not silently replace a missing checkpoint or reset a budget."""
import io,json,os,unittest
from unittest.mock import patch
import test_remote_window as fixtures
from sushiwait.remote import RemoteStore
from sushiwait.remotetasks import RemoteTaskError
from sushiwait.remotewindow import RemoteWindowTask,RemoteWindowService,remote_window_status
from sushiwait.remoteservice import serve_local,RemoteServiceError
from sushiwait.cli import main

class DeploymentTests(unittest.TestCase):
    setUp=fixtures.RemoteWindowTests.setUp
    tearDown=fixtures.RemoteWindowTests.tearDown
    config=fixtures.RemoteWindowTests.config
    pending=fixtures.RemoteWindowTests.pending
    collect=fixtures.RemoteWindowTests.collect

    def auto(self,config=None):
        return RemoteWindowTask(self.path,config=config or self.config(),resume=False,
            resume_if_present=True,now=self.clock.wall())

    def test_empty_state_selects_fresh_database_with_exclusive_creation(self):
        with self.auto() as task:
            self.assertFalse(task.loaded);self.assertTrue(task.require_new_database)
            task.prepare_database()
            with RemoteStore(self.db,exclusive_create=task.require_new_database) as database:
                task.bind(database,now=self.clock.wall())
        self.assertEqual(remote_window_status(self.path)['completed_pair_slots'],0)
        self.assertEqual(self.opener.calls,[])

    def test_existing_checkpoint_loads_original_deadline_and_budget(self):
        config=self.pending();before=remote_window_status(self.path);self.clock.seconds=10
        with self.auto(config) as task:
            self.assertTrue(task.loaded);self.assertFalse(task.require_new_database)
            with RemoteStore(self.db) as database:task.bind(database,now=self.clock.wall())
            self.assertEqual(task.value['deadline_at'],before['deadline_at'])
            self.assertEqual(task.value['config']['max_pairs'],before['maximum_pair_budget'])
            self.assertEqual(task.value['cursor'],1)
        self.assertEqual(len(self.opener.calls),2)

    def test_missing_checkpoint_with_database_refuses_fresh_window(self):
        config=self.pending();self.path.unlink();before=self.db.read_bytes()
        with self.auto(config) as task:
            with self.assertRaisesRegex(RemoteTaskError,'missing_with_existing_database'):task.prepare_database()
        self.assertFalse(self.path.exists());self.assertEqual(self.db.read_bytes(),before)
        self.assertEqual(len(self.opener.calls),2)

    def test_database_created_after_precheck_is_not_adopted(self):
        with self.auto() as task:
            task.prepare_database()
            with RemoteStore(self.db):pass
            before=self.db.read_bytes()
            with self.assertRaises(FileExistsError):RemoteStore(self.db,exclusive_create=True)
        self.assertFalse(self.path.exists());self.assertEqual(self.db.read_bytes(),before)

    def test_direct_bind_cannot_bypass_new_database_requirement(self):
        with RemoteStore(self.db):pass
        before=self.db.read_bytes()
        with self.auto() as task:
            with RemoteStore(self.db) as database:
                with self.assertRaisesRegex(RemoteTaskError,'new_database_required'):
                    task.bind(database,now=self.clock.wall())
        self.assertFalse(self.path.exists());self.assertEqual(before,self.db.read_bytes())

    def test_corrupt_or_symlink_checkpoint_never_becomes_fresh(self):
        for symlink in (False,True):
            target=self.parent/'unsafe.json';target.write_text('{}');target.chmod(0o600)
            if symlink:self.path.symlink_to(target)
            else:self.path.write_text('{}');self.path.chmod(0o600)
            with self.assertRaises(RemoteTaskError):self.auto()
            self.assertFalse(self.db.exists());self.path.unlink()
        self.assertEqual(self.opener.calls,[])

    def test_terminal_cli_auto_resume_has_zero_client_or_network_and_unchanged_files(self):
        self.collect(self.config(pairs=1));before=(self.db.read_bytes(),self.path.read_bytes())
        with patch('sushiwait.remote.RemoteClient',side_effect=AssertionError('client')) as client, \
             patch('socket.socket',side_effect=AssertionError('socket')) as socket, \
             patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('auth')) as auth, \
             patch('sushiwait.cli._utc_clock',side_effect=self.clock.wall),patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(main(['remote-window-collect','--db',str(self.db),'--task-file',str(self.path),
                '--plan-file',str(self.plan),'--store-id','900001','--duration','180','--base-interval','60',
                '--max-pairs','1','--resume-if-present']),0)
        self.assertEqual((client.call_count,socket.call_count,auth.call_count),(0,0,0))
        self.assertEqual(before,(self.db.read_bytes(),self.path.read_bytes()))

    def test_failed_window_auto_resume_does_not_retry(self):
        self.opener.fail=True;self.collect();before=remote_window_status(self.path)
        with self.auto() as task:
            with RemoteStore(self.db) as database:task.bind(database,now=self.clock.wall())
            self.assertEqual(task.value['state'],'failed')
        self.assertEqual(len(self.opener.calls),1)
        self.assertEqual(before,remote_window_status(self.path))

    def test_auto_resume_is_exclusive_with_explicit_resume(self):
        with self.assertRaisesRegex(RemoteTaskError,'invalid_resume_mode'):
            RemoteWindowTask(self.path,config=self.config(),resume=True,resume_if_present=True,now=self.clock.wall())
        with patch('sys.stderr',new_callable=io.StringIO),self.assertRaises(SystemExit):
            main(['remote-window-collect','--db',str(self.db),'--task-file',str(self.path),'--plan-file',str(self.plan),
                '--store-id','900001','--resume-task','--resume-if-present'])
        self.assertFalse(self.path.exists())

    def test_existing_task_still_requires_resume_by_default(self):
        config=self.pending()
        with self.assertRaisesRegex(RemoteTaskError,'exists_use_resume'):
            RemoteWindowTask(self.path,config=config,resume=False,now=self.clock.wall())

    def test_explicit_container_bind_keeps_single_worker_no_proxy_and_cleanup(self):
        class Service:
            stopped=False
            def shutdown(self):self.stopped=True
        service=Service()
        class Runner:
            def run(self,app,**kwargs):self.kwargs=kwargs
        runner=Runner()
        with patch.dict('sys.modules',{'uvicorn':runner}):serve_local(service,listen_host='0.0.0.0')
        self.assertEqual(runner.kwargs['host'],'0.0.0.0');self.assertEqual(runner.kwargs['workers'],1)
        self.assertFalse(runner.kwargs['proxy_headers']);self.assertFalse(runner.kwargs['access_log'])
        self.assertTrue(service.stopped)
        with self.assertRaises(RemoteServiceError):serve_local(service,listen_host='example.com')

    def test_missing_checkpoint_service_fails_before_client_construction(self):
        self.pending();self.path.unlink();before=self.db.read_bytes()
        factory=lambda:(_ for _ in ()).throw(AssertionError('unexpected_client'))
        service=RemoteWindowService(db=self.db,task_file=self.path,plan_file=self.plan,
            store_ids=['900001'],base_interval=60,duration_seconds=180,max_pairs=20,
            resume_if_present=True,wall_clock=self.clock.wall,client_factory=factory)
        try:
            with self.assertRaisesRegex(RemoteServiceError,'missing_with_existing_database'):service.start()
            self.assertEqual(service.status()['service_state'],'failed')
            self.assertEqual(before,self.db.read_bytes());self.assertFalse(self.path.exists())
        finally:service.shutdown()

    def test_second_auto_instance_cannot_take_busy_checkpoint(self):
        with self.auto():
            with self.assertRaisesRegex(RemoteTaskError,'busy'):self.auto()
        self.assertFalse(self.db.exists());self.assertEqual(self.opener.calls,[])

if __name__=='__main__':unittest.main()
