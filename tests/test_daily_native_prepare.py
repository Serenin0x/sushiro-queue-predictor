"""Prepare only; generated daily units never modify the old trial or network."""
from argparse import Namespace
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import os
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'deploy'))
try:
    import prepare_daily_native as preparation
finally:
    sys.path.pop(0)
from test_daily_controller import BASE


class DailyNativePrepareTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.folder=Path(self.tmp.name).resolve()
        self.release=self.folder/'release';self.release.mkdir()
        (self.release/'.venv/bin').mkdir(parents=True)
        (self.release/'.venv/bin/sushiwait').touch()
        (self.release/'config').mkdir()
        for name in ['collection-stores.json','default-business-hours.json']:
            (self.release/'config'/name).write_bytes((ROOT/'config'/name).read_bytes())
        self.args=Namespace(release_dir=str(self.release),root=str(self.folder/'new-daily'),
            not_before=(BASE+timedelta(days=2)).isoformat(),prefix='test-daily',user='ubuntu',first_port=18821)
        self.old_umask=os.umask(0o077)

    def tearDown(self):
        os.umask(self.old_umask);self.tmp.cleanup()

    def test_twelve_new_units_have_explicit_activation_and_separate_private_roots(self):
        result=preparation.prepare(self.args,now=BASE)
        root=Path(self.args.root);units=list(root.glob('*.service'))
        self.assertEqual(len(units),12);self.assertFalse(result['services_started'])
        self.assertEqual(result['origin_requests'],0);self.assertFalse(result['same_store_old_writer_stop_verified'])
        for unit in units:
            content=unit.read_text()
            self.assertIn('remote-daily-serve',content)
            self.assertIn('--not-before 2026-10-11T03:00:00.000Z',content)
            self.assertIn('--daily-pair-cap 1500',content)
            self.assertIn('ProtectSystem=strict',content)
            self.assertNotIn('remote-campaign-serve',content)
        self.assertEqual(len(list(root.glob('store-*'))),12)
        self.assertTrue(all(p.stat().st_mode&0o777==0o700 for p in root.glob('store-*')))
        hub=json.loads((root/'hub.json').read_text())
        self.assertEqual(hub['schema_version'],2);self.assertNotIn('deadline_at',hub)
        self.assertEqual(len(hub['workers']),12)

    def test_existing_root_is_not_adopted_or_overwritten(self):
        preparation.prepare(self.args,now=BASE)
        root=Path(self.args.root);before={p:p.read_bytes() for p in root.iterdir() if p.is_file()}
        with self.assertRaises(FileExistsError):preparation.prepare(self.args,now=BASE)
        self.assertEqual({p:p.read_bytes() for p in before},before)

    def test_past_activation_is_rejected_before_any_files_are_created(self):
        self.args.not_before=(BASE-timedelta(seconds=1)).isoformat()
        with self.assertRaises(ValueError):preparation.prepare(self.args,now=BASE)
        self.assertFalse(Path(self.args.root).exists())

    def test_explicit_legacy_cutoff_is_read_only_and_must_precede_activation(self):
        self.args.legacy_exports_root=str(self.folder/'old-exports')
        self.args.legacy_through_date='2026-10-10'
        preparation.prepare(self.args,now=BASE)
        root=Path(self.args.root)
        for unit in root.glob('*.service'):
            content=unit.read_text()
            self.assertIn('--legacy-through-date 2026-10-10',content)
            self.assertIn('--legacy-exports-root',content)
            self.assertNotIn('ReadWritePaths='+self.args.legacy_exports_root,content)
        self.assertFalse(Path(self.args.legacy_exports_root).exists())
        self.assertEqual(json.loads((root/'units.json').read_text())['legacy_reader']['through_date'],'2026-10-10')

    def test_invalid_legacy_pair_is_rejected_before_new_directories(self):
        self.args.legacy_exports_root=str(self.folder/'old-exports')
        self.args.legacy_through_date='2026-10-11'
        with self.assertRaises(ValueError):preparation.prepare(self.args,now=BASE)
        self.assertFalse(Path(self.args.root).exists())
