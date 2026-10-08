"""Prospective date-aware ranks, real private files, no official transport."""
import contextlib
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sushiwait.cli import main
from sushiwait.fusion import context_from_history, public_request
from sushiwait.remote import RemoteStore
from sushiwait.remoteservice import LiveRemoteView
from sushiwait.trendprofiles import (POLICY, TrendProfileError, _digest, build_profile,
    enrich_context, read_profile, score_context, validate_profile, write_profile)
import test_remote_signals as fixtures

BASE = datetime(2026, 10, 6, 20, tzinfo=timezone.utc)


def stamp(seconds=0):
    return (BASE+timedelta(seconds=seconds)).isoformat()


def profile(*, removed=3, duration=90000, dates=(1,2), samples_per_day=4):
    samples=[]
    for day in dates:
        for slot in range(samples_per_day):
            at=datetime(2026,10,day,18,tzinfo=timezone.utc)+timedelta(minutes=30*slot)
            samples.append([at.isoformat(),3,duration,removed,removed+2])
    value={'trend_profile_schema_version':1,'policy':POLICY,'source':'crm_remote_v1_1',
        'store_id':'900001','created_at':stamp(-1),'history_cutoff':stamp(-3600),
        'window_seconds':120,'max_gap_seconds':60,'minimum_pairs':2,
        'minimum_coverage_ppm':500000,'samples':samples,'availability_basis':'local_first_receipt'}
    value['profile_sha256']=_digest(value)
    return value


def context(removed=3,duration=90000,queue='ordinary'):
    return {'schema_version':1,'source':'crm_remote_v1_1','store_id':'900001','queue_type':queue,
        'observation_revision':8,'as_of':stamp(),'expires_at':stamp(60),
        'window_seconds':120,'max_local_age_seconds':60,'latest_queue_received_at':stamp(-1),
        'latest_count_received_at':stamp(-.5),'source_freshness':'unknown','count_unit':'unknown',
        'store_identity_verified':False,'collector_running':True,'latest_queue_origin':'worker_commit',
        'features':[{'feature_id':queue+suffix,'value':value,'available_at':stamp()}
            for suffix,value in [('_removed_labels',removed),('_comparable_pairs',3),
                ('_observed_milliseconds',duration)]]}


def record(second,labels=(),**kw):
    offset=(BASE-fixtures.BASE).total_seconds()
    return fixtures.record(offset+second,labels,**kw)


class TrendProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.root.chmod(0o700)
        self.db=self.root/'remote.sqlite3'

    def write(self,seconds,*,receipt=None,run=None,**kw):
        with RemoteStore(self.db) as remote:
            if run is not None:remote.run_id=run
            else:remote.run_id=fixtures.RUN
            for index,second in enumerate(seconds):
                with patch('sushiwait.remoteintake._clock',return_value=BASE+timedelta(
                        seconds=second+.5 if receipt is None else receipt)):
                    remote.append(record(second,[str(index),'99-1'],**kw))

    def build(self,as_of=-1,**kw):
        with RemoteStore(self.db,read_only=True) as remote,patch('socket.socket',side_effect=AssertionError('network')):
            options=dict(window_seconds=120,max_gap_seconds=60,minimum_pairs=2)
            options.update(kw)
            return build_profile(remote,'900001',as_of=stamp(as_of),now=BASE,**options)

    def test_whole_nonoverlapping_windows_and_database_unchanged(self):
        self.write(range(-1200,-29,30));before=self.db.read_bytes()
        p=self.build();self.assertEqual(len(p['samples']),9)
        self.assertTrue(all(row[1:]==[3,90000,3,0] for row in p['samples']))
        self.assertEqual(before,self.db.read_bytes())
        self.assertTrue(all((datetime.fromisoformat(b[0])-datetime.fromisoformat(a[0])).total_seconds()==120
            for a,b in zip(p['samples'],p['samples'][1:])))

    def test_future_success_does_not_change_earlier_profile(self):
        self.write(range(-1200,-329,30));a=self.build(as_of=-300)
        self.write([-180,-150,-120,-90]);b=self.build(as_of=-300)
        self.assertEqual(a,b)

    def test_future_local_receipts_are_not_historical_input(self):
        self.write(range(-1200,-29,30),receipt=-1)
        self.assertEqual(self.build()['samples'],[])

    def test_run_change_and_sampling_gap_do_not_become_large_turnover(self):
        self.write([-1200,-1170]);self.write([-800,-770],run='00000000-0000-0000-0000-000000000002')
        self.assertEqual(self.build()['samples'],[])

    def test_failure_does_not_create_zero_rate(self):
        self.write([-1200,-1170,-1140,-1110],queue_error=True)
        self.assertEqual(self.build()['samples'],[])

    def test_count_failure_keeps_valid_display_stream(self):
        self.write(range(-1200,-29,30),count_error=True)
        self.assertEqual(len(self.build()['samples']),9)

    def test_corrupt_history_fails_whole_operation(self):
        self.write(range(-1200,-29,30))
        db=sqlite3.connect(self.db);db.execute("UPDATE remote_samples SET payload_json='{}' WHERE id=1");db.commit();db.close()
        with self.assertRaises(TrendProfileError):self.build()

    def test_active_writer_database_is_rejected(self):
        self.write([-1200,-1170])
        with RemoteStore(self.db):
            with self.assertRaises(OSError):self.build()

    def test_window_limit_rejects_without_truncating(self):
        self.write([-20000,-30])
        with self.assertRaisesRegex(TrendProfileError,'window_limit'):self.build()

    def test_empty_database_gives_no_reference(self):
        with RemoteStore(self.db):pass
        self.assertEqual(self.build()['samples'],[])

    def test_zero_and_positive_ties_keep_rank_interval(self):
        p=profile(removed=0)
        score=score_context(p,context(0),now=BASE)
        self.assertEqual((score['rank_lower_ppm'],score['rank_upper_ppm']),(0,1000000))
        self.assertEqual(score['display_turnover_band'],'within_or_tied_reference')
        self.assertIsNone(score['no_show_rate']);self.assertFalse(score['risk_probability_calibrated'])
        score=score_context(profile(),context(),now=BASE)
        self.assertEqual((score['rank_lower_ppm'],score['rank_upper_ppm']),(0,1000000))

    def test_changed_live_rate_changes_rank_numerically(self):
        a=score_context(profile(),context(0),now=BASE);b=score_context(profile(),context(6),now=BASE)
        self.assertEqual((a['rank_lower_ppm'],a['rank_upper_ppm']),(0,0))
        self.assertEqual((b['rank_lower_ppm'],b['rank_upper_ppm']),(1000000,1000000))
        self.assertEqual(b['display_turnover_band'],'higher_than_reference')
        self.assertFalse(b['eta_available'])

    def test_rate_uses_observed_time_not_whole_window_or_number_gap(self):
        score=score_context(profile(removed=3,duration=90000),context(2,duration=90000),now=BASE)
        self.assertEqual(score['rank_upper_ppm'],0)
        reservation=score_context(profile(),context(6,queue='reservation'),now=BASE)
        self.assertEqual(reservation['rank_lower_ppm'],1000000)

    def test_incompatible_sampling_cadence_is_not_fast_or_slow_evidence(self):
        score=score_context(profile(),context(3,duration=120000),now=BASE)
        self.assertIsNone(score['rank_upper_ppm'])
        c=context();c['features'].append({'feature_id':'longest_gap_seconds','value':300,'available_at':stamp()})
        self.assertEqual(score_context(profile(),c,now=BASE)['unavailable_reason'],'current_window_unavailable_or_sparse')

    def test_legacy_receipts_only_support_explicit_prospective_reconstruction(self):
        self.write(range(-1200,-29,30));db=sqlite3.connect(self.db)
        for row in db.execute('SELECT id,payload_json FROM remote_samples').fetchall():
            value=json.loads(row[1]);value.pop('local_intake');db.execute('UPDATE remote_samples SET payload_json=? WHERE id=?',(json.dumps(value),row[0]))
        db.commit();db.close();before=self.db.read_bytes()
        self.assertEqual(self.build()['samples'],[])
        reconstructed=self.build(availability_basis='sealed_response_reconstruction')
        self.assertEqual(len(reconstructed['samples']),9)
        self.assertEqual(reconstructed['availability_basis'],'sealed_response_reconstruction')
        self.assertEqual(before,self.db.read_bytes())

    def test_many_same_day_windows_do_not_satisfy_distinct_days(self):
        score=score_context(profile(dates=(1,),samples_per_day=8),context(),now=BASE)
        self.assertEqual(score['unavailable_reason'],'reference_dates_or_windows_insufficient')

    def test_calendar_fallback_reports_which_group_was_used(self):
        score=score_context(profile(),context(),now=BASE)
        self.assertEqual(score['matched_group'],'day_type')
        self.assertEqual((score['reference_windows'],score['reference_days']),(8,2))

    def test_holiday_samples_do_not_silently_become_workday_prior(self):
        c=context();c['as_of']=datetime(2026,10,10,20,tzinfo=timezone.utc).isoformat()
        c['expires_at']=datetime(2026,10,10,20,1,tzinfo=timezone.utc).isoformat()
        c['latest_queue_received_at']=c['as_of'];c['latest_count_received_at']=c['as_of']
        for f in c['features']:f['available_at']=c['as_of']
        score=score_context(profile(),c,now=datetime(2026,10,10,20,tzinfo=timezone.utc))
        self.assertEqual(score['unavailable_reason'],'reference_dates_or_windows_insufficient')

    def test_unknown_calendar_coverage_does_not_match_unknown_as_known(self):
        c=context();c['as_of']='2027-10-07T20:00:00Z';c['expires_at']='2027-10-07T20:01:00Z'
        c['latest_queue_received_at']=c['as_of'];c['latest_count_received_at']=c['as_of']
        for f in c['features']:f['available_at']=c['as_of']
        score=score_context(profile(),c,now=datetime(2027,10,7,20,tzinfo=timezone.utc))
        self.assertEqual(score['unavailable_reason'],'calendar_type_unknown')

    def test_unavailable_or_sparse_current_never_has_zero_percentile(self):
        for key,value in [('collector_running',False),('latest_queue_origin','saved_history'),
                ('latest_queue_received_at',stamp(-100))]:
            c=context();c[key]=value
            score=score_context(profile(),c,now=BASE)
            self.assertIsNone(score['rank_lower_ppm'])
        c=context(duration=20000);enriched,score=enrich_context(profile(),c,now=BASE)
        self.assertTrue(all(f['value'] is None for f in enriched['features'] if '_turnover_' in f['feature_id']))

    def test_invalid_clock_scope_digest_bounds_and_overlap_rejected(self):
        mutations=[lambda p:p.update(created_at=stamp(1)),lambda p:p.update(window_seconds=True),
            lambda p:p.update(minimum_coverage_ppm=0),lambda p:p['samples'].append(p['samples'][-1]),
            lambda p:p['samples'][0].__setitem__(2,120001)]
        for mutate in mutations:
            p=profile();mutate(p);p['profile_sha256']=_digest(p)
            with self.assertRaises(TrendProfileError):validate_profile(p,now=BASE)
        p=profile();p['samples'][0][3]+=1
        with self.assertRaises(TrendProfileError):validate_profile(p,now=BASE)
        for key,value in [('store_id','900002'),('window_seconds',600),('as_of',stamp(-2))]:
            c=context();c[key]=value
            with self.assertRaises(TrendProfileError):score_context(profile(),c,now=BASE)

    def test_reference_window_overlapping_current_is_excluded(self):
        p=profile();p['samples'].append([stamp(),3,90000,999,999]);p['history_cutoff']=stamp();p['created_at']=stamp()
        p['profile_sha256']=_digest(p)
        score=score_context(p,context(6),now=BASE)
        self.assertEqual((score['reference_windows'],score['rank_lower_ppm']),(8,1000000))

    def test_enriched_public_contract_exposes_ranks_not_reference_file(self):
        c,score=enrich_context(profile(),context(6),now=BASE)
        from test_fusion import plan
        p=plan();p['public_context']=c
        request=public_request(p,now=BASE);text=json.dumps(request)
        self.assertIn('ordinary_turnover_rank_lower_ppm',text)
        self.assertNotIn('profile_sha256',text);self.assertNotIn('samples',text)
        self.assertNotIn('history_cutoff',text);self.assertEqual(len(c['features']),7)

    def test_public_rank_bundle_bounds_and_arithmetic_are_validated(self):
        from sushiwait.fusion import FusionError
        c,_=enrich_context(profile(),context(6),now=BASE)
        for change in ('missing','lower_greater','days_greater','rank_overflow','duration_overflow'):
            value=deepcopy(c);by_id={f['feature_id']:f for f in value['features']}
            if change=='missing':value['features'].pop()
            elif change=='lower_greater':by_id['ordinary_turnover_rank_upper_ppm']['value']=0
            elif change=='days_greater':by_id['ordinary_turnover_reference_days']['value']=9
            elif change=='rank_overflow':by_id['ordinary_turnover_rank_upper_ppm']['value']=1000001
            else:by_id['ordinary_observed_milliseconds']['value']=120001
            with self.assertRaises(FusionError):
                from sushiwait.trendprofiles import _context
                _context(value,BASE)

    def test_private_new_file_cli_and_no_overwrite_or_socket(self):
        self.write(range(-1200,-29,30));dest=self.root/'profile.json';out=io.StringIO()
        with patch('sushiwait.trendprofiles._now',return_value=BASE),patch('socket.socket',side_effect=AssertionError('network')),contextlib.redirect_stdout(out):
            code=main(['trend-profile','--remote-db',str(self.db),'--store-id','900001','--as-of',stamp(-1),
                '--window-seconds','120','--max-gap-seconds','60','--output',str(dest)])
        self.assertEqual(code,0);self.assertNotIn(str(self.root),out.getvalue())
        self.assertEqual(dest.stat().st_mode&0o777,0o600)
        before=dest.read_bytes()
        with patch('sushiwait.trendprofiles._now',return_value=BASE),self.assertRaises(ValueError):write_profile(read_profile(dest,now=BASE),dest)
        self.assertEqual(before,dest.read_bytes())

    def test_actual_score_cli_saves_enriched_context(self):
        p=self.root/'profile.json';p.write_text(json.dumps(profile()));p.chmod(0o600)
        c=self.root/'context.json';c.write_text(json.dumps(context(6)));c.chmod(0o600)
        output=self.root/'score.json';out=io.StringIO()
        with patch('sushiwait.trendprofiles._now',return_value=BASE),patch('socket.socket',side_effect=AssertionError('network')),contextlib.redirect_stdout(out):
            code=main(['trend-score','--profile-file',str(p),'--context-file',str(c),'--output',str(output)])
        self.assertEqual(code,0);self.assertNotIn(str(self.root),out.getvalue())
        saved=json.loads(output.read_bytes());self.assertEqual(saved['trend_score']['rank_lower_ppm'],1000000)
        self.assertEqual(output.stat().st_mode&0o777,0o600)

    def test_file_permissions_and_oversize_rejected(self):
        p=self.root/'profile.json';p.write_text(json.dumps(profile()));p.chmod(0o644)
        with self.assertRaises(TrendProfileError):read_profile(p,now=BASE)
        p.chmod(0o600);p.write_text(' '*16385)
        with self.assertRaises(TrendProfileError):read_profile(p,now=BASE)

    def test_fractional_millisecond_projection_coverage_is_not_window_size(self):
        view=LiveRemoteView(['900001'],stale_after_seconds=60)
        for second in (-60,-30,-.001):view.publish(record(second,['1','2']))
        now=BASE+timedelta(seconds=1)
        history=view.monitor_history('900001',now=now,service_state='running',worker_alive=True)
        c=context_from_history(history,queue_type='ordinary',now=now,window_seconds=120)
        values={f['feature_id']:f['value'] for f in c['features']}
        self.assertEqual(values['ordinary_observed_milliseconds'],59999)
        self.assertEqual(values['ordinary_removed_labels'],0)

    def test_profile_enrichment_runs_inside_real_coordinator_observation(self):
        import test_tracker_loop as tracking_helpers
        from sushiwait.trackerloop import TrackingCoordinator
        from sushiwait.tracking import TrackingSession
        helper=tracking_helpers.TrackerLoopTests(methodName='runTest');helper.setUp()
        self.addCleanup(helper.doCleanups)
        p=profile();p['created_at']=(helper.now-timedelta(seconds=1)).isoformat()
        p['history_cutoff']=(helper.now-timedelta(seconds=120)).isoformat();p['profile_sha256']=_digest(p)
        directory=helper.root/'reference';directory.mkdir(mode=0o700)
        file=directory/'profile.json';file.write_text(json.dumps(p));file.chmod(0o600)
        coordinator=TrackingCoordinator([helper.state],stores=['900001'],reader=helper.reader,
            trend_profile_files=[file],clock=lambda:helper.now)
        self.addCleanup(coordinator.close)
        coordinator.cycle()
        helper.finish(coordinator)
        with TrackingSession(helper.state) as session:
            _,receipt,_=session.load(now=helper.now)
        ids={f['feature_id'] for f in receipt['observation']['public_context']['features']}
        self.assertIn('ordinary_turnover_rank_lower_ppm',ids)
        self.assertEqual(receipt['observation']['public_context']['window_seconds'],120)
        self.assertEqual(len(helper.reads),1)

    def test_duplicate_or_wrong_store_reference_cannot_start_tracker(self):
        import test_tracker_loop as tracking_helpers
        from sushiwait.trackerloop import TrackingCoordinator,TrackerLoopError
        helper=tracking_helpers.TrackerLoopTests(methodName='runTest');helper.setUp();self.addCleanup(helper.doCleanups)
        paths=[]
        directory=helper.root/'reference';directory.mkdir(mode=0o700)
        p=profile();p['created_at']=(helper.now-timedelta(seconds=1)).isoformat()
        p['history_cutoff']=(helper.now-timedelta(seconds=120)).isoformat();p['profile_sha256']=_digest(p)
        for index in range(2):
            file=directory/f'profile-{index}.json';file.write_text(json.dumps(p));file.chmod(0o600);paths.append(file)
        with self.assertRaises(TrackerLoopError):
            TrackingCoordinator([helper.state],stores=['900001'],reader=helper.reader,
                trend_profile_files=paths,clock=lambda:helper.now)
