"""Recover only unpublished startup state, retaining all original evidence."""
from datetime import timedelta
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from sushiwait.dailycontroller import DailyController, daily_config
from sushiwait.remotecampaign import RemoteCampaign
from sushiwait.remotewindow import RemoteWindowTask
from sushiwait.remote import RemoteStore
from test_daily_controller import BASE
from test_business_hours import rules

spec=importlib.util.spec_from_file_location('startup_repair',Path(__file__).resolve().parents[1]/'deploy/repair_daily_startup.py')
recovery=importlib.util.module_from_spec(spec);spec.loader.exec_module(recovery)


class StartupRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.parent=Path(self.tmp.name).resolve()
        self.root=self.parent/'fleet';self.root.mkdir(mode=0o700)
        (self.root/'fleet.lock').touch(mode=0o600)
        self.incident=self.parent/'incident'
    def tearDown(self):self.tmp.cleanup()

    def startup(self,shape='bound',store='900001'):
        folder=self.root/('store-'+store)
        cfg=daily_config(folder,store,rules(),not_before=BASE.isoformat())
        with DailyController(config=cfg,now=BASE) as c:
            c._prepare(BASE);cc=c._day_config()
            with RemoteCampaign(config=cc,now=BASE) as campaign:
                if shape!='none':
                    window=Path(cc['root'])/'window-01';window.mkdir(mode=0o700)
                    (window/'task.json.lock').touch(mode=0o600)
                    if shape=='empty_db':(window/'remote.sqlite3').touch(mode=0o600)
                    if shape in ('schema_db','bound','pending','observed'):
                        wc=campaign._config_for({'name':'window-01','duration_seconds':3600,'max_pairs':20},BASE)
                        with RemoteWindowTask(window/'task.json',config=wc,resume=False,now=BASE) as task:
                            task.prepare_database()
                            with RemoteStore(wc['db']) as db:
                                if shape!='schema_db':task.bind(db,now=BASE)
                                if shape=='pending':task.begin(store,now=BASE)
                                if shape=='observed':
                                    db.db.execute("INSERT INTO remote_samples VALUES(1,'synthetic',?,1,'{}')",(store,));db.db.commit()
            c._halt(BASE,'daily_controller_storage_or_input_error')
        return folder

    def invoke(self,**kwargs):
        return recovery.repair(self.root,['900001'],self.incident,now=BASE+timedelta(seconds=60),**kwargs)

    def test_all_zero_attempt_creation_stages_preserve_originals_and_frozen_policy(self):
        for shape in ('none','lock','empty_db','schema_db','bound'):
            with self.subTest(shape=shape):
                self.tearDown();self.setUp();folder=self.startup(shape)
                before=(folder/'controller.json').read_bytes()
                original=json.loads(before)
                report=self.invoke(apply=True)
                after=json.loads((folder/'controller.json').read_bytes())
                self.assertEqual(after['current'],original['current']);self.assertEqual(after['config'],original['config'])
                self.assertEqual(after['state'],'active');self.assertIsNone(after['error_code'])
                self.assertEqual((self.incident/'store-900001/controller.before.json').read_bytes(),before)
                self.assertTrue(report['applied']);self.assertEqual(report['official_requests_added'],0)
                self.assertEqual({p.name for p in (folder/'2026-10-09/campaign').iterdir()}, {'campaign.json','campaign.json.lock'})

    def test_pending_or_observed_state_never_changes_checkpoint(self):
        for shape in ('pending','observed'):
            with self.subTest(shape=shape):
                self.tearDown();self.setUp();folder=self.startup(shape)
                before=(folder/'controller.json').read_bytes()
                with self.assertRaises(ValueError):self.invoke(apply=True)
                self.assertEqual((folder/'controller.json').read_bytes(),before);self.assertFalse(self.incident.exists())

    def test_running_fleet_lock_blocks_recovery(self):
        self.startup()
        with (self.root/'fleet.lock').open('r+b') as stream:
            recovery.fcntl.flock(stream.fileno(),recovery.fcntl.LOCK_EX|recovery.fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):self.invoke(apply=True)
        self.assertFalse(self.incident.exists())

    def test_foreign_files_and_scope_are_not_adopted(self):
        folder=self.startup()
        foreign=folder/'2026-10-09/campaign/window-01/unknown'
        foreign.touch(mode=0o600)
        with self.assertRaises(ValueError):self.invoke(apply=True)
        self.assertTrue(foreign.exists());self.assertFalse(self.incident.exists())

    def test_dry_run_and_second_application_do_not_reset_or_create_incident(self):
        folder=self.startup();before=(folder/'controller.json').read_bytes()
        self.assertFalse(self.invoke()['applied']);self.assertFalse(self.incident.exists())
        self.assertEqual((folder/'controller.json').read_bytes(),before)
        self.invoke(apply=True)
        with self.assertRaises(ValueError):self.invoke(apply=True)

    def test_all_stores_are_checked_before_first_mutation(self):
        good=self.startup('bound');self.startup('pending','900002')
        before=(good/'controller.json').read_bytes()
        with self.assertRaises(ValueError):
            recovery.repair(self.root,['900001','900002'],self.incident,apply=True,now=BASE+timedelta(seconds=60))
        self.assertEqual((good/'controller.json').read_bytes(),before);self.assertFalse(self.incident.exists())
