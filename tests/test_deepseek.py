"""Official wire shape, private isolation, finite durable quota and late replies."""
import contextlib
from copy import deepcopy
from datetime import timedelta
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sushiwait.cli import main
from sushiwait.deepseek import (DeepSeekError, MODEL, _Ledger, _https_post,
                                run_deepseek, write_deepseek_fusion)
from sushiwait.fusion import FusionError, public_request
from test_fusion import BASE, plan, stamp


def budget():
    return {'schema_version': 1, 'provider': 'deepseek_official', 'model': MODEL,
        'created_at': stamp(-1), 'deadline_at': stamp(3600), 'max_calls': 3,
        'max_reserved_tokens': 100_000, 'max_reserved_cost_microunits': 1_000_000,
        'max_prompt_tokens': 20_000, 'max_output_tokens': 512,
        'input_rate_microunits_per_million': 2_000_000,
        'output_rate_microunits_per_million': 8_000_000,
        'price_basis': 'caller_supplied_ceiling_not_verified', 'timeout_seconds': 10}


def response():
    return {'object': 'chat.completion', 'model': MODEL,
        'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant',
            'content': json.dumps({'weights_ppm': {'fast': 1_000_000, 'history': 0},
                'feature_ids': ['ordinary_removed_labels'], 'reason_code': 'rapid_display_turnover'})}}],
        'usage': {'prompt_tokens': 100, 'completion_tokens': 30, 'total_tokens': 130,
            'prompt_cache_hit_tokens': 80, 'prompt_cache_miss_tokens': 20,
            'prompt_tokens_details': {'cached_tokens': 80}}}


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.ledger = self.root/'ledger'; self.ledger.mkdir(mode=0o700)
        self.output = self.root/'outputs'; self.output.mkdir(mode=0o700)
        self.budget_file = self.ledger/'budget.json'
        self.key_file = self.root/'key.json'
        self.save(self.budget_file, budget())
        self.save(self.key_file, {'schema_version': 1, 'purpose': 'deepseek_official', 'api_key': 'sk-SyntheticOnlyKey'})
        self.replies, self.elapsed, self.calls = [], 0, []
        self.value = response()

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, path, value):
        path.write_text(json.dumps(value)); path.chmod(0o600)

    def transport(self, body, key, timeout):
        self.calls.append((json.loads(body), key, timeout))
        if self.replies:
            return self.replies.pop(0)
        return json.dumps(self.value).encode()

    def run_adapter(self, p=None, **kwargs):
        return run_deepseek(plan() if p is None else p, budget_file=self.budget_file,
            key_file=self.key_file, allow_paid_request=True, transport=self.transport,
            clock=lambda: BASE+timedelta(seconds=self.elapsed), monotonic=lambda: self.elapsed, **kwargs)

    def test_disabled_accesses_neither_key_ledger_nor_network(self):
        with patch('sushiwait.deepseek._read_private_file', side_effect=AssertionError('private')) as read:
            result = run_deepseek(plan(), clock=lambda: BASE, transport=self.transport)
        self.assertEqual(read.call_count, 0); self.assertEqual(self.calls, [])
        self.assertEqual(result['fallback_reason'], 'deepseek_disabled')
        self.assertFalse(result['provider_request_attempted'])

    def test_wire_only_public_data_and_exact_model_parameters(self):
        result = self.run_adapter()
        body, key, timeout = self.calls[0]
        self.assertEqual(body['model'], MODEL); self.assertEqual(body['thinking'], {'type': 'disabled'})
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertFalse(body['stream']); self.assertEqual(body['max_tokens'], 512)
        self.assertEqual(json.loads(body['messages'][1]['content']), public_request(plan(), now=BASE))
        self.assertEqual(set(body), {'model','thinking','stream','temperature','max_tokens','response_format','messages'})
        self.assertNotIn('atoms', json.dumps(body)); self.assertNotIn('prior_weights_ppm', json.dumps(body))
        self.assertNotIn('ai_blend_ppm', json.dumps(body)); self.assertEqual(timeout, 10)
        self.assertTrue(result['advice_accepted']); self.assertTrue(result['ai_numerical_influence_applied'])
        self.assertEqual(result['wait_quantile_envelopes_us']['p50']['lower_us'], 0)
        self.assertFalse(result['provider_called']); self.assertFalse(result['network_performed'])
        self.assertEqual(result['advice_origin_basis'], 'injected_transport_not_provider_attested')
        self.assertFalse(result['eta_available']); self.assertFalse(result['billing_verified'])

    def test_reservation_exists_durably_before_post_and_is_private(self):
        actual = self.transport
        def transmit(body, key, timeout):
            call = self.ledger/'deepseek-call-1.json'
            self.assertTrue(call.exists()); self.assertEqual(call.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(call.read_text())['reserved_tokens'], 20512)
            self.assertNotIn(key, call.read_text())
            return actual(body, key, timeout)
        result = run_deepseek(plan(), budget_file=self.budget_file, key_file=self.key_file,
            allow_paid_request=True, transport=transmit, clock=lambda: BASE, monotonic=lambda: 0)
        self.assertTrue(result['advice_accepted'])
        for path in self.ledger.glob('deepseek-*.json'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('SyntheticOnlyKey', path.read_text())

    def test_usage_cost_no_assumed_cache_discount(self):
        result = self.run_adapter()
        self.assertEqual(result['usage']['total_tokens'], 130)
        self.assertEqual(result['estimated_cost_microunits'], 440)
        self.assertEqual(result['usage']['prompt_cache_hit_tokens'], 80)
        self.assertFalse(result['reservation_refunded'])

    def test_exact_input_cache_reuses_weights_without_second_key_read(self):
        first = self.run_adapter(); self.key_file.unlink()
        self.elapsed = 1
        second = self.run_adapter()
        self.assertEqual(len(self.calls), 1); self.assertTrue(second['advice_cache_hit'])
        self.assertFalse(second['provider_request_attempted']); self.assertEqual(second['usage_source'], 'cached_response')
        self.assertEqual(first['advice']['generated_at'], second['advice']['generated_at'])

    def test_new_read_clock_same_observation_does_not_pay_or_restamp_advice(self):
        self.run_adapter(); p = plan(); self.elapsed = 1
        p['public_context']['as_of'] = stamp(1); p['public_context']['expires_at'] = stamp(61)
        for feature in p['public_context']['features']: feature['available_at'] = stamp(1)
        result = self.run_adapter(p)
        self.assertEqual(len(self.calls), 1); self.assertEqual(result['fallback_reason'], 'deepseek_duplicate_observation')
        self.assertFalse(result['advice_accepted'])

    def test_different_personal_distributions_share_public_advice(self):
        self.run_adapter(); p = plan(); p['candidates'][0]['atoms'][0]['lower_us'] = 200
        p['candidates'][0]['atoms'][0]['upper_us'] = 200
        result = self.run_adapter(p)
        self.assertTrue(result['advice_cache_hit']); self.assertEqual(len(self.calls), 1)
        self.assertEqual(result['wait_quantile_envelopes_us']['p90']['upper_us'], 200)

    def test_new_actual_observation_may_use_next_slot(self):
        self.run_adapter(); p = plan(); p['public_context']['observation_revision'] = 4
        p['public_context']['latest_queue_received_at'] = stamp(-1)
        result = self.run_adapter(p)
        self.assertEqual(len(self.calls), 2); self.assertTrue(result['advice_accepted'])

    def test_expired_cached_advice_never_rebilled(self):
        self.run_adapter(); self.elapsed=60
        result=self.run_adapter()
        self.assertFalse(result['advice_accepted']); self.assertEqual(len(self.calls),1)

    def test_budget_changed_during_request_rejects_reply_and_keeps_spent_slot(self):
        actual=self.transport
        def changed(*args):
            b=budget();b['max_calls']=4;self.save(self.budget_file,b)
            return actual(*args)
        with patch.object(self,'transport',side_effect=changed):result=self.run_adapter()
        self.assertEqual(result['fallback_reason'],'deepseek_budget_changed');self.assertFalse(result['advice_accepted'])
        self.assertEqual(len(self.calls),1)

    def test_input_byte_guard_blocks_post_and_creates_no_receipt(self):
        b=budget();b['max_prompt_tokens']=4096;self.save(self.budget_file,b)
        result=self.run_adapter();self.assertEqual(result['fallback_reason'],'deepseek_input_too_large')
        self.assertFalse((self.ledger/'deepseek-call-1.json').exists());self.assertEqual(self.calls,[])

    def test_call_token_and_cost_caps_each_block_new_post(self):
        for field, cap in [('max_calls',1), ('max_reserved_tokens',20512), ('max_reserved_cost_microunits',44096)]:
            with self.subTest(field=field):
                for p in self.ledger.glob('deepseek-*.json'): p.unlink()
                b=budget(); b[field]=cap; self.save(self.budget_file,b); self.calls=[]
                self.run_adapter(); p=plan(); p['public_context']['observation_revision']=4
                result=self.run_adapter(p)
                self.assertEqual(len(self.calls),1); self.assertEqual(result['fallback_reason'],'deepseek_budget_exhausted')

    def test_timeout_unknown_result_no_retry_after_reopening_ledger(self):
        def failed(*args):
            self.calls.append(args); raise TimeoutError('sk-SyntheticOnlyKey-private-path')
        with patch.object(self,'transport',side_effect=failed): first=self.run_adapter()
        self.assertFalse(first['advice_accepted']); self.assertEqual(len(self.calls),1)
        second=self.run_adapter()
        self.assertEqual(second['fallback_reason'],'deepseek_duplicate_observation'); self.assertEqual(len(self.calls),1)
        self.assertNotIn('SyntheticOnlyKey',json.dumps(first))

    def test_crash_reservation_without_response_keeps_budget_used(self):
        with patch('sushiwait.deepseek._response_advice',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): self.run_adapter()
        result=self.run_adapter()
        self.assertEqual(result['fallback_reason'],'deepseek_duplicate_observation'); self.assertEqual(len(self.calls),1)

    def test_budget_mutation_cannot_reset_old_reservations(self):
        self.run_adapter(); b=budget(); b['max_calls']=4; self.save(self.budget_file,b)
        result=self.run_adapter()
        self.assertEqual(result['fallback_reason'],'deepseek_invalid_ledger'); self.assertEqual(len(self.calls),1)

    def test_fsync_failure_after_reservation_publication_sends_no_post(self):
        actual=os.fsync; count=[0]
        def sync(fd):
            count[0]+=1
            if count[0]==3: raise OSError('private')
            return actual(fd)
        with patch('sushiwait.deepseek.os.fsync',side_effect=sync): result=self.run_adapter()
        self.assertEqual(result['fallback_reason'],'deepseek_ledger_write_failed'); self.assertEqual(self.calls,[])
        self.assertTrue((self.ledger/'deepseek-call-1.json').exists())
        self.assertEqual(self.run_adapter()['fallback_reason'],'deepseek_duplicate_observation')

    def test_busy_ledger_falls_back_without_call(self):
        ledger=_Ledger(self.budget_file,BASE)
        try: result=self.run_adapter()
        finally: ledger.close()
        self.assertEqual(result['fallback_reason'],'deepseek_ledger_busy'); self.assertEqual(self.calls,[])

    def test_noncontiguous_receipts_cannot_hide_spent_budget(self):
        self.run_adapter(); (self.ledger/'deepseek-call-1.json').rename(self.ledger/'deepseek-call-2.json')
        result=self.run_adapter(); self.assertEqual(result['fallback_reason'],'deepseek_invalid_ledger')
        self.assertEqual(len(self.calls),1)

    def test_bad_usage_blocks_advice_and_keeps_full_reservation(self):
        for change in [{'total_tokens':1}, {'prompt_tokens':True}, {'prompt_cache_miss_tokens':21},
                       {'prompt_tokens_details':{'cached_tokens':2}}]:
            for p in self.ledger.glob('deepseek-*.json'): p.unlink()
            self.value=response(); self.value['usage'].update(change)
            result=self.run_adapter(); self.assertEqual(result['fallback_reason'],'deepseek_invalid_usage')
            self.assertFalse(result['advice_accepted']); self.assertTrue((self.ledger/'deepseek-call-1.json').exists())

    def test_usage_overrun_stops_all_further_requests(self):
        self.value['usage'].update({'completion_tokens':513,'total_tokens':613})
        result=self.run_adapter(); self.assertEqual(result['fallback_reason'],'deepseek_usage_overrun')
        p=plan(); p['public_context']['observation_revision']=4
        self.assertEqual(self.run_adapter(p)['fallback_reason'],'deepseek_usage_overrun'); self.assertEqual(len(self.calls),1)

    def test_thinking_tokens_accounted_but_disabled_mode_reply_rejected(self):
        self.value['usage']['completion_tokens_details']={'reasoning_tokens':20}
        result=self.run_adapter()
        self.assertEqual(result['usage']['reasoning_tokens'],20); self.assertEqual(result['fallback_reason'],'deepseek_unexpected_thinking')

    def test_late_complete_reply_discarded_after_socket_returns(self):
        actual=self.transport
        def late(*args):
            self.elapsed=11; return actual(*args)
        with patch.object(self,'transport',side_effect=late): result=self.run_adapter()
        self.assertEqual(result['fallback_reason'],'deepseek_timeout'); self.assertFalse(result['advice_accepted'])
        self.assertEqual(result['usage']['total_tokens'],130)

    def test_newer_input_rejects_entire_old_result_even_after_paid_work(self):
        initial=public_request(plan(),now=BASE)
        def current(): return initial if not self.calls else {**initial,'model_version':'new'}
        with self.assertRaises(FusionError) as caught: self.run_adapter(current_request=current)
        self.assertEqual(caught.exception.error_code,'fusion_superseded_input'); self.assertEqual(len(self.calls),1)
        self.assertIsNone(json.loads((self.ledger/'deepseek-result-1.json').read_text())['advice'])

    def test_preflight_stale_stopped_saved_or_expired_no_key_no_call(self):
        for field,value in [('collector_running',False),('latest_queue_origin','saved_history'),
                            ('latest_queue_received_at',stamp(-91)),('expires_at',stamp(0))]:
            p=plan(); p['public_context'][field]=value
            with patch('sushiwait.deepseek._key',side_effect=AssertionError('key')) as key:
                result=self.run_adapter(p) if field!='expires_at' else None
                if field=='expires_at':
                    with self.assertRaises(FusionError): self.run_adapter(p)
            self.assertEqual(key.call_count,0); self.assertEqual(self.calls,[])
            if result is not None: self.assertFalse(result['advice_accepted'])

    def test_invalid_budget_or_key_private_permissions_no_post(self):
        self.key_file.chmod(0o644)
        self.assertEqual(self.run_adapter()['fallback_reason'],'deepseek_invalid_key')
        self.key_file.chmod(0o600); self.budget_file.chmod(0o644)
        self.assertEqual(self.run_adapter()['fallback_reason'],'deepseek_invalid_ledger'); self.assertEqual(self.calls,[])

    def test_invalid_fields_dates_rates_and_limits(self):
        for field,value in [('deadline_at',stamp(-1)),('deadline_at',stamp(260000)),('model','other'),
                            ('timeout_seconds',True),('max_calls',0),('input_rate_microunits_per_million',0),
                            ('price_basis','official_verified')]:
            b=budget(); b[field]=value; self.save(self.budget_file,b)
            result=self.run_adapter(); self.assertTrue(result['fallback_reason'].startswith('deepseek_'))
            self.assertEqual(self.calls,[])

    def test_finish_reason_tools_wrong_model_or_invented_advice_fallback(self):
        for mutate in [lambda v:v['choices'][0].update(finish_reason='length'),
                       lambda v:v.update(model='wrong'),
                       lambda v:v['choices'][0]['message'].update(tool_calls=[{}]),
                       lambda v:v['choices'][0]['message'].update(content='{"weights_ppm":{}}'),
                       lambda v:v['choices'][0]['message'].update(content='{"reason_code":1,"reason_code":2}')]:
            for p in self.ledger.glob('deepseek-*.json'):p.unlink()
            self.value=response(); mutate(self.value)
            result=self.run_adapter(); self.assertFalse(result['advice_accepted'])
            self.assertEqual(result['wait_quantile_envelopes_us']['p50']['lower_us'],100)

    def test_reject_provider_invented_metadata_and_bad_arithmetic(self):
        for change in [{'generated_at':'2026-10-08T00:00:00Z'},
                       {'weights_ppm':{'fast':999999,'history':0}},
                       {'weights_ppm':{'fast':True,'history':999999}},
                       {'feature_ids':['reported_count_raw']}, {'reason_code':'insufficient_evidence'}]:
            for p in self.ledger.glob('deepseek-*.json'):p.unlink()
            self.value=response(); answer=json.loads(self.value['choices'][0]['message']['content']);answer.update(change)
            self.value['choices'][0]['message']['content']=json.dumps(answer)
            self.assertFalse(self.run_adapter()['advice_accepted'])

    def test_oversized_malformed_or_duplicate_response_rejected_without_raw_log(self):
        for raw in [b'x'*32769,b'not-json-private',b'{"usage":{},"usage":{}}',b'{"usage":NaN}']:
            for p in self.ledger.glob('deepseek-*.json'):p.unlink()
            self.replies=[raw]; result=self.run_adapter()
            self.assertFalse(result['advice_accepted']);self.assertNotIn('not-json-private',json.dumps(result))

    def test_output_preflight_avoids_paid_request_and_default_cli_writes_private_baseline(self):
        output=self.output/'result.json';output.write_text('existing');output.chmod(0o600)
        with self.assertRaises(DeepSeekError):
            write_deepseek_fusion(plan(),destination=output,allow_paid_request=True,
                budget_file=self.budget_file,key_file=self.key_file,transport=self.transport,clock=lambda:BASE)
        self.assertEqual(self.calls,[])
        input_file=self.root/'plan.json';self.save(input_file,plan());out=self.output/'baseline.json'
        stdout=io.StringIO()
        with patch('sushiwait.fusion._clock',return_value=BASE), patch('sushiwait.deepseek._now',return_value=BASE),\
             patch('sushiwait.cli.write_deepseek_fusion',wraps=lambda p,**k:write_deepseek_fusion(p,clock=lambda:BASE,**k)),\
             patch('socket.socket',side_effect=AssertionError('socket')) as sockets,contextlib.redirect_stdout(stdout):
            self.assertEqual(main(['deepseek-fusion','--input',str(input_file),'--output',str(out)]),0)
        self.assertEqual(sockets.call_count,0);summary=json.loads(stdout.getvalue())
        self.assertEqual(summary['fallback_reason'],'deepseek_disabled');self.assertNotIn(str(self.root),stdout.getvalue())
        self.assertNotIn('atoms',stdout.getvalue());self.assertEqual(out.stat().st_mode&0o777,0o600)
        self.assertEqual(json.loads(out.read_text())['plan'],plan()|{'public_context':public_request(plan(),now=BASE)['context']})


class HttpsTests(unittest.TestCase):
    def test_fixed_verified_tls_destination_headers_and_single_post(self):
        calls=[]
        class Response:
            status=200
            def getheader(self,name):return None
            def read(self,size):return b''
        class Connection:
            sock=None
            def __init__(self,host,**kwargs):
                calls.append(('connect',host,kwargs['timeout'],kwargs['context'].check_hostname))
            def request(self,method,path,**kwargs):calls.append(('request',method,path,kwargs))
            def getresponse(self):return Response()
            def close(self):calls.append(('close',))
        with patch('sushiwait.deepseek.http.client.HTTPSConnection',Connection):
            self.assertEqual(_https_post(b'{}','sk-Synthetic',5),b'')
        self.assertEqual(calls[0],('connect','api.deepseek.com',5,True))
        self.assertEqual(calls[1][1:3],('POST','/chat/completions'));self.assertEqual(len(calls),3)
        self.assertEqual(calls[1][3]['headers']['Authorization'],'Bearer sk-Synthetic')

    def test_redirect_rejected_never_followed_and_transport_errors_redacted(self):
        class Connection:
            def __init__(self,*args,**kwargs):pass
            def request(self,*args,**kwargs):pass
            def getresponse(self):return type('Response',(),{'status':302})()
            def close(self):pass
        with patch('sushiwait.deepseek.http.client.HTTPSConnection',Connection):
            with self.assertRaises(DeepSeekError) as error:_https_post(b'{}','sk-Synthetic',5)
        self.assertEqual(str(error.exception),'deepseek_http_failed')
        with patch.object(Connection,'request',side_effect=OSError('sk-Synthetic /private/path')),\
             patch('sushiwait.deepseek.http.client.HTTPSConnection',Connection):
            with self.assertRaises(DeepSeekError) as error:_https_post(b'{}','sk-Synthetic',5)
        self.assertNotIn('sk-',str(error.exception));self.assertIsNone(error.exception.__cause__)


if __name__=='__main__':unittest.main()
