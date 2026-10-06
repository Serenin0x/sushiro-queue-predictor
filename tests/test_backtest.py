"""Chronological replay must not borrow later outcomes or review corrections."""
import contextlib
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from sushiwait.backtest import BacktestError,build_backtest,validate_backtest_plan
from sushiwait.cli import main
from sushiwait.intake import OutcomeIntakeStore
from sushiwait.reviews import OutcomeReviewStore,draft_review
import test_outcomes as fixtures

BASE=datetime(2026,10,6,4,30,tzinfo=timezone.utc)
def stamp(second):
    return (BASE+timedelta(seconds=second)).isoformat()


class BacktestTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.parent=Path(self.tmp.name).resolve();self.parent.chmod(0o700)
        for name in ('source','reviews','output'):
            (self.parent/name).mkdir(mode=0o700)
        self.source_path=self.parent/'source/source.sqlite3'
        self.review_path=self.parent/'reviews/reviews.sqlite3'
        self.input_path=self.parent/'output/plan.json'
        self.output_path=self.parent/'output/result.json'
        self.history=self.episode(-900,600,660)
        self.history_review=self.add(self.history,0)
        self.target=self.episode(120,600,660)
        self.target_review=self.add(self.target,900)
        self.plan={'schema_version':1,'as_of':stamp(1000),'data_origin':'synthetic',
            'api_profile':'miniapp_gateway','store_id':'900001','minimum_samples':1,
            'elapsed_seconds':[0,300],'max_cases':100}

    def episode(self,issued,lower,upper,**options):
        value=fixtures.record();value.update(episode_id=str(uuid4()),**options)
        value['events']=[value['events'][0],value['events'][2]]
        for event,l,u in zip(value['events'],[issued,issued+lower],[issued,issued+upper]):
            event.update(event_id=str(uuid4()),event_time_lower=stamp(l),event_time_upper=stamp(u),observed_at=stamp(u))
        value['recorded_at']=stamp(issued+upper+60)
        return value

    def receive_review(self,review,receipt):
        with OutcomeIntakeStore(self.source_path,read_only=True) as source,OutcomeReviewStore(self.review_path) as reviews, \
                patch('sushiwait.reviews._clock',return_value=BASE+timedelta(seconds=receipt)):
            reviews.append(review,source=source)

    def add(self,value,receipt):
        with OutcomeIntakeStore(self.source_path) as source,patch('sushiwait.intake._clock',return_value=BASE+timedelta(seconds=receipt)):
            source.append(value)
        path=self.parent/'reviews/draft.json'
        if path.exists():path.unlink()
        with OutcomeIntakeStore(self.source_path,read_only=True) as source,patch('sushiwait.reviews._clock',return_value=BASE+timedelta(seconds=receipt+1)):
            draft_review(source,value,path)
        review=json.loads(path.read_text());review.update(decision='accept',reason_code='confirmed_call_interval',
            issued_time_bounds_checked=True,called_time_bounds_checked=True,store_and_queue_checked=True)
        self.receive_review(review,receipt+2)
        return review

    def calculate(self,plan=None,**kwargs):
        with OutcomeIntakeStore(self.source_path,read_only=True) as source,OutcomeReviewStore(self.review_path,read_only=True) as reviews, \
                patch('sushiwait.backtest._clock',return_value=BASE+timedelta(seconds=3000)), \
                patch('sushiwait.baseline._clock',return_value=BASE+timedelta(seconds=3000)), \
                patch('sushiwait.reviews._clock',return_value=BASE+timedelta(seconds=3000)):
            return build_backtest(source=source,reviews=reviews,plan=self.plan if plan is None else plan,**kwargs)

    def test_prior_received_history_scores_later_target_and_retains_cold_start(self):
        result=self.calculate()
        self.assertEqual(result['attempted_cases'],4)
        self.assertEqual(result['scored_cases'],2)
        for group in result['evaluations_by_elapsed_seconds']:
            self.assertEqual(group['availability_counts']['scored_cases'],1)
            self.assertEqual(group['availability_counts']['insufficient_matching_history'],1)
            evaluation=group['interval_evaluation']
            self.assertEqual(evaluation['mean_absolute_error_seconds'],{'lower':0.0,'upper':30.0})
            self.assertEqual(evaluation['coverage']['lower'],1)
        self.assertFalse(result['forecast_log_verified'])
        self.assertFalse(result['historical_target_context_verified'])
        self.assertEqual(result['target_context_basis'],'reviewed_outcome_reconstruction_not_historical_request')
        self.assertFalse(result['model_performance_verified'])
        self.assertFalse(result['eta_available'])
        self.assertEqual(result['verified_training_labels'],0)
        self.assertFalse(result['lead_groups_are_independent_samples'])

    def test_late_receipts_cannot_score_their_own_old_episode(self):
        result=self.calculate()
        first=[c for c in result['cases'] if c['episode_id']==self.history['episode_id']]
        self.assertEqual(len(first),2)
        self.assertTrue(all(not c['research_forecast_available'] for c in first))

    def test_source_and_review_truth_cutoff_are_both_required(self):
        before=self.calculate({**self.plan,'as_of':stamp(901)})
        after=self.calculate({**self.plan,'as_of':stamp(902)})
        self.assertEqual(before['reviewed_scope_episodes'],1)
        self.assertEqual(before['scored_cases'],0)
        self.assertEqual(after['reviewed_scope_episodes'],2)
        self.assertEqual(after['scored_cases'],2)

    def test_review_withdrawal_after_prediction_does_not_erase_prior_training(self):
        self.source_path.unlink();self.review_path.unlink()
        self.history_review=self.add(self.history,0)
        changed={**self.history_review,'revision':2,'supersedes_revision':1,'reviewed_at':stamp(199),
            'decision':'reject','reason_code':'withdrawn'}
        self.receive_review(changed,200)
        self.add(self.target,900)
        result=self.calculate()
        self.assertEqual(result['reviewed_scope_episodes'],1)
        self.assertEqual(result['scored_cases'],1)
        by_elapsed={c['elapsed_seconds']:c for c in result['cases']}
        self.assertTrue(by_elapsed[0]['research_forecast_available'])
        self.assertFalse(by_elapsed[300]['research_forecast_available'])

    def test_source_correction_after_prediction_is_not_backfilled(self):
        before=self.calculate()
        self.source_path.unlink();self.review_path.unlink()
        self.add(self.history,0)
        changed=deepcopy(self.history);changed.update(revision=2,supersedes_revision=1,recorded_at=stamp(100))
        changed['events'][1].update(event_time_lower=stamp(-60),event_time_upper=stamp(0),observed_at=stamp(0))
        self.add(changed,200)
        self.add(self.target,900)
        after=self.calculate()
        get=lambda r:next(c for c in r['cases'] if c['episode_id']==self.target['episode_id'] and c['elapsed_seconds']==0)
        self.assertEqual(get(before)['historical_input_sha256'],get(after)['historical_input_sha256'])
        self.assertEqual(get(before)['evaluation_record']['point_call_at'],get(after)['evaluation_record']['point_call_at'])

    def test_future_targets_change_audit_not_prior_replay_fingerprint(self):
        before=self.calculate()
        self.add(self.episode(1200,600,660),2000)
        after=self.calculate()
        self.assertEqual(before['historical_replay_sha256'],after['historical_replay_sha256'])
        self.assertGreater(after['audit_diagnostics_not_features']['source_revisions_audited'],
            before['audit_diagnostics_not_features']['source_revisions_audited'])

    def test_elapsed_after_possible_call_is_excluded_per_group(self):
        result=self.calculate({**self.plan,'elapsed_seconds':[0,900]})
        group=result['evaluations_by_elapsed_seconds'][1]
        self.assertEqual(group['attempted_cases'],0)
        self.assertEqual(group['excluded_call_not_definitely_future'],2)
        self.assertIsNone(group['interval_evaluation']['mean_absolute_error_seconds']['lower'])
        self.assertEqual(result['excluded_cases']['call_not_definitely_future'],2)

    def test_case_limit_rejects_before_any_forecast_not_silent_sampling(self):
        with patch('sushiwait.backtest.build_baseline',side_effect=AssertionError('must not forecast')) as forecast:
            with self.assertRaises(BacktestError) as caught:
                self.calculate({**self.plan,'max_cases':3})
        self.assertEqual(caught.exception.error_code,'backtest_case_limit')
        self.assertEqual(forecast.call_count,0)

    def test_store_profile_and_origin_are_separate(self):
        for key,value in [('store_id','900002'),('api_profile','legacy'),('data_origin','self_reported')]:
            result=self.calculate({**self.plan,key:value})
            self.assertEqual(result['reviewed_scope_episodes'],0)
            self.assertEqual(result['scored_cases'],0)

    def test_different_queue_cannot_supply_target_training(self):
        target=self.episode(120,600,660,queue_type='reservation')
        self.add(target,910)
        result=self.calculate()
        cases=[c for c in result['cases'] if c['episode_id']==target['episode_id']]
        self.assertTrue(cases)
        self.assertTrue(all(not c['research_forecast_available'] for c in cases))

    def test_wide_label_uses_error_bounds_not_midpoint_accuracy(self):
        changed=deepcopy(self.target);changed.update(revision=2,supersedes_revision=1,recorded_at=stamp(850))
        changed['events'][1].update(event_time_lower=stamp(720),event_time_upper=stamp(790),observed_at=stamp(790))
        self.add(changed,920)
        evaluation=self.calculate()['evaluations_by_elapsed_seconds'][0]['interval_evaluation']
        self.assertEqual(evaluation['mean_absolute_error_seconds'],{'lower':0.0,'upper':40.0})
        self.assertEqual(evaluation['coverage']['lower'],0)
        self.assertEqual(evaluation['coverage']['upper'],1)

    def test_insufficient_sample_count_keeps_all_missing_cases(self):
        result=self.calculate({**self.plan,'minimum_samples':2})
        self.assertEqual(result['attempted_cases'],4)
        self.assertEqual(result['scored_cases'],0)
        self.assertTrue(all(g['interval_evaluation']['coverage']['lower'] is None for g in result['evaluations_by_elapsed_seconds']))

    def test_repeated_replay_fingerprint_is_stable(self):
        self.assertEqual(self.calculate()['historical_replay_sha256'],self.calculate()['historical_replay_sha256'])

    def test_invalid_plans_and_elapsed_grid_are_rejected(self):
        for key,value in [('schema_version',True),('as_of',stamp(3001)),('store_id','001'),('max_cases',101),
            ('minimum_samples',True),('elapsed_seconds',[]),('elapsed_seconds',[0,0]),('elapsed_seconds',[300,0]),
            ('elapsed_seconds',[True]),('elapsed_seconds',[-1]),('elapsed_seconds',[172801]),('authorization','private-marker')]:
            with self.subTest(key=key),self.assertRaises(BacktestError) as caught:
                validate_backtest_plan({**self.plan,key:value},now=BASE+timedelta(seconds=3000))
            self.assertEqual(caught.exception.error_code,'backtest_invalid_plan')

    def test_complete_revision_bound_refuses_partial_scores(self):
        with self.assertRaises(BacktestError) as caught:
            self.calculate(max_revisions=1)
        self.assertEqual(caught.exception.error_code,'backtest_source_invalid')

    def cli(self):
        self.input_path.write_text(json.dumps(self.plan));self.input_path.chmod(0o600)
        output=io.StringIO()
        with contextlib.redirect_stdout(output),patch('socket.socket',side_effect=AssertionError('no network')), \
                patch('sushiwait.cli.client_for',side_effect=AssertionError('no client')), \
                patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('no credentials')):
            code=main(['baseline-backtest','--source-db',str(self.source_path),'--reviews-db',str(self.review_path),
                '--input',str(self.input_path),'--output',str(self.output_path)])
        return code,json.loads(output.getvalue())

    def test_cli_artifact_is_private_and_both_sources_unchanged(self):
        before=(self.source_path.read_bytes(),self.review_path.read_bytes())
        code,result=self.cli()
        self.assertEqual(code,0)
        self.assertEqual(result['scored_cases'],2)
        self.assertEqual(self.output_path.stat().st_mode & 0o777,0o600)
        self.assertEqual(before,(self.source_path.read_bytes(),self.review_path.read_bytes()))
        for secret in (self.target['episode_id'],'900001',stamp(120)):
            self.assertNotIn(secret,json.dumps(result))

    def test_cli_existing_file_is_not_replaced(self):
        self.cli();before=self.output_path.read_bytes()
        code,result=self.cli()
        self.assertEqual(code,1)
        self.assertEqual(result['error_code'],'backtest_output_exists')
        self.assertEqual(before,self.output_path.read_bytes())

    def test_durability_uncertainty_is_explicit(self):
        with patch('sushiwait.backtest._write_packet',return_value={'committed':True,'durability_confirmed':False}):
            code,result=self.cli()
        self.assertEqual(code,1)
        self.assertTrue(result['committed'])
        self.assertFalse(result['durability_confirmed'])

    def test_invalid_input_does_not_open_sources(self):
        self.plan['authorization']='private-marker'
        with patch('sushiwait.cli.OutcomeIntakeStore',side_effect=AssertionError('must not open')) as source:
            code,result=self.cli()
        self.assertEqual(code,1);self.assertEqual(source.call_count,0)
        self.assertEqual(result['error_code'],'backtest_invalid_input')

    def test_readers_are_required(self):
        with OutcomeIntakeStore(self.source_path,read_only=True) as source,OutcomeReviewStore(self.review_path) as reviews:
            with self.assertRaises(BacktestError):
                build_backtest(source=source,reviews=reviews,plan=self.plan)
