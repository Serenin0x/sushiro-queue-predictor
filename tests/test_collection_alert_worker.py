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
