from copy import deepcopy
from datetime import datetime,timezone
from contextlib import contextmanager
import contextlib,io
import hashlib,http.client,json,os,socket,sqlite3,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch

from sushiwait import receiver as r
from sushiwait.observations import normalize_snapshot
from sushiwait.packets import build_packet,encoded
from sushiwait.receipts import ReceiptError


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.root.chmod(0o700)
        self.db=self.root/'archive.sqlite3';self.token='SYNTHETIC_RECEIVER_TEST_ONLY_0123456789'
        self.at='2026-10-06T01:00:00.000000Z';self.now=datetime(2026,10,6,1,tzinfo=timezone.utc)
        fixture=json.loads((Path(__file__).resolve().parents[1]/'examples/fixtures/store-detail-01.synthetic.json').read_text())
        body=normalize_snapshot(fixture['payload'],'900001',request_started_at=fixture['observed_at'],
            received_at=fixture['observed_at'],elapsed_ms=0,data_origin='synthetic',api_profile='legacy')
        row={'id':1,'run_id':'764c40ca-8f27-41b7-8026-28d046d526cb','store_id':'900001',
            'api_profile':'legacy','data_origin':'synthetic','received_at':fixture['observed_at'],
            'ok':1,'payload_json':json.dumps(body)}
        self.packet=build_packet([row],as_of=self.at,store_ids=['900001'],api_profile='legacy',data_origin='synthetic')
        self.body=encoded(self.packet)

    @contextmanager
    def server(self,*,seconds=5,max_requests=1):
        ready=threading.Event();values={}
        def run():
            try:values['result']=r.run_receiver(self.db,self.token,seconds=seconds,max_requests=max_requests,clock=lambda:self.now,
                     on_ready=lambda data:(values.update(data),ready.set()))
            except BaseException as error:values['error']=error;ready.set()
        thread=threading.Thread(target=run,daemon=True);thread.start()
        self.assertTrue(ready.wait(2));self.assertNotIn('error',values)
        try:yield values
        finally:
            thread.join(seconds+2);self.assertFalse(thread.is_alive());self.assertNotIn('error',values)

    def request(self,port,*,body=None,headers=None,path='/v1/public-observations',method='POST'):
        client=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
        try:
            client.request(method,path,self.body if body is None else body,
                headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json',**(headers or {})})
            response=client.getresponse();return response.status,json.loads(response.read())
        finally:client.close()

    def count(self):
        with sqlite3.connect(self.db.resolve().as_uri()+'?mode=ro',uri=True) as db:
            return db.execute('SELECT COUNT(*) FROM archive_records').fetchone()[0]

    def test_library_valid_packet_acknowledges_exact_observations_and_duplicate(self):
        first=r.receive_packet(self.body,self.db,now=self.now)
        second=r.receive_packet(self.body,self.db,now=self.now)
        self.assertEqual((first['inserted_records'],second['inserted_records'],second['duplicate_records']),(1,0,1))
        self.assertEqual(first['observations'],[{k:self.packet['records'][0][k] for k in ('observation_id','record_sha256')}])
        self.assertEqual(first['packet_sha256'],hashlib.sha256(self.body).hexdigest())
        self.assertTrue(first['receiver_archive_received']);self.assertFalse(first['remote_deployment_verified'])
        self.assertFalse(first['source_claims_verified']);self.assertEqual(first['verified_training_labels'],0)

    def test_bad_json_unknown_fields_rekeys_empty_and_future_do_not_create_database(self):
        for body in (b'{}',b'{"records":[],"records":[]}',b'{"private":NaN}',encoded(dict(self.packet,records=[])),
                     encoded(dict(self.packet,as_of='2027-01-01T00:00:00.000000Z'))):
            with self.assertRaises(ReceiptError):r.receive_packet(body,self.db,now=self.now)
            self.assertFalse(self.db.exists())

    def test_conflicting_same_observation_preserves_existing_database(self):
        r.receive_packet(self.body,self.db,now=self.now);before=self.db.read_bytes()
        changed=deepcopy(self.packet);record=changed['records'][0];record['display']['raw_wait']['value']=100
        record['record_sha256']=hashlib.sha256(encoded({k:v for k,v in record.items() if k!='record_sha256'})).hexdigest()
        with self.assertRaises(ReceiptError) as failure:r.receive_packet(encoded(changed),self.db,now=self.now)
        self.assertEqual(failure.exception.error_code,'archive_observation_conflict');self.assertEqual(before,self.db.read_bytes())

    def test_local_commit_unknown_and_committed_unconfirmed_do_not_acknowledge(self):
        for state in ('unknown','committed'):
            with patch.object(r.PacketArchive,'append',side_effect=ReceiptError('archive_commit_unconfirmed',commit_status=state)):
                with self.assertRaises(ReceiptError) as failure:r.receive_packet(self.body,self.db,now=self.now)
            self.assertEqual(failure.exception.commit_status,state)

    def test_library_does_not_access_query_auth_network_or_native(self):
        with patch('socket.socket',side_effect=AssertionError('network')) as sock,patch('subprocess.Popen',side_effect=AssertionError('child')) as child,patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('auth')) as auth:
            r.receive_packet(self.body,self.db,now=self.now)
            self.assertEqual((sock.call_count,child.call_count,auth.call_count),(0,0,0))

    def test_receiver_token_file_is_private_bounded_and_not_a_query_config(self):
        tokenfile=self.root/'receiver.token';tokenfile.write_text(self.token+'\n');tokenfile.chmod(0o600)
        self.assertEqual(r.read_receiver_token(tokenfile),self.token)
        for value in ('short','{"authorization":"PRIVATE"}',self.token+'\nOTHER'):
            tokenfile.write_text(value)
            with self.assertRaises(r.ReceiverError):r.read_receiver_token(tokenfile)
        tokenfile.write_text(self.token);tokenfile.chmod(0o644)
        with self.assertRaises(r.ReceiverError):r.read_receiver_token(tokenfile)

    def test_token_symlink_or_multiple_link_rejected(self):
        tokenfile=self.root/'receiver.token';tokenfile.write_text(self.token);tokenfile.chmod(0o600)
        link=self.root/'link';link.symlink_to(tokenfile)
        with self.assertRaises(r.ReceiverError):r.read_receiver_token(link)
        link.unlink();os.link(tokenfile,link)
        with self.assertRaises(r.ReceiverError):r.read_receiver_token(tokenfile)

    def test_invalid_limits_or_token_fail_before_socket_creation(self):
        with patch('socket.socket',side_effect=AssertionError('network')) as sock:
            for change in ({'seconds':0},{'seconds':61},{'port':True},{'max_requests':0}):
                with self.assertRaises(r.ReceiverError):r.run_receiver(self.db,self.token,**change)
            with self.assertRaises(r.ReceiverError):r.run_receiver(self.db,'short')
            self.assertEqual(sock.call_count,0)

    def test_http_roundtrip_private_archive_and_loopback_only(self):
        with self.server() as values:
            status,body=self.request(values['bound_port'])
        self.assertEqual(status,200);self.assertTrue(body['durability_confirmed']);self.assertEqual(self.count(),1)
        self.assertEqual(values['result']['successful_receipts'],1);self.assertFalse(values['result']['outbound_network_performed'])

    def test_http_bad_auth_rejected_without_reading_packet_or_creating_archive(self):
        with patch.object(r,'receive_packet',side_effect=AssertionError('unexpected_body')) as ingest:
            with self.server() as values:status,body=self.request(values['bound_port'],headers={'Authorization':'Bearer WRONG'})
        self.assertEqual(status,401);self.assertEqual(ingest.call_count,0);self.assertFalse(self.db.exists())
        self.assertNotIn('WRONG',str(body))

    def test_http_host_origin_cookie_transfer_encoding_and_expect_rejected(self):
        for headers in ({'Host':'example.invalid'},{'Origin':'https://example.invalid'},{'Cookie':'PRIVATE=VALUE'},
                        {'Transfer-Encoding':'chunked'},{'Expect':'100-continue'}):
            with self.server() as values:status,body=self.request(values['bound_port'],headers=headers)
            self.assertEqual(status,400);self.assertFalse(self.db.exists());self.assertNotIn('VALUE',str(body))

    def test_http_non_json_bad_length_and_oversized_rejected_without_archive(self):
        for headers,status_wanted in (({'Content-Type':'text/plain'},400),({'Content-Length':'0'},400),
                                     ({'Content-Length':'01'},400),({'Content-Length':str(r.MAX_PACKET_BYTES+1)},413)):
            with self.server() as values:status,_=self.request(values['bound_port'],headers=headers)
            self.assertEqual(status,status_wanted);self.assertFalse(self.db.exists())

    def test_http_wrong_path_query_string_and_method_rejected(self):
        for path,method,wanted in (('/other','POST',404),('/v1/public-observations?secret=VALUE','POST',404),
                                   ('/v1/public-observations','GET',405)):
            with self.server() as values:status,body=self.request(values['bound_port'],path=path,method=method)
            self.assertEqual(status,wanted);self.assertNotIn('VALUE',str(body));self.assertFalse(self.db.exists())

    def test_http_duplicate_authorization_and_lengths_rejected(self):
        for duplicate,wanted in (('Authorization',401),('Content-Length',400)):
            with self.server() as values:
                client=http.client.HTTPConnection('127.0.0.1',values['bound_port'],timeout=3)
                try:
                    client.putrequest('POST','/v1/public-observations')
                    client.putheader('Authorization','Bearer '+self.token);client.putheader('Content-Type','application/json')
                    client.putheader('Content-Length',str(len(self.body)))
                    client.putheader(duplicate,'Bearer '+self.token if duplicate=='Authorization' else str(len(self.body)))
                    client.endheaders(self.body);response=client.getresponse();status=response.status;response.read()
                finally:client.close()
            self.assertEqual(status,wanted);self.assertFalse(self.db.exists())

    def test_http_commit_fault_reports_unknown_without_success_receipt(self):
        with patch.object(r,'receive_packet',side_effect=ReceiptError('archive_commit_unconfirmed',commit_status='unknown')):
            with self.server() as values:status,body=self.request(values['bound_port'])
        self.assertEqual(status,503);self.assertEqual(body['local_archive_commit_status'],'unknown')
        self.assertEqual(values['result']['successful_receipts'],0)

    def test_lost_reply_after_commit_then_exact_retry_is_duplicate(self):
        original=r._Handler._reply
        def lost_reply(handler,status,value):
            if status==200:raise BrokenPipeError('lost_reply')
            return original(handler,status,value)
        with patch.object(r._Handler,'_reply',lost_reply):
            with self.server() as values:
                with self.assertRaises(http.client.RemoteDisconnected):self.request(values['bound_port'])
        self.assertEqual(self.count(),1);self.assertEqual(values['result']['successful_receipts'],0)
        with self.server() as next_values:status,body=self.request(next_values['bound_port'])
        self.assertEqual(status,200);self.assertEqual(body['inserted_records'],0);self.assertEqual(body['duplicate_records'],1)

    def test_idle_deadline_closes_listener(self):
        started=time.monotonic()
        with self.server(seconds=1) as values:port=values['bound_port']
        self.assertLess(time.monotonic()-started,2);self.assertEqual(values['result']['stop_reason'],'deadline')
        with self.assertRaises(OSError):socket.create_connection(('127.0.0.1',port),timeout=0.2)

    def test_partial_header_cannot_keep_connection_beyond_deadline(self):
        started=time.monotonic()
        with self.server(seconds=1) as values:
            client=socket.create_connection(('127.0.0.1',values['bound_port']),timeout=2)
            try:
                client.sendall(b'POST /v1/public-observations HTTP/1.1\r\nHost:')
                client.recv(1024)
            finally:client.close()
        self.assertLess(time.monotonic()-started,2);self.assertFalse(self.db.exists())

    def test_partial_body_deadline_does_not_archive_or_acknowledge(self):
        started=time.monotonic()
        with self.server(seconds=1) as values:
            port=values['bound_port'];client=socket.create_connection(('127.0.0.1',port),timeout=2)
            try:
                headers=f'POST /v1/public-observations HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {self.token}\r\nContent-Type: application/json\r\nContent-Length: 1000\r\n\r\n'
                client.sendall(headers.encode()+b'partial');client.recv(1024)
            finally:client.close()
        self.assertLess(time.monotonic()-started,2);self.assertFalse(self.db.exists())
        self.assertEqual(values['result']['successful_receipts'],0)

    def test_nonascii_authorization_is_rejected_with_fixed_response(self):
        with self.server() as values:status,body=self.request(values['bound_port'],headers={'Authorization':'Bearer '+chr(233)})
        self.assertEqual(status,401);self.assertEqual(body['error_code'],'receiver_unauthorized');self.assertFalse(self.db.exists())

    def test_unknown_method_cannot_echo_request_in_error_response(self):
        with self.server() as values:status,body=self.request(values['bound_port'],method='PRIVATE_METHOD')
        self.assertEqual(status,501);self.assertEqual(body['error_code'],'receiver_http_rejected')
        self.assertNotIn('PRIVATE_METHOD',str(body));self.assertFalse(self.db.exists())

    def test_http_conflict_returns_409_without_changing_first_archive(self):
        with self.server() as values:self.request(values['bound_port'])
        before=self.db.read_bytes();changed=deepcopy(self.packet);record=changed['records'][0]
        record['display']['raw_wait']['value']=100
        record['record_sha256']=hashlib.sha256(encoded({k:v for k,v in record.items() if k!='record_sha256'})).hexdigest()
        with self.server() as values:status,body=self.request(values['bound_port'],body=encoded(changed))
        self.assertEqual(status,409);self.assertEqual(body['local_archive_commit_status'],'not_started')
        self.assertEqual(before,self.db.read_bytes())

    def test_cli_invalid_limits_reject_before_receiver_or_query_credentials(self):
        from sushiwait.cli import main
        with patch('socket.socket',side_effect=AssertionError('network')) as sock,patch('sushiwait.cli.read_receiver_token',side_effect=AssertionError('receiver_auth')) as token,patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('query_auth')) as query:
            out=io.StringIO()
            with contextlib.redirect_stdout(out):
                result=main(['packet-receiver','--db',str(self.db),'--receiver-token-file',str(self.root/'missing'),'--seconds','0'])
            self.assertEqual(result,1);self.assertEqual((sock.call_count,token.call_count,query.call_count),(0,0,0))
        self.assertEqual(json.loads(out.getvalue())['error_code'],'receiver_invalid_limits')

    def test_cli_token_path_conflict_and_unsafe_file_do_not_bind(self):
        from sushiwait.cli import main
        tokenfile=self.root/'receiver.token';tokenfile.write_text(self.token);tokenfile.chmod(0o644)
        with patch('socket.socket',side_effect=AssertionError('network')) as sock:
            for database,expected in ((tokenfile,'receiver_path_conflict'),(self.db,'receiver_token_unsafe')):
                out=io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(main(['packet-receiver','--db',str(database),'--receiver-token-file',str(tokenfile)]),1)
                self.assertEqual(json.loads(out.getvalue())['error_code'],expected);self.assertNotIn(self.token,out.getvalue())
            self.assertEqual(sock.call_count,0)


if __name__=='__main__':unittest.main()
