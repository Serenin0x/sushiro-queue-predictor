"""Local operator publication, strict input and no implicit worker acknowledgement."""
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

from sushiwait.planupdates import MAX_BYTES, MAX_REVISION, PlanUpdateError, publish_update, read_update

spec = importlib.util.spec_from_file_location('sushiwait_deploy_plan_admin',
    Path(__file__).resolve().parents[1] / 'deploy/plan_admin.py')
admin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admin)
NOW = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)


class PlanAdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.parent = Path(self.tmp.name).resolve()
        self.parent.chmod(0o700)
        self.path = self.parent / 'current.json'
    def tearDown(self): self.tmp.cleanup()
    def manage(self, action, **kwargs):
        return admin.manage(action, self.path, stores=['900001'], clock=lambda: NOW, **kwargs)
    def doc(self, minutes=20, **extra):
        return {'schema_version': 1, 'plans': [{'store_id': '900001',
            'desired_arrival_at': (NOW + timedelta(minutes=minutes)).isoformat(), **extra}]}
    def read(self): return read_update(self.path, stores=['900001'], base_interval=300, now=NOW)

    def test_init_creates_private_empty_feed_with_fresh_series(self):
        result = self.manage('init'); value = self.read()
        self.assertTrue(result['committed']); self.assertEqual(value['revision'], 1)
        self.assertEqual(UUID(value['series_id']).version, 4)
        self.assertEqual(value['document']['plans'], [])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(result['worker_application_verified'])
        self.assertEqual(sorted(p.name for p in self.parent.iterdir()), ['current.json'])

    def test_repeated_init_preserves_file_without_publish_or_repair(self):
        self.manage('init'); before = self.path.read_bytes()
        with patch.object(admin, 'publish_update', side_effect=AssertionError('publish')):
            result = self.manage('init')
        self.assertFalse(result['committed']); self.assertEqual(before, self.path.read_bytes())

    def test_status_requires_existing_feed_and_preserves_it(self):
        with self.assertRaisesRegex(admin.PlanAdminError, 'feed_missing'): self.manage('status')
        self.manage('init'); before = self.path.read_bytes()
        with patch.object(admin, 'publish_update', side_effect=AssertionError('publish')):
            self.assertEqual(self.manage('status')['revision'], 1)
        self.assertEqual(before, self.path.read_bytes())

    def test_corrupt_existing_feed_is_not_reinitialized(self):
        self.path.write_bytes(b'{'); self.path.chmod(0o600)
        with self.assertRaises(PlanUpdateError): self.manage('init')
        self.assertEqual(self.path.read_bytes(), b'{')

    def test_unsafe_parent_prevents_initialization(self):
        self.parent.chmod(0o755)
        with self.assertRaises(admin.PlanAdminError): self.manage('init')
        self.assertFalse(self.path.exists())

    def test_symlink_or_hardlink_feed_is_not_adopted(self):
        target = self.parent / 'target.json'; target.write_text('{}'); target.chmod(0o600)
        self.path.symlink_to(target)
        with self.assertRaises(PlanUpdateError): self.manage('init')
        self.path.unlink()
        import os
        os.link(target, self.path)
        with self.assertRaises(PlanUpdateError): self.manage('init')
        self.assertEqual(target.read_text(), '{}')

    def test_publish_keeps_series_and_replaces_entire_document(self):
        self.manage('init'); series = self.read()['series_id']
        first = self.doc(); first['plans'] *= 2
        self.assertEqual(self.manage('publish', document=first)['plan_count'], 2)
        self.assertEqual(self.manage('publish', document=self.doc(14))['revision'], 3)
        value = self.read(); self.assertEqual(value['series_id'], series)
        self.assertEqual(len(value['document']['plans']), 1)

    def test_clear_is_new_revision_and_does_not_stop_a_worker(self):
        self.manage('init'); self.manage('publish', document=self.doc())
        result = self.manage('clear')
        self.assertEqual(result['revision'], 3); self.assertEqual(result['plan_count'], 0)
        self.assertFalse(result['worker_application_verified'])

    def test_publish_and_clear_cannot_implicitly_initialize(self):
        for action in ('publish', 'clear'):
            with self.assertRaisesRegex(admin.PlanAdminError, 'feed_missing'):
                self.manage(action, document=self.doc())
        self.assertFalse(self.path.exists())

    def test_invalid_scope_or_borrowed_start_preserves_old_feed(self):
        self.manage('init'); before = self.path.read_bytes()
        doc = self.doc(); doc['plans'][0]['store_id'] = '900002'
        with self.assertRaises(PlanUpdateError): self.manage('publish', document=doc)
        doc = self.doc(); doc['last_poll_started_at'] = {'900001': NOW.isoformat()}
        with self.assertRaises(PlanUpdateError): self.manage('publish', document=doc)
        self.assertEqual(before, self.path.read_bytes())

    def test_revision_exhaustion_preserves_original(self):
        self.manage('init'); value = self.read(); value['revision'] = MAX_REVISION
        self.path.write_text(json.dumps(value)); before = self.path.read_bytes()
        with self.assertRaisesRegex(admin.PlanAdminError, 'revision_exhausted'): self.manage('clear')
        self.assertEqual(before, self.path.read_bytes())

    def test_future_existing_declaration_has_no_fallback(self):
        self.manage('init'); value = self.read(); value['declared_at'] = (NOW + timedelta(seconds=1)).isoformat()
        self.path.write_text(json.dumps(value)); before = self.path.read_bytes()
        with self.assertRaisesRegex(PlanUpdateError, 'future'): self.manage('clear')
        self.assertEqual(before, self.path.read_bytes())

    def test_concurrent_publisher_conflict_is_not_retried_or_overwritten(self):
        self.manage('init'); original = admin.publish_update; entered = False
        def raced(source, destination, **kwargs):
            nonlocal entered
            if not entered:
                entered = True; self.manage('clear')
            return original(source, destination, **kwargs)
        with patch.object(admin, 'publish_update', side_effect=raced):
            with self.assertRaisesRegex(PlanUpdateError, 'same_revision_conflict'):
                self.manage('publish', document=self.doc())
        self.assertEqual(self.read()['revision'], 2); self.assertEqual(self.read()['document']['plans'], [])
        self.assertEqual(sorted(p.name for p in self.parent.iterdir()), ['current.json'])

    def test_strict_stdin_rejects_duplicates_nonfinite_invalid_and_oversized(self):
        for raw in (b'{"plans":[],"plans":[]}', b'{"a":NaN}', b'{', b' ' * (MAX_BYTES + 1)):
            with self.assertRaises(admin.PlanAdminError): admin.decode_document(io.BytesIO(raw))

    def test_stdout_has_no_series_path_or_personal_arrival(self):
        args = ['--output', str(self.path), '--store-id', '900001']
        with patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(admin.main(['init', *args], clock=lambda: NOW), 0)
            self.assertEqual(admin.main(['publish', *args], stdin=io.BytesIO(json.dumps(self.doc()).encode()), clock=lambda: NOW), 0)
            text = out.getvalue()
        self.assertNotIn(str(self.path), text); self.assertNotIn(self.read()['series_id'], text)
        self.assertNotIn(self.doc()['plans'][0]['desired_arrival_at'], text)

    def test_invalid_stdin_returns_fixed_code_without_echoing_input(self):
        with patch('sys.stdout', new_callable=io.StringIO) as out:
            code = admin.main(['publish', '--output', str(self.path)], stdin=io.BytesIO(b'PRIVATE_BAD_DOCUMENT'))
        self.assertEqual(code, 1); self.assertNotIn('PRIVATE_BAD_DOCUMENT', out.getvalue())
        self.assertFalse(self.path.exists())

    def test_post_commit_uncertainty_is_forwarded_without_retry(self):
        value = {'ok': False, 'committed': True, 'durability_confirmed': False, 'revision': 1,
                 'plan_count': 0, 'network_performed': False, 'credentials_accessed': False,
                 'business_operation_performed': False, 'eta_available': False}
        with patch.object(admin, 'publish_update', return_value=value) as publish, \
                patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(admin.main(['init', '--output', str(self.path), '--store-id', '900001'], clock=lambda: NOW), 1)
        self.assertEqual(publish.call_count, 1); self.assertTrue(json.loads(out.getvalue())['committed'])
        self.assertFalse(json.loads(out.getvalue())['durability_confirmed'])

    def test_all_local_actions_have_no_socket_or_credentials(self):
        with patch('socket.socket', side_effect=AssertionError('network')) as sockets, \
                patch('sushiwait.credentials.read_credentials_file', side_effect=AssertionError('credentials')) as auth:
            self.manage('init'); self.manage('publish', document=self.doc()); self.manage('clear'); self.manage('status')
        self.assertEqual((sockets.call_count, auth.call_count), (0, 0))


if __name__ == '__main__': unittest.main()
