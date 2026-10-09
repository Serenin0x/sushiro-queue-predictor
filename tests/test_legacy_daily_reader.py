"""Old finite daily exports remain visible without migration or origin work."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from sushiwait.cli import main
from sushiwait.dailyarchive import legacy_reader_config, read_legacy_day, read_month
import sushiwait.dailyarchive as reader
from sushiwait.dailycontroller import DailyController
from sushiwait.dailyservice import DailyCollectorService
from sushiwait.remoteservice import RemoteASGI
from sushiwait.remotetasks import RemoteTaskError
import test_daily_controller as fixture
import test_daily_archive_service as archive_fixture
from test_remote_service import request

spec = importlib.util.spec_from_file_location('finite_export_reader_fixture',
    Path(__file__).resolve().parents[1]/'deploy/archive_daily_statistics.py')
exporter = importlib.util.module_from_spec(spec); spec.loader.exec_module(exporter)


class LegacyDailyReaderTests(unittest.TestCase):
    setUp = fixture.DailyControllerTests.setUp
    tearDown = fixture.DailyControllerTests.tearDown
    config = fixture.DailyControllerTests.config
    tick = fixture.DailyControllerTests.tick
    fill = archive_fixture.DailyArchiveServiceTests.fill

    def prepare(self):
        self.fill(3)
        self.original = self.root; self.root = self.original.parent/'new-reader'
        self.root.mkdir(mode=0o700)
        self.exports = self.original.parent/'finite-exports'; self.exports.mkdir(mode=0o700)
        self.legacy = legacy_reader_config(self.exports, '2026-10-10',
            activation=(fixture.BASE+timedelta(days=2)).isoformat(),daily_root=self.root)
        for day in ['2026-10-09', '2026-10-10']:
            folder = self.exports/day; folder.mkdir(mode=0o700)
            raw = (self.original/day/'projection.json').read_bytes()
            value = json.loads(raw)
            self.write(folder/'store-900001.json', value)
            raw = (folder/'store-900001.json').read_bytes()
            self.write(folder/'manifest.json', {'local_date':day,
                'exported_at':day+'T14:05:00+00:00', 'stores':[
                    {'store_id':'900001','name':'合成旧门店','state':'saved',
                     'points':len(value['points']), 'complete_observed_projection':True,
                     'sha256':hashlib.sha256(raw).hexdigest(),'daily_quality':value['summary']}],
                'official_requests_added_by_export':0,'active_database_opened':False,
                'independent_backup':False,'full_day_source_quality_verified':False})
        # One genuinely new-format date, disjoint from old ownership.
        source = self.original/'2026-10-11'; target = self.root/source.name; target.mkdir(mode=0o700)
        for name in ['projection.json','result.json']:
            (target/name).write_bytes((source/name).read_bytes()); (target/name).chmod(0o600)

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value,ensure_ascii=False,separators=(',',':'))); path.chmod(0o600)

    def service(self):
        return DailyCollectorService(root=self.root,store_id='900001',business_hours=self.hours,
            not_before=(fixture.BASE+timedelta(days=2)).isoformat(),wall_clock=self.clock.wall,
            legacy_exports_root=self.exports,legacy_through_date='2026-10-10')

    def mutate(self, change, day='2026-10-09'):
        path=self.exports/day/'manifest.json'; value=json.loads(path.read_text());change(value);self.write(path,value)

    def test_disjoint_old_and_new_month_and_asgi_never_mutate_files_or_query(self):
        self.prepare(); service=self.service(); before=len(self.opener.calls)
        originals={p:p.read_bytes() for p in self.exports.rglob('*') if p.is_file()}
        with patch('sqlite3.connect',side_effect=AssertionError('reader opened database')), \
                patch('sushiwait.remote.RemoteClient.fetch',side_effect=AssertionError('reader fetched origin')):
            value=service.daily_index('900001','2026-10')
            self.assertEqual([d['local_date'] for d in value['days']],['2026-10-09','2026-10-10','2026-10-11'])
            self.assertEqual(value['archive_origins']['2026-10-09'],'finite_trial_daily_export')
            self.assertEqual(value['archive_origins']['2026-10-11'],'daily_controller')
            self.assertEqual(value['unavailable_archive_dates'],[])
            status,detail,_=asyncio.run(request(RemoteASGI(service),'/api/v1/stores/900001/days/2026-10-09'))
            self.assertEqual(status,200)
            old=json.loads((self.exports/'2026-10-09/store-900001.json').read_text())
            self.assertEqual(detail['generated_at'],old['generated_at']);self.assertEqual(detail['points'],old['points'])
            self.assertEqual(detail['archive_lineage']['exported_at'],'2026-10-09T14:05:00+00:00')
            self.assertFalse(detail['archive_lineage']['full_day_source_quality_verified'])
            self.assertFalse(detail['eta_available']);self.assertIsNone(detail['actual_called_count'])
        self.assertEqual(len(self.opener.calls),before);self.assertIsNone(service.thread)
        self.assertEqual({p:p.read_bytes() for p in originals},originals)

    def test_actual_finite_export_producer_is_compatible(self):
        self.prepare(); old=json.loads((self.exports/'2026-10-09/store-900001.json').read_text())
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def read(self,size):return json.dumps(old).encode()[:size]
        producer_root=self.original.parent/'producer';producer_root.mkdir(mode=0o700)
        with patch.object(exporter.urllib.request,'build_opener') as make:
            make.return_value.open.return_value=Response()
            exporter.export_day(producer_root,{'workers':[{'endpoint':'http://127.0.0.1:18801','stores':{'900001':'合成旧门店'}}]},'2026-10-09')
        loaded=read_legacy_day(producer_root/'daily-exports','900001','2026-10-09')
        self.assertEqual(loaded['points'],old['points'])

    def test_hash_summary_count_and_duplicate_scope_corruption_is_unavailable(self):
        self.prepare(); path=self.exports/'2026-10-09/manifest.json'; original=path.read_bytes()
        changes=[lambda m:m['stores'][0].update(sha256='0'*64),
            lambda m:m['stores'][0].update(points=1),
            lambda m:m['stores'][0].update(daily_quality=None),
            lambda m:m['stores'].append(deepcopy(m['stores'][0])),
            lambda m:m.update(local_date='2026-10-08'),
            lambda m:m.update(official_requests_added_by_export=True),
            lambda m:m.update(exported_at='2026-10-09T00:00:00Z'),
            lambda m:m['stores'][0].update(complete_observed_projection=False)]
        for change in changes:
            path.write_bytes(original);self.mutate(change)
            value=read_month(self.root,'900001','2026-10',now=self.clock.wall(),legacy=self.legacy)
            self.assertIn('2026-10-09',value['unavailable_archive_dates'])
            self.assertNotIn('2026-10-09',value['missing_archive_dates'])

    def test_export_failure_missing_projection_or_missing_manifest_is_not_zero(self):
        self.prepare(); path=self.exports/'2026-10-09/manifest.json'; raw=path.read_bytes()
        self.mutate(lambda m:m.update(stores=[{'store_id':'900001','name':'合成','error_type':'TimeoutError'}]))
        service=self.service()
        self.assertEqual(asyncio.run(request(RemoteASGI(service),'/api/v1/stores/900001/days/2026-10-09'))[0],503)
        path.write_bytes(raw); projection=self.exports/'2026-10-09/store-900001.json'; saved=projection.read_bytes()
        projection.unlink()
        with self.assertRaises(RemoteTaskError):read_legacy_day(self.exports,'900001','2026-10-09')
        projection.write_bytes(saved);projection.chmod(0o600);path.unlink()
        with self.assertRaises(RemoteTaskError):read_legacy_day(self.exports,'900001','2026-10-09')

    def test_new_archive_on_old_date_is_conflict_even_after_cache_warmed(self):
        self.prepare();service=self.service();service.daily_index('900001','2026-10')
        folder=self.root/'2026-10-09';folder.mkdir(mode=0o700);self.write(folder/'result.json',{})
        value=service.daily_index('900001','2026-10')
        self.assertIn('2026-10-09',value['unavailable_archive_dates'])
        self.assertNotIn('2026-10-09',[d['local_date'] for d in value['days']])

    def test_cache_revalidates_manifest_or_projection_identity_changes(self):
        self.prepare();service=self.service();service.daily_index('900001','2026-10')
        with patch('sushiwait.dailyarchive.read_daily_archive',side_effect=AssertionError('cached bytes reread')):
            self.assertEqual(len(service.daily_index('900001','2026-10')['days']),3)
        self.mutate(lambda m:m.update(independent_backup=True))
        self.assertIn('2026-10-09',service.daily_index('900001','2026-10')['unavailable_archive_dates'])
        (self.exports/'2026-10-10/store-900001.json').write_text('{}')
        self.assertIn('2026-10-10',service.daily_index('900001','2026-10')['unavailable_archive_dates'])

    def test_private_modes_symlinks_and_unknown_projected_fields_are_rejected(self):
        self.prepare();manifest=self.exports/'2026-10-09/manifest.json';manifest.chmod(0o644)
        with self.assertRaises(RemoteTaskError):read_legacy_day(self.exports,'900001','2026-10-09')
        manifest.chmod(0o600);projection=self.exports/'2026-10-09/store-900001.json';raw=projection.read_bytes()
        value=json.loads(raw);value['points'][0]['authorization']='synthetic forbidden field';self.write(projection,value)
        self.mutate(lambda m:m['stores'][0].update(sha256=hashlib.sha256(projection.read_bytes()).hexdigest()))
        with self.assertRaises(RemoteTaskError):read_legacy_day(self.exports,'900001','2026-10-09')
        projection.unlink();projection.symlink_to(self.original/'2026-10-09/projection.json')
        with self.assertRaises(RemoteTaskError):read_legacy_day(self.exports,'900001','2026-10-09')

    def test_missing_export_directory_is_missing_not_created(self):
        self.prepare();absent=self.exports/'not-created'
        legacy=legacy_reader_config(absent,'2026-10-10',daily_root=self.root)
        value=read_month(self.root,'900001','2026-10',now=self.clock.wall(),legacy=legacy)
        self.assertIn('2026-10-09',value['missing_archive_dates']);self.assertEqual(value['unavailable_archive_dates'],[])
        self.assertFalse(absent.exists())

    def test_directory_permission_or_symlink_failure_marks_only_that_day_unavailable(self):
        self.prepare();folder=self.exports/'2026-10-09';folder.chmod(0o755)
        value=read_month(self.root,'900001','2026-10',now=self.clock.wall(),legacy=self.legacy)
        self.assertEqual(value['unavailable_archive_dates'],['2026-10-09'])
        self.assertEqual(len(value['days']),2)
        folder.chmod(0o700);alias=self.exports.parent/'export-alias';alias.symlink_to(self.exports,target_is_directory=True)
        config=legacy_reader_config(alias,'2026-10-10',daily_root=self.root)
        value=read_month(self.root,'900001','2026-10',now=self.clock.wall(),legacy=config)
        self.assertEqual(value['unavailable_archive_dates'],[f'2026-10-{i:02d}' for i in range(1,11)])

    def test_manifest_changed_between_reads_is_rejected(self):
        self.prepare();read=reader.read_daily_archive
        def changing(path):
            raw=read(path)
            if Path(path).name=='store-900001.json':
                self.mutate(lambda m:m.update(exported_at='2026-10-09T14:06:00+00:00'))
            return raw
        with patch.object(reader,'read_daily_archive',side_effect=changing):
            with self.assertRaises(RemoteTaskError):read_legacy_day(self.exports,'900001','2026-10-09')

    def test_projection_can_be_incomplete_without_becoming_full_day_or_eta(self):
        self.prepare();path=self.exports/'2026-10-09/store-900001.json';value=json.loads(path.read_text())
        value['points']=value['points'][1:];value['returned_graph_points']=1;value['graph_truncated']=True
        self.write(path,value)
        self.mutate(lambda m:m['stores'][0].update(points=1,complete_observed_projection=False,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        loaded=read_legacy_day(self.exports,'900001','2026-10-09')
        self.assertTrue(loaded['graph_truncated']);self.assertFalse(loaded['archive_lineage']['complete_observed_projection'])
        self.assertFalse(loaded['eta_available']);self.assertIsNone(loaded['actual_called_count'])

    def test_cutoff_pair_path_and_activation_are_validated_without_files(self):
        self.prepare();activation=(fixture.BASE+timedelta(days=2)).isoformat()
        for root,day in [(self.exports,None),(None,'2026-10-10'),('relative','2026-10-10'),
                (self.exports,'2026-10-11'),(self.exports,'2026-02-30'),(self.root,'2026-10-10')]:
            with self.assertRaises(RemoteTaskError):legacy_reader_config(root,day,activation=activation,daily_root=self.root)

    def test_cli_month_reads_old_dates_but_does_not_print_private_paths(self):
        self.prepare();out=io.StringIO()
        with patch('sys.stdout',out):
            result=main(['daily-archive-month','--root',str(self.root),'--store-id','900001','--month','2026-10',
                '--legacy-exports-root',str(self.exports),'--legacy-through-date','2026-10-10'])
        self.assertEqual(result,0);self.assertEqual(len(json.loads(out.getvalue())['days']),3)
        self.assertNotIn(str(self.exports),out.getvalue());self.assertNotIn(str(self.root),out.getvalue())


if __name__=='__main__':unittest.main()
