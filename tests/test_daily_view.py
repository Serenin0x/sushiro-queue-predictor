"""Full daily counters, bounded graphs and HTTP reads without source access."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sushiwait.dailyview import DailyView
from sushiwait.remote import RemoteStore, QUEUE_NAMES
from sushiwait.remotecampaign import RemoteCampaignService
from sushiwait.remoteservice import RemoteASGI
from sushiwait.monitorhub import MonitorHub, HubError
from test_business_hours import rules
from test_remote_service import BASE, sample, request, wait_for
from test_monitor_hub import config, NOW
import test_remote_window as fixture

class DailyTests(unittest.TestCase):
    def view(self):return DailyView(['900001'],hours=rules(),base_interval=60)
    def record(self,seconds=0,labels=('12','13','14','15')):
        r=sample()
        for q in r['queries'].values():
            if q['started_at'] is not None:q.update(started_at=(BASE+timedelta(seconds=seconds)).isoformat(),received_at=(BASE+timedelta(seconds=seconds)).isoformat())
        r['queries']['groupqueues']['payload']['queues']={n:list(labels) for n in QUEUE_NAMES}
        return r
    def test_duplicates_do_not_inflate_daily_totals_and_all_arrays_remain_in_source(self):
        v=self.view();r=self.record();v.publish(r,key=('db',1),run='run');v.publish(r,key=('db',1),run='run')
        data=v.detail('900001','2026-10-06',now=BASE+timedelta(seconds=60))
        self.assertEqual(data['summary']['observations'],1);self.assertEqual(data['points'][0]['queues']['storeQueue'],['12','13','14'])
        self.assertEqual(r['queries']['groupqueues']['payload']['queues']['storeQueue'],['12','13','14','15'])
        self.assertEqual(data['points'][0]['display_sizes']['storeQueue'],4)
    def test_comparable_sets_gap_and_new_run_are_distinguished(self):
        v=self.view()
        for i,(sec,labels,run) in enumerate([(0,('12','13'),'a'),(60,('13','14'),'a'),(300,('15',),'a'),(360,('16',),'b')]):
            v.publish(self.record(sec,labels),key=('db',i),run=run)
        d=v.detail('900001','2026-10-06',now=BASE+timedelta(seconds=400))
        self.assertEqual([p['comparison_state'] for p in d['points']],['insufficient','comparable_display_sets','gap','run_boundary'])
        self.assertEqual(d['summary']['display_removed_labels']['storeQueue'],1)
        self.assertEqual(d['summary']['long_gap_boundaries'],1);self.assertEqual(d['summary']['run_boundaries'],1)
        self.assertIsNone(d['actual_called_count'])
    def test_midnight_uses_shanghai_date_and_does_not_compare_previous_day(self):
        v=self.view();v.publish(self.record(),key=('db',1),run='a');v.publish(self.record(14400),key=('db',2),run='a')
        self.assertEqual([d['local_date'] for d in v.index('900001',now=BASE+timedelta(days=1))['days']],['2026-10-06','2026-10-07'])
        self.assertEqual(v.detail('900001','2026-10-07',now=BASE+timedelta(days=1))['points'][0]['comparison_state'],'insufficient')
    def test_background_bins_do_not_double_count_fast_polling(self):
        v=self.view();v.publish(self.record(0),key=('db',1),run='a');v.publish(self.record(30),key=('db',2),run='a')
        d=v.index('900001',now=BASE+timedelta(seconds=60))['days'][0]
        self.assertEqual(d['observations'],2);self.assertEqual(d['observed_background_slots'],1)
    def test_graph_truncation_does_not_truncate_summary(self):
        v=self.view()
        with patch('sushiwait.dailyview.MAX_DAY_POINTS',2):
            for i in range(4):v.publish(self.record(60*i),key=('db',i),run='a')
        d=v.detail('900001','2026-10-06',now=BASE+timedelta(seconds=250))
        self.assertEqual(len(d['points']),2);self.assertEqual(d['summary']['observations'],4);self.assertTrue(d['graph_truncated'])
    def test_restoration_same_rows_is_idempotent_and_private_values_not_returned(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder).resolve()/'db.sqlite3'
            with RemoteStore(p) as db:
                db.append(self.record());db.append(self.record(60));v=self.view();v.restore(db);v.restore(db)
                d=v.detail('900001','2026-10-06',now=BASE+timedelta(seconds=120))
                self.assertEqual(d['summary']['successful_pairs'],2);self.assertNotIn(str(p),json.dumps(d))
    def test_empty_date_is_missing_not_zero_called(self):
        d=self.view().detail('900001','2026-10-09',now=BASE)
        self.assertIsNone(d['summary']);self.assertIsNone(d['actual_called_count']);self.assertEqual(d['points'],[])
    def test_actual_campaign_worker_daily_routes_add_no_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            parent=Path(folder).resolve();root=parent/'campaign';root.mkdir(mode=0o700);plan=parent/'plans.json';plan.write_text('{"schema_version":1,"plans":[]}');plan.chmod(0o600)
            clock=fixture.Clock();opener=fixture.Opener(clock)
            from sushiwait.remote import RemoteClient
            service=RemoteCampaignService(root=root,plan_file=plan,store_ids=['900001'],base_interval=60,duration_seconds=120,window_seconds=60,max_pairs=8,business_hours=rules(),wall_clock=clock.wall,monotonic_clock=clock.mono,wait=lambda seconds,stop:clock.sleep(seconds),client_factory=lambda:RemoteClient(opener=opener))
            with patch('sushiwait.remote._utc',side_effect=lambda:clock.wall().isoformat()):
                service.start();wait_for(lambda:not service.thread.is_alive());before=len(opener.calls)
                for path in ['/api/v1/days','/api/v1/stores/900001/days','/api/v1/stores/900001/days/2026-10-06']:
                    status,value,_=asyncio.run(request(RemoteASGI(service),path));self.assertEqual(status,200);self.assertFalse(value['network_performed_by_read'])
                self.assertEqual(len(opener.calls),before);self.assertEqual(service.daily_index('900001')['days'][0]['successful_pairs'],2)
                service.shutdown()
    def test_hub_calendar_keeps_missing_worker_explicit(self):
        cfg=config();v=self.view();v.publish(self.record(),key=('db',1),run='a')
        def reader(worker,path):
            if '900001' in path:return v.index('900001',now=BASE+timedelta(seconds=60))
            raise HubError()
        hub=MonitorHub(cfg,reader=reader,now=NOW)
        status,body,_=hub.dispatch('/api/v1/days',now=NOW);d=json.loads(body)
        self.assertEqual(status,200);self.assertEqual(d['unavailable_store_ids'],['900002','900003'])
        self.assertEqual(d['days']['2026-10-06']['900001']['successful_pairs'],1)
        self.assertEqual(hub.dispatch('/api/v1/stores/900004/days/2026-10-06',now=NOW)[0],503)

if __name__=='__main__':unittest.main()
