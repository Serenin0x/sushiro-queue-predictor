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
