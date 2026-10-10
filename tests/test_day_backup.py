"""Closed-day raw recovery, immutable sources and invalid archives; no network."""
from datetime import timedelta
import hashlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import stat
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

from sushiwait.cli import main
from sushiwait.daybackup import BackupError, create_backup, restore_backup
from sushiwait.dailyarchive import read_day, read_legacy_day
from sushiwait.dailycontroller import DailyController
from sushiwait.remote import QUEUE_NAMES, RemoteStore
import test_daily_controller as fixture

DAY = '2026-10-09'
STORE = '900001'


class DayBackupTests(unittest.TestCase):
    setUp = fixture.DailyControllerTests.setUp
    tearDown = fixture.DailyControllerTests.tearDown
    config = fixture.DailyControllerTests.config
    tick = fixture.DailyControllerTests.tick

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':')))
        path.chmod(0o600)

    def prepare(self):
        self.parent = self.root.parent
        self.fleet = self.parent/'fleet'; self.fleet.mkdir(mode=0o700)
        self.root = self.fleet/('store-'+STORE)
        def read(response, size):
            return json.dumps({k:['0200','0201','0202','0203','0204'] for k in QUEUE_NAMES}
                if 'groupqueues?' in response.request.full_url else 5).encode()[:size]
        with patch('test_remote_window.Response.read', read):
            with DailyController(config=self.config(), now=self.clock.wall()) as controller:
                self.tick(controller)
        self.bundle = self.parent/'backup.zip'
        self.campaign = self.root/DAY/'campaign'
        self.window = self.campaign/'window-01'
        self.now = fixture.BASE+timedelta(seconds=121)

    def create(self, **changes):
        args = dict(root=self.fleet, store_ids=[STORE], day=DAY, output=self.bundle, now=self.now)
        args.update(changes)
        return create_backup(**args)

    def original_bytes(self):
        return {p:p.read_bytes() for p in self.fleet.rglob('*') if p.is_file()}

    def rewrite(self, mutate=None, extra=None):
        with ZipFile(self.bundle) as archive:
            data = {n:archive.read(n) for n in archive.namelist()}
        if mutate: mutate(data)
        self.bundle.unlink()
        with ZipFile(self.bundle, 'w', compression=ZIP_DEFLATED) as archive:
            for name, raw in data.items(): archive.writestr(name, raw)
            if extra: extra(archive)
        self.bundle.chmod(0o600)

    def test_full_raw_restore_preserves_more_than_three_labels_times_and_original_files(self):
        self.prepare(); before = self.original_bytes(); calls = len(self.opener.calls)
        destination = self.parent/'restored'
        with patch('socket.socket', side_effect=AssertionError('backup opened network')):
            created = self.create()
            checked = restore_backup(bundle=self.bundle)
            restored = restore_backup(bundle=self.bundle, destination=destination)
        self.assertEqual(created['raw_records'], 2)
        self.assertTrue(checked['restored_readable']); self.assertTrue(restored['restored_readable'])
        self.assertFalse(restored['independent_machine_verified'])
        self.assertFalse(restored['collector_started_or_resumed'])
        self.assertEqual(stat.S_IMODE(self.bundle.stat().st_mode),0o600)
        original = read_day(self.root, STORE, DAY)
        recovered = read_day(destination/('store-'+STORE), STORE, DAY)
        self.assertEqual(original,recovered)
        db_path = destination/('store-'+STORE)/DAY/'campaign/window-01/remote.sqlite3'
        with RemoteStore(db_path, read_only=True) as database:
            records = list(database.db.execute('SELECT payload_json FROM remote_samples ORDER BY id'))
        for (raw,) in records:
            self.assertEqual(json.loads(raw)['queries']['groupqueues']['payload']['queues']['mixedQueue'],
                ['0200','0201','0202','0203','0204'])
        self.assertFalse(list(destination.rglob('*.lock')))
        self.assertFalse((destination/('store-'+STORE)/'controller.json').exists())
        self.assertEqual(self.original_bytes(),before); self.assertEqual(len(self.opener.calls),calls)
        self.assertFalse(list(self.parent.glob('.backup-*')))

    def test_legacy_stopped_campaign_and_daily_export_are_restorable(self):
        self.prepare(); legacy = self.parent/'legacy'; legacy.mkdir(mode=0o700)
        store = legacy/('store-'+STORE); store.mkdir(mode=0o700)
        shutil.copytree(self.campaign,store/'campaign')
        campaign = store/'campaign/campaign.json'; value=json.loads(campaign.read_text())
        value.update(state='active',end_reason=None); self.write(campaign,value)
        export=legacy/'daily-exports'/DAY; export.mkdir(mode=0o700,parents=True)
        export.parent.chmod(0o700)
        raw=(self.root/DAY/'projection.json').read_bytes(); projection=json.loads(raw)
        (export/f'store-{STORE}.json').write_bytes(raw); (export/f'store-{STORE}.json').chmod(0o600)
        self.write(export/'manifest.json',{'local_date':DAY,'exported_at':DAY+'T14:05:00Z',
            'stores':[{'store_id':STORE,'name':'合成旧门店','state':'saved','points':2,
                'complete_observed_projection':True,'sha256':hashlib.sha256(raw).hexdigest(),
                'daily_quality':projection['summary']}], 'official_requests_added_by_export':0,
            'active_database_opened':False,'independent_backup':False,'full_day_source_quality_verified':False})
        self.create(root=legacy, source_kind='legacy', now=fixture.BASE+timedelta(hours=12))
        destination=self.parent/'legacy-restored'; restore_backup(bundle=self.bundle,destination=destination)
        restored=read_legacy_day(destination/'legacy-exports',STORE,DAY)
        self.assertEqual(restored['points'],projection['points'])

    def test_campaign_and_window_writer_locks_each_prevent_backup(self):
        import fcntl
        self.prepare()
        for base in (self.campaign/'campaign.json',self.window/'task.json'):
            with open(str(base)+'.lock','rb') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with self.assertRaisesRegex(BackupError,'backup_source_writer_active'): self.create()
            self.assertFalse(self.bundle.exists())

    def test_open_day_or_incomplete_campaign_is_not_backed_up(self):
        self.prepare()
        with self.assertRaisesRegex(BackupError,'backup_day_not_closed'):
            self.create(now=fixture.BASE+timedelta(seconds=60))
        campaign=self.campaign/'campaign.json'; value=json.loads(campaign.read_text())
        value.update(state='active',end_reason=None); self.write(campaign,value)
        with self.assertRaisesRegex(BackupError,'backup_day_not_closed'):self.create()
        self.assertFalse(self.bundle.exists())

    def test_raw_content_change_is_detected_by_checkpoint_chain(self):
        self.prepare(); db=sqlite3.connect(self.window/'remote.sqlite3')
        raw=db.execute('SELECT payload_json FROM remote_samples WHERE id=1').fetchone()[0]
        value=json.loads(raw); value['queries']['storequeuecount']['payload']['raw_count']=99
        value.pop('local_intake',None)
        db.execute('UPDATE remote_samples SET payload_json=? WHERE id=1',(json.dumps(value),));db.commit();db.close()
        with self.assertRaises(BackupError):self.create()
        self.assertFalse(self.bundle.exists())

    def test_projection_changed_without_result_digest_is_rejected(self):
        self.prepare(); path=self.root/DAY/'projection.json'; value=json.loads(path.read_text())
        value['points'][0]['reference_called_label']='9999';self.write(path,value)
        with self.assertRaises(BackupError):self.create()
        self.assertFalse(self.bundle.exists())

    def test_unsafe_source_symlink_and_public_modes_are_rejected(self):
        self.prepare(); path=self.window/'remote.sqlite3'
        path.chmod(0o644)
        with self.assertRaises(BackupError):self.create()
        path.chmod(0o600); raw=path.read_bytes(); path.unlink(); elsewhere=self.parent/'elsewhere.sqlite3'
        elsewhere.write_bytes(raw);elsewhere.chmod(0o600);path.symlink_to(elsewhere)
        with self.assertRaises(BackupError):self.create()

    def test_existing_backup_and_restore_destinations_are_never_overwritten(self):
        self.prepare(); self.create(); raw=self.bundle.read_bytes()
        with self.assertRaisesRegex(BackupError,'backup_destination_exists'):self.create()
        self.assertEqual(self.bundle.read_bytes(),raw)
        destination=self.parent/'existing';destination.mkdir(mode=0o700)
        marker=destination/'keep';marker.write_text('unchanged')
        with self.assertRaisesRegex(BackupError,'backup_destination_exists'):
            restore_backup(bundle=self.bundle,destination=destination)
        self.assertEqual(marker.read_text(),'unchanged')

    def test_backup_inside_source_is_rejected(self):
        self.prepare()
        with self.assertRaisesRegex(BackupError,'backup_destination_overlaps_source'):
            self.create(output=self.fleet/'backup.zip')

    def test_wrong_store_duplicate_scope_and_invalid_date_are_rejected(self):
        self.prepare()
        for change in ({'store_ids':[STORE,STORE]},{'store_ids':['900002']},{'day':'../2026-10-09'}):
            with self.assertRaises(ValueError):self.create(**change)
            self.assertFalse(self.bundle.exists())

    def test_zip_changed_bytes_fail_hash_before_retained_restore(self):
        self.prepare();self.create()
        def change(data):
            key=next(k for k in data if k.endswith('remote.sqlite3'));data[key]+=b'x'
        self.rewrite(change);destination=self.parent/'not-created'
        with self.assertRaises(BackupError):restore_backup(bundle=self.bundle,destination=destination)
        self.assertFalse(destination.exists());self.assertFalse(list(self.parent.glob('.backup-restore-*')))

    def test_duplicate_extra_traversal_and_symlink_members_are_rejected(self):
        self.prepare(); self.create(); original=self.bundle.read_bytes()
        def symlink(archive):
            item=ZipInfo('evil');item.external_attr=(stat.S_IFLNK|0o600)<<16
            archive.writestr(item,'target')
        extras=[lambda a:a.writestr('manifest.json','{}'),lambda a:a.writestr('extra','x'),
            lambda a:a.writestr('../escape','x'),symlink]
        for extra in extras:
            self.bundle.write_bytes(original)
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter('ignore',UserWarning); self.rewrite(extra=extra)
            with self.assertRaises(BackupError):restore_backup(bundle=self.bundle)
        self.assertFalse((self.parent/'escape').exists())

    def test_forged_manifest_flags_scope_counts_and_missing_files_are_rejected(self):
        self.prepare();self.create();original=self.bundle.read_bytes()
        changes=[lambda m:m.update(full_raw_databases_included=False),
            lambda m:m.update(collector_resume_allowed=True),lambda m:m.update(independent_machine_verified=True),
            lambda m:m.update(store_ids=['900002']),lambda m:m['rows'][0].update(raw_records=1),
            lambda m:m['files'].pop(),lambda m:m.update(official_requests_added=True)]
        for change in changes:
            self.bundle.write_bytes(original)
            def mutate(data):
                manifest=json.loads(data['manifest.json']);change(manifest)
                data['manifest.json']=json.dumps(manifest).encode()
            self.rewrite(mutate)
            with self.assertRaises(BackupError):restore_backup(bundle=self.bundle)

    def test_capacity_shortage_leaves_no_retained_restore(self):
        self.prepare();self.create();destination=self.parent/'no-space'
        with patch('sushiwait.daybackup.shutil.disk_usage') as usage:
            usage.return_value.free=0
            with self.assertRaisesRegex(BackupError,'backup_insufficient_space'):
                restore_backup(bundle=self.bundle,destination=destination)
        self.assertFalse(destination.exists())

    def test_recovery_check_and_cli_never_construct_source_client(self):
        self.prepare()
        commands=[['day-backup-create','--root',str(self.fleet),'--store-id',STORE,'--date',DAY,'--output',str(self.bundle)],
            ['day-backup-check','--input',str(self.bundle)],
            ['day-backup-restore','--input',str(self.bundle),'--destination',str(self.parent/'cli-restored')]]
        with patch('sushiwait.remote.RemoteClient',side_effect=AssertionError('client created')):
            for args in commands:
                out=io.StringIO()
                with patch('sys.stdout',out):self.assertEqual(main(args),0)
                value=json.loads(out.getvalue());self.assertFalse(value['network_performed'])
                self.assertFalse(value['collector_started_or_resumed'])
        out=io.StringIO()
        with patch('sys.stdout',out):self.assertEqual(main(['day-backup-check','--input',str(self.parent/'missing')]),2)
        self.assertNotIn(str(self.parent),out.getvalue())


if __name__ == '__main__': unittest.main()
