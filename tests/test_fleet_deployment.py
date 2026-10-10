from argparse import Namespace
from datetime import timedelta
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'deploy'))
try:
    import prepare_fleet_native as preparation
    import cutover_fleet as handover
finally:sys.path.pop(0)
from test_daily_controller import BASE
from test_daily_fleet import catalog


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.folder=Path(self.tmp.name).resolve()
        self.release=self.folder/'release';(self.release/'.venv/bin').mkdir(parents=True)
        for name in ('sushiwait','python'):(self.release/'.venv/bin'/name).touch()
        (self.release/'config').mkdir()
        (self.release/'config/mainland-store-catalog.json').write_text(json.dumps(catalog(147)))
        (self.release/'config/default-business-hours.json').write_bytes((ROOT/'config/default-business-hours.json').read_bytes())
        self.args=Namespace(release_dir=str(self.release),root=str(self.folder/'fleet'),
            not_before=(BASE+timedelta(days=1)).isoformat(),prefix='sushiwait-mainland',user='ubuntu',
            port=18821,hub_port=18820,public_port=18080,requests_per_second=5,
            legacy_exports_root=None,legacy_through_date=None,legacy_store_id=[])
        self.mask=os.umask(0o077)
    def tearDown(self):os.umask(self.mask);self.tmp.cleanup()

    def test_147_stores_share_three_units_with_one_rate_limit(self):
        result=preparation.prepare(self.args,now=BASE)
        root=Path(self.args.root)
        self.assertEqual(result['prepared_stores'],147);self.assertEqual(result['prepared_services'],3)
        self.assertEqual(result['official_requests'],0);self.assertFalse(result['services_started'])
        self.assertEqual(len(list(root.glob('store-*'))),147)
        unit=(root/'sushiwait-mainland-collector.service').read_text()
        self.assertIn('MemoryMax=1792M',unit);self.assertIn('TasksMax=512',unit)
        self.assertIn('LimitNOFILE=8192',unit)
        self.assertIn('remote-daily-fleet-serve',unit);self.assertIn('--requests-per-second 5',unit)
        self.assertEqual(json.loads((root/'hub.json').read_text())['schema_version'],3)
        self.assertNotIn('ssh',unit.lower());self.assertNotIn('sudo',unit)

    def test_existing_root_and_colliding_ports_are_not_changed(self):
        self.args.hub_port=self.args.port
        with self.assertRaises(ValueError):preparation.prepare(self.args,now=BASE)
        self.assertFalse(Path(self.args.root).exists())
        self.args.hub_port=18820;preparation.prepare(self.args,now=BASE)
        before=(Path(self.args.root)/'units.json').read_bytes()
        with self.assertRaises(FileExistsError):preparation.prepare(self.args,now=BASE)
        self.assertEqual((Path(self.args.root)/'units.json').read_bytes(),before)

    def test_timer_prepared_without_starting_or_changing_old_units(self):
        old=self.folder/'old';old.mkdir(mode=0o700)
        data={'store_ids':['900001'],'user':'ubuntu','units':['sushiwait-old-store900001.service']}
        (old/'units.json').write_text(json.dumps(data));before=(old/'units.json').read_bytes()
        self.args.old_root=str(old);self.args.cutover_at='2026-10-09T14:10:00Z'
        self.args.not_before='2026-10-10T02:30:00Z'
        self.args.old_auxiliary_unit=['sushiwait-old-statistics.service']
        self.args.legacy_store_id=['900001'];self.args.legacy_exports_root=str(old/'exports')
        self.args.legacy_through_date='2026-10-09'
        result=preparation.prepare(self.args,now=BASE)
        self.assertTrue(result['cutover_timer_prepared']);self.assertFalse(result['cutover_timer_started'])
        root=Path(self.args.root);meta=json.loads((root/'cutover.json').read_text())
        self.assertEqual(handover.validate(meta),meta)
        self.assertIn('OnCalendar=2026-10-09 14:10:00 UTC',(root/'sushiwait-mainland-cutover.timer').read_text())
        self.assertIn('User=root',(root/'sushiwait-mainland-cutover.service').read_text())
        self.assertEqual((old/'units.json').read_bytes(),before)

    def test_invalid_cutover_does_not_create_new_root(self):
        self.args.old_root=str(self.folder/'old')
        with self.assertRaises(ValueError):preparation.prepare(self.args,now=BASE)
        self.assertFalse(Path(self.args.root).exists())

    def meta(self):
        root=Path(self.args.root);root.mkdir(mode=0o700,exist_ok=True)
        return {'schema_version':1,'release':str(self.release),'root':str(root),'user':'ubuntu',
            'not_before':'2026-10-10T02:30:00Z','cutover_at':'2026-10-09T14:10:00Z',
            'old_units':['sushiwait-old-store900001.service','sushiwait-old-statistics.service'],
            'old_collector_units':['sushiwait-old-store900001.service'],
            'new_units':['sushiwait-mainland-collector.service','sushiwait-mainland-hub.service',
                         'sushiwait-mainland-statistics.service'],
            'port':18821,'public_port':18080,'legacy_exports_root':str(self.folder/'old-exports'),
            'legacy_through_date':'2026-10-09','legacy_store_ids':['900001']}

    def reader(self,port,path):
        ids=[r['store_id'] for r in catalog(147)['stores']]
        if port==18821:return {'service_state':'running','worker_alive':True,'store_ids':ids,
            'alive_store_workers':147,'failed_store_ids':[],'origin_gate':{'origin_halted':False}}
        return {'configured_store_ids':ids,'unavailable_store_ids':[],'days':{'2026-10-09':{'900001':{}}}}

    def runner(self,cmd,**kwargs):
        self.commands.append(cmd)
        if cmd[1]=='is-enabled':
            return SimpleNamespace(stdout='enabled\n' if '-old-' in cmd[2] else 'disabled\n',returncode=0)
        return SimpleNamespace(stdout='inactive\n',returncode=3)

    def test_closed_handover_verifies_before_stopping_and_enables_only_after_health(self):
        self.commands=[];meta=self.meta()
        def archives(meta,runner):self.commands.append(['verified-archives'])
        proof=handover.run(meta,now=BASE+timedelta(hours=11,minutes=10),runner=self.runner,
            archive_check=archives,reader=self.reader,sleep=lambda _:None)
        self.assertEqual(self.commands[0],['verified-archives'])
        self.assertIn(['systemctl','stop',*meta['old_units']],self.commands)
        self.assertEqual(self.commands[-2],['systemctl','enable',*meta['new_units']])
        self.assertEqual(self.commands[-1],['systemctl','disable',*meta['old_units']])
        self.assertEqual(proof['configured_stores'],147)
        self.assertEqual(proof['official_requests_added_by_handover'],0)
        self.assertTrue((Path(meta['root'])/'cutover-proof.json').is_file())
        commands=len(self.commands)
        with self.assertRaises(ValueError):
            handover.run(meta,now=BASE+timedelta(hours=11,minutes=10),runner=self.runner,
                archive_check=archives,reader=self.reader,sleep=lambda _:None)
        self.assertEqual(len(self.commands),commands)

    def test_no_handover_during_open_hours_before_due_or_after_activation(self):
        self.commands=[];meta=self.meta()
        for now in [BASE,BASE+timedelta(days=1)]:
            with self.assertRaises(ValueError):handover.run(meta,now=now,runner=self.runner,
                archive_check=lambda *_:None,reader=self.reader,sleep=lambda _:None)
        meta['cutover_at']='2026-10-09T04:00:00Z'
        with self.assertRaises(ValueError):handover.run(meta,now=BASE+timedelta(hours=2),runner=self.runner,
            archive_check=lambda *_:None,reader=self.reader,sleep=lambda _:None)
        self.assertEqual(self.commands,[])

    def test_failed_archive_never_stops_old_writers(self):
        self.commands=[]
        with self.assertRaises(ValueError):handover.run(self.meta(),now=BASE+timedelta(hours=11,minutes=10),
            runner=self.runner,archive_check=lambda *_:(_ for _ in ()).throw(ValueError('bad archive')))
        self.assertEqual(self.commands,[])

    def test_failed_new_health_rolls_back_without_editing_original_task_or_enabling(self):
        self.commands=[];meta=self.meta()
        with self.assertRaises(ValueError):handover.run(meta,now=BASE+timedelta(hours=11,minutes=10),runner=self.runner,
            archive_check=lambda *_:None,reader=lambda *_:{},sleep=lambda _:None)
        mutations=[x for x in self.commands if x[1] not in ('is-active','is-enabled')]
        self.assertEqual(mutations[-2:], [['systemctl','stop',*meta['new_units']],
            ['systemctl','start',*meta['old_units']]])
        self.assertFalse(any('enable' in x for x in self.commands))
        self.assertFalse((Path(meta['root'])/'cutover-proof.json').exists())

    def test_unknown_unit_namespace_or_store_scope_rejected(self):
        meta=self.meta();meta['old_units']=['sshd.service']
        with self.assertRaises(ValueError):handover.validate(meta)

    def test_partial_enable_failure_restores_original_boot_states_and_old_services(self):
        self.commands=[];meta=self.meta()
        def runner(command,**kwargs):
            result=self.runner(command,**kwargs)
            if command==['systemctl','disable',*meta['old_units']]:
                raise RuntimeError('simulated enable-state failure')
            return result
        with self.assertRaises(RuntimeError):
            handover.run(meta,now=BASE+timedelta(hours=11,minutes=10),runner=runner,
                archive_check=lambda *_:None,reader=self.reader,sleep=lambda _:None)
        mutations=[x for x in self.commands if x[1] not in ('is-active','is-enabled')]
        self.assertEqual(mutations[-4:],[['systemctl','stop',*meta['new_units']],
            ['systemctl','enable',*meta['old_units']],['systemctl','disable',*meta['new_units']],
            ['systemctl','start',*meta['old_units']]])
        self.assertFalse((Path(meta['root'])/'cutover-proof.json').exists())

    def test_unknown_boot_state_never_stops_old_services(self):
        self.commands=[]
        def runner(command,**kwargs):
            self.commands.append(command);return SimpleNamespace(stdout='masked\n')
        with self.assertRaises(ValueError):
            handover.run(self.meta(),now=BASE+timedelta(hours=11,minutes=10),runner=runner,
                archive_check=lambda *_:None,reader=self.reader,sleep=lambda _:None)
        self.assertFalse(any(x[1]=='stop' for x in self.commands))

    def test_unconfirmed_new_stop_does_not_restart_duplicate_old_writers(self):
        self.commands=[];meta=self.meta()
        def runner(command,**kwargs):
            result=self.runner(command,**kwargs)
            if command[1]=='is-active' and command[2] in meta['new_units']:
                result.stdout='active\n'
            return result
        with self.assertRaisesRegex(ValueError,'rollback_stop_unconfirmed'):
            handover.run(meta,now=BASE+timedelta(hours=11,minutes=10),runner=runner,
                archive_check=lambda *_:None,reader=lambda *_:{},sleep=lambda _:None)
        self.assertFalse(any(x[1]=='start' and x[2:] == meta['old_units'] for x in self.commands))


if __name__=='__main__':unittest.main()
