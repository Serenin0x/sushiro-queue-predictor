"""Inverse planning checks full distributions, strict boundaries and private sources."""
from copy import deepcopy
from datetime import timedelta
from fractions import Fraction
from itertools import product
import contextlib, io, json, unittest
from unittest.mock import patch
from sushiwait.idealplanning import _bounds, score_issue_grid, build_ideal_plan, IdealPlanError
from sushiwait.cli import main
from sushiwait.intake import OutcomeIntakeStore
from sushiwait.reviews import OutcomeReviewStore
import test_fusion as fusion
import test_baseline as baseline
import test_history_fusion as history

MINUTE=60_000_000


def plan(offset=0):
    return {'schema_version':1,'as_of':fusion.stamp(),'data_origin':'synthetic','api_profile':'miniapp_gateway',
        'store_id':'900001','queue_type':'ordinary','party_size':None,'table_type':'unknown','minimum_samples':1,
        'target_episode_id':None,'mode':'ideal_time','desired_arrival_at':fusion.stamp(1800),
        'call_offset_minutes':offset,'candidate_issue_times':[fusion.stamp(s) for s in (0,300,600)]}


def model(rows):
    p=fusion.plan();p['schema_version']=2;p['ai_blend_ppm']=0
    p['candidates']=[{'candidate_id':'history','interval_sample':{'elapsed_us':None,'intervals':rows}}]
    p['prior_weights_ppm']={'history':1_000_000};return p


def entries(models,p=None):
    p=p or plan()
    return [{'candidate_index':i,'candidate_issue_at':t,'fusion_plan':m}
            for i,(t,m) in enumerate(zip(p['candidate_issue_times'],models))]


def ratio(value):return Fraction(value['numerator'],value['denominator'])


