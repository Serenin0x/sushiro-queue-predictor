from copy import deepcopy
from datetime import datetime,timezone
import contextlib,io
import hashlib,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

from sushiwait import pending as p
from sushiwait.observations import normalize_snapshot
from sushiwait.packets import build_packet,encoded


class PendingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.root.chmod(0o700)
        self.queue=self.root/'queue';self.queue.mkdir(mode=0o700)
        self.source=self.root/'source.json';self.at='2026-10-06T01:00:00.000000Z'
        fixture=json.loads((Path(__file__).resolve().parents[1]/'examples/fixtures/store-detail-01.synthetic.json').read_text())
        body=normalize_snapshot(fixture['payload'],'900001',request_started_at=fixture['observed_at'],
            received_at=fixture['observed_at'],elapsed_ms=0,data_origin='synthetic',api_profile='legacy')
        row={'id':1,'run_id':'764c40ca-8f27-41b7-8026-28d046d526cb','store_id':'900001',
            'api_profile':'legacy','data_origin':'synthetic','received_at':fixture['observed_at'],
            'ok':1,'payload_json':json.dumps(body)}
        self.packet=build_packet([row],as_of=self.at,store_ids=['900001'],api_profile='legacy',data_origin='synthetic')
        self.write(self.packet)

    def write(self,packet):
        self.source.write_bytes(encoded(packet));self.source.chmod(0o600)

    def enqueue(self):return p.enqueue_packet(self.source,self.queue,as_of=self.at)
    def status(self):return p.pending_status(self.queue,as_of=self.at)

    def test_real_persistence_reopen_and_repeat_preserve_source_and_file(self):
        before=self.source.read_bytes();first=self.enqueue();target=next(self.queue.iterdir())
        identity=(target.stat().st_ino,target.read_bytes(),target.stat().st_mtime_ns)
        second=self.enqueue();self.assertFalse(first['already_queued']);self.assertTrue(second['already_queued'])
        self.assertTrue(first['committed']);self.assertTrue(second['durability_confirmed'])
        self.assertEqual(identity,(target.stat().st_ino,target.read_bytes(),target.stat().st_mtime_ns))
        self.assertEqual(before,self.source.read_bytes());self.assertEqual(self.status()['pending_packets'],1)
        self.assertFalse(second['server_received']);self.assertFalse(second['source_claims_verified'])
        self.assertEqual(second['verified_training_labels'],0)

    def test_status_empty_and_readonly_counts_occurrences_not_unique_observations(self):
        self.assertEqual(self.status()['pending_packets'],0)
        self.enqueue();changed=deepcopy(self.packet);changed['as_of']='2026-10-06T00:59:59.000000Z';self.write(changed);self.enqueue()
        before={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in self.queue.iterdir()}
        report=self.status();self.assertEqual(report['pending_packets'],2)
        self.assertEqual(report['pending_record_occurrences'],2);self.assertEqual(report['distinct_scopes'],1)
        self.assertEqual(before,{f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in self.queue.iterdir()})

    def test_scope_origins_remain_separate(self):
        self.enqueue();changed=deepcopy(self.packet);changed['data_origin']='fixture'
        record=changed['records'][0];record['data_origin']='fixture'
        record['record_sha256']=hashlib.sha256(encoded({k:v for k,v in record.items() if k!='record_sha256'})).hexdigest()
        self.write(changed);self.enqueue();self.assertEqual(self.status()['distinct_scopes'],2)

    def test_invalid_and_empty_input_create_no_pending_file(self):
        for packet in ({'secret':'VALUE'},dict(self.packet,records=[])):
            self.write(packet)
            with self.assertRaises(p.PendingError):self.enqueue()
            self.assertEqual(list(self.queue.iterdir()),[])

    def test_nonprivate_source_and_spool_are_rejected(self):
        self.source.chmod(0o644)
        with self.assertRaises(p.PendingError):self.enqueue()
        self.source.chmod(0o600);self.queue.chmod(0o755)
        with self.assertRaises(p.PendingError):self.enqueue()

    def test_unknown_files_or_interrupted_temporary_require_review_without_deletion(self):
        for name in ('foreign.txt','.sushiwait-packet-interrupted.tmp'):
            target=self.queue/name;target.write_bytes(b'KEEP');target.chmod(0o600)
            with self.assertRaises(p.PendingError) as failure:self.enqueue()
            self.assertEqual(failure.exception.error_code,'pending_directory_requires_review')
            self.assertEqual(target.read_bytes(),b'KEEP');target.unlink()

    def test_capacity_is_checked_under_publication_lock_and_duplicates_still_work(self):
        with patch.object(p,'MAX_PENDING_PACKETS',1):
            self.enqueue();self.assertTrue(self.enqueue()['already_queued'])
            changed=deepcopy(self.packet);changed['as_of']='2026-10-06T00:59:59.000000Z';self.write(changed)
            with self.assertRaises(p.PendingError) as failure:self.enqueue()
            self.assertEqual(failure.exception.error_code,'pending_capacity_exceeded')
            self.assertEqual(len(list(self.queue.iterdir())),1)

    def test_existing_corrupt_or_valid_different_content_is_never_overwritten(self):
        self.enqueue();target=next(self.queue.iterdir())
        changed=deepcopy(self.packet);changed['as_of']='2026-10-06T00:59:59.000000Z';target.write_bytes(encoded(changed))
        before=target.read_bytes()
        with self.assertRaises(p.PendingError) as failure:self.enqueue()
        self.assertEqual(failure.exception.error_code,'pending_content_conflict')
        self.assertEqual(before,target.read_bytes())
        with self.assertRaises(p.PendingError):self.status()

    def test_symlink_and_hardlink_entries_rejected(self):
        self.enqueue();target=next(self.queue.iterdir());extra=self.root/'extra.json';os.link(target,extra)
        with self.assertRaises(p.PendingError):self.status()
        extra.unlink();data=target.read_bytes();target.unlink();self.source.write_bytes(data);target.symlink_to(self.source)
        with self.assertRaises(p.PendingError):self.status()
        with self.assertRaises(p.PendingError):self.enqueue()

    def test_cooperative_lock_blocks_new_publish_without_mutation(self):
        import fcntl
        fd=os.open(self.queue,os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(p.PendingError):self.enqueue()
            with self.assertRaises(p.PendingError):self.status()
            self.assertEqual(list(self.queue.iterdir()),[])
        finally:os.close(fd)

    def test_duplicate_sync_failure_reports_existing_commit_and_preserves_file(self):
        self.enqueue();target=next(self.queue.iterdir());before=target.read_bytes()
        with patch('sushiwait.pending.os.fsync',side_effect=OSError('disk')):
            with self.assertRaises(p.PendingError) as failure:self.enqueue()
        self.assertTrue(failure.exception.committed);self.assertEqual(before,target.read_bytes())

    def test_no_network_credentials_or_child_and_no_raw_payload_in_summary(self):
        with patch('socket.socket',side_effect=AssertionError('network')) as sock, patch('subprocess.Popen',side_effect=AssertionError('child')) as child, patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('auth')) as auth:
            result=self.enqueue();status=self.status()
            self.assertEqual((sock.call_count,child.call_count,auth.call_count),(0,0,0))
        self.assertNotIn('records',result);self.assertNotIn('display',status);self.assertNotIn('directory',status)

    def test_cli_enqueue_and_status_emit_only_aggregate_without_access(self):
        from sushiwait.cli import main
        results=[]
        with patch('sushiwait.cli._utc_clock',return_value=datetime(2026,10,6,1,tzinfo=timezone.utc)), patch('socket.socket',side_effect=AssertionError('network')) as sock, patch('subprocess.Popen',side_effect=AssertionError('child')) as child, patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('auth')) as auth:
            for args in (['packet-enqueue','--input',str(self.source),'--directory',str(self.queue)],['pending-status','--directory',str(self.queue)]):
                out=io.StringIO()
                with contextlib.redirect_stdout(out):self.assertEqual(main(args),0)
                results.append(json.loads(out.getvalue()))
            self.assertEqual((sock.call_count,child.call_count,auth.call_count),(0,0,0))
        self.assertTrue(all(item['ok'] for item in results));self.assertEqual(results[1]['pending_packets'],1)
        self.assertFalse(results[0]['server_received']);self.assertNotIn(str(self.queue),str(results))

    def test_cli_invalid_source_is_safe_failure_and_does_not_queue(self):
        from sushiwait.cli import main
        self.source.write_text('{"secret":"VALUE"}')
        out=io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(['packet-enqueue','--input',str(self.source),'--directory',str(self.queue)]),1)
        value=json.loads(out.getvalue());self.assertFalse(value['ok']);self.assertFalse(value['committed'])
        self.assertNotIn('VALUE',out.getvalue());self.assertEqual(list(self.queue.iterdir()),[])


if __name__=='__main__':unittest.main()
