"""A faster cadence must carry forward spent calls, crashes and resume targets."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sushiwait.packets import _write_packet, encoded
from tests.test_official_cloud_controller import cloud, plan


def history(interval, *, completed, pending=False, rounds=None):
    rounds = (44 if interval == 900 else 66) if rounds is None else rounds
    return {'store_ids':['3014','3004','2009'],'api_profile':'miniapp_gateway',
        'interval_seconds':interval,'target_rounds':rounds,'target_slots':rounds*3,
        'completed_slots':completed,'pending_attempt':pending}


class OfficialCloudPollingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.stores = plan()['store_ids']
        self.start = cloud.validate_plan(plan())[0][0]

    def put(self, name):
        (self.root/name).touch(mode=0o600)

    def test_only_explicit_ten_minute_policy_keeps_original_global_limit(self):
        self.assertEqual(cloud.polling_policy(self.root)['interval_seconds'],900)
        path = self.root/'polling-policy.json'
        value = {'schema_version':1,'interval_seconds':600,'ordinary_query_limit':396}
        _write_packet(encoded(value),path)
        self.assertEqual(cloud.polling_policy(self.root),
            {'interval_seconds':600,'ordinary_query_limit':396})
        for change in ({'interval_seconds':300},{'ordinary_query_limit':594},
                       {'schema_version':True},{'deadline':'2026-10-15'}):
            with self.subTest(change=change):
                path.unlink()
                _write_packet(encoded({**value,**change}),path)
                with self.assertRaises(cloud.TrialError):cloud.polling_policy(self.root)

    def test_switch_counts_completed_unknown_and_pending_slots_of_old_phase(self):
        # completed_slots includes successful, failed and uncertain slots.
        self.put('collection-2026-10-12.json')
        self.put('collection-2026-10-13-600.json')
        statuses = [history(900,completed=132),history(600,completed=197,pending=True)]
        with patch.object(cloud,'task_status',side_effect=statuses):
            used,_ = cloud.query_usage(self.root,self.stores)
        self.assertEqual(used,330)

    def test_new_phase_can_spend_only_the_remaining_global_budget(self):
        self.put('collection-2026-10-12.json')
        self.put('collection-2026-10-13-600.json')
        statuses = [history(900,completed=132),history(600,completed=197,pending=True)]
        start = cloud.validate_plan(plan())[2][0]
        with patch.object(cloud,'task_status',side_effect=statuses):
            task,rounds,resume,previous = cloud.window_budget(self.root,self.stores,start,600,396)
        self.assertEqual((rounds,resume,previous),(22,False,None))
        self.assertEqual(task.name,'collection-2026-10-14-600.json')

    def test_restart_preserves_existing_target_instead_of_granting_more_calls(self):
        self.put('collection-2026-10-12.json')
        self.put('collection-2026-10-12-600.json')
        statuses = [history(900,completed=132),history(600,completed=99,rounds=44)]
        with patch.object(cloud,'task_status',side_effect=statuses):
            _,rounds,resume,_ = cloud.window_budget(self.root,self.stores,self.start,600,396)
        self.assertEqual((rounds,resume),(44,True))
        args=cloud.collect_arguments(self.root,self.stores,rounds,Path('task'),True,
            self.start,cloud.validate_plan(plan())[0][1],interval=600)
        self.assertEqual(args[args.index('--interval')+1],'600')
        self.assertEqual(args[args.index('--samples')+1],'44')
        self.assertIn('--resume-task',args)

    def test_budget_exhaustion_allocates_no_new_task(self):
        for day in (12,13,14):self.put(f'collection-2026-10-{day}.json')
        with patch.object(cloud,'task_status',side_effect=[history(900,completed=132)]*3):
            _,rounds,resume,_ = cloud.window_budget(self.root,self.stores,self.start,600,396)
        self.assertEqual((rounds,resume),(0,False))

    def test_incompatible_saved_history_cannot_be_ignored_to_reset_budget(self):
        self.put('collection-2026-10-12.json')
        changed=history(900,completed=3)
        changed['store_ids']=['9999']
        with patch.object(cloud,'task_status',return_value=changed):
            with self.assertRaisesRegex(cloud.TrialError,'cloud_task_budget_invalid'):
                cloud.window_budget(self.root,self.stores,self.start,600,396)


class SmoothCloudBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.root.chmod(0o700)
        self.stores=plan()['store_ids'];self.start=cloud.validate_plan(plan())[1][0]

    def policy(self, limit=396):
        _write_packet(encoded({'schema_version':2,'interval_seconds':600,
            'ordinary_query_limit':limit,'cadence':'meal-peaks-v1'}),self.root/'polling-policy.json')

    def test_explicit_smooth_policy_accepted_but_not_implicit_budget_increase(self):
        self.policy();v=cloud.polling_policy(self.root)
        self.assertEqual(v['cadence'],'meal-peaks-v1');self.assertEqual(v['ordinary_query_limit'],396)

    def test_each_explicit_budget_has_fixed_upper_bound(self):
        self.policy(1008);self.assertEqual(cloud.polling_policy(self.root)['ordinary_query_limit'],1008)
        for limit in (1009,0,True):
            p=self.root/'polling-policy.json';p.unlink();self.policy(limit)
            with self.assertRaises(cloud.TrialError):cloud.polling_policy(self.root)

    def test_old_phase_unknown_pending_and_new_phase_all_count_together(self):
        self.policy()
        for name in ('collection-2026-10-12.json','collection-2026-10-13-600.json','collection-2026-10-13-smooth.json'):
            (self.root/name).touch(mode=0o600)
        values=[history(900,completed=141),history(600,completed=18),
            {**history(600,completed=6,pending=True,rounds=79),
             'cadence_policy':{'id':'meal-peaks-v1'}}]
        with patch.object(cloud,'task_status',side_effect=values):
            used,_=cloud.query_usage(self.root,self.stores)
        self.assertEqual(used,166)

    def test_new_smooth_phase_carries_old_spent_calls_and_cli_policy(self):
        self.policy()
        (self.root/'collection-2026-10-12.json').touch(mode=0o600)
        with patch.object(cloud,'task_status',return_value=history(900,completed=141)):
            task,rounds,resume,_=cloud.window_budget(self.root,self.stores,self.start,600,396,cadence='meal-peaks-v1')
        self.assertEqual(rounds,85);self.assertFalse(resume);self.assertTrue(task.name.endswith('-smooth.json'))
        args=cloud.collect_arguments(self.root,self.stores,rounds,task,False,self.start,
            self.start.replace(hour=14),interval=600,cadence='meal-peaks-v1')
        self.assertEqual(args[args.index('--cadence')+1],'meal-peaks-v1')

    def test_full_day_smooth_phase_demand_respects_explicit_new_budget(self):
        self.policy(1008)
        (self.root/'collection-2026-10-12.json').touch(mode=0o600)
        with patch.object(cloud,'task_status',return_value=history(900,completed=141)):
            _,rounds,_,_=cloud.window_budget(self.root,self.stores,self.start,600,1008,cadence='meal-peaks-v1')
        self.assertEqual(rounds,136)

    def test_same_day_phase_switch_waits_a_full_period_without_origin_call(self):
        from datetime import timedelta
        now=self.start+timedelta(hours=1)
        task=self.root/'collection-2026-10-13-smooth.json'
        old=self.root/'collection-2026-10-13-600.json'
        for completed,pending in ((3,False),(0,True)):
            with patch.object(cloud,'query_usage',return_value=(3,{old:history(600,completed=completed,pending=pending)})):
                self.assertEqual(cloud.phase_not_before(self.root,self.stores,task,now,cadence='meal-peaks-v1'),now+timedelta(seconds=180))

    def test_old_day_or_fixed_mode_does_not_add_a_phase_wait(self):
        from datetime import timedelta
        now=self.start+timedelta(hours=1)
        task=self.root/'collection-2026-10-13-smooth.json'
        old=self.root/'collection-2026-10-12-600.json'
        with patch.object(cloud,'query_usage',return_value=(3,{old:history(600,completed=3)})):
            self.assertEqual(cloud.phase_not_before(self.root,self.stores,task,now,cadence='meal-peaks-v1'),now)
        with patch.object(cloud,'query_usage') as usage:
            self.assertEqual(cloud.phase_not_before(self.root,self.stores,task,now),now)
            usage.assert_not_called()