class IdealGridMathTests(unittest.TestCase):
    def score(self,models,*,p=None,**options):
        p=p or plan();return score_issue_grid(p,entries(models,p),now=fusion.BASE,**options)

    def test_full_mass_selects_broad_success_over_an_accurate_but_rare_median(self):
        one=model([[15*MINUTE,15*MINUTE,49],[30*MINUTE,30*MINUTE,2],[60*MINUTE,60*MINUTE,49]])
        two=model([[5*MINUTE,5*MINUTE,3],[25*MINUTE,25*MINUTE,7]])
        result=self.score([one,two,None],late_minutes=0,maximum_early_mass_ppm=500000,maximum_late_mass_ppm=500000)
        self.assertEqual(result['selected_candidate_index'],1)
        self.assertEqual(len(result['candidates']),3)
        self.assertEqual(result['candidates'][2]['state'],'insufficient_model')
        self.assertEqual(ratio(result['candidates'][0]['empirical_mass_bounds']['within_window']['lower']),Fraction(1,50))

    def test_positive_and_negative_offsets_are_not_uncertainty_windows(self):
        for offset,minutes in [(10,40),(-10,20)]:
            result=self.score([model([[minutes*MINUTE,minutes*MINUTE,1]]),None,None],p=plan(offset),late_minutes=0)
            from sushiwait.outcomes import _time
            self.assertEqual(_time(result['target_call_at']),fusion.BASE+timedelta(minutes=minutes))
            self.assertEqual(result['selected_candidate_index'],0)
            self.assertEqual(result['preferences']['early_minutes'],0)

    def test_closed_window_includes_both_boundaries_but_early_and_late_are_strict(self):
        result=self.score([model([[29*MINUTE,29*MINUTE,1],[31*MINUTE,31*MINUTE,1]]),None,None],early_minutes=1,late_minutes=1)
        masses=result['candidates'][0]['empirical_mass_bounds']
        self.assertEqual(ratio(masses['within_window']['lower']),1)
        self.assertEqual(ratio(masses['early']['upper']),0);self.assertEqual(ratio(masses['late']['upper']),0)

    def test_unknown_upper_end_cannot_be_certified_on_time(self):
        result=self.score([model([[30*MINUTE,None,1]]),None,None],late_minutes=0)
        masses=result['candidates'][0]['empirical_mass_bounds']
        self.assertEqual(ratio(masses['within_window']['lower']),0)
        self.assertEqual(ratio(masses['within_window']['upper']),1)
        self.assertEqual(ratio(masses['late']['upper']),1)
        self.assertIsNone(result['selected_candidate_index'])

    def test_nonmonotone_future_wait_models_and_all_missing_candidates_are_preserved(self):
        result=self.score([model([[40*MINUTE,40*MINUTE,1]]),model([[40*MINUTE,40*MINUTE,1]]),model([[20*MINUTE,20*MINUTE,1]])],late_minutes=0)
        self.assertEqual(result['selected_candidate_index'],2);self.assertFalse(result['monotonicity_assumed'])
        result=self.score([None,None,None]);self.assertEqual(len(result['candidates']),3)
        self.assertEqual(result['unavailable_reason'],'insufficient_models')

    def test_exact_third_is_not_rounded_into_a_threshold_and_preferences_are_hash_bound(self):
        models=[model([[0,0,2],[30*MINUTE,30*MINUTE,1]]),None,None]
        one=self.score(models,late_minutes=0,minimum_window_mass_ppm=333333,maximum_early_mass_ppm=1000000)
        two=self.score(models,late_minutes=0,minimum_window_mass_ppm=333334,maximum_early_mass_ppm=1000000)
        self.assertEqual(one['selected_candidate_index'],0);self.assertIsNone(two['selected_candidate_index'])
        self.assertNotEqual(one['input_sha256'],two['input_sha256'])

    def test_bounds_contain_every_small_latent_assignment(self):
        intervals=[(0,2),(1,3),(2,4)]
        candidate={'candidate_id':'history','interval_sample':{'elapsed_us':None,'intervals':[[l,u,1] for l,u in intervals]}}
        for lower,upper in [(0,0),(1,2),(2,3),(4,4),(5,6),(-2,-1)]:
            bounds=_bounds(candidate,lower,upper)
            for values in product(*(range(l,u+1) for l,u in intervals)):
                actual={'early':sum(v<lower for v in values),'within_window':sum(lower<=v<=upper for v in values),'late':sum(v>upper for v in values)}
                for key,n in actual.items():
                    self.assertLessEqual(bounds[key][0],Fraction(n,3));self.assertGreaterEqual(bounds[key][1],Fraction(n,3))

    def test_complete_validation_precedes_any_fusion_or_partial_selection(self):
        good=entries([model([[30*MINUTE,30*MINUTE,1]])]*3)
        for mutate in [lambda e:e.pop(),lambda e:e[2].update(candidate_index=True),
                       lambda e:e[2]['fusion_plan']['public_context'].update(observation_revision=4),
                       lambda e:e[2]['fusion_plan'].update(prediction_target='remaining')]:
            value=deepcopy(good);mutate(value)
            with patch('sushiwait.idealplanning.fuse',side_effect=AssertionError('premature fusion')),self.assertRaises(IdealPlanError):
                score_issue_grid(plan(),value,now=fusion.BASE)

    def test_stale_context_and_passed_issue_times_do_not_generate_actionable_suggestions(self):
        p=plan();p['candidate_issue_times']=[fusion.stamp()]
        rows=entries([model([[30*MINUTE,30*MINUTE,1]])],p)
        for now,reason in [(fusion.BASE+timedelta(seconds=1),'issue_times_passed'),(fusion.BASE+timedelta(seconds=61),'issue_times_passed')]:
            result=score_issue_grid(p,rows,now=now,late_minutes=0)
            self.assertIsNone(result['selected_candidate_index']);self.assertEqual(result['unavailable_reason'],reason)
        p['candidate_issue_times']=[fusion.stamp(300)];rows=entries([model([[25*MINUTE,25*MINUTE,1]])],p)
        result=score_issue_grid(p,rows,now=fusion.BASE+timedelta(seconds=61),late_minutes=0)
        self.assertIsNone(result['selected_candidate_index']);self.assertEqual(result['unavailable_reason'],'current_context_unavailable')

    def test_shared_public_advice_changes_each_distribution_without_reading_a_key(self):
        p=plan();p['candidate_issue_times']=[fusion.stamp(300),fusion.stamp(600)]
        one=fusion.plan();one['ai_blend_ppm']=1000000
        one['candidates'][0]['atoms']=[fusion.atom(25*MINUTE,25*MINUTE)]
        one['candidates'][1]['atoms']=[fusion.atom(0,0)]
        two=deepcopy(one);two['candidates'][0]['atoms']=[fusion.atom(20*MINUTE,20*MINUTE)]
        for advice in (None,fusion.advice(one)):
            with patch('socket.socket',side_effect=AssertionError('network')),patch('sushiwait.deepseek._key',side_effect=AssertionError('key')):
                result=score_issue_grid(p,entries([one,two],p),advice=advice,now=fusion.BASE+timedelta(seconds=2),late_minutes=0)
            self.assertEqual(result['selected_candidate_index'],0 if advice is None else None)
            self.assertEqual(result['ai_numerical_influence_applied'],advice is not None)

    def test_complete_288_candidate_grid_keeps_past_winner_but_selects_future_candidate(self):
        p=plan();p['candidate_issue_times']=[fusion.stamp(i) for i in range(288)]
        models=[model([[(1800-i)*1_000_000,(1800-i)*1_000_000,1]]) for i in range(288)]
        result=score_issue_grid(p,entries(models,p),now=fusion.BASE+timedelta(seconds=2),late_minutes=0)
        self.assertEqual(result['feasible_candidate_indices'],list(range(288)))
        self.assertEqual(result['future_feasible_candidate_indices'],list(range(2,288)))
        self.assertEqual(result['selected_candidate_index'],2)
        self.assertEqual(len(result['candidates']),288)


