"""Crash/restart checkpoints, exact result reconciliation and private boundaries."""

import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sushiwait.cli import main
from sushiwait.client import QueryResult
from sushiwait.storage import SnapshotStore
from sushiwait.tasks import CollectionTask, TaskError, task_status
from test_collection_credentials import bundle, jwt


BASE = datetime(2026, 10, 6, 4, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.value = 0
        self.callback = None

    def now(self):
        return BASE + timedelta(seconds=self.value)

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds
        if self.callback:
            self.callback(self.value)


class CollectionTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name).resolve()
        self.parent.chmod(0o700)
        self.task = self.parent / 'collection.json'
        self.db = self.parent / 'samples.sqlite3'
        self.context = self.parent / 'context.json'
        self.clock = Clock()
        self.requests = []
        self.failure = None
        self.before_request = None
        self.write_context(bundle())

    def write_context(self, value):
        p = self.parent / 'next-context.json'
        p.write_text(json.dumps(value))
        p.chmod(0o600)
        p.replace(self.context)

    def args(self, *, resume=False, ids=('900001',), samples=3, interval=30):
        args = ['collect', '--api-profile', 'miniapp_gateway', '--credentials-file', str(self.context),
                '--db', str(self.db), '--task-file', str(self.task), '--interval', str(interval), '--samples', str(samples)]
        for sid in ids:
            args += ['--store-id', sid]
        if resume:
            args += ['--resume-task']
        return args

    def run_cli(self, args):
        outer = self
        class Client:
            def fetch_store(self, store_id):
                outer.requests.append((store_id, outer.clock.value))
                if outer.before_request:
                    outer.before_request(store_id)
                now = outer.clock.now().isoformat()
                if outer.failure:
                    return QueryResult(False, None, 'http_error', outer.failure, now, now, 1)
                return QueryResult(True, {'id': int(store_id), 'name': '合成门店', 'wait': 1},
                                   None, 200, now, now, 1)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), \
                patch('sushiwait.cli.client_for', return_value=Client()), \
                patch('sushiwait.cli._utc_clock', side_effect=self.clock.now), \
                patch('sushiwait.cli.time.monotonic', side_effect=self.clock.monotonic), \
                patch('sushiwait.cli.time.sleep', side_effect=self.clock.sleep), \
                patch('socket.socket', side_effect=AssertionError('unexpected network')):
            code = main(args)
        text = output.getvalue()
        for secret in ('synthetic-old-query-secret', 'synthetic-old-code-secret', 'synthetic-old-client-secret',
                       str(self.context), str(self.parent)):
            self.assertNotIn(secret, text)
        return code, [json.loads(s) for s in text.splitlines()]

    def raw(self):
        return json.loads(self.task.read_text())

    def count(self):
        with SnapshotStore(self.db, read_only=True) as db:
            return sum(g['successful_samples'] for g in db.report()['groups'])

    def interrupt_after_commit(self):
        original = CollectionTask.reconcile
        def interrupted(task, *, now, interrupted=False):
            if task.value['cursor'] == 0 and not interrupted:
                raise KeyboardInterrupt
            return original(task, now=now, interrupted=interrupted)
        with patch.object(CollectionTask, 'reconcile', new=interrupted):
            code, rows = self.run_cli(self.args())
        self.assertEqual(code, 130)
        self.assertTrue(self.raw()['pending'])
        self.assertEqual(self.count(), 1)

    def test_complete_task_checkpoints_private_no_raw_context(self):
        code, _ = self.run_cli(self.args())
        self.assertEqual(code, 0)
        self.assertEqual(self.requests, [('900001', 0), ('900001', 30), ('900001', 60)])
        summary = task_status(self.task)
        self.assertTrue(summary['all_slots_successful'])
        self.assertEqual(summary['completed_slots'], 3)
        self.assertEqual(self.count(), 3)
        self.assertEqual(self.task.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.task.with_name(self.task.name + '.lock').stat().st_mode & 0o777, 0o600)
        for secret in ('synthetic-old-query-secret', 'synthetic-old-code-secret', 'synthetic-old-client-secret'):
            self.assertNotIn(secret, self.task.read_text())
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)

    def test_saved_result_before_checkpoint_is_reconciled_once(self):
        self.interrupt_after_commit()
        self.requests.clear()
        self.clock.value = 10
        code, _ = self.run_cli(self.args(resume=True))
        self.assertEqual(code, 0)
        self.assertEqual(self.requests, [('900001', 40), ('900001', 70)])
        self.assertEqual(self.count(), 3)
        self.assertEqual(self.raw()['successful'], 3)

    def test_interrupted_attempt_without_result_is_unknown_never_fabricated(self):
        self.before_request = lambda _: (_ for _ in ()).throw(KeyboardInterrupt())
        code, _ = self.run_cli(self.args())
        self.assertEqual(code, 130)
        self.assertEqual(self.count(), 0)
        self.before_request = None
        self.requests.clear()
        self.clock.value = 10
        code, _ = self.run_cli(self.args(resume=True))
        self.assertEqual(code, 1)
        self.assertEqual(self.requests, [('900001', 40), ('900001', 70)])
        self.assertEqual(self.count(), 2)
        summary = task_status(self.task)
        self.assertEqual(summary['uncertain_slots'], 1)
        self.assertFalse(summary['all_slots_successful'])
        self.assertEqual(summary['last_gap']['reason'], 'uncertain_attempt')

    def test_real_child_process_exit_after_sqlite_commit_restores_without_duplicate(self):
        code = '''import sys,json,os
from pathlib import Path
from datetime import datetime,timezone
sys.path.insert(0,sys.argv[1])
from sushiwait.tasks import CollectionTask
from sushiwait.storage import SnapshotStore
from sushiwait.observations import normalize_snapshot
from sushiwait.credentials import read_credentials_file
now=datetime(2026,10,6,4,0,tzinfo=timezone.utc)
config={'db':sys.argv[3],'api_profile':'miniapp_gateway','store_ids':['900001'],'interval':30,'samples':3,'wait_for_credentials':0}
with CollectionTask(sys.argv[2],config=config,resume=False,now=now) as task:
 task.prepare_database()
 with SnapshotStore(sys.argv[3]) as db:
  task.bind(db,now=now)
  task.check_context(read_credentials_file(sys.argv[4],api_profile='miniapp_gateway'),None)
  task.begin(now=now)
  db.save(normalize_snapshot({'id':900001,'name':'synthetic'},'900001',request_started_at=now.isoformat(),received_at=now.isoformat(),elapsed_ms=0,data_origin='live',api_profile='miniapp_gateway'))
  os._exit(23)
'''
        source = str(Path(__file__).resolve().parents[1] / 'src')
        r = subprocess.run([sys.executable, '-c', code, source, str(self.task), str(self.db), str(self.context)],
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 23, r.stderr)
        self.assertTrue(self.raw()['pending'])
        self.clock.value = 10
        code, _ = self.run_cli(self.args(resume=True))
        self.assertEqual(code, 0)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.count(), 3)

    def test_existing_task_requires_explicit_resume_without_database_changes(self):
        self.run_cli(self.args(samples=1))
        before = self.db.read_bytes(), self.task.read_bytes()
        self.requests.clear()
        code, rows = self.run_cli(self.args(samples=1))
        self.assertEqual(code, 1)
        self.assertEqual(rows[0]['error_code'], 'collection_task_exists_use_resume')
        self.assertFalse(self.requests)
        self.assertEqual(before, (self.db.read_bytes(), self.task.read_bytes()))

    def test_missing_resume_never_creates_database(self):
        code, rows = self.run_cli(self.args(resume=True))
        self.assertEqual(code, 1)
        self.assertEqual(rows[0]['error_code'], 'collection_task_missing')
        self.assertFalse(self.db.exists())

    def test_changed_config_rejected_before_database_or_client(self):
        self.interrupt_after_commit()
        before = self.db.read_bytes(), self.task.read_bytes()
        self.requests.clear()
        for args in (self.args(resume=True, interval=60), self.args(resume=True, samples=4),
                     self.args(resume=True, ids=('900002',))):
            with self.subTest(args=args):
                code, rows = self.run_cli(args)
                self.assertEqual(code, 1)
                self.assertEqual(rows[0]['error_code'], 'collection_task_config_conflict')
                self.assertFalse(self.requests)
                self.assertEqual(before, (self.db.read_bytes(), self.task.read_bytes()))

    def test_cooperating_worker_lock_prevents_second_worker(self):
        config = {'db': str(self.db), 'api_profile': 'miniapp_gateway', 'store_ids': ['900001'],
                  'interval': 30, 'samples': 3, 'wait_for_credentials': 0}
        with CollectionTask(self.task, config=config, resume=False, now=BASE):
            with self.assertRaisesRegex(TaskError, 'collection_task_busy'):
                CollectionTask(self.task, config=config, resume=False, now=BASE)
        self.assertFalse(self.db.exists())

    def test_database_replaced_rejected_without_queries(self):
        self.interrupt_after_commit()
        other = self.parent / 'other.sqlite3'
        with SnapshotStore(other):
            pass
        other.replace(self.db)
        before = self.db.read_bytes()
        self.requests.clear()
        code, rows = self.run_cli(self.args(resume=True))
        self.assertEqual(code, 1)
        self.assertEqual(rows[-1]['error_code'], 'collection_task_database_changed')
        self.assertFalse(self.requests)
        self.assertEqual(before, self.db.read_bytes())

    def test_missing_resumed_database_is_not_recreated(self):
        self.interrupt_after_commit()
        self.db.unlink()
        self.requests.clear()
        code, _ = self.run_cli(self.args(resume=True))
        self.assertEqual(code, 1)
        self.assertFalse(self.db.exists())
        self.assertFalse(self.requests)

    def test_changed_committed_row_is_detected(self):
        self.run_cli(self.args(samples=1))
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE samples SET payload_json='{}'")
        before = self.db.read_bytes()
        self.requests.clear()
        code, rows = self.run_cli(self.args(resume=True, samples=1))
        self.assertEqual(code, 1)
        self.assertEqual(rows[-1]['error_code'], 'collection_task_database_changed')
        self.assertFalse(self.requests)
        self.assertEqual(before, self.db.read_bytes())

    def test_unsafe_state_and_database_modes_are_not_repaired(self):
        self.interrupt_after_commit()
        self.requests.clear()
        self.task.chmod(0o644)
        code, _ = self.run_cli(self.args(resume=True))
        self.assertEqual(code, 1)
        self.assertEqual(self.task.stat().st_mode & 0o777, 0o644)
        self.task.chmod(0o600)
        self.db.chmod(0o644)
        code, rows = self.run_cli(self.args(resume=True))
        self.assertEqual(code, 1)
        self.assertEqual(rows[-1]['error_code'], 'collection_task_database_unsafe')
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o644)
        self.assertFalse(self.requests)

    def test_context_revision_and_bundle_consistency_survive_restart(self):
        self.write_context(bundle(2))
        self.interrupt_after_commit()
        self.requests.clear()
        for value, error in ((bundle(1), 'credentials_revision_rollback'),
                             (bundle(2, app_code='changed-private-marker'), 'credentials_revision_conflict')):
            with self.subTest(error=error):
                self.write_context(value)
                code, rows = self.run_cli(self.args(resume=True))
                self.assertEqual(code, 1)
                self.assertEqual(rows[-1]['stop_reason'], error)
                self.assertFalse(self.requests)

    def test_expiry_checkpoint_cannot_be_reused_after_wall_clock_rollback(self):
        token = jwt(exp=BASE-timedelta(seconds=1), iat=BASE-timedelta(hours=1))
        self.write_context(bundle(1, token))
        code, _ = self.run_cli(self.args())
        self.assertEqual(code, 1)
        self.assertFalse(self.requests)
        # A declared-time check alone would become valid under this changed clock.
        self.clock.value = -60
        code, rows = self.run_cli(self.args(resume=True))
        self.assertEqual(code, 1)
        self.assertEqual(rows[-1]['error_code'], 'collection_task_clock_rollback')
        self.assertFalse(self.requests)

    def test_failed_http_request_is_not_replayed_and_401_requires_new_authorization(self):
        self.failure = 401
        code, _ = self.run_cli(self.args())
        self.assertEqual(code, 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.raw()['cursor'], 1)
        self.failure = None
        self.requests.clear()
        self.clock.value = 10
        code, rows = self.run_cli(self.args(resume=True))
        self.assertEqual(code, 1)
        self.assertFalse(self.requests)
        self.assertEqual(rows[-1]['stop_reason'], 'credentials_refresh_required')
        self.write_context(bundle(2, 'synthetic-new-query-secret'))
        code, _ = self.run_cli(self.args(resume=True))
        self.assertEqual(code, 1)  # Prior failed slot remains a real failure.
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.raw()['failed'], 1)

    def test_first_429_stops_without_retry_or_wait(self):
        self.failure = 429
        code, _ = self.run_cli(self.args())
        self.assertEqual(code, 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.clock.value, 0)

    def test_partial_round_resumes_at_next_store_with_full_interval(self):
        original = CollectionTask.begin
        def interrupted(task, *, now):
            if task.value['cursor'] == 1:
                raise KeyboardInterrupt
            return original(task, now=now)
        ids = ('900001', '900002', '900003')
        with patch.object(CollectionTask, 'begin', new=interrupted):
            code, _ = self.run_cli(self.args(ids=ids, samples=1))
        self.assertEqual(code, 130)
        self.requests.clear()
        self.clock.value = 10
        code, _ = self.run_cli(self.args(resume=True, ids=ids, samples=1))
        self.assertEqual(code, 0)
        self.assertEqual(self.requests, [('900002', 40), ('900003', 40)])
        self.assertEqual(self.count(), 3)

    def test_status_and_completed_resume_do_not_construct_client_or_read_context(self):
        self.run_cli(self.args(samples=1))
        before = self.db.read_bytes(), self.task.read_bytes()
        self.context.unlink()
        self.requests.clear()
        with patch('sushiwait.cli._QuerySession', side_effect=AssertionError('context/client not needed')):
            code, rows = self.run_cli(['task-status', '--task-file', str(self.task)])
            self.assertEqual(code, 0)
            self.assertFalse(rows[0]['network_performed'])
            code, _ = self.run_cli(self.args(resume=True, samples=1))
            self.assertEqual(code, 0)
        self.assertEqual(before, (self.db.read_bytes(), self.task.read_bytes()))
        self.assertFalse(self.requests)

    def test_malformed_or_oversized_status_is_safe_and_offline(self):
        self.run_cli(self.args(samples=1))
        for body in ('private-malformed-marker', '{"schema_version":1,"schema_version":1}', 'x'*17000):
            with self.subTest(body_size=len(body)):
                self.task.write_text(body)
                code, rows = self.run_cli(['task-status', '--task-file', str(self.task)])
                self.assertEqual(code, 1)
                self.assertNotIn(body, json.dumps(rows))
                self.assertFalse(rows[0]['network_performed'])

    def test_conflicting_paths_or_missing_private_mode_fail_before_writes(self):
        args = self.args()
        args[args.index('--task-file')+1] = str(self.context)
        before = self.context.read_bytes()
        code, _ = self.run_cli(args)
        self.assertEqual(code, 2)
        self.assertEqual(self.context.read_bytes(), before)
        self.assertFalse(self.db.exists())
        code, _ = self.run_cli(['collect', '--store-id', '900001', '--resume-task', '--db', str(self.db)])
        self.assertEqual(code, 2)
        self.assertFalse(self.db.exists())

    def test_pre_rename_write_failure_preserves_checkpoint_and_stops_before_get(self):
        original = CollectionTask.begin
        def failed(task, *, now):
            before = task.path.read_bytes()
            with patch('sushiwait.tasks.os.fsync', side_effect=OSError('private-disk-error')):
                with self.assertRaisesRegex(TaskError, 'collection_task_write_failed'):
                    original(task, now=now)
            self.assertEqual(task.path.read_bytes(), before)
            raise TaskError('collection_task_write_failed')
        with patch.object(CollectionTask, 'begin', new=failed):
            code, rows = self.run_cli(self.args())
        self.assertEqual(code, 1)
        self.assertEqual(rows[-1]['error_code'], 'collection_task_write_failed')
        self.assertFalse(self.requests)
        self.assertFalse(list(self.parent.glob('*.tmp')))

    def test_post_rename_durability_failure_is_committed_and_no_query_sent(self):
        original = CollectionTask.begin
        real = os.fsync
        def failed(task, *, now):
            def fsync(fd):
                if fd == task.parent_fd:
                    raise OSError('private-dir-error')
                return real(fd)
            with patch('sushiwait.tasks.os.fsync', side_effect=fsync):
                return original(task, now=now)
        with patch.object(CollectionTask, 'begin', new=failed):
            code, rows = self.run_cli(self.args())
        self.assertEqual(code, 1)
        self.assertEqual(rows[-1]['error_code'], 'collection_task_durability_unconfirmed')
        self.assertTrue(self.raw()['pending'])
        self.assertFalse(self.requests)

    def test_ambiguous_pending_database_results_stop_without_guessing(self):
        self.interrupt_after_commit()
        with sqlite3.connect(self.db) as db:
            db.execute('INSERT INTO samples(run_id,store_id,data_origin,api_profile,received_at,ok,payload_json) '
                       'SELECT run_id,store_id,data_origin,api_profile,received_at,ok,payload_json FROM samples LIMIT 1')
        self.requests.clear()
        before = self.task.read_bytes()
        code, rows = self.run_cli(self.args(resume=True))
        self.assertEqual(code, 1)
        self.assertEqual(rows[-1]['error_code'], 'collection_task_result_conflict')
        self.assertFalse(self.requests)
        self.assertEqual(before, self.task.read_bytes())

    def test_task_or_lock_replacement_during_wait_stops_before_next_query(self):
        for name in ('collection.json', 'collection.json.lock'):
            with self.subTest(name=name):
                path = self.parent / name
                def replace(_):
                    if len(self.requests) != 1:
                        return
                    other = self.parent / 'replacement-private.json'
                    other.write_bytes(path.read_bytes())
                    other.chmod(0o600)
                    other.replace(path)
                self.clock.callback = replace
                code, rows = self.run_cli(self.args())
                self.assertEqual(code, 1)
                self.assertEqual(len(self.requests), 1)
                self.assertIn(rows[-1]['error_code'], ('collection_task_changed', 'collection_task_lock_changed'))
                self.clock.callback = None
                self.task.unlink()
                self.db.unlink()
                self.requests.clear()
                self.clock.value = 0

    def test_symlinked_task_and_database_paths_are_rejected(self):
        victim = self.parent / 'victim-private.json'
        victim.write_text('private-victim-marker')
        victim.chmod(0o600)
        self.task.symlink_to(victim)
        code, _ = self.run_cli(self.args())
        self.assertEqual(code, 1)
        self.assertEqual(victim.read_text(), 'private-victim-marker')
        self.assertFalse(self.requests)
        self.task.unlink()
        self.db.symlink_to(victim)
        code, _ = self.run_cli(self.args())
        self.assertEqual(code, 1)
        self.assertFalse(self.requests)
        self.assertEqual(victim.read_text(), 'private-victim-marker')

    def test_persistent_expiry_wait_accepts_complete_new_context(self):
        old = jwt(exp=BASE-timedelta(seconds=1), iat=BASE-timedelta(hours=1))
        new = jwt(exp=BASE+timedelta(hours=1), iat=BASE)
        self.write_context(bundle(1, old))
        self.clock.callback = lambda seconds: self.write_context(bundle(2, new)) if seconds == 1 else None
        args = self.args(samples=1) + ['--wait-for-credentials', '3']
        code, rows = self.run_cli(args)
        self.assertEqual(code, 0)
        self.assertEqual(self.requests, [('900001', 1)])
        self.assertEqual(self.raw()['credential']['revision'], 2)
        self.assertIsNone(self.raw()['credential']['blocked_authorization_digest'])
        self.assertEqual(sum(r.get('event') == 'credentials_resumed' for r in rows), 1)

    def test_higher_revision_with_blocked_same_authorization_cannot_resume(self):
        self.failure = 401
        self.run_cli(self.args())
        self.failure = None
        self.requests.clear()
        self.write_context(bundle(2))
        code, rows = self.run_cli(self.args(resume=True))
        self.assertEqual(code, 1)
        self.assertFalse(self.requests)
        self.assertEqual(rows[-1]['stop_reason'], 'credentials_refresh_required')


if __name__ == '__main__':
    unittest.main()
