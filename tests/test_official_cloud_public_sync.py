"""Real synthetic SQLite selection/commit/replay; no credential or network."""
import hashlib
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sushiwait.observations import normalize_snapshot
from sushiwait.storage import SnapshotStore

SPEC=importlib.util.spec_from_file_location('cloud_public_sync',
    Path(__file__).resolve().parents[1]/'deploy'/'official_cloud_public_sync.py')
sync=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(sync)
AT='2026-10-12T05:00:00Z'


class OfficialCloudPublicSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.root.chmod(0o700)
        self.source=self.root/'source.sqlite';self.archive=self.root/'archive.sqlite'
        self.outbox=self.root/'outbox';self.outbox.mkdir(mode=0o700)
        self.scope={'store_ids':['900001'],'api_profile':'miniapp_gateway','data_origin':'synthetic'}
        self.writer=SnapshotStore(self.source);self.addCleanup(self.writer.db.close)
        for minute,number in [(0,'100'),(15,'125')]:
            stamp=f'2026-10-12T03:{minute:02}:00Z'
            self.writer.save(normalize_snapshot({'id':900001,'groupQueues':{'mixedQueue':[number],
                'reservationQueue':[],'boothQueue':[],'counterQueue':[]}},'900001',
                request_started_at=stamp,received_at=stamp,elapsed_ms=0,
                api_profile='miniapp_gateway',data_origin='synthetic'))
        self.socket=patch('socket.socket',side_effect=AssertionError('no network')).start()
        self.addCleanup(patch.stopall)

    def publish(self,after=0,**changes):
        return sync.publish_page(self.source,self.archive,self.outbox,after,
            **{'as_of':AT,'scope':self.scope,**changes})

    def test_public_three_table_copy_keeps_source_and_does_not_read_credentials(self):
        before=hashlib.sha256(self.source.read_bytes()).hexdigest()
        result=self.publish()
        self.assertTrue(result['durability_confirmed'])
        self.assertEqual((result['inserted_records'],result['inserted_links']),(2,2))
        self.assertEqual(before,hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertFalse(result['query_credentials_accessed']);self.socket.assert_not_called()

    def test_crash_after_commit_before_cursor_write_replays_same_bundle_once(self):
        first=self.publish();again=self.publish()
        self.assertEqual(first['bundle_sha256'],again['bundle_sha256'])
        self.assertEqual((again['inserted_records'],again['inserted_links']),(0,0))
        with sqlite3.connect(self.archive) as db:
            self.assertEqual([db.execute('SELECT count(*) FROM '+t).fetchone()[0]
                for t in ('archive_records','continuity_claims','continuity_bundles')],[2,2,1])
        self.assertIsNone(self.publish(after=first['after_id']))

    def test_archive_failure_keeps_the_same_pending_file_for_explicit_recovery(self):
        with patch.object(sync,'ContinuityArchive',side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):self.publish()
        self.assertEqual(len(list(self.outbox.glob('pending-*.json'))),1)
        self.assertFalse(self.archive.exists())
        self.assertEqual(self.publish()['inserted_records'],2)

    def test_unconfirmed_commit_does_not_produce_cursor_result(self):
        original=sync.ContinuityArchive.append_bundle
        def uncertain(archive,*args,**kwargs):
            result=original(archive,*args,**kwargs);result['durability_confirmed']=False;return result
        with patch.object(sync.ContinuityArchive,'append_bundle',uncertain):
            with self.assertRaisesRegex(ValueError,'cloud_public_commit_unconfirmed'):self.publish()
        self.assertEqual(self.publish()['inserted_records'],0)

    def test_scope_change_or_source_destination_alias_is_rejected(self):
        self.publish()
        with self.assertRaises(ValueError):self.publish(scope={**self.scope,'store_ids':['900002']})
        with self.assertRaises(ValueError):sync.publish_page(self.source,self.source,self.outbox,0,
            as_of=AT,scope=self.scope)

    def test_pending_replay_uses_canonical_store_order(self):
        scope={**self.scope,'store_ids':['900002','900001']}
        first=self.publish(scope=scope);again=self.publish(scope=scope)
        self.assertEqual(first['bundle_sha256'],again['bundle_sha256'])
        self.assertEqual(again['inserted_records'],0)
