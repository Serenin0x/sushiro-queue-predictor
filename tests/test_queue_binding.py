"""Different raw arrays must not cross ordinary and reservation meanings."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from sushiwait.fusion import context_from_history, validate_plan, FusionError
from sushiwait.queuebinding import POLICY, field_for_queue, require_bound_context
from sushiwait.remoteservice import LiveRemoteView
from sushiwait.tracking import (TrackingError, TrackingSession, create_session,
    normalize_observation, observe_session)
from sushiwait.trendprofiles import validate_profile, _digest, TrendProfileError
import test_fusion as fusion_fixture
import test_tracking as tracking_fixture
import test_trend_profiles as profile_fixture


class QueueBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.view = LiveRemoteView(['900001'], stale_after_seconds=120)
        self.now = tracking_fixture.BASE + timedelta(seconds=1)

    def record(self, second=0, *, ordinary=('093', '096', '098'),
               reservation=('7070',), aggregate=('7070', '080', '093')):
        row = tracking_fixture.record(second)
        row['queries']['groupqueues']['payload']['queues'].update(
            mixedQueue=list(ordinary), reservationQueue=list(reservation),
            storeQueue=list(aggregate), counterQueue=[], boothQueue=list(ordinary))
        return row

    def projections(self, ticket):
        view = self.view.snapshot('900001', now=self.now, service_state='running', worker_alive=True)
        history = self.view.monitor_history('900001', now=self.now, service_state='running', worker_alive=True)
        context = context_from_history(history, queue_type=ticket['queue_type'], now=self.now)
        return view, context

    def observed(self, *, queue_type='ordinary', number='093'):
        ticket = tracking_fixture.ticket(queue_type=queue_type, number=number)
        state = self.root / str(uuid4())
        state.mkdir(mode=0o700)
        create_session(ticket, directory=state, now=self.now)
        view, context = self.projections(ticket)
        observe_session(directory=state, view=view, context=context, as_of=context['as_of'], now=self.now)
        with TrackingSession(state) as session:
            _, receipt, _ = session.load(now=self.now)
        return state, receipt

    def test_ordinary_uses_raw_mixed_order_even_when_aggregate_starts_with_reservation(self):
        self.view.publish(self.record())
        _, receipt = self.observed()
        display = receipt['display']
        self.assertEqual(display['selected_display_numbers'], ['093', '096', '098'])
        self.assertTrue(display['own_number_in_selected_display'])
        self.assertEqual([x['arithmetic_difference'] for x in display['candidate_number_differences']], [0, -3, -5])
        self.assertFalse(display['called_verified'] or display['difference_is_front_tables'])

    def test_aggregate_only_number_is_not_ordinary_display_evidence(self):
        self.view.publish(self.record())
        _, receipt = self.observed(number='7070')
        self.assertEqual(receipt['display']['state'], 'not_locally_seen')
        self.assertFalse(receipt['display']['own_number_in_selected_display'])

    def test_reservation_stays_separate_and_is_not_ordinary_membership(self):
        self.view.publish(self.record())
        _, reservation = self.observed(queue_type='reservation', number='7070')
        _, absent = self.observed(queue_type='reservation', number='093')
        self.assertEqual(reservation['display']['selected_display_numbers'], ['7070'])
        self.assertTrue(reservation['display']['own_number_in_selected_display'])
        self.assertFalse(absent['display']['own_number_in_selected_display'])

    def test_aggregate_changes_do_not_create_ordinary_turnover(self):
        self.view.publish(self.record(aggregate=('083', '7070', '093')))
        self.view.publish(self.record(30, aggregate=('7070', '080', '093')))
        self.now += timedelta(seconds=30)
        _, context = self.projections(tracking_fixture.ticket())
        features = {f['feature_id']: f['value'] for f in context['features']}
        self.assertEqual(features['ordinary_removed_labels'], 0)
        self.assertEqual(features['ordinary_comparable_pairs'], 1)
        self.assertEqual(features['reservation_removed_labels'], 0)
        self.assertEqual(context['schema_version'], 2)
        self.assertEqual(context['queue_binding_policy'], POLICY)

    def test_legacy_math_context_is_not_accepted_for_new_private_tracking(self):
        self.view.publish(self.record())
        ticket = tracking_fixture.ticket()
        view, context = self.projections(ticket)
        legacy = deepcopy(context)
        legacy['schema_version'] = 1
        legacy.pop('queue_binding_policy')
        plan = fusion_fixture.plan()
        plan['public_context'] = legacy
        self.assertEqual(validate_plan(plan, now=self.now)['public_context']['schema_version'], 1)
        with self.assertRaises(TrackingError):
            normalize_observation(ticket, view, legacy, as_of=legacy['as_of'], now=self.now)

    def test_false_or_unknown_binding_is_rejected_before_use(self):
        for change in [{'queue_binding_policy': 'ordinary_store_legacy'}, {'schema_version': True}]:
            context = {**fusion_fixture.plan()['public_context'], **change}
            with self.assertRaises(ValueError):
                require_bound_context(context)
            plan = fusion_fixture.plan()
            plan['public_context'] = context
            with self.assertRaises(FusionError):
                validate_plan(plan, now=fusion_fixture.BASE)
        for kind in ['storeQueue', None, True, '']:
            with self.assertRaises(ValueError):
                field_for_queue(kind)

    def test_old_tracking_policy_is_not_silently_reinterpreted_or_rewritten(self):
        self.view.publish(self.record())
        state, receipt = self.observed()
        receipt['policy'] = 'private_manual_ticket_observation_publication_v1'
        path = state / 'observation-0001.json'
        path.write_text(json.dumps(receipt))
        before = path.read_bytes()
        with TrackingSession(state) as session, self.assertRaises(TrackingError):
            session.load(now=self.now)
        self.assertEqual(path.read_bytes(), before)

    def test_old_turnover_profile_is_not_relabelled_as_ordinary_mixed(self):
        profile = profile_fixture.profile()
        profile['policy'] = 'dated_nonoverlapping_display_turnover_ranks_v1'
        profile['profile_sha256'] = _digest(profile)
        with self.assertRaises(TrendProfileError):
            validate_profile(profile, now=profile_fixture.BASE)
