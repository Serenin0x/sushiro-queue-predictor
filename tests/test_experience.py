"""Explicit observations become private claims, never inferred call labels."""
from copy import deepcopy
from datetime import timedelta
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sushiwait.cli import main
from sushiwait.experience import ExperienceError, draft_experience, write_experience
from sushiwait.intake import OutcomeIntakeStore
from sushiwait.outcomes import candidate_targets
from sushiwait.tracking import create_session, end_session
import test_tracking as fixture


class ExperienceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.chmod(0o700)
        self.state = self.root/'tracking';self.state.mkdir(mode=0o700)
        self.output = self.root/'drafts';self.output.mkdir(mode=0o700)
        self.ticket = fixture.ticket(number='A7654321')
        create_session(self.ticket, directory=self.state, now=fixture.BASE)

    def draft(self, event='called', second=60, **options):
        return draft_experience(directory=self.state, event_type=event,
            now=fixture.BASE+timedelta(seconds=second), **options)

    def test_now_is_only_the_explicit_event_action_and_ticket_issue_is_preserved(self):
        before = (self.state/'ticket.json').read_bytes()
        with patch('socket.socket', side_effect=AssertionError('network')) as sock:
            episode = self.draft()
        self.assertEqual(episode['episode_id'], self.ticket['episode_id'])
        self.assertEqual(episode['events'][0]['event_time_lower'], '2026-10-06T04:25:00.000000Z')
        self.assertEqual(candidate_targets(episode)['called_wait'], {'lower_seconds':360.0,'upper_seconds':360.0})
        self.assertEqual(before, (self.state/'ticket.json').read_bytes())
        self.assertNotIn(self.ticket['number'], json.dumps(episode))
        self.assertFalse(candidate_targets(episode)['training_eligible'])
        self.assertEqual(sock.call_count, 0)

    def test_interval_claims_propagate_to_wait_bounds(self):
        episode = self.draft(lower=fixture.stamp(20), upper=fixture.stamp(40),
            issued_lower=fixture.stamp(-320), issued_upper=fixture.stamp(-280))
        self.assertEqual(candidate_targets(episode)['called_wait'], {'lower_seconds':300.0,'upper_seconds':360.0})
        self.assertTrue(all(e['verification_status']=='unverified' for e in episode['events']))

    def test_unknown_issue_stays_unknown_until_caller_provides_bounds(self):
        second = self.root/'unknown';second.mkdir(mode=0o700)
        create_session(fixture.ticket(issued_at=None), directory=second, now=fixture.BASE)
        with self.assertRaises(ExperienceError) as raised:
            draft_experience(directory=second,event_type='called',now=fixture.BASE)
        self.assertEqual(raised.exception.error_code,'experience_issued_time_required')
        episode = draft_experience(directory=second,event_type='called',now=fixture.BASE,
            issued_lower=fixture.stamp(-320),issued_upper=fixture.stamp(-280))
        self.assertEqual(candidate_targets(episode)['called_wait'],{'lower_seconds':280.0,'upper_seconds':320.0})

    def test_checked_in_boolean_and_terminal_call_are_not_auto_call_events(self):
        end_session(directory=self.state,status='called',declared_at=fixture.stamp(0),now=fixture.BASE)
        result = self.draft('observation_ended')
        self.assertEqual([e['event_type'] for e in result['events']],['issued','observation_ended'])
        self.assertTrue(candidate_targets(result)['right_censored_without_call'])
        self.assertIsNone(candidate_targets(result)['called_wait'])

    def test_later_revision_preserves_event_ids_and_uncertain_issue_interval(self):
        first = self.draft('checked_in',second=0,issued_lower=fixture.stamp(-320),issued_upper=fixture.stamp(-280))
        second = self.draft(previous=first)
        self.assertEqual(second['revision'],2)
        self.assertEqual(second['supersedes_revision'],1)
        self.assertEqual(first['events'],second['events'][:2])
        self.assertEqual(candidate_targets(second)['called_wait'],{'lower_seconds':340.0,'upper_seconds':380.0})
        third = self.draft('seated',second=100,previous=second)
        self.assertEqual(candidate_targets(third)['called_to_seated'],{'lower_seconds':40.0,'upper_seconds':40.0})

    def test_cross_ticket_previous_and_changed_issue_bounds_rejected(self):
        first = self.draft('checked_in',second=0)
        for key,value in [('store_id','900002'),('party_size',3),('data_origin','self_reported')]:
            changed = deepcopy(first);changed[key]=value
            with self.subTest(key=key), self.assertRaises(ExperienceError):self.draft(previous=changed)
        changed = deepcopy(first);changed['events'][0]['event_time_upper']=fixture.stamp(-301)
        changed['events'][0]['event_time_lower']=fixture.stamp(-301)
        with self.assertRaises(ExperienceError):self.draft(previous=changed)
        with self.assertRaises(ExperienceError):
            self.draft(previous=first,issued_lower=fixture.stamp(-310),issued_upper=fixture.stamp(-300))

    def test_impossible_and_partial_times_and_duplicate_or_terminal_events_rejected(self):
        for options in ({'lower':fixture.stamp(20)},
            {'lower':fixture.stamp(40),'upper':fixture.stamp(20)},
            {'lower':fixture.stamp(61),'upper':fixture.stamp(61)},
            {'issued_lower':fixture.stamp(-2),'issued_upper':fixture.stamp(-2)}):
            with self.subTest(options=options),self.assertRaises(ExperienceError):self.draft(**options)
        for prior,event in [(self.draft(),'called'),(self.draft('cancelled'),'called')]:
            with self.assertRaises(ExperienceError):self.draft(event,second=120,previous=prior)

    def test_receive_keeps_draft_time_separate_and_does_not_backfill_truth(self):
        first = self.draft('checked_in',second=0)
        second = self.draft(previous=first)
        db = self.root/'intake.sqlite3'
        for episode,received in [(first,100),(second,200)]:
            with OutcomeIntakeStore(db) as intake,patch('sushiwait.intake._clock',return_value=fixture.BASE+timedelta(seconds=received)):
                self.assertTrue(intake.append(episode)['committed'])
        with OutcomeIntakeStore(db,read_only=True) as intake,patch('sushiwait.intake._clock',return_value=fixture.BASE+timedelta(seconds=300)):
            early=intake.cohort(as_of=fixture.stamp(199),data_origin='synthetic',api_profile='miniapp_gateway')
            later=intake.cohort(as_of=fixture.stamp(200),data_origin='synthetic',api_profile='miniapp_gateway')
        self.assertEqual(early['unverified_called_wait_candidates'],0)
        self.assertEqual(later['unverified_called_wait_candidates'],1)
        self.assertEqual(later['verified_training_labels'],0)

    def test_private_output_no_overwrite_and_safe_cli_summary(self):
        destination=self.output/'draft.json'
        stdout=io.StringIO()
        with patch('sushiwait.experience._now',return_value=fixture.BASE),contextlib.redirect_stdout(stdout),patch('socket.socket',side_effect=AssertionError('network')) as sock:
            self.assertEqual(main(['ticket-outcome-draft','--state-dir',str(self.state),
                '--event-type','called','--output',str(destination)]),0)
        safe=json.loads(stdout.getvalue());self.assertFalse(safe['intake_received'] or safe['review_accepted'])
        self.assertNotIn(self.ticket['number'],stdout.getvalue());self.assertNotIn(str(self.state),stdout.getvalue())
        self.assertEqual(destination.stat().st_mode&0o777,0o600)
        before=destination.read_bytes()
        with self.assertRaises(ExperienceError) as error:
            write_experience(destination=destination,directory=self.state,event_type='called',now=fixture.BASE)
        self.assertEqual(error.exception.error_code,'experience_output_exists')
        self.assertEqual(destination.read_bytes(),before);self.assertEqual(sock.call_count,0)

    def test_cli_previous_revision_and_unknown_issue_error_are_safe(self):
        prior=self.output/'first.json';write_experience(destination=prior,directory=self.state,
            event_type='checked_in',now=fixture.BASE)
        result=self.output/'second.json';stdout=io.StringIO()
        with patch('sushiwait.experience._now',return_value=fixture.BASE+timedelta(seconds=60)),contextlib.redirect_stdout(stdout):
            self.assertEqual(main(['ticket-outcome-draft','--state-dir',str(self.state),'--event-type','called',
                '--previous-file',str(prior),'--output',str(result)]),0)
        self.assertEqual(json.loads(result.read_bytes())['revision'],2)
        with patch('sushiwait.experience._now',return_value=fixture.BASE),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['ticket-outcome-draft','--state-dir',str(self.state),'--event-type','called',
                '--event-lower',fixture.stamp(0),'--output',str(self.output/'invalid.json')]),1)
        self.assertFalse((self.output/'invalid.json').exists())

    def test_self_reported_remains_unverified_and_draft_cannot_pollute_session(self):
        state=self.root/'self-reported';state.mkdir(mode=0o700)
        create_session(fixture.ticket(data_origin='self_reported',checked_in=True),directory=state,now=fixture.BASE)
        episode=draft_experience(directory=state,event_type='observation_ended',now=fixture.BASE)
        self.assertEqual(episode['data_origin'],'self_reported')
        self.assertEqual([e['event_type'] for e in episode['events']],['issued','observation_ended'])
        self.assertTrue(all(e['evidence_kind']=='self_observation' and e['verification_status']=='unverified' for e in episode['events']))
        with self.assertRaises(ExperienceError):
            write_experience(destination=state/'draft.json',directory=state,event_type='called',now=fixture.BASE)
        self.assertEqual([p.name for p in state.iterdir()],['ticket.json'])

    def test_output_symlink_loop_and_post_publish_durability_failure_are_safe(self):
        loop=self.root/'loop';loop.symlink_to('loop')
        with self.assertRaises(ExperienceError):
            write_experience(destination=loop/'draft.json',directory=self.state,event_type='called',now=fixture.BASE)
        with patch('sushiwait.experience._write_packet',return_value={'committed':True,'durability_confirmed':False}):
            result=write_experience(destination=self.output/'uncertain.json',directory=self.state,event_type='called',now=fixture.BASE)
        self.assertTrue(result['committed']);self.assertFalse(result['durability_confirmed'])
        self.assertFalse(result['intake_received'] or result['review_accepted'])
