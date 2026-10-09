"""Actual private sources, episode balance and numeric realtime/AI effects.

All episodes and queue responses are explicitly synthetic. These checks do
not assert real waiting accuracy, true no-show rate or authenticated labels.
"""
import contextlib
from copy import deepcopy
from datetime import timedelta
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_features as features_fixture
import test_fusion as fusion_fixture
import test_tracking as tracking_fixture
from sushiwait.cli import main
from sushiwait.fusion import fuse,public_request,validate_plan,FusionError
from sushiwait.intake import OutcomeIntakeStore
from sushiwait.remote import RemoteStore
from sushiwait.reviews import OutcomeReviewStore
from sushiwait.realtimefusion import build_realtime_fusion,RealtimeFusionError
from sushiwait.tracking import create_session,TrackingSession
from sushiwait.trackerloop import TrackingCoordinator,TrackerLoopError
from sushiwait.remoteservice import LiveRemoteView

BASE=features_fixture.BASE
stamp=features_fixture.stamp


class DirectConditionalTests(unittest.TestCase):
    def value(self):
        p=fusion_fixture.plan();p['schema_version']=2
        p.update(prediction_target='remaining',conditioning='call_not_observed_after_elapsed')
        p['candidates']=[{'candidate_id':'realtime','conditional_interval_sample':{
            'conditioned_elapsed_us':300,'intervals':[[100,110,1],[200,210,1],[300,310,1]]}}]
        p['prior_weights_ppm']={'realtime':1000000};return p

    def test_direct_remaining_is_not_survival_conditioned_twice(self):
        r=fuse(self.value(),now=fusion_fixture.BASE)
        self.assertEqual(r['wait_quantile_envelopes_us']['p50'],{'lower_us':200,'upper_us':210})
        self.assertFalse(r['survival_conditioning_applied_here'])
        self.assertTrue(r['direct_conditional_samples_present'])

    def test_exact_thirds_mix_with_static_survival_distribution(self):
        p=self.value();p['candidates'].append({'candidate_id':'history','interval_sample':{
            'elapsed_us':300,'intervals':[[310,320,1],[400,420,2]]}})
        p['prior_weights_ppm']={'history':500000,'realtime':500000}
        r=fuse(p,now=fusion_fixture.BASE)
        self.assertTrue(r['survival_conditioning_applied_here'])
        self.assertEqual(r['wait_quantile_envelopes_us']['p50'],{'lower_us':100,'upper_us':120})

    def test_direct_payload_and_elapsed_stay_private(self):
        p=self.value();r=public_request(p,now=fusion_fixture.BASE)
        text=json.dumps(r)
        self.assertNotIn('conditioned_elapsed_us',text)
        self.assertNotIn('intervals',text)
        self.assertEqual(r['candidate_ids'],['realtime'])

    def test_direct_distributions_reject_new_join_schema1_and_conflicting_elapsed(self):
        p=self.value()
        changes=[dict(schema_version=1),dict(prediction_target='new_join_total',conditioning='new_join')]
        for change in changes:
            bad=deepcopy(p);bad.update(change)
            with self.assertRaises(FusionError):validate_plan(bad,now=fusion_fixture.BASE)
        p['candidates'].append({'candidate_id':'history','interval_sample':{
            'elapsed_us':301,'intervals':[[400,420,1]]}})
        p['prior_weights_ppm']={'history':500000,'realtime':500000}
        with self.assertRaises(FusionError):validate_plan(p,now=fusion_fixture.BASE)


