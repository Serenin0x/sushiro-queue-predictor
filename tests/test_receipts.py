from copy import deepcopy
from datetime import datetime,timezone
import contextlib,io
import hashlib,json,os,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

from sushiwait import receipts as r
from sushiwait.observations import normalize_snapshot
from sushiwait.packets import build_packet,encoded


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name).resolve();self.folder.chmod(0o700)
        self.input=self.folder/'packet.json';self.db=self.folder/'archive.sqlite3'
        self.at='2026-10-06T01:00:00.000000Z';self.now=datetime(2026,10,6,1,tzinfo=timezone.utc)
        fixture=json.loads((Path(__file__).resolve().parents[1]/'examples/fixtures/store-detail-01.synthetic.json').read_text())
        body=normalize_snapshot(fixture['payload'],'900001',request_started_at=fixture['observed_at'],
            received_at=fixture['observed_at'],elapsed_ms=0,data_origin='synthetic',api_profile='legacy')
        self.row={'id':1,'run_id':'764c40ca-8f27-41b7-8026-28d046d526cb','store_id':'900001',
            'api_profile':'legacy','data_origin':'synthetic','received_at':fixture['observed_at'],
            'ok':1,'payload_json':json.dumps(body)}
        self.packet=build_packet([self.row],as_of=self.at,store_ids=['900001'],api_profile='legacy',data_origin='synthetic')
        self.write(self.packet)

    def write(self,packet):
        self.input.write_bytes(encoded(packet));self.input.chmod(0o600)

    def digest(self,record):
        record['record_sha256']=hashlib.sha256(encoded({k:v for k,v in record.items() if k!='record_sha256'})).hexdigest()

    def validate(self,packet):return r.validate_packet(packet,as_of=self.at)
    def import_file(self):return r.archive_packet(self.input,self.db,now=self.now)
    def count(self):
        con=sqlite3.connect(self.db.resolve().as_uri()+'?mode=ro',uri=True)
        value=con.execute('SELECT COUNT(*) FROM archive_records').fetchone()[0];con.close();return value

    def test_valid_roundtrip_is_copy_without_claiming_server_or_truth(self):
        before=deepcopy(self.packet);clean=self.validate(self.packet)
        self.assertEqual(clean,before);self.assertIsNot(clean,self.packet)
        summary=r.packet_summary(clean);self.assertFalse(summary['source_claims_verified'])
        self.assertFalse(summary['server_received']);self.assertEqual(summary['verified_training_labels'],0)

    def test_unknown_top_record_and_display_fields_are_rejected(self):
        for position in ('top','record','display','timing','field'):
            packet=deepcopy(self.packet);record=packet['records'][0]
            target={'top':packet,'record':record,'display':record['display'],'timing':record['timing'],
                    'field':record['display']['raw_wait']}[position]
            target['private']='PRIVATE_VALUE';self.digest(record)
            with self.assertRaises(r.ReceiptError):self.validate(packet)

    def test_changed_body_without_new_checksum_is_rejected(self):
        packet=deepcopy(self.packet);packet['records'][0]['display']['raw_wait']['value']=100
        with self.assertRaises(r.ReceiptError) as failure:self.validate(packet)
        self.assertEqual(failure.exception.error_code,'packet_checksum_mismatch')

    def test_protocol_flags_cannot_promote_truth_labels_or_delivery(self):
        for key,value in [('server_received',True),('eta_available',True),('verified_training_labels',True),
                          ('verified_training_labels',1),('credentials_included',True),('packet_schema_version',True),
                          ('source_freshness','fresh')]:
            packet=deepcopy(self.packet);packet[key]=value
            with self.assertRaises(r.ReceiptError):self.validate(packet)

    def test_future_packet_record_and_noncanonical_time_are_rejected(self):
        for change in ('packet_future','record_future','offset'):
            packet=deepcopy(self.packet);record=packet['records'][0]
            if change=='packet_future':packet['as_of']='2026-10-07T01:00:00.000000Z'
            elif change=='record_future':record['recorded_at']='2026-10-07T01:00:00.000000Z'
            else:record['recorded_at']='2026-10-02T17:00:00+08:00'
            self.digest(record)
            with self.assertRaises(r.ReceiptError):self.validate(packet)

    def test_duplicate_observation_inside_packet_is_rejected(self):
        packet=deepcopy(self.packet);packet['records'].append(deepcopy(packet['records'][0]))
        with self.assertRaises(r.ReceiptError):self.validate(packet)

    def test_missing_false_and_opaque_labels_are_preserved(self):
        packet=deepcopy(self.packet);record=packet['records'][0]
        record['display']['raw_wait']={'presence':'missing','value':None}
        record['display']['groupQueues']['groups']['mixedQueue']['value']=['19n','100-2']
        self.digest(record);clean=self.validate(packet)
        self.assertEqual(clean['records'][0]['display']['raw_wait']['presence'],'missing')
        self.assertEqual(clean['records'][0]['display']['groupQueues']['groups']['mixedQueue']['value'],['19n','100-2'])

    def test_label_and_group_scope_invalid_values_have_safe_errors(self):
        for change in ('label','parent','store','origin','id','unhashable'):
            packet=deepcopy(self.packet);record=packet['records'][0]
            if change=='label':record['display']['groupQueues']['groups']['mixedQueue']['value']=['13800138000']
            elif change=='parent':record['display']['groupQueues']['presence']='missing'
            elif change=='store':record['store_id']='900002'
            elif change=='origin':record['data_origin']='live'
            elif change=='id':record['observation_id']='PRIVATE_VALUE'
            else:record['display']['groupQueues']['presence']=[]
            self.digest(record)
            with self.assertRaises(r.ReceiptError) as failure:self.validate(packet)
            self.assertNotIn('PRIVATE_VALUE',str(failure.exception))

    def test_preflight_has_local_time_and_no_http_response(self):
        row={**self.row,'ok':0}
        row['payload_json']=json.dumps({'store_id':'900001','api_profile':'legacy','data_origin':'synthetic',
            'failure_phase':'preflight','error_code':'auth_expiring','http_status':None,
            'timing':{'checked_at':row['received_at']}})
        packet=build_packet([row],as_of=self.at,store_ids=['900001'],api_profile='legacy',data_origin='synthetic')
        self.assertEqual(self.validate(packet)['records'][0]['timing']['semantics'],'local_preflight_check')
        packet['records'][0]['http_status']=401;self.digest(packet['records'][0])
        with self.assertRaises(r.ReceiptError):self.validate(packet)

    def test_request_failure_retains_fixed_code_and_status(self):
        row={**self.row,'ok':0};body=json.loads(row['payload_json'])
        body.update(failure_phase='request',error_code='http_error',http_status=504);row['payload_json']=json.dumps(body)
        packet=build_packet([row],as_of=self.at,store_ids=['900001'],api_profile='legacy',data_origin='synthetic')
        self.assertEqual(self.validate(packet)['records'][0]['http_status'],504)

    def test_repeated_import_is_idempotent_and_does_not_rewrite_first_seen(self):
        first=self.import_file();self.assertEqual(first['inserted_records'],1)
        before=hashlib.sha256(self.db.read_bytes()).hexdigest()
        second=self.import_file();self.assertEqual(second['duplicate_records'],1)
        self.assertEqual(second['inserted_records'],0);self.assertEqual(self.count(),1)
        self.assertEqual(before,hashlib.sha256(self.db.read_bytes()).hexdigest())

    def test_same_id_different_body_conflict_rolls_back_entire_page(self):
        self.import_file();before=self.db.read_bytes()
        new=build_packet([{**self.row,'id':2}],as_of=self.at,store_ids=['900001'],api_profile='legacy',data_origin='synthetic')['records'][0]
        changed=deepcopy(self.packet['records'][0]);changed['display']['raw_wait']['value']=100;self.digest(changed)
        packet={**self.packet,'records':[new,changed]};self.write(packet)
        with self.assertRaises(r.ReceiptError) as failure:self.import_file()
        self.assertEqual(failure.exception.error_code,'archive_observation_conflict')
        self.assertEqual(self.count(),1);self.assertEqual(before,self.db.read_bytes())

    def test_invalid_input_never_creates_archive(self):
        self.input.write_text('{"private":"PRIVATE_VALUE"}')
        with self.assertRaises(r.ReceiptError):self.import_file()
        self.assertFalse(self.db.exists())

    def test_duplicate_json_keys_and_nonfinite_values_are_rejected(self):
        for text in ('{"records":[],"records":[]}','{"private":NaN}'):
            self.input.write_text(text)
            with self.assertRaises(r.ReceiptError):r.read_packet(self.input,as_of=self.at)
        self.assertFalse(self.db.exists())

    def test_bad_private_permissions_links_and_large_input_are_rejected(self):
        self.input.chmod(0o644)
        with self.assertRaises(r.ReceiptError):self.import_file()
        self.input.chmod(0o600);link=self.folder/'hard.json';os.link(self.input,link)
        with self.assertRaises(r.ReceiptError):self.import_file()
        link.unlink();self.input.write_bytes(b'x'*(r.MAX_PACKET_BYTES+1))
        with self.assertRaises(r.ReceiptError):self.import_file()
        self.assertFalse(self.db.exists())

    def test_wrong_database_schema_is_preserved_and_rejected(self):
        con=sqlite3.connect(self.db);con.execute('PRAGMA user_version=2');con.close();self.db.chmod(0o600)
        before=self.db.read_bytes()
        with self.assertRaises(r.ReceiptError):self.import_file()
        self.assertEqual(before,self.db.read_bytes())

    def test_input_as_output_is_rejected_without_change(self):
        before=self.input.read_bytes()
        with self.assertRaises(r.ReceiptError):r.archive_packet(self.input,self.input,now=self.now)
        self.assertEqual(before,self.input.read_bytes())

    def test_cooperating_archive_instance_lock_rejects_second_writer(self):
        with r.PacketArchive(self.db):
            with self.assertRaises(r.ReceiptError):r.PacketArchive(self.db)

    def test_import_uses_no_socket_child_client_or_query_credentials(self):
        with patch('socket.socket',side_effect=AssertionError('network')), \
             patch('subprocess.Popen',side_effect=AssertionError('child')), \
             patch('sushiwait.credentials.read_credentials_file',side_effect=AssertionError('auth')):
            result=self.import_file()
        self.assertEqual(result['inserted_records'],1);self.assertFalse(result['server_received'])

    def test_postcommit_directory_failure_reports_committed_and_keeps_data(self):
        with patch('sushiwait.receipts.os.fsync',side_effect=OSError('PRIVATE_VALUE')):
            with self.assertRaises(r.ReceiptError) as failure:self.import_file()
        self.assertEqual(failure.exception.commit_status,'committed');self.assertEqual(self.count(),1)
        self.assertNotIn('PRIVATE_VALUE',str(failure.exception))

    def test_commit_exception_is_unknown_even_if_underlying_commit_succeeded(self):
        class UncertainConnection:
            def __init__(self,connection):self.connection=connection
            def __getattr__(self,name):return getattr(self.connection,name)
            def commit(self):
                self.connection.commit();raise sqlite3.OperationalError('PRIVATE_VALUE')
        with r.PacketArchive(self.db) as archive:
            archive.db=UncertainConnection(archive.db)
            with self.assertRaises(r.ReceiptError) as failure:archive.append(self.packet,as_of=self.at)
        self.assertEqual(failure.exception.commit_status,'unknown');self.assertEqual(self.count(),1)

    def test_insert_exception_rolls_back_without_claiming_commit(self):
        class FailedConnection:
            def __init__(self,connection):self.connection=connection
            def __getattr__(self,name):return getattr(self.connection,name)
            def execute(self,sql,*args):
                if sql.startswith('INSERT'):raise sqlite3.OperationalError('PRIVATE_VALUE')
                return self.connection.execute(sql,*args)
        with r.PacketArchive(self.db) as archive:
            archive.db=FailedConnection(archive.db)
            with self.assertRaises(r.ReceiptError) as failure:archive.append(self.packet,as_of=self.at)
        self.assertEqual(failure.exception.commit_status,'not_started');self.assertEqual(self.count(),0)

    def cli(self,command,*extra):
        from sushiwait.cli import main
        out=io.StringIO()
        with patch('socket.socket',side_effect=AssertionError('network')), \
             patch('socket.create_connection',side_effect=AssertionError('network')), \
             patch('subprocess.Popen',side_effect=AssertionError('child')), \
             patch('sushiwait.cli.client_for',side_effect=AssertionError('client')), \
             patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('auth')), \
             patch('sushiwait.cli._utc_clock',return_value=self.now),contextlib.redirect_stdout(out):
            code=main([command,'--input',str(self.input),*extra])
        self.assertNotIn(str(self.folder),out.getvalue());self.assertNotIn('display',out.getvalue())
        return code,json.loads(out.getvalue())

    def test_cli_check_and_archive_are_aggregate_only_and_idempotent(self):
        code,check=self.cli('packet-check');self.assertEqual(code,0);self.assertTrue(check['validation_only'])
        self.assertFalse(self.db.exists())
        code,first=self.cli('packet-archive','--db',str(self.db));self.assertEqual(code,0)
        code,second=self.cli('packet-archive','--db',str(self.db));self.assertEqual(code,0)
        self.assertEqual(first['inserted_records'],1);self.assertEqual(second['duplicate_records'],1)
        self.assertFalse(second['server_received']);self.assertFalse(second['source_claims_verified'])

    def test_cli_invalid_packet_does_not_create_database(self):
        self.input.write_text('{"private":"PRIVATE_VALUE"}')
        code,result=self.cli('packet-archive','--db',str(self.db))
        self.assertEqual(code,1);self.assertFalse(self.db.exists());self.assertFalse(result['server_received'])
        self.assertNotIn('PRIVATE_VALUE',json.dumps(result))

    def test_cli_unknown_commit_does_not_claim_unwritten_or_success(self):
        with patch('sushiwait.cli.archive_packet',side_effect=r.ReceiptError('archive_commit_unconfirmed',commit_status='unknown')):
            code,result=self.cli('packet-archive','--db',str(self.db))
        self.assertEqual(code,1);self.assertFalse(result['ok'])
        self.assertEqual(result['local_archive_commit_status'],'unknown')


if __name__=='__main__':unittest.main()
