"""New scope is explicit; historic five-city manifests and journals stay valid."""
from copy import deepcopy
from datetime import datetime,timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from sushiwait.cityscope import (STORES,APPROVED_STORES,ADDITIONAL_CITIES,CITIES,
    SCOPE_ID,EXPANDED_SCOPE_ID,validate_scope,scope_for_stores,interleaved_stores)
from sushiwait.client import QueryResult
from sushiwait.packets import encoded
from test_five_city_scope import FiveCityScopeTests,daily
from test_fleet_isolation import fleet


def expanded_config():
    result=FiveCityScopeTests().expanded()
    result.update(schema_version=3,first_day='2026-10-19',fleet_scope=EXPANDED_SCOPE_ID)
    extra=sorted(set(APPROVED_STORES)-set(STORES),key=int)
    for index in range(0,len(extra),3):
        name='newcity'+chr(97+index//3)
        result['archive_groups'].append({'name':name,'store_ids':extra[index:index+3],
            'archive':'/tmp/'+name+'.sqlite'})
    return result


class FifteenCityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.root.chmod(0o700)
        self.now=datetime.fromisoformat('2026-10-19T11:00:00+08:00')
        self.config=expanded_config();self.plan=daily.make_plan(self.config,self.now)

    def open_fleet(self,at=None):
        value=fleet.Fleet(self.root,self.plan,0,at or self.now)
        self.addCleanup(value.close);return value

    def success(self,f,store,start,seconds=.1):
        received=start+timedelta(seconds=seconds)
        f.reserve(store)
        f.db._insert(store,'live','miniapp_gateway',received.isoformat(),True,
            {'timing':{'request_started_at':start.isoformat()}})
        f.reconcile();return received

    def test_old_scope_is_unchanged_and_new_scope_is_explicit(self):
        self.assertEqual(len(STORES),83);self.assertEqual(len(APPROVED_STORES),119)
        self.assertEqual(len(ADDITIONAL_CITIES),10)
        self.assertEqual({v['city'] for v in APPROVED_STORES.values()},set(CITIES+ADDITIONAL_CITIES))
        self.assertFalse(validate_scope(SCOPE_ID,list(APPROVED_STORES)))
        self.assertTrue(validate_scope(EXPANDED_SCOPE_ID,list(APPROVED_STORES)))
        self.assertFalse(validate_scope(EXPANDED_SCOPE_ID,list(APPROVED_STORES)+['99999']))
        self.assertEqual(scope_for_stores(list(STORES)),SCOPE_ID)
        self.assertEqual(scope_for_stores(list(APPROVED_STORES)),EXPANDED_SCOPE_ID)

    def test_new_day_budget_and_all_original_archives_are_preserved(self):
        self.assertEqual(self.plan['ordinary_query_limit'],136*119)
        self.assertEqual(len(self.config['archive_groups']),40)
        old=FiveCityScopeTests().expanded()['archive_groups']
        self.assertEqual(self.config['archive_groups'][:len(old)],old)
        self.assertEqual(self.plan['renewal_limit'],14)
        self.assertEqual(self.plan['windows'],[{'start':self.now.isoformat(),
            'end':self.now.replace(hour=22).isoformat()}])
        self.assertEqual(self.plan['fleet_scope'],EXPANDED_SCOPE_ID)
        with self.assertRaises(ValueError):
            daily.make_plan(self.config,self.now-timedelta(days=1))
        config_file=self.root/'daily-config.json'
        config_file.write_bytes(encoded(self.config));config_file.chmod(0o600)
        self.assertEqual(daily.read_config(self.root),self.config)
        bad=deepcopy(self.config);bad['fleet_scope']=SCOPE_ID
        config_file.write_bytes(encoded(bad))
        with self.assertRaises(ValueError):daily.read_config(self.root)

    def test_schedule_alternates_all_cities_before_second_store(self):
        ids=self.plan['store_ids']
        self.assertEqual(ids,interleaved_stores(list(reversed(list(APPROVED_STORES)))))
        self.assertEqual([APPROVED_STORES[s]['city'] for s in ids[:15]],list(CITIES+ADDITIONAL_CITIES))
        self.assertEqual(set(ids),set(APPROVED_STORES))
        f=self.open_fleet();due=[fleet.instant(f.value['stores'][s][0]) for s in ids]
        self.assertEqual(due[0],self.now)
        self.assertTrue(all((b-a).total_seconds()==1.4 for a,b in zip(due,due[1:])))
        self.success(f,ids[0],self.now)
        self.assertIsNone(f.due(self.now+timedelta(seconds=1)))
        self.assertEqual(f.due(self.now+timedelta(seconds=1.4)),ids[1])

    def test_restart_preserves_progress_and_global_start_spacing(self):
        f=self.open_fleet();ids=self.plan['store_ids']
        self.success(f,ids[0],self.now,seconds=.1)
        state=deepcopy(f.value);reopened=self.open_fleet(self.now+timedelta(seconds=.2))
        self.assertEqual(reopened.value,state)
        self.assertAlmostEqual(reopened.dispatch_delay(self.now+timedelta(seconds=.2)),1.2)
        self.assertEqual(reopened.dispatch_delay(self.now+timedelta(seconds=2)),0)

    def test_full_round_stays_serial_and_has_no_city_or_store_starvation(self):
        f=self.open_fleet();now=self.now;visited=[];starts=[]
        while len(visited)<119:
            store=f.due(now)
            delay=f.dispatch_delay(now)
            if delay or store is None:
                now+=timedelta(seconds=max(.1,delay));continue
            visited.append(store);starts.append(now)
            now=self.success(f,store,now)
        self.assertEqual(visited,self.plan['store_ids'])
        self.assertTrue(all((b-a).total_seconds()>=1.4 for a,b in zip(starts,starts[1:])))
        self.assertEqual(f.value['issued'],119)
        self.assertEqual(f.value['successful'],119)

    def test_one_failed_store_does_not_stop_other_cities(self):
        f=self.open_fleet();first,second=self.plan['store_ids'][:2]
        f.reserve(first);f.db.save_failure(first,QueryResult(False,None,'timeout',None,
            self.now.isoformat(),self.now.isoformat(),1),api_profile='miniapp_gateway',failure_phase='request')
        f.reconcile()
        self.assertIn(first,f.status()['unavailable_store_ids'])
        self.assertEqual(f.due(self.now+timedelta(seconds=1.4)),second)

    def test_directory_is_one_journaled_get_and_does_not_mutate_scope_or_usage(self):
        f=self.open_fleet();client=Mock()
        data=[{'id':int(s),'name':v['name'],'area':v['city']+'合成区'} for s,v in APPROVED_STORES.items()]
        client.fetch_stores.return_value=QueryResult(True,{'data':data},None,200,
            self.now.isoformat(),self.now.isoformat(),1)
        original=deepcopy(f.plan)
        self.assertTrue(fleet.check_directory(f,client,self.now))
        self.assertFalse(fleet.check_directory(f,client,self.now+timedelta(seconds=1)))
        client.fetch_stores.assert_called_once()
        self.assertEqual(f.plan,original);self.assertEqual(f.value['issued'],0)
        self.assertEqual(json.loads((self.root/'directory-check.json').read_text())['state'],'verified')

    def test_directory_failure_or_unknown_marker_does_not_retry_or_stop_fleet(self):
        f=self.open_fleet();client=Mock();client.fetch_stores.return_value=QueryResult(False,None,
            'http_error',401,self.now.isoformat(),self.now.isoformat(),1)
        self.assertTrue(fleet.check_directory(f,client,self.now))
        self.assertIsNotNone(f.due(self.now));self.assertEqual(f.value['failed'],0)
        self.assertFalse(fleet.check_directory(f,client,self.now))
        (self.root/'directory-check.json').write_bytes(encoded({'schema_version':1,'state':'reserved'}))
        self.assertFalse(fleet.check_directory(f,client,self.now))
        client.fetch_stores.assert_called_once()

    def test_directory_rate_limit_uses_existing_shared_backoff(self):
        f=self.open_fleet();client=Mock();client.fetch_stores.return_value=QueryResult(False,None,
            'http_error',429,self.now.isoformat(),self.now.isoformat(),1,
            transport={'retry_after_seconds':1800})
        fleet.check_directory(f,client,self.now)
        self.assertEqual(fleet.instant(f.value['rate_limit_until']),self.now+timedelta(minutes=30))
        self.assertIsNone(f.due(self.now+timedelta(minutes=1)))
        self.assertEqual(f.value['issued'],0)
        self.assertEqual(self.open_fleet().value['rate_limit_until'],f.value['rate_limit_until'])

    def test_directory_change_is_reported_and_not_silently_activated(self):
        f=self.open_fleet();client=Mock();client.fetch_stores.return_value=QueryResult(True,
            {'data':[{'id':99999,'name':'合成新店','area':'杭州合成区'}]},None,200,
            self.now.isoformat(),self.now.isoformat(),1)
        fleet.check_directory(f,client,self.now)
        value=json.loads((self.root/'directory-check.json').read_text())
        self.assertEqual(value['state'],'changed');self.assertEqual(value['additional'],['99999'])
        self.assertNotIn('99999',f.plan['store_ids']);self.assertEqual(len(value['missing']),119)

    def test_weekend_target_keeps_original_cadence_and_never_bursts(self):
        now=datetime.fromisoformat('2026-10-24T12:00:00+08:00')
        plan=daily.make_plan(self.config,now)
        self.assertEqual(plan['cadence'],'calendar-meal-peaks-v1')
        from sushiwait.cadence import interval_at
        self.assertEqual(interval_at(now,plan['cadence']),120)
        self.assertGreater(len(plan['store_ids'])*1.4,120)
        self.assertEqual(fleet.request_delay(plan['cadence'],.2),1.2)

    def test_before_opening_neither_directory_nor_store_queries_run(self):
        import threading
        from types import SimpleNamespace
        stop=threading.Event();before=self.now-timedelta(seconds=10)
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return before.astimezone(tz)
        controller=Mock();service=SimpleNamespace(campaign=daily.campaign,
            reauthorization=daily.reauthorization,publish_status=Mock(),CHINA=daily.CHINA)
        with patch.object(fleet,'datetime',Clock),patch.object(daily.campaign.legacy,'Controller',return_value=controller),\
             patch.object(daily.campaign.legacy,'prepare_material_source',return_value=object()),\
             patch.object(fleet.cli,'client_for') as client,patch.object(stop,'wait',side_effect=lambda _:stop.set()):
            fleet.run(service,self.root,self.root,self.plan,Mock(),stop)
        controller.ensure_refresh.assert_not_called();client.assert_not_called()
        self.assertFalse((self.root/'directory-check.json').exists())
