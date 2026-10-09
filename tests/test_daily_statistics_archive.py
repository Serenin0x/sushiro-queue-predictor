import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('daily_archive', Path(__file__).resolve().parents[1] / 'deploy/archive_daily_statistics.py')
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)


class Response:
    def __init__(self, value):
        self.raw = json.dumps(value).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, limit):
        return self.raw[:limit]


class DailyArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.config = {'workers': [{'endpoint': 'http://127.0.0.1:18801', 'stores': {'3014': 'Test'}}]}
        self.value = {'requested_store_id': '3014', 'local_date': '2026-10-09',
                      'network_performed_by_read': False, 'graph_truncated': False,
                      'points': [{'labels': ['1', '2', '3'], 'padding': 'x' * 30}] * 660,
                      'summary': {'observations': 660}}

    def export(self, value=None):
        with patch.object(archive.urllib.request, 'build_opener') as factory:
            factory.return_value.open.return_value = Response(self.value if value is None else value)
            return archive.export_day(self.root, self.config, '2026-10-09')

    def test_large_daily_projection_is_saved_completely_with_digest(self):
        proof = self.export()
        row = proof['stores'][0]
        path = self.root / 'daily-exports/2026-10-09/store-3014.json'
        self.assertGreater(path.stat().st_size, 16384)
        self.assertTrue(row['complete_observed_projection'])
        self.assertEqual(json.loads(archive.read_private_archive(path)), self.value)
        self.assertEqual(row['sha256'], archive.hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(proof['official_requests_added_by_export'], 0)

    def test_immutable_export_never_overwrites_previous_points(self):
        self.export()
        changed = dict(self.value, points=[], summary=None)
        proof = self.export(changed)
        row = proof['stores'][0]
        self.assertEqual(row['state'], 'already_saved')
        self.assertEqual(row['points'], 660)

    def test_truncated_or_mismatched_count_is_not_claimed_complete(self):
        proof = self.export(dict(self.value, graph_truncated=True))
        self.assertFalse(proof['stores'][0]['complete_observed_projection'])
        with tempfile.TemporaryDirectory() as second:
            self.root = Path(second)
            proof = self.export(dict(self.value, summary={'observations': 700}))
            self.assertFalse(proof['stores'][0]['complete_observed_projection'])

    def test_wrong_store_date_or_network_scope_is_rejected(self):
        for change in [{'requested_store_id': '3004'}, {'local_date': '2026-10-08'},
                       {'network_performed_by_read': True}]:
            proof = self.export(dict(self.value, **change))
            self.assertEqual(proof['stores'][0]['error_type'], 'ValueError')
        self.assertFalse((self.root / 'daily-exports/2026-10-09/store-3014.json').exists())

    def test_symlink_or_public_archive_is_rejected(self):
        target = self.root / 'private.json'
        archive.immutable_json(target, {'safe': True})
        linked = self.root / 'linked.json'
        linked.symlink_to(target)
        with self.assertRaises(ValueError):
            archive.immutable_json(linked, {'safe': False})
        target.chmod(0o644)
        with self.assertRaises(ValueError):
            archive.read_private_archive(target)

    def test_oversized_archive_rejected_without_creating_final_file(self):
        target = self.root / 'large.json'
        with self.assertRaises(ValueError):
            archive.immutable_json(target, {'text': 'x' * archive.MAX_ARCHIVE_BYTES})
        self.assertFalse(target.exists())

    def test_noncanonical_date_cannot_create_path(self):
        with self.assertRaises(ValueError):
            archive.export_day(self.root, self.config, '../2026-10-09')
        self.assertFalse((self.root / 'daily-exports').exists())


if __name__ == '__main__':
    unittest.main()
