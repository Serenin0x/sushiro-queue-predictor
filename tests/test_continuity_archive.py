"""Separate transactional continuity archive and real official read routes."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from sushiwait.continuity import select_continuity_packet, validate_continuity_bundle
from sushiwait.continuityarchive import ContinuityArchive
from sushiwait.continuityview import ContinuityStatisticsView
from sushiwait.observations import normalize_snapshot
from sushiwait.monitoring import _stamp, _time
from sushiwait.officialview import OfficialStatisticsView, read_config
from sushiwait.packets import encoded
from sushiwait.receipts import PacketArchive, ReceiptError
from sushiwait.storage import SnapshotStore

ROOT = Path(__file__).resolve().parents[1]
HOURS = json.loads((ROOT/'config/default-business-hours.json').read_bytes())
AT = '2026-10-11T12:00:00Z'


class ContinuityArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name).resolve();self.folder.chmod(0o700)
        self.source = self.folder/'source.sqlite3'
        self.database = self.folder/'linked.sqlite3'
        self.scope = dict(as_of=AT,store_ids=['900001'],api_profile='miniapp_gateway',data_origin='synthetic')
        with SnapshotStore(self.source) as writer:
            for minute, labels in [('00',['12','13','14','15']),('01',['13','14','16'])]:
                stamp = f'2026-10-11T03:{minute}:00Z'
                writer.save(normalize_snapshot({'id':900001,'groupQueues':{'mixedQueue':labels,
                    'reservationQueue':[],'counterQueue':[],'boothQueue':[]}},'900001',
                    request_started_at=stamp,received_at=stamp,elapsed_ms=0,
                    api_profile='miniapp_gateway',data_origin='synthetic'))

    def bundle(self, **bounds):
        return select_continuity_packet(self.source,**self.scope,**bounds)[0]

    def append(self, bundle=None, at=AT):
        with ContinuityArchive(self.database) as archive:
            return archive.append_bundle(self.bundle() if bundle is None else bundle,as_of=at)

    def view(self, kind=ContinuityStatisticsView):
        return kind(self.database,store_names={'900001':'Synthetic'},hours=HOURS,
                    data_origin='synthetic',base_interval=900)

    def day(self):
        return self.view().read('/api/v1/official/stores/900001/days/2026-10-11',
                                now=datetime.fromisoformat(AT.replace('Z','+00:00')))

    def rehash(self, link):
        link['link_sha256']=hashlib.sha256(encoded({k:v for k,v in link.items() if k!='link_sha256'})).hexdigest()

    def count(self):
        with sqlite3.connect(self.database) as db:
            return tuple(db.execute('SELECT count(*) FROM '+table).fetchone()[0]
                         for table in ('archive_records','continuity_claims','continuity_bundles'))

    def test_plain_import_and_atomic_bundle_duplicate_keep_original_hashes(self):
        bundle=self.bundle()
        with ContinuityArchive(self.database) as archive:
            archive.append(bundle['packet'],as_of=AT)
        result=self.append(bundle)
        self.assertEqual(result['inserted_records'],0)
        self.assertEqual(result['inserted_links'],2)
        again=self.append(bundle)
        self.assertEqual(again['duplicate_records'],2)
        self.assertEqual(again['duplicate_links'],2)
        self.assertEqual(self.count(),(2,2,1))
        with sqlite3.connect(self.database) as db:
            for record in bundle['packet']['records']:
                saved=db.execute('SELECT record_sha256,record_json FROM archive_records WHERE observation_id=?',
                                 [record['observation_id']]).fetchone()
                self.assertEqual(saved,(record['record_sha256'],encoded(record).decode()))

    def test_existing_v1_database_never_migrates_and_v1_reader_rejects_new_kind(self):
        with PacketArchive(self.database) as old:
            old.append(self.bundle()['packet'],as_of=AT)
        before=self.database.read_bytes()
        with self.assertRaises(ReceiptError): ContinuityArchive(self.database)
        self.assertEqual(before,self.database.read_bytes())
        self.assertEqual(self.view().dispatch('/api/v1/official/months/2026-10')[0],503)
        other=self.folder/'new.sqlite3'
        with ContinuityArchive(other) as archive:
            archive.append_bundle(self.bundle(),as_of=AT)
        with self.assertRaises(ReceiptError): PacketArchive(other)
        old_view=OfficialStatisticsView(other,store_names={'900001':'Synthetic'},hours=HOURS,data_origin='synthetic')
        self.assertEqual(old_view.dispatch('/api/v1/official/months/2026-10')[0],503)

    def test_paginated_anchor_later_gains_link_without_changing_records_or_first_claim(self):
        self.append(self.bundle(after_id=1))
        first=self.bundle(limit=1);identity=first['links'][0]['observation_id']
        with sqlite3.connect(self.database) as db:
            before=db.execute('SELECT record_json FROM archive_records WHERE observation_id=?',[identity]).fetchone()
            self.assertEqual(db.execute('SELECT link_json,linked_at FROM continuity_claims WHERE observation_id=?',
                                       [identity]).fetchone(),(None,None))
        result=self.append(first,at='2026-10-11T12:01:00Z')
        self.assertEqual(result['inserted_links'],1)
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute('SELECT record_json FROM archive_records WHERE observation_id=?',[identity]).fetchone(),before)
            self.assertEqual(db.execute('SELECT claimed_at,linked_at FROM continuity_claims WHERE observation_id=?',[identity]).fetchone(),
                             (_stamp(_time(AT)),_stamp(_time('2026-10-11T12:01:00Z'))))

    def test_writer_conflict_rolls_back_all_new_observations_and_bundle(self):
        self.append()
        with SnapshotStore(self.source) as writer:
            writer.save(normalize_snapshot({'id':900001,'groupQueues':{'mixedQueue':[],'reservationQueue':[],
                'counterQueue':[],'boothQueue':[]}},'900001',request_started_at='2026-10-11T03:02:00Z',
                received_at='2026-10-11T03:02:00Z',elapsed_ms=0,api_profile='miniapp_gateway',data_origin='synthetic'))
        bundle=self.bundle();alias=str(uuid5(NAMESPACE_URL,'different-writer'))
        bundle['links'][0]['writer_id']=alias;self.rehash(bundle['links'][0])
        bundle['links'][1]['predecessor_writer_id']=alias;self.rehash(bundle['links'][1])
        validate_continuity_bundle(bundle,as_of=AT)
        with self.assertRaisesRegex(ReceiptError,'continuity_archive_writer_conflict'): self.append(bundle)
        self.assertEqual(self.count(),(2,2,1))

    def test_conflicting_root_relation_is_rejected_without_mutating_archive(self):
        self.append()
        bundle=self.bundle(after_id=1);bundle['anchors']=[]
        link=bundle['links'][0]
        link.update(predecessor_id=None,predecessor_sha256=None,predecessor_writer_id=None);self.rehash(link)
        validate_continuity_bundle(bundle,as_of=AT)
        before=self.database.read_bytes()
        with self.assertRaisesRegex(ReceiptError,'continuity_archive_link_conflict'):self.append(bundle)
        self.assertEqual(self.database.read_bytes(),before)

    def test_view_retains_unknown_plain_record_and_correct_mapped_annotation(self):
        bundle=self.bundle()
        with ContinuityArchive(self.database) as archive:archive.append(bundle['packet'],as_of=AT)
        self.append(self.bundle(limit=1))
        with patch('socket.socket',side_effect=AssertionError('no origin')):
            value=self.day()
        self.assertEqual(value['summary']['observations'],2)
        self.assertEqual([p['comparison_state'] for p in value['points']],['insufficient','packet_continuity_unknown'])
        self.assertIsNone(value['summary']['display_removed_labels']['mixedQueue'])
        self.append()
        value=self.day()
        self.assertEqual(value['points'][1]['removed_labels']['mixedQueue'],2)
        self.assertEqual(value['points'][1]['continuity_received_at'],_stamp(_time(AT)))
        self.assertEqual(value['summary']['comparable_display_pairs'],1)
        self.assertIsNone(value['summary']['no_show_rate'])
        self.assertFalse(value['eta_available'] or value['source_claims_verified'])

    def test_explicit_seven_key_config_selects_new_kind_and_old_mode_stays_old(self):
        self.append();file=self.folder/'view.json'
        config={'schema_version':1,'mode':'official_packet_archive_with_continuity',
                'database_file':str(self.database),'store_names':{'900001':'Synthetic'},
                'data_origin':'synthetic','hours':HOURS,'base_interval':900}
        file.write_bytes(encoded(config));file.chmod(0o600)
        self.assertIsInstance(read_config(file),ContinuityStatisticsView)
        config['mode']='official_packet_archive_readonly';file.write_bytes(encoded(config))
        self.assertIs(type(read_config(file)),OfficialStatisticsView)

    def test_missing_reference_tampered_columns_and_link_bytes_fail_closed(self):
        self.append();bundle=self.bundle();first,last=bundle['packet']['records']
        for sql,params in [
            ('UPDATE archive_records SET store_id=? WHERE observation_id=?',['900002',first['observation_id']]),
            ('UPDATE continuity_claims SET link_json=? WHERE observation_id=?',['x'*4097,last['observation_id']]),
            ('DELETE FROM archive_records WHERE observation_id=?',[first['observation_id']])]:
            with self.subTest(sql=sql):
                with sqlite3.connect(self.database) as db:
                    db.execute('SAVEPOINT damage');db.execute(sql,params);db.commit()
                self.assertEqual(self.view().dispatch('/api/v1/official/stores/900001/days/2026-10-11')[0],503)
                self.database.unlink();self.append()

    def test_saved_view_matches_full_bundle_projection_without_rebuilding_each_point(self):
        from sushiwait.continuity import project_continuity_bundle
        bundle=self.bundle();self.append(bundle)
        expected=project_continuity_bundle(bundle,as_of=AT,hours=HOURS,base_interval=900,
            store_names={'900001':'Synthetic'})['days'][('900001','2026-10-11')]
        actual=self.day()
        for a,b in zip(actual['points'],expected['points']):
            for key in ('continuity','comparison_state','interval_seconds','removed_labels'):
                self.assertEqual(a[key],b[key])
        for key in ('continuity_states','comparable_display_pairs','display_removed_labels'):
            self.assertEqual(actual['summary'][key],expected['summary'][key])

    def test_warm_projection_revalidates_later_links_and_predecessor_damage(self):
        bundle=self.bundle()
        with ContinuityArchive(self.database) as archive:archive.append(bundle['packet'],as_of=AT)
        view=self.view();path='/api/v1/official/stores/900001/days/2026-10-11'
        now=datetime.fromisoformat(AT.replace('Z','+00:00'))
        self.assertEqual(view.read(path,now=now)['points'][1]['comparison_state'],'packet_continuity_unknown')
        self.append()
        self.assertEqual(view.read(path,now=now)['points'][1]['comparison_state'],'comparable_display_sets')
        first=bundle['packet']['records'][0]['observation_id']
        with sqlite3.connect(self.database) as db:db.execute('UPDATE archive_records SET store_id=? WHERE observation_id=?',['900002',first])
        self.assertEqual(view.dispatch(path)[0],503)

    def test_postcommit_directory_sync_error_reports_committed_without_erasing_data(self):
        with ContinuityArchive(self.database) as archive:
            with patch('sushiwait.continuityarchive.os.fsync',side_effect=OSError('do not echo')):
                with self.assertRaises(ReceiptError) as error:archive.append_bundle(self.bundle(),as_of=AT)
            self.assertEqual(error.exception.commit_status,'committed')
            self.assertNotIn('do not echo',str(error.exception))
        self.assertEqual(self.count(),(2,2,1))
        self.assertEqual(self.append()['duplicate_links'],2)

    def test_real_reader_lock_rolls_back_without_erasing_pending_data(self):
        with ContinuityArchive(self.database):pass
        reader=sqlite3.connect(self.database);self.addCleanup(reader.close)
        reader.execute('BEGIN');reader.execute('SELECT count(*) FROM archive_records').fetchone()
        bundle=self.bundle()
        with ContinuityArchive(self.database) as archive:
            with self.assertRaises(ReceiptError) as error:archive.append_bundle(bundle,as_of=AT)
            self.assertEqual(error.exception.error_code,'continuity_archive_busy')
            self.assertEqual(error.exception.commit_status,'unknown')
        reader.rollback()
        self.assertEqual(self.count(),(0,0,0))
        result=self.append(bundle);self.assertEqual(result['inserted_records'],2)
        self.assertEqual(self.append(bundle)['inserted_records'],0)

    def test_unlinked_anchor_and_later_link_keep_separate_reception_times(self):
        self.append(self.bundle(after_id=1))
        first=self.bundle(limit=1)
        self.append(first,at='2026-10-11T12:01:00Z')
        with self.assertRaises(ValueError):
            self.view().read('/api/v1/official/stores/900001/days/2026-10-11',
                             now=datetime.fromisoformat(AT.replace('Z','+00:00')))
        value=self.view().read('/api/v1/official/stores/900001/days/2026-10-11',
                              now=datetime.fromisoformat('2026-10-11T12:02:00+00:00'))
        self.assertEqual(value['points'][0]['continuity_received_at'],_stamp(_time('2026-10-11T12:01:00Z')))

    def test_month_projection_and_scope_route_remain_bounded_and_source_explicit(self):
        self.append()
        value=self.view().read('/api/v1/official/months/2026-10',
                               now=datetime.fromisoformat(AT.replace('Z','+00:00')))
        self.assertEqual(value['source'],'sapi_miniapp_gateway')
        self.assertEqual(value['days']['2026-10-11']['900001']['continuity_states'],
                         {'insufficient':1,'comparable_display_sets':1})
        self.assertEqual(self.view().dispatch('/api/v1/official/stores/900002/days/2026-10-11')[0],404)

    def test_duplicate_receipt_cannot_accept_damaged_columns_or_missing_link_time(self):
        self.append();identity=self.bundle()['links'][0]['observation_id']
        for sql,value in [('UPDATE archive_records SET store_id=? WHERE observation_id=?','900002'),
                          ('UPDATE continuity_claims SET linked_at=? WHERE observation_id=?',None)]:
            with self.subTest(sql=sql):
                with sqlite3.connect(self.database) as db:db.execute(sql,[value,identity]);db.commit()
                before=self.database.read_bytes()
                with self.assertRaises(ReceiptError):self.append()
                self.assertEqual(before,self.database.read_bytes())
                self.database.unlink();self.append()

    def test_completed_anchor_cannot_create_cycle_between_valid_individual_pages(self):
        self.append(self.bundle(after_id=1))
        first=self.bundle(limit=1);second=self.bundle()['packet']['records'][1]
        first['anchors']=[{'record':second,'writer_id':self.bundle()['links'][1]['writer_id']}]
        first['links'][0].update(predecessor_id=second['observation_id'],predecessor_sha256=second['record_sha256'],
            predecessor_writer_id=first['anchors'][0]['writer_id']);self.rehash(first['links'][0])
        validate_continuity_bundle(first,as_of=AT)
        before=self.database.read_bytes()
        with self.assertRaisesRegex(ReceiptError,'continuity_archive_graph_cycle'):self.append(first)
        self.assertEqual(before,self.database.read_bytes())
