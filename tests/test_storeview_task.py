"""A store view cannot infer collection health from another store's last response."""
import contextlib
from datetime import timedelta
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sushiwait.cli import main
from sushiwait.storage import SnapshotStore
from sushiwait.storeview import store_view
from sushiwait.tasks import CollectionTask, TaskError
from test_storeview import BASE, snapshot, stamp


class StoreViewTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name).resolve()
        self.parent.chmod(0o700)
        self.path = self.parent / 'samples.sqlite3'
        self.task_file = self.parent / 'collection.json'
        self.config = dict(db=str(self.path), api_profile='miniapp_gateway',
            store_ids=['900001', '900002'], interval=30, samples=3, wait_for_credentials=600)

    def prepare(self, *, stopped=False, completed=False, pending=False):
        if completed:
            self.config['samples'] = 1
        with SnapshotStore(self.path) as db, CollectionTask(self.task_file,
                config=self.config, resume=False, now=BASE) as task:
            task.prepare_database()
            task.bind(db, now=BASE)
            for sid in self.config['store_ids']:
                task.begin(now=BASE)
                db.save(snapshot(0, store_id=sid, origin='live'))
                task.reconcile(now=BASE)
            if stopped:
                task.stop('auth_expiring', now=BASE+timedelta(seconds=30))
                db.save_preflight_stop('900001', 'auth_expiring', checked_at=stamp(30),
                    data_origin='live', api_profile='miniapp_gateway')
            if pending:
                task.begin(now=BASE+timedelta(seconds=30))

    def view(self, *, sid='900002', second=60, **options):
        with SnapshotStore(self.path, read_only=True) as db:
            return store_view(db, sid, data_origin='live', api_profile='miniapp_gateway',
                as_of=stamp(second), task_file=str(self.task_file), **options)

    def edit(self, callback):
        value=json.loads(self.task_file.read_text())
        callback(value)
        self.task_file.write_text(json.dumps(value))

    def test_global_stop_marks_other_store_last_known(self):
        self.prepare(stopped=True)
        value=self.view()
        self.assertEqual(value['refresh_state'], 'response_received')
        self.assertEqual(value['availability'], 'last_known_only')
        self.assertTrue(value['display_is_last_known'])
        self.assertTrue(value['collector_state_available'])
        self.assertEqual(value['collector_checkpoint']['state'], 'stopped')
        self.assertEqual(value['collector_checkpoint']['stop_reason'], 'auth_expiring')
        self.assertEqual(value['collector_liveness'], 'unknown')
        self.assertFalse(value['collector_checkpoint']['is_live_process_health_check'])

    def test_recent_ready_checkpoint_does_not_assert_process_liveness(self):
        self.prepare()
        value=self.view()
        self.assertEqual(value['availability'], 'recent_response')
        self.assertEqual(value['collector_checkpoint']['checkpoint_age_seconds'],60)
        self.assertEqual(value['collector_checkpoint']['process_liveness'],'unknown')
        self.assertFalse(value['eta_available'])

    def test_completed_task_marks_recent_display_last_known(self):
        self.prepare(completed=True)
        value=self.view(second=0)
        self.assertEqual(value['collector_checkpoint']['state'], 'completed')
        self.assertTrue(value['collector_checkpoint']['all_slots_successful'])
        self.assertEqual(value['availability'], 'last_known_only')

    def test_old_checkpoint_blocks_recent_new_response(self):
        self.prepare()
        with SnapshotStore(self.path) as db:
            db.save(snapshot(100, origin='live', store_id='900002'))
        value=self.view(second=100)
        self.assertEqual(value['last_response_age_seconds'],0)
        self.assertEqual(value['collector_checkpoint']['checkpoint_age_status'],'stale')
        self.assertEqual(value['availability'],'last_known_only')

    def test_pending_query_is_a_saved_attempt_not_a_live_process(self):
        self.prepare(pending=True)
        value=self.view(second=30)
        self.assertTrue(value['collector_checkpoint']['pending_attempt'])
        self.assertEqual(value['collector_checkpoint']['state'],'running')
        self.assertEqual(value['collector_liveness'],'unknown')

    def test_future_checkpoint_rejected_for_historical_projection(self):
        self.prepare(stopped=True)
        with self.assertRaises(TaskError) as error:
            self.view(second=20)
        self.assertEqual(error.exception.error_code,'collection_task_view_future_checkpoint')

    def test_store_profile_origin_and_database_must_match(self):
        self.prepare()
        for mutate in (lambda v:v['config'].update(store_ids=['900001']),
                       lambda v:v['config'].update(api_profile='legacy'),
                       lambda v:v['config'].update(db=str(self.parent/'other.sqlite3'))):
            original=self.task_file.read_bytes()
            self.edit(mutate)
            with self.assertRaises(TaskError):self.view()
            self.task_file.write_bytes(original)
        with SnapshotStore(self.path, read_only=True) as db, self.assertRaises(TaskError):
            store_view(db,'900002',data_origin='synthetic',api_profile='miniapp_gateway',
                as_of=stamp(60),task_file=str(self.task_file))

    def test_replaced_database_identity_and_changed_last_row_rejected(self):
        self.prepare()
        self.edit(lambda v:v.update(database_identity=[0,0]))
        with self.assertRaises(TaskError):self.view()
        info=self.path.stat()
        self.edit(lambda v:v.update(database_identity=[info.st_dev,info.st_ino]))
        with SnapshotStore(self.path) as db:
            db.db.execute("UPDATE samples SET payload_json='{}' WHERE id=2")
            db.db.commit()
        with self.assertRaises(TaskError):self.view()

    def test_wrong_last_row_scope_even_with_recomputed_hash_rejected(self):
        self.prepare()
        from sushiwait.tasks import _digest
        with SnapshotStore(self.path) as db:
            db.db.execute("UPDATE samples SET data_origin='synthetic' WHERE id=2")
            db.db.commit()
            row=db.db.execute('SELECT id,run_id,store_id,data_origin,api_profile,ok,payload_json FROM samples WHERE id=2').fetchone()
        self.edit(lambda v:v['last_sample'].update(digest=_digest(row)))
        with self.assertRaises(TaskError):self.view()

    def test_last_row_cannot_have_been_recorded_after_checkpoint(self):
        self.prepare()
        with SnapshotStore(self.path) as db:
            db.db.execute('UPDATE samples SET received_at=? WHERE id=2',(stamp(30),))
            db.db.commit()
        with self.assertRaises(TaskError):self.view()

    def test_private_input_permissions_and_symlink_rejected(self):
        self.prepare()
        self.task_file.chmod(0o644)
        with self.assertRaises(TaskError):self.view()
        self.task_file.chmod(0o600)
        moved=self.task_file.with_suffix('.saved');self.task_file.rename(moved)
        self.task_file.symlink_to(moved)
        with self.assertRaises(TaskError):self.view()

    def test_projection_neither_locks_worker_nor_changes_database_or_task(self):
        self.prepare()
        before=[hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.path,self.task_file)]
        with patch('fcntl.flock',side_effect=AssertionError('worker lock acquired')), \
             patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('credentials read')), \
             patch('subprocess.Popen',side_effect=AssertionError('child created')), \
             patch('socket.socket',side_effect=AssertionError('network created')):
            result=self.view()
        after=[hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.path,self.task_file)]
        self.assertEqual(before,after)
        text=json.dumps(result)
        for private in (str(self.parent),'database_identity','context_digest','authorization_digest',
                        'credential_revision','last_sample','run_id'):
            self.assertNotIn(private,text)

    def test_cli_rejects_bad_link_without_leaking_path_or_creating_missing_task(self):
        self.prepare()
        self.task_file.unlink()
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            code=main(['store-view','--db',str(self.path),'--store-id','900002',
                '--api-profile','miniapp_gateway','--data-origin','live','--as-of',stamp(60),
                '--task-file',str(self.task_file)])
        self.assertEqual(code,1)
        self.assertFalse(self.task_file.exists())
        self.assertNotIn(str(self.parent),output.getvalue())
        self.assertFalse(json.loads(output.getvalue())['network_performed'])