class RealtimeModelTests(unittest.TestCase):
    episode=features_fixture.FeatureDatasetTests.episode
    add=features_fixture.FeatureDatasetTests.add
    receive_review=features_fixture.FeatureDatasetTests.receive_review
    write=features_fixture.FeatureDatasetTests.write

    def setUp(self):
        features_fixture.FeatureDatasetTests.setUp(self)
        self.write([(t,['999','888'],10) for t in (200,230,260,290,310)])
        self.write([(t,[str(i),'888'],10) for i,t in enumerate((500,530,560,590,610))])
        self.slow=[];self.fast=[]
        for issued in (320,330,340,350):
            ep=self.episode(issued,900,960);self.add(ep,2000);self.slow.append(ep)
        for issued in (620,630,640,650):
            ep=self.episode(issued,120,180);self.add(ep,2000);self.fast.append(ep)
        self.target_plan={'schema_version':1,'as_of':stamp(2300),'data_origin':'synthetic',
            'api_profile':'miniapp_gateway','store_id':'900001','queue_type':'ordinary',
            'party_size':2,'table_type':'unknown','mode':'new_join','minimum_samples':1,'target_episode_id':None}
        self.plan.update(as_of=stamp(2300),elapsed_seconds=[0])
        self.context=fusion_fixture.plan()['public_context']
        self.context.update(as_of=stamp(2300),expires_at=stamp(2360),window_seconds=120,
            latest_queue_received_at=stamp(2295),latest_count_received_at=stamp(2296))
        self.context['features']=[{'feature_id':key,'value':value,'available_at':stamp(2300)}
            for key,value in {'ordinary_removed_labels':4,'ordinary_comparable_pairs':4,
                'ordinary_observed_milliseconds':110000,'reported_count_raw':10,
                'groupqueues_failures':0}.items()]
        self.now=BASE+timedelta(seconds=2300)

    def calculate(self,**options):
        kw=dict(source=None,reviews=None,remote=None,plan=self.target_plan,feature_plan=self.plan,
            context=self.context,neighbors=2,minimum_episodes=2,now=self.now)
        kw.update(options)
        with OutcomeIntakeStore(self.source_path,read_only=True) as source, \
                OutcomeReviewStore(self.review_path,read_only=True) as reviews, \
                RemoteStore(self.remote_path,read_only=True) as remote, \
                patch('socket.socket',side_effect=AssertionError('network')):
            kw.update(source=source,reviews=reviews,remote=remote)
            return build_realtime_fusion(**kw)

    def context_with(self,**values):
        context=deepcopy(self.context)
        for f in context['features']:
            if f['feature_id'] in values:f['value']=values[f['feature_id']]
        return context

    def test_real_three_private_sources_fit_numeric_realtime_distribution(self):
        before=[p.read_bytes() for p in (self.source_path,self.review_path,self.remote_path)]
        result=self.calculate()
        self.assertTrue(result['research_realtime_model_fitted'])
        self.assertTrue(result['realtime_candidate_added'])
        self.assertEqual([c['candidate_id'] for c in result['fusion_plan']['candidates']],['history','realtime'])
        self.assertGreaterEqual(result['selected_independent_episodes'],2)
        self.assertFalse(result['eta_available'] or result['training_eligible'] or result['model_performance_verified'])
        self.assertEqual(result['verified_training_labels'],0)
        self.assertEqual(before,[p.read_bytes() for p in (self.source_path,self.review_path,self.remote_path)])

    def test_observed_speed_changes_the_actual_wait_quantiles(self):
        fast=self.calculate();slow=self.calculate(context=self.context_with(ordinary_removed_labels=0))
        self.assertLess(fast['research_quantiles_us']['p50']['upper_us'],
            slow['research_quantiles_us']['p50']['lower_us'])
        self.assertEqual(fast['history']['historical_input_sha256'],slow['history']['historical_input_sha256'])

    def test_private_training_intervals_and_episode_ids_never_enter_model_request(self):
        result=self.calculate();request=json.dumps(public_request(result['fusion_plan'],now=self.now))
        for row in result['selected_private_rows']:
            self.assertNotIn(row['episode_id'],request)
            self.assertNotIn(row['truth_source_receipt_sha256'],request)
        self.assertNotIn('intervals',request);self.assertNotIn('party_size',request)

    def test_ai_reweighting_genuinely_changes_the_two_fitted_distributions(self):
        p=self.calculate(ai_blend_ppm=1000000)['fusion_plan']
        request=public_request(p,now=self.now)
        advice={'schema_version':1,'public_input_sha256':request['public_input_sha256'],
            'observation_revision':self.context['observation_revision'],'model_version':p['model_version'],
            'generated_at':stamp(2300),'expires_at':stamp(2350),
            'weights_ppm':{'history':1000000,'realtime':0},'feature_ids':['ordinary_removed_labels'],
            'reason_code':'history_dominant'}
        result=fuse(p,advice=advice,now=self.now)
        normal=fuse(p,now=self.now)
        self.assertTrue(result['ai_numerical_influence_applied'])
        self.assertNotEqual(result['wait_quantile_envelopes_us'],normal['wait_quantile_envelopes_us'])
        self.assertFalse(result['provider_called'] or result['eta_available'])

    def test_repeated_landmarks_never_add_independent_probability_mass(self):
        result=self.calculate(feature_plan={**self.plan,'elapsed_seconds':[0,1,2,3]},
            plan={**self.target_plan,'mode':'remaining','issued_at':stamp(2298),
                'call_not_observed':True,'target_episode_id':str(uuid4())})
        ids=[r['episode_id'] for r in result['selected_private_rows']]
        self.assertEqual(len(ids),len(set(ids)))
        self.assertTrue(all(r['features']['elapsed_seconds']==2 for r in result['selected_private_rows']))

    def test_target_episode_is_excluded_in_every_landmark(self):
        excluded=self.fast[0]['episode_id']
        result=self.calculate(plan={**self.target_plan,'target_episode_id':excluded})
        self.assertNotIn(excluded,[r['episode_id'] for r in result['selected_private_rows']])

    def test_future_receipts_do_not_change_model_or_earlier_numeric_result(self):
        before=self.calculate();self.add(self.episode(640,60,90),2400)
        after=self.calculate()
        self.assertEqual(before['model_input_sha256'],after['model_input_sha256'])
        self.assertEqual(before['research_quantiles_us'],after['research_quantiles_us'])

    def test_future_queries_do_not_change_training_features(self):
        before=self.calculate();self.write([(2500,['999999'],99)])
        after=self.calculate()
        self.assertEqual(before['model_input_sha256'],after['model_input_sha256'])

    def test_one_episode_cannot_satisfy_minimum_through_repeated_rows(self):
        result=self.calculate(minimum_episodes=20,neighbors=20)
        self.assertFalse(result['research_realtime_model_fitted'])
        self.assertEqual(result['unavailable_reason'],'insufficient_independent_neighbor_episodes')
        self.assertEqual(len(result['fusion_plan']['candidates']),1)

    def test_incompatible_cadence_is_not_used_as_waiting_evidence(self):
        result=self.calculate(context=self.context_with(ordinary_comparable_pairs=20))
        self.assertFalse(result['research_realtime_model_fitted'])

    def test_missing_count_is_a_separate_feature_mask(self):
        result=self.calculate(context=self.context_with(reported_count_raw=None))
        self.assertFalse(result['research_realtime_model_fitted'])

    def test_insufficient_window_coverage_and_queue_failure_do_not_fit(self):
        for change in ({'ordinary_observed_milliseconds':1000},{'groupqueues_failures':1}):
            r=self.calculate(context=self.context_with(**change))
            self.assertEqual(r['unavailable_reason'],'current_trend_unavailable')

    def test_stale_and_saved_context_fall_back_to_history(self):
        for change in ({'latest_queue_origin':'saved_history'},{'collector_running':False},
                {'latest_queue_received_at':stamp(2000)}):
            r=self.calculate(context={**self.context,**change})
            self.assertEqual(r['unavailable_reason'],'current_trend_unavailable')
            self.assertFalse(r['realtime_candidate_added'])

    def test_no_neighbors_on_another_queue_or_table(self):
        for change in ({'table_type':'booth'},{'party_size':3}):
            r=self.calculate(plan={**self.target_plan,**change})
            self.assertFalse(r['research_prediction_available'])

    def test_fractional_elapsed_updates_without_false_microsecond_extrapolation(self):
        plan={**self.target_plan,'mode':'remaining','issued_at':stamp(1999.5),
            'call_not_observed':True,'target_episode_id':str(uuid4())}
        r=self.calculate(plan=plan,feature_plan={**self.plan,'elapsed_seconds':[0,300]})
        self.assertTrue(r['realtime_candidate_added'])
        candidate=next(c for c in r['fusion_plan']['candidates'] if c['candidate_id']=='realtime')
        self.assertEqual(candidate['conditional_interval_sample']['conditioned_elapsed_us'],300500000)
        self.assertEqual(candidate['conditional_interval_sample']['intervals'][0][0],599500000)
        self.assertTrue(r['nearby_landmark_age_transport_applied'])
        fast_ids={e['episode_id'] for e in self.fast}
        self.assertFalse(fast_ids&{v['episode_id'] for v in r['selected_private_rows']})

    def test_kth_distance_ties_are_all_included(self):
        context=self.context_with(ordinary_removed_labels=3,ordinary_observed_milliseconds=80000,ordinary_comparable_pairs=3)
        r=self.calculate(context=context,neighbors=1,minimum_episodes=1)
        self.assertGreater(r['selected_independent_episodes'],1)
        self.assertEqual(len(r['selected_private_rows']),len({v['episode_id'] for v in r['selected_private_rows']}))

    def test_ranking_does_not_use_the_waiting_time_outcome(self):
        before=self.calculate()
        chosen={v['episode_id'] for v in before['selected_private_rows']}
        for original in self.fast:
            revised=self.episode(int((__import__('datetime').datetime.fromisoformat(original['events'][0]['event_time_lower'].replace('Z','+00:00'))-BASE).total_seconds()),900,960)
            revised.update(episode_id=original['episode_id'],revision=2,supersedes_revision=1)
            self.add(revised,2050)
        after=self.calculate()
        self.assertEqual(chosen,{v['episode_id'] for v in after['selected_private_rows']})
        self.assertNotEqual(before['research_quantiles_us'],after['research_quantiles_us'])

    def test_outside_training_support_returns_history_instead_of_extrapolation(self):
        r=self.calculate(context=self.context_with(ordinary_removed_labels=1000000))
        self.assertFalse(r['research_realtime_model_fitted'])
        self.assertEqual(len(r['fusion_plan']['candidates']),1)

    def test_options_are_bound_to_private_model_input_identity(self):
        one=self.calculate();two=self.calculate(realtime_weight_ppm=0)
        self.assertNotEqual(one['model_input_sha256'],two['model_input_sha256'])

    def test_aggregate_only_changes_do_not_change_fitted_ordinary_wait(self):
        import sqlite3
        from sushiwait.remote import validate_record
        from sushiwait.remoteintake import _digest
        before = self.calculate()
        self.assertTrue(before['research_realtime_model_fitted'])
        with sqlite3.connect(self.remote_path) as database:
            rows = list(database.execute('SELECT id,run_id FROM remote_samples'))
        for index, run in rows:
            features_fixture.FeatureDatasetTests.mutate(self,
                lambda row: row['queries']['groupqueues']['payload']['queues'].update(storeQueue=['7000']),
                row=index)
            features_fixture.FeatureDatasetTests.mutate(self,
                lambda row: row['local_intake'].update(record_sha256=_digest(
                    validate_record(row), run, row['local_intake']['received_at'])), row=index)
        after = self.calculate()
        self.assertTrue(after['research_realtime_model_fitted'])
        self.assertEqual(before['research_quantiles_us'], after['research_quantiles_us'])

    def test_unknown_count_is_not_implicitly_zero(self):
        missing=self.calculate(context=self.context_with(reported_count_raw=None))
        zero=self.calculate(context=self.context_with(reported_count_raw=0))
        self.assertNotEqual(missing['model_input_sha256'],zero['model_input_sha256'])

    def test_invalid_options_do_not_become_silent_defaults(self):
        for options in ({'neighbors':True},{'neighbors':1,'minimum_episodes':2},
                {'realtime_weight_ppm':1000001},{'maximum_standardized_distance':0}):
            with self.assertRaises(RealtimeFusionError):self.calculate(**options)

    def test_scope_and_window_mismatch_are_rejected(self):
        for change in ({'store_id':'900002'},{'window_seconds':121}):
            with self.assertRaises(RealtimeFusionError):self.calculate(feature_plan={**self.plan,**change})

    def test_full_history_limit_is_preserved_and_not_tail_sampled(self):
        with self.assertRaises(RealtimeFusionError):self.calculate(max_observations=2)

    def test_actual_cli_keeps_output_private_and_does_not_overwrite(self):
        inputs={'plan':self.target_plan,'features':self.plan,'context':self.context}
        paths={}
        for name,value in inputs.items():
            p=self.parent/'output'/f'{name}.json';p.write_text(json.dumps(value));p.chmod(0o600);paths[name]=p
        argv=['realtime-fusion-research','--source-db',str(self.source_path),'--reviews-db',str(self.review_path),
            '--remote-db',str(self.remote_path),'--input',str(paths['plan']),'--feature-plan',str(paths['features']),
            '--context-file',str(paths['context']),'--output',str(self.output_path),'--neighbors','2','--minimum-episodes','2']
        with contextlib.redirect_stdout(io.StringIO()) as out,patch('socket.socket',side_effect=AssertionError('network')), \
                patch('sushiwait.features._clock',return_value=self.now):
            status=main(argv)
        self.assertEqual(status,0,out.getvalue())
        self.assertTrue(json.loads(out.getvalue())['research_realtime_model_fitted'])
        self.assertEqual(self.output_path.stat().st_mode&0o777,0o600)
        saved=self.output_path.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()) as out,patch('sushiwait.features._clock',return_value=self.now):
            self.assertEqual(main(argv),1)
        self.assertEqual(saved,self.output_path.read_bytes())
        self.assertNotIn(self.target['episode_id'],out.getvalue())

    def test_actual_coordinator_runs_learner_and_cold_start_fallback_in_worker(self):
        state=self.parent/'state';state.mkdir(mode=0o700)
        ticket=tracking_fixture.ticket(issued_at=stamp(2000),created_at=stamp(2300),desired_arrival_at=None)
        create_session(ticket,directory=state,now=self.now)
        view=LiveRemoteView(['900001'],stale_after_seconds=360)
        view.publish(features_fixture.queue_record(2295,['20','21'],10))
        c=TrackingCoordinator([state],stores=['900001'],
            reader=lambda store:view.tracking_projection(store,now=self.now,service_state='running',worker_alive=True),
            source_db=self.source_path,reviews_db=self.review_path,sealed_remote_db=self.remote_path,
            landmark_seconds=[0,300],clock=lambda:self.now)
        self.addCleanup(c.close)
        c.cycle()
        for future in list(c.futures.values()):future.result(timeout=3)
        c._reap()
        self.assertEqual(c.counts['failed_jobs'],0)
        self.assertEqual(c.counts['predictions_committed'],1)
        with TrackingSession(state) as session:r=session._read('prediction-0001.json')
        self.assertEqual(r['realtime_research']['unavailable_reason'],'current_trend_unavailable')
        self.assertTrue(r['research_prediction_available'])
        self.assertFalse(r['eta_available'])

    def test_actual_coordinator_fits_and_commits_direct_remaining_in_real_thread(self):
        state=self.parent/'state';state.mkdir(mode=0o700)
        ticket=tracking_fixture.ticket(issued_at=stamp(2000),created_at=stamp(2300),desired_arrival_at=None)
        create_session(ticket,directory=state,now=self.now)
        view=LiveRemoteView(['900001'],stale_after_seconds=360)
        for i,t in enumerate((2180,2210,2240,2270,2290)):
            view.publish(features_fixture.queue_record(t,[str(20+i),'888'],10))
        c=TrackingCoordinator([state],stores=['900001'],
            reader=lambda store:view.tracking_projection(store,now=self.now,service_state='running',worker_alive=True),
            source_db=self.source_path,reviews_db=self.review_path,sealed_remote_db=self.remote_path,
            landmark_seconds=[0,300],realtime_window_seconds=120,realtime_minimum_episodes=2,
            realtime_neighbors=2,clock=lambda:self.now)
        self.addCleanup(c.close);c.cycle()
        for future in list(c.futures.values()):future.result(timeout=3)
        c._reap()
        self.assertEqual(c.counts['failed_jobs'],0)
        self.assertEqual(c.counts['predictions_committed'],1)
        with TrackingSession(state) as session:r=session._read('prediction-0001.json')
        self.assertTrue(r['realtime_research']['research_realtime_model_fitted'])
        self.assertTrue(r['realtime_research']['realtime_candidate_added'])
        direct=next(c for c in r['fusion_plan']['candidates'] if c['candidate_id']=='realtime')
        self.assertEqual(direct['conditional_interval_sample']['conditioned_elapsed_us'],300000000)
        self.assertEqual(r['fusion']['wait_quantile_envelopes_us']['p50']['lower_us'],600000000)
        self.assertEqual(c.counts['local_projection_reads'],1)
        self.assertEqual(c.counts['provider_network_calls'],0)
        self.assertFalse(r['eta_available'])

    def test_active_queue_database_is_rejected_without_interrupting_its_writer(self):
        state=self.parent/'state';state.mkdir(mode=0o700)
        create_session(tracking_fixture.ticket(),directory=state,now=self.now)
        with RemoteStore(self.remote_path) as writer:
            with self.assertRaises(TrackerLoopError):
                TrackingCoordinator([state],stores=['900001'],reader=lambda _:None,
                    source_db=self.source_path,reviews_db=self.review_path,
                    sealed_remote_db=self.remote_path,clock=lambda:self.now)
            writer._guard()

    def test_coordinator_requires_real_distinct_sealed_sources(self):
        state=self.parent/'state';state.mkdir(mode=0o700)
        create_session(tracking_fixture.ticket(),directory=state,now=self.now)
        with self.assertRaises(TrackerLoopError):
            TrackingCoordinator([state],stores=['900001'],reader=lambda _:None,
                sealed_remote_db=self.remote_path,clock=lambda:self.now)
