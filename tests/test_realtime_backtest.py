"""Actual sealed sources and chronological comparators, all explicitly synthetic."""
import contextlib
from copy import deepcopy
from datetime import timedelta
import io
import json
import unittest
from unittest.mock import patch

import test_features as fixture
from sushiwait.cli import main
from sushiwait.intake import OutcomeIntakeStore
from sushiwait.remote import RemoteStore
from sushiwait.reviews import OutcomeReviewStore
from sushiwait.realtimebacktest import build_realtime_backtest, validate_plan, _paired, RealtimeBacktestError
from sushiwait.realtimefusion import build_realtime_fusion
from sushiwait.features import build_feature_dataset

BASE=fixture.BASE
stamp=fixture.stamp


class RealtimeBacktestTests(unittest.TestCase):
    episode=fixture.FeatureDatasetTests.episode
    add=fixture.FeatureDatasetTests.add
    receive_review=fixture.FeatureDatasetTests.receive_review
    write=fixture.FeatureDatasetTests.write
    mutate=fixture.FeatureDatasetTests.mutate

    def setUp(self):
        fixture.FeatureDatasetTests.setUp(self)
        self.write([(t,['999','888'],10) for t in (200,230,260,290,310,340,370)])
        self.write([(t,[str(i),'888'],10) for i,t in enumerate((500,530,560,590,610,640,670))])
        self.slow=[];self.fast=[]
        for issued in (620,630,640,650):
            ep=self.episode(issued,120,180);self.add(ep,900);self.fast.append(ep)
        for issued in (320,330,340,350):
            ep=self.episode(issued,900,960);self.add(ep,1500);self.slow.append(ep)
        self.fast_target=self.episode(2000,120,180);self.add(self.fast_target,2250)
        self.slow_target=self.episode(2600,900,960);self.add(self.slow_target,3650)
        self.write([(t,[str(i),'888'],10) for i,t in enumerate((1880,1910,1940,1970,1990,2020,2050))])
        self.write([(t,['999','888'],10) for t in (2480,2510,2540,2570,2590,2620,2650)])
        self.plan.update(as_of=stamp(4000),elapsed_seconds=[0,30],minimum_samples=1,neighbors=2,
            minimum_episodes=2,maximum_elapsed_difference_seconds=300,maximum_standardized_distance=3,
            realtime_weight_ppm=500000)

    def calculate(self,**kw):
        args=dict(plan=self.plan,now=BASE+timedelta(seconds=4000));args.update(kw)
        with OutcomeIntakeStore(self.source_path,read_only=True) as source, \
                OutcomeReviewStore(self.review_path,read_only=True) as reviews, \
                RemoteStore(self.remote_path,read_only=True) as remote, \
                patch('socket.socket',side_effect=AssertionError('network')):
            return build_realtime_backtest(source=source,reviews=reviews,remote=remote,**args)

    def case(self,result=None,*,target=None,elapsed=0):
        result=self.calculate() if result is None else result
        target=self.fast_target if target is None else target
        return next(c for c in result['cases'] if c['episode_id']==target['episode_id'] and c['elapsed_seconds']==elapsed)

    def test_actual_three_sources_fit_score_and_remain_unchanged(self):
        before=[p.read_bytes() for p in (self.source_path,self.review_path,self.remote_path)]
        result=self.calculate()
        self.assertEqual(result['attempted_cases'],24)
        self.assertTrue(self.case(result)['realtime_model_fitted'])
        self.assertEqual(before,[p.read_bytes() for p in (self.source_path,self.review_path,self.remote_path)])
        self.assertFalse(result['forecast_log_verified'] or result['eta_available'] or result['provider_called'])
        self.assertEqual(result['verified_training_labels'],0)

    def test_numeric_speed_comparison_on_same_targets_can_improve_or_worsen(self):
        result=self.calculate()
        fast=self.case(result);slow=self.case(result,target=self.slow_target)
        self.assertLess(fast['variants']['realtime_only']['quantiles_us']['p50']['upper_us'],
                        slow['variants']['realtime_only']['quantiles_us']['p50']['lower_us'])
        for group in result['evaluations_by_elapsed_seconds']:
            comparison=group['paired_against_history']['realtime_only']
            self.assertGreaterEqual(comparison['paired_cases'],2)
            self.assertGreaterEqual(comparison['definitely_lower_absolute_error_cases'],1)
            self.assertEqual(comparison['paired_cases'],comparison['history_on_common_cases']['interval_labels_evaluated'])
            self.assertEqual(comparison['paired_cases'],comparison['comparator_on_common_cases']['interval_labels_evaluated'])
        self.assertFalse(result['best_variant_selected'] or result['parameters_tuned_on_test_outcomes'])

    def test_no_target_or_later_truth_enters_any_training_episode(self):
        for c in self.calculate()['cases']:
            self.assertNotIn(c['episode_id'],c['selected_training_episode_ids'])
            self.assertTrue(all(t<=c['reconstructed_prediction_at'] for t in c['selected_training_truth_available_at']))

    def test_misleading_observed_trend_reports_worse_error_without_selecting_winner(self):
        # Display turnover is only a covariate, not a causal call-rate promise.
        import sqlite3
        from sushiwait.remote import validate_record
        from sushiwait.remoteintake import _digest
        with sqlite3.connect(self.remote_path) as db:
            runs=dict(db.execute('SELECT id,run_id FROM remote_samples'))
        for index in range(20,27):
            self.mutate(lambda r:r['queries']['groupqueues']['payload']['queues'].update(storeQueue=['999','888']),row=index)
            # Mutating a synthetic record requires a matching valid receipt.
            self.mutate(lambda r:r['local_intake'].update(record_sha256=_digest(
                validate_record(r),runs[index],r['local_intake']['received_at'])),row=index)
        result=self.calculate()
        c=self.case(result)
        group=_paired([c],'realtime_only')[2]
        self.assertEqual(group['definitely_higher_absolute_error_cases'],1)
        self.assertFalse(result['best_variant_selected'])

    def test_no_history_and_insufficient_neighbors_are_counted_not_dropped(self):
        result=self.calculate(plan={**self.plan,'minimum_episodes':100,'neighbors':100})
        first=result['cases'][0]
        self.assertFalse(first['variants']['history_only']['research_forecast_available'])
        self.assertTrue(all(not c['variants']['realtime_only']['research_forecast_available'] for c in result['cases']))
        for group in result['evaluations_by_elapsed_seconds']:
            self.assertEqual(group['variants']['realtime_only']['attempted_cases'],12)
            self.assertEqual(group['variants']['realtime_only']['score_unavailable_counts'],{'no_eligible_distribution':12})

    def test_later_queue_and_claim_do_not_change_prior_case(self):
        before=self.case()
        self.write([(3900,['700'],15)])
        self.add(self.episode(3800,60,90),3950)
        after=self.case()
        for k in ('model_input_sha256','historical_input_sha256','admitted_queue_inputs_sha256'):
            self.assertEqual(before[k],after[k])
        for name in before['variants']:
            self.assertEqual(before['variants'][name]['quantiles_us'],after['variants'][name]['quantiles_us'])

    def test_target_queue_late_receipt_is_not_retroactive(self):
        before=self.case()
        self.write([(1995,['900'],42)],receipt=3900)
        after=self.case()
        self.assertEqual(before['model_input_sha256'],after['model_input_sha256'])

    def test_saved_context_is_never_claimed_running_and_live_default_still_rejects(self):
        from sushiwait.realtimebacktest import _context
        fp={k:v for k,v in self.plan.items() if k not in
            ('minimum_samples','neighbors','minimum_episodes','maximum_elapsed_difference_seconds',
             'maximum_standardized_distance','realtime_weight_ppm')}
        with OutcomeIntakeStore(self.source_path,read_only=True) as source, \
                OutcomeReviewStore(self.review_path,read_only=True) as reviews,RemoteStore(self.remote_path,read_only=True) as remote:
            ds=build_feature_dataset(source=source,reviews=reviews,remote=remote,plan=fp)
            row=next(r for r in ds['rows'] if r['episode_id']==self.fast_target['episode_id'] and r['features']['elapsed_seconds']==0)
            ctx=_context(row,self.plan)
            self.assertFalse(ctx['collector_running']);self.assertEqual(ctx['latest_queue_origin'],'saved_history')
            target={'schema_version':1,'as_of':stamp(2000),'data_origin':'synthetic','api_profile':'miniapp_gateway',
                'store_id':'900001','queue_type':'ordinary','party_size':2,'table_type':'unknown',
                'mode':'new_join','minimum_samples':1,'target_episode_id':self.fast_target['episode_id']}
            args=dict(source=source,reviews=reviews,remote=remote,plan=target,feature_plan={**fp,'as_of':stamp(2000)},
                context=ctx,neighbors=2,minimum_episodes=2,now=BASE+timedelta(seconds=2000))
            self.assertFalse(build_realtime_fusion(**args)['research_realtime_model_fitted'])
            self.assertTrue(build_realtime_fusion(**args,sealed_replay=True)['research_realtime_model_fitted'])
            with self.assertRaises(RealtimeBacktestError):validate_plan({**self.plan,'neighbors':0})
            from sushiwait.realtimefusion import RealtimeFusionError
            with self.assertRaises(RealtimeFusionError):build_realtime_fusion(**args,sealed_replay=True,ai_blend_ppm=1)
            with self.assertRaises(RealtimeFusionError):build_realtime_fusion(**{**args,'context':{**ctx,
                'collector_running':True,'latest_queue_origin':'worker_commit'}},sealed_replay=True)
        self.assertTrue(all(not c['context_collector_running'] for c in self.calculate()['cases']))

    def test_remaining_quantiles_are_residual_once_and_groups_do_not_pool_updates(self):
        result=self.calculate()
        a=self.case(result)['variants']['realtime_only']['quantiles_us']['p50']
        b=self.case(result,elapsed=30)['variants']['realtime_only']['quantiles_us']['p50']
        self.assertEqual(a['lower_us']-b['lower_us'],30_000_000)
        self.assertEqual(a['upper_us']-b['upper_us'],30_000_000)
        self.assertFalse(result['elapsed_groups_are_independent_samples'])
        self.assertTrue(all(e['independent_target_episodes']==12 for e in result['evaluations_by_elapsed_seconds']))

    def test_deterministic_replay_hash_ignores_random_evaluation_ids(self):
        self.assertEqual(self.calculate()['replay_sha256'],self.calculate()['replay_sha256'])

    def test_unknown_history_receipts_and_failures_reduce_realtime_availability(self):
        self.mutate(lambda r:r.pop('local_intake'),row=24)
        self.assertFalse(self.case()['realtime_model_fitted'])

    def test_source_scan_case_limits_and_active_writer_are_preserved(self):
        for kwargs in ({'max_observations':2},{'max_revisions':1},{'plan':{**self.plan,'max_cases':1}}):
            with self.assertRaises(RealtimeBacktestError):self.calculate(**kwargs)
        with RemoteStore(self.remote_path):
            with self.assertRaises(OSError):self.calculate()

    def test_invalid_plan_rejected_before_any_source_access(self):
        for bad in ({**self.plan,'neighbors':False},{**self.plan,'minimum_episodes':3,'neighbors':2},
                    {**self.plan,'realtime_weight_ppm':1000001},{**self.plan,'as_of':stamp(5000)}):
            with self.assertRaises(RealtimeBacktestError):validate_plan(bad,now=BASE+timedelta(seconds=4000))

    def test_actual_cli_new_private_artifact_no_overwrite_or_episode_output(self):
        self.input_path.write_text(json.dumps(self.plan));self.input_path.chmod(0o600)
        args=['realtime-backtest','--source-db',str(self.source_path),'--reviews-db',str(self.review_path),
              '--remote-db',str(self.remote_path),'--input',str(self.input_path),'--output',str(self.output_path)]
        with patch('socket.socket',side_effect=AssertionError('network')),contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(args),0)
        emitted=json.loads(out.getvalue());self.assertTrue(emitted['paired_comparison_available'])
        self.assertNotIn(self.fast_target['episode_id'],out.getvalue())
        self.assertEqual(self.output_path.stat().st_mode&0o777,0o600)
        before=self.output_path.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()) as out:self.assertEqual(main(args),1)
        self.assertEqual(json.loads(out.getvalue())['error_code'],'realtime_backtest_output_exists')
        self.assertEqual(before,self.output_path.read_bytes())


