from copy import deepcopy
from datetime import datetime,timezone
import hashlib,json,unittest
import contextlib, io, os, sqlite3, tempfile
from pathlib import Path
from unittest.mock import patch
from sushiwait.observations import normalize_snapshot
from sushiwait import packets as m


class PacketStudyTests(unittest.TestCase):
    def row(self, **changes):
        fixture=json.loads((Path(__file__).resolve().parents[1] / 'examples/fixtures/store-detail-01.synthetic.json').read_text())
        value=normalize_snapshot(fixture['payload'],'900001',request_started_at=fixture['observed_at'],
            received_at=fixture['observed_at'],elapsed_ms=0,data_origin='synthetic',api_profile='legacy')
        row={'id':1,'run_id':'764c40ca-8f27-41b7-8026-28d046d526cb','store_id':'900001',
            'api_profile':'legacy','data_origin':'synthetic','received_at':fixture['observed_at'],
            'ok':1,'payload_json':json.dumps(value)}
        row.update(changes)
        return row
    def packet(self, rows=None, **changes):
        args=dict(as_of='2026-10-06T07:00:00+08:00',store_ids=['900001'],api_profile='legacy',data_origin='synthetic')
        args.update(changes)
        return m.build_packet([self.row()] if rows is None else rows,**args)
    def change(self, row, function):
        value=json.loads(row['payload_json']);function(value);row['payload_json']=json.dumps(value)
        return row
    def test_public_fields_and_unknown_units_are_explicit(self):
        record=self.packet()['records'][0]
        self.assertEqual(record['display']['groupQueues']['groups']['reservationQueue']['value'],['R010','R011','R012'])
        self.assertEqual(record['display']['raw_wait']['unit'],'unknown')
        self.assertEqual(record['display']['storeStatus']['value'],'OPEN')
    def test_stable_identity_and_record_digest_across_packet_times(self):
        a=self.packet()['records'][0];b=self.packet(as_of='2026-10-06T08:00:00+08:00')['records'][0]
        self.assertEqual(a['observation_id'],b['observation_id']);self.assertEqual(a['record_sha256'],b['record_sha256'])
        digest=a.pop('record_sha256');self.assertEqual(digest,hashlib.sha256(m.encoded(a)).hexdigest())
    def test_changed_body_keeps_identity_and_changes_digest(self):
        row=self.change(self.row(),lambda v:v['normalized']['groupQueuesCount'].update(value=13))
        a=self.packet()['records'][0];b=self.packet([row])['records'][0]
        self.assertEqual(a['observation_id'],b['observation_id']);self.assertNotEqual(a['record_sha256'],b['record_sha256'])
    def test_extra_raw_fields_and_auth_metadata_are_never_exported(self):
        row=self.change(self.row(),lambda v:v.update(raw_headers={'authorization':'PRIVATE_VALUE'},
            auth_status={'private':'PRIVATE_VALUE'},personal_ticket='PRIVATE_VALUE',transport={'private':'PRIVATE_VALUE'}))
        text=json.dumps(self.packet([row]));self.assertNotIn('PRIVATE_VALUE',text)
        self.assertNotIn('合成示例地址',text);self.assertNotIn('run_id',text)
    def test_phone_like_and_unknown_labels_fail_whole_packet(self):
        for label in ['13800138000','Bearer PRIVATE_VALUE','eyJabc.def.ghi','号码一','']:
            row=self.change(self.row(),lambda v:v['normalized']['groupQueues']['groups']['mixedQueue'].update(value=[label]))
            with self.assertRaises(m.PacketError):self.packet([row])
    def test_label_suffix_order_and_empty_queue_are_preserved(self):
        row=self.change(self.row(),lambda v:v['normalized']['groupQueues']['groups']['mixedQueue'].update(value=['19n','R002','18','100-2']))
        rec=self.packet([row])['records'][0]
        self.assertEqual(rec['display']['groupQueues']['groups']['mixedQueue']['value'],['19n','R002','18','100-2'])
        self.assertEqual(rec['display']['groupQueues']['groups']['counterQueue']['value'],[])
    def test_missing_field_is_not_zero(self):
        row=self.change(self.row(),lambda v:v['normalized'].pop('groupQueuesCount'))
        self.assertEqual(self.packet([row])['records'][0]['display']['groupQueuesCount'],{'presence':'missing','value':None})
    def test_unknown_status_is_not_assumed_open_or_closed(self):
        row=self.change(self.row(),lambda v:v['normalized']['storeStatus'].update(value='PRIVATE_VALUE'))
        with self.assertRaises(m.PacketError):self.packet([row])
    def test_signed_sentinel_is_preserved_without_decoding(self):
        row=self.change(self.row(),lambda v:v['normalized']['waitTimeCounter'].update(presence='present',value=-1))
        self.assertEqual(self.packet([row])['records'][0]['display']['waitTimeCounter']['value'],-1)
    def test_duplicate_rows_and_unexpected_profile_fail(self):
        with self.assertRaises(m.PacketError):self.packet([self.row(),self.row()])
        with self.assertRaises(m.PacketError):self.packet(api_profile='miniapp_gateway')
    def test_future_record_is_rejected(self):
        with self.assertRaises(m.PacketError):self.packet(as_of='2026-10-02T08:59:59Z')
    def test_bad_uuid_or_row_shape_is_rejected(self):
        for changes in [{'run_id':42},{'run_id':'PRIVATE_VALUE'},{'id':True},{'ok':True},{'store_id':'900002'},{'extra':'PRIVATE_VALUE'}]:
            with self.assertRaises(m.PacketError):self.packet([self.row(**changes)])
    def test_scope_nonfinite_and_unhashable_inputs_are_sanitized(self):
        for changes in [{'store_ids':[['900001']]},{'api_profile':[]},{'data_origin':[]},{'as_of':None}]:
            with self.assertRaises(m.PacketError):self.packet(**changes)
    def test_empty_packet_has_no_eta_delivery_or_labels(self):
        result=self.packet([]);self.assertEqual(result['records'],[])
        for key in ['server_received','credentials_included','personal_ticket_included','eta_available']:
            self.assertFalse(result[key])
        self.assertEqual(result['verified_training_labels'],0)
    def test_source_is_unchanged_and_no_socket_native_or_auth_calls(self):
        row=self.row();before=deepcopy(row)
        with patch('socket.socket',side_effect=AssertionError('network')),patch('subprocess.Popen',side_effect=AssertionError('child')):
            self.packet([row])
        self.assertEqual(row,before)
    def test_bad_or_large_payload_is_rejected(self):
        for body in ['not-json','x'*65537,'{"private":NaN}','{}']:
            with self.assertRaises(m.PacketError):self.packet([self.row(payload_json=body)])
    def test_timing_order_and_row_mismatch_is_rejected(self):
        for changes in [{'received_at':'2026-10-02T08:00:00Z'}, {'elapsed_ms':True}, {'request_started_at':'2026-10-02T10:00:00Z'}]:
            row=self.change(self.row(),lambda v:v['timing'].update(changes))
            with self.assertRaises(m.PacketError):self.packet([row])
    def test_failure_records_omit_auth_and_preserve_local_semantics(self):
        row=self.row(ok=0)
        body={'store_id':'900001','data_origin':'synthetic','api_profile':'legacy',
            'failure_phase':'preflight','error_code':'auth_expiring','http_status':None,
            'auth_status':{'private':'PRIVATE_VALUE'},'timing':{'checked_at':row['received_at']}}
        row['payload_json']=json.dumps(body);rec=self.packet([row])['records'][0]
        self.assertEqual(rec['timing']['semantics'],'local_preflight_check')
        self.assertNotIn('PRIVATE_VALUE',json.dumps(rec));self.assertFalse(rec['ok'])
    def test_http_failure_keeps_code_and_response_semantics(self):
        row=self.row(ok=0);body=json.loads(row['payload_json'])
        body.update(failure_phase='request',error_code='http_error',http_status=504)
        row['payload_json']=json.dumps(body);rec=self.packet([row])['records'][0]
        self.assertEqual(rec['http_status'],504);self.assertEqual(rec['timing']['semantics'],'http_response_received')
    def test_unknown_failure_code_is_not_exported(self):
        row=self.row(ok=0);body=json.loads(row['payload_json'])
        body.update(failure_phase='request',error_code='PRIVATE_VALUE',http_status=504)
        row['payload_json']=json.dumps(body)
        with self.assertRaises(m.PacketError):self.packet([row])

    def test_payload_store_identity_must_match(self):
        for update in ({'id':{'presence':'present','value':900002}},
                       {'id':{'presence':'missing','value':None}}):
            row=self.change(self.row(),lambda v:v['normalized'].update(update))
            with self.assertRaises(m.PacketError):self.packet([row])

    def test_group_parent_missing_is_preserved(self):
        row=self.change(self.row(),lambda v:v['normalized'].pop('groupQueues'))
        groups=self.packet([row])['records'][0]['display']['groupQueues']
        self.assertEqual(groups['presence'],'missing')
        self.assertTrue(all(f=={'presence':'missing','value':None} for f in groups['groups'].values()))

    def test_unhashable_nested_values_are_sanitized(self):
        row=self.change(self.row(),lambda v:v['normalized'].update(
            storeStatus={'presence':'present','value':[]}))
        self.assertEqual(self.packet([row])['records'][0]['display']['storeStatus'],
                         {'presence':'invalid','value':None})
        row=self.change(self.row(),lambda v:v['normalized'].update(
            raw_wait={'presence':[],'value':1}))
        self.assertEqual(self.packet([row])['records'][0]['display']['raw_wait'],
                         {'presence':'unknown','value':None})


