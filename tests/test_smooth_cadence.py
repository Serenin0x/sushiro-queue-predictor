"""Phase continuity and actual persistent CLI scheduling; no upstream network."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import contextlib
import io
import unittest
from unittest.mock import patch

from sushiwait import cadence
from sushiwait.cli import main, _collection_period
from sushiwait.tasks import task_status
import test_collection_tasks as task_helpers


def at(hour, minute=0, second=0):
    return datetime(2026, 10, 13, hour, minute, second, tzinfo=cadence.CHINA)


class SmoothCadenceTests(unittest.TestCase):
    def test_constant_plateaus_and_timezone_equivalence(self):
        for clock, expected in ((at(10,30),600),(at(15),600),(at(21),600),
                                 (at(12),180),(at(19),180)):
            self.assertAlmostEqual(cadence.interval_at(clock),expected,places=6)
            self.assertEqual(cadence.interval_at(clock),cadence.interval_at(clock.astimezone(timezone.utc)))

    def test_shoulders_are_monotone_bounded_and_not_a_discrete_switch(self):
        entering=[cadence.interval_at(at(11)+timedelta(minutes=m)) for m in range(31)]
        leaving=[cadence.interval_at(at(13,30)+timedelta(minutes=m)) for m in range(31)]
        self.assertEqual(len(set(round(p,5) for p in entering)),31)
        self.assertTrue(all(180-1e-9<=p<=600+1e-9 for p in entering+leaving))
        self.assertTrue(all(a>b for a,b in zip(entering,entering[1:])))
        self.assertTrue(all(a<b for a,b in zip(leaving,leaving[1:])))

    def test_join_values_and_derivatives_are_continuous(self):
        for clock in (at(11),at(11,30),at(13,30),at(14),at(17,30),at(18),at(20),at(20,30)):
            h=timedelta(milliseconds=1)
            left,center,right=map(cadence.interval_at,(clock-h,clock,clock+h))
            self.assertLess(abs(left-right),0.00001)
            self.assertLess(abs((center-left)/.001-(right-center)/.001),0.0001)

    def test_next_time_integrates_one_phase_even_across_shoulders(self):
        for clock in (at(10,59),at(11,8),at(11,29),at(13,29),at(13,59),at(17,44),at(20,29),at(23,58)):
            end=cadence.next_sample_at(clock)
            self.assertTrue(180 <= (end-clock).total_seconds() <= 600)
            self.assertAlmostEqual(cadence.phase_between(clock,end),1,places=7)

    def test_integrated_period_changes_smoothly_across_successive_rounds(self):
        clock=at(11); periods=[]
        while clock<at(11,30):
            end=cadence.next_sample_at(clock);periods.append((end-clock).total_seconds());clock=end
        self.assertTrue(all(a>b for a,b in zip(periods,periods[1:])))
        self.assertGreater(periods[0],180);self.assertLess(periods[0],600)

    def test_period_and_phase_across_midnight(self):
        clock=at(23,58);end=cadence.next_sample_at(clock)
        self.assertEqual(end.date(),clock.date()+timedelta(days=1))
        self.assertAlmostEqual((end-clock).total_seconds(),600,places=5)
        self.assertAlmostEqual(cadence.phase_between(clock,end),1,places=7)

    def test_naive_unknown_backward_and_unbounded_spans_rejected(self):
        with self.assertRaises(ValueError):cadence.interval_at(datetime(2026,10,13))
        with self.assertRaises(ValueError):cadence.interval_at(at(12),'unapproved-policy')
        for end in (at(11),at(12)+timedelta(days=8)):
            with self.assertRaises(ValueError):cadence.phase_between(at(12),end)
        with self.assertRaises(ValueError):cadence.planned_rounds(at(11),at(11))

    def test_fixed_default_is_unchanged_and_new_policy_has_bounds(self):
        self.assertEqual(_collection_period(SimpleNamespace(interval=600)),600)
        self.assertAlmostEqual(_collection_period(SimpleNamespace(interval=600,cadence=cadence.POLICY),at(12)),180,places=5)
        desc=cadence.description(cadence.POLICY);desc['peaks'].clear()
        self.assertEqual(len(cadence.description(cadence.POLICY)['peaks']),2)

    def test_invalid_cli_options_do_not_read_credentials_or_query(self):
        for options in ([],['--task-file','/synthetic/task','--credentials-file','/synthetic/context','--interval','300']):
            with patch('sushiwait.cli.client_for') as client,patch('sushiwait.cli._persistent_collect') as collect,contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['collect','--store-id','900001','--cadence',cadence.POLICY]+options),2)
                client.assert_not_called();collect.assert_not_called()

    def helper(self):
        h=task_helpers.CollectionTaskTests(methodName='runTest');h.setUp();self.addCleanup(h.doCleanups)
        return h

    def test_actual_persistent_cli_samples_at_three_minutes_and_reports_policy(self):
        h=self.helper()
        code,_=h.run_cli(h.args(samples=3,interval=600)+['--cadence',cadence.POLICY])
        self.assertEqual(code,0)
        self.assertEqual([round(t,4) for _,t in h.requests],[0,180,360])
        s=task_status(h.task)
        self.assertEqual(s['completed_slots'],3)
        self.assertEqual(s['cadence_policy']['peak_seconds'],180)
        self.assertEqual(h.raw()['config']['cadence'],cadence.POLICY)

    def test_actual_transition_uses_progressive_intervals(self):
        h=self.helper();base=at(11)
        h.clock.now=lambda:base+timedelta(seconds=h.clock.value)
        code,_=h.run_cli(h.args(samples=5,interval=600)+['--cadence',cadence.POLICY])
        times=[t for _,t in h.requests];intervals=[b-a for a,b in zip(times,times[1:])]
        self.assertEqual(code,0);self.assertEqual(len(intervals),4)
        self.assertTrue(all(180<=d<600 for d in intervals))
        self.assertTrue(all(a>b for a,b in zip(intervals,intervals[1:])))

    def test_resume_preserves_slots_unknown_outcome_and_waits_no_catchup(self):
        h=self.helper();h.before_request=lambda _:(_ for _ in ()).throw(KeyboardInterrupt())
        args=h.args(samples=3,interval=600)+['--cadence',cadence.POLICY]
        code,_=h.run_cli(args);self.assertEqual(code,130)
        h.before_request=None;h.requests.clear();h.clock.value=10
        code,_=h.run_cli(args+['--resume-task'])
        self.assertEqual(code,1)
        self.assertEqual([round(t,4) for _,t in h.requests],[190,370])
        self.assertEqual(task_status(h.task)['uncertain_slots'],1)
        self.assertEqual(task_status(h.task)['completed_slots'],3)

    def test_completed_task_never_requeries_and_policy_cannot_be_removed_on_resume(self):
        h=self.helper();args=h.args(samples=2,interval=600)+['--cadence',cadence.POLICY]
        self.assertEqual(h.run_cli(args)[0],0);h.requests.clear()
        self.assertEqual(h.run_cli(args+['--resume-task'])[0],0);self.assertEqual(h.requests,[])
        self.assertEqual(h.run_cli(h.args(samples=2,interval=600,resume=True))[0],1)
        self.assertEqual(h.requests,[])

    def test_full_day_demand_and_large_adaptive_task_bound_are_explicit(self):
        self.assertEqual(cadence.planned_rounds(at(11),at(22)),136)
        h=self.helper()
        args=h.args(samples=136,interval=600)+['--cadence',cadence.POLICY]
        with patch('sushiwait.cli._persistent_collect',return_value=0) as collect,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(args),0);collect.assert_called_once()
        with patch('sushiwait.cli._persistent_collect') as collect,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(h.args(samples=136,interval=600)),2);collect.assert_not_called()