class PairedIntervalTests(unittest.TestCase):
    def result(self,history,other,lo,hi):
        def r(point):return {'point_call_at':stamp(point),'observed_call_lower':stamp(lo),'observed_call_upper':stamp(hi)}
        return _paired([{'variants':{'history_only':{'evaluation_record':r(history)},
            'realtime_only':{'evaluation_record':r(other)}}}],'realtime_only')[2]

    def test_shared_truth_tight_bounds_not_independent_error_ranges(self):
        r=self.result(10,20,0,30)
        self.assertEqual(r['mean_absolute_error_change_seconds'],{'lower':-10.0,'upper':10.0})
        self.assertEqual(r['equal_or_uncertain_absolute_error_cases'],1)

    def test_definite_improvement_and_worsening_both_retained(self):
        self.assertEqual(self.result(100,15,10,20)['definitely_lower_absolute_error_cases'],1)
        self.assertEqual(self.result(15,100,10,20)['definitely_higher_absolute_error_cases'],1)

    def test_identical_predictors_zero_and_empty_cohort_unknown(self):
        self.assertEqual(self.result(15,15,10,20)['mean_absolute_error_change_seconds'],{'lower':0.0,'upper':0.0})
        self.assertEqual(_paired([],'realtime_only')[2]['mean_absolute_error_change_seconds'],{'lower':None,'upper':None})


if __name__=='__main__':unittest.main()
