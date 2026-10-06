import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sushiwait.cli import main, _transient_failure_after
from sushiwait.client import QueryResult
from sushiwait.storage import SnapshotStore
from sushiwait.tasks import CollectionTask, TaskError, task_status
from test_collection_credentials import bundle
from test_collection_tasks import Clock


class TransientSamplingTests(unittest.TestCase):
    def setup_case(self, *, persistent, budget=1, samples=2, statuses=(504, None, None, None), durations=()):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.parent.chmod(0o700)
        self.context = self.parent / 'context.json'
        self.context.write_text(json.dumps(bundle()))
        self.context.chmod(0o600)
        self.path = self.parent / 'samples.sqlite3'
        self.task = self.parent / 'collection.json'
        self.clock = Clock()
        self.requests = []
        self.statuses, self.durations = list(statuses), list(durations)
        self.args = ['collect', '--api-profile', 'miniapp_gateway', '--credentials-file', str(self.context),
            '--db', str(self.path), '--store-id', '900001', '--store-id', '900002',
            '--interval', '30', '--samples', str(samples), '--transient-failure-budget', str(budget)]
        if persistent: self.args += ['--task-file', str(self.task)]

    def run_case(self, extra=()):
        outer = self
        class Client:
            def fetch_store(self, sid):
                number = len(outer.requests)
                outer.requests.append((sid, outer.clock.value))
                started = outer.clock.now().isoformat()
                duration = outer.durations[number] if number < len(outer.durations) else 0
                outer.clock.value += duration
                received = outer.clock.now().isoformat()
                status = outer.statuses[number] if number < len(outer.statuses) else None
                if status == 'malformed':
                    return QueryResult(True, {'id': 'invalid'}, None, 200, started, received, duration * 1000)
                if status is not None:
                    return QueryResult(False, None, 'http_error', status, started, received, duration * 1000)
                return QueryResult(True, {'id': int(sid), 'storeStatus': 'OPEN'}, None, 200,
                                   started, received, duration * 1000)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch('sushiwait.cli.client_for', return_value=Client()), \
                patch('sushiwait.cli._utc_clock', side_effect=self.clock.now), \
                patch('sushiwait.cli.time.monotonic', side_effect=self.clock.monotonic), \
                patch('sushiwait.cli.time.sleep', side_effect=self.clock.sleep), \
                patch('socket.socket', side_effect=AssertionError('unexpected network')):
            code = main(self.args + list(extra))
        return code, [json.loads(s) for s in output.getvalue().splitlines()]

    def test_enabled_policy_records_failure_and_finishes_later_slots_without_repeating_it(self):
        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                self.setup_case(persistent=persistent)
                code, rows = self.run_case()
                self.assertEqual(code, 1)
                self.assertEqual(self.requests, [('900001', 0), ('900002', 0), ('900001', 30), ('900002', 30)])
                with SnapshotStore(self.path, read_only=True) as db:
                    self.assertEqual(db.db.execute('SELECT SUM(ok),COUNT(*) FROM samples').fetchone(), (3, 4))
                events = [r for r in rows if r.get('event') == 'transient_query_failure_recorded']
                self.assertEqual(len(events), 1)
                self.assertFalse(events[0]['failed_slot_retried'])
                if persistent:
                    state = task_status(self.task)
                    self.assertEqual(state['state'], 'completed')
                    self.assertEqual(state['failed_slots'], 1)
                    self.assertFalse(state['all_slots_successful'])
                    self.assertEqual(state['transient_failure_budget'], 1)

    def test_default_zero_keeps_first_error_stop(self):
        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                self.setup_case(persistent=persistent, budget=0)
                code, rows = self.run_case()
                self.assertEqual(code, 1)
                self.assertEqual(self.requests, [('900001', 0)])
                self.assertFalse(any(r.get('event') == 'transient_query_failure_recorded' for r in rows))
                if persistent:
                    raw = json.loads(self.task.read_text())
                    self.assertNotIn('transient_failure_budget', raw['config'])

    def test_slow_failed_round_waits_full_interval_after_completion(self):
        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                self.setup_case(persistent=persistent, durations=(45,))
                self.assertEqual(self.run_case()[0], 1)
                self.assertEqual(self.requests, [('900001', 0), ('900002', 45), ('900001', 75), ('900002', 75)])

    def test_only_502_503_504_continue_and_all_other_http_errors_stop(self):
        for status in (400, 401, 403, 404, 429, 500, 501, 502, 503, 504, 505):
            for persistent in (False, True):
                with self.subTest(status=status, persistent=persistent):
                    self.setup_case(persistent=persistent, statuses=(status,))
                    with patch('sushiwait.cli._await_credentials', side_effect=AssertionError('no auth retry')):
                        self.assertEqual(self.run_case()[0], 1)
                    self.assertEqual(len(self.requests), 4 if status in (502, 503, 504) else 1)

    def test_budget_exhaustion_records_extra_failure_then_stops(self):
        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                self.setup_case(persistent=persistent, statuses=(504, 503))
                code, rows = self.run_case()
                self.assertEqual(code, 1)
                self.assertEqual(len(self.requests), 2)
                self.assertEqual(sum(r.get('event') == 'transient_query_failure_recorded' for r in rows), 1)
                if persistent:
                    state = task_status(self.task)
                    self.assertEqual(state['failed_slots'], 2)
                    self.assertEqual(state['state'], 'stopped')
                    self.assertFalse(state['pending_attempt'])

    def test_normalization_failure_never_uses_http_transient_budget(self):
        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                self.setup_case(persistent=persistent, statuses=('malformed',))
                code, rows = self.run_case()
                self.assertEqual(code, 1)
                self.assertEqual(len(self.requests), 1)
                self.assertFalse(any(r.get('event') == 'transient_query_failure_recorded' for r in rows))

    def interrupt_after_continuation(self):
        original = CollectionTask.continue_after_transient_failure
        def interrupted(task, *, now):
            original(task, now=now)
            raise KeyboardInterrupt
        with patch.object(CollectionTask, 'continue_after_transient_failure', new=interrupted):
            self.assertEqual(self.run_case()[0], 130)

    def test_restart_preserves_budget_and_does_not_repeat_failed_slot(self):
        self.setup_case(persistent=True)
        self.interrupt_after_continuation()
        self.assertEqual(task_status(self.task)['state'], 'ready')
        self.assertEqual(self.run_case(['--resume-task'])[0], 1)
        self.assertEqual(self.requests, [('900001', 0), ('900002', 30), ('900001', 60), ('900002', 60)])
        self.assertEqual(task_status(self.task)['failed_slots'], 1)

    def test_restart_cannot_reset_budget_or_silently_change_policy(self):
        self.setup_case(persistent=True, statuses=(504, 503))
        self.interrupt_after_continuation()
        code, _ = self.run_case(['--resume-task', '--transient-failure-budget', '2'])
        self.assertEqual(code, 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.run_case(['--resume-task'])[0], 1)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(task_status(self.task)['failed_slots'], 2)

    def test_invalid_budget_or_environment_only_mode_fails_before_io(self):
        for budget, credentials in ((-1, True), (11, True), (1, False)):
            args = ['collect', '--store-id', '900001', '--transient-failure-budget', str(budget)]
            if credentials: args += ['--credentials-file', '/synthetic/must-not-be-opened.json']
            with patch('sushiwait.cli.SnapshotStore', side_effect=AssertionError('database opened')), \
                    patch('sushiwait.cli.read_credentials_file', side_effect=AssertionError('auth read')), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 2)

    def test_old_failure_and_wrong_scope_cannot_authorize_continuation(self):
        self.setup_case(persistent=False)
        at = self.clock.now().isoformat()
        with SnapshotStore(self.path) as db:
            old_id = db.save_failure('900001', QueryResult(False, None, 'http_error', 504, at, at, 0),
                api_profile='miniapp_gateway')
            self.assertFalse(_transient_failure_after(db, '900001', 'miniapp_gateway', old_id))
            self.assertFalse(_transient_failure_after(db, '900002', 'miniapp_gateway', 0))
            self.assertFalse(_transient_failure_after(db, '900001', 'legacy', 0))

    def test_tampered_budget_is_rejected_when_reading_task(self):
        self.setup_case(persistent=True)
        self.interrupt_after_continuation()
        original = json.loads(self.task.read_text())
        for budget in (True, 0, 11, '1', None):
            with self.subTest(budget=budget):
                value = json.loads(json.dumps(original))
                value['config']['transient_failure_budget'] = budget
                self.task.write_text(json.dumps(value))
                with self.assertRaises(TaskError): task_status(self.task)

    def test_task_continuation_method_cannot_clear_401_stop(self):
        self.setup_case(persistent=True, budget=10, statuses=(401,))
        self.assertEqual(self.run_case()[0], 1)
        config = json.loads(self.task.read_text())['config']
        with CollectionTask(self.task, config=config, resume=True, now=self.clock.now()) as task:
            task.prepare_database()
            with SnapshotStore(self.path) as db:
                task.bind(db, now=self.clock.now())
                with self.assertRaises(TaskError): task.continue_after_transient_failure(now=self.clock.now())
                self.assertEqual(task.value['state'], 'stopped')
                self.assertIsNotNone(task.value['credential']['blocked_authorization_digest'])
