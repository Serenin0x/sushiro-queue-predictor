"""Bounded private tracking of writer projections, asynchronous computations.

The only default network reader is a fixed IPv4 loopback GET. Collection,
account operations and notifications remain separate. Paid advice is opt-in.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import http.client
import math
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

from .deepseek import run_deepseek
from .fusion import context_from_history, public_request
from .intake import OutcomeIntakeStore
from .monitoring import polling_policy
from .outcomes import _json, _time, _utc, _uuid
from .planupdates import read_update, publish_document
from .remote import SOURCE, _id
from .reviews import OutcomeReviewStore
from .tracking import (TrackingSession, TrackingError, normalize_observation, observe_session,
    prepare_prediction, calculate_prediction, publish_prediction, _hash, _integer)

MAX_SESSIONS = 16
MAX_FRAME_BYTES = 2 * 1024 * 1024


class TrackerLoopError(ValueError):
    def __init__(self, code):
        self.error_code = code
        super().__init__(code)


def _now():
    return datetime.now(timezone.utc)


class RemoteProjectionReader:
    """No environment proxies, redirects, cookies, headers or arbitrary hosts."""
    def __init__(self, url):
        try:
            parsed = urlsplit(url)
            if (type(url) is not str or parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
                    or parsed.username is not None or parsed.password is not None
                    or parsed.port is None or not 1 <= parsed.port <= 65535
                    or parsed.path not in ('', '/') or parsed.query or parsed.fragment
                    or parsed.netloc != f'127.0.0.1:{parsed.port}'):
                raise ValueError
            self.port = parsed.port
        except Exception:
            raise TrackerLoopError('tracker_loop_invalid_local_url') from None

    def __call__(self, store):
        _id(store)
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            connection.request('GET', f'/api/v1/stores/{store}/tracking-projection')
            response = connection.getresponse()
            if (response.status != 200 or response.getheader('Content-Type', '').split(';')[0]
                    != 'application/json'):
                raise ValueError
            body = response.read(MAX_FRAME_BYTES+1)
            if len(body) > MAX_FRAME_BYTES:
                raise ValueError
            return _json(body)
        except Exception:
            raise TrackerLoopError('tracker_loop_projection_unavailable') from None
        finally:
            connection.close()


def _signature(observation):
    """Changing read time is not changing source content or a new source GET."""
    context = observation['public_context']
    return _hash({key: observation[key] for key in ('queue_view_state', 'queue_received_at',
        'current_display_evidence', 'queues', 'retained_display_available')} | {
        'context': {key: context[key] for key in ('observation_revision', 'latest_queue_received_at',
            'latest_count_received_at', 'latest_queue_origin', 'collector_running',
            'window_seconds', 'max_local_age_seconds')},
        'features': [(f['feature_id'], f['value']) for f in context['features']]})


def _interval(ticket, latest, prediction, now, base):
    interval = base
    if ticket['desired_arrival_at'] is not None:
        interval = polling_policy(desired_arrival_at=ticket['desired_arrival_at'],
            call_offset_minutes=ticket['call_offset_minutes'], as_of=_utc(now),
            base_interval=base)['requested_interval_seconds']
    if latest is not None and latest['display']['first_locally_seen_at'] is not None:
        interval = min(interval, 30)
    if prediction is not None:
        if prediction.get('expected_receipt_sha256') != _hash(latest):
            raise TrackerLoopError('tracker_loop_invalid_prediction')
        context = latest['observation']['public_context']
        fusion = prediction.get('fusion')
        valid = now < _time(context['expires_at']) and (not fusion or not fusion['advice_accepted']
            or now < _time(fusion['advice']['expires_at']))
        requested = prediction.get('polling_request', {}).get('requested_interval_seconds')
        if not _integer(requested, 30, base):
            raise TrackerLoopError('tracker_loop_invalid_prediction')
        if valid:
            interval = min(interval, requested)
    return interval


class TrackingCoordinator:
    """One local projection read per store per cycle, at most four compute jobs.

    Private versions advance without waiting for a model. A late job must pass
    the tracking directory's current-version atomic publication. Dedicated
    plan output is a whole replacement set, owned by an explicit series ID.
    """
    def __init__(self, directories, *, stores, reader, base_interval=300, read_interval=5,
                 source_db=None, reviews_db=None, candidate_builder=None,
                 model_version='reviewed-history-intervals-v1', ai_blend_ppm=0,
                 plan_output=None, plan_series_id=None, allow_paid_request=False,
                 budget_file=None, key_file=None, transport=None, trend_profile_files=None,
                 trend_archive_files=None, clock=_now,
                 monotonic=time.monotonic):
        try:
            if (type(directories) is not list or not 1 <= len(directories) <= MAX_SESSIONS
                    or type(stores) is not list or not 1 <= len(stores) <= 3
                    or len(set(stores)) != len(stores) or not callable(reader)
                    or not _integer(base_interval, 60, 3600) or not _integer(read_interval, 5, 30)
                    or not _integer(ai_blend_ppm, 0, 1_000_000)
                    or type(allow_paid_request) is not bool
                    or bool(source_db) != bool(reviews_db)
                    or source_db and candidate_builder is not None
                    or (plan_output is None) != (plan_series_id is None)
                    or allow_paid_request and (budget_file is None or key_file is None)):
                raise ValueError
            for store in stores:
                _id(store)
            self.directories = [Path(os.path.abspath(p)) for p in directories]
            if len(set(self.directories)) != len(self.directories):
                raise ValueError
            profiles = [] if trend_profile_files is None else trend_profile_files
            archives = [] if trend_archive_files is None else trend_archive_files
            if (type(profiles) is not list or len(profiles) > 3
                    or type(archives) is not list or len(archives) > 3):
                raise ValueError
            external = [Path(os.path.abspath(p)) for p in
                [source_db, reviews_db, plan_output, budget_file, key_file]+profiles+archives if p is not None]
            if (len(set(external)) != len(external)
                    or any(p.is_relative_to(d) or d.is_relative_to(p.parent)
                           for p in external for d in self.directories)
                    or source_db and Path(source_db).absolute().parent == Path(reviews_db).absolute().parent
                    or plan_output and any(Path(plan_output).absolute().parent == p.parent
                                          for p in external if p != Path(plan_output).absolute())):
                raise ValueError
            if plan_series_id is not None:
                _uuid(plan_series_id)
            self.stores, self.reader, self.base, self.read_interval = stores[:], reader, base_interval, read_interval
            self.source_db, self.reviews_db, self.candidate_builder = source_db, reviews_db, candidate_builder
            self.model_version, self.ai_blend = model_version, ai_blend_ppm
            self.plan_output, self.series = plan_output, plan_series_id
            self.paid, self.budget, self.key, self.transport = allow_paid_request, budget_file, key_file, transport
            self.clock, self.monotonic = clock, monotonic
            from .trendprofiles import read_profile
            self.trend_profiles = {}
            for path in profiles:
                profile = read_profile(path, now=clock())
                if profile['store_id'] not in stores or profile['store_id'] in self.trend_profiles:
                    raise ValueError
                self.trend_profiles[profile['store_id']] = profile
            from .trendarchive import read_archive
            self.trend_archives = {}
            for path in archives:
                archive = read_archive(path, now=clock())
                store = archive['scope']['store_id']
                if store not in stores or store in self.trend_profiles or store in self.trend_archives:
                    raise ValueError
                self.trend_archives[store] = archive
            episodes = []
            for directory in self.directories:
                with TrackingSession(directory) as session:
                    ticket, _, _ = session.load(now=clock())
                if ticket['store_id'] not in stores:
                    raise ValueError
                episodes.append(ticket['episode_id'])
            if len(set(episodes)) != len(episodes):
                raise ValueError
        except Exception:
            raise TrackerLoopError('tracker_loop_invalid_configuration') from None
        self.pool = ThreadPoolExecutor(max_workers=min(4, len(self.directories)), thread_name_prefix='sushiwait-tracking')
        self.futures, self.closed, self.gate = {}, False, threading.RLock()
        self.cycle_lock = threading.Lock()
        self.counts = {'cycles': 0, 'local_projection_reads': 0, 'observations_committed': 0,
            'predictions_committed': 0, 'superseded_jobs': 0, 'failed_jobs': 0,
            'provider_request_attempts': 0, 'provider_network_calls': 0, 'plan_revisions_published': 0}

    def _states(self, now):
        with self.gate:
            return self._states_locked(now)

    def _states_locked(self, now):
        result = {}
        for directory in self.directories:
            with TrackingSession(directory) as session:
                ticket, latest, terminal = session.load(now=now)
                name = f"prediction-{latest['tracking_version']:04d}.json" if latest else None
                prediction = session._read(name) if name and name in os.listdir(session.fd) else None
                result[directory] = ticket, latest, terminal, prediction
        return result

    def _reap(self):
        for directory, future in list(self.futures.items()):
            if not future.done():
                continue
            del self.futures[directory]
            outcome = future.result()
            for key in ('predictions_committed', 'superseded_jobs', 'failed_jobs',
                        'provider_request_attempts', 'provider_network_calls'):
                self.counts[key] += outcome.get(key, 0)

    def _job(self, directory, version):
        outcome = {}
        try:
            with self.gate:
                preparation = prepare_prediction(directory=directory, version=version, now=self.clock())
            options = dict(base_interval=self.base, model_version=self.model_version, ai_blend_ppm=self.ai_blend)
            if self.source_db:
                with OutcomeIntakeStore(self.source_db, read_only=True) as source, \
                        OutcomeReviewStore(self.reviews_db, read_only=True) as reviews:
                    result = calculate_prediction(preparation, source=source, reviews=reviews,
                                                  now=self.clock(), **options)
            else:
                plan = self.candidate_builder(deepcopy(preparation)) if self.candidate_builder and \
                    preparation['ticket']['issued_at'] is not None and \
                    preparation['receipt']['observation']['current_display_evidence'] else None
                result = calculate_prediction(preparation, fusion_plan=plan, now=self.clock(), **options)
            plan = result['fusion_plan']
            if self.paid and plan is not None and len(plan['candidates']) > 1 and plan['ai_blend_ppm'] > 0:
                request = public_request(plan, now=self.clock())
                def current():
                    with self.gate:
                        if self.closed:
                            return None
                        with TrackingSession(directory) as session:
                            _, latest, terminal = session.load(now=self.clock())
                    return request if terminal is None and latest is not None and \
                        _hash(latest) == preparation['expected_receipt_sha256'] else None
                provider = run_deepseek(plan, budget_file=self.budget, key_file=self.key,
                    allow_paid_request=True, current_request=current, clock=self.clock,
                    monotonic=self.monotonic, transport=self.transport)
                outcome['provider_request_attempts'] = int(provider['provider_request_attempted'])
                outcome['provider_network_calls'] = int(provider['network_performed'])
                advice = provider['advice']
                if advice is not None:
                    advice = {**advice, 'schema_version': 1,
                        'public_input_sha256': request['public_input_sha256'],
                        'observation_revision': request['context']['observation_revision'],
                        'model_version': request['model_version']}
                updated = calculate_prediction(preparation, fusion_plan=plan, advice=advice,
                                               now=self.clock(), **options)
                if not updated['research_prediction_available']:
                    raise TrackingError('tracking_superseded')
                updated['history'] = result['history']
                updated['fusion']['fallback_reason'] = provider['fallback_reason']
                updated['provider_adapter_summary'] = {key: provider[key] for key in
                    ('provider', 'provider_model', 'provider_request_attempted', 'network_performed',
                     'advice_cache_hit', 'transport_basis', 'advice_origin_basis', 'billing_verified')}
                result = updated
            with self.gate:
                if self.closed:
                    raise TrackingError('tracking_superseded')
                receipt = publish_prediction(directory=directory, preparation=preparation,
                                             result=result, now=self.clock())
            outcome['predictions_committed'] = int(receipt['committed'])
            if not receipt['durability_confirmed']:
                outcome['failed_jobs'] = 1
        except Exception as error:
            if getattr(error, 'provider_request_attempted', False):
                outcome['provider_request_attempts'] = 1
                outcome['provider_network_calls'] = int(getattr(error, 'network_performed', False))
            code = getattr(error, 'error_code', '')
            if code in ('tracking_superseded', 'tracking_unavailable_version', 'tracking_terminal',
                        'tracking_deadline', 'fusion_superseded_input'):
                outcome['superseded_jobs'] = 1
            else:
                outcome['failed_jobs'] = 1
        return outcome

    def publish_demands(self):
        """A dedicated whole-set feed, not a merger into another operator's file.

        Private session locks stay held through plan publication. Thus a newer
        local observation or caller end cannot race an obsolete demand commit.
        The worker still must separately accept the revision; reads never GET.
        """
        if self.plan_output is None:
            return None
        with self.gate:
            if self.closed:
                raise TrackerLoopError('tracker_loop_closed')
            return self._publish_demands_locked()

    def _publish_demands_locked(self):
        now = self.clock()
        with ExitStack() as stack:
            plans = []
            for directory in sorted(self.directories):
                session = stack.enter_context(TrackingSession(directory))
                ticket, latest, terminal = session.load(now=now)
                name = f"prediction-{latest['tracking_version']:04d}.json" if latest else None
                prediction = session._read(name) if name and name in os.listdir(session.fd) else None
                active = terminal is None and now < _time(ticket['deadline_at']) and \
                    (latest is None or latest['tracking_version'] < ticket['max_updates'])
                interval = _interval(ticket, latest, prediction, now, self.base)
                plans.append({'store_id': ticket['store_id'], 'desired_arrival_at': ticket['desired_arrival_at'],
                    'call_offset_minutes': ticket['call_offset_minutes'] if ticket['desired_arrival_at'] else 0,
                    'plan_status': 'waiting' if active else 'ended', 'plan_expires_at': ticket['deadline_at'],
                    'polling_request': {'interval_seconds': interval,
                        'expires_at': _utc(now+timedelta(seconds=60))}})
            old = None
            if Path(self.plan_output).exists():
                old = read_update(self.plan_output, stores=self.stores, base_interval=self.base, now=now)
                if old['series_id'] != self.series:
                    raise TrackerLoopError('tracker_loop_plan_series_conflict')
                before, after = deepcopy(old['document']), {'schema_version': 1, 'plans': deepcopy(plans)}
                expires = []
                for document in (before, after):
                    for plan in document['plans']:
                        expires.append(_time(plan['polling_request']['expires_at']))
                        plan['polling_request']['expires_at'] = None
                if before == after and min(expires) > now+timedelta(seconds=30):
                    return {'committed': False, 'idempotent': True, 'revision': old['revision']}
            update = {'schema_version': 1, 'series_id': self.series,
                'revision': old['revision']+1 if old else 1, 'declared_at': _utc(now),
                'document': {'schema_version': 1, 'plans': plans}}
            result = publish_document(update, self.plan_output, stores=self.stores,
                                      base_interval=self.base, clock=self.clock)
            self.counts['plan_revisions_published'] += 1
            if not result['durability_confirmed']:
                raise TrackerLoopError('tracker_loop_plan_durability_unconfirmed')
            return result

    def cycle(self):
        if not self.cycle_lock.acquire(blocking=False):
            raise TrackerLoopError('tracker_loop_cycle_busy')
        try:
            if self.closed:
                raise TrackerLoopError('tracker_loop_closed')
            self._reap()
            if self.counts['failed_jobs']:
                raise TrackerLoopError('tracker_loop_calculation_failed')
            now = self.clock()
            states = self._states(now)
            active = {d: state for d, state in states.items() if state[2] is None and
                now < _time(state[0]['deadline_at']) and
                (state[1] is None or state[1]['tracking_version'] < state[0]['max_updates'])}
            frames = {}
            for store in sorted({state[0]['store_id'] for state in active.values()}):
                self.counts['local_projection_reads'] += 1
                frame = self.reader(store)
                try:
                    at = _time(frame['generated_at']); clock = self.clock()
                    valid = (type(frame) is dict and frame.get('tracking_projection_schema_version') == 1
                        and type(frame['tracking_projection_schema_version']) is int
                        and frame.get('source') == SOURCE and frame.get('requested_store_id') == store
                        and frame.get('local_projection_atomic') is True
                        and frame.get('upstream_snapshot_atomic') is False
                        and frame.get('network_performed_by_read') is False
                        and frame.get('source_freshness') == 'unknown'
                        and frame['history']['generated_at'] == frame['generated_at']
                        and 0 <= (clock-at).total_seconds() <= 30)
                except Exception:
                    valid = False
                if not valid:
                    raise TrackerLoopError('tracker_loop_invalid_projection')
                frames[store] = frame
            references = {}
            for directory, (ticket, latest, _, prediction) in active.items():
                with self.gate:
                    self._advance(directory, ticket, latest, prediction, frames[ticket['store_id']],
                                  reference_cache=references)
            self.publish_demands()
            self.counts['cycles'] += 1
            return self.summary(active_sessions=len(active))
        finally:
            self.cycle_lock.release()

    def _advance(self, directory, ticket, latest, prediction, frame, *, reference_cache=None):
        # Gate only local private commits, never projection HTTP or AI.
        with TrackingSession(directory) as session:
            ticket, latest, terminal = session.load(now=self.clock())
            name = f"prediction-{latest['tracking_version']:04d}.json" if latest else None
            prediction = session._read(name) if name and name in os.listdir(session.fd) else None
        if terminal is not None or self.clock() >= _time(ticket['deadline_at']):
            return
        profile = self.trend_profiles.get(ticket['store_id'])
        archive = self.trend_archives.get(ticket['store_id'])
        window = (archive['scope']['window_seconds'] if archive else
                  profile['window_seconds'] if profile else 600)
        context = context_from_history(frame['history'], queue_type=ticket['queue_type'],
            now=self.clock(), window_seconds=window,
            max_local_age_seconds=360, ttl_seconds=60)
        if archive is not None:
            from .trendarchive import select_profile
            from .trendprofiles import _calendar
            values = {f['feature_id']:f['value'] for f in context['features']}
            prefix = ticket['queue_type']
            milliseconds = values.get(prefix+'_observed_milliseconds')
            pairs = values.get(prefix+'_comparable_pairs')
            cadence = [milliseconds,pairs] if milliseconds and pairs else None
            calendar = _calendar(context['as_of'], context['as_of'])
            key = (archive['archive_sha256'],
                int(_time(context['as_of']).timestamp())//window,
                tuple(calendar[k] for k in ('day_type','weekday','hour','month','season','holiday_name')),
                tuple(cadence) if cadence else None)
            profile = reference_cache.get(key) if reference_cache is not None else None
            if profile is None:
                profile = select_profile(archive, reference_for=context['as_of'],
                    cadence=cadence, now=self.clock())
                if reference_cache is not None:reference_cache[key] = profile
            # Selection is derived now; prepare a fresh context from this same
            # atomically copied frame after its creation, without a second GET.
            context = context_from_history(frame['history'], queue_type=ticket['queue_type'],
                now=self.clock(), window_seconds=window, max_local_age_seconds=360, ttl_seconds=60)
        if profile is not None:
            from .trendprofiles import enrich_context
            context, _ = enrich_context(profile, context, now=self.clock())
        observation = normalize_observation(ticket, frame['view'], context,
                                           as_of=context['as_of'], now=self.clock())
        elapsed = None if latest is None else (_time(context['as_of'])-
                                               _time(latest['observation']['as_of'])).total_seconds()
        if latest is None or elapsed is not None and elapsed > 0 and (
                _signature(observation) != _signature(latest['observation'])
                or elapsed >= min(30, _interval(ticket, latest, prediction, self.clock(), self.base))):
            receipt = observe_session(directory=directory, view=frame['view'], context=context,
                as_of=context['as_of'], now=self.clock())
            if not receipt['idempotent']:
                self.counts['observations_committed'] += 1
            if not receipt['durability_confirmed']:
                raise TrackerLoopError('tracker_loop_observation_durability_unconfirmed')
        with TrackingSession(directory) as session:
            _, latest, terminal = session.load(now=self.clock())
            present = latest and f"prediction-{latest['tracking_version']:04d}.json" in os.listdir(session.fd)
        if terminal is None and latest is not None and not present and directory not in self.futures:
            self.futures[directory] = self.pool.submit(self._job, directory, latest['tracking_version'])

    def summary(self, *, active_sessions=None):
        return {'ok': self.counts['failed_jobs'] == 0, **self.counts,
            'active_sessions': active_sessions, 'pending_jobs': len(self.futures),
            'official_collection_requests_by_tracker': 0, 'official_query_credentials_accessed': False,
            'scheduler_applied_by_tracker': False, 'notification_sent': False, 'business_writes': 0,
            'source_freshness': 'unknown', 'verified_training_labels': 0, 'eta_available': False,
            'contains_personal_identifiers': False, 'automatic_paid_retry': False}

    def close(self):
        with self.gate:
            self.closed = True
        self.pool.shutdown(wait=True, cancel_futures=True)
        # Cancelled jobs were never started, so neither publication nor provider
        # work happened. Running jobs see closed and cannot commit afterward.
        for directory, future in list(self.futures.items()):
            if future.cancelled():
                del self.futures[directory]
        self._reap()


def run_tracking(coordinator, *, duration_seconds, max_cycles, stop=None, emit=lambda _: None):
    if not _integer(duration_seconds, 1, 21600) or not _integer(max_cycles, 1, 4320):
        raise TrackerLoopError('tracker_loop_invalid_bounds')
    stop = threading.Event() if stop is None else stop
    try:
        began = coordinator.monotonic()
        if type(began) not in (int, float) or not math.isfinite(began) or not 0 <= began <= 1e12:
            raise TrackerLoopError('tracker_loop_invalid_clock')
        previous = began
        while not stop.is_set() and coordinator.counts['cycles'] < max_cycles:
            current = coordinator.monotonic()
            if type(current) not in (int, float) or not math.isfinite(current) or not 0 <= current <= 1e12:
                raise TrackerLoopError('tracker_loop_invalid_clock')
            if current < previous:
                raise TrackerLoopError('tracker_loop_clock_regressed')
            previous = current
            if current-began >= duration_seconds:
                break
            summary = coordinator.cycle()
            emit(summary)
            if summary['active_sessions'] == 0:
                break
            remaining = duration_seconds-(coordinator.monotonic()-began)
            if coordinator.counts['cycles'] < max_cycles and remaining > 0:
                stop.wait(min(coordinator.read_interval, remaining))
    finally:
        coordinator.close()
    return coordinator.summary()
