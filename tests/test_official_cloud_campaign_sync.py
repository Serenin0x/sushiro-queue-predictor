"""Isolated SQLite and crash replay; live-shaped fixtures are NOT real data."""
from datetime import datetime, timezone, timedelta
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sushiwait.observations import normalize_snapshot
from sushiwait.storage import SnapshotStore
from tests.test_official_cloud_campaign import plan

SPEC = importlib.util.spec_from_file_location('campaign_projection',
    Path(__file__).resolve().parents[1]/'deploy'/'official_cloud_campaign_sync.py')
project = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(project)
NOW = datetime(2026, 10, 17, 5, tzinfo=timezone.utc)


class CampaignProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name).resolve()
        self.parent.chmod(0o700)
        self.value = plan()
        self.root = self.parent/self.value['plan_id']
        self.root.mkdir(mode=0o700)
        self.source = self.root/'observations.sqlite'
        self.archive = self.parent/'archive.sqlite'
        self.writer = SnapshotStore(self.source)
        self.addCleanup(self.writer.db.close)
        for minute, number in [(0, '100'), (10, '125')]:
            stamp = f'2026-10-17T03:{minute:02}:00Z'
            self.writer.save(normalize_snapshot({'id': 3004, 'groupQueues': {'mixedQueue': [number],
                'reservationQueue': [], 'boothQueue': [], 'counterQueue': []}}, '3004',
                request_started_at=stamp, received_at=stamp, elapsed_ms=0,
                api_profile='miniapp_gateway', data_origin='live'))
        self.network = patch('socket.socket', side_effect=AssertionError('no network')).start()
        self.material = patch.object(project.legacy, 'prepare_material_source', side_effect=AssertionError('no account material')).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(lambda: self.network.assert_not_called())
        self.addCleanup(lambda: self.material.assert_not_called())

    def projection(self, **changes):
        return project.Projection(self.root, self.archive, {**self.value, **changes})

    def rows(self):
        with sqlite3.connect(self.archive) as db:
            return db.execute('SELECT count(*) FROM archive_records').fetchone()[0]

    def test_actual_projection_is_readonly_and_restarts_at_confirmed_cursor(self):
        before = self.source.read_bytes()
        with self.projection() as projection:
            first = projection.step(NOW)
            self.assertEqual((first['inserted_records'], first['inserted_links']), (2, 2))
            self.assertEqual((first['source_queries_added'], first['query_credentials_accessed']), (0, False))
        with self.projection() as projection:
            self.assertEqual(projection.after, first['after_id'])
            self.assertIsNone(projection.step(NOW))
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(self.rows(), 2)

    def test_archive_commit_then_checkpoint_failure_replays_without_duplicates(self):
        original = project.sync._write_packet
        def fail_checkpoint(body, path):
            if Path(path).name.startswith('confirmed-'):
                raise OSError('synthetic checkpoint failure')
            return original(body, path)
        with self.projection() as projection, patch.object(project.sync, '_write_packet', side_effect=fail_checkpoint):
            with self.assertRaises(OSError): projection.step(NOW)
            self.assertEqual(projection.after, 0)
        self.assertEqual(self.rows(), 2)
        with self.projection() as restored:
            replay = restored.step(NOW)
            self.assertEqual((replay['inserted_records'], replay['inserted_links']), (0, 0))
        self.assertEqual(self.rows(), 2)

    def test_new_source_database_keeps_preexisting_archive_records(self):
        old = self.parent/'old.sqlite'
        with SnapshotStore(old) as writer:
            stamp='2026-10-12T03:00:00Z'
            writer.save(normalize_snapshot({'id': 3004, 'groupQueues': {'mixedQueue': ['50'],
                'reservationQueue': [], 'boothQueue': [], 'counterQueue': []}}, '3004',
                request_started_at=stamp, received_at=stamp, elapsed_ms=0,
                api_profile='miniapp_gateway', data_origin='live'))
        old_outbox=self.parent/'old-outbox'
        old_outbox.mkdir(mode=0o700)
        project.sync.publish_page(old, self.archive, old_outbox, 0,
            as_of=NOW.isoformat(), scope={'store_ids':['3004'],'api_profile':'miniapp_gateway','data_origin':'live'})
        self.assertEqual(self.rows(), 1)
        with self.projection() as projection: projection.step(NOW)
        self.assertEqual(self.rows(), 3)

    def test_full_plan_binding_rejects_budget_change_even_with_same_deadline(self):
        with self.projection() as projection: projection.step(NOW)
        with self.assertRaisesRegex(ValueError, 'cloud_public_checkpoint_changed'):
            self.projection(ordinary_query_limit=1403)
        # Failed construction must release its lock so normal restoration works.
        with self.projection() as restored: self.assertEqual(restored.index, 1)

    def test_explicit_sync_deadline_keeps_two_minute_tail_and_no_access_after(self):
        with self.projection() as projection:
            self.assertEqual(projection.deadline, datetime(2026, 10, 21, 14, 2, tzinfo=timezone.utc))
            with patch.object(project.sync, 'publish_page') as publish:
                self.assertIsNone(projection.step(projection.deadline))
            publish.assert_not_called()
            with self.assertRaisesRegex(ValueError, 'cloud_clock_invalid'):
                projection.step(datetime(2026, 10, 21, 14, 2))
        self.assertEqual(project.sync.DEADLINE, datetime(2026, 10, 14, 14, 2, tzinfo=timezone.utc))

    def test_checkpoint_names_and_restoration_work_beyond_999(self):
        with self.projection() as projection: pin=projection.pin
        outbox=self.root/'public-outbox'
        receipt={'after_id':1001,'bundle_sha256':'a'*64,'durability_confirmed':True,
            'inserted_records':0,'inserted_links':0,'source_queries_added':0,
            'query_credentials_accessed':False,'scope_sha256':pin}
        # Deliberately simulated journal, not 1001 real deliveries.
        for index in range(1001):
            project.sync._write_packet(project.sync.encoded(receipt), outbox/f'confirmed-{index:06}.json')
        with self.projection() as restored:
            self.assertEqual((restored.index,restored.after),(1001,1001))
            result={k:v for k,v in receipt.items() if k!='scope_sha256'}
            result['after_id']=1002
            with patch.object(project.sync,'publish_page',return_value=result): restored.step(NOW)
        self.assertTrue((outbox/'confirmed-001001.json').exists())
        with self.projection() as restored: self.assertEqual((restored.index,restored.after),(1002,1002))

    def test_legacy_500_outbox_default_is_unchanged_and_override_is_bounded(self):
        outbox=self.parent/'many-pending'
        outbox.mkdir(mode=0o700)
        for cursor in range(1,502): (outbox/f'pending-{cursor:012}.json').touch(mode=0o600)
        scope={'store_ids':['3004'],'api_profile':'miniapp_gateway','data_origin':'live'}
        with self.assertRaisesRegex(ValueError,'cloud_public_outbox_invalid'):
            project.sync.publish_page(self.source,self.archive,outbox,501,as_of=NOW.isoformat(),scope=scope)
        self.assertIsNone(project.sync.publish_page(self.source,self.archive,outbox,501,
                as_of=NOW.isoformat(),scope=scope,pending_limit=5000))
        for limit in (True,0,5001):
            with self.assertRaises(ValueError): project.sync.publish_page(self.source,self.archive,outbox,501,
                as_of=NOW.isoformat(),scope=scope,pending_limit=limit)

    def test_source_archive_alias_and_wrong_root_cannot_replace_data(self):
        with self.assertRaises(ValueError): project.Projection(self.root,self.source,self.value)
        other=self.parent/'old-root'
        other.mkdir(mode=0o700)
        with self.assertRaises(ValueError): project.Projection(other,self.archive,self.value)

    def test_saved_manifest_parse_does_not_read_private_query_files(self):
        project.sync._write_packet(project.sync.encoded(self.value),self.root/'campaign-plan.json')
        original=project.legacy._read_private_file
        def only_manifest(path):
            self.assertEqual(Path(path).name,'campaign-plan.json')
            return original(path)
        with patch.object(project.legacy,'_read_private_file',side_effect=only_manifest):
            self.assertEqual(project.read_plan(self.root),self.value)

    def test_no_automatic_delivery_after_local_batch_limit(self):
        with self.projection() as projection:
            projection.index=projection.limit
            with patch.object(project.sync,'publish_page') as publish, \
                    self.assertRaisesRegex(ValueError,'cloud_public_checkpoint_limit'):
                projection.step(NOW)
            publish.assert_not_called()


if __name__ == '__main__':
    unittest.main()
