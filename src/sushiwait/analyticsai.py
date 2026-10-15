"""DeepSeek explains public aggregates using the existing shared daily ledger."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from .analytics import POLICY
from .credentials import _read_private_file
from .deepseek import _Ledger, _key, _wire, _https_post, _usage, _cost, MODEL, DeepSeekError
from .intake import _canonical
from .outcomes import _json

PROMPT = '''你是门店排队数据分析员。输入只有已保存的公共统计摘要。只用这些事实，以简洁中文解释数据可用性、门店差异和下一步收集建议。
readiness_score 是数据准备度启发式评分，不是预测准确率；展示号码不是人数、桌数或真实入座数；采集空白不等于无客流；peak_hours 是已有连续可比号码推进的高峰参考，不是发号高峰。hourly_summary每行为[小时,观测组数,有号占比百分比,号码推进速度每分钟,有观测天数]；daily_summary每行为[日期,观测组数,覆盖15分钟格数,已到时段格数]，最多最近14个已有日期。不得编造等待时间、实际客流、准确率或新数据。
输出严格JSON，字段仅 summary 和 stores。summary为不超过240字的总结。stores是数组，每项仅store_id、finding、recommendation、evidence_fields。finding和recommendation各不超过200字，evidence_fields为用到的输入数值字段名数组。每个门店ID必须来自输入。给出可执行且按数据缺口排序的收集建议，不能建议批量真实取号。不要输出Markdown、推理过程或外部链接。'''
FIELDS = {'observations','successful','empty','failed','missing','coverage_percent','days_observed','comparable_intervals','readiness_score','peak_hours','sampling_gaps','hourly_summary','daily_summary'}


def write_state(path, value):
    path=Path(path); raw=json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
    if len(raw)>100000: raise ValueError('analytics_state_too_large')
    fd,name=tempfile.mkstemp(prefix='.analytics-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream: stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.chmod(name,0o600);os.replace(name,path)
        parent=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(parent)
        finally:os.close(parent)
    finally:
        if os.path.exists(name):os.unlink(name)


def read_state(path):
    return _json(_read_private_file(Path(path)))


def validate_answer(value, context):
    ids={r['store_id'] for r in context['stores']}
    if type(value) is not dict or set(value)!={'summary','stores'} or type(value['summary']) is not str or not 1<=len(value['summary'])<=240:
        raise ValueError('invalid_analysis_answer')
    if type(value['stores']) is not list or len(value['stores'])!=len(ids): raise ValueError('invalid_analysis_answer')
    seen=set()
    for row in value['stores']:
        if (type(row) is not dict or set(row)!={'store_id','finding','recommendation','evidence_fields'}
                or row['store_id'] not in ids or row['store_id'] in seen
                or any(type(row[k]) is not str or not 1<=len(row[k])<=200 for k in ('finding','recommendation'))
                or type(row['evidence_fields']) is not list or not row['evidence_fields']
                or any(type(k) is not str or k not in FIELDS for k in row['evidence_fields'])):
            raise ValueError('invalid_analysis_answer')
        seen.add(row['store_id'])
    return value


def explain(context, *, budget_file, key_file, now=None, transport=None):
    if context.get('policy')!=POLICY or context.get('scope')!='public_aggregates_only': raise ValueError('invalid_analysis_context')
    now=now or datetime.now(timezone.utc)
    digest=hashlib.sha256(_canonical({'prompt_version':'analytics-1','context':context}).encode()).hexdigest()
    ledger=_Ledger(budget_file,now)
    try:
        for call,result in ledger.calls:
            if call['observation_key']==digest:
                advice=result.get('advice') if result else None
                if advice:validate_answer(advice['answer'],context)
                return {'state':'ready' if advice else 'previous_attempt_unknown','advice':advice,'network_performed':False}
        mode=ledger.budget.get('thinking_mode','thinking')
        wire=json.loads(_wire(context,ledger.budget['max_output_tokens'],mode));wire['messages'][0]['content']=PROMPT
        if mode=='thinking':wire['reasoning_effort']='high'
        body=_canonical(wire).encode()
        if len(body)+4096>ledger.budget['max_prompt_tokens']:raise DeepSeekError('deepseek_input_too_large')
        key=_key(key_file);ledger.check_budget(now)
        ledger.publish('public-input-'+digest+'.json',context)
        ledger.reserve({'public_input_sha256':digest},digest,now)
        began=time.monotonic();response=_json((transport or _https_post)(body,key,ledger.budget['timeout_seconds']))
        usage=_usage(response);advice=None
        overrun=usage['prompt_tokens']>ledger.budget['max_prompt_tokens'] or usage['completion_tokens']>ledger.budget['max_output_tokens']
        ended=datetime.now(timezone.utc) if transport is None else now
        try:
            if overrun or time.monotonic()-began>=ledger.budget['timeout_seconds']:raise ValueError('invalid_usage')
            if response['model'] not in (MODEL,'deepseek-v4.1-flash','DeepSeek-V4.1-Flash') or response['object']!='chat.completion' or len(response['choices'])!=1:raise ValueError('invalid_model')
            choice=response['choices'][0];message=choice['message']
            if choice['index']!=0 or choice['finish_reason']!='stop' or message['role']!='assistant' or message.get('tool_calls'):raise ValueError('invalid_choice')
            answer=validate_answer(_json(message['content']),context);ledger.check_budget(ended)
            advice={'policy':POLICY,'model':MODEL,'mode':mode,'report_id':context['report_id'],'generated_at':ended.isoformat(),'answer':answer}
        except Exception:pass
        ledger.publish(f'deepseek-result-{len(ledger.calls)}.json',{'public_input_sha256':digest,
            'transport_basis':'official_https' if transport is None else 'injected_transport','usage':usage,'advice':advice,'overrun':overrun})
        return {'state':'ready' if advice else 'fallback','advice':advice,'network_performed':transport is None,
            'estimated_cost_microunits':_cost(usage['prompt_tokens'],usage['completion_tokens'],ledger.budget)}
    finally:ledger.close()


def tick(root, prepare_budget, business_window, *, now=None, suggest=explain, job_file=None):
    root=Path(root); jobfile=Path(job_file) if job_file else root/'analytics-job.json';output=root/'analytics-result.json'
    if not jobfile.exists():return
    now=now or datetime.now(timezone.utc);job=read_state(jobfile)
    report_id=job.get('report_id')
    if not isinstance(report_id,str) or not re.fullmatch('[0-9a-f]{64}',report_id):return
    expiry=datetime.fromisoformat(job['expires_at'])
    if expiry<=now:return
    old=read_state(output) if output.exists() else {}
    if old.get('report_id')==report_id and old.get('state') in {'ready','fallback','previous_attempt_unknown','deepseek_budget_exhausted','failed'}:return
    start,end=business_window(now)
    state={'report_id':report_id,'updated_at':now.isoformat(),'state':'waiting_for_open','advice':None}
    if not start<=now<end:write_state(output,state);return
    state['state']='running';write_state(output,state)
    try:
        result=suggest(job['context'],budget_file=prepare_budget(root,'thinking',now),key_file=root/'key/key.private.json',now=now)
        state.update({k:result[k] for k in ('state','advice')})
    except Exception as error:
        state.update(state=getattr(error,'error_code','failed'),advice=None)
    state['updated_at']=datetime.now(timezone.utc).isoformat() if now is None else now.isoformat();write_state(output,state)
