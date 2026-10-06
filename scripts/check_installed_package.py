"""Check a wheel install outside the checkout, using only a synthetic fixture."""

import argparse
import asyncio
import base64
import contextlib
from datetime import datetime,timedelta,timezone
import io
import json
import hashlib
import hmac
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

import sushiwait
from sushiwait.cli import main
from sushiwait.cli import _recovery_poll_target


def check() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--version-file", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--outcome-fixture", type=Path, required=True)
    parser.add_argument("--evaluation-fixture", type=Path)
    args = parser.parse_args()
    expected = args.version_file.read_text(encoding="utf-8").strip()
    if sushiwait.__version__ != expected:
        raise SystemExit("installed_version_mismatch")
    if Path(sushiwait.__file__).resolve().is_relative_to(args.source_root.resolve()):
        raise SystemExit("package_imported_from_checkout")
    help_result = subprocess.run([sys.executable, "-I", "-m", "sushiwait", "--help"],
        capture_output=True, text=True, timeout=10, check=True)
    if any(command not in help_result.stdout for command in
            ("baseline-backtest", "baseline-research", "outcome-review-draft", "outcome-review-receive", "outcome-reviewed-cohort", "remote-window-quality", "remote-signal-report", "remote-window-collect", "remote-window-serve", "remote-window-status", "remote-serve", "remote-snapshot", "remote-collect", "remote-report", "remote-monitor", "remote-task-status", "capture-import", "context-bridge", "context-surge", "surge-guard", "context-promote", "context-window", "monitor-plan", "monitor-stores", "monitor-collect", "interval-evaluate", "outcome-cohort", "outcome-receive", "outcome-received-cohort", "task-status", "outcome-import", "outcome-report", "date-features", "signal-report", "store-view", "packet-export", "packet-check", "packet-archive", "packet-enqueue", "pending-status", "packet-receiver", "receipt-check", "packet-deliver-local")):
        raise SystemExit("installed_cli_missing_commands")
    bridge_help = subprocess.run([sys.executable, "-I", "-m", "sushiwait",
                                  "context-bridge", "--help"],
        capture_output=True, text=True, timeout=10, check=True)
    if "--diagnostics" not in bridge_help.stdout:
        raise SystemExit("installed_bridge_diagnostics_missing")
    window_help=subprocess.run([sys.executable,"-I","-m","sushiwait","remote-window-serve","--help"],
                               capture_output=True,text=True,timeout=10,check=True)
    if any(flag not in window_help.stdout for flag in ("--resume-if-present","--listen-host")):
        raise SystemExit("installed_container_options_missing")
    surge_help = subprocess.run([sys.executable, "-I", "-m", "sushiwait",
                                 "context-surge", "--help"],
        capture_output=True, text=True, timeout=10, check=True)
    if any(option not in surge_help.stdout for option in ("--credentials-file", "--revision", "--seconds")):
        raise SystemExit("installed_surge_intake_missing")
    guard_help = subprocess.run([sys.executable, "-I", "-m", "sushiwait",
                                 "surge-guard", "--help"],
        capture_output=True, text=True, timeout=10, check=True)
    if "--seconds" not in guard_help.stdout:
        raise SystemExit("installed_surge_guard_missing")
    promotion_help = subprocess.run([sys.executable, "-I", "-m", "sushiwait",
                                     "context-promote", "--help"],
        capture_output=True, text=True, timeout=10, check=True)
    if any(option not in promotion_help.stdout for option in
           ("--credentials-file", "--staged-file", "--expected-revision")):
        raise SystemExit("installed_promotion_missing")
    collect_help = subprocess.run([sys.executable, "-I", "-m", "sushiwait", "collect", "--help"],
        capture_output=True, text=True, timeout=10, check=True)
    if any(option not in collect_help.stdout for option in ("--task-file", "--resume-task", "--transient-failure-budget")):
        raise SystemExit("installed_collection_task_options_missing")
    remote_collect_help = subprocess.run([sys.executable, "-I", "-m", "sushiwait", "remote-collect", "--help"],
        capture_output=True, text=True, timeout=10, check=True)
    if any(option not in remote_collect_help.stdout for option in ("--task-file", "--resume-task")):
        raise SystemExit("installed_remote_task_options_missing")
    with tempfile.TemporaryDirectory() as directory, asyncio.Runner() as service_runner:
        service_loop=service_runner.get_loop()
        database = str(Path(directory).resolve() / "synthetic.sqlite3")
        # Fail immediately if the replay/report smoke path tries any socket I/O.
        with patch("socket.socket", side_effect=AssertionError("unexpected_network")), \
                patch("socket.create_connection", side_effect=AssertionError("unexpected_network")):
            from sushiwait.remote import RemoteClient, QUEUE_NAMES
            class RemoteFakeResponse:
                def __init__(self, request):
                    self.url = request.full_url
                    self.headers = {}
                def getcode(self): return 200
                def geturl(self): return self.url
                def close(self): pass
                def read(self, size):
                    value = ({name: (["12", "12", "13-1"] if name == "storeQueue" else [])
                              for name in QUEUE_NAMES} if "/groupqueues?" in self.url else 0)
                    return json.dumps(value).encode()[:size]
            class RemoteFakeOpener:
                def open(self, request, *, timeout): return RemoteFakeResponse(request)
            remote_db = Path(directory).resolve() / "synthetic-remote.sqlite3"
            remote_output = io.StringIO()
            with patch("sushiwait.remote.RemoteClient", return_value=RemoteClient(opener=RemoteFakeOpener())), \
                    patch("sushiwait.cli.read_credentials_file", side_effect=AssertionError("unexpected_credentials")) as remote_auth, \
                    contextlib.redirect_stdout(remote_output):
                if main(["remote-snapshot", "--db", str(remote_db), "--store-id", "3014"]) != 0:
                    raise SystemExit("installed_remote_snapshot_failed")
                before = remote_db.read_bytes()
                if main(["remote-report", "--db", str(remote_db), "--store-id", "3014"]) != 0:
                    raise SystemExit("installed_remote_report_failed")
            remote_values = [json.loads(line) for line in remote_output.getvalue().splitlines()]
            if (remote_auth.call_count or remote_db.read_bytes() != before
                    or remote_values[1]["successful_pairs"] != 1
                    or remote_values[1]["latest_record"]["queries"]["storequeuecount"]["payload"] != {"raw_count": 0, "unit": "unknown"}
                    or remote_values[1]["latest_record"]["queries"]["groupqueues"]["payload"]["queues"]["storeQueue"] != ["12", "12", "13-1"]
                    or remote_values[1]["latest_record"]["atomic_snapshot"]
                    or remote_values[1]["eta_available"]):
                raise SystemExit("installed_remote_semantics_failed")
            print(json.dumps({"installed_remote_snapshot_and_report_ok": True,
                "remote_is_live_acceptance": False, "remote_socket_calls": 0,
                "remote_query_credentials_accessed": False, "remote_report_database_unchanged": True}))
            signal_output=io.StringIO();signal_before=remote_db.read_bytes()
            with patch('sushiwait.remote.RemoteClient',side_effect=AssertionError('unexpected_client')) as signal_client, \
                    patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('unexpected_credentials')) as signal_auth, \
                    contextlib.redirect_stdout(signal_output):
                if main(['remote-signal-report','--db',str(remote_db),'--store-id','3014',
                         '--as-of',datetime.now(timezone.utc).isoformat()])!=0:
                    raise SystemExit('installed_remote_signal_failed')
            signal=json.loads(signal_output.getvalue())
            if (signal_client.call_count or signal_auth.call_count or signal_before!=remote_db.read_bytes()
                    or signal['scan']['admitted_pair_rows']!=1 or signal['source']!='crm_remote_v1_1'
                    or set(signal['endpoints']['groupqueues']['queues'])!=set(QUEUE_NAMES)
                    or signal['endpoints']['groupqueues']['queues']['storeQueue']['whole']['comparable_pairs']
                    or signal['endpoints']['storequeuecount']['reported_count']['unit']!='unknown'
                    or signal['true_no_show_rate'] is not None or signal['historical_availability_verified']
                    or signal['eta_available'] or signal['network_performed']
                    or str(remote_db) in signal_output.getvalue() or '\"13-1\"' in signal_output.getvalue()):
                raise SystemExit('installed_remote_signal_semantics_failed')
            print(json.dumps({'installed_remote_signal_ok':True,'remote_signal_admitted_synthetic_pairs':1,
                'remote_signal_database_unchanged':True,'remote_signal_socket_calls':0,
                'remote_signal_query_credentials_accessed':False,'remote_signal_clients_created':0,
                'remote_signal_is_live_acceptance':False}))
            from sushiwait.remote import RemoteStore
            from sushiwait.remoteintake import read_receipt
            from sushiwait.remote import validate_record
            with RemoteStore(remote_db,read_only=True) as receipt_db:
                intake_run,intake_body=receipt_db.db.execute('SELECT run_id,payload_json FROM remote_samples').fetchone()
                intake_stored=json.loads(intake_body)
                intake_at=read_receipt(intake_stored,validate_record(intake_stored),intake_run)
            intake_output=io.StringIO()
            with patch('sushiwait.remote.RemoteClient',side_effect=AssertionError('unexpected_client')) as intake_client, \
                    patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('unexpected_credentials')) as intake_auth, \
                    contextlib.redirect_stdout(intake_output):
                for at in (intake_at-timedelta(microseconds=1),intake_at):
                    if main(['remote-signal-report','--db',str(remote_db),'--store-id','3014',
                             '--as-of',at.isoformat(),'--availability-basis','local-first-receipt'])!=0:
                        raise SystemExit('installed_remote_intake_failed')
            intake_values=[json.loads(v) for v in intake_output.getvalue().splitlines()]
            if (intake_client.call_count or intake_auth.call_count or signal_before!=remote_db.read_bytes()
                    or [v['scan'].get('admitted_pair_rows',0) for v in intake_values]!=[0,1]
                    or any(v['availability_basis']!='local-first-receipt' or v['durable_availability_verified']
                           or v['historical_availability_verified'] or v['independent_time_attestation']
                           or v['eta_available'] for v in intake_values)):
                raise SystemExit('installed_remote_intake_semantics_failed')
            print(json.dumps({'installed_remote_intake_ok':True,'synthetic_first_receipt_cutoff_rows':[0,1],
                'remote_intake_database_unchanged':True,'socket_calls':0,'credentials_accessed':False,
                'clients_created':0,'is_live_acceptance':False}))
            remote_plan = Path(directory).resolve() / "synthetic-remote-plan.json"
            arrival = (datetime.now(timezone.utc) + timedelta(minutes=16)).isoformat()
            remote_plan.write_text(json.dumps({"schema_version": 1, "plans": [
                {"store_id": "3014", "desired_arrival_at": arrival}] * 2}))
            remote_plan.chmod(0o600)
            plan_before = remote_plan.read_bytes()
            monitor_output = io.StringIO()
            monitor_db = Path(directory).resolve() / "synthetic-remote-monitor.sqlite3"
            with patch("sushiwait.remote.RemoteClient", return_value=RemoteClient(opener=RemoteFakeOpener())), \
                    patch("sushiwait.cli.read_credentials_file", side_effect=AssertionError("unexpected_credentials")) as monitor_auth, \
                    contextlib.redirect_stdout(monitor_output):
                if main(["remote-monitor", "--db", str(monitor_db), "--store-id", "3014",
                         "--plan-file", str(remote_plan), "--max-pairs", "1"]) != 0:
                    raise SystemExit("installed_remote_monitor_failed")
            monitor_values = [json.loads(line) for line in monitor_output.getvalue().splitlines()]
            summary = monitor_values[-1]["remote_monitor_summary"]
            if (monitor_auth.call_count or remote_plan.read_bytes() != plan_before
                    or len(monitor_values) != 2 or summary["pairs_started"] != 1
                    or summary["successful_pairs"] != 1 or summary["requests_attempted"] != 2
                    or summary["maximum_request_budget"] != 2 or not summary["scheduler_applied"]
                    or summary["eta_available"] or summary["personal_plan_details_in_output"]
                    or arrival in monitor_output.getvalue()):
                raise SystemExit("installed_remote_monitor_semantics_failed")
            print(json.dumps({"installed_remote_monitor_ok": True,
                "remote_monitor_coalesced_pair_count": 1,
                "remote_monitor_socket_calls": 0, "remote_monitor_query_credentials_accessed": False,
                "remote_monitor_plan_file_unchanged": True, "remote_monitor_is_live_acceptance": False}))
            from sushiwait.remotetasks import RemoteTask, remote_task_status
            task_db = Path(directory).resolve() / "synthetic-remote-task.sqlite3"
            task_file = Path(directory).resolve() / "synthetic-remote-task.json"
            task_args = ["remote-collect", "--db", str(task_db), "--store-id", "3014",
                "--task-file", str(task_file), "--samples", "3", "--interval", "30"]
            task_clock = [0]
            task_base = datetime(2026,10,6,tzinfo=timezone.utc)
            original_reconcile = RemoteTask.reconcile
            def interrupt_after_saved_pair(task, *, now, interrupted=False):
                if not interrupted: raise KeyboardInterrupt
                return original_reconcile(task, now=now, interrupted=interrupted)
            task_output = io.StringIO()
            with patch("sushiwait.remote.RemoteClient", return_value=RemoteClient(opener=RemoteFakeOpener())), \
                    patch("sushiwait.cli._utc_clock", side_effect=lambda:task_base+timedelta(seconds=task_clock[0])), \
                    patch("sushiwait.remote._utc", side_effect=lambda:(task_base+timedelta(seconds=task_clock[0])).isoformat()), \
                    patch("sushiwait.cli.time.monotonic", side_effect=lambda:task_clock[0]), \
                    patch("sushiwait.cli.time.sleep", side_effect=lambda seconds:task_clock.__setitem__(0,task_clock[0]+seconds)), \
                    patch("sushiwait.cli.read_credentials_file", side_effect=AssertionError("unexpected_credentials")) as task_auth, \
                    contextlib.redirect_stdout(task_output):
                with patch.object(RemoteTask, "reconcile", new=interrupt_after_saved_pair):
                    if main(task_args) != 130:raise SystemExit("installed_remote_task_interrupt_failed")
                if not remote_task_status(task_file)['pending_attempt']:
                    raise SystemExit("installed_remote_task_checkpoint_missing")
                task_clock[0] = 10
                if main(task_args+["--resume-task"]) != 0:raise SystemExit("installed_remote_task_resume_failed")
                before_task_db,before_task_file = task_db.read_bytes(),task_file.read_bytes()
                if main(["remote-task-status", "--task-file", str(task_file)]) != 0:
                    raise SystemExit("installed_remote_task_status_failed")
                if main(task_args+["--resume-task"]) != 0:
                    raise SystemExit("installed_remote_task_completed_resume_failed")
            task_status = remote_task_status(task_file)
            if (task_auth.call_count or task_status['successful_pairs'] != 3
                    or task_status['recorded_http_attempts'] != 6 or task_status['uncertain_pair_slots']
                    or not task_status['all_slots_successful'] or task_clock[0] != 70
                    or task_db.read_bytes() != before_task_db or task_file.read_bytes() != before_task_file
                    or str(task_db) in task_output.getvalue() or str(task_file) in task_output.getvalue()):
                raise SystemExit("installed_remote_task_restart_semantics_failed")
            print(json.dumps({"installed_remote_task_restart_ok": True,
                "remote_task_successful_pairs": 3, "remote_task_recorded_http_attempts": 6,
                "remote_task_saved_pair_not_requeried": True, "remote_task_socket_calls": 0,
                "remote_task_query_credentials_accessed": False,
                "remote_task_terminal_database_unchanged": True, "remote_task_is_live_acceptance": False}))
            from sushiwait.remoteservice import RemoteQueueService, RemoteASGI
            class ServiceOpener(RemoteFakeOpener):
                def __init__(self):self.calls=0
                def open(self,request,*,timeout):
                    self.calls+=1
                    return super().open(request,timeout=timeout)
            service_opener=ServiceOpener()
            service=RemoteQueueService(db=Path(directory).resolve()/"synthetic-service.sqlite3",
                task_file=Path(directory).resolve()/"synthetic-service-task.json",store_ids=['3014'],
                interval=30,samples=2,client_factory=lambda:RemoteClient(opener=service_opener))
            app=RemoteASGI(service)
            async def service_smoke():
                inputs=asyncio.Queue();started=asyncio.Event();events=[]
                async def receive_lifecycle():return await inputs.get()
                async def send_lifecycle(event):
                    events.append(event)
                    if event['type']=='lifespan.startup.complete':started.set()
                lifecycle=asyncio.create_task(app({'type':'lifespan'},receive_lifecycle,send_lifecycle))
                await inputs.put({'type':'lifespan.startup'})
                try:
                    await asyncio.wait_for(started.wait(),3)
                    async def saved():
                        while service.status()['task']['completed_pair_slots']!=1:await asyncio.sleep(.001)
                    await asyncio.wait_for(saved(),3)
                    for unused in range(5):
                        replies=[]
                        async def receive_http():return {'type':'http.request','body':b'','more_body':False}
                        async def send_http(event):replies.append(event)
                        await app({'type':'http','method':'GET','path':'/api/v1/stores/3014/queue','query_string':b''},receive_http,send_http)
                        view=json.loads(replies[1]['body'])
                        if (replies[0]['status']!=200 or view['fields']['groupqueues']['state']!='recent_response'
                                or view['fields']['groupqueues']['payload']['queues']['storeQueue']!=['12','12','13-1']
                                or view['fields']['storequeuecount']['payload']!={'raw_count':0,'unit':'unknown'}
                                or view['network_performed_by_read'] or view['eta_available']):
                            raise SystemExit('installed_remote_service_view_failed')
                finally:
                    await inputs.put({'type':'lifespan.shutdown'})
                    await asyncio.wait_for(lifecycle,3)
                if [e['type'] for e in events]!=['lifespan.startup.complete','lifespan.shutdown.complete']:
                    raise SystemExit('installed_remote_service_lifecycle_failed')
            with patch("sushiwait.cli.read_credentials_file",side_effect=AssertionError('unexpected_credentials')) as service_auth:
                # asyncio's own selector needs a local socket pair; use a loop
                # created before the outer socket guard, then run no socket I/O.
                service_loop.run_until_complete(service_smoke())
            if (service_opener.calls!=2 or service_auth.call_count or service.status()['worker_alive']
                    or service.status()['task']['completed_pair_slots']!=1):
                raise SystemExit('installed_remote_service_ownership_failed')
            print(json.dumps({'installed_remote_service_asgi_ok':True,'service_saved_pairs':1,
                'service_synthetic_http_attempts':2,'service_display_reads':5,'service_extra_upstream_requests':0,
                'service_socket_calls':0,'service_query_credentials_accessed':False,'service_is_live_acceptance':False}))
            from sushiwait.remotewindow import RemoteWindowTask,remote_window_status
            window_db=Path(directory).resolve()/"synthetic-window.sqlite3"
            window_task=Path(directory).resolve()/"synthetic-window-task.json"
            window_plan=Path(directory).resolve()/"synthetic-window-plan.json"
            window_clock=[0.0];window_starts=[]
            window_arrival=(task_base+timedelta(minutes=16)).isoformat()
            window_plan.write_text(json.dumps({'schema_version':1,'plans':[
                {'store_id':'3014','desired_arrival_at':window_arrival}]*2}))
            window_plan.chmod(0o600);window_plan_before=window_plan.read_bytes()
            class WindowOpener(RemoteFakeOpener):
                def open(self,request,*,timeout):
                    if '/groupqueues?' in request.full_url:window_starts.append(window_clock[0])
                    return super().open(request,timeout=timeout)
            window_args=['remote-window-collect','--db',str(window_db),'--task-file',str(window_task),
                '--plan-file',str(window_plan),'--store-id','3014','--base-interval','300',
                '--duration','151','--max-pairs','10']
            original_window_reconcile=RemoteWindowTask.reconcile
            def interrupt_window(task,*,now,interrupted=False):
                if not interrupted:raise KeyboardInterrupt
                return original_window_reconcile(task,now=now,interrupted=interrupted)
            window_output=io.StringIO()
            with patch('sushiwait.remote.RemoteClient',return_value=RemoteClient(opener=WindowOpener())), \
                    patch('sushiwait.cli._utc_clock',side_effect=lambda:task_base+timedelta(seconds=window_clock[0])), \
                    patch('sushiwait.remote._utc',side_effect=lambda:(task_base+timedelta(seconds=window_clock[0])).isoformat()), \
                    patch('sushiwait.cli.time.monotonic',side_effect=lambda:window_clock[0]), \
                    patch('sushiwait.cli.time.sleep',side_effect=lambda seconds:window_clock.__setitem__(0,window_clock[0]+seconds)), \
                    patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('unexpected_credentials')) as window_auth, \
                    contextlib.redirect_stdout(window_output):
                with patch.object(RemoteWindowTask,'reconcile',new=interrupt_window):
                    if main(window_args)!=130:raise SystemExit('installed_window_interrupt_failed')
                pending_status=remote_window_status(window_task)
                if not pending_status['pending_attempt']:raise SystemExit('installed_window_pending_missing')
                window_clock[0]=10
                if main(window_args+['--resume-task'])!=0:raise SystemExit('installed_window_resume_failed')
                before_window=window_db.read_bytes(),window_task.read_bytes()
                with patch('sushiwait.remote.RemoteClient',side_effect=AssertionError('terminal_client')):
                    if main(window_args+['--resume-task'])!=0:raise SystemExit('installed_window_terminal_failed')
                    if main(['remote-window-status','--task-file',str(window_task)])!=0:
                        raise SystemExit('installed_window_status_failed')
                    window_quality_output=io.StringIO()
                    with contextlib.redirect_stdout(window_quality_output):
                        if main(['remote-window-quality','--db',str(window_db),'--task-file',str(window_task),
                                '--as-of',(task_base+timedelta(seconds=window_clock[0])).isoformat(),
                                '--max-gap','90'])!=0:
                            raise SystemExit('installed_window_quality_failed')
                    quality=json.loads(window_quality_output.getvalue())
                    if (quality['rows_examined']!=5 or quality['recorded_http_attempts']!=10
                            or not quality['checkpoint_chain_verified'] or quality['analysis_interval_seconds']!=151
                            or quality['stores'][0]['run_boundaries']!=1 or quality['network_performed']
                            or quality['continuous_collection_verified'] or quality['eta_available']
                            or quality['verified_training_labels']):
                        raise SystemExit('installed_window_quality_semantics_failed')
            window_status=remote_window_status(window_task)
            if (window_starts!=[0,60,90,120,150] or window_clock[0]!=151 or window_auth.call_count
                    or window_status['deadline_at']!=pending_status['deadline_at']
                    or window_status['maximum_pair_budget']!=10 or window_status['recorded_http_attempts']!=10
                    or window_status['successful_pairs']!=5 or window_status['uncertain_pair_slots']
                    or window_status['end_reason']!='deadline' or window_status['eta_available']
                    or window_plan.read_bytes()!=window_plan_before
                    or before_window!=(window_db.read_bytes(),window_task.read_bytes())
                    or any(private in window_output.getvalue() for private in
                        (str(window_db),str(window_task),str(window_plan),window_arrival))):
                raise SystemExit('installed_window_persistence_semantics_failed')
            print(json.dumps({'installed_remote_window_restart_ok':True,'window_saved_pairs':5,
                'window_synthetic_http_attempts':10,'window_saved_pair_not_requeried':True,
                'window_shared_60_to_30_seconds_applied':True,'window_original_deadline_and_budget_preserved':True,
                'window_plan_file_unchanged':True,'window_socket_calls':0,'window_query_credentials_accessed':False,
                'window_terminal_database_unchanged':True,'window_is_live_acceptance':False}))
            print(json.dumps({'installed_remote_window_quality_ok':True,
                'quality_rows_examined':5,'quality_recorded_synthetic_http_attempts':10,
                'quality_run_boundary_preserved':True,'quality_database_and_task_unchanged':True,
                'quality_socket_calls':0,'quality_credentials_accessed':False,'quality_is_live_acceptance':False}))
            with patch("sushiwait.cli.time.monotonic", return_value=6):
                if (_recovery_poll_target(30, 1, 30) != 30
                        or _recovery_poll_target(30, 2, 30) != 36
                        or _recovery_poll_target(30, 2, 30, 0) != 30
                        or _recovery_poll_target(30, 2, 30, 19) != 49):
                    raise SystemExit("installed_recovery_target_semantics_mismatch")
            with patch("sushiwait.cli.time.monotonic", return_value=45):
                if _recovery_poll_target(30, 1, 30) != 30:
                    raise SystemExit("installed_late_recovery_target_reset")
            evaluation_input = Path(directory).resolve() / "synthetic-evaluation.json"
            evaluation_fixture = (args.evaluation_fixture or
                                  args.source_root / "examples/fixtures/evaluation-01.synthetic.json")
            evaluation_input.write_bytes(evaluation_fixture.read_bytes())
            evaluation_input.chmod(0o600)
            evaluation_output = io.StringIO()
            with patch("sushiwait.cli.read_credentials_file", side_effect=AssertionError("unexpected_credentials")) as auth, \
                    patch("sushiwait.surgeguard._command", side_effect=AssertionError("unexpected_native_command")) as native, \
                    patch("subprocess.Popen", side_effect=AssertionError("unexpected_child")) as child, \
                    contextlib.redirect_stdout(evaluation_output):
                if main(["interval-evaluate", "--input", str(evaluation_input)]) != 0:
                    raise SystemExit("installed_interval_evaluation_failed")
            evaluation_summary = json.loads(evaluation_output.getvalue())
            if (auth.call_count or native.call_count or child.call_count
                    or evaluation_summary["data_origin"] != "synthetic"
                    or evaluation_summary["interval_labels_evaluated"] != 2
                    or evaluation_summary["mean_absolute_error_seconds"] != {"lower": 60, "upper": 210}
                    or evaluation_summary["coverage"]["lower"] != .5
                    or evaluation_summary["coverage"]["upper"] != 1
                    or evaluation_summary["model_performance_verified"]
                    or evaluation_summary["verified_training_labels"] != 0
                    or not evaluation_summary["output_requires_private_handling"]):
                raise SystemExit("installed_interval_evaluation_semantics_mismatch")
            guard_output = io.StringIO()
            with patch("sushiwait.surgeguard._command", side_effect=AssertionError("unexpected_native_command")) as native, \
                    contextlib.redirect_stdout(guard_output):
                if main(["surge-guard", "--seconds", "0"]) != 1:
                    raise SystemExit("installed_guard_invalid_window_accepted")
            if (native.call_count != 0 or
                    json.loads(guard_output.getvalue())["error_code"] != "surge_guard_invalid_window"):
                raise SystemExit("installed_guard_validation_semantics_mismatch")
            promotion_output = io.StringIO()
            with patch("sushiwait.promotion.read_state", side_effect=AssertionError("unexpected_native_command")) as native, \
                    contextlib.redirect_stdout(promotion_output):
                if main(["context-promote", "--credentials-file", "unused-current.json",
                         "--staged-file", "unused-staged.json", "--expected-revision", "0"]) != 1:
                    raise SystemExit("installed_promotion_invalid_revision_accepted")
            if (native.call_count != 0 or
                    json.loads(promotion_output.getvalue())["error_code"] != "promotion_invalid_input"):
                raise SystemExit("installed_promotion_validation_semantics_mismatch")
            window_output = io.StringIO()
            with patch("sushiwait.window._spawn_guard", side_effect=AssertionError("unexpected_guard_child")) as child, \
                    patch("sushiwait.window.read_credentials_file", side_effect=AssertionError("unexpected_credentials")) as context, \
                    patch("sushiwait.surgeguard._command", side_effect=AssertionError("unexpected_native_command")) as native, \
                    contextlib.redirect_stdout(window_output):
                if main(["context-window", "--credentials-file", "synthetic-main", "--staged-file", "synthetic-stage",
                         "--revision", "2", "--seconds", "0", "--collector-paused"]) != 1:
                    raise SystemExit("installed_window_invalid_seconds_failed")
            if (child.call_count or context.call_count or native.call_count
                    or json.loads(window_output.getvalue())["error_code"] != "window_invalid_input"):
                raise SystemExit("installed_window_validation_semantics_mismatch")
            monitoring_output = io.StringIO()
            with patch("sushiwait.credentials.read_credentials_file", side_effect=AssertionError("unexpected_credentials")), \
                    patch("sushiwait.surgeguard._command", side_effect=AssertionError("unexpected_native_command")), \
                    contextlib.redirect_stdout(monitoring_output):
                if main(["monitor-plan", "--desired-arrival-at", "2026-10-06T19:00:00+08:00",
                         "--as-of", "2026-10-06T18:40:00+08:00", "--base-interval", "300",
                         "--call-offset-minutes", "-10"]) != 0:
                    raise SystemExit("installed_monitoring_failed")
            monitoring = json.loads(monitoring_output.getvalue())
            if (monitoring["target_call_at"] != "2026-10-06T10:50:00.000000Z"
                    or monitoring["requested_interval_seconds"] != 30 or monitoring["eta_available"]
                    or monitoring["scheduler_applied"] or monitoring["notification_sent"]):
                raise SystemExit("installed_monitoring_semantics_mismatch")
            shared_file = Path(directory).resolve() / "synthetic-plans.json"
            shared_file.write_text(json.dumps({"schema_version": 1, "plans": [
                {"store_id": "3004", "desired_arrival_at": "2026-10-06T19:00:00+08:00"},
                {"store_id": "3004", "desired_arrival_at": "2026-10-06T18:50:00+08:00"}]}))
            shared_file.chmod(0o600)
            shared_output = io.StringIO()
            with patch("sushiwait.credentials.read_credentials_file", side_effect=AssertionError("unexpected_credentials")), \
                    patch("sushiwait.surgeguard._command", side_effect=AssertionError("unexpected_native_command")), \
                    contextlib.redirect_stdout(shared_output):
                if main(["monitor-stores", "--plans-file", str(shared_file), "--as-of",
                         "2026-10-06T18:40:00+08:00", "--base-interval", "300"]) != 0:
                    raise SystemExit("installed_shared_monitoring_failed")
            shared_policy = json.loads(shared_output.getvalue())
            if (shared_policy["store_count"] != 1 or shared_policy["plan_count"] != 2
                    or shared_policy["stores"][0]["requested_interval_seconds"] != 30
                    or shared_policy["stores"][0]["requested_queries_per_due"] != 1
                    or shared_policy["scheduler_applied"] or shared_policy["eta_available"]
                    or not shared_policy["output_requires_private_handling"]):
                raise SystemExit("installed_shared_monitoring_semantics_mismatch")
            from types import SimpleNamespace
            from sushiwait.client import QueryResult
            from sushiwait.credentials import QueryCredentials
            recovery_simulated_queries = 0
            for persistent in (False, True):
                simulated_time, simulated_starts = [0], []
                recovery_base = datetime(2020,1,1,tzinfo=timezone.utc)
                def simulated_wall():
                    return recovery_base+timedelta(seconds=simulated_time[0])
                def simulated_sleep(seconds):
                    simulated_time[0]+=seconds
                def synthetic_context(revision, expiry):
                    encode=lambda raw:base64.urlsafe_b64encode(raw).decode().rstrip('=')
                    token='.'.join((encode(b'{"alg":"HS256"}'),encode(json.dumps({
                        'iat':int(recovery_base.timestamp()),'exp':int((recovery_base+timedelta(seconds=expiry)).timestamp()),
                        'synthetic_revision':revision}).encode()),encode(b'synthetic-only-signature')))
                    return QueryCredentials('miniapp_gateway',revision,token,'synthetic-client','synthetic-code',
                        'Synthetic Agent/1.0','https://synthetic.invalid/reference','application/json')
                old_context,new_context=synthetic_context(1,35),synthetic_context(2,3600)
                def synthetic_source(_source):
                    return new_context if simulated_time[0]>=45 else old_context
                def synthetic_fetch(store_id):
                    simulated_starts.append((store_id,simulated_time[0]));stamp=simulated_wall().isoformat()
                    return QueryResult(True,{'id':int(store_id),'name':'synthetic store'},None,200,stamp,stamp,0)
                simulated_output=io.StringIO()
                collection_args=['collect','--api-profile','miniapp_gateway','--store-id','900001','--store-id','900002',
                    '--credentials-file',str(Path(directory).resolve()/'unused-query-context'),
                    '--db',str(Path(directory).resolve()/f'synthetic-recovery-{persistent}.sqlite3'),
                    '--samples','3','--interval','30','--wait-for-credentials','60']
                if persistent:collection_args+=['--task-file',str(Path(directory).resolve()/'synthetic-recovery-task.json')]
                with patch('sushiwait.credentials.CredentialSource.current',new=synthetic_source), \
                        patch('sushiwait.cli.client_for',return_value=SimpleNamespace(fetch_store=synthetic_fetch)), \
                        patch('sushiwait.cli.time.monotonic',side_effect=lambda:simulated_time[0]), \
                        patch('sushiwait.cli.time.sleep',side_effect=simulated_sleep), \
                        patch('sushiwait.cli._utc_clock',side_effect=simulated_wall), \
                        patch('sushiwait.credentials.read_credentials_file',side_effect=AssertionError('unexpected_query_context')) as auth, \
                        patch('sushiwait.surgeguard._command',side_effect=AssertionError('unexpected_native')) as native, \
                        patch('subprocess.Popen',side_effect=AssertionError('unexpected_child')) as child, \
                        contextlib.redirect_stdout(simulated_output):
                    if main(collection_args)!=0:raise SystemExit('installed_multistore_simulation_failed')
                events=[json.loads(line) for line in simulated_output.getvalue().splitlines()]
                if (auth.call_count or native.call_count or child.call_count
                        or simulated_starts!=[('900001',0),('900002',0),('900001',45),('900002',45),('900001',75),('900002',75)]
                        or sum(row.get('event')=='credentials_resumed' for row in events)!=1):
                    raise SystemExit('installed_multistore_simulation_semantics_mismatch')
                recovery_simulated_queries+=len(simulated_starts)
            from sushiwait.storage import SnapshotStore
            from sushiwait.tasks import task_status
            transient_simulated_queries = 0
            for persistent in (False, True):
                simulated_time, simulated_starts = [0], []
                context = synthetic_context(1, 3600)
                def transient_fetch(store_id):
                    number = len(simulated_starts)
                    simulated_starts.append((store_id, simulated_time[0]))
                    began = simulated_wall().isoformat()
                    if number == 0:
                        simulated_time[0] += 45
                        return QueryResult(False, None, 'http_error', 504, began,
                                           simulated_wall().isoformat(), 45000)
                    return QueryResult(True, {'id': int(store_id), 'storeStatus': 'OPEN'}, None, 200,
                                       began, began, 0)
                policy_db = Path(directory).resolve()/f'synthetic-transient-{persistent}.sqlite3'
                policy_task = Path(directory).resolve()/'synthetic-transient-task.json'
                policy_args = ['collect', '--api-profile', 'miniapp_gateway', '--store-id', '900001',
                    '--store-id', '900002', '--credentials-file', str(Path(directory).resolve()/'unused-policy-context'),
                    '--db', str(policy_db), '--samples', '2', '--interval', '30', '--transient-failure-budget', '1']
                if persistent: policy_args += ['--task-file', str(policy_task)]
                policy_output = io.StringIO()
                with patch('sushiwait.credentials.CredentialSource.current', return_value=context), \
                        patch('sushiwait.cli.client_for', return_value=SimpleNamespace(fetch_store=transient_fetch)), \
                        patch('sushiwait.cli.time.monotonic', side_effect=lambda: simulated_time[0]), \
                        patch('sushiwait.cli.time.sleep', side_effect=simulated_sleep), \
                        patch('sushiwait.cli._utc_clock', side_effect=simulated_wall), \
                        patch('sushiwait.credentials.read_credentials_file', side_effect=AssertionError('unexpected_credentials')) as auth, \
                        patch('sushiwait.surgeguard._command', side_effect=AssertionError('unexpected_native')) as native, \
                        patch('subprocess.Popen', side_effect=AssertionError('unexpected_child')) as child, \
                        contextlib.redirect_stdout(policy_output):
                    if main(policy_args) != 1:
                        raise SystemExit('installed_transient_failure_exit_lost')
                events = [json.loads(line) for line in policy_output.getvalue().splitlines()]
                with SnapshotStore(policy_db, read_only=True) as db:
                    counts = db.db.execute('SELECT COUNT(*),SUM(ok) FROM samples').fetchone()
                if (auth.call_count or native.call_count or child.call_count or counts != (4, 3)
                        or simulated_starts != [('900001', 0), ('900002', 45), ('900001', 75), ('900002', 75)]
                        or sum(row.get('event') == 'transient_query_failure_recorded' for row in events) != 1):
                    raise SystemExit('installed_transient_policy_semantics_mismatch')
                if persistent:
                    state = task_status(policy_task)
                    if (state['state'] != 'completed' or state['failed_slots'] != 1
                            or state['transient_failure_budget'] != 1 or state['all_slots_successful']):
                        raise SystemExit('installed_transient_task_failure_lost')
                transient_simulated_queries += len(simulated_starts)
            linked_before = [hashlib.sha256(p.read_bytes()).digest() for p in (policy_db, policy_task)]
            linked_reports = []
            with patch('sushiwait.cli.read_credentials_file', side_effect=AssertionError('unexpected_credentials')) as auth, \
                    patch('sushiwait.surgeguard._command', side_effect=AssertionError('unexpected_native')) as native, \
                    patch('subprocess.Popen', side_effect=AssertionError('unexpected_child')) as child, \
                    patch('fcntl.flock', side_effect=AssertionError('unexpected_worker_lock')):
                for sid in ('900001', '900002'):
                    linked_output = io.StringIO()
                    with contextlib.redirect_stdout(linked_output):
                        if main(['store-view', '--db', str(policy_db), '--store-id', sid,
                            '--api-profile', 'miniapp_gateway', '--data-origin', 'live',
                            '--as-of', simulated_wall().isoformat(), '--task-file', str(policy_task)]) != 0:
                            raise SystemExit('installed_linked_store_view_failed')
                    linked_reports.append(json.loads(linked_output.getvalue()))
            if (auth.call_count or native.call_count or child.call_count
                    or linked_before != [hashlib.sha256(p.read_bytes()).digest() for p in (policy_db, policy_task)]
                    or any(v['availability'] != 'last_known_only' or not v['collector_state_available']
                        or v['collector_checkpoint']['state'] != 'completed'
                        or v['collector_checkpoint']['failed_slots'] != 1
                        or v['collector_checkpoint']['all_slots_successful']
                        or v['collector_liveness'] != 'unknown' for v in linked_reports)):
                raise SystemExit('installed_linked_store_view_semantics_mismatch')
            adaptive_file = Path(directory).resolve() / "adaptive-plans.json"
            adaptive_database = str(Path(directory).resolve() / "adaptive.sqlite3")
            adaptive_file.write_text(json.dumps({"schema_version":1,"plans":[
                {"store_id":"900001","desired_arrival_at":"2020-01-01T00:10:00Z"},
                {"store_id":"900001","desired_arrival_at":"2020-01-01T00:12:00Z"}]}))
            adaptive_file.chmod(0o600)
            adaptive_before = adaptive_file.read_bytes()
            adaptive_clock, adaptive_starts = [0], []
            def adaptive_wall():
                return datetime(2020,1,1,tzinfo=timezone.utc)+timedelta(seconds=adaptive_clock[0])
            def adaptive_wait(deadline):
                adaptive_clock[0] = deadline
            def adaptive_observe(_client, store_id, _database, **_options):
                adaptive_starts.append((store_id, adaptive_clock[0]))
                return True
            adaptive_session = SimpleNamespace(args=SimpleNamespace(api_profile="miniapp_gateway"),
                client=lambda: object(), wait_until=adaptive_wait)
            adaptive_output = io.StringIO()
            with patch("sushiwait.cli._QuerySession", return_value=adaptive_session), \
                    patch("sushiwait.cli._utc_clock", side_effect=adaptive_wall), \
                    patch("sushiwait.cli.time.monotonic", side_effect=lambda:adaptive_clock[0]), \
                    patch("sushiwait.cli.observe", side_effect=adaptive_observe), \
                    patch("sushiwait.credentials.read_credentials_file", side_effect=AssertionError("unexpected_credentials")) as auth, \
                    patch("sushiwait.surgeguard._command", side_effect=AssertionError("unexpected_native_command")) as native, \
                    patch("subprocess.Popen", side_effect=AssertionError("unexpected_child")) as child, \
                    contextlib.redirect_stdout(adaptive_output):
                if main(["monitor-collect", "--plans-file", str(adaptive_file), "--db", adaptive_database,
                         "--credentials-file", str(Path(directory).resolve()/"unused-context.json"),
                         "--store-id", "900001", "--api-profile", "miniapp_gateway",
                         "--duration-seconds", "90"]) != 0:
                    raise SystemExit("installed_adaptive_simulation_failed")
            adaptive_summary = json.loads(adaptive_output.getvalue())
            if (auth.call_count or native.call_count or child.call_count
                    or adaptive_starts != [("900001",0),("900001",30),("900001",60)]
                    or adaptive_file.read_bytes() != adaptive_before
                    or adaptive_summary["successful_queries"] != 3
                    or not adaptive_summary["scheduler_applied"]
                    or adaptive_summary["eta_available"]
                    or adaptive_summary["missed_call_prevention_guaranteed"]):
                raise SystemExit("installed_adaptive_simulation_semantics_mismatch")
            with contextlib.redirect_stdout(io.StringIO()):
                if main(["replay", "--fixture", str(args.fixture), "--db", database]) != 0:
                    raise SystemExit("installed_replay_failed")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                if main(["report", "--db", database]) != 0:
                    raise SystemExit("installed_report_failed")
            group = json.loads(output.getvalue())["groups"][0]
            view_before = hashlib.sha256(Path(database).read_bytes()).digest()
            view_reports = []
            view_at = datetime.fromisoformat(group["last_received_at"].replace("Z", "+00:00"))
            with patch("sushiwait.cli.read_credentials_file", side_effect=AssertionError("unexpected_credentials")) as auth, \
                    patch("sushiwait.surgeguard._command", side_effect=AssertionError("unexpected_native_command")) as native, \
                    patch("subprocess.Popen", side_effect=AssertionError("unexpected_child")) as child:
                for second in (0, 91):
                    view_output = io.StringIO()
                    with contextlib.redirect_stdout(view_output):
                        if main(["store-view", "--db", database, "--store-id", group["store_id"],
                            "--api-profile", group["api_profile"], "--data-origin", "synthetic",
                            "--as-of", (view_at + timedelta(seconds=second)).isoformat()]) != 0:
                            raise SystemExit("installed_store_view_failed")
                    view_reports.append(json.loads(view_output.getvalue()))
            if (auth.call_count or native.call_count or child.call_count
                    or view_before != hashlib.sha256(Path(database).read_bytes()).digest()
                    or [v["availability"] for v in view_reports] != ["recent_response", "stale"]
                    or [v["display_is_last_known"] for v in view_reports] != [False, True]
                    or view_reports[0]["last_response"]["display"] != view_reports[1]["last_response"]["display"]
                    or any(v["network_performed"] or v["eta_available"]
                           or v["source_freshness"] != "unknown" for v in view_reports)):
                raise SystemExit("installed_store_view_semantics_mismatch")
            packet_file = Path(directory).resolve() / "public-fields.json"
            packet_before = hashlib.sha256(Path(database).read_bytes()).digest()
            packet_output = io.StringIO()
            with patch("sushiwait.cli.read_credentials_file", side_effect=AssertionError("unexpected_credentials")) as auth, \
                    patch("sushiwait.surgeguard._command", side_effect=AssertionError("unexpected_native_command")) as native, \
                    patch("subprocess.Popen", side_effect=AssertionError("unexpected_child")) as child, \
                    contextlib.redirect_stdout(packet_output):
                if main(["packet-export", "--db", database, "--output", str(packet_file),
                         "--store-id", group["store_id"], "--api-profile", group["api_profile"],
                         "--data-origin", "synthetic", "--as-of", group["last_received_at"]]) != 0:
                    raise SystemExit("installed_packet_export_failed")
            packet_summary = json.loads(packet_output.getvalue())
            packet = json.loads(packet_file.read_bytes())
            if (auth.call_count or native.call_count or child.call_count
                    or packet_before != hashlib.sha256(Path(database).read_bytes()).digest()
                    or packet_summary["record_count"] != 1 or packet_summary["server_received"]
                    or packet_summary["credentials_accessed"] or packet["eta_available"]
                    or packet["verified_training_labels"] != 0
                    or packet["records"][0]["display"]["groupQueues"]["groups"]["boothQueue"]["value"] != ["B001"]):
                raise SystemExit("installed_packet_export_semantics_mismatch")
            archive_database = Path(directory).resolve() / "packet-archive.sqlite3"
            archive_summaries = []
            with patch("sushiwait.cli.read_credentials_file", side_effect=AssertionError("unexpected_credentials")) as auth, \
                    patch("sushiwait.surgeguard._command", side_effect=AssertionError("unexpected_native_command")) as native, \
                    patch("subprocess.Popen", side_effect=AssertionError("unexpected_child")) as child, \
                    patch("sushiwait.cli._utc_clock",return_value=datetime.fromisoformat(group["last_received_at"].replace("Z","+00:00"))):
                for command in (["packet-check","--input",str(packet_file)],
                                ["packet-archive","--input",str(packet_file),"--db",str(archive_database)],
                                ["packet-archive","--input",str(packet_file),"--db",str(archive_database)]):
                    archive_output = io.StringIO()
                    with contextlib.redirect_stdout(archive_output):
                        if main(command) != 0:
                            raise SystemExit("installed_packet_archive_failed")
                    archive_summaries.append(json.loads(archive_output.getvalue()))
            if (auth.call_count or native.call_count or child.call_count
                    or [s.get("inserted_records") for s in archive_summaries] != [None,1,0]
                    or archive_summaries[2]["duplicate_records"] != 1
                    or any(s["server_received"] or s["source_claims_verified"] for s in archive_summaries)
                    or packet_before != hashlib.sha256(Path(database).read_bytes()).digest()):
                raise SystemExit("installed_packet_archive_semantics_mismatch")
            pending_directory = Path(directory).resolve() / "pending-packets"
            pending_directory.mkdir(mode=0o700)
            pending_summaries = []
            with patch("sushiwait.cli.read_credentials_file", side_effect=AssertionError("unexpected_credentials")) as auth, \
                    patch("sushiwait.surgeguard._command", side_effect=AssertionError("unexpected_native_command")) as native, \
                    patch("subprocess.Popen", side_effect=AssertionError("unexpected_child")) as child, \
                    patch("sushiwait.cli._utc_clock",return_value=datetime.fromisoformat(group["last_received_at"].replace("Z","+00:00"))):
                for command in (["packet-enqueue","--input",str(packet_file),"--directory",str(pending_directory)],
                                ["packet-enqueue","--input",str(packet_file),"--directory",str(pending_directory)],
                                ["pending-status","--directory",str(pending_directory)]):
                    pending_output = io.StringIO()
                    with contextlib.redirect_stdout(pending_output):
                        if main(command) != 0:
                            raise SystemExit("installed_pending_packet_failed")
                    pending_summaries.append(json.loads(pending_output.getvalue()))
            if (auth.call_count or native.call_count or child.call_count
                    or pending_summaries[0]["already_queued"] or not pending_summaries[1]["already_queued"]
                    or pending_summaries[2]["pending_packets"] != 1
                    or pending_summaries[2]["pending_record_occurrences"] != 1
                    or any(s["server_received"] or s["source_claims_verified"] for s in pending_summaries)
                    or packet_before != hashlib.sha256(Path(database).read_bytes()).digest()):
                raise SystemExit("installed_pending_packet_semantics_mismatch")
            from sushiwait.receiver import receive_packet
            receiver_database = Path(directory).resolve() / "receiver-archive.sqlite3"
            with patch("sushiwait.cli.read_credentials_file",side_effect=AssertionError("unexpected_query_auth")) as auth, \
                    patch("sushiwait.cli.read_receiver_token",side_effect=AssertionError("unexpected_receiver_auth")) as receiver_auth, \
                    patch("subprocess.Popen",side_effect=AssertionError("unexpected_child")) as child, \
                    patch("sushiwait.surgeguard._command",side_effect=AssertionError("unexpected_native_command")) as native:
                at=datetime.fromisoformat(group["last_received_at"].replace("Z","+00:00"))
                receipts=[receive_packet(packet_file.read_bytes(),receiver_database,now=at) for _ in range(2)]
                invalid_receiver=io.StringIO()
                with contextlib.redirect_stdout(invalid_receiver):
                    if main(["packet-receiver","--db",str(receiver_database),
                             "--receiver-token-file",str(Path(directory)/"absent.token"),"--seconds","0"])!=1:
                        raise SystemExit("installed_receiver_invalid_limits_accepted")
            if (auth.call_count or receiver_auth.call_count or child.call_count or native.call_count
                    or [s["inserted_records"] for s in receipts]!=[1,0]
                    or receipts[1]["duplicate_records"]!=1
                    or any(s["remote_deployment_verified"] or s["source_claims_verified"] for s in receipts)
                    or json.loads(invalid_receiver.getvalue())["error_code"]!="receiver_invalid_limits"):
                raise SystemExit("installed_receiver_semantics_mismatch")
            from sushiwait.packets import encoded
            receiver_key="SYNTHETIC_INSTALLED_RECEIVER_KEY_0123456789"
            receiver_key_file=Path(directory).resolve()/"synthetic-receiver.token"
            receiver_key_file.write_text(receiver_key);receiver_key_file.chmod(0o600)
            confirmation_file=Path(directory).resolve()/"confirmation.json"
            signed_receipt={"ok":True,**receipts[1],"receipt_schema_version":2}
            signed_receipt["receipt_hmac_sha256"]=hmac.new(receiver_key.encode(),encoded(signed_receipt),hashlib.sha256).hexdigest()
            delivery_summaries=[]
            with patch("sushiwait.delivery._exchange",return_value=signed_receipt) as exchange, \
                    patch("sushiwait.cli._utc_clock",return_value=at), \
                    patch("sushiwait.cli.read_credentials_file",side_effect=AssertionError("unexpected_query_auth")) as delivery_auth, \
                    patch("subprocess.Popen",side_effect=AssertionError("unexpected_child")) as delivery_child, \
                    patch("sushiwait.surgeguard._command",side_effect=AssertionError("unexpected_native_command")) as delivery_native:
                for command in (["packet-deliver-local","--input",str(packet_file),"--receiver-token-file",str(receiver_key_file),
                                 "--confirmation",str(confirmation_file),"--port","12345"],
                                ["receipt-check","--input",str(packet_file),"--receiver-token-file",str(receiver_key_file),
                                 "--confirmation",str(confirmation_file)]):
                    delivery_output=io.StringIO()
                    with contextlib.redirect_stdout(delivery_output):
                        if main(command)!=0:raise SystemExit("installed_delivery_failed")
                    delivery_summaries.append(json.loads(delivery_output.getvalue()))
            if (exchange.call_count!=1 or delivery_auth.call_count or delivery_child.call_count or delivery_native.call_count
                    or not delivery_summaries[0]["durability_confirmed"]
                    or not all(s["receipt_hmac_verified"] and s["confirmed_record_count"]==1 for s in delivery_summaries)
                    or not delivery_summaries[1]["historical_confirmation_only"]
                    or delivery_summaries[1]["network_performed"]
                    or any(s["source_claims_verified"] or s["remote_deployment_verified"] for s in delivery_summaries)):
                raise SystemExit("installed_delivery_semantics_mismatch")
            from sushiwait.tasks import CollectionTask
            from sushiwait.storage import SnapshotStore
            task_path = str(Path(directory).resolve() / "collection.json")
            config = {"db": database, "api_profile": group["api_profile"], "store_ids": [group["store_id"]],
                      "interval": 60, "samples": 1, "wait_for_credentials": 0}
            with CollectionTask(task_path, config=config, resume=False, now=datetime.now(timezone.utc)) as task:
                task.prepare_database()
                with SnapshotStore(database) as db:
                    task.bind(db, now=datetime.now(timezone.utc))
            task_output = io.StringIO()
            with contextlib.redirect_stdout(task_output):
                if main(["task-status", "--task-file", task_path]) != 0:
                    raise SystemExit("installed_task_status_failed")
            signal_output = io.StringIO()
            with contextlib.redirect_stdout(signal_output):
                if main(["signal-report", "--db", database, "--store-id", group["store_id"],
                         "--api-profile", group["api_profile"], "--data-origin", "synthetic",
                         "--as-of", group["last_received_at"]]) != 0:
                    raise SystemExit("installed_signals_failed")
            outcome_database = str(Path(directory).resolve() / "outcomes.sqlite3")
            intake_database = str(Path(directory).resolve() / "received-outcomes.sqlite3")
            intake_reports = []
            with patch("sushiwait.cli.read_credentials_file", side_effect=AssertionError("unexpected_credentials")) as auth, \
                    patch("sushiwait.cli.SnapshotStore", side_effect=AssertionError("unexpected_public_store")) as public_store, \
                    patch("sushiwait.surgeguard._command", side_effect=AssertionError("unexpected_native")) as native, \
                    patch("subprocess.Popen", side_effect=AssertionError("unexpected_child")) as child:
                intake_output = io.StringIO()
                with contextlib.redirect_stdout(intake_output):
                    if main(["outcome-receive", "--synthetic-fixture", str(args.outcome_fixture), "--db", intake_database]) != 0:
                        raise SystemExit("installed_outcome_receive_failed")
                if not json.loads(intake_output.getvalue())["committed"]:
                    raise SystemExit("installed_outcome_receive_not_committed")
                intake_before = hashlib.sha256(Path(intake_database).read_bytes()).digest()
                duplicate_output = io.StringIO()
                with contextlib.redirect_stdout(duplicate_output):
                    if main(["outcome-receive", "--synthetic-fixture", str(args.outcome_fixture), "--db", intake_database]) != 0:
                        raise SystemExit("installed_outcome_receive_duplicate_failed")
                if not json.loads(duplicate_output.getvalue())["idempotent"]:
                    raise SystemExit("installed_outcome_receive_duplicate_rewritten")
                for at in ("2020-10-01T11:30:00+08:00", datetime.now(timezone.utc).isoformat()):
                    intake_output = io.StringIO()
                    with contextlib.redirect_stdout(intake_output):
                        if main(["outcome-received-cohort", "--db", intake_database, "--as-of", at,
                            "--data-origin", "synthetic", "--api-profile", "miniapp_gateway"]) != 0:
                            raise SystemExit("installed_received_cohort_failed")
                    intake_reports.append(json.loads(intake_output.getvalue()))
            if (auth.call_count or public_store.call_count or native.call_count or child.call_count
                    or intake_before != hashlib.sha256(Path(intake_database).read_bytes()).digest()
                    or [r["selected_episodes"] for r in intake_reports] != [0, 1]
                    or any(r["availability_basis"] != "local_first_receipt_time"
                           or r["historical_availability_verified"] or r["training_eligible"]
                           or r["verified_training_labels"] != 0 for r in intake_reports)):
                raise SystemExit("installed_outcome_intake_semantics_mismatch")
            review_directory = Path(directory).resolve() / 'private-reviews'
            review_directory.mkdir(mode=0o700)
            review_file, review_db = review_directory/'draft.json', review_directory/'reviews.sqlite3'
            review_output = io.StringIO()
            with patch('socket.socket', side_effect=AssertionError('unexpected_review_network')) as review_socket, \
                    patch('sushiwait.cli.read_credentials_file', side_effect=AssertionError('unexpected_review_auth')) as review_auth, \
                    patch('sushiwait.cli.client_for', side_effect=AssertionError('unexpected_review_client')) as review_client, \
                    contextlib.redirect_stdout(review_output):
                if main(['outcome-review-draft','--source-db',intake_database,'--synthetic-fixture',str(args.outcome_fixture),
                         '--output',str(review_file)]) != 0:
                    raise SystemExit('installed_review_draft_failed')
                review = json.loads(review_file.read_text())
                if review['decision'] != 'insufficient_evidence' or review['called_time_bounds_checked']:
                    raise SystemExit('installed_review_draft_approved')
                review.update(decision='accept', reason_code='confirmed_call_interval', issued_time_bounds_checked=True,
                              called_time_bounds_checked=True, store_and_queue_checked=True)
                review_file.write_text(json.dumps(review))
                if main(['outcome-review-receive','--source-db',intake_database,'--db',str(review_db),'--input',str(review_file)]) != 0:
                    raise SystemExit('installed_review_receive_failed')
                review_before = review_db.read_bytes()
                for at in ('2020-10-01T11:30:00+08:00',datetime.now(timezone.utc).isoformat()):
                    if main(['outcome-reviewed-cohort','--source-db',intake_database,'--db',str(review_db),'--as-of',at,
                             '--data-origin','synthetic','--api-profile','miniapp_gateway']) != 0:
                        raise SystemExit('installed_review_cohort_failed')
            review_reports = [json.loads(v) for v in review_output.getvalue().splitlines()]
            if (review_socket.call_count or review_auth.call_count or review_client.call_count
                    or review_before != review_db.read_bytes()
                    or intake_before != hashlib.sha256(Path(intake_database).read_bytes()).digest()
                    or [r['accepted_synthetic_call_intervals'] for r in review_reports[2:]] != [0,1]
                    or any(r['authenticity_verified'] or r['verified_training_labels'] for r in review_reports)
                    or any(review[key] in review_output.getvalue() for key in
                           ('review_id','reviewer_id','episode_id','episode_receipt_sha256'))):
                raise SystemExit('installed_review_semantics_failed')
            print(json.dumps({'installed_outcome_reviews_ok':True,'synthetic_review_cutoff_counts':[0,1],
                'source_and_review_files_unchanged':True,'review_socket_calls':0,'review_credentials_accessed':False,
                'real_labels_admitted':0,'authenticity_verified':False,'is_live_acceptance':False}))
            baseline_directory = Path(directory).resolve()/'private-baseline'
            baseline_directory.mkdir(mode=0o700)
            baseline_input, baseline_output = baseline_directory/'plan.json',baseline_directory/'result.json'
            episode = json.loads(args.outcome_fixture.read_text())
            plan = {'schema_version':1,'as_of':datetime.now(timezone.utc).isoformat(),
                'data_origin':'synthetic','api_profile':episode['api_profile'],'store_id':episode['store_id'],
                'queue_type':episode['queue_type'],'party_size':episode['party_size'],'table_type':episode['table_type'],
                'mode':'new_join','minimum_samples':1,'target_episode_id':None}
            baseline_input.write_text(json.dumps(plan))
            baseline_input.chmod(0o600)
            baseline_logs=io.StringIO()
            with patch('socket.socket',side_effect=AssertionError('unexpected_baseline_network')) as baseline_socket, \
                    patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('unexpected_baseline_auth')) as baseline_auth, \
                    patch('sushiwait.cli.client_for',side_effect=AssertionError('unexpected_baseline_client')) as baseline_client, \
                    contextlib.redirect_stdout(baseline_logs):
                if main(['baseline-research','--source-db',intake_database,'--reviews-db',str(review_db),
                         '--input',str(baseline_input),'--output',str(baseline_output)]) != 0:
                    raise SystemExit('installed_baseline_failed')
            candidate=json.loads(baseline_output.read_text())
            summary=json.loads(baseline_logs.getvalue())
            if (not candidate['forecast']['research_forecast_available'] or candidate['eta_available']
                    or candidate['authenticity_verified'] or candidate['verified_training_labels']
                    or baseline_socket.call_count or baseline_auth.call_count or baseline_client.call_count
                    or review_before != review_db.read_bytes()
                    or intake_before != hashlib.sha256(Path(intake_database).read_bytes()).digest()
                    or summary['eta_available'] or not summary['durability_confirmed']
                    or baseline_output.stat().st_mode & 0o777 != 0o600
                    or episode['episode_id'] in baseline_logs.getvalue() or episode['store_id'] in baseline_logs.getvalue()):
                raise SystemExit('installed_baseline_semantics_failed')
            print(json.dumps({'installed_baseline_research_ok':True,'synthetic_forecast_available':True,
                'source_and_review_files_unchanged':True,'baseline_socket_calls':0,'baseline_credentials_accessed':False,
                'real_labels_admitted':0,'eta_available':False,'is_live_acceptance':False}))
            backtest_plan={key:plan[key] for key in ('schema_version','as_of','data_origin','api_profile','store_id','minimum_samples')}
            backtest_plan.update(elapsed_seconds=[0],max_cases=100)
            backtest_input,backtest_output=baseline_directory/'backtest-plan.json',baseline_directory/'backtest.json'
            backtest_input.write_text(json.dumps(backtest_plan));backtest_input.chmod(0o600)
            backtest_logs=io.StringIO()
            with patch('socket.socket',side_effect=AssertionError('unexpected_backtest_network')) as backtest_socket, \
                    patch('sushiwait.cli.read_credentials_file',side_effect=AssertionError('unexpected_backtest_auth')) as backtest_auth, \
                    patch('sushiwait.cli.client_for',side_effect=AssertionError('unexpected_backtest_client')) as backtest_client, \
                    contextlib.redirect_stdout(backtest_logs):
                if main(['baseline-backtest','--source-db',intake_database,'--reviews-db',str(review_db),
                         '--input',str(backtest_input),'--output',str(backtest_output)])!=0:
                    raise SystemExit('installed_backtest_failed')
            replay=json.loads(backtest_output.read_text());summary=json.loads(backtest_logs.getvalue())
            if (replay['reviewed_scope_episodes']!=1 or replay['attempted_cases']!=1 or replay['scored_cases']!=0
                    or replay['cases'][0]['research_forecast_available'] or replay['forecast_log_verified']
                    or replay['historical_target_context_verified']
                    or replay['model_performance_verified'] or replay['eta_available'] or replay['verified_training_labels']
                    or not summary['durability_confirmed'] or backtest_output.stat().st_mode & 0o777!=0o600
                    or backtest_socket.call_count or backtest_auth.call_count or backtest_client.call_count
                    or review_before!=review_db.read_bytes()
                    or intake_before!=hashlib.sha256(Path(intake_database).read_bytes()).digest()
                    or episode['episode_id'] in backtest_logs.getvalue() or episode['store_id'] in backtest_logs.getvalue()):
                raise SystemExit('installed_backtest_semantics_failed')
            print(json.dumps({'installed_baseline_backtest_ok':True,'synthetic_late_receipt_cases':1,
                'synthetic_scored_cases':0,'source_and_review_files_unchanged':True,'backtest_socket_calls':0,
                'backtest_credentials_accessed':False,'real_labels_admitted':0,'eta_available':False,'is_live_acceptance':False}))
            with contextlib.redirect_stdout(io.StringIO()):
                if main(["outcome-check", "--synthetic-fixture", str(args.outcome_fixture)]) != 0:
                    raise SystemExit("installed_outcome_check_failed")
                if main(["outcome-import", "--synthetic-fixture", str(args.outcome_fixture),
                         "--db", outcome_database]) != 0:
                    raise SystemExit("installed_outcome_import_failed")
            outcome_output = io.StringIO()
            with contextlib.redirect_stdout(outcome_output):
                if main(["outcome-report", "--db", outcome_database]) != 0:
                    raise SystemExit("installed_outcome_report_failed")
            cohort_before = hashlib.sha256(Path(outcome_database).read_bytes()).digest()
            cohort_reports = []
            with patch("sushiwait.cli.read_credentials_file", side_effect=AssertionError("unexpected_credentials")) as auth, \
                    patch("sushiwait.cli.SnapshotStore", side_effect=AssertionError("unexpected_public_store")) as public_store, \
                    patch("sushiwait.surgeguard._command", side_effect=AssertionError("unexpected_native_command")) as native, \
                    patch("subprocess.Popen", side_effect=AssertionError("unexpected_child")) as child:
                for at in ("2020-10-01T11:30:00+08:00", "2020-10-01T10:59:59+08:00"):
                    cohort_output = io.StringIO()
                    with contextlib.redirect_stdout(cohort_output):
                        if main(["outcome-cohort", "--db", outcome_database, "--as-of", at,
                                 "--data-origin", "synthetic", "--api-profile", "miniapp_gateway"]) != 0:
                            raise SystemExit("installed_cohort_failed")
                    cohort_reports.append(json.loads(cohort_output.getvalue()))
            if (auth.call_count or public_store.call_count or native.call_count or child.call_count
                    or cohort_before != hashlib.sha256(Path(outcome_database).read_bytes()).digest()
                    or [r["selected_episodes"] for r in cohort_reports] != [1, 0]
                    or cohort_reports[0]["unverified_called_wait_candidates"] != 1
                    or any(r["verified_training_labels"] != 0 or r["historical_availability_verified"]
                           or not r["output_requires_private_handling"] for r in cohort_reports)):
                raise SystemExit("installed_cohort_semantics_mismatch")
            calendar_output = io.StringIO()
            with contextlib.redirect_stdout(calendar_output):
                if main(["date-features", "--at", "2026-10-10T12:00:00+08:00",
                         "--as-of", "2026-10-04T12:00:00Z"]) != 0:
                    raise SystemExit("installed_calendar_failed")
        report = json.loads(output.getvalue())
        if not report["groups"] or any(g["data_origin"] != "synthetic" for g in report["groups"]):
            raise SystemExit("installed_report_origin_mismatch")
        outcome_report = json.loads(outcome_output.getvalue())
        if (outcome_report["origins"] != {"synthetic": 1} or outcome_report["verified_training_labels"] != 0
                or outcome_report["eta_available"]):
            raise SystemExit("installed_outcome_report_semantics_mismatch")
        calendar_report = json.loads(calendar_output.getvalue())
        if (calendar_report["date_type"] != "makeup_workday" or calendar_report["eta_available"]
                or calendar_report["store_open_status"] != "unknown"):
            raise SystemExit("installed_calendar_semantics_mismatch")
        signal_summary = json.loads(signal_output.getvalue())
        if (signal_summary["scan"].get("valid_successful_rows") != 1
                or signal_summary["eta_available"] or signal_summary["true_no_show_rate"] is not None
                or signal_summary["network_performed"]):
            raise SystemExit("installed_signals_semantics_mismatch")
        task_summary = json.loads(task_output.getvalue())
        if (task_summary["task_schema_version"] != 1 or task_summary["completed_slots"] != 0
                or task_summary["network_performed"] or task_summary["eta_available"]):
            raise SystemExit("installed_task_status_semantics_mismatch")
    print(json.dumps({"installed_version": expected, "import_outside_checkout": True,
        "cli_help_ok": True, "bridge_diagnostic_option_ok": True, "surge_intake_help_ok": True,
        "surge_guard_help_ok": True, "guard_invalid_window_ok": True,
        "guard_validation_socket_calls": 0, "guard_validation_native_cli_calls": 0,
        "promotion_help_ok": True, "promotion_invalid_revision_ok": True,
        "promotion_validation_socket_calls": 0, "promotion_validation_native_cli_calls": 0,
        "window_invalid_seconds_ok": True, "window_validation_child_process_calls": 0,
        "window_validation_socket_calls": 0, "window_validation_native_cli_calls": 0,
        "window_validation_credentials_accessed": False,
        "monitoring_policy_ok": True, "monitoring_policy_socket_calls": 0,
        "monitoring_policy_native_cli_calls": 0, "monitoring_policy_credentials_accessed": False,
        "shared_monitoring_policy_ok": True, "shared_monitoring_socket_calls": 0,
        "shared_monitoring_native_cli_calls": 0, "shared_monitoring_credentials_accessed": False,
        "adaptive_scheduler_simulation_ok": True, "adaptive_simulated_queries": 3,
        "adaptive_simulation_socket_calls": 0, "adaptive_simulation_native_cli_calls": 0,
        "adaptive_simulation_child_process_calls": 0, "adaptive_simulation_credentials_accessed": False,
        "adaptive_plan_unchanged": True, "adaptive_simulation_is_live_acceptance": False,
        "single_store_recovery_target_ok": True, "multistore_restart_full_period_ok": True,
        "multistore_completion_bound_ok": True, "multistore_cli_recovery_simulation_ok": True,
        "multistore_recovery_simulated_queries": recovery_simulated_queries,
        "multistore_recovery_transport_stubbed": True, "multistore_recovery_is_live_acceptance": False,
        "multistore_recovery_query_credentials_accessed": False,
        "multistore_recovery_native_cli_calls": 0, "multistore_recovery_child_process_calls": 0,
        "recovery_target_socket_calls": 0,
        "transient_query_policy_simulation_ok": True,
        "transient_query_policy_simulated_queries": transient_simulated_queries,
        "transient_query_policy_transport_stubbed": True, "transient_query_policy_is_live_acceptance": False,
        "transient_query_policy_socket_calls": 0, "transient_query_policy_query_credentials_accessed": False,
        "transient_query_policy_native_cli_calls": 0, "transient_query_policy_child_process_calls": 0,
        "interval_evaluation_ok": True, "evaluation_socket_calls": 0,
        "evaluation_native_cli_calls": 0, "evaluation_child_process_calls": 0,
        "evaluation_query_credentials_accessed": False,
        "private_claim_cohort_ok": True, "cohort_database_unchanged": True,
        "cohort_socket_calls": 0, "cohort_native_cli_calls": 0,
        "cohort_child_process_calls": 0, "cohort_query_credentials_accessed": False,
        "cohort_public_snapshot_store_accessed": False,
        "synthetic_replay_ok": True, "readonly_report_ok": True,
        "replay_report_socket_calls": 0, "synthetic_outcomes_ok": True,
        "outcomes_socket_calls": 0, "verified_training_labels": 0,
        "outcome_intake_ok": True, "outcome_intake_synthetic_cli_calls": 4,
        "outcome_intake_duplicate_and_projection_files_unchanged": True,
        "outcome_intake_socket_calls": 0, "outcome_intake_query_credentials_accessed": False,
        "outcome_intake_public_store_calls": 0, "outcome_intake_native_cli_calls": 0,
        "outcome_intake_child_process_calls": 0, "outcome_intake_is_live_acceptance": False,
        "calendar_package_data_ok": True, "calendar_socket_calls": 0,
        "readonly_signal_report_ok": True, "signal_socket_calls": 0,
        "readonly_store_view_ok": True, "store_view_database_unchanged": True,
        "store_view_socket_calls": 0, "store_view_native_cli_calls": 0,
        "store_view_child_process_calls": 0, "store_view_query_credentials_accessed": False,
        "store_view_is_live_acceptance": False,
        "linked_store_views_ok": True, "linked_store_views_simulated_count": 2,
        "linked_store_views_files_unchanged": True, "linked_store_views_socket_calls": 0,
        "linked_store_views_query_credentials_accessed": False,
        "linked_store_views_native_cli_calls": 0, "linked_store_views_child_process_calls": 0,
        "linked_store_views_worker_lock_calls": 0, "linked_store_views_is_live_acceptance": False,
        "public_field_packet_export_ok": True, "packet_database_unchanged": True,
        "packet_socket_calls": 0, "packet_native_cli_calls": 0,
        "packet_child_process_calls": 0, "packet_query_credentials_accessed": False,
        "packet_server_received": False,
        "local_packet_archive_ok": True, "packet_archive_duplicate_records": 1,
        "packet_archive_socket_calls": 0, "packet_archive_native_cli_calls": 0,
        "packet_archive_child_process_calls": 0, "packet_archive_credentials_accessed": False,
        "packet_archive_source_claims_verified": False, "packet_archive_server_received": False,
        "persistent_pending_packets_ok": True, "pending_packet_count": 1,
        "pending_socket_calls": 0, "pending_native_cli_calls": 0,
        "pending_child_process_calls": 0, "pending_credentials_accessed": False,
        "pending_server_received": False,
        "packet_receiver_core_ok": True, "receiver_invalid_limits_ok": True,
        "receiver_socket_calls": 0, "receiver_query_credentials_accessed": False,
        "receiver_invalid_limits_token_file_accessed": False,
        "receiver_child_process_calls": 0, "receiver_native_cli_calls": 0,
        "receiver_remote_deployment_verified": False,
        "signed_receipt_save_and_reopen_ok": True, "delivery_exchange_stubbed": True,
        "delivery_socket_calls": 0, "delivery_query_credentials_accessed": False,
        "delivery_native_cli_calls": 0, "delivery_child_process_calls": 0,
        "delivery_remote_deployment_verified": False,
        "collection_task_options_ok": True, "private_task_status_ok": True, "task_status_socket_calls": 0}))


if __name__ == "__main__":
    check()
