from datetime import datetime,timedelta,timezone
import unittest
import tempfile
from pathlib import Path
import os, json, contextlib, io
from unittest.mock import patch
from sushiwait.cli import main
from copy import deepcopy
import sushiwait.evaluation as e

class IntervalArithmeticTests(unittest.TestCase):
    def row(self,point=15,p=12,q=18,l=10,u=20,**changes):
        def t(minutes):return (datetime(2020,1,1,tzinfo=timezone.utc)+timedelta(minutes=minutes)).isoformat()
        return {'prediction_id':'00000000-0000-4000-8000-000000000001','prediction_made_at':t(0),'point_call_at':t(point),'predicted_call_lower':t(p),'predicted_call_upper':t(q),'observed_call_lower':t(l),'observed_call_upper':t(u),'observation_received_at':t(max(l,u)),**changes}
    def report(self,rows):return e.evaluate_intervals(rows,as_of='2020-01-02T00:00:00Z',data_origin='synthetic')
    def test_uncertain_label_keeps_error_bounds(self):
        r=self.report([self.row()]);self.assertEqual(r['mean_absolute_error_seconds'],{'lower':0,'upper':300});self.assertEqual(r['coverage'],{'confirmed_labels':0,'possible_labels':1,'lower':0,'upper':1})
    def test_exact_call_has_exact_error(self):
        r=self.report([self.row(point=12,p=8,q=14,l=10,u=10)]);self.assertEqual(r['mean_absolute_error_seconds'],{'lower':120,'upper':120});self.assertEqual(r['exact_labels'],1);self.assertEqual(r['coverage']['lower'],1)
    def test_prediction_before_observed_interval(self):
        self.assertEqual(self.report([self.row(point=5,p=0,q=8)])['mean_absolute_error_seconds'],{'lower':300,'upper':900})
    def test_prediction_after_observed_interval(self):
        self.assertEqual(self.report([self.row(point=25,p=22,q=30)])['mean_absolute_error_seconds'],{'lower':300,'upper':900})
    def test_full_containment_confirms_coverage(self):
        r=self.report([self.row(p=5,q=25)]);self.assertEqual(r['coverage']['lower'],1);self.assertEqual(r['mean_prediction_interval_width_seconds'],1200);self.assertEqual(r['mean_observed_interval_width_seconds'],600)
    def test_no_intersection_is_uncovered(self):
        r=self.report([self.row(point=25,p=22,q=30)]);self.assertEqual(r['coverage']['upper'],0)
    def test_touching_endpoint_is_only_possible(self):
        r=self.report([self.row(point=15,p=10,q=20,l=20,u=25)]);self.assertEqual(r['coverage']['lower'],0);self.assertEqual(r['coverage']['upper'],1)
    def test_single_point_prediction_does_not_confirm_uncertain_label(self):
        r=self.report([self.row(point=15,p=15,q=15)]);self.assertEqual(r['coverage']['lower'],0);self.assertEqual(r['coverage']['upper'],1)
    def test_average_with_distinct_predictions(self):
        a=self.row();b=self.row(point=12,p=8,q=14,l=10,u=10,prediction_id='00000000-0000-4000-8000-000000000002')
        r=self.report([a,b]);self.assertEqual(r['mean_absolute_error_seconds'],{'lower':60,'upper':210});self.assertEqual(r['coverage']['lower'],.5);self.assertEqual(r['coverage']['upper'],1)
    def test_empty_has_no_score(self):
        r=self.report([]);self.assertEqual(r['mean_absolute_error_seconds'],{'lower':None,'upper':None});self.assertIsNone(r['coverage']['upper'])
    def test_duplicate_prediction_is_rejected(self):
        with self.assertRaises(e.EvaluationError):self.report([self.row(),self.row()])
    def test_retroactive_claim_is_rejected(self):
        with self.assertRaises(e.EvaluationError):self.report([self.row(prediction_made_at='2020-01-01T00:11:00Z')])
    def test_unreceived_result_is_rejected(self):
        with self.assertRaises(e.EvaluationError):self.report([self.row(observation_received_at='2020-01-03T00:00:00Z')])
    def test_reverse_result_and_prediction_intervals_are_rejected(self):
        for row in [self.row(l=21,u=20),self.row(p=19,q=18),self.row(point=22)]:
            with self.assertRaises(e.EvaluationError):self.report([row])
    def test_missing_timezone_is_rejected(self):
        with self.assertRaises(e.EvaluationError):self.report([self.row(point_call_at='2020-01-01T00:15:00')])
    def test_unknown_private_fields_are_refused(self):
        with self.assertRaises(e.EvaluationError):self.report([self.row(authorization='do-not-output-this')])
    def test_origin_and_bounds_are_not_inferred(self):
        for origin in ['live',None,True,[]]:
            with self.assertRaises(e.EvaluationError):e.evaluate_intervals([],as_of='2020-01-02T00:00:00Z',data_origin=origin)
        with self.assertRaises(e.EvaluationError):self.report(tuple())
    def test_inputs_unchanged_and_no_personal_keys_in_aggregate(self):
        a=self.row();b=deepcopy(a);r=self.report([a]);self.assertEqual(a,b);self.assertNotIn('prediction_id',str(r));self.assertNotIn('2020-',str(r));self.assertFalse(r['model_performance_verified']);self.assertEqual(r['verified_training_labels'],0)
    def test_equivalent_timezones_score_identically(self):
        a=self.row();b=self.row(point_call_at='2020-01-01T08:15:00+08:00');self.assertEqual(self.report([a]),self.report([b]))
    def test_microsecond_precision_is_preserved(self):
        a=self.row(point=10,p=10,q=10,l=10,u=10,observed_call_lower='2020-01-01T00:10:00.000001Z',observed_call_upper='2020-01-01T00:10:00.000003Z',observation_received_at='2020-01-01T00:10:00.000003Z')
        self.assertEqual(self.report([a])['mean_absolute_error_seconds'],{'lower':.000001,'upper':.000003})

