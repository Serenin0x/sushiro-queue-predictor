from copy import deepcopy
from datetime import datetime, timedelta
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sushiwait.cli import main
from sushiwait.continuity import select_continuity_packet, validate_continuity_bundle
from sushiwait.continuityarchive import ContinuityArchive
from sushiwait.continuitybackup import ContinuityBackupError, snapshot_archive, verify_snapshot, restore_snapshot, _snapshot_file
from sushiwait.continuityview import ContinuityStatisticsView
from sushiwait.monitoring import _stamp
from sushiwait.observations import normalize_snapshot
from sushiwait.packets import encoded
from sushiwait.storage import SnapshotStore

NOW=datetime.fromisoformat('2026-10-11T12:00:00+00:00')
HOURS=json.loads((Path(__file__).resolve().parents[1]/'config/default-business-hours.json').read_bytes())


class ContinuityBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.root.chmod(0o700)
        self.source=self.root/'source.sqlite3';self.database=self.root/'archive.sqlite3'
        with SnapshotStore(self.source) as writer:
            for i,labels in enumerate((['12','13','14'],['13','14','16'])):
                at=f'2026-10-11T03:0{i}:00Z'
                writer.save(normalize_snapshot({'id':900001,'groupQueues':{'mixedQueue':labels,
                    'reservationQueue':[],'counterQueue':[],'boothQueue':[]}},'900001',
                    request_started_at=at,received_at=at,elapsed_ms=0,
                    api_profile='miniapp_gateway',data_origin='synthetic'))
        self.scope=dict(as_of=NOW.isoformat(),store_ids=['900001'],api_profile='miniapp_gateway',data_origin='synthetic')
        self.bundle=select_continuity_packet(self.source,after_id=1,**self.scope)[0]
        with ContinuityArchive(self.database) as archive:archive.append_bundle(self.bundle,as_of=NOW.isoformat())
        self.config={'schema_version':1,'mode':'official_packet_archive_with_continuity',
            'database_file':str(self.database),'store_names':{'900001':'Synthetic'},
            'data_origin':'synthetic','hours':HOURS,'base_interval':900}
        self.view=self.root/'view.json';self.view.write_bytes(encoded(self.config));self.view.chmod(0o600)
        self.backup=self.root/'backup';self.restored=self.root/'restored'

    def snapshot(self,**kwargs):
        return snapshot_archive(self.view,self.backup,clock=lambda:NOW,**kwargs)

    def reseal_hash_for_damage_test(self):
        file=self.backup/'manifest.json';manifest=json.loads(file.read_bytes())
        raw=(self.backup/'archive.sqlite3').read_bytes()
        manifest.update(archive_sha256=hashlib.sha256(raw).hexdigest(),archive_bytes=len(raw))
        file.write_bytes(encoded(manifest))

    def test_full_snapshot_and_independent_restore_keep_records_partial_claims_and_bytes(self):
        before=self.database.read_bytes()
        with patch('socket.socket',side_effect=AssertionError('no network')):
            saved=self.snapshot();checked=verify_snapshot(self.backup,as_of=NOW.isoformat())
            restored=restore_snapshot(self.backup,self.restored,as_of=NOW.isoformat())
        self.assertEqual((saved['records'],saved['linked_observations'],saved['partial_writer_claims']),(2,1,1))
        self.assertTrue(saved['durability_confirmed'] and checked['snapshot_validated'] and restored['restored'])
        self.assertFalse(restored['writer_started'] or saved['complete_business_day_claim'] or checked['offsite_location_verified'])
        self.assertEqual(before,self.database.read_bytes())
        self.assertEqual((self.backup/'archive.sqlite3').read_bytes(),(self.restored/'archive.sqlite3').read_bytes())
        for root in (self.backup,self.restored):
            self.assertEqual(root.stat().st_mode&0o777,0o700)
            self.assertTrue(all((root/name).stat().st_mode&0o777==0o600 for name in ('archive.sqlite3','manifest.json')))
        view=ContinuityStatisticsView(self.restored/'archive.sqlite3',store_names={'900001':'Synthetic'},
            data_origin='synthetic',hours=HOURS,base_interval=900)
        value=view.read('/api/v1/official/stores/900001/days/2026-10-11',now=NOW)
        self.assertEqual([p['comparison_state'] for p in value['points']],['packet_continuity_unknown','comparable_display_sets'])
        self.assertEqual(value['points'][1]['removed_labels']['mixedQueue'],1)

    def test_open_archive_writer_is_not_stopped_or_required_to_release_its_lock(self):
        with ContinuityArchive(self.database) as writer:
            self.snapshot()
            self.assertEqual(writer.append_bundle(self.bundle,as_of=NOW.isoformat())['duplicate_links'],1)
        self.assertTrue(verify_snapshot(self.backup,as_of=NOW.isoformat())['snapshot_validated'])

    def test_later_full_link_restores_exact_first_claim_and_first_link_times(self):
        first=select_continuity_packet(self.source,limit=1,**self.scope)[0]
        with ContinuityArchive(self.database) as archive:archive.append_bundle(first,as_of=(NOW+timedelta(minutes=1)).isoformat())
        snapshot_archive(self.view,self.backup,clock=lambda:NOW+timedelta(minutes=2))
        restore_snapshot(self.backup,self.restored,as_of=(NOW+timedelta(minutes=2)).isoformat())
        identity=first['links'][0]['observation_id']
        with sqlite3.connect(self.restored/'archive.sqlite3') as db:
            self.assertEqual(db.execute('SELECT claimed_at,linked_at FROM continuity_claims WHERE observation_id=?',[identity]).fetchone(),
                             (_stamp(NOW),_stamp(NOW+timedelta(minutes=1))))

    def test_existing_destination_and_old_mode_never_overwrite_source_or_backup(self):
        self.snapshot();before=(self.backup/'archive.sqlite3').read_bytes()
        with self.assertRaises(ContinuityBackupError):self.snapshot()
        with self.assertRaises(ContinuityBackupError):restore_snapshot(self.backup,self.backup,as_of=NOW.isoformat())
        self.assertEqual(before,(self.backup/'archive.sqlite3').read_bytes())
        self.view.write_bytes(encoded({**self.config,'mode':'official_packet_archive_readonly'}))
        with self.assertRaisesRegex(ContinuityBackupError,'continuity_backup_wrong_mode'):
            snapshot_archive(self.view,self.root/'another',clock=lambda:NOW)
        self.assertFalse((self.root/'another').exists())

    def test_corrupt_bytes_and_manifest_hash_do_not_restore(self):
        self.snapshot();file=self.backup/'manifest.json';manifest=json.loads(file.read_bytes())
        file.write_bytes(encoded({**manifest,'archive_sha256':'0'*64}))
        with self.assertRaisesRegex(ContinuityBackupError,'continuity_backup_hash_mismatch'):
            restore_snapshot(self.backup,self.restored,as_of=NOW.isoformat())
        self.assertFalse(self.restored.exists())

    def test_rehashed_copy_still_checks_bundle_evidence_and_column_integrity(self):
        for sql in ('DELETE FROM continuity_bundles',
                    "UPDATE archive_records SET store_id='900002'",
                    "UPDATE continuity_claims SET linked_at='2026-10-11T12:00:01.000000Z' WHERE link_json IS NOT NULL",
                    "UPDATE continuity_bundles SET archived_at='2026-10-11T11:59:59.000000Z'"):
            with self.subTest(sql=sql):
                self.snapshot()
                with sqlite3.connect(self.backup/'archive.sqlite3') as db:db.execute(sql);db.commit()
                self.reseal_hash_for_damage_test()
                with self.assertRaises(ContinuityBackupError):verify_snapshot(self.backup,as_of=(NOW+timedelta(minutes=1)).isoformat())
                for child in self.backup.iterdir():child.unlink()
                self.backup.rmdir()

    def test_global_cycle_between_individually_valid_pages_fails_full_audit(self):
        first=select_continuity_packet(self.source,limit=1,**self.scope)[0]
        second=self.bundle['packet']['records'][0];second_alias=self.bundle['links'][0]['writer_id']
        first['anchors']=[{'record':second,'writer_id':second_alias}]
        link=first['links'][0];link.update(predecessor_id=second['observation_id'],
            predecessor_sha256=second['record_sha256'],predecessor_writer_id=second_alias)
        link['link_sha256']=hashlib.sha256(encoded({k:v for k,v in link.items() if k!='link_sha256'})).hexdigest()
        validate_continuity_bundle(first,as_of=NOW.isoformat())
        # Inject mutually consistent damaged tables externally: the normal
        # writer now rejects this cross-page cycle before committing it.
        at=_stamp(NOW+timedelta(minutes=1));digest=hashlib.sha256(encoded(first)).hexdigest()
        with sqlite3.connect(self.database) as db:
            db.execute('UPDATE continuity_claims SET link_json=?,link_sha256=?,linked_at=? WHERE observation_id=?',
                [encoded(link).decode(),link['link_sha256'],at,link['observation_id']])
            db.execute('INSERT INTO continuity_bundles VALUES(?,?,?)',[digest,encoded(first).decode(),at]);db.commit()
        with self.assertRaisesRegex(ContinuityBackupError,'continuity_backup_graph_cycle'):
            snapshot_archive(self.view,self.backup,clock=lambda:NOW+timedelta(minutes=2))
        self.assertFalse((self.backup/'manifest.json').exists())

    def test_claim_time_without_original_bundle_evidence_is_rejected(self):
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE continuity_claims SET claimed_at='2026-10-11T11:59:59.000000Z'");db.commit()
        with self.assertRaises(ContinuityBackupError):self.snapshot()
        self.assertFalse((self.backup/'manifest.json').exists())

    def test_private_paths_symlinks_and_shared_modes_are_rejected(self):
        self.snapshot();target=self.backup/'archive.sqlite3';original=self.root/'original.sqlite3'
        target.rename(original);target.symlink_to(original)
        with self.assertRaises(ContinuityBackupError):verify_snapshot(self.backup,as_of=NOW.isoformat())
        target.unlink();original.rename(target);target.chmod(0o644)
        with self.assertRaises(ContinuityBackupError):verify_snapshot(self.backup,as_of=NOW.isoformat())

    def test_manifest_extra_fields_boolean_counts_and_future_cutoff_fail(self):
        self.snapshot();file=self.backup/'manifest.json';original=json.loads(file.read_bytes())
        for change in ({'unexpected':'PRIVATE_SENTINEL'},
                       {'audit':{**original['audit'],'linked_observations':True}},
                       {'audit':{**original['audit'],'all_relation_hashes_checked':1}}):
            file.write_bytes(encoded({**original,**change}))
            with self.assertRaises(ContinuityBackupError) as error:verify_snapshot(self.backup,as_of=NOW.isoformat())
            self.assertNotIn('PRIVATE_SENTINEL',str(error.exception))
        file.write_bytes(encoded(original))
        with self.assertRaises(ContinuityBackupError):verify_snapshot(self.backup,as_of=(NOW-timedelta(seconds=1)).isoformat())

    def test_bounded_counts_and_deadlines_never_claim_complete_output(self):
        with patch('sushiwait.continuitybackup.MAX_RECORDS',1):
            with self.assertRaisesRegex(ContinuityBackupError,'continuity_backup_capacity_exceeded'):self.snapshot()
        self.assertFalse((self.backup/'manifest.json').exists())
        for seconds in (0,31,True,float('nan')):
            with self.assertRaises(ContinuityBackupError):snapshot_archive(self.view,self.root/'invalid',seconds=seconds)
        self.assertFalse((self.root/'invalid').exists())

    def test_postcopy_manifest_durability_failure_preserves_partial_output_without_success(self):
        with patch('sushiwait.continuitybackup._write_packet',return_value={'durability_confirmed':False}):
            with self.assertRaises(ContinuityBackupError) as error:self.snapshot()
        self.assertTrue(error.exception.output_may_exist)
        self.assertTrue((self.backup/'archive.sqlite3').exists())
        self.assertFalse((self.backup/'manifest.json').exists())

    def test_cli_creates_checks_and_restores_with_no_source_calls(self):
        with patch('sushiwait.cli._utc_clock',return_value=NOW),patch('socket.socket',side_effect=AssertionError('no network')):
            arguments=[['official-archive-backup','--view-file',str(self.view),'--output',str(self.backup)],
                ['official-archive-backup-check','--input',str(self.backup)],
                ['official-archive-restore','--input',str(self.backup),'--output',str(self.restored)]]
            for args in arguments:
                output=io.StringIO()
                with contextlib.redirect_stdout(output):self.assertEqual(main(args),0)
                result=json.loads(output.getvalue());self.assertTrue(result['ok'])
                self.assertFalse(result['network_performed'] or result['query_credentials_accessed'])

    def test_ctime_only_metadata_change_keeps_same_bytes_and_identity_valid(self):
        original_stat,original_fstat=os.stat,os.fstat;changed=False
        def adjusted(result):
            names=('st_dev','st_ino','st_mode','st_uid','st_nlink','st_size','st_mtime_ns','st_ctime_ns')
            return SimpleNamespace(**{n:getattr(result,n)+(1 if changed and n=='st_ctime_ns' else 0) for n in names})
        with patch('sushiwait.continuitybackup.os.stat',side_effect=lambda *a,**k:adjusted(original_stat(*a,**k))), \
             patch('sushiwait.continuitybackup.os.fstat',side_effect=lambda *a,**k:adjusted(original_fstat(*a,**k))):
            with _snapshot_file(self.database,deadline=time.monotonic()+5) as (fd,size):
                changed=True;self.assertEqual(len(os.pread(fd,size,0)),size)

    def test_changed_content_with_restored_mtime_still_fails_complete_hash_guard(self):
        original=self.database.stat()
        with self.assertRaisesRegex(ContinuityBackupError,'continuity_backup_file_changed'):
            with _snapshot_file(self.database,deadline=time.monotonic()+5):
                with self.database.open('r+b') as file:file.seek(0);file.write(b'X');file.flush()
                os.utime(self.database,ns=(original.st_atime_ns,original.st_mtime_ns))
