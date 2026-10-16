"""Natural-clock public delivery races never weaken timestamp validation."""
from datetime import datetime,timedelta
from pathlib import Path
import tempfile,unittest
from unittest.mock import Mock,patch
from sushiwait.packets import PacketError,encoded
from test_official_daily_service import daily,config
import test_official_cloud_campaign_sync as campaign_fixture

class DailyProjectionRaceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.root.chmod(0o700)
        self.now=datetime.fromisoformat('2026-10-16T15:00:00+08:00')
        self.config=config();self.plan=daily.make_plan(self.config,self.now)
        self.day=self.root/self.plan['plan_id'];self.day.mkdir(mode=0o700)
        (self.day/'campaign-plan.json').write_bytes(encoded(self.plan));(self.day/'campaign-plan.json').chmod(0o600)
        (self.day/'observations.sqlite').touch(mode=0o600)
    def run_projection(self, errors, loops):
        first=Mock(after=0);first.step.side_effect=errors
        other=Mock(after=0);other.step.return_value=None
        stopped=Mock();stopped.is_set.side_effect=[False]*loops+[True]
        ticks=[self.now+timedelta(milliseconds=i) for i in range(loops*3)]
        clock=Mock();clock.now.side_effect=ticks
        with patch.object(daily,'make_plan',return_value=self.plan),patch.object(daily,'datetime',clock),patch.object(daily.projection,'Projection',side_effect=[first,other]),patch.object(daily,'publish_status') as status:
            daily.run_projection(self.root,self.config,stopped)
        return first,other,status,ticks
    def test_one_group_future_row_defers_without_stopping_other_groups(self):
        first,other,status,ticks=self.run_projection([PacketError('packet_future_record')],1)
        first.step.assert_called_once_with(ticks[1]);other.step.assert_called_once_with(ticks[2])
        self.assertEqual(status.call_args.args[2]['state'],'running')
        self.assertEqual(status.call_args.args[2]['deferred_groups'],['primary'])
        self.assertEqual(first.after,0)
    def test_persistent_clock_anomaly_alerts_then_automatically_recovers(self):
        first,other,status,_=self.run_projection([PacketError('packet_future_record')]*3+[None],4)
        self.assertEqual([c.args[2]['state'] for c in status.call_args_list],['running','running','needs_attention','running'])
        self.assertEqual(other.step.call_count,4)
        self.assertEqual(status.call_args.args[2]['deferred_groups'],[])
    def test_other_integrity_errors_are_not_swallowed(self):
        with self.assertRaises(PacketError):
            self.run_projection([PacketError('packet_database_changed')],1)
    def test_actual_future_row_is_not_exported_or_cursor_advanced(self):
        fixture=campaign_fixture.CampaignProjectionTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        try:
            with fixture.projection() as projection:
                with self.assertRaisesRegex(PacketError,'packet_future_record'):
                    projection.step(campaign_fixture.NOW.replace(hour=3,minute=5))
                self.assertEqual(projection.after,0)
                self.assertEqual(len(list(projection.outbox.glob('confirmed-*.json'))),0)
                result=projection.step(campaign_fixture.NOW)
                self.assertEqual(result['inserted_records'],2)
        finally:
            fixture.tearDown()
