"""Exact empirical/conditional mixture and real private reviewed-claim bridge."""
import contextlib
from copy import deepcopy
from datetime import timedelta
from fractions import Fraction
import io
from itertools import combinations_with_replacement, product
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from sushiwait.cli import main
from sushiwait.deepseek import run_deepseek
from sushiwait.fusion import FusionError, fuse, public_request, validate_plan
from sushiwait.historyfusion import HistoryFusionError, build_history_fusion
from sushiwait.intake import OutcomeIntakeStore
from sushiwait.outcomes import _time
from sushiwait.reviews import OutcomeReviewStore
import test_baseline as baseline
import test_fusion as fusion


def sample(rows, elapsed=None, candidate_id='history'):
    return {'candidate_id': candidate_id, 'interval_sample': {
        'elapsed_us': elapsed, 'intervals': [list(row) for row in rows]}}


def empirical(candidates, elapsed=None):
    value=fusion.plan();value['schema_version']=2;value['candidates']=candidates
    value['prior_weights_ppm']={c['candidate_id']:1000000//len(candidates) for c in candidates}
    value['ai_blend_ppm']=0
    if elapsed is not None:
        value['prediction_target']='remaining';value['conditioning']='call_not_observed_after_elapsed'
    return value


class EmpiricalCdfTests(unittest.TestCase):
    def calc(self,value):return fuse(value,now=fusion.BASE)

    def test_three_equal_samples_use_exact_thirds_not_rounded_ppm(self):
        value=empirical([sample([[1,10,1],[20,30,1],[40,50,1]])])
        q=self.calc(value)['wait_quantile_envelopes_us']
        self.assertEqual(q,{'p10':{'lower_us':1,'upper_us':10},'p50':{'lower_us':20,'upper_us':30},'p90':{'lower_us':40,'upper_us':50}})

    def test_remaining_conditions_survival_instead_of_subtracting_unconditional_median(self):
        value=empirical([sample([[60,60,1],[600,600,1]],300)],300)
        result=self.calc(value)
        self.assertEqual(result['wait_quantile_envelopes_us']['p50'],{'lower_us':300,'upper_us':300})
        self.assertTrue(result['survival_conditioning_applied_here'])
        self.assertFalse(result['eta_available'])

    def test_ambiguous_survivor_changes_denominators_and_retains_zero_infimum(self):
        result=self.calc(empirical([sample([[0,120,1],[180,240,1]],60)],60))
        self.assertEqual(result['wait_quantile_envelopes_us']['p50'],{'lower_us':0,'upper_us':180})

    def test_envelopes_cover_all_small_discrete_latent_survival_assignments(self):
        pairs=[(l,u) for l in range(4) for u in range(l,4)]
        checked=0
        for intervals in combinations_with_replacement(pairs,2):
            rows=[[l,u,intervals.count((l,u))] for l,u in sorted(set(intervals))]
            for e in range(3):
                if not any(l>e for l,u in intervals):continue
                result=self.calc(empirical([sample(rows,e)],e))['wait_quantile_envelopes_us']
                for values in product(*(range(l,u+1) for l,u in intervals)):
                    survivors=sorted(t-e for t in values if t>e)
                    for name,num,den in [('p10',1,10),('p50',1,2),('p90',9,10)]:
                        actual=survivors[(num*len(survivors)+den-1)//den-1]
                        self.assertLessEqual(result[name]['lower_us'],actual)
                        self.assertGreaterEqual(result[name]['upper_us'],actual);checked+=1
        self.assertGreater(checked,500)

    def test_two_conditional_scenarios_cover_every_assignment_with_variable_survivor_counts(self):
        sets=[[(0,3),(2,4)],[(0,2),(2,3),(3,4)]];e=1
        candidates=[sample([[l,u,1] for l,u in rows],e,key) for rows,key in zip(sets,['history','fast'])]
        bounds=self.calc(empirical(candidates,e))['wait_quantile_envelopes_us']
        for one in product(*(range(l,u+1) for l,u in sets[0])):
            for two in product(*(range(l,u+1) for l,u in sets[1])):
                a=[v-e for v in one if v>e];b=[v-e for v in two if v>e]
                points=sorted(set(a+b))
                for name,q in [('p10',Fraction(1,10)),('p50',Fraction(1,2)),('p90',Fraction(9,10))]:
                    actual=next(t for t in points if Fraction(sum(v<=t for v in a),2*len(a))+Fraction(sum(v<=t for v in b),2*len(b))>=q)
                    self.assertLessEqual(bounds[name]['lower_us'],actual);self.assertGreaterEqual(bounds[name]['upper_us'],actual)

    def test_mixing_samples_and_interval_atoms_does_not_average_quantiles(self):
        value=empirical([sample([[0,0,1]]),{'candidate_id':'fast','atoms':[fusion.atom(100,100)]}])
        self.assertEqual(self.calc(value)['wait_quantile_envelopes_us']['p50']['lower_us'],0)
        self.assertEqual(self.calc(value)['wait_quantile_envelopes_us']['p90']['lower_us'],100)

    def test_unknown_upper_support_stays_unknown_for_new_and_conditional_wait(self):
        for elapsed in (None,50):
            value=empirical([sample([[100,None,1],[200,240,1]],elapsed)],elapsed)
            result=self.calc(value);self.assertIsNone(result['wait_quantile_envelopes_us']['p90']['upper_us'])
            self.assertFalse(result['coverage_calibrated'])

    def test_counts_are_not_approximated_and_zero_weight_unknown_support_does_not_dominate(self):
        value=empirical([sample([[1,1,9999],[100,100,1]]),sample([[0,None,1]],candidate_id='fast')])
        value['prior_weights_ppm']={'history':1000000,'fast':0}
        self.assertEqual(self.calc(value)['wait_quantile_envelopes_us']['p90']['upper_us'],1)

    def test_private_samples_do_not_enter_public_hash_or_wire(self):
        value=empirical([sample([[1,10,1],[20,30,1]])]);changed=deepcopy(value)
        changed['candidates'][0]['interval_sample']['intervals'][0][2]=2
        self.assertEqual(public_request(value,now=fusion.BASE),public_request(changed,now=fusion.BASE))
        text=json.dumps(public_request(value,now=fusion.BASE))
        self.assertNotIn('interval_sample',text);self.assertNotIn('elapsed_us',text)
        self.assertEqual(public_request(value,now=fusion.BASE)['policy'],'public_empirical_interval_cdf_mixture_v2')

    def test_old_schema_and_policy_remain_compatible(self):
        value=fusion.plan();result=self.calc(value)
        self.assertEqual(result['policy'],'public_scenario_interval_mixture_v1')
        self.assertEqual(result['quantile_method'],'inverse_mixture_cdf_interval_envelopes')
        self.assertFalse(result['survival_conditioning_applied_here'])

    def test_input_order_is_canonical_and_does_not_change_sample_result(self):
        one=empirical([sample([[20,30,2],[1,10,1]])]);two=deepcopy(one)
        two['candidates'][0]['interval_sample']['intervals'].reverse()
        self.assertEqual(validate_plan(one,now=fusion.BASE),validate_plan(two,now=fusion.BASE))

    def test_wrong_schema_mode_conditioning_mismatched_elapsed_and_no_definite_survivors_reject(self):
        values=[]
        v=empirical([sample([[1,2,1]])]);v['schema_version']=1;values.append(v)
        v=empirical([sample([[1,2,1]],1)]);values.append(v)
        v=empirical([sample([[0,120,1]],60)],60);values.append(v)
        v=empirical([sample([[3,4,1]],1),sample([[3,4,1]],2,'fast')],1);values.append(v)
        v=empirical([sample([[3,4,1]],1)],1);v['conditioning']='caller_supplied_conditional';values.append(v)
        for value in values:
            with self.assertRaises(FusionError):validate_plan(value,now=fusion.BASE)

    def test_bad_counts_duplicates_bounds_floats_bool_or_oversized_support_reject(self):
        for rows in [[[1,2,True]],[[1,2,0]],[[1,2,10001]],[[2,1,1]],[[1,2.0,1]],
                     [[1,2,1],[1,2,1]],[[i,i,1] for i in range(129)]]:
            with self.assertRaises(FusionError):self.calc(empirical([sample(rows)]))

    def test_bounded_ai_weights_can_change_empirical_cdf_without_changing_private_sample(self):
        value=empirical([sample([[100,100,1]]),sample([[0,0,1]],candidate_id='fast')])
        value['ai_blend_ppm']=1000000
        advice=fusion.advice(value)
        result=fuse(value,advice=advice,now=fusion.BASE+timedelta(seconds=2))
        self.assertTrue(result['ai_numerical_influence_applied'])
        self.assertEqual(result['wait_quantile_envelopes_us']['p90']['upper_us'],0)
        self.assertFalse(result['model_performance_verified'])

    def test_single_history_scenario_does_not_read_key_or_pay_for_fixed_weight(self):
        value=empirical([sample([[1,2,1]])])
        with patch('sushiwait.deepseek._key',side_effect=AssertionError('key')) as key:
            result=run_deepseek(value,allow_paid_request=True,clock=lambda:fusion.BASE,
                               transport=lambda *args: (_ for _ in ()).throw(AssertionError('provider')))
        self.assertEqual(result['fallback_reason'],'deepseek_single_scenario');self.assertEqual(key.call_count,0)


class HistoryBridgeTests(unittest.TestCase):
    setUp=baseline.ReviewedBaselineTests.setUp
    episode=baseline.ReviewedBaselineTests.episode
    add=baseline.ReviewedBaselineTests.add
    receive=baseline.ReviewedBaselineTests.receive
    ideal=baseline.ReviewedBaselineTests.ideal

    def context(self,plan=None):
        p=self.plan if plan is None else plan
        value=fusion.plan()['public_context'];value['store_id']=p['store_id'];value['queue_type']=p['queue_type']
        value.update(as_of=p['as_of'],expires_at=(_time(p['as_of'])+timedelta(seconds=60)).isoformat(),
            latest_queue_received_at=p['as_of'],latest_count_received_at=p['as_of'])
        for f in value['features']:f['available_at']=p['as_of']
        return value

    def calculate(self,plan=None,context=None,**options):
        p=self.plan if plan is None else plan
        with OutcomeIntakeStore(self.source_path,read_only=True) as source,OutcomeReviewStore(self.review_path,read_only=True) as reviews,\
             patch('sushiwait.baseline._clock',return_value=baseline.BASE+timedelta(seconds=1000)),\
             patch('sushiwait.reviews._clock',return_value=baseline.BASE+timedelta(seconds=1000)):
            return build_history_fusion(source=source,reviews=reviews,plan=p,
                context=self.context(p) if context is None else context,**options)

    def test_complete_reviewed_interval_becomes_counted_sample_not_three_pseudo_points(self):
        result=self.calculate();entry=result['candidates'][0]
        self.assertEqual(entry['fusion_plan']['schema_version'],2)
        self.assertEqual(entry['fusion_plan']['candidates'][0]['interval_sample']['intervals'],[[600000000,660000000,1]])
        self.assertEqual(entry['history_only_fusion_quantiles_us'],entry['forecast']['wait_quantile_envelopes_us'])
        self.assertFalse(result['eta_available']);self.assertEqual(result['verified_training_labels'],0)
        self.assertFalse(result['realtime_regime_models_fitted']);self.assertEqual(result['scenario_ids_generated'],['history'])

    def test_late_source_or_review_cannot_leak_into_prior_candidate(self):
        before=self.calculate();self.add(self.episode(lower=1200,upper=1200),100)
        after=self.calculate()
        self.assertEqual(before['historical_input_sha256'],after['historical_input_sha256'])
        self.assertEqual(before['candidates'],after['candidates'])

    def test_target_episode_static_profile_and_source_scope_are_excluded(self):
        for change in [{'target_episode_id':self.value['episode_id']},{'party_size':3},{'table_type':'counter'},
                       {'api_profile':'legacy'},{'data_origin':'self_reported'}]:
            result=self.calculate({**self.plan,**change})
            self.assertFalse(result['research_candidate_available']);self.assertIsNone(result['candidates'][0]['fusion_plan'])

    def test_cold_start_preserves_missing_and_does_not_invent_prior(self):
        result=self.calculate({**self.plan,'minimum_samples':2})
        self.assertEqual(result['scenario_ids_generated'],[])
        self.assertFalse(result['research_candidate_available']);self.assertFalse(result['eta_available'])
        self.assertIsNone(result['candidates'][0]['history_only_fusion_quantiles_us'])

    def test_remaining_mode_keeps_total_intervals_and_conditions_in_fusion(self):
        plan={**self.plan,'mode':'remaining','issued_at':baseline.stamp(-270),
              'call_not_observed':True,'target_episode_id':str(uuid4())}
        result=self.calculate(plan);entry=result['candidates'][0]
        self.assertEqual(entry['fusion_plan']['candidates'][0]['interval_sample']['elapsed_us'],300000000)
        self.assertEqual(entry['history_only_fusion_quantiles_us']['p50'],{'lower_us':300000000,'upper_us':360000000})

    def test_remaining_without_definite_survivors_returns_missing(self):
        plan={**self.plan,'mode':'remaining','issued_at':baseline.stamp(-1000),
              'call_not_observed':True,'target_episode_id':str(uuid4())}
        result=self.calculate(plan);self.assertFalse(result['research_candidate_available'])
        self.assertEqual(result['candidates'][0]['forecast']['reason_code'],'insufficient_definite_survivors')

    def test_ideal_grid_evaluates_all_candidates_and_preserves_offset_sign(self):
        for offset,selected in [(10,2),(-10,0)]:
            result=self.calculate(self.ideal(offset))
            self.assertEqual(len(result['candidates']),3);self.assertEqual(result['selected_candidate_index'],selected)
            self.assertFalse(result['future_queue_observations_used']);self.assertFalse(result['candidate_bookability_verified'])
            self.assertFalse(result['monotonicity_assumed'])
            self.assertTrue(all(e['fusion_plan'] is not None for e in result['candidates']))

    def test_ideal_empty_history_keeps_all_missing_candidates(self):
        result=self.calculate({**self.ideal(),'minimum_samples':2})
        self.assertEqual(len(result['candidates']),3);self.assertIsNone(result['selected_candidate_index'])

    def test_public_context_scope_and_cutoff_are_bound_even_with_no_history(self):
        for change in [{'store_id':'900002'},{'queue_type':'reservation'},{'as_of':baseline.stamp(29)}]:
            context=self.context();context.update(change)
            with self.assertRaises(HistoryFusionError):self.calculate(context=context)

    def test_unknown_year_calendar_explicitly_falls_back(self):
        value=baseline.fixtures.record();value['episode_id']=str(uuid4());self.add(value,3)
        p={**self.plan,'target_episode_id':self.value['episode_id']}
        result=self.calculate(p)
        self.assertEqual(result['candidates'][0]['forecast']['matched_group'],'store_queue_party_table')

    def test_duplicate_waits_are_aggregated_without_losing_empirical_frequency(self):
        self.add(self.episode(),3)
        result=self.calculate();rows=result['candidates'][0]['fusion_plan']['candidates'][0]['interval_sample']['intervals']
        self.assertEqual(rows,[[600000000,660000000,2]])

    def test_invalid_maximum_model_blend_or_private_context_extra_reject(self):
        for options in [{'max_revisions':0},{'max_revisions':True},{'ai_blend_ppm':True},{'model_version':'private/path'}]:
            with self.assertRaises(HistoryFusionError):self.calculate(**options)
        context=self.context();context['authorization']='private-marker'
        with self.assertRaises(HistoryFusionError) as error:self.calculate(context=context)
        self.assertNotIn('private-marker',str(error.exception))

    def test_support_limit_is_explicit_never_truncates_cohort(self):
        # Feed 129 distinct reviewed intervals through the actual matching/math path.
        import sushiwait.historyfusion as bridge
        prepared=bridge._prepare
        def many(*args):
            pool,excluded=prepared(*args)
            return [{**pool[0],'interval':(i,i),'source_receipt_sha256':str(i)} for i in range(129)],excluded
        with patch('sushiwait.historyfusion._prepare',side_effect=many),self.assertRaises(HistoryFusionError) as error:
            self.calculate()
        self.assertEqual(error.exception.error_code,'history_fusion_support_limit')

    def test_actual_private_cli_no_socket_credentials_or_alive_remote_database(self):
        self.input_path.write_text(json.dumps(self.plan));self.input_path.chmod(0o600)
        context_file=self.parent/'output/context.json';context_file.write_text(json.dumps(self.context()));context_file.chmod(0o600)
        output=io.StringIO()
        before={p:p.read_bytes() for p in [self.source_path,self.review_path]}
        with patch('socket.socket',side_effect=AssertionError('network')) as sockets,\
             patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('credentials')) as credentials,\
             patch('sushiwait.baseline._clock',return_value=baseline.BASE+timedelta(seconds=1000)),\
             patch('sushiwait.reviews._clock',return_value=baseline.BASE+timedelta(seconds=1000)),contextlib.redirect_stdout(output):
            code=main(['history-fusion-research','--source-db',str(self.source_path),'--reviews-db',str(self.review_path),
                '--input',str(self.input_path),'--context-file',str(context_file),'--output',str(self.output_path)])
        self.assertEqual(code,0);self.assertEqual(sockets.call_count,0);self.assertEqual(credentials.call_count,0)
        self.assertEqual(before,{p:p.read_bytes() for p in before});self.assertEqual(self.output_path.stat().st_mode&0o777,0o600)
        self.assertNotIn(str(self.parent),output.getvalue());self.assertNotIn('intervals',output.getvalue())
        self.assertTrue(json.loads(output.getvalue())['research_candidate_available'])
        result=json.loads(self.output_path.read_bytes());self.assertFalse(result['eta_available'])


if __name__=='__main__':unittest.main()
