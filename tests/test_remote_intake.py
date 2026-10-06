"""Late intake, rollback and legacy gaps without actual queries or labels."""
from copy import deepcopy
from datetime import timedelta,timezone
import io,json,sqlite3,unittest
from unittest.mock import patch

import test_remote_signals as fixtures
from sushiwait.cli import main
from sushiwait.remote import RemoteStore
from sushiwait.remoteintake import read_receipt
from sushiwait.remotesignals import remote_signal_report
from sushiwait.signals import SignalError


record,BASE,RUN,stamp=fixtures.record,fixtures.BASE,fixtures.RUN,fixtures.stamp

class AnonymousIntakeTests(unittest.TestCase):
    setUp=fixtures.RemoteSignalTests.setUp
    tearDown=fixtures.RemoteSignalTests.tearDown
    report=fixtures.RemoteSignalTests.report
    queue=fixtures.RemoteSignalTests.queue
    mutate=fixtures.RemoteSignalTests.mutate
    def append(self, value, received, *, run=RUN):
        with patch('sushiwait.remoteintake._clock',return_value=BASE+timedelta(seconds=received)), \
                RemoteStore(self.path) as db:
            db.run_id=run;db.append(value)

    def local_report(self,at=120.3,**kwargs):
        with RemoteStore(self.path,read_only=True) as db:
            return remote_signal_report(db,'900001',as_of=stamp(at),availability_basis='local-first-receipt',**kwargs)

    def raw(self):
        with RemoteStore(self.path,read_only=True) as db:
            return [(run,json.loads(body)) for run,body in db.db.execute('SELECT run_id,payload_json FROM remote_samples ORDER BY id')]

    def legacy(self,value):
        with RemoteStore(self.path) as db:
            db.db.execute('INSERT INTO remote_samples(run_id,store_id,ok,payload_json) VALUES(?,?,?,?)',
                (RUN,value['requested_store_id'],int(value['ok']),json.dumps(value)))
            db.db.commit()

    def test_writer_assigns_receipt_and_ignores_supplied_backdate(self):
        value=record(0,['1']);value['local_intake']={'received_at':stamp(-100),'schema_version':99}
        self.append(value,60)
        run,stored=self.raw()[0]
        self.assertEqual(read_receipt(stored,{k:v for k,v in value.items() if k!='local_intake'},run),BASE+timedelta(seconds=60))
        self.assertEqual(self.local_report(at=59)['scan'].get('admitted_pair_rows',0),0)
        self.assertEqual(self.local_report(at=60)['scan']['admitted_pair_rows'],1)

    def test_late_second_pair_is_excluded_at_original_response_time(self):
        self.append(record(0,['1']),1);self.append(record(30,['2']),90)
        before=self.local_report(at=60);after=self.local_report(at=90)
        self.assertEqual(self.queue(before)['whole']['comparable_pairs'],0)
        self.assertEqual(before['scan']['future_local_receipts_excluded'],1)
        self.assertEqual(self.queue(after)['whole']['comparable_pairs'],1)
        self.assertEqual(self.report(at=60)['scan']['admitted_pair_rows'],2)

    def test_old_rows_are_not_backfilled_or_admitted_by_local_policy(self):
        self.legacy(record(0,['1']));before=self.path.read_bytes()
        r=self.local_report(at=30)
        self.assertEqual(r['scan']['local_receipt_missing_rows'],1)
        self.assertEqual(r['endpoints']['groupqueues']['availability'],'unknown')
        self.assertEqual(before,self.path.read_bytes());self.assertNotIn('local_intake',self.raw()[0][1])

    def test_mixed_legacy_gap_is_not_bridged(self):
        self.append(record(0,['1']),1);self.legacy(record(30,['2']));self.append(record(60,['3']),61)
        r=self.local_report(at=61)
        self.assertEqual(self.queue(r)['whole']['comparable_pairs'],0)
        self.assertEqual(r['endpoints']['groupqueues']['chain_breaks'],{'local_receipt_missing':1})

    def test_failed_count_uses_same_receipt_but_keeps_endpoint_independence(self):
        self.append(record(0,['1']),1);self.append(record(30,['2'],count_error=True),90)
        before=self.local_report(at=60);after=self.local_report(at=90)
        self.assertEqual(before['endpoints']['storequeuecount']['availability'],'recent_responses')
        self.assertEqual(after['endpoints']['storequeuecount']['availability'],'interrupted')
        self.assertEqual(self.queue(after)['whole']['comparable_pairs'],1)

    def test_receipt_before_pair_end_rolls_back(self):
        self.append(record(0),1);before=self.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'clock_order'):self.append(record(30,count_received=90),60)
        self.assertEqual(before,self.path.read_bytes())

    def test_receipt_clock_rollback_stops_even_after_reopen_and_new_run(self):
        self.append(record(0),90);before=self.path.read_bytes()
        other='00000000-0000-0000-0000-000000000002'
        with self.assertRaisesRegex(ValueError,'clock_order'):self.append(record(30),60,run=other)
        self.assertEqual(before,self.path.read_bytes())

    def test_equal_intake_times_keep_rows_and_run_boundaries_separate(self):
        self.append(record(0),90);self.append(record(30),90,run='00000000-0000-0000-0000-000000000002')
        r=self.local_report(at=90)
        self.assertEqual(r['scan']['admitted_pair_rows'],2)
        self.assertEqual(r['endpoints']['groupqueues']['chain_breaks'],{'run_changed':1})

    def test_changed_payload_receipt_digest_is_rejected_without_echo(self):
        self.append(record(0,['1']),1);self.append(record(30,['2']),31)
        run,stored=self.raw()[1];stored['queries']['groupqueues']['payload']['queues']['storeQueue']=['9876543']
        self.mutate('UPDATE remote_samples SET payload_json=? WHERE id=2',(json.dumps(stored),))
        r=self.local_report(at=31)
        self.assertEqual(r['scan']['invalid_records'],1);self.assertNotIn('9876543',json.dumps(r))
        before=self.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'receipt_invalid'):self.append(record(60),61)
        self.assertEqual(before,self.path.read_bytes())

    def test_metadata_run_is_bound_and_invalid_metadata_is_not_legacy(self):
        self.append(record(0),1)
        self.mutate('UPDATE remote_samples SET run_id=?',('00000000-0000-0000-0000-000000000002',))
        self.assertEqual(self.local_report(at=1)['scan']['invalid_records'],1)
        run,stored=self.raw()[0];stored['local_intake']=None
        self.mutate('UPDATE remote_samples SET payload_json=?',(json.dumps(stored),))
        self.assertEqual(self.local_report(at=1)['scan']['invalid_records'],1)

    def test_receipt_schema_unknown_keys_and_bad_time_are_rejected(self):
        self.append(record(0),1);run,original=self.raw()[0]
        for key,value in [('schema_version',True),('received_at',stamp(1)),('record_sha256','f'*64),('unknown','private')]:
            stored=deepcopy(original);stored['local_intake'][key]=value
            self.mutate('UPDATE remote_samples SET payload_json=?',(json.dumps(stored),))
            self.assertEqual(self.local_report(at=2)['scan']['invalid_records'],1)

    def test_future_receipts_occupying_limit_make_history_incomplete(self):
        for response,receipt in [(0,1),(30,31),(60,120),(90,150)]:self.append(record(response),receipt)
        r=self.local_report(at=100,sample_limit=2)
        self.assertEqual(r['endpoints']['groupqueues']['availability'],'history_scan_incomplete')
        self.assertEqual(r['scan']['future_local_receipts_excluded'],2)

    def test_future_receipt_failure_does_not_change_earlier_stream(self):
        self.append(record(0),1);self.append(record(30),31);before=self.local_report(at=60)
        self.append(record(45,queue_error=True),90);after=self.local_report(at=60)
        self.assertEqual(before['endpoints'],after['endpoints'])

    def test_intake_delay_does_not_replace_response_spacing_with_receipt_spacing(self):
        self.append(record(0,['1'],10,count_received=2),80);self.append(record(30,['2'],8,count_received=35),90)
        r=self.local_report(at=90)
        self.assertEqual(self.queue(r)['whole']['observed_seconds'],30)
        self.assertEqual(r['endpoints']['storequeuecount']['reported_count']['observed_seconds'],33)

    def test_read_only_local_cli_has_no_network_and_preserves_database(self):
        self.append(record(0,['7654321']),1);before=self.path.read_bytes()
        with patch('socket.socket',side_effect=AssertionError('network')) as sock, \
                patch('sushiwait.remote.RemoteClient',side_effect=AssertionError('client')) as client, \
                patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('auth')) as auth, \
                patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(main(['remote-signal-report','--db',str(self.path),'--store-id','900001',
                '--as-of',stamp(1),'--availability-basis','local-first-receipt']),0)
        r=json.loads(output.getvalue());self.assertEqual(r['availability_basis'],'local-first-receipt')
        self.assertEqual((sock.call_count,client.call_count,auth.call_count),(0,0,0))
        self.assertEqual(before,self.path.read_bytes());self.assertNotIn('7654321',output.getvalue())
        self.assertFalse(r['historical_availability_verified']);self.assertFalse(r['durable_availability_verified'])
        self.assertFalse(r['independent_time_attestation']);self.assertFalse(r['eta_available'])

    def test_bad_basis_does_not_start_transaction(self):
        self.append(record(0),1)
        with RemoteStore(self.path,read_only=True) as db:
            with self.assertRaises(SignalError):remote_signal_report(db,'900001',as_of=stamp(1),availability_basis='invented')
            self.assertFalse(db.db.in_transaction)

    def test_naive_or_bad_writer_clock_stops_before_insert(self):
        self.append(record(0),1);before=self.path.read_bytes()
        for clock in (BASE.replace(tzinfo=None),'private_bad_clock'):
            with patch('sushiwait.remoteintake._clock',return_value=clock),RemoteStore(self.path) as db:
                with self.assertRaisesRegex(ValueError,'clock_invalid'):db.append(record(30))
                self.assertFalse(db.db.in_transaction)
            self.assertEqual(before,self.path.read_bytes())

    def test_timezone_is_canonical_and_private_receipt_not_in_public_record(self):
        received=(BASE+timedelta(seconds=1)).astimezone(timezone(timedelta(hours=8)))
        with patch('sushiwait.remoteintake._clock',return_value=received),RemoteStore(self.path) as db:
            db.run_id=RUN;db.append(record(0));r=db.report('900001')
        self.assertNotIn('local_intake',r['latest_record'])
        self.assertEqual(self.raw()[0][1]['local_intake']['received_at'],stamp(1).replace('01Z','01.000000Z'))

    def test_real_write_keeps_legacy_rows_byte_identical(self):
        self.legacy(record(0))
        old=self.raw()[0]
        self.append(record(30),31)
        self.assertEqual(old,self.raw()[0])
        self.assertNotIn('local_intake',self.raw()[0][1]);self.assertIn('local_intake',self.raw()[1][1])

    def test_clock_rollback_cannot_hide_behind_intervening_legacy_row(self):
        self.append(record(0),90);self.legacy(record(30));before=self.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'clock_order'):self.append(record(45),60)
        self.assertEqual(before,self.path.read_bytes())


if __name__=='__main__':unittest.main()
