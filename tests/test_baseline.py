"""History cutoffs, interval survival arithmetic and private forecast candidates."""
import contextlib
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
from itertools import combinations_with_replacement, product
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from sushiwait.baseline import BaselineError, build_baseline, interval_quantiles, validate_plan, write_baseline
from sushiwait.cli import main
from sushiwait.intake import OutcomeIntakeStore
from sushiwait.reviews import OutcomeReviewStore, draft_review
import test_outcomes as fixtures

BASE = datetime(2026,10,6,4,30,tzinfo=timezone.utc)
def stamp(second=0):
    return (BASE+timedelta(seconds=second)).isoformat()


class IntervalBaselineTests(unittest.TestCase):
    def test_empirical_type_one_envelopes_preserve_interval_bounds(self):
        q=interval_quantiles([(1,10),(20,30),(40,50)])['quantiles']
        self.assertEqual(q['p50'],{'lower_us':20,'upper_us':30})
        self.assertEqual(q['p10'],{'lower_us':1,'upper_us':10})
        self.assertEqual(q['p90'],{'lower_us':40,'upper_us':50})

    def test_remaining_is_survival_conditioned_not_unconditional_minus_elapsed(self):
        value=interval_quantiles([(60,60),(600,600)],elapsed_us=300)
        self.assertEqual(value['quantiles']['p50'],{'lower_us':300,'upper_us':300})
        self.assertEqual(value['definite_survivors'],1)
        self.assertEqual(value['possible_survivors'],1)

    def test_ambiguous_survivor_retains_uncertainty(self):
        value=interval_quantiles([(0,120),(180,240)],elapsed_us=60)
        self.assertEqual(value['quantiles']['p50'],{'lower_us':0,'upper_us':180})
        self.assertEqual(value['definite_survivors'],1)
        self.assertEqual(value['possible_survivors'],2)

    def test_no_definite_survivors_returns_unknown(self):
        for intervals in [[(0,120)],[(60,60)],[(0,60)]]:
            value=interval_quantiles(intervals,elapsed_us=60)
            self.assertIsNone(value['quantiles'])
            self.assertEqual(value['reason_code'],'no_definite_survivors')

    def test_survival_bounds_contain_all_small_discrete_assignments(self):
        pairs=[(l,u) for l in range(4) for u in range(l,4)]
        checked=0
        for intervals in combinations_with_replacement(pairs,3):
            for elapsed in range(3):
                result=interval_quantiles(list(intervals),elapsed_us=elapsed)
                if result['quantiles'] is None:
                    continue
                for assignments in product(*(range(l,u+1) for l,u in intervals)):
                    survivors=sorted(v-elapsed for v in assignments if v>elapsed)
                    self.assertTrue(survivors)
                    for name,num,den in [('p10',1,10),('p50',1,2),('p90',9,10)]:
                        value=survivors[(num*len(survivors)+den-1)//den-1]
                        bounds=result['quantiles'][name]
                        self.assertLessEqual(bounds['lower_us'],value)
                        self.assertGreaterEqual(bounds['upper_us'],value)
                        checked+=1
        self.assertGreater(checked,3000)

    def test_order_invariance(self):
        pairs=[(0,120),(180,240),(300,350)]
        for elapsed in (None,60,250):
            self.assertEqual(interval_quantiles(pairs,elapsed_us=elapsed),
                interval_quantiles(list(reversed(pairs)),elapsed_us=elapsed))

    def test_invalid_bounds_and_bool_are_rejected(self):
        for value in ([],[(True,5)],[(5,4)],[(-1,2)],[(0,172_800_000_001)],[(1.0,2)],'private-marker'):
            with self.subTest(value=value),self.assertRaises(BaselineError) as caught:
                interval_quantiles(value)
            self.assertEqual(str(caught.exception),'baseline_invalid_intervals')
        for elapsed in (-1,True,1.5,172_800_000_001):
            with self.assertRaises(BaselineError):
                interval_quantiles([(1,2)],elapsed_us=elapsed)


class ReviewedBaselineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent=Path(self.tmp.name).resolve()
        self.parent.chmod(0o700)
        for name in ('source','reviews','output'):
            (self.parent/name).mkdir(mode=0o700)
        self.source_path=self.parent/'source/source.sqlite3'
        self.review_path=self.parent/'reviews/reviews.sqlite3'
        self.output_path=self.parent/'output/result.json'
        self.input_path=self.parent/'output/plan.json'
        self.value=self.episode()
        self.review=self.add(self.value)
        self.plan={'schema_version':1,'as_of':stamp(30),'data_origin':'synthetic',
            'api_profile':'miniapp_gateway','store_id':'900001','queue_type':'ordinary',
            'party_size':2,'table_type':'unknown','mode':'new_join','minimum_samples':1,
            'target_episode_id':None}

    def episode(self, *, issued=-1800, lower=600, upper=660, **options):
        value=fixtures.record()
        value.update(episode_id=str(uuid4()),**options)
        value['events']=[value['events'][0],value['events'][2]]
        for event,l,u in zip(value['events'],[issued,issued+lower],[issued,issued+upper]):
            event.update(event_id=str(uuid4()),event_time_lower=stamp(l),event_time_upper=stamp(u),observed_at=stamp(u))
        value['recorded_at']=stamp(issued+upper+60)
        return value

    def add(self,value,receipt=0):
        with OutcomeIntakeStore(self.source_path) as source,patch('sushiwait.intake._clock',return_value=BASE+timedelta(seconds=receipt)):
            source.append(value)
        draft_path=self.parent/'reviews/draft.json'
        if draft_path.exists():
            draft_path.unlink()
        with OutcomeIntakeStore(self.source_path,read_only=True) as source,patch('sushiwait.reviews._clock',return_value=BASE+timedelta(seconds=receipt+1)):
            draft_review(source,value,draft_path)
        review=json.loads(draft_path.read_text())
        review.update(decision='accept',reason_code='confirmed_call_interval',issued_time_bounds_checked=True,
            called_time_bounds_checked=True,store_and_queue_checked=True)
        self.receive(review,receipt+2)
        return review

    def receive(self,review,receipt):
        with OutcomeIntakeStore(self.source_path,read_only=True) as source,OutcomeReviewStore(self.review_path) as reviews, \
                patch('sushiwait.reviews._clock',return_value=BASE+timedelta(seconds=receipt)):
            reviews.append(review,source=source)

    def calculate(self,plan=None):
        with OutcomeIntakeStore(self.source_path,read_only=True) as source,OutcomeReviewStore(self.review_path,read_only=True) as reviews, \
                patch('sushiwait.baseline._clock',return_value=BASE+timedelta(seconds=1000)), \
                patch('sushiwait.reviews._clock',return_value=BASE+timedelta(seconds=1000)):
            return build_baseline(source=source,reviews=reviews,plan=self.plan if plan is None else plan)

    def ideal(self,offset=10):
        return {**self.plan,'mode':'ideal_time','desired_arrival_at':stamp(1200),
            'call_offset_minutes':offset,'candidate_issue_times':[stamp(30),stamp(600),stamp(1200)]}

    def test_exact_date_hour_matching_and_honest_research_state(self):
        result=self.calculate()
        forecast=result['forecast']
        self.assertTrue(forecast['research_forecast_available'])
        self.assertEqual(forecast['matched_group'],'exact_date_hour')
        self.assertEqual(forecast['wait_quantile_envelopes_us']['p50'],{'lower_us':600_000_000,'upper_us':660_000_000})
        self.assertEqual(forecast['representative_wait_us'],630_000_000)
        for key in ('eta_available','authenticity_verified','uses_realtime_queue_data','production_forecast_log','training_eligible'):
            self.assertFalse(result[key])
        self.assertEqual(result['verified_training_labels'],0)
        self.assertFalse(forecast['coverage_calibrated'])
        self.assertNotIn(self.value['episode_id'],json.dumps(result))

    def test_static_cohort_never_mixes_store_queue_party_or_table(self):
        for key,value in [('store_id','900002'),('queue_type','reservation'),('party_size',3),('party_size',None),('table_type','counter')]:
            plan={**self.plan,key:value}
            result=self.calculate(plan)
            self.assertFalse(result['forecast']['research_forecast_available'])
            self.assertEqual(result['excluded_samples']['static_scope'],1)
            self.assertIsNone(result['forecast']['representative_call_at'])

    def test_profile_and_origin_are_isolated(self):
        for key,value in [('api_profile','legacy'),('data_origin','self_reported')]:
            result=self.calculate({**self.plan,key:value})
            self.assertEqual(result['eligible_static_cohort_samples'],0)

    def test_target_episode_is_excluded(self):
        result=self.calculate({**self.plan,'target_episode_id':self.value['episode_id']})
        self.assertEqual(result['excluded_samples']['target_episode'],1)
        self.assertFalse(result['forecast']['research_forecast_available'])

    def test_minimum_samples_is_not_accuracy_guarantee(self):
        result=self.calculate({**self.plan,'minimum_samples':2})
        self.assertEqual(result['forecast']['reason_code'],'insufficient_matching_history')
        self.assertFalse(result['forecast']['minimum_is_quality_guarantee'])

    def test_older_year_uses_explicit_static_fallback(self):
        plan={**self.plan,'as_of':'2025-10-06T04:30:00Z'}
        result=self.calculate(plan)
        self.assertFalse(result['forecast']['research_forecast_available'])
        old=fixtures.record()
        old['episode_id']=str(uuid4())
        self.add(old,3)
        self.plan['target_episode_id']=self.value['episode_id']
        forecast=self.calculate()['forecast']
        self.assertEqual(forecast['matched_group'],'store_queue_party_table')
        self.assertTrue(forecast['coarse_fallback'])

    def test_uncertain_issue_crossing_hours_cannot_enter_hour_group(self):
        value=self.episode(issued=-1860)
        value['events'][0].update(event_time_upper=stamp(-1740),observed_at=stamp(-1740))
        self.add(value,3)
        self.plan['target_episode_id']=self.value['episode_id']
        forecast=self.calculate()['forecast']
        self.assertEqual(forecast['matched_group'],'date_type')

    def test_new_receipts_and_future_corrections_do_not_change_historical_inputs(self):
        before=self.calculate()
        self.add(self.episode(lower=1200,upper=1200),100)
        after=self.calculate()
        self.assertEqual(before['historical_input_sha256'],after['historical_input_sha256'])
        self.assertEqual(before['forecast'],after['forecast'])
        self.assertGreater(after['audit_diagnostics_not_features']['source_revisions_audited'],
            before['audit_diagnostics_not_features']['source_revisions_audited'])

    def test_review_receipt_cutoff_is_enforced(self):
        result=self.calculate({**self.plan,'as_of':stamp(1)})
        self.assertFalse(result['forecast']['research_forecast_available'])
        result=self.calculate({**self.plan,'as_of':stamp(2)})
        self.assertTrue(result['forecast']['research_forecast_available'])

    def test_source_correction_invalidates_baseline_until_new_review(self):
        value=deepcopy(self.value)
        value.update(revision=2,supersedes_revision=1,recorded_at=stamp(-100))
        with OutcomeIntakeStore(self.source_path) as source,patch('sushiwait.intake._clock',return_value=BASE+timedelta(seconds=10)):
            source.append(value)
        result=self.calculate()
        self.assertFalse(result['forecast']['research_forecast_available'])
        self.assertEqual(result['audit_diagnostics_not_features']['stale_current_reviews'],1)
        self.add(value,11)
        self.assertTrue(self.calculate()['forecast']['research_forecast_available'])

    def test_conflicting_review_is_excluded(self):
        review={**self.review,'review_id':str(uuid4()),'reviewer_id':str(uuid4()),
            'decision':'reject','reason_code':'mismatch'}
        self.receive(review,10)
        self.assertFalse(self.calculate()['forecast']['research_forecast_available'])

    def test_remaining_wait_uses_elapsed_and_known_not_called(self):
        plan={**self.plan,'mode':'remaining','issued_at':stamp(-270),'call_not_observed':True,
            'target_episode_id':str(uuid4())}
        forecast=self.calculate(plan)['forecast']
        self.assertEqual(forecast['wait_quantile_envelopes_us']['p50'],{'lower_us':300_000_000,'upper_us':360_000_000})
        self.assertEqual(forecast['conditioning'],'call_not_observed_after_elapsed')

    def test_remaining_without_survivors_is_unknown_not_zero_wait(self):
        plan={**self.plan,'mode':'remaining','issued_at':stamp(-1000),'call_not_observed':True,
            'target_episode_id':str(uuid4())}
        forecast=self.calculate(plan)['forecast']
        self.assertFalse(forecast['research_forecast_available'])
        self.assertEqual(forecast['reason_code'],'insufficient_definite_survivors')
        self.assertIsNone(forecast['representative_wait_us'])

    def test_ideal_offsets_have_user_defined_sign(self):
        for offset in (10,-10):
            forecast=self.calculate(self.ideal(offset))['forecast']
            self.assertEqual(forecast['target_call_at'],(BASE+timedelta(seconds=1200+offset*60)).isoformat(timespec='microseconds').replace('+00:00','Z'))
            self.assertFalse(forecast['monotonicity_assumed'])
            self.assertFalse(forecast['candidate_bookability_verified'])
        self.assertEqual(self.calculate(self.ideal(10))['forecast']['selected_candidate_index'],2)
        self.assertEqual(self.calculate(self.ideal(-10))['forecast']['selected_candidate_index'],0)

    def test_ideal_search_evaluates_nonmonotone_candidate_forecasts(self):
        import sushiwait.baseline as baseline
        original=baseline._forecast
        calls=[]
        def discontinuous(pool,plan,issued_at,**kwargs):
            result=original(pool,plan,issued_at,**kwargs)
            calls.append(issued_at)
            if len(calls)==2:
                result['median_call_envelope']={'lower':stamp(1800),'upper':stamp(1800)}
            return result
        with patch('sushiwait.baseline._forecast',side_effect=discontinuous):
            result=self.calculate(self.ideal(10))['forecast']
        self.assertEqual(len(calls),3)
        self.assertEqual(result['selected_candidate_index'],1)

    def test_ideal_no_history_returns_no_candidate(self):
        plan={**self.ideal(),'minimum_samples':2}
        forecast=self.calculate(plan)['forecast']
        self.assertIsNone(forecast['candidate_issue_at'])
        self.assertIsNone(forecast['selected_candidate_index'])

    def test_invalid_plans_reject_private_extras_and_future_cutoff(self):
        for key,value in [('schema_version',True),('store_id','001'),('party_size',True),('minimum_samples',0),
            ('target_episode_id','private-marker'),('as_of',stamp(1001)),('authorization','private-marker')]:
            plan={**self.plan,key:value}
            with self.subTest(key=key),self.assertRaises(BaselineError) as caught:
                validate_plan(plan,now=BASE+timedelta(seconds=1000))
            self.assertNotIn('private-marker',str(caught.exception))

    def test_invalid_ideal_grid_offsets_and_remaining_assertion(self):
        for values in ([stamp(600),stamp(30)],[stamp(30),stamp(30)],[],[stamp(-1)],[stamp(86431)]):
            with self.assertRaises(BaselineError):
                validate_plan({**self.ideal(),'candidate_issue_times':values},now=BASE+timedelta(seconds=1000))
        for offset in (True,121,-121,1.5):
            with self.assertRaises(BaselineError):
                validate_plan(self.ideal(offset),now=BASE+timedelta(seconds=1000))
        for target,not_called in [(None,True),(str(uuid4()),False),(str(uuid4()),1)]:
            with self.assertRaises(BaselineError):
                validate_plan({**self.plan,'mode':'remaining','issued_at':stamp(-10),
                    'target_episode_id':target,'call_not_observed':not_called},now=BASE+timedelta(seconds=1000))

    def cli(self):
        self.input_path.write_text(json.dumps(self.plan))
        self.input_path.chmod(0o600)
        output=io.StringIO()
        with contextlib.redirect_stdout(output),patch('socket.socket',side_effect=AssertionError('no network')), \
                patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('no auth')), \
                patch('sushiwait.cli.client_for',side_effect=AssertionError('no official client')):
            code=main(['baseline-research','--source-db',str(self.source_path),'--reviews-db',str(self.review_path),
                '--input',str(self.input_path),'--output',str(self.output_path)])
        return code,json.loads(output.getvalue())

    def test_cli_writes_private_artifact_leaves_both_databases_unchanged(self):
        before=(self.source_path.read_bytes(),self.review_path.read_bytes())
        code,result=self.cli()
        self.assertEqual(code,0)
        self.assertTrue(result['research_forecast_available'])
        self.assertEqual(self.output_path.stat().st_mode & 0o777,0o600)
        self.assertEqual(before,(self.source_path.read_bytes(),self.review_path.read_bytes()))
        self.assertNotIn('900001',json.dumps(result))
        self.assertNotIn(self.value['episode_id'],json.dumps(result))
        self.assertFalse(json.loads(self.output_path.read_text())['eta_available'])

    def test_existing_output_is_not_replaced(self):
        self.cli()
        before=self.output_path.read_bytes()
        code,result=self.cli()
        self.assertEqual(code,1)
        self.assertEqual(result['error_code'],'baseline_output_exists')
        self.assertEqual(before,self.output_path.read_bytes())

    def test_publication_durability_failure_is_explicit(self):
        with patch('sushiwait.baseline._write_packet',return_value={'committed':True,'durability_confirmed':False}):
            code,result=self.cli()
        self.assertEqual(code,1)
        self.assertTrue(result['committed'])
        self.assertFalse(result['durability_confirmed'])

    def test_invalid_input_does_not_open_databases(self):
        self.plan['authorization']='private-marker'
        with patch('sushiwait.cli.OutcomeIntakeStore',side_effect=AssertionError('must not open')) as source:
            code,result=self.cli()
        self.assertEqual(code,1)
        self.assertEqual(source.call_count,0)
        self.assertEqual(result['error_code'],'baseline_invalid_input')

    def test_read_only_reader_is_required(self):
        with OutcomeIntakeStore(self.source_path,read_only=True) as source,OutcomeReviewStore(self.review_path) as reviews:
            with self.assertRaises(BaselineError) as caught:
                build_baseline(source=source,reviews=reviews,plan=self.plan)
        self.assertEqual(caught.exception.error_code,'baseline_source_invalid')
