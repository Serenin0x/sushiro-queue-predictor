from copy import deepcopy
from datetime import datetime,timezone
import contextlib,io,hashlib,hmac,json,time,unittest
from pathlib import Path
from unittest.mock import patch
import test_receiver as fixture

from sushiwait import delivery as d,receiver as r
from sushiwait.packets import encoded


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        fixture.ReceiverTests.setUp(self)
        self.packet_file=self.root/'packet.json';self.packet_file.write_bytes(self.body);self.packet_file.chmod(0o600)
        self.token_file=self.root/'receiver.token';self.token_file.write_text(self.token);self.token_file.chmod(0o600)
        self.confirmation=self.root/'confirmation.json'

    def signed(self):
        receipt={'ok':True,**r.receive_packet(self.body,self.db,now=self.now),'receipt_schema_version':2}
        self.sign(receipt);return receipt

    def sign(self,receipt):
        receipt['receipt_hmac_sha256']=hmac.new(self.token.encode(),encoded({k:v for k,v in receipt.items() if k!='receipt_hmac_sha256'}),hashlib.sha256).hexdigest()

    def deliver(self,port=12345):
        return d.deliver_local(self.packet_file,self.token_file,self.confirmation,port=port,clock=lambda:self.now)

    def test_signed_receipt_exact_packet_and_each_observation(self):
        receipt=self.signed();clean=d.validate_receipt(receipt,self.packet,self.token)
        self.assertEqual(clean,receipt);self.assertIsNot(clean,receipt)

    def test_wrong_key_or_tampered_counts_rejected(self):
        receipt=self.signed()
        with self.assertRaises(d.DeliveryError):d.validate_receipt(receipt,self.packet,'OTHER_SYNTHETIC_RECEIVER_KEY_0123456789')
        receipt['inserted_records']=0
        with self.assertRaises(d.DeliveryError) as error:d.validate_receipt(receipt,self.packet,self.token)
        self.assertEqual(error.exception.error_code,'delivery_receipt_hmac_mismatch')

    def test_resigned_wrong_packet_missing_reordered_or_changed_observation_rejected(self):
        for mutation in ('packet','missing','checksum','extra'):
            receipt=self.signed()
            if mutation=='packet':receipt['packet_sha256']='0'*64
            elif mutation=='missing':receipt['observations']=[]
            elif mutation=='checksum':receipt['observations'][0]['record_sha256']='0'*64
            else:receipt['observations'][0]['private']='VALUE'
            self.sign(receipt)
            with self.assertRaises(d.DeliveryError):d.validate_receipt(receipt,self.packet,self.token)

    def test_legacy_unsigned_unknown_fields_and_boolean_versions_rejected(self):
        for key,value in (('receipt_schema_version',1),('receipt_schema_version',True),('durability_confirmed',False),
                          ('remote_deployment_verified',True),('verified_training_labels',True),('private','VALUE')):
            receipt=self.signed();receipt[key]=value;self.sign(receipt)
            with self.assertRaises(d.DeliveryError):d.validate_receipt(receipt,self.packet,self.token)

    def test_local_http_send_sign_verify_save_and_reopen_no_source_mutation(self):
        before=self.packet_file.read_bytes()
        with fixture.ReceiverTests.server(self) as values:result=self.deliver(values['bound_port'])
        self.assertTrue(result['committed']);self.assertTrue(result['durability_confirmed']);self.assertTrue(result['receipt_hmac_verified'])
        self.assertEqual(result['receiver_commit_status'],'reported_committed')
        report=d.check_confirmation(self.packet_file,self.confirmation,self.token_file,as_of=self.at)
        self.assertTrue(report['historical_confirmation_only']);self.assertFalse(report['network_performed'])
        self.assertEqual(before,self.packet_file.read_bytes());self.assertFalse(result['remote_deployment_verified'])

    def test_bad_port_existing_output_and_path_conflict_make_no_connection_or_key_read(self):
        with patch.object(d,'_exchange',side_effect=AssertionError('network')) as exchange,patch.object(d,'read_receiver_token',side_effect=AssertionError('key')) as key:
            with self.assertRaises(d.DeliveryError):self.deliver(True)
            with self.assertRaises(d.DeliveryError):d.deliver_local(self.packet_file,self.token_file,self.packet_file,port=12345)
            self.confirmation.write_text('KEEP');self.confirmation.chmod(0o600)
            with self.assertRaises(d.DeliveryError):self.deliver()
            self.assertEqual((exchange.call_count,key.call_count),(0,0));self.assertEqual(self.confirmation.read_text(),'KEEP')

    def test_invalid_packet_or_nonprivate_output_parent_does_not_connect(self):
        with patch.object(d,'_exchange',side_effect=AssertionError('network')) as exchange:
            self.packet_file.write_bytes(b'{}')
            with self.assertRaises(d.DeliveryError):self.deliver()
            self.packet_file.write_bytes(self.body);self.root.chmod(0o755)
            with self.assertRaises(d.DeliveryError):self.deliver()
            self.assertEqual(exchange.call_count,0)

    def test_network_failure_keeps_packet_and_no_confirmation(self):
        with patch.object(d,'_exchange',side_effect=d.DeliveryError('delivery_transport_failed',receiver_commit_status='unknown')):
            with self.assertRaises(d.DeliveryError) as failure:self.deliver()
        self.assertEqual(failure.exception.receiver_commit_status,'unknown');self.assertFalse(self.confirmation.exists())
        self.assertEqual(self.packet_file.read_bytes(),self.body)

    def test_invalid_receipt_keeps_packet_with_receiver_status_unknown(self):
        receipt=self.signed();receipt['receipt_hmac_sha256']='0'*64
        with patch.object(d,'_exchange',return_value=receipt):
            with self.assertRaises(d.DeliveryError) as failure:self.deliver()
        self.assertEqual(failure.exception.receiver_commit_status,'unknown');self.assertFalse(self.confirmation.exists())

    def test_save_before_and_after_publication_failures_preserve_receiver_state(self):
        from sushiwait.packets import PacketError
        for committed in (False,True):
            with patch.object(d,'_exchange',return_value=self.signed()),patch.object(d,'_write_packet',side_effect=PacketError('packet_output_error',committed=committed)):
                with self.assertRaises(d.DeliveryError) as failure:self.deliver()
            self.assertEqual(failure.exception.confirmation_committed,committed)
            self.assertEqual(failure.exception.receiver_commit_status,'reported_committed')

    def test_clock_reversal_after_receipt_does_not_save(self):
        ticks=iter((self.now,datetime(2026,10,5,tzinfo=timezone.utc)))
        with patch.object(d,'_exchange',return_value=self.signed()):
            with self.assertRaises(d.DeliveryError):d.deliver_local(self.packet_file,self.token_file,self.confirmation,port=12345,clock=lambda:next(ticks))
        self.assertFalse(self.confirmation.exists())

    def test_slow_trickling_body_is_cut_off_by_independent_deadline(self):
        def slow_reply(handler,status,value):
            body=encoded(value)
            handler.send_response(status);handler.send_header('Content-Type','application/json')
            handler.send_header('Content-Length',str(len(body)));handler.end_headers()
            try:
                for byte in body:
                    handler.wfile.write(bytes([byte]));handler.wfile.flush();time.sleep(.15)
            except OSError:pass
        started=time.monotonic()
        with patch.object(d,'NETWORK_SECONDS',1),patch.object(r._Handler,'_reply',slow_reply):
            with fixture.ReceiverTests.server(self) as values:
                with self.assertRaises(d.DeliveryError) as error:self.deliver(values['bound_port'])
        self.assertEqual(error.exception.error_code,'delivery_network_deadline')
        self.assertEqual(error.exception.receiver_commit_status,'unknown')
        self.assertLess(time.monotonic()-started,3);self.assertFalse(self.confirmation.exists())
        self.assertEqual(self.packet_file.read_bytes(),self.body)

    def test_connection_refused_has_not_attempted_commit_state(self):
        with patch('socket.create_connection',side_effect=ConnectionRefusedError):
            with self.assertRaises(d.DeliveryError) as error:self.deliver()
        self.assertEqual(error.exception.receiver_commit_status,'not_attempted')
        self.assertFalse(self.confirmation.exists())

    def test_receipt_loss_after_real_commit_can_retry_without_new_records(self):
        original=r._Handler._reply
        def truncate(handler,status,value):
            if status!=200:return original(handler,status,value)
            handler.send_response(200);handler.send_header('Content-Type','application/json')
            handler.send_header('Content-Length','100');handler.end_headers();handler.wfile.write(b'{')
        with patch.object(r._Handler,'_reply',truncate):
            with fixture.ReceiverTests.server(self) as values:
                with self.assertRaises(d.DeliveryError) as error:self.deliver(values['bound_port'])
        self.assertEqual(error.exception.receiver_commit_status,'unknown');self.assertFalse(self.confirmation.exists())
        self.assertEqual(fixture.ReceiverTests.count(self),1)
        with fixture.ReceiverTests.server(self) as values:result=self.deliver(values['bound_port'])
        self.assertEqual(result['confirmed_record_count'],1)
        artifact=json.loads(self.confirmation.read_text());self.assertEqual(artifact['receipt']['inserted_records'],0)
        self.assertEqual(artifact['receipt']['duplicate_records'],1);self.assertEqual(fixture.ReceiverTests.count(self),1)

    def test_confirmation_tamper_wrong_key_and_unsafe_file_fail_without_network(self):
        with patch.object(d,'_exchange',return_value=self.signed()):self.deliver()
        original=self.confirmation.read_bytes()
        with patch('socket.socket',side_effect=AssertionError('network')) as sock:
            for key,value in (('receiver_host','example.invalid'),('receiver_port',True),('scope_sha256','0'*64),
                              ('received_at','2027-01-01T00:00:00.000000Z'),('private','VALUE')):
                artifact=json.loads(original);artifact[key]=value;self.confirmation.write_bytes(encoded(artifact))
                with self.assertRaises(d.DeliveryError):d.check_confirmation(self.packet_file,self.confirmation,self.token_file,as_of=self.at)
            self.confirmation.write_bytes(original);self.confirmation.chmod(0o644)
            with self.assertRaises(d.DeliveryError):d.check_confirmation(self.packet_file,self.confirmation,self.token_file,as_of=self.at)
            self.confirmation.chmod(0o600);self.token_file.write_text('OTHER_SYNTHETIC_RECEIVER_KEY_0123456789')
            with self.assertRaises(d.DeliveryError):d.check_confirmation(self.packet_file,self.confirmation,self.token_file,as_of=self.at)
            self.assertEqual(sock.call_count,0)

    def test_cli_local_roundtrip_and_offline_reopen(self):
        from sushiwait.cli import main
        output=io.StringIO()
        with patch('sushiwait.cli._utc_clock',return_value=self.now),contextlib.redirect_stdout(output):
            with fixture.ReceiverTests.server(self) as values:
                result=main(['packet-deliver-local','--input',str(self.packet_file),'--receiver-token-file',str(self.token_file),
                             '--confirmation',str(self.confirmation),'--port',str(values['bound_port'])])
            reopened=main(['receipt-check','--input',str(self.packet_file),'--receiver-token-file',str(self.token_file),
                           '--confirmation',str(self.confirmation)])
        self.assertEqual((result,reopened),(0,0))
        records=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(all(item['ok'] and item['receipt_hmac_verified'] for item in records))
        self.assertFalse(records[1]['network_performed']);self.assertNotIn(self.token,output.getvalue())

    def test_cli_bad_port_and_missing_confirmation_are_fixed_safe_errors(self):
        from sushiwait.cli import main
        output=io.StringIO()
        with patch('sushiwait.cli._utc_clock',return_value=self.now),contextlib.redirect_stdout(output),patch.object(d,'_exchange',side_effect=AssertionError('network')) as exchange:
            result=main(['packet-deliver-local','--input',str(self.packet_file),'--receiver-token-file',str(self.token_file),
                         '--confirmation',str(self.confirmation),'--port','0'])
            reopened=main(['receipt-check','--input',str(self.packet_file),'--receiver-token-file',str(self.token_file),
                           '--confirmation',str(self.confirmation)])
        self.assertEqual((result,reopened),(1,1));self.assertEqual(exchange.call_count,0)
        self.assertNotIn(self.token,output.getvalue());self.assertNotIn(str(self.root),output.getvalue())

    def test_postpublication_durability_failure_returns_committed_without_claiming_durable(self):
        from sushiwait.cli import main
        output=io.StringIO()
        with patch('sushiwait.cli._utc_clock',return_value=self.now),patch.object(d,'_exchange',return_value=self.signed()),\
             patch.object(d,'_write_packet',return_value={'committed':True,'durability_confirmed':False}),contextlib.redirect_stdout(output):
            result=main(['packet-deliver-local','--input',str(self.packet_file),'--receiver-token-file',str(self.token_file),
                         '--confirmation',str(self.confirmation),'--port','12345'])
        data=json.loads(output.getvalue());self.assertEqual(result,1);self.assertFalse(data['ok'])
        self.assertTrue(data['committed']);self.assertFalse(data['durability_confirmed'])
        self.assertEqual(data['receiver_commit_status'],'reported_committed')

    def test_redirect_cookie_duplicate_headers_and_bad_json_do_not_save_or_follow(self):
        for variation in ('redirect','cookie','duplicate_length','duplicate_json','nonfinite'):
            def bad_reply(handler,status,value):
                body=(b'{"ok":true,"ok":true}' if variation=='duplicate_json'
                      else b'{"ok":NaN}' if variation=='nonfinite' else encoded(value))
                handler.send_response(302 if variation=='redirect' else status)
                handler.send_header('Content-Type','application/json');handler.send_header('Content-Length',str(len(body)))
                if variation=='redirect':handler.send_header('Location','https://example.invalid/private')
                if variation=='cookie':handler.send_header('Set-Cookie','private=VALUE')
                if variation=='duplicate_length':handler.send_header('Content-Length',str(len(body)))
                handler.end_headers();handler.wfile.write(body)
            with patch.object(r._Handler,'_reply',bad_reply):
                with fixture.ReceiverTests.server(self) as values:
                    with self.assertRaises(d.DeliveryError) as error:self.deliver(values['bound_port'])
            self.assertEqual(error.exception.receiver_commit_status,'unknown');self.assertFalse(self.confirmation.exists())
            self.assertEqual(self.packet_file.read_bytes(),self.body)