class PacketIOTests(unittest.TestCase):
    def setUp(self):
        from sushiwait.storage import SnapshotStore
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name).resolve();self.folder.chmod(0o700)
        self.db=self.folder/'samples.sqlite3';self.out=self.folder/'packet.json'
        self.snapshot=json.loads(PacketStudyTests().row()['payload_json'])
        with SnapshotStore(self.db) as store:
            store.save(self.snapshot);store.save(self.snapshot)
        self.scope=dict(as_of='2026-10-06T08:00:00+08:00',store_ids=['900001'],
                       api_profile='legacy',data_origin='synthetic')

    def export(self, **changes):
        return m.export_packet(self.db,self.out,**{**self.scope,**changes})

    def test_readonly_source_and_private_complete_output(self):
        before=hashlib.sha256(self.db.read_bytes()).hexdigest()
        result=self.export()
        self.assertEqual(before,hashlib.sha256(self.db.read_bytes()).hexdigest())
        self.assertEqual(self.out.stat().st_mode & 0o777,0o600)
        self.assertEqual(self.out.stat().st_nlink,1)
        self.assertEqual(result['packet_sha256'],hashlib.sha256(self.out.read_bytes()).hexdigest())
        self.assertEqual(result['record_count'],2);self.assertTrue(result['durability_confirmed'])
        self.assertFalse(result['server_received']);self.assertFalse(result['has_more'])
        self.assertEqual(list(self.folder.glob('.sushiwait-packet-*')),[])

    def test_pagination_only_advances_selected_rows(self):
        first,local=m.select_packet(self.db,**self.scope,limit=1)
        self.assertTrue(local['has_more']);self.assertEqual(local['next_after_id'],1)
        second,end=m.select_packet(self.db,**self.scope,after_id=local['next_after_id'],limit=1)
        self.assertFalse(end['has_more']);self.assertEqual(end['next_after_id'],2)
        self.assertNotEqual(first['records'][0]['observation_id'],second['records'][0]['observation_id'])
        packet,empty=m.select_packet(self.db,**self.scope,after_id=2)
        self.assertEqual(packet['records'],[]);self.assertEqual(empty['next_after_id'],2)

    def test_origin_and_profile_filters_do_not_mix(self):
        from sushiwait.storage import SnapshotStore
        with SnapshotStore(self.db) as store:
            store.save({**self.snapshot,'api_profile':'miniapp_gateway'})
            store.save({**self.snapshot,'data_origin':'fixture'})
        packet,local=m.select_packet(self.db,**self.scope)
        self.assertEqual(len(packet['records']),2);self.assertEqual(local['next_after_id'],2)

    def test_invalid_selected_row_aborts_page_without_output(self):
        con=sqlite3.connect(self.db)
        con.execute('UPDATE samples SET payload_json=? WHERE id=2',('PRIVATE_VALUE',));con.commit();con.close()
        with self.assertRaises(m.PacketError):self.export()
        self.assertFalse(self.out.exists())

    def test_future_row_is_not_skipped_or_cursor_advanced(self):
        with self.assertRaises(m.PacketError):self.export(as_of='2026-10-02T08:00:00Z')
        self.assertFalse(self.out.exists())

    def test_large_selected_payload_fails_without_materialization(self):
        con=sqlite3.connect(self.db);con.execute('UPDATE samples SET payload_json=? WHERE id=1',('x'*65537,));con.commit();con.close()
        with self.assertRaises(m.PacketError):self.export()
        self.assertFalse(self.out.exists())

    def test_existing_and_racing_destinations_are_never_replaced(self):
        self.out.write_bytes(b'previous');self.out.chmod(0o600)
        with self.assertRaises(m.PacketError):self.export()
        self.assertEqual(self.out.read_bytes(),b'previous')
        self.out.unlink()
        real_link=os.link
        def race(*args,**kwargs):
            self.out.write_bytes(b'concurrent');self.out.chmod(0o600)
            return real_link(*args,**kwargs)
        with patch('sushiwait.packets.os.link',side_effect=race):
            with self.assertRaises(m.PacketError):self.export()
        self.assertEqual(self.out.read_bytes(),b'concurrent')
        self.assertEqual(list(self.folder.glob('.sushiwait-packet-*')),[])

    def test_precommit_failure_leaves_no_partial_file(self):
        with patch('sushiwait.packets.os.fsync',side_effect=OSError('PRIVATE_VALUE')):
            with self.assertRaises(m.PacketError) as failure:self.export()
        self.assertFalse(failure.exception.committed);self.assertFalse(self.out.exists())
        self.assertNotIn('PRIVATE_VALUE',str(failure.exception))

    def test_postcommit_fsync_failure_keeps_committed_file(self):
        real_fsync=os.fsync;calls=0
        def fail_last(fd):
            nonlocal calls
            calls+=1
            if calls==3:raise OSError('PRIVATE_VALUE')
            return real_fsync(fd)
        with patch('sushiwait.packets.os.fsync',side_effect=fail_last):result=self.export()
        self.assertTrue(result['committed']);self.assertFalse(result['durability_confirmed'])
        self.assertEqual(len(json.loads(self.out.read_bytes())['records']),2)

    def test_concurrent_append_belongs_to_next_page(self):
        from sushiwait.storage import SnapshotStore
        con=sqlite3.connect(self.db);con.execute('PRAGMA journal_mode=WAL');con.close()
        real_build=m.build_packet;appended=False
        def append_during_read(rows,**kwargs):
            nonlocal appended
            if rows and not appended:
                appended=True
                with SnapshotStore(self.db) as store:store.save(self.snapshot)
            return real_build(rows,**kwargs)
        with patch('sushiwait.packets.build_packet',side_effect=append_during_read):
            first,local=m.select_packet(self.db,**self.scope)
        self.assertEqual(len(first['records']),2);self.assertEqual(local['next_after_id'],2)
        second,next_local=m.select_packet(self.db,**self.scope,after_id=2)
        self.assertEqual(len(second['records']),1);self.assertEqual(next_local['next_after_id'],3)

    def test_source_replacement_is_detected(self):
        original=self.db.read_bytes();real_build=m.build_packet;changed=False
        def replace_during_read(rows,**kwargs):
            nonlocal changed
            if rows and not changed:
                changed=True;self.db.rename(self.folder/'old.sqlite3')
                self.db.write_bytes(original);self.db.chmod(0o600)
            return real_build(rows,**kwargs)
        with patch('sushiwait.packets.build_packet',side_effect=replace_during_read):
            with self.assertRaises(m.PacketError):self.export()
        self.assertFalse(self.out.exists())

    def test_unsafe_source_parent_file_links_and_output_are_rejected(self):
        self.db.chmod(0o644)
        with self.assertRaises(m.PacketError):self.export()
        self.db.chmod(0o600);link=self.folder/'linked.sqlite3';os.link(self.db,link)
        with self.assertRaises(m.PacketError):self.export()
        link.unlink();self.folder.chmod(0o755)
        with self.assertRaises(m.PacketError):self.export()
        self.folder.chmod(0o700)
        destination=self.folder/'symlink.json';destination.symlink_to(self.db)
        with self.assertRaises(m.PacketError):m.export_packet(self.db,destination,**self.scope)

    def test_bounds_and_missing_database_do_not_create_files(self):
        for changes in ({'limit':0},{'limit':1001},{'after_id':-1},{'after_id':True}):
            with self.assertRaises(m.PacketError):self.export(**changes)
        absent=self.folder/'absent.sqlite3'
        with self.assertRaises(m.PacketError):m.export_packet(absent,self.out,**self.scope)
        self.assertFalse(absent.exists());self.assertFalse(self.out.exists())

    def test_unsupported_schema_and_source_symlink_fail_closed(self):
        con=sqlite3.connect(self.db);con.execute('PRAGMA user_version=3');con.close()
        with self.assertRaises(m.PacketError):self.export()
        con=sqlite3.connect(self.db);con.execute('PRAGMA user_version=2');con.close()
        link=self.folder/'source-link.sqlite3';link.symlink_to(self.db)
        with self.assertRaises(m.PacketError):m.export_packet(link,self.out,**self.scope)
        self.assertFalse(self.out.exists())

    def test_same_input_output_path_is_rejected(self):
        before=self.db.read_bytes()
        with self.assertRaises(m.PacketError):m.export_packet(self.db,self.db,**self.scope)
        self.assertEqual(before,self.db.read_bytes())

    def test_cli_outputs_aggregate_only_without_auth_network_or_child(self):
        from sushiwait.cli import main
        out=io.StringIO()
        with patch('socket.socket',side_effect=AssertionError('network')), \
             patch('socket.create_connection',side_effect=AssertionError('network')), \
             patch('subprocess.Popen',side_effect=AssertionError('child')), \
             patch('sushiwait.cli.client_for',side_effect=AssertionError('client')), \
             patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('auth')), \
             contextlib.redirect_stdout(out):
            code=main(['packet-export','--db',str(self.db),'--output',str(self.out),
                       '--store-id','900001','--api-profile','legacy','--data-origin','synthetic',
                       '--as-of',self.scope['as_of']])
        result=json.loads(out.getvalue());self.assertEqual(code,0);self.assertEqual(result['record_count'],2)
        self.assertNotIn('display',result);self.assertNotIn(str(self.folder),out.getvalue())
        self.assertFalse(result['credentials_accessed']);self.assertFalse(result['network_performed'])

    def test_cli_postcommit_uncertain_result_does_not_claim_success(self):
        from sushiwait.cli import main
        out=io.StringIO()
        with patch('sushiwait.cli.export_packet',return_value={'committed':True,'durability_confirmed':False}), \
             contextlib.redirect_stdout(out):
            code=main(['packet-export','--db',str(self.db),'--output',str(self.out),
                       '--store-id','900001','--api-profile','legacy','--data-origin','synthetic',
                       '--as-of',self.scope['as_of']])
        result=json.loads(out.getvalue());self.assertEqual(code,1);self.assertFalse(result['ok'])
        self.assertTrue(result['committed']);self.assertEqual(result['error_code'],'packet_durability_unconfirmed')


if __name__=='__main__':unittest.main()
