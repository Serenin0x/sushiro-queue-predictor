from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import tempfile
from pathlib import Path
import unittest
from sushiwait.analytics import selection, summarize_day, build_report, ai_context
from sushiwait.analyticsai import explain, tick, write_state, validate_answer, PROMPT
from sushiwait.deepseek import _Ledger, DeepSeekError
from test_display_replay import archive
from test_model_advice_worker import worker

class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,10,14,3,tzinfo=timezone.utc)
        self.scope={'from':'2026-10-13','to':'2026-10-13','source':'sapi_miniapp_gateway','queue':'ordinary','store_id':''}
        self.detail=archive();self.detail['requested_store_id']='3014'
    def read(self,path):
        if '/months/' in path:return {'source':'sapi_miniapp_gateway','days':{'2026-10-13':{'3014':{}}}}
        return deepcopy(self.detail)
    def report(self):return build_report(self.read,{'3014':'合成门店'},self.scope,now=self.now)
    def test_bounds_dates_scope_and_no_arbitrary_paths(self):
        for change in ({'from':'2026-02-30'},{'to':'2027-10-13'},{'from':'2020-01-01'},{'queue':'mixed'},{'store_id':'../secret'},{'source':'unknown'}):
            with self.subTest(change=change),self.assertRaises(ValueError):selection({**self.scope,**change},self.now)
    def test_linear_rates_coverage_and_unique_labels_are_not_customer_count(self):
        row=self.report()['stores'][0]
        self.assertEqual(row['comparable_intervals'],19)
        self.assertEqual(row['hourly'][1]['rate'],2)
        self.assertEqual(row['unique_displayed'],20)
        self.assertEqual(row['covered_slots'],7)
        self.assertEqual(row['expected_slots'],44)
        self.assertIsNone(row['prediction_accuracy'])
        self.assertLess(row['readiness_score'],40)
        self.assertEqual(self.report()['semantics']['activity'],'sampled_display_activity_not_customer_count')
    def test_empty_failed_missing_and_no_progress_are_separate(self):
        d=archive((100,100,None,120,130,140))
        d['points'][3]['pair_ok']=False
        d['points'][3]['queue_field_presence']=None
        d['points'][4]['queues'].pop('mixedQueue')
        r=summarize_day(d,'ordinary',self.now)
        self.assertEqual((r['visible'],r['empty'],r['failed'],r['missing']),(3,1,1,1))
        self.assertEqual({g['kind'] for g in r['gaps']},{'empty_display','no_progress'})
        self.assertEqual(r['comparable_intervals'],1)
    def test_no_rate_across_gap_reset_broken_link_or_truncation(self):
        d=archive((100,120,90,140))
        d['points'][3]['continuity']['predecessor_id']='unrelated'
        row=summarize_day(d,'ordinary',self.now)
        self.assertEqual(row['reversals'],1);self.assertEqual(row['comparable_intervals'],1)
        d['graph_truncated']=True;self.assertEqual(summarize_day(d,'ordinary',self.now)['comparable_intervals'],0)
        d=archive((100,110,120,130,140))
        for key in ('queue_received_at','request_started_at'):
            d['points'][-1][key]=(datetime.fromisoformat(d['points'][-1][key])+timedelta(hours=1)).isoformat()
        r=summarize_day(d,'ordinary',self.now)
        self.assertTrue(any(g['kind']=='sampling_gap' for g in r['gaps']));self.assertEqual(r['comparable_intervals'],3)
    def test_intentional_fifteen_minute_sampling_not_gap(self):
        r=summarize_day(archive(step=15),'ordinary',self.now)
        self.assertFalse(any(g['kind']=='sampling_gap' for g in r['gaps']))
    def test_all_read_failures_disclosed_and_no_ai(self):
        report=build_report(lambda _: (_ for _ in ()).throw(ValueError()),{'3014':'合成'},self.scope,now=self.now)
        self.assertTrue(report['incomplete']);self.assertIsNone(report['stores'][0]['success_percent'])
        with self.assertRaises(ValueError):ai_context(report)
    def test_ai_payload_explicit_allowlist_drops_private_fields(self):
        report=self.report();report['stores'][0].update(number='SECRET_NUMBER',issued_at='SECRET_TIME',owner='SECRET_OWNER')
        context=ai_context(report);text=json.dumps(context)
        self.assertNotIn('SECRET',text);self.assertNotIn('issued_at',text);self.assertEqual(context['scope'],'public_aggregates_only')
    def test_report_identity_ignores_read_time_on_completed_day(self):
        a=self.report();b=build_report(self.read,{'3014':'合成门店'},self.scope,now=self.now+timedelta(minutes=1))
        self.assertEqual(a['report_id'],b['report_id'])

class AnalyticsModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.root.chmod(0o700)
        self.now=datetime(2026,10,14,4,tzinfo=timezone.utc)
        self.budget=worker.prepare_budget(self.root,'thinking',self.now)
        self.key=self.root/'key/key.private.json';self.key.parent.mkdir(mode=0o700)
        self.key.write_text(json.dumps({'schema_version':1,'purpose':'deepseek_official','api_key':'sk-synthetic-only-key'}));self.key.chmod(0o600)
        self.context={'policy':'store_readiness_v1','scope':'public_aggregates_only','report_id':'b'*64,'stores':[{'store_id':'3014','observations':20}]}
    def response(self,bad=False):
        answer={'summary':'覆盖仍不足。','stores':[{'store_id':'9999' if bad else '3014','finding':'有20个观测。','recommendation':'积累连续样本。','evidence_fields':['observations']}]}
        return json.dumps({'object':'chat.completion','model':'deepseek-flash','choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','reasoning_content':'not saved','content':json.dumps(answer)}}], 'usage':{'prompt_tokens':200,'completion_tokens':100,'total_tokens':300,'prompt_cache_hit_tokens':0,'prompt_cache_miss_tokens':200,'completion_tokens_details':{'reasoning_tokens':50}}}).encode()
    def test_shared_ledger_exact_cache_and_thinking_wire(self):
        sent=[]
        def transport(body,*_):sent.append(json.loads(body));return self.response()
        a=explain(self.context,budget_file=self.budget,key_file=self.key,now=self.now,transport=transport)
        b=explain(self.context,budget_file=self.budget,key_file=self.key,now=self.now,transport=lambda *_:self.fail('duplicate bill'))
        self.assertEqual(a['state'],'ready');self.assertEqual(a['advice'],b['advice'])
        self.assertEqual(sent[0]['thinking'],{'type':'enabled'});self.assertEqual(sent[0]['reasoning_effort'],'high')
        ledger=_Ledger(self.budget,self.now);self.assertEqual(len(ledger.calls),1);ledger.close()
        self.assertNotIn('not saved',''.join(p.read_text() for p in self.budget.parent.glob('*.json')))
    def test_response_scope_rejects_invented_store_and_never_retries(self):
        a=explain(self.context,budget_file=self.budget,key_file=self.key,now=self.now,transport=lambda *_:self.response(True))
        self.assertEqual(a['state'],'fallback')
        b=explain(self.context,budget_file=self.budget,key_file=self.key,now=self.now,transport=lambda *_:self.fail())
        self.assertEqual(b['state'],'previous_attempt_unknown')
    def test_freshness_evidence_and_safe_failure_diagnostics(self):
        response=json.loads(self.response());answer=json.loads(response['choices'][0]['message']['content'])
        answer['stores'][0]['evidence_fields']=['generated_at','observations']
        context={**self.context,'generated_at':self.now.isoformat()}
        self.assertEqual(validate_answer(answer,context),answer)
        with self.assertRaisesRegex(ValueError,'analysis_evidence_fields'):validate_answer(answer,self.context)
        answer['stores'][0]['evidence_fields']=['invented_sensitive_content']
        response['choices'][0]['message']['content']=json.dumps(answer)
        result=explain(context,budget_file=self.budget,key_file=self.key,now=self.now,transport=lambda *_:json.dumps(response).encode())
        self.assertEqual(result['state'],'fallback')
        stored=json.loads(next(self.budget.parent.glob('analytics-diagnostic-*.json')).read_text())
        self.assertEqual(stored['validation_error'],'analysis_evidence_fields')
        self.assertNotIn('invented_sensitive_content',json.dumps(stored))
        self.assertIn('generated_at',PROMPT)
    def test_output_requires_all_stores_and_bounded_text(self):
        response=json.loads(self.response());answer=json.loads(response['choices'][0]['message']['content'])
        answer['stores'][0]['recommendation']='字'*201
        with self.assertRaisesRegex(ValueError,'analysis_text_length'):validate_answer(answer,self.context)
        answer['stores']=[]
        with self.assertRaisesRegex(ValueError,'analysis_store_count'):validate_answer(answer,self.context)
    def test_explicit_closed_hours_analysis_and_idempotent_request(self):
        job=self.root/'analytics-job.json';now=self.now.replace(hour=0);calls=[]
        def suggest(*a,**k):calls.append(k);return {'state':'ready','advice':None}
        write_state(job,{'report_id':'b'*64,'request_id':'1'*32,'context':self.context,'expires_at':(now+timedelta(seconds=90)).isoformat()})
        tick(self.root,worker.prepare_analysis_budget,worker.business_window,now=now,suggest=suggest)
        tick(self.root,worker.prepare_analysis_budget,worker.business_window,now=now,suggest=suggest)
        self.assertEqual(len(calls),1)
        self.assertEqual(json.loads((self.root/'analytics-result.json').read_text())['state'],'ready')
        write_state(job,{'report_id':'b'*64,'request_id':'2'*32,'context':self.context,'expires_at':(now+timedelta(seconds=90)).isoformat()})
        tick(self.root,worker.prepare_analysis_budget,worker.business_window,now=now,suggest=suggest)
        self.assertEqual(len(calls),2)
        tick(self.root,worker.prepare_analysis_budget,worker.business_window,now=self.now,suggest=lambda *_a,**_k:self.fail())
    def test_explicit_repeat_calls_model_and_retry_same_intent_does_not(self):
        calls=[]
        def transport(*_):calls.append(1);return self.response()
        for request_id in ('1'*32,'1'*32,'2'*32):
            explain(self.context,budget_file=self.budget,key_file=self.key,now=self.now,transport=transport,request_id=request_id)
        self.assertEqual(len(calls),2)
    def test_late_manual_analysis_retains_day_charges_and_cap(self):
        ledger=_Ledger(self.budget,self.now);ledger.reserve({'public_input_sha256':'a'*64},'a'*64,self.now)
        charged=ledger.calls[0][0]['reserved_cost_microunits'];ledger.close()
        late=self.now.replace(hour=15)
        path=worker.prepare_analysis_budget(self.root,'thinking',late)
        self.assertEqual(json.loads(path.read_text())['max_reserved_cost_microunits']+charged,8000000)
        self.assertEqual(worker.prepare_analysis_budget(self.root,'thinking',late),path)
        result=explain(self.context,budget_file=path,key_file=self.key,now=late,transport=lambda *_:self.response())
        self.assertEqual(result['state'],'ready')
        self.assertTrue(self.budget.exists())
    def test_exhausted_shared_budget_cannot_be_reset_for_analytics(self):
        value=json.loads(self.budget.read_text());value['max_calls']=1;self.budget.write_text(json.dumps(value))
        explain(self.context,budget_file=self.budget,key_file=self.key,now=self.now,transport=lambda *_:self.response())
        with self.assertRaises(DeepSeekError):explain({**self.context,'report_id':'c'*64},budget_file=self.budget,key_file=self.key,now=self.now,transport=lambda *_:self.fail())
