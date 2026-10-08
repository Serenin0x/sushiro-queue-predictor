"""Complete private archive, cross-date selection, genuine CLI and tracker."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from sushiwait.cli import main
from sushiwait.outcomes import _utc
from sushiwait.trendarchive import (append_profiles, read_archive, select_profile,
    validate_archive, write_archive, _hash, MAX_BYTES)
from sushiwait.trendprofiles import (POLICY, TrendProfileError, _digest, validate_profile,
    score_context, enrich_context)
import test_trend_profiles as helpers

NOW = datetime(2026,10,7,20,tzinfo=timezone.utc)


def profile(month=9, day=1, slots=8, *, removed=3, store='900001'):
    rows=[]
    for i in range(slots):
        at=datetime(2026,month,day,4,tzinfo=timezone.utc)+timedelta(seconds=120*i)
        rows.append([at.isoformat(),3,90000,removed,removed+1])
    cutoff=datetime.fromisoformat(rows[-1][0])+timedelta(seconds=30)
    value={'trend_profile_schema_version':1,'policy':POLICY,'source':'crm_remote_v1_1',
        'store_id':store,'created_at':(cutoff+timedelta(seconds=1)).isoformat(),
        'history_cutoff':cutoff.isoformat(),'window_seconds':120,'max_gap_seconds':60,
        'minimum_pairs':2,'minimum_coverage_ppm':500000,'availability_basis':'local_first_receipt',
        'samples':rows}
    value['profile_sha256']=_digest(value);return value


def archive(*profiles, clock=NOW-timedelta(seconds=120)):
    return append_profiles(list(profiles) or [profile(day=1),profile(day=2)],now=clock)


def select(value, **options):
    settings={'reference_for':NOW.isoformat(),'cadence':[90000,3],'now':NOW}
    settings.update(options);return select_profile(value,**settings)


def context(queue='ordinary',removed=100):
    value=helpers.context(queue=queue,removed=removed)
    value['as_of']=NOW.isoformat();value['expires_at']=(NOW+timedelta(seconds=60)).isoformat()
    for name in ('latest_queue_received_at','latest_count_received_at'):value[name]=NOW.isoformat()
    for feature in value['features']:feature['available_at']=NOW.isoformat()
    return value


class TrendArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.root.chmod(0o700)

    def test_complete_archive_long_span_and_current_numeric_rank(self):
        value=archive(profile(month=7,day=7),profile(day=1))
        chosen=select(value)
        self.assertEqual(chosen['trend_profile_schema_version'],2)
        self.assertEqual(len(chosen['samples']),16)
        self.assertEqual(chosen['selection']['archive_rows_known'],16)
        self.assertEqual(chosen['selection']['matched_group'],'day_type')
        score=score_context(chosen,context(),now=NOW)
        self.assertEqual(score['rank_lower_ppm'],1000000)
        self.assertFalse(score['risk_probability_calibrated'])

    def test_duplicate_profile_and_duplicate_windows_do_not_reset_receipts(self):
        p=profile();value=archive(p);before=deepcopy(value)
        duplicate=append_profiles([p,p],archive=value,now=NOW)
        self.assertEqual(duplicate,before)
        other=deepcopy(p);other['created_at']=(datetime.fromisoformat(other['created_at'])+timedelta(seconds=1)).isoformat()
        other['profile_sha256']=_digest(other)
        updated=append_profiles([other],archive=value,now=NOW-timedelta(seconds=60))
        self.assertEqual(updated['revision'],2)
        self.assertEqual(updated['supersedes_sha256'],value['archive_sha256'])
        self.assertEqual(updated['imports'][:1],value['imports'])
        chosen=select(updated,reference_for=(NOW-timedelta(seconds=90)).isoformat())
        self.assertEqual(chosen['selection']['archive_rows_known'],8)
        self.assertEqual(chosen['selection']['archive_imports_known'],1)
        self.assertEqual(value,before)

    def test_whole_batch_conflict_preserves_old_snapshot(self):
        value=archive(profile());before=deepcopy(value)
        with self.assertRaises(TrendProfileError):
            append_profiles([profile(day=3),profile(removed=999)],archive=value,now=NOW)
        self.assertEqual(value,before)

    def test_scope_and_availability_modes_cannot_mix(self):
        value=archive(profile());bad=[]
        for key,new in [('store_id','900002'),('max_gap_seconds',120),('minimum_pairs',3),
                        ('availability_basis','sealed_response_reconstruction')]:
            p=profile(day=3);p[key]=new;p['profile_sha256']=_digest(p);bad.append(p)
        for p in bad:
            with self.assertRaises(TrendProfileError):append_profiles([p],archive=value,now=NOW)

    def test_later_receipts_never_backfill_target(self):
        value=archive(clock=NOW)
        chosen=select(value,reference_for=(NOW-timedelta(seconds=1)).isoformat())
        self.assertEqual(chosen['samples'],[])
        self.assertEqual(chosen['selection']['archive_imports_known'],0)
        self.assertEqual(chosen['selection']['archive_rows_known'],0)

    def test_every_import_validated_even_if_received_after_query_cutoff(self):
        value=archive();value['imports'][-1]['profile']['samples'][0][3]+=1
        value['archive_sha256']=_hash(value)
        with self.assertRaises(TrendProfileError):
            select(value,reference_for=(NOW-timedelta(seconds=300)).isoformat())

    def test_archive_hash_receipt_order_clock_and_lineage_rejected(self):
        good=archive()
        changes=[lambda a:a.update(revision=True),lambda a:a.update(revision=2),
            lambda a:a['imports'][0].update(received_at=(NOW+timedelta(seconds=1)).isoformat()),
            lambda a:a['imports'][-1].update(received_at=(NOW-timedelta(seconds=121)).isoformat()),
            lambda a:a.update(extra=1)]
        for change in changes:
            a=deepcopy(good);change(a);a['archive_sha256']=_hash(a)
            with self.assertRaises(TrendProfileError):validate_archive(a,now=NOW)
        a=deepcopy(good);a['archive_sha256']='0'*64
        with self.assertRaises(TrendProfileError):validate_archive(a,now=NOW)

    def test_source_profile_and_archive_cannot_claim_future_creation(self):
        p=profile();p['created_at']=(NOW+timedelta(seconds=1)).isoformat();p['profile_sha256']=_digest(p)
        with self.assertRaises(TrendProfileError):append_profiles([p],now=NOW)
        with self.assertRaises(TrendProfileError):select(archive(),reference_for=(NOW+timedelta(seconds=1)).isoformat())

    def test_capacity_limits_reject_without_dropping_old_imports(self):
        p=profile();items=[]
        for i in range(512):
            value=deepcopy(p);value['created_at']=(datetime.fromisoformat(p['created_at'])+timedelta(seconds=i)).isoformat()
            value['profile_sha256']=_digest(value);items.append({'profile':value,'received_at':_utc(NOW-timedelta(seconds=120))})
        a=archive();a['imports']=items;a['archive_sha256']=_hash(a);validate_archive(a,now=NOW)
        before=deepcopy(a)
        with self.assertRaises(TrendProfileError):append_profiles([profile(day=3)],archive=a,now=NOW)
        self.assertEqual(a,before)

    def test_date_balancing_and_explicit_full_versus_selected_counts(self):
        a=archive(profile(day=1,slots=96),profile(day=2,slots=96))
        chosen=select(a,maximum_windows=8,maximum_per_day=8)
        self.assertEqual(len(chosen['samples']),8)
        self.assertEqual(chosen['selection']['archive_rows_known'],192)
        self.assertGreater(chosen['selection']['matching_windows'],8)
        dates={row[0][:10] for row in chosen['samples']};self.assertEqual(len(dates),2)
        self.assertTrue(all(sum(row[0][:10]==d for row in chosen['samples'])==4 for d in dates))

    def test_many_months_selects_spread_dates_without_latest_only_truncation(self):
        profiles=[]
        at=datetime(2026,1,1,tzinfo=timezone.utc)
        while at.month <= 9:
            profiles.append(profile(month=at.month,day=at.day))
            at+=timedelta(days=1)
        value=None
        for start in range(0,len(profiles),16):
            value=append_profiles(profiles[start:start+16],archive=value,now=NOW-timedelta(seconds=120))
        chosen=select(value)
        self.assertEqual(len(chosen['samples']),96)
        self.assertEqual(chosen['selection']['selected_days'],96)
        self.assertEqual(chosen['selection']['archive_rows_known'],len(profiles)*8)
        self.assertTrue(chosen['samples'][0][0].startswith('2026-01-'))
        self.assertTrue(chosen['samples'][-1][0].startswith('2026-09-'))

    def test_unique_window_limit_is_separate_from_profile_count(self):
        initial=archive(profile(month=1,day=1,slots=96))
        imports=[]
        for i in range(256):
            at=datetime(2026,1,1,tzinfo=timezone.utc)+timedelta(days=i)
            imports.append({'profile':profile(month=at.month,day=at.day,slots=96),
                'received_at':_utc(NOW-timedelta(seconds=120))})
        initial['imports']=imports;initial['archive_sha256']=_hash(initial)
        validate_archive(initial,now=NOW)
        before=deepcopy(initial)
        at=datetime(2026,1,1,tzinfo=timezone.utc)+timedelta(days=256)
        with self.assertRaises(TrendProfileError):
            append_profiles([profile(month=at.month,day=at.day,slots=96)],archive=initial,now=NOW)
        self.assertEqual(initial,before)

    def test_selection_is_rate_independent_and_deterministic(self):
        a=archive(profile(day=1,slots=96),profile(day=2,slots=96))
        b=archive(profile(day=1,slots=96,removed=999),profile(day=2,slots=96,removed=999))
        one=select(a,maximum_windows=8);two=select(b,maximum_windows=8)
        self.assertEqual([r[0] for r in one['samples']],[r[0] for r in two['samples']])
        self.assertEqual(one,select(a,maximum_windows=8))

    def test_same_day_and_per_day_cap_cannot_fake_eight_windows(self):
        self.assertEqual(select(archive(profile(slots=96)))['samples'],[])
        self.assertEqual(select(archive(),maximum_per_day=1)['samples'],[])

    def test_holiday_and_sampling_cadence_keep_missing_evidence(self):
        self.assertEqual(select(archive(profile(month=10,day=1),profile(month=10,day=2)))['samples'],[])
        self.assertEqual(select(archive(),cadence=[90000,1])['samples'],[])
        self.assertEqual(select(archive(),cadence=None)['samples'],[])

    def test_reference_overlapping_current_window_is_excluded(self):
        p=profile(month=10,day=8,slots=1)
        p['samples'][0][0]=(NOW-timedelta(seconds=120)).isoformat()
        p['history_cutoff']=(NOW-timedelta(seconds=100)).isoformat();p['created_at']=(NOW-timedelta(seconds=90)).isoformat();p['profile_sha256']=_digest(p)
        a=archive(p,clock=NOW-timedelta(seconds=60))
        chosen=select(a,minimum_windows=2,minimum_days=1)
        self.assertEqual(chosen['selection']['archive_rows_known'],1)
        target=NOW-timedelta(seconds=1)
        self.assertEqual(select(a,reference_for=target.isoformat(),minimum_windows=2,minimum_days=1)['selection']['archive_rows_known'],0)

    def test_selected_metadata_bounds_and_source_scope_are_validated(self):
        good=select(archive())
        changes=[lambda p:p['selection'].update(selected_windows=999),
            lambda p:p['selection'].update(selected_days=99),lambda p:p['selection'].update(maximum_per_day=1),
            lambda p:p['selection'].update(reference_for=NOW.replace(hour=5).isoformat()),
            lambda p:p['selection'].update(cadence=[300000,1]),lambda p:p['selection'].update(archive_sha256='z'*64)]
        for change in changes:
            p=deepcopy(good);change(p);p['profile_sha256']=_digest(p)
            with self.assertRaises(TrendProfileError):validate_profile(p,now=NOW)

    def test_selected_profile_cannot_be_reimported_as_original_evidence(self):
        p=select(archive())
        with self.assertRaises(TrendProfileError):append_profiles([p],now=NOW)

    def test_public_enrichment_excludes_private_archive_and_selection_metadata(self):
        chosen=select(archive());public,score=enrich_context(chosen,context(),now=NOW)
        self.assertEqual(score['rank_lower_ppm'],1000000)
        text=json.dumps(public)
        for key in ('archive_sha256','reference_for','selection','samples','archive_id'):self.assertNotIn(key,text)

    def test_target_date_changes_do_not_reuse_selected_prior(self):
        chosen=select(archive());c=context()
        later=datetime(2026,10,10,4,tzinfo=timezone.utc)
        c['as_of']=later.isoformat();c['expires_at']=(later+timedelta(seconds=60)).isoformat()
        c['latest_queue_received_at']=c['as_of'];c['latest_count_received_at']=c['as_of']
        for f in c['features']:f['available_at']=c['as_of']
        self.assertEqual(score_context(chosen,c,now=later)['unavailable_reason'],'reference_target_changed')

    def test_private_snapshot_file_no_overwrite_or_socket(self):
        path=self.root/'archive.json';value=archive()
        with patch('socket.socket',side_effect=AssertionError('network')) as sockets:
            write_archive(value,path);before=path.read_bytes();self.assertEqual(read_archive(path,now=NOW),value)
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            with self.assertRaises(Exception):write_archive(value,path)
            self.assertEqual(path.read_bytes(),before);self.assertFalse(sockets.called)

    def test_unsafe_symlink_oversize_and_duplicate_json_rejected(self):
        p=self.root/'archive.json';write_archive(archive(),p)
        p.chmod(0o644)
        with self.assertRaises(TrendProfileError):read_archive(p,now=NOW)
        p.chmod(0o600);link=self.root/'alias.json';link.symlink_to(p)
        with self.assertRaises(TrendProfileError):read_archive(link,now=NOW)
        p.write_bytes(b' '*(MAX_BYTES+1))
        with self.assertRaises(TrendProfileError):read_archive(p,now=NOW)
        p.write_text('{"trend_archive_schema_version":1,"trend_archive_schema_version":1}')
        with self.assertRaises(TrendProfileError):read_archive(p,now=NOW)

    def test_actual_add_select_cli_preserves_source_and_stdout_privacy(self):
        sources=[]
        for i,p in enumerate((profile(day=1),profile(day=2))):
            name=self.root/f'p{i}.json';name.write_text(json.dumps(p));name.chmod(0o600);sources.append(name)
        before=[p.read_bytes() for p in sources];a=self.root/'archive.json';selected=self.root/'selected.json';stdout=io.StringIO()
        with patch('sushiwait.trendarchive._now',return_value=NOW),contextlib.redirect_stdout(stdout),patch('socket.socket',side_effect=AssertionError('network')) as sockets:
            self.assertEqual(main(['trend-archive-add','--profile-file',str(sources[0]),'--profile-file',str(sources[1]),'--output',str(a)]),0)
            self.assertEqual(main(['trend-archive-select','--archive-file',str(a),'--reference-for',NOW.isoformat(),'--cadence-ms','30000','--output',str(selected)]),0)
            self.assertEqual(main(['trend-archive-add','--profile-file',str(sources[0]),'--archive-file',str(a),'--output',str(a)]),1)
        self.assertEqual([p.read_bytes() for p in sources],before)
        self.assertEqual(len(json.loads(selected.read_bytes())['samples']),16)
        self.assertNotIn(str(self.root),stdout.getvalue());self.assertFalse(sockets.called)

    def test_cli_rejects_large_import_batch_before_reading_profiles(self):
        arguments=['trend-archive-add','--output',str(self.root/'archive.json')]
        for _ in range(17):arguments+=['--profile-file',str(self.root/'missing.json')]
        with patch('sushiwait.trendprofiles.read_profile',side_effect=AssertionError('unbounded_read')) as reads,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(arguments),1)
        self.assertFalse(reads.called)

    def test_archive_constructor_and_real_tracking_cycle_share_one_reader(self):
        import test_tracker_loop as loop
        from sushiwait.trackerloop import TrackingCoordinator
        from sushiwait.tracking import create_session, TrackingSession
        refdir=self.root/'references';refdir.mkdir(mode=0o700);path=refdir/'archive.json';write_archive(archive(),path)
        ticket=loop.fixture.ticket();delta=(NOW-loop.BASE).total_seconds()
        for key in ('issued_at','created_at','deadline_at','desired_arrival_at'):
            if ticket.get(key) is not None:ticket[key]=(datetime.fromisoformat(ticket[key])+timedelta(seconds=delta)).isoformat()
        state=self.root/'state';state.mkdir(mode=0o700);create_session(ticket,directory=state,now=NOW)
        state2=self.root/'state2';state2.mkdir(mode=0o700)
        other=deepcopy(ticket);other['episode_id']=str(uuid4());create_session(other,directory=state2,now=NOW)
        view=loop.LiveRemoteView(['900001'],stale_after_seconds=360)
        view.publish(loop.fixture.record(0,('13','14')))
        frame=view.tracking_projection('900001',now=loop.BASE+timedelta(seconds=1),service_state='running',worker_alive=True)
        def shift(value):
            if isinstance(value,dict):
                for k,v in value.items():
                    if isinstance(v,str) and ('T' in v) and (v.endswith('Z') or '+00:00' in v):
                        try:value[k]=(datetime.fromisoformat(v.replace('Z','+00:00'))+timedelta(seconds=delta-1)).isoformat()
                        except ValueError:pass
                    else:shift(v)
            elif isinstance(value,list):
                for v in value:shift(v)
        shift(frame)
        class Reader:
            calls=0
            def __call__(self,store):self.calls+=1;return frame
        reader=Reader()
        coordinator=TrackingCoordinator([state,state2],stores=['900001'],reader=reader,trend_archive_files=[str(path)],clock=lambda:NOW)
        self.addCleanup(coordinator.close)
        with patch('sushiwait.trendarchive.select_profile', wraps=select_profile) as selections:
            coordinator.cycle()
            self.assertEqual(selections.call_count,1)
        for future in list(coordinator.futures.values()):future.result(timeout=3)
        coordinator._reap()
        with TrackingSession(state) as session:
            _,receipt,_=session.load(now=NOW)
            ids={f['feature_id'] for f in receipt['observation']['public_context']['features']}
            self.assertIn('ordinary_turnover_rank_lower_ppm',ids)
        self.assertEqual(reader.calls,1)

    def test_archive_and_static_profile_for_same_store_cannot_start_tracker(self):
        import test_tracker_loop as loop
        from sushiwait.trackerloop import TrackingCoordinator, TrackerLoopError
        from sushiwait.tracking import create_session
        refdir=self.root/'references';refdir.mkdir(mode=0o700);path=refdir/'archive.json';write_archive(archive(),path)
        p=refdir/'profile.json';p.write_text(json.dumps(profile()));p.chmod(0o600)
        state=self.root/'state';state.mkdir(mode=0o700);create_session(loop.fixture.ticket(),directory=state,now=loop.BASE)
        with self.assertRaises(TrackerLoopError):
            TrackingCoordinator([state],stores=['900001'],reader=lambda s:None,trend_archive_files=[str(path)],trend_profile_files=[str(p)],clock=lambda:NOW)


if __name__=='__main__':unittest.main()
