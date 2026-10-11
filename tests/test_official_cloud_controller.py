"""Finite trial control boundaries. No real session, package or network."""
from datetime import datetime,timezone
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from tests.test_materialsource import TARGET,PACKAGE

SPEC=importlib.util.spec_from_file_location('official_cloud_controller',
    Path(__file__).resolve().parents[1]/'deploy'/'official_cloud_controller.py')
cloud=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(cloud)


def plan():
    return {'schema_version':1,'app_id':TARGET,'source_version':181,'package_sha256':PACKAGE,
        'store_ids':['3014','3004','2009'],'windows':[
            {'start':f'2026-10-{day}T11:00:00+08:00','end':f'2026-10-{day}T22:00:00+08:00'}
            for day in (12,13,14)],'renewal_limit':42}


class OfficialCloudControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.root.chmod(0o700)
        self.value=plan()
        self.socket=patch('socket.create_connection',side_effect=AssertionError('no network')).start()
        self.addCleanup(patch.stopall)

    def test_finite_three_day_slots_and_deadline(self):
        windows=cloud.validate_plan(self.value)
        self.assertEqual(sum(r*len(self.value['store_ids']) for _,_,r in windows),396)
        self.assertEqual(windows[-1][1],datetime(2026,10,14,14,tzinfo=timezone.utc))

    def test_unapproved_date_offhours_frequency_or_store_cannot_expand_plan(self):
        for change in ({'renewal_limit':43},{'store_ids':['9999']},{'store_ids':['3014','3014']},
                       {'interval':300},{'source_version':182}):
            with self.subTest(change=change),self.assertRaises(cloud.TrialError):
                cloud.validate_plan({**plan(),**change})
        for start in ('2026-10-15T11:00:00+08:00','2026-10-12T10:30:00+08:00','2026-10-12T11:00:00'):
            value=plan();value['windows'][0]['start']=start
            with self.assertRaises(cloud.TrialError):cloud.validate_plan(value)

    def test_attempt_reservation_is_durable_and_crash_does_not_retry(self):
        ledger=cloud.Ledger(self.root,self.value);ledger.reserve()
        self.assertEqual(ledger.value['attempts'],1)
        with self.assertRaisesRegex(cloud.TrialError,'cloud_refresh_outcome_unknown'):
            cloud.Ledger(self.root,self.value)
        self.assertEqual(len(list(self.root.glob('checkpoint-*.json'))),2)
        self.socket.assert_not_called()

    def test_clean_restart_preserves_attempt_revision_and_digest(self):
        ledger=cloud.Ledger(self.root,self.value);ledger.reserve();ledger.accepted(1,'b'*64)
        restored=cloud.Ledger(self.root,self.value)
        self.assertEqual((restored.value['attempts'],restored.value['highest_revision']),(1,1))
        restored.check_context(1,'b'*64)
        for revision,digest in [(0,'b'*64),(1,'c'*64),(2,'b'*64)]:
            with self.assertRaises(cloud.TrialError):restored.check_context(revision,digest)

    def test_same_private_checkpoint_cannot_extend_budget_or_dates(self):
        cloud.Ledger(self.root,self.value)
        for change in ({'renewal_limit':41},{'store_ids':['3014']},{'windows':plan()['windows'][:2]}):
            with self.assertRaises(cloud.TrialError):cloud.Ledger(self.root,{**plan(),**change})

    def test_attempt_limit_survives_reconstruction(self):
        value=plan();value['renewal_limit']=1
        ledger=cloud.Ledger(self.root,value);ledger.reserve();ledger.accepted(1,'a'*64)
        restored=cloud.Ledger(self.root,value)
        with self.assertRaisesRegex(cloud.TrialError,'cloud_refresh_limit'):restored.reserve()

    def test_halt_and_finished_windows_survive_restart(self):
        ledger=cloud.Ledger(self.root,self.value);ledger.finish(0)
        ledger=cloud.Ledger(self.root,self.value);self.assertEqual(ledger.value['finished_windows'],[0])
        ledger.halt('cloud_reader_failed')
        with self.assertRaisesRegex(cloud.TrialError,'cloud_trial_halted'):cloud.Ledger(self.root,self.value)

    def test_checkpoint_holes_do_not_reset_attempts(self):
        ledger=cloud.Ledger(self.root,self.value);ledger.reserve();ledger.accepted(1,'a'*64)
        (self.root/'checkpoint-001.json').unlink()
        with self.assertRaisesRegex(cloud.TrialError,'cloud_ledger_invalid'):cloud.Ledger(self.root,self.value)

    def test_offhours_performs_no_source_context_or_origin_access(self):
        source=Mock();ledger=Mock()
        controller=cloud.Controller(self.root,self.value,source,ledger)
        controller.context=Mock(side_effect=AssertionError('no credentials read'))
        controller.perform=Mock(side_effect=AssertionError('no supply or query'))
        start,end,_=cloud.validate_plan(self.value)[0]
        for at in (start.replace(hour=2),end,end.replace(hour=23)):
            self.assertFalse(controller.ensure_refresh(start,end,now=at))
        self.assertEqual(ledger.mock_calls,[])
        source.assert_not_called();controller.perform.assert_not_called();self.socket.assert_not_called()

    def test_bootstrap_close_to_closing_does_not_spend_attempt(self):
        from datetime import timedelta
        source=Mock();ledger=Mock();ledger.value={'highest_revision':0}
        controller=cloud.Controller(self.root,self.value,source,ledger)
        start,end,_=cloud.validate_plan(self.value)[0]
        self.assertFalse(controller.ensure_refresh(start,end,now=end-timedelta(seconds=99)))
        ledger.reserve.assert_not_called()

    def test_restart_keeps_same_task_target_with_explicit_resume_and_installed_code(self):
        task=self.root/'collection-2026-10-12.json'
        start,end,_=cloud.validate_plan(self.value)[0]
        args=cloud.collect_arguments(self.root,self.value['store_ids'],44,task,True,start,end)
        self.assertIn('-I',args);self.assertIn('--resume-task',args)
        self.assertEqual(args[args.index('--samples')+1],'44')
        self.assertEqual(args[args.index('--interval')+1],'900')
        self.assertEqual(args[args.index('--task-file')+1],str(task))

    def test_each_child_fetch_has_its_own_opening_and_closing_gate(self):
        from datetime import timedelta
        spec=importlib.util.spec_from_file_location('business_collect',
            Path(__file__).resolve().parents[1]/'deploy'/'official_business_collect.py')
        wrapper=importlib.util.module_from_spec(spec);spec.loader.exec_module(wrapper)
        start,end,_=cloud.validate_plan(self.value)[0]
        original=Mock(return_value='synthetic-result');client=object()
        fetch=wrapper.guard_fetch(original,start,end,clock=Mock(return_value=start-timedelta(seconds=1)))
        with self.assertRaises(SystemExit) as before:fetch(client,'3014')
        self.assertEqual(before.exception.code,2);original.assert_not_called()
        for at in (end-timedelta(seconds=16),end,end+timedelta(days=1)):
            fetch=wrapper.guard_fetch(original,start,end,clock=Mock(return_value=at))
            with self.assertRaises(SystemExit) as after:fetch(client,'3014')
            self.assertEqual(after.exception.code,0);original.assert_not_called()
        fetch=wrapper.guard_fetch(original,start,end,clock=Mock(return_value=start))
        self.assertEqual(fetch(client,'3014'),'synthetic-result')
        original.assert_called_once_with(client,'3014')

    def test_bootstrap_and_renewal_use_their_distinct_supported_settings_schemas(self):
        source=Mock();source.app_client='synthetic-client';source.query_content_type='application/x-www-form-urlencoded'
        controller=cloud.Controller(self.root,self.value,source,Mock())
        start,end,_=cloud.validate_plan(self.value)[0]
        with patch.object(cloud,'prepare_bootstrap_verifier'),patch.object(cloud,'initialize_for_target_from_yyb') as init:
            controller.perform(start,end,None)
            self.assertEqual(init.call_args.kwargs['settings_file'].name,'bootstrap-settings.json')
        previous=Mock();previous.revision=1
        with patch.object(cloud,'prepare_verifier'),patch.object(cloud,'initialize_from_yyb') as init:
            controller.perform(start,end,previous)
            self.assertEqual(init.call_args.kwargs['settings_file'].name,'renewal-settings.json')

    def test_invalid_json_duplicates_and_nonfinite_stop_without_network(self):
        for body in [b'{"attempts":1,"attempts":0}',b'{"attempts":NaN}']:
            with self.assertRaises(cloud.TrialError):cloud._decode(body)
        self.socket.assert_not_called()

    def test_actual_pipeline_bootstraps_then_renews_without_old_import_or_new_reader(self):
        import contextlib,io,json
        from datetime import timedelta
        from tests.test_credentials import write_bundle
        from tests.test_initialization import Response,CODE
        from tests.test_materialsource import bundle
        from tests.test_sessionverification import token,success
        from sushiwait.credentials import CredentialSource
        write_bundle(self.root/'initialization-material.json',bundle())
        for filename,schema in [('bootstrap-settings.json',2),('renewal-settings.json',1)]:
            settings={'schema_version':schema,'ref':'1','protocol_token':'a'*64}
            if schema==2:settings['app_id']=TARGET
            write_bundle(self.root/filename,settings)
        state=self.root/'state';state.mkdir(mode=0o700)
        ledger=cloud.Ledger(state,self.value)
        with patch('sushiwait.materialsource._cipher_backend',return_value=Mock(return_value='synthetic-date-code')):
            source=cloud.prepare_material_source(bundle_file=self.root/'initialization-material.json',
                expected_app_id=TARGET,expected_source_version=181,expected_package_sha256=PACKAGE)
        controller=cloud.Controller(self.root,self.value,source,ledger)
        reader=CredentialSource('miniapp_gateway',credentials_file=controller.credentials)
        now=datetime.now(timezone.utc);start=now-timedelta(hours=1);end=now+timedelta(hours=1)
        client=Mock();client.fetch_store.return_value=success()
        for index,seconds in [(1,80),(2,3600)]:
            supplier,initialize=Mock(),Mock()
            supplier.open.return_value=Response(body=json.dumps({'code':0,'data':{'result':{
                'code':CODE+str(index),'errMsg':'login:ok'}}}).encode(),url='http://127.0.0.1:18081/wxapp/getCode')
            # Mint at this test step, independent of full-suite import time.
            with patch('tests.test_sessionverification.NOW',datetime.now(timezone.utc)):
                fresh_token=token(str(index),seconds)
            initialize.open.return_value=Response(body=json.dumps({'data':{'auth_token':fresh_token}}).encode())
            with patch('urllib.request.build_opener',side_effect=[supplier,initialize]),\
                    patch('sushiwait.sessionverification.SushiroClient',return_value=client),\
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(controller.ensure_refresh(start,end))
            self.assertEqual(controller.context()[0].revision,index)
            self.assertEqual(reader.current().revision,index)
            self.assertEqual(controller.context()[0].content_type,'application/x-www-form-urlencoded')
            self.assertEqual(initialize.open.call_args.args[0].get_header('Content-type'),'application/json')
        self.assertEqual(ledger.value['attempts'],2)
        self.assertEqual(client.fetch_store.call_count,2)
        restored=cloud.Ledger(state,self.value)
        self.assertEqual(restored.value['highest_revision'],2)
        self.socket.assert_not_called()
