import json
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest

from sushiwait.modeladvice import public_context, suggest_weights
from sushiwait.displayreplay import _time
from test_display_replay import archive


class PublicModelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.root.chmod(0o700)
        self.value=archive();self.now=_time(self.value['points'][-1]['queue_received_at'])+timedelta(seconds=1)
        self.context=public_context(self.value,now=self.now)
        self.key=self.root/'key.json';self.key.write_text(json.dumps({'schema_version':1,'purpose':'deepseek_official','api_key':'sk-'+'test-only-key'*4}));self.key.chmod(0o600)
    def budget(self,mode='nonthinking'):
        directory=self.root/mode;directory.mkdir(mode=0o700)
        value={'schema_version':1,'provider':'deepseek_official','model':'deepseek-flash','thinking_mode':mode,
            'created_at':(self.now-timedelta(seconds=1)).isoformat(),'deadline_at':(self.now+timedelta(hours=1)).isoformat(),
            'max_calls':2,'max_reserved_tokens':30000,'max_reserved_cost_microunits':500000,
            'max_prompt_tokens':9000,'max_output_tokens':2048,'input_rate_microunits_per_million':4000000,
            'output_rate_microunits_per_million':16000000,'price_basis':'caller_supplied_ceiling_not_verified','timeout_seconds':30}
        path=directory/'budget.json';path.write_text(json.dumps(value));path.chmod(0o600);return path
    def response(self,thinking=False,invalid=False):
        weights={'history':400000,'steady':300000,'fast':200000,'slow':100000}
        if invalid:weights['fast']+=1
        return json.dumps({'object':'chat.completion','model':'deepseek-flash',
            'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','reasoning_content':'private reasoning discarded' if thinking else None,
                'content':json.dumps({'weights_ppm':weights,'feature_ids':['sample_count'],'reason_code':'mixed_evidence'})}}],
            'usage':{'prompt_tokens':200,'completion_tokens':100,'total_tokens':300,'prompt_cache_hit_tokens':0,'prompt_cache_miss_tokens':200,
                'completion_tokens_details':{'reasoning_tokens':50 if thinking else 0}}}).encode()
    def test_public_whitelist_does_not_accept_personal_extras(self):
        context=deepcopy(self.context);context['ticket_number']='4045'
        with self.assertRaises(ValueError):suggest_weights(context,budget_file=None,key_file=None,now=self.now)
        self.assertNotIn('number',self.context);self.assertNotIn('issued_at',self.context)
    def test_exact_curve_input_is_auditable_without_private_reasoning(self):
        from sushiwait.modeladvice import SYSTEM_PROMPT, PROMPT_VERSION
        path=self.budget('thinking');sent=[]
        def transport(body,*_):
            sent.append(json.loads(body));return self.response(True)
        suggest_weights(self.context,budget_file=path,key_file=self.key,now=self.now,transport=transport)
        self.assertEqual(sent[0]['messages'][0]['content'],SYSTEM_PROMPT)
        stored=json.loads(next(path.parent.glob('public-input-*.json')).read_text())
        self.assertEqual(stored,self.context)
        self.assertEqual(stored['prompt_version'],PROMPT_VERSION)
        self.assertEqual(stored['trend_points'][-1][0],0)
        self.assertTrue(all(len(row)==4 for row in stored['trend_points']))
        self.assertNotIn('private reasoning discarded',''.join(p.read_text() for p in path.parent.glob('*.json')))
    def test_curve_nested_extras_cannot_reach_provider(self):
        for field,extra in [('trend_points',[[0,100,101,102,'private']]),('history_windows',[{'rate':2,'personal_ticket':123}])]:
            context=deepcopy(self.context);context[field]=extra
            with self.assertRaises(ValueError):
                suggest_weights(context,budget_file=None,key_file=None,now=self.now)
    def test_modes_have_separate_budget_and_durable_cache(self):
        for mode in ('nonthinking','thinking'):
            path=self.budget(mode);bodies=[]
            def transport(body,key,timeout):bodies.append(json.loads(body));return self.response(mode=='thinking')
            result=suggest_weights(self.context,budget_file=path,key_file=self.key,now=self.now,transport=transport)
            self.assertEqual(result['state'],'ready');self.assertEqual(result['thinking_mode'],mode)
            self.assertEqual(bodies[0]['thinking']['type'],'enabled' if mode=='thinking' else 'disabled')
            if mode=='thinking':self.assertEqual(bodies[0]['reasoning_effort'],'high')
            self.assertNotIn('api_key',bodies[0]);self.assertFalse(result['network_performed'])
            cached=suggest_weights(self.context,budget_file=path,key_file=self.key,now=self.now,transport=lambda *a: self.fail('must not retry'))
            self.assertEqual(cached['state'],'cached');self.assertEqual(cached['attempts_used'],1)
            self.assertNotIn('reasoning',json.dumps(result['advice']))
    def test_invalid_weights_are_rejected_but_charge_stays_reserved(self):
        path=self.budget();result=suggest_weights(self.context,budget_file=path,key_file=self.key,now=self.now,transport=lambda *a:self.response(invalid=True))
        self.assertEqual(result['state'],'fallback');self.assertIsNone(result['advice'])
        again=suggest_weights(self.context,budget_file=path,key_file=self.key,now=self.now,transport=lambda *a:self.fail('must not retry'))
        self.assertEqual(again['state'],'previous_attempt_unknown')
    def test_transport_loss_is_not_retried_on_restart(self):
        path=self.budget()
        def lost(*args):raise OSError('synthetic lost reply')
        with self.assertRaises(OSError):suggest_weights(self.context,budget_file=path,key_file=self.key,now=self.now,transport=lost)
        result=suggest_weights(self.context,budget_file=path,key_file=self.key,now=self.now,transport=lambda *a:self.fail('must not retry'))
        self.assertEqual(result['state'],'previous_attempt_unknown');self.assertEqual(result['attempts_used'],1)
    def test_historical_connection_is_explicit_not_live_forecast(self):
        self.value['points'][-1]['queues']['mixedQueue']=[]
        with self.assertRaises(ValueError):public_context(self.value,now=self.now)
        context=public_context(self.value,now=self.now+timedelta(hours=2),connection_check=True)
        self.assertEqual(context['phase'],'historical_connection_check')
    def test_stale_live_input_rejected_before_key_or_budget_access(self):
        with self.assertRaises(ValueError):suggest_weights(self.context,budget_file=None,key_file=None,now=self.now+timedelta(hours=1))
    def test_budget_cannot_reset_or_overrun(self):
        path=self.budget();value=json.loads(path.read_bytes());value['max_calls']=1;path.write_text(json.dumps(value))
        suggest_weights(self.context,budget_file=path,key_file=self.key,now=self.now,transport=lambda *a:self.response())
        other=deepcopy(self.context);other['observation_id']='new'
        from sushiwait.deepseek import DeepSeekError
        with self.assertRaises(DeepSeekError):suggest_weights(other,budget_file=path,key_file=self.key,now=self.now,transport=lambda *a:self.fail('quota must stop request'))

    def test_actual_known_cost_releases_estimate_but_unknown_keeps_reservation(self):
        path=self.budget();value=json.loads(path.read_bytes());value.update(accounting_policy='actual_known_reserved_unknown',max_reserved_cost_microunits=75000);path.write_text(json.dumps(value))
        suggest_weights(self.context,budget_file=path,key_file=self.key,now=self.now,transport=lambda *a:self.response())
        other=deepcopy(self.context);other['observation_id']='new'
        second=suggest_weights(other,budget_file=path,key_file=self.key,now=self.now,transport=lambda *a:self.response())
        self.assertEqual(second['state'],'ready')

    def test_smooth_cadence_change_does_not_repeat_a_public_observation(self):
        path=self.budget()
        suggest_weights(self.context,budget_file=path,key_file=self.key,now=self.now,transport=lambda *a:self.response())
        changed={**self.context,'cadence_seconds':180}
        result=suggest_weights(changed,budget_file=path,key_file=self.key,now=self.now,transport=lambda *a:self.fail('same source observation'))
        self.assertEqual(result['state'],'cached');self.assertEqual(result['attempts_used'],1)
