"""Database adjacency and conservative official projections, without upstream."""
from copy import deepcopy
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sushiwait.cli import main
from sushiwait.continuity import (ContinuityError, select_continuity_packet,
    validate_continuity_bundle, export_continuity_packet, project_continuity_bundle)
from sushiwait.observations import normalize_snapshot
from sushiwait.packets import PacketError, select_packet, encoded
from sushiwait.receipts import ReceiptError, validate_packet
from sushiwait.storage import SnapshotStore

ROOT = Path(__file__).resolve().parents[1]
HOURS = json.loads((ROOT / 'config/default-business-hours.json').read_bytes())
AS_OF = '2026-10-11T12:00:00Z'


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name).resolve()
        self.folder.chmod(0o700)
        self.db = self.folder / 'snapshots.sqlite3'
        self.scope = dict(as_of=AS_OF, store_ids=['900001'], api_profile='miniapp_gateway',
                          data_origin='synthetic')
        self.writer = SnapshotStore(self.db)
        self.addCleanup(self.writer.db.close)

    def snapshot(self, stamp, *, store='900001', labels=('12','13','14'), missing=False):
        groups = {'mixedQueue':list(labels), 'reservationQueue':[], 'boothQueue':[], 'counterQueue':[]}
        if missing: del groups['mixedQueue']
        return normalize_snapshot({'id':int(store),'groupQueues':groups}, store,
            request_started_at=stamp, received_at=stamp, elapsed_ms=0,
            api_profile='miniapp_gateway', data_origin='synthetic')

    def save(self, stamp='2026-10-11T03:00:00Z', **args):
        self.writer.save(self.snapshot(stamp, **args))

    def failure(self, stamp='2026-10-11T03:01:00Z'):
        self.writer.db.execute(
            "INSERT INTO samples(run_id,store_id,data_origin,api_profile,received_at,ok,payload_json) "
            "VALUES(?,?,?,?,?,0,?)", [self.writer.run_id,'900001','synthetic','miniapp_gateway',stamp,
            json.dumps({'store_id':'900001','data_origin':'synthetic','api_profile':'miniapp_gateway',
                        'error_code':'http_error','failure_phase':'request','http_status':403,
                        'timing':{'request_started_at':stamp,'received_at':stamp,'elapsed_ms':0}})])
        self.writer.db.commit()

    def bundle(self, **bounds):
        return select_continuity_packet(self.db, **self.scope, **bounds)[0]

    def day(self, bundle=None):
        return project_continuity_bundle(self.bundle() if bundle is None else bundle,
            as_of=AS_OF, hours=HOURS, base_interval=900)['days'][('900001','2026-10-11')]

    def rehash(self, link):
        link['link_sha256'] = hashlib.sha256(encoded({k:v for k,v in link.items()
                                                    if k != 'link_sha256'})).hexdigest()

    def test_unchanged_v1_records_private_identity_and_readonly_database(self):
        self.save();self.save('2026-10-11T03:01:00Z')
        before = self.db.read_bytes()
        with patch('socket.socket', side_effect=AssertionError('no network')):
            plain, cursor = select_packet(self.db, **self.scope)
            bundle, linked_cursor = select_continuity_packet(self.db, **self.scope)
            self.assertEqual(bundle['packet'], plain)
            self.assertEqual(cursor, linked_cursor)
            self.assertNotIn(self.writer.run_id.encode(), encoded(bundle))
            self.assertFalse(bundle['source_claims_verified'])
            self.assertEqual(self.db.read_bytes(), before)
        with self.assertRaises(ReceiptError): validate_packet(bundle, as_of=AS_OF)
        self.assertEqual(bundle['anchors'], [])

    def test_paginated_predecessor_anchor_preserves_original_hash(self):
        self.save();self.save('2026-10-11T03:01:00Z')
        first = self.bundle(limit=1)
        second = self.bundle(after_id=1, limit=1)
        self.assertEqual(second['anchors'][0]['record'], first['packet']['records'][0])
        self.assertEqual(second['links'][0]['predecessor_sha256'],
                         first['packet']['records'][0]['record_sha256'])
        self.assertEqual(self.day(second)['points'][0]['comparison_state'], 'comparable_display_sets')

    def test_removed_display_sets_use_all_numbers_not_first_three_or_actual_calls(self):
        self.save(labels=('12','13','14','15'))
        self.save('2026-10-11T03:01:00Z', labels=('13','14','16','17'))
        value = self.day()
        self.assertEqual(value['points'][1]['removed_labels']['mixedQueue'], 2)
        self.assertEqual(value['points'][1]['interval_seconds'], 60)
        self.assertIsNone(value['summary']['no_show_rate'])
        self.assertIsNone(value['summary']['actual_called_count'])
        self.assertFalse(value['eta_available'] or value['source_claims_verified'])

    def test_different_snapshotstore_instance_is_boundary(self):
        self.save()
        with SnapshotStore(self.db) as next_writer:
            next_writer.save(self.snapshot('2026-10-11T03:01:00Z'))
        self.assertEqual(self.day()['points'][1]['comparison_state'], 'run_boundary')
        self.assertIsNone(self.day()['points'][1]['removed_labels'])

    def test_failure_is_not_skipped_to_find_previous_success(self):
        self.save();self.failure();self.save('2026-10-11T03:02:00Z')
        value = self.day()
        self.assertEqual([v['comparison_state'] for v in value['points']],
                         ['insufficient','failed_observation_boundary','failed_observation_boundary'])
        self.assertEqual(value['summary']['failed_pairs'], 1)
        self.assertIsNone(value['summary']['display_removed_labels']['mixedQueue'])

    def test_fifteen_minute_samples_remain_gaps_and_cannot_relax_model_guard(self):
        self.save();self.save('2026-10-11T03:15:00Z')
        point = self.day()['points'][1]
        self.assertEqual(point['comparison_state'], 'gap')
        self.assertIsNone(point['removed_labels'])
        for gap in (True,0,121,900):
            with self.assertRaises(ContinuityError):
                project_continuity_bundle(self.bundle(), as_of=AS_OF, hours=HOURS, max_gap_seconds=gap)

    def test_duplicate_and_backwards_clock_do_not_produce_turnover(self):
        self.save();self.save();self.save('2026-10-11T02:59:00Z')
        by_id = {v['observation_id']:v for v in self.day()['points']}
        links = self.bundle()['links']
        for link in links[1:]:
            self.assertEqual(by_id[link['observation_id']]['comparison_state'], 'time_order_or_duplicate')

    def test_midnight_boundary_never_compares_across_days(self):
        self.save('2026-10-10T15:59:00Z');self.save('2026-10-10T16:00:00Z')
        self.assertEqual(self.day()['points'][0]['comparison_state'], 'day_boundary')

    def test_missing_arrays_remain_unknown(self):
        self.save();self.save('2026-10-11T03:01:00Z', missing=True)
        self.assertEqual(self.day()['points'][1]['comparison_state'], 'missing_queue_fields')
        self.assertIsNone(self.day()['points'][1]['removed_labels'])

    def test_store_scoped_predecessors_and_writer_aliases(self):
        self.save();self.save('2026-10-11T03:00:01Z', store='900002')
        self.save('2026-10-11T03:01:00Z')
        bundle = select_continuity_packet(self.db, **{**self.scope,'store_ids':['900001','900002']})[0]
        self.assertNotEqual(bundle['links'][0]['writer_id'], bundle['links'][1]['writer_id'])
        self.assertEqual(bundle['links'][2]['predecessor_id'], bundle['links'][0]['observation_id'])
        bad = deepcopy(bundle)
        bad['links'][2].update(predecessor_id=bad['links'][1]['observation_id'],
            predecessor_sha256=bad['links'][1]['record_sha256'],
            predecessor_writer_id=bad['links'][1]['writer_id'])
        self.rehash(bad['links'][2])
        with self.assertRaises(ContinuityError): validate_continuity_bundle(bad, as_of=AS_OF)

    def test_skipping_selected_predecessor_is_rejected_even_with_valid_hash(self):
        for minute in ('00','01','02'): self.save(f'2026-10-11T03:{minute}:00Z')
        bundle = self.bundle()
        last = bundle['links'][2]
        last.update(predecessor_id=bundle['links'][0]['observation_id'],
                    predecessor_sha256=bundle['links'][0]['record_sha256'])
        self.rehash(last)
        with self.assertRaises(ContinuityError): validate_continuity_bundle(bundle, as_of=AS_OF)

    def test_invalid_links_anchors_extra_secret_keys_and_authenticity_claim_fail(self):
        self.save();self.save('2026-10-11T03:01:00Z')
        bundle = self.bundle(after_id=1)
        cases = []
        bad=deepcopy(bundle);bad['source_claims_verified']=True;cases.append(bad)
        bad=deepcopy(bundle);bad['token']='must not leak';cases.append(bad)
        bad=deepcopy(bundle);bad['links'][0]['writer_id']=self.writer.run_id;cases.append(bad)
        bad=deepcopy(bundle);bad['anchors'][0]['record']['ok']=False;cases.append(bad)
        bad=deepcopy(bundle);bad['links'][0]['predecessor_id']=None;self.rehash(bad['links'][0]);cases.append(bad)
        bad=deepcopy(bundle);bad['anchors'].append(deepcopy(bad['anchors'][0]));cases.append(bad)
        for bad in cases:
            with self.subTest(case=cases.index(bad)),self.assertRaises(ContinuityError):
                validate_continuity_bundle(bad, as_of=AS_OF)

    def test_corrupt_predecessor_aborts_page_even_if_current_record_is_good(self):
        self.save();self.save('2026-10-11T03:01:00Z')
        self.writer.db.execute("UPDATE samples SET payload_json='invalid' WHERE id=1")
        self.writer.db.commit()
        with self.assertRaises(PacketError): self.bundle(after_id=1)
        self.assertEqual(len(select_packet(self.db, **self.scope, after_id=1)[0]['records']),1)

    def test_private_atomic_export_and_cli_flag_leave_plain_format_as_default(self):
        self.save()
        out = self.folder/'bundle.json'
        result = export_continuity_packet(self.db, out, **self.scope)
        self.assertTrue(result['durability_confirmed'])
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(PacketError): export_continuity_packet(self.db, out, **self.scope)
        argv=['packet-export','--db',str(self.db),'--output',str(self.folder/'cli.json'),
              '--store-id','900001','--api-profile','miniapp_gateway',
              '--data-origin','synthetic','--as-of',AS_OF,'--with-continuity']
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(argv), 0)
        self.assertEqual(json.loads(output.getvalue())['continuity_schema_version'], 1)
        self.assertNotIn(self.writer.run_id, output.getvalue())

    def test_selection_and_predecessors_share_one_read_transaction(self):
        self.save();self.save('2026-10-11T03:01:00Z')
        self.writer.db.execute('PRAGMA journal_mode=WAL')
        from sushiwait import packets
        build = packets.build_packet
        appended = False
        def append(rows, **kwargs):
            nonlocal appended
            if rows and not appended:
                appended = True
                with SnapshotStore(self.db) as other:
                    other.save(self.snapshot('2026-10-11T03:02:00Z'))
            return build(rows, **kwargs)
        with patch('sushiwait.packets.build_packet', side_effect=append):
            bundle = self.bundle()
        self.assertEqual(len(bundle['packet']['records']), 2)
        self.assertEqual(len(bundle['links']), 2)
        self.assertEqual(len(self.bundle(after_id=2)['packet']['records']), 1)

    def test_unsafe_database_and_path_conflict_are_rejected(self):
        self.save()
        with self.assertRaisesRegex(ContinuityError,'packet_path_conflict'):
            export_continuity_packet(self.db,self.db,**self.scope)
        self.db.chmod(0o644)
        with self.assertRaises(PacketError): self.bundle()
