"""Synthetic response-time reconstruction, no actual endpoint or source claims."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime,timedelta,timezone
import io,json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

from sushiwait.cli import main
from sushiwait.remote import SOURCE,QUEUE_NAMES,RemoteResult,RemoteStore
from sushiwait.remotesignals import remote_signal_report
from sushiwait.signals import SignalError

BASE=datetime(2026,10,6,8,tzinfo=timezone.utc)
RUN='00000000-0000-0000-0000-000000000001'
def stamp(seconds):return (BASE+timedelta(seconds=seconds)).isoformat().replace('+00:00','Z')
def record(seconds,labels=(),count=0,*,store='900001',queue_error=False,count_error=False,count_received=None):
    queues={k:list(labels) if k=='storeQueue' else [] for k in QUEUE_NAMES}
    q=RemoteResult('groupqueues',True,not queue_error,None if queue_error else {'queues':queues},
        'http_error' if queue_error else None,503 if queue_error else 200,stamp(seconds),stamp(seconds+.1),100)
    c=(RemoteResult('storequeuecount',False,False,None,'preceding_query_failed',None,None,None,None)
        if queue_error else RemoteResult('storequeuecount',True,not count_error,
        None if count_error else {'raw_count':count,'unit':'unknown'},'http_error' if count_error else None,
        503 if count_error else 200,stamp(seconds+.2),stamp(seconds+.3 if count_received is None else count_received),100))
    return {'schema_version':1,'source':SOURCE,'data_origin':'live','requested_store_id':store,
        'response_store_identity_verified':False,'ok':q.ok and c.ok,'queries':{'groupqueues':asdict(q),'storequeuecount':asdict(c)},
        'atomic_snapshot':False,'source_update_time_verified':False,'source_freshness':'unknown',
        'eta_available':False,'complete_queue_cursor_available':False,'verified_training_labels':0}

class RemoteSignalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.parent=Path(self.tmp.name).resolve();self.parent.chmod(0o700)
        self.path=self.parent/'synthetic.sqlite3'
    def tearDown(self):self.tmp.cleanup()
    def write(self,items):
        with RemoteStore(self.path) as db:
            for item in items:
                value,run=item if isinstance(item,tuple) else (item,RUN)
                db.run_id=run;db.append(value)
    def report(self,*,at=120.3,**kwargs):
        with RemoteStore(self.path,read_only=True) as db:
            return remote_signal_report(db,'900001',as_of=stamp(at),**kwargs)
    def queue(self,result):return result['endpoints']['groupqueues']['queues']['storeQueue']
    def mutate(self,sql,params=()):
        db=sqlite3.connect(self.path);db.execute(sql,params);db.commit();db.close()

    def test_two_halves_measure_display_turnover_without_queue_event_attribution(self):
        self.write([record(t,[str(i)],10+i) for i,t in enumerate([0,20,40,60,80,100,120])])
        r=self.report(at=120.3,window_seconds=121);q=self.queue(r)
        self.assertEqual(q['whole']['comparable_pairs'],6)
        self.assertEqual(q['whole']['removed_labels'],6)
        self.assertEqual(q['rate_comparison']['status'],'available')
        self.assertEqual(q['rate_comparison']['direction'],'equal')
        self.assertIsNone(r['true_no_show_rate']);self.assertFalse(r['eta_available'])
        self.assertEqual(r['calendar_at_as_of']['date_type'],'holiday')
    def test_duplicates_suffixes_and_order_stay_opaque(self):
        self.write([record(0,['99','99','100-1']),record(30,['100-1','99','99']),record(60,['A1'])])
        q=self.queue(self.report(at=60.3))['whole']
        self.assertEqual((q['display_values_changed_pairs'],q['display_set_changed_pairs']),(2,1))
        self.assertEqual((q['removed_labels'],q['added_labels']),(2,1))
    def test_empty_arrays_have_observed_zero_but_no_records_have_null_rates(self):
        self.write([record(0),record(30)])
        q=self.queue(self.report(at=30.3))['whole'];self.assertEqual(q['removed_labels_per_observed_minute'],0)
        q=self.queue(self.report(at=-100))['whole'];self.assertIsNone(q['removed_labels_per_observed_minute'])
    def test_count_failure_does_not_break_valid_queue_stream(self):
        self.write([record(0,['1'],4),record(30,['2'],count_error=True),record(60,['3'],2)])
        r=self.report(at=60.3)
        self.assertEqual(self.queue(r)['whole']['comparable_pairs'],2)
        count=r['endpoints']['storequeuecount'];self.assertEqual(count['chain_breaks'],{'failed_response':1})
        self.assertEqual(count['reported_count']['comparable_pairs'],0)
        self.assertIsNone(count['reported_count']['sum_of_pair_deltas'])
    def test_queue_failure_and_skipped_count_break_their_own_streams(self):
        self.write([record(0,['1']),record(30,queue_error=True),record(60,['2'])])
        r=self.report(at=60.3)
        self.assertEqual(self.queue(r)['whole']['comparable_pairs'],0)
        self.assertEqual(r['endpoints']['groupqueues']['chain_breaks'],{'failed_response':1})
        self.assertEqual(r['endpoints']['storequeuecount']['chain_breaks'],{'skipped_query':1})
    def test_tail_count_failure_preserves_queue_response_state(self):
        self.write([record(0,['1']),record(30,['2'],count_error=True)])
        r=self.report(at=30.3)
        self.assertEqual(r['endpoints']['groupqueues']['availability'],'recent_responses')
        self.assertEqual(r['endpoints']['storequeuecount']['availability'],'interrupted')
    def test_request_runs_and_long_gaps_are_not_bridged(self):
        other='00000000-0000-0000-0000-000000000002'
        self.write([record(0,['1']),(record(30,['2']),other),(record(200,['3']),other)])
        r=self.report(at=200.3,window_seconds=300)
        self.assertEqual(self.queue(r)['whole']['comparable_pairs'],0)
        self.assertEqual(r['endpoints']['groupqueues']['chain_breaks'],{'run_changed':1,'sampling_gap':1})
    def test_reversed_and_repeated_times_do_not_double_count_coverage(self):
        self.write([record(0),record(30),record(20),record(30),record(60),record(90)])
        r=self.report(at=90.3)
        self.assertEqual(self.queue(r)['whole']['comparable_pairs'],2)
        self.assertEqual(self.queue(r)['whole']['observed_seconds'],60)
        self.assertEqual(r['endpoints']['groupqueues']['chain_breaks']['non_increasing_time'],2)
    def test_overlapping_endpoint_requests_are_not_comparable(self):
        first=record(0);first['queries']['groupqueues']['received_at']=stamp(20)
        first['queries']['storequeuecount'].update(started_at=stamp(20.1),received_at=stamp(20.2))
        second=record(15)
        second['queries']['groupqueues']['received_at']=stamp(25)
        second['queries']['storequeuecount'].update(started_at=stamp(25.1),received_at=stamp(25.2))
        self.write([first,second])
        r=self.report(at=25.3)
        self.assertEqual(self.queue(r)['whole']['comparable_pairs'],0)
        self.assertEqual(r['endpoints']['groupqueues']['chain_breaks'],{'overlapping_requests':1})
    def test_response_clocks_have_independent_coverage_and_count_deltas(self):
        self.write([record(0,['1'],10,count_received=2),record(30,['2'],8,count_received=35)])
        r=self.report(at=35.3)
        self.assertEqual(self.queue(r)['whole']['observed_seconds'],30)
        count=r['endpoints']['storequeuecount']['reported_count']
        self.assertEqual(count['observed_seconds'],33);self.assertEqual(count['sum_of_pair_deltas'],-2)
        self.assertEqual(count['unit'],'unknown')
    def test_entire_pair_excluded_until_later_count_response(self):
        self.write([record(0,['1']),record(30,['2'],count_received=70)])
        before=self.report(at=60);after=self.report(at=70)
        self.assertEqual(before['scan']['future_pair_rows_excluded'],1)
        self.assertEqual(self.queue(before)['whole']['comparable_pairs'],0)
        self.assertEqual(self.queue(after)['whole']['comparable_pairs'],1)
        self.assertFalse(after['historical_availability_verified'])
    def test_future_failure_does_not_interrupt_current_history(self):
        self.write([record(0,['1']),record(30,['2'])]);before=self.report(at=30.3)
        self.write([record(90,queue_error=True)]);after=self.report(at=30.3)
        self.assertEqual(before['endpoints'],after['endpoints'])
        self.assertEqual(after['scan']['future_pair_rows_excluded'],1)
    def test_bounded_scan_does_not_claim_complete_past_when_future_rows_occupy_limit(self):
        self.write([record(0),record(30),record(90),record(120)])
        r=self.report(at=30.3,sample_limit=2)
        self.assertTrue(r['scan']['truncated'])
        self.assertEqual(r['endpoints']['groupqueues']['availability'],'history_scan_incomplete')
        self.assertEqual(self.queue(r)['rate_comparison']['status'],'window_not_current')
    def test_midpoint_crossing_is_in_whole_but_not_half_intervals(self):
        self.write([record(0,['1']),record(50,['2']),record(70,['3']),record(120,['4'])])
        q=self.queue(self.report(at=120.1))
        self.assertEqual(q['whole']['comparable_pairs'],2)
        self.assertEqual(q['cross_midpoint_pairs'],1)
        self.assertEqual(q['previous_half']['comparable_pairs'],1)
    def test_stale_and_zero_baselines_do_not_invent_acceleration(self):
        self.write([record(t) for t in [0,20,40,60,80,100,120]])
        q=self.queue(self.report(at=120.3,window_seconds=121))
        self.assertEqual(q['rate_comparison']['status'],'previous_zero');self.assertIsNone(q['rate_comparison']['removed_label_rate_ratio'])
        r=self.report(at=220,window_seconds=300)
        self.assertEqual(r['endpoints']['groupqueues']['availability'],'stale_responses')
        self.assertEqual(self.queue(r)['rate_comparison']['status'],'window_not_current')
    def test_other_store_is_not_merged_and_no_labels_or_paths_are_output(self):
        self.write([record(0,['A7654321']),record(30,['1']),record(30,['2'],store='900002')])
        r=self.report(at=30.3);text=json.dumps(r)
        self.assertEqual(r['scan']['total_store_rows'],2)
        self.assertNotIn('A7654321',text);self.assertNotIn(str(self.path),text)
    def test_corrupt_payload_or_identity_breaks_comparison_without_echo(self):
        self.write([record(0),record(30),record(60)])
        self.mutate("UPDATE remote_samples SET payload_json=? WHERE id=2",('private_bad_payload',))
        r=self.report(at=60.3)
        self.assertEqual(r['scan']['invalid_records'],1);self.assertEqual(self.queue(r)['whole']['comparable_pairs'],0)
        self.assertNotIn('private_bad_payload',json.dumps(r))
    def test_invalid_tail_and_oversize_run_are_unknown_not_empty(self):
        self.write([record(0),record(30)])
        self.mutate('UPDATE remote_samples SET run_id=? WHERE id=2',('x'*1000,))
        r=self.report(at=30.3);self.assertEqual(r['endpoints']['groupqueues']['availability'],'unknown')
        self.assertIsNone(r['true_no_show_rate'])
    def test_unknown_year_retains_calendar_uncertainty(self):
        self.write([record(0)])
        with RemoteStore(self.path,read_only=True) as db:
            r=remote_signal_report(db,'900001',as_of='2027-01-01T00:00:00Z')
        self.assertEqual(r['calendar_at_as_of']['date_type'],'unknown')
    def test_invalid_settings_and_existing_transactions_are_left_unchanged(self):
        self.write([record(0)])
        with RemoteStore(self.path,read_only=True) as db:
            for key,value in [('window_seconds',29),('max_gap_seconds',0),('sample_limit',10001),('as_of','bad')]:
                with self.assertRaises(SignalError):remote_signal_report(db,'900001',**{'as_of':stamp(0),key:value})
                self.assertFalse(db.db.in_transaction)
            db.db.execute('BEGIN')
            with self.assertRaises(SignalError):remote_signal_report(db,'900001',as_of=stamp(0))
            self.assertTrue(db.db.in_transaction);db.db.rollback()
        with RemoteStore(self.path) as db:
            with self.assertRaises(SignalError):remote_signal_report(db,'900001',as_of=stamp(0))
    def test_read_transaction_ends_when_sql_fails(self):
        self.write([record(0)])
        with RemoteStore(self.path,read_only=True) as db:
            original=db.db
            class Broken:
                def __getattr__(self,k):return getattr(original,k)
                def execute(self,sql,args=()):
                    if sql.startswith('SELECT'):raise sqlite3.OperationalError('private_sql_error')
                    return original.execute(sql,args)
            db.db=Broken()
            with self.assertRaises(sqlite3.Error):remote_signal_report(db,'900001',as_of=stamp(0))
            self.assertFalse(original.in_transaction);db.db=original
    def test_cli_read_only_and_zero_network_credentials_or_client(self):
        self.write([record(0,['1']),record(30,['2'])]);before=self.path.read_bytes()
        with patch('socket.socket',side_effect=AssertionError('network')) as sock, \
                patch('sushiwait.remote.RemoteClient',side_effect=AssertionError('client')) as client, \
                patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('credentials')) as auth, \
                patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(main(['remote-signal-report','--db',str(self.path),'--store-id','900001','--as-of',stamp(30.3)]),0)
            self.assertEqual(main(['remote-signal-report','--db',str(self.path),'--store-id','private_id','--as-of','private_time']),2)
        self.assertEqual((sock.call_count,client.call_count,auth.call_count),(0,0,0))
        self.assertNotIn(str(self.path),output.getvalue());self.assertNotIn('private_id',output.getvalue())
        self.assertNotIn('private_time',output.getvalue());self.assertEqual(before,self.path.read_bytes())

if __name__=='__main__':unittest.main()
