from datetime import datetime,timedelta,timezone
import json,sqlite3
from pathlib import Path
import unittest
from unittest.mock import patch
from test_continuity_archive import ContinuityArchiveTests,AT,HOURS
from sushiwait.readmodel import RollupWriter,ReadModelView,connection,source_key,pack,unpack
from sushiwait.analytics import build_report

NOW=datetime.fromisoformat(AT.replace('Z','+00:00'))
DAY='/api/v1/official/stores/900001/days/2026-10-11'
MONTH='/api/v1/official/months/2026-10'

class ReadModelTests(unittest.TestCase):
    def setUp(self):
        self.fixture=ContinuityArchiveTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.fixture.append();self.view=self.fixture.view();self.path=self.fixture.folder/'rollups.sqlite'
        self.writer=RollupWriter(self.path,[self.view]);self.key=source_key(self.view)
    def ready(self):
        value=self.writer.step(now=NOW)
        self.assertTrue(value['caught_up'],value)
        return ReadModelView(self.path,[self.view])
    def test_first_bootstrap_matches_strict_original_and_web_does_not_open_raw_archive(self):
        model=self.ready();before=self.fixture.database.read_bytes()
        with patch('sushiwait.readmodel.source_connection',side_effect=AssertionError('web may not read history')):
            status,body,_=model.dispatch(DAY)
            self.assertEqual(status,200)
            got=json.loads(body);expected=self.view.read(DAY,now=NOW)
            self.assertEqual(got['points'],expected['points'])
            self.assertEqual(got['summary']['observations'],2)
            self.assertEqual(json.loads(model.dispatch(MONTH)[1])['days']['2026-10-11']['900001']['observations'],2)
        self.assertEqual(before,self.fixture.database.read_bytes())
    def test_duplicate_replay_and_restart_do_not_double_counts(self):
        self.ready();self.fixture.append();again=RollupWriter(self.path,[self.view]);r=again.step(now=NOW)
        self.assertEqual(r['accepted'],0);self.assertEqual(r['pending_days'],0)
        with connection(self.path) as db:self.assertEqual(db.execute('SELECT count(*) FROM pointers').fetchone()[0],2)
    def test_incremental_batches_keep_durable_progress_and_bounded_work(self):
        r=self.writer.ingest(self.key,now=NOW,records=1,bundles=1)
        self.assertFalse(r['caught_up'])
        with connection(self.path) as db:self.assertEqual(db.execute('SELECT record_cursor FROM sources').fetchone()[0],1)
        self.ready()
        with connection(self.path) as db:
            plan=db.execute('EXPLAIN QUERY PLAN SELECT source_rowid FROM pointers WHERE source_key=? AND store_id=? AND day=?',(self.key,'900001','2026-10-11')).fetchall()
            self.assertIn('pointers_day',str(plan))
    def test_corrupt_snapshot_fails_without_silent_raw_fallback(self):
        model=self.ready()
        with connection(self.path,write=True) as db,db:db.execute('UPDATE snapshots SET detail_sha256=?',('0'*64,))
        self.assertEqual(model.dispatch(DAY)[0],503)
    def test_source_replacement_is_quarantined_preserving_other_data(self):
        self.ready();old=self.fixture.database;temporary=old.with_suffix('.replacement');temporary.write_bytes(old.read_bytes());temporary.chmod(0o600);temporary.replace(old)
        r=self.writer.step(now=NOW);self.assertEqual(r['error_sources'],1)
        model=ReadModelView(self.path,[self.view]);value=json.loads(model.dispatch(MONTH)[1]);self.assertEqual(value['unavailable_store_ids'],['900001'])
        self.assertEqual(model.dispatch(DAY)[0],503)
    def test_late_continuity_link_invalidates_and_rebuilds_only_affected_day(self):
        # Bootstrap a plain accepted packet, then deliver its continuity later.
        other=self.fixture.folder/'plain.sqlite';from sushiwait.continuityarchive import ContinuityArchive
        bundle=self.fixture.bundle()
        with ContinuityArchive(other) as archive:archive.append(bundle['packet'],as_of=AT)
        from sushiwait.continuityview import ContinuityStatisticsView
        view=ContinuityStatisticsView(other,store_names=self.view.names,hours=HOURS,data_origin='synthetic',base_interval=900)
        path=self.fixture.folder/'late.sqlite';writer=RollupWriter(path,[view]);writer.step(now=NOW)
        model=ReadModelView(path,[view]);self.assertEqual(json.loads(model.dispatch(DAY)[1])['points'][1]['comparison_state'],'packet_continuity_unknown')
        with ContinuityArchive(other) as archive:archive.append_bundle(bundle,as_of=AT)
        result=writer.step(now=NOW);self.assertTrue(result['caught_up'])
        self.assertEqual(json.loads(model.dispatch(DAY)[1])['points'][1]['comparison_state'],'comparable_display_sets')
    def test_materialized_report_matches_raw_statistics_without_reading_points(self):
        model=self.ready();sel={'from':'2026-10-11','to':'2026-10-11','source':'sapi_miniapp_gateway','queue':'ordinary','store_id':''}
        raw=build_report(lambda p:self.view.read(p,now=NOW),self.view.names,sel,now=NOW)
        with model.report_reader() as reader:
            with patch.object(model,'_day_value',side_effect=AssertionError('report may not read raw day points')):
                got=build_report(lambda p:json.loads(reader.dispatch(p)[1]),reader.names,sel,now=NOW,summarized_day=reader.summarized_day)
        self.assertEqual(raw,got)
    def test_new_observation_and_interrupted_projection_resume_from_dirty_partition(self):
        model=self.ready()
        from sushiwait.storage import SnapshotStore
        from sushiwait.observations import normalize_snapshot
        with SnapshotStore(self.fixture.source) as writer:
            stamp='2026-10-11T03:02:00Z'
            writer.save(normalize_snapshot({'id':900001,'groupQueues':{'mixedQueue':['15','16','17'],'reservationQueue':[],'counterQueue':[],'boothQueue':[]}},'900001',request_started_at=stamp,received_at=stamp,elapsed_ms=0,api_profile='miniapp_gateway',data_origin='synthetic'))
        self.fixture.append(self.fixture.bundle(after_id=2))
        self.writer.ingest(self.key,now=NOW)
        with patch('sushiwait.readmodel.pack',side_effect=RuntimeError('synthetic interruption')):
            with self.assertRaises(RuntimeError):self.writer.refresh(self.key,'900001','2026-10-11',now=NOW)
        with connection(self.path) as db:self.assertEqual(db.execute('SELECT count(*) FROM dirty').fetchone()[0],1)
        result=self.writer.step(now=NOW);self.assertTrue(result['caught_up'])
        self.assertEqual(json.loads(model.dispatch(DAY)[1])['summary']['observations'],3)

    def test_bad_batch_does_not_acknowledge_cursor(self):
        with patch('sushiwait.readmodel.validate_packet',side_effect=ValueError('synthetic invalid record')):
            with self.assertRaises(ValueError):self.writer.ingest(self.key,now=NOW)
        with connection(self.path) as db:
            self.assertEqual(db.execute('SELECT record_cursor FROM sources').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM pointers').fetchone()[0],0)

    def test_inflight_backlog_keeps_last_good_partition_available(self):
        model=self.ready()
        with connection(self.path,write=True) as db,db:db.execute('UPDATE sources SET backlog=20000')
        self.assertEqual(model.dispatch(DAY)[0],200)

    def test_publisher_heartbeat_loss_is_explicit(self):
        self.ready();path=self.fixture.folder/'status.json'
        path.write_text(json.dumps({'at':'2000-01-01T00:00:00+00:00','policy':'indexed_day_rollups_v1','state':'running'}));path.chmod(0o600)
        model=ReadModelView(self.path,[self.view],status_file=path)
        self.assertEqual(model.dispatch(MONTH)[0],503)

    def test_capacity_warning_keeps_valid_archives_readable(self):
        self.ready();path=self.fixture.folder/'status.json'
        path.write_text(json.dumps({'at':datetime.now(timezone.utc).isoformat(),'policy':'indexed_day_rollups_v1','state':'needs_attention','storage_pressure':True,'data_available':True}));path.chmod(0o600)
        model=ReadModelView(self.path,[self.view],status_file=path)
        self.assertEqual(model.health()['state'],'needs_attention')
        self.assertEqual(model.dispatch(DAY)[0],200)
        self.assertEqual(model.dispatch(MONTH)[0],200)

    def test_large_history_uses_date_and_rowid_indexes(self):
        self.ready()
        # Metadata-only synthetic distractors; the original source is unchanged.
        with connection(self.path,write=True) as db,db:
            db.executemany('INSERT INTO pointers VALUES(?,?,?,?,?,?)',((self.key,'synthetic-'+str(i),i+10,'900001','2000-01-01','0'*64) for i in range(100000)))
            plan=str(db.execute('EXPLAIN QUERY PLAN SELECT source_rowid FROM pointers WHERE source_key=? AND store_id=? AND day=?',(self.key,'900001','2026-10-11')).fetchall())
            self.assertIn('SEARCH',plan);self.assertIn('pointers_day',plan)
            self.assertEqual(db.execute('SELECT count(*) FROM pointers WHERE source_key=? AND store_id=? AND day=?',(self.key,'900001','2026-10-11')).fetchone()[0],2)
            plan=str(db.execute('EXPLAIN QUERY PLAN SELECT summary FROM snapshots WHERE day>=? AND day<?',('2026-10-01','2026-11-01')).fetchall())
            self.assertIn('snapshots_day',plan)

    def test_compressed_blob_has_strict_size_hash_and_trailing_data_checks(self):
        blob,digest=pack({'a':'b'})
        self.assertEqual(unpack(blob,digest),{'a':'b'})
        for bad in [blob+b'junk',blob[:-1]]:
            with self.assertRaises(ValueError):unpack(bad,digest)
        with self.assertRaises(ValueError):unpack(blob,'0'*64)