class IdealReviewedBridgeTests(unittest.TestCase):
    setUp=history.HistoryBridgeTests.setUp
    episode=history.HistoryBridgeTests.episode
    add=history.HistoryBridgeTests.add
    receive=history.HistoryBridgeTests.receive
    ideal=history.HistoryBridgeTests.ideal
    context=history.HistoryBridgeTests.context

    def test_private_reviewed_grid_keeps_all_candidates_and_sources_unchanged(self):
        p=self.ideal();before={f:f.read_bytes() for f in (self.source_path,self.review_path)}
        with OutcomeIntakeStore(self.source_path,read_only=True) as source,OutcomeReviewStore(self.review_path,read_only=True) as reviews:
            result=build_ideal_plan(source=source,reviews=reviews,plan=p,context=self.context(p),now=baseline.BASE+timedelta(seconds=30),late_minutes=2)
        self.assertEqual(len(result['candidates']),3);self.assertEqual(result['selected_candidate_index'],2)
        self.assertEqual(result['models_generated'],['history']);self.assertFalse(result['realtime_regime_models_fitted'])
        self.assertEqual({f:f.read_bytes() for f in before},before)
        self.assertFalse(result['eta_available']);self.assertEqual(result['verified_training_labels'],0)

    def test_cli_writes_private_complete_result_without_external_actions_or_overwrite(self):
        p=self.ideal();context_file=self.parent/'output/context.json'
        for path,value in [(self.input_path,p),(context_file,self.context(p))]:path.write_text(json.dumps(value));path.chmod(0o600)
        args=['ideal-time-research','--source-db',str(self.source_path),'--reviews-db',str(self.review_path),
              '--input',str(self.input_path),'--context-file',str(context_file),'--output',str(self.output_path)]
        output=io.StringIO()
        with patch('socket.socket',side_effect=AssertionError('network')),contextlib.redirect_stdout(output):
            self.assertEqual(main(args),0);before=self.output_path.read_bytes();self.assertEqual(main(args),1)
        self.assertEqual(self.output_path.read_bytes(),before);self.assertEqual(self.output_path.stat().st_mode&0o777,0o600)
        self.assertEqual(len(json.loads(before)['candidates']),3);self.assertNotIn(str(self.parent),output.getvalue())
        self.assertNotIn(self.value['episode_id'],output.getvalue())
