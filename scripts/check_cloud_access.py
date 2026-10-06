"""Manually invoked anonymous source probe: fixed pilots, one round, aggregate-only output.

No personal credentials, raw queue labels/counts, source bodies or transport
headers are printed or uploaded. Observations stay in memory and are discarded after
the check; this is network evidence, not a long-term dataset or deployment.
"""
import argparse,json,platform,time
from pathlib import Path
import sushiwait
from sushiwait.remote import RemoteClient,validate_record

PILOTS=('3014','3004','2009')

def run_check(*,client_factory=RemoteClient,system=platform.system):
    summary={'canary_schema_version':1,'version':sushiwait.__version__,
        'source':'crm_remote_v1_1','store_ids':list(PILOTS),'maximum_http_budget':6,
        'attempted_pairs':0,'successful_pairs':0,'checked_pairs':0,'http_attempts':0,
        'http_statuses':[],'retries':0,'stopped_on_failure':False,'ok':False,
        'execution_system':system(),'raw_observations_retained':False,
        'production_deployment_verified':False,'continuous_collection_verified':False,
        'response_store_identity_verified':False,'source_freshness':'unknown',
        'count_unit':'unknown','eta_available':False,'verified_training_labels':0,
        'request_accounting_complete':True,'unrecorded_http_attempts':0}
    if summary['execution_system']!='Linux':
        summary['error_code']='canary_requires_linux';return summary
    started=time.monotonic()
    pending=False
    try:
        client=client_factory()
        for store in PILOTS:
            pending=True
            record=validate_record(client.snapshot(store))
            if record['requested_store_id']!=store:raise ValueError
            summary['attempted_pairs']+=1
            attempts=[q for q in record['queries'].values() if q['attempted']]
            summary['http_attempts']+=len(attempts)
            summary['http_statuses'] += [q['http_status'] for q in attempts]
            summary['successful_pairs']+=int(record['ok']);summary['checked_pairs']+=1
            pending=False
            if not record['ok']:
                summary['stopped_on_failure']=True;break
        summary['ok']=(summary['successful_pairs']==summary['checked_pairs']==3
            and summary['http_attempts']==6 and summary['http_statuses']==[200]*6)
    except (OSError,ValueError,TypeError,KeyError,OverflowError):
        summary['error_code']='canary_record_or_client_error'
        summary['stopped_on_failure']=True
        if pending:
            summary['request_accounting_complete']=False
            summary['unrecorded_http_attempts']='unknown'
    summary['elapsed_seconds']=round(time.monotonic()-started,3)
    return summary

def main():
    parser=argparse.ArgumentParser(description='固定三试点的一次Linux匿名连通性检查；不保留或输出原始数据')
    parser.add_argument('--version-file',type=Path,required=True)
    args=parser.parse_args()
    try:expected=args.version_file.read_text().strip()
    except (OSError,UnicodeError):expected=None
    if expected!=sushiwait.__version__:
        result={'ok':False,'error_code':'canary_installed_version_mismatch','http_attempts':0}
    else:result=run_check()
    print(json.dumps(result,sort_keys=True,allow_nan=False))
    return 0 if result['ok'] else 1

if __name__=='__main__':raise SystemExit(main())
