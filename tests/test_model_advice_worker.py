"""No paid requests in worker tests: daily budgets and presence scheduling."""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json, tempfile, unittest
from test_display_replay import archive
from sushiwait.ticketresearch import TicketResearch
from sushiwait.deepseek import _Ledger, DeepSeekError

spec=importlib.util.spec_from_file_location('model_worker',Path(__file__).parents[1]/'deploy/model_advice_worker.py')
worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)

class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.root.chmod(0o700)
        self.research=self.root/'tickets.sqlite';self.output=self.root/'advice.json'
        self.now=datetime(2026,10,13,4,tzinfo=timezone.utc)
        self.cache={};self.history={};self.requests=[];self.calls=[]
        r=TicketResearch(self.research);r.close()
    def reader(self,path):
        self.requests.append(path)
        if path=='/api/v1/collection/health': return {'state':'running','heartbeat_stale':False}
        if path.endswith('2026-10-13'):
            d=archive(tuple(range(100,180,10)),step=5);d['requested_store_id']='3014'
            return d
        raise ValueError('no historical record')
    def suggest(self,*args,**kwargs):
        self.calls.append((args,kwargs));return {'state':'fallback','advice':None}
    def tick(self,at=None):
        worker.tick(self.root,self.output,self.research,self.cache,self.history,
            now=at or self.now,reader=self.reader,suggest=self.suggest)
    def test_no_viewer_no_provider_call_and_no_source_query(self):
        self.tick();self.assertEqual(self.calls,[])
        self.assertEqual(self.requests,['/api/v1/collection/health'])
        from sushiwait.cityscope import APPROVED_STORES
        self.assertEqual(set(json.loads(self.output.read_text())['supported_store_ids']),set(APPROVED_STORES))

    def test_expanded_store_thinking_is_published_as_computing_then_ready(self):
        self.now=self.now.replace(hour=3,minute=36)
        research=TicketResearch(self.research)
        research.presence({'client_id':'b'*32,'store_id':'3006','queue_type':'ordinary',
                           'mode':'thinking','visible':True},now=self.now)
        research.close()
        def reader(path):
            if path=='/api/v1/collection/health':return {'state':'running','heartbeat_stale':False}
            if path.endswith('2026-10-13'):
                detail=self.reader('/api/v1/official/stores/3014/days/2026-10-13')
                detail['requested_store_id']='3006';return detail
            raise ValueError('no historical record')
        def suggest(context,**kwargs):
            projected=json.loads(self.output.read_text())
            self.assertEqual(projected['stores']['3006']['ordinary']['thinking']['state'],'computing')
            self.assertEqual(context['store_id'],'3006')
            self.assertEqual(kwargs['budget_file'].parent.name,'thinking-v2')
            self.assertNotIn('issued_at',json.dumps(context))
            return {'state':'ready','advice':None}
        worker.tick(self.root,self.output,self.research,self.cache,self.history,
                    now=self.now,reader=reader,suggest=suggest)
        self.assertEqual(json.loads(self.output.read_text())['stores']['3006']['ordinary']['thinking']['state'],'ready')
    def test_day_budget_shared_total_and_never_reset(self):
        paths=[worker.prepare_budget(self.root,m,self.now) for m in ('thinking','nonthinking')]
        self.assertEqual(sum(json.loads(p.read_text())['max_reserved_cost_microunits'] for p in paths),10000000)
        (paths[0].parent/'preserved-receipt').write_text('preserve')
        self.assertEqual(worker.prepare_budget(self.root,'thinking',self.now+timedelta(minutes=1)),paths[0])
        self.assertTrue((paths[0].parent/'preserved-receipt').exists())
        nextpath=worker.prepare_budget(self.root,'thinking',self.now+timedelta(days=1))
        self.assertNotEqual(nextpath,paths[0]);self.assertTrue(paths[0].exists())
        data=json.loads(paths[0].read_text());data['max_reserved_cost_microunits']+=1;paths[0].write_text(json.dumps(data))
        with self.assertRaises(ValueError):worker.prepare_budget(self.root,'thinking',self.now)
    def test_closed_hours_skip_provider_and_expected_weekend_open(self):
        self.tick(self.now.replace(hour=0));self.assertEqual(self.calls,[])
        self.assertEqual(json.loads(self.output.read_text())['status'],'waiting_for_open')
        weekend=self.now.replace(day=17);a,b=worker.business_window(weekend)
        self.assertEqual((a.hour,a.minute,b.hour),(10,30,22))
    def test_fresh_visible_lease_calls_requested_mode_then_hiding_stops(self):
        r=TicketResearch(self.research)
        lease={'client_id':'a'*32,'store_id':'3014','queue_type':'ordinary','mode':'thinking','visible':True}
        # Fixture latest sample is 11:35; read at 11:36 to avoid stale features.
        self.now=self.now.replace(hour=3,minute=36)
        r.presence(lease,now=self.now);r.close();self.tick()
        self.assertEqual(len(self.calls),1);self.assertEqual(self.calls[0][1]['budget_file'].parent.name,'thinking-v2')
        r=TicketResearch(self.research);r.presence({**lease,'visible':False},now=self.now);r.close()
        self.tick();self.assertEqual(len(self.calls),1)
        self.assertTrue(all(p.startswith('/api/v1/') for p in self.requests))

    def test_retired_ledger_charge_is_kept_and_same_observation_not_repeated(self):
        self.now=self.now.replace(hour=3,minute=36)
        path=worker.prepare_budget(self.root,'thinking',self.now)
        value=json.loads(path.read_text())
        legacy=path.parent.parent/'thinking';legacy.mkdir(mode=0o700)
        old=legacy/'budget.json';value.update(max_output_tokens=4096,timeout_seconds=30)
        old.write_text(json.dumps(value));old.chmod(0o600)
        context=worker.public_context(self.reader('/api/v1/official/stores/3014/days/2026-10-13'),now=self.now)
        import hashlib
        from sushiwait.intake import _canonical
        identity={k:context[k] for k in ('policy','source','store_id','queue_type','local_date','observation_id','sample_at','phase')}
        ledger=_Ledger(old,self.now)
        ledger.reserve({'public_input_sha256':'a'*64},hashlib.sha256(_canonical(identity).encode()).hexdigest(),self.now)
        reserved=ledger.calls[0][0]['reserved_cost_microunits'];ledger.close()
        # Test-only removal of the unused v2 fixture, not an existing real ledger.
        path.unlink()
        new=worker.prepare_budget(self.root,'thinking',self.now)
        self.assertEqual(json.loads(new.read_text())['max_reserved_cost_microunits']+reserved,8000000)
        self.assertTrue(worker.retired_observation(self.root,'thinking',context,self.now))
        self.assertTrue(old.exists())