class EvaluationPrivateInputTests(unittest.TestCase):
    row = IntervalArithmeticTests.row
    report = IntervalArithmeticTests.report

    def document(self):
        return {'schema_version':1,'as_of':'2020-01-02T00:00:00Z','data_origin':'synthetic','records':[self.row()]}

    def invoke(self, body, *, file_mode=0o600, directory_mode=0o700):
        with tempfile.TemporaryDirectory() as folder:
            folder=str(Path(folder).resolve());os.chmod(folder,directory_mode);p=Path(folder)/'input.json'
            p.write_bytes(body);p.chmod(file_mode);out=io.StringIO()
            with patch('socket.socket',side_effect=AssertionError('no_network')), patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('no_query_credentials')), patch('sushiwait.cli.client_for',side_effect=AssertionError('no_client')), patch('sushiwait.surgeguard._command',side_effect=AssertionError('no_native')), patch('subprocess.Popen',side_effect=AssertionError('no_child')), contextlib.redirect_stdout(out):
                before=p.read_bytes();result=main(['interval-evaluate','--input',str(p)]);self.assertEqual(p.read_bytes(),before)
            return result,json.loads(out.getvalue())

    def test_private_cli_returns_aggregate_arithmetic_only(self):
        code,r=self.invoke(json.dumps(self.document()).encode());self.assertEqual(code,0);self.assertEqual(r['mean_absolute_error_seconds'],{'lower':0,'upper':300});self.assertFalse(r['model_performance_verified']);self.assertTrue(r['output_requires_private_handling']);self.assertNotIn('2020-',str(r))

    def test_self_reported_math_does_not_certify_truth(self):
        d=self.document();d['data_origin']='self_reported';code,r=self.invoke(json.dumps(d).encode());self.assertEqual(code,0);self.assertEqual(r['verified_training_labels'],0);self.assertFalse(r['authenticity_verified']);self.assertFalse(r['forecast_log_verified'])

    def test_public_file_or_directory_is_rejected(self):
        for fm,dm in [(0o644,0o700),(0o600,0o755)]:
            code,r=self.invoke(json.dumps(self.document()).encode(),file_mode=fm,directory_mode=dm);self.assertEqual(code,1);self.assertEqual(r['error_code'],'evaluation_invalid_input_file')

    def test_bad_json_duplicate_keys_and_nonfinite_are_masked(self):
        for raw in [b'PRIVATE_NEVER_PRINT',b'{"a":1,"a":2}',b'{"a":NaN}',b'{"a":Infinity}',b'\xff',b'[]']:
            code,r=self.invoke(raw);self.assertEqual(code,1);self.assertNotIn('PRIVATE_NEVER_PRINT',str(r));self.assertNotIn('mean_absolute_error_seconds',r)

    def test_exact_read_limit_and_one_byte_over(self):
        raw=json.dumps(self.document()).encode();code,_=self.invoke(raw+b' '*(16384-len(raw)));self.assertEqual(code,0);code,r=self.invoke(raw+b' '*(16385-len(raw)));self.assertEqual(code,1);self.assertEqual(r['error_code'],'evaluation_invalid_input_file')

    def test_hardlinked_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder).resolve()/'input.json';p.write_text(json.dumps(self.document()));p.chmod(0o600);os.link(p,Path(folder)/'another.json')
            with self.assertRaises(e.EvaluationError):e.read_evaluation_document(p)

    def test_symlinked_ancestor_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            actual=Path(folder).resolve()/'actual';actual.mkdir(mode=0o700);p=actual/'input.json';p.write_text(json.dumps(self.document()));p.chmod(0o600);link=Path(folder).resolve()/'alias';link.symlink_to(actual,target_is_directory=True)
            with self.assertRaises(e.EvaluationError):e.read_evaluation_document(link/'input.json')

    def test_schema_boolean_extra_field_and_missing_record_list_rejected(self):
        for d in [dict(self.document(),schema_version=True),dict(self.document(),extra='private'),{k:v for k,v in self.document().items() if k!='records'}]:
            code,r=self.invoke(json.dumps(d).encode());self.assertEqual(code,1);self.assertEqual(r['error_code'],'evaluation_invalid_document')

    def test_invalid_second_record_returns_no_partial_metrics(self):
        d=self.document();d['records'].append(dict(self.row(),point_call_at='PRIVATE_NEVER_PRINT',prediction_id='00000000-0000-4000-8000-000000000002'));code,r=self.invoke(json.dumps(d).encode());self.assertEqual(code,1);self.assertNotIn('mean_absolute_error_seconds',r);self.assertNotIn('PRIVATE_NEVER_PRINT',str(r))

    def test_identifier_is_canonical_uuid4(self):
        for value in [None,True,'00000000-0000-1000-8000-000000000001','not-a-uuid','AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA']:
            with self.assertRaises(e.EvaluationError):self.report([self.row(prediction_id=value)])

    def test_record_bound_refused_before_iteration(self):
        with self.assertRaises(e.EvaluationError) as error:self.report([None]*10001)
        self.assertEqual(error.exception.error_code,'evaluation_invalid_bounds')

    def test_forecast_point_after_as_of_can_still_be_a_real_miss(self):
        # A forecast of 00:15, actual call 00:10, checked at 00:12.
        r=e.evaluate_intervals([self.row(point=15,p=14,q=20,l=10,u=10)],as_of='2020-01-01T00:12:00Z',data_origin='synthetic')
        self.assertEqual(r['mean_absolute_error_seconds'],{'lower':300,'upper':300})


if __name__=='__main__':unittest.main()
