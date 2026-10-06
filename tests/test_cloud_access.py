"""The cloud probe is bounded and reveals aggregates even on client or source failure."""
import importlib.util,json,unittest
from pathlib import Path
from unittest.mock import patch
from sushiwait.remote import RemoteClient,QUEUE_NAMES

spec=importlib.util.spec_from_file_location('cloud_check',Path(__file__).resolve().parents[1]/'scripts/check_cloud_access.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class Response:
    headers={}
    def __init__(self,request,status):self.request,self.status=request,status
    def geturl(self):return self.request.full_url
    def getcode(self):return self.status
    def close(self):pass
    def read(self,size):
        return json.dumps({name:['777777'] for name in QUEUE_NAMES}
            if 'groupqueues?' in self.request.full_url else 918273).encode()[:size]
class Opener:
    def __init__(self,statuses):self.statuses=statuses;self.calls=[]
    def open(self,request,*,timeout):
        self.calls.append(request)
        return Response(request,self.statuses[len(self.calls)-1])

class CloudCheckTests(unittest.TestCase):
    def run_probe(self,statuses):
        opener=Opener(statuses)
        with patch('socket.socket',side_effect=AssertionError('network')) as socket, \
             patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('auth')) as auth:
            result=module.run_check(client_factory=lambda:RemoteClient(opener=opener),system=lambda:'Linux')
        self.assertEqual((socket.call_count,auth.call_count),(0,0))
        self.assertNotIn('777777',json.dumps(result));self.assertNotIn('918273',json.dumps(result))
        return result,opener

    def test_three_pilots_one_round_six_fake_requests_and_no_raw_output(self):
        result,opener=self.run_probe([200]*6)
        self.assertTrue(result['ok']);self.assertEqual(result['http_attempts'],6)
        self.assertEqual(result['checked_pairs'],3)
        self.assertEqual([request.full_url.rsplit('=',1)[1] for request in opener.calls],
            ['3014','3014','3004','3004','2009','2009'])
        self.assertFalse(result['production_deployment_verified'])
        self.assertFalse(result['continuous_collection_verified'])
        self.assertFalse(result['raw_observations_retained'])
        self.assertEqual(result['verified_training_labels'],0)

    def test_first_queue_failure_skips_count_and_other_stores(self):
        result,opener=self.run_probe([503])
        self.assertFalse(result['ok']);self.assertTrue(result['stopped_on_failure'])
        self.assertEqual((result['http_attempts'],result['checked_pairs'],len(opener.calls)),(1,1,1))

    def test_count_failure_stops_without_next_store_or_retry(self):
        result,opener=self.run_probe([200,429])
        self.assertEqual((result['attempted_pairs'],result['http_attempts'],len(opener.calls)),(1,2,2))
        self.assertFalse(result['ok']);self.assertEqual(result['retries'],0)

    def test_later_failure_preserves_previous_aggregates(self):
        result,opener=self.run_probe([200,200,503])
        self.assertEqual((result['successful_pairs'],result['checked_pairs'],result['http_attempts']),(1,2,3))
        self.assertFalse(result['ok'])

    def test_non_linux_fails_before_client_database_or_network(self):
        with patch('socket.socket',side_effect=AssertionError('network')) as socket:
            def forbidden():raise AssertionError('client')
            result=module.run_check(client_factory=forbidden,system=lambda:'Darwin')
        self.assertFalse(result['ok']);self.assertEqual(result['http_attempts'],0)
        self.assertEqual(socket.call_count,0)

    def test_client_exception_marks_unrecorded_attempts_unknown_without_exception_text(self):
        class BrokenClient:
            def snapshot(self,store):raise OSError('PRIVATE_PATH_AND_QUEUE_VALUE')
        result=module.run_check(client_factory=BrokenClient,system=lambda:'Linux')
        self.assertEqual((result['http_attempts'],result['checked_pairs']),(0,0))
        self.assertEqual(result['error_code'],'canary_record_or_client_error')
        self.assertFalse(result['request_accounting_complete'])
        self.assertEqual(result['unrecorded_http_attempts'],'unknown')
        self.assertNotIn('PRIVATE_PATH_AND_QUEUE_VALUE',json.dumps(result))

    def test_malformed_record_stops_and_does_not_assert_zero_actual_network(self):
        class BrokenClient:
            def snapshot(self,store):return {'private':'777777'}
        result=module.run_check(client_factory=BrokenClient,system=lambda:'Linux')
        self.assertFalse(result['ok']);self.assertFalse(result['request_accounting_complete'])
        self.assertEqual(result['unrecorded_http_attempts'],'unknown')
        self.assertNotIn('777777',json.dumps(result))

if __name__=='__main__':unittest.main()
