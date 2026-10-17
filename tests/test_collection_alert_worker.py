from datetime import datetime,timezone
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('checked_alert_worker',Path(__file__).resolve().parents[1]/'deploy/collection_alert_worker.py')
worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
NOW=datetime(2026,10,16,10,30,tzinfo=timezone.utc)
RUNNING={'state':'running','at':NOW.isoformat(),'failed_slots':0,'successful_slots':1,'last_progress_at':NOW.isoformat()}
class AlertWorkerTests(unittest.TestCase):
    def test_missing_or_invalid_sync_file_is_a_sync_incident(self):
        with patch.object(worker,'read_status',side_effect=[RUNNING,ValueError('invalid file')]):
            self.assertEqual(worker.current_condition(Path('/synthetic'),NOW),'public_sync_stopped')
    def test_collection_read_failure_remains_separate(self):
        with patch.object(worker,'read_status',side_effect=ValueError('invalid file')):
            self.assertEqual(worker.current_condition(Path('/synthetic'),NOW),'collection_status_unavailable')
    def test_two_fresh_statuses_are_healthy(self):
        with patch.object(worker,'read_status',side_effect=[RUNNING,RUNNING]):
            self.assertEqual(worker.current_condition(Path('/synthetic'),NOW),'healthy')

    def test_read_model_heartbeat_and_storage_are_not_silent(self):
        model={'at':NOW.isoformat(),'policy':'indexed_day_rollups_v1','state':'running'}
        for change,expected in [({},'healthy'),({'state':'needs_attention'},'public_sync_stopped'),({'at':'2000-01-01T00:00:00Z'},'public_sync_stopped'),({'storage_pressure':True},'storage_capacity_low')]:
            with patch.object(worker,'read_status',side_effect=[RUNNING,RUNNING,{**model,**change}]):
                self.assertEqual(worker.current_condition(Path('/synthetic'),NOW,Path('/synthetic/model-status.json')),expected)

    def test_failed_web_service_is_reported_without_querying_source(self):
        model={'at':NOW.isoformat(),'policy':'indexed_day_rollups_v1','state':'running'}
        with patch.object(worker,'read_status',side_effect=[RUNNING,RUNNING,model]):
            self.assertEqual(worker.current_condition(Path('/synthetic'),NOW,Path('/synthetic/status'),gateway_probe=lambda:False),'public_sync_stopped')
