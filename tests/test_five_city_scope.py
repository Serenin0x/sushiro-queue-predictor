from copy import deepcopy
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from sushiwait.cityscope import STORES,SCOPE_ID,validate_scope
from sushiwait.storage import SnapshotStore
from sushiwait.tasks import CollectionTask
from sushiwait.packets import encoded
from test_official_daily_service import daily,config
from test_statistics_gateway import gateway,JoinedViewTests

class FiveCityScopeTests(unittest.TestCase):
    def expanded(self):
        old=config();old['schema_version']=2;old['first_day']='2026-10-16'
        remaining=sorted(set(STORES)-set(daily.STORES),key=int)
        for i in range(0,len(remaining),3):
            name='batch'+chr(97+i//3)
            old['archive_groups'].append({'name':name,'store_ids':remaining[i:i+3],'archive':'/tmp/'+name+'.sqlite'})
        return old
    def test_all_83_and_exact_five_cities(self):
        self.assertEqual(len(STORES),83)
        self.assertEqual({r['city'] for r in STORES.values()},{'北京','成都','上海','广州','深圳'})
        self.assertTrue(validate_scope(SCOPE_ID,list(STORES)))
        self.assertFalse(validate_scope(SCOPE_ID,list(STORES)+['99999']))
    def test_whole_day_budget_and_small_public_groups(self):
        c=self.expanded();p=daily.make_plan(c,datetime.fromisoformat('2026-10-16T10:40:00+08:00'))
        self.assertEqual(p['schema_version'],4);self.assertEqual(p['ordinary_query_limit'],11288)
        self.assertEqual(len(p['store_ids']),83);self.assertEqual(len(c['archive_groups']),28)
        self.assertEqual(p['windows'][0]['start'],'2026-10-16T11:00:00+08:00')
        with tempfile.TemporaryDirectory() as root:
            root=Path(root).resolve();root.chmod(0o700);(root/'daily-config.json').write_bytes(encoded(c));(root/'daily-config.json').chmod(0o600)
            self.assertEqual(daily.read_config(root),c)
            c['archive_groups'][-1]['store_ids'].append('99999');(root/'daily-config.json').write_bytes(encoded(c))
            with self.assertRaises(ValueError):daily.read_config(root)
    def test_old_plan_not_expanded_implicitly(self):
        p=daily.make_plan(config(),datetime.fromisoformat('2026-10-16T11:00:00+08:00'))
        p['store_ids']+=['1']
        with self.assertRaises(daily.campaign.TrialError):daily.campaign.validate_plan(p)
    def test_child_has_explicit_scope_and_persistent_resume(self):
        c=self.expanded();p=daily.make_plan(c,datetime.fromisoformat('2026-10-16T11:00:00+08:00'))
        start,end,rounds=daily.campaign.validate_plan(p)[0]
        with tempfile.TemporaryDirectory() as root:
            root=Path(root).resolve();root.chmod(0o700)
            args=daily.campaign.legacy.collect_arguments(root,p['store_ids'],rounds,root/'task.json',False,start,end,interval=600,cadence=p['cadence'])
            self.assertEqual(args[args.index('--fleet-scope')+1],SCOPE_ID)
            cfg={'db':str(root/'data.sqlite'),'api_profile':'miniapp_gateway','store_ids':p['store_ids'],'interval':600,'samples':rounds,'wait_for_credentials':600,'cadence':p['cadence'],'fleet_scope':SCOPE_ID}
            with CollectionTask(root/'task.json',config=cfg,resume=False,now=start) as task:
                task.prepare_database()
                with SnapshotStore(cfg['db']) as db:task.bind(db,now=start)
            with CollectionTask(root/'task.json',config=cfg,resume=True,now=start):pass
            bad=deepcopy(cfg);bad['store_ids'][-1]='99999'
            with self.assertRaises(ValueError):
                with CollectionTask(root/'bad.json',config=bad,resume=False,now=start):pass
    def test_28_views_merge_without_losing_first_stores(self):
        fixture=JoinedViewTests();ids=list(STORES);joined=fixture.view(ids[0])
        for store in ids[1:]:joined=gateway.JoinedOfficialViews(joined,fixture.view(store))
        status,raw,_=joined.dispatch('/api/v1/official/months/2026-10')
        self.assertEqual(status,200);self.assertEqual(set(json.loads(raw)['store_names']),set(STORES))

    def test_actual_cli_completes_one_synthetic_round_for_all_83(self):
        from test_collection_tasks import CollectionTaskTests
        fixture=CollectionTaskTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        args=fixture.args(ids=list(STORES),samples=1,interval=600)+['--cadence','meal-peaks-v1','--fleet-scope',SCOPE_ID]
        result,_=fixture.run_cli(args)
        self.assertEqual(result,0)
        self.assertEqual({r[0] for r in fixture.requests},set(STORES))
        self.assertEqual(len(fixture.requests),83)

    def test_expanded_incident_uses_new_day_for_email_recovery_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();root.chmod(0o700)
            settings=root/'daily-config.json';settings.write_bytes(encoded(self.expanded()));settings.chmod(0o600)
            diag=root/'five-cities-20261016'/'refresh-diagnostics';diag.mkdir(parents=True,mode=0o700);diag.parent.chmod(0o700)
            p=diag/'attempt-001.json';p.write_bytes(encoded({'phase':'supplier','error_code':'yyb_reauthorization_required'}));p.chmod(0o600)
            block={'value':{'day':'2026-10-16','error_code':'cloud_refresh_failed'}}
            self.assertTrue(daily.reauthorization.requires_scan(root,block))
            block['value']['day']='2026-10-17';self.assertFalse(daily.reauthorization.requires_scan(root,block))

    def test_fleet_queries_are_spaced_and_still_obey_business_gate(self):
        import sys
        from unittest.mock import Mock
        spec=importlib.util.spec_from_file_location('fleet_business_collect',Path(__file__).resolve().parents[1]/'deploy/official_business_collect.py')
        wrapper=importlib.util.module_from_spec(spec);spec.loader.exec_module(wrapper)
        start=datetime.fromisoformat('2026-10-16T11:00:00+08:00');end=start.replace(hour=22)
        original=Mock();fetch=wrapper.guard_fetch(original,start,end,clock=Mock(return_value=start))
        with patch.object(sys,'argv',['collect','--fleet-scope',SCOPE_ID]),patch.object(wrapper.time,'monotonic',side_effect=[0.0,0.1,0.5]),patch.object(wrapper.time,'sleep') as sleep:
            fetch(object(),'3014');fetch(object(),'3004')
        sleep.assert_called_once_with(0.4);self.assertEqual(original.call_count,2)
