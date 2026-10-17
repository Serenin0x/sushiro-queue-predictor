"""Anonymous writer continuity alongside unchanged public v1 records.

These are exporter-reported database predecessors, not authenticated source
events or proof of uninterrupted process liveness. No auth or network access.
The live v1 receiver deliberately does not accept this separate bundle format.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import os
import re
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from .monitoring import _time
from .observations import QUEUE_NAMES
from .packets import PacketError, _select_packet, _write_packet, encoded, public_record
from .receipts import MAX_PACKET_BYTES, ReceiptError, validate_packet

SEMANTICS = 'exporter_reported_nearest_scoped_database_predecessor'
_KEYS = {'continuity_schema_version', 'packet', 'anchors', 'links',
         'continuity_semantics', 'source_claims_verified'}
_LINK_KEYS = {'observation_id', 'record_sha256', 'writer_id', 'predecessor_id',
              'predecessor_sha256', 'predecessor_writer_id', 'link_sha256'}


class ContinuityError(PacketError):
    pass


def _uuid5(value):
    if type(value) is not str or len(value) != 36:
        raise ContinuityError('continuity_invalid_identity')
    parsed = UUID(value)
    if str(parsed) != value or parsed.version != 5:
        raise ContinuityError('continuity_invalid_identity')
    return value


def _digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def _writer(row):
    # The input run_id is a random SnapshotStore-instance UUID, never a WeChat
    # identity. Scope the alias so it cannot correlate unrelated store streams.
    return str(uuid5(NAMESPACE_URL,
        f"sushiwait:public-writer:1:{row['run_id']}:{row['api_profile']}:"
        f"{row['data_origin']}:{row['store_id']}"))


def build_continuity_bundle(packet, rows, predecessors, *, as_of):
    """Called inside the selector transaction; raw rows never enter the bundle."""
    safe = validate_packet(packet, as_of=as_of)
    if (type(rows) is not list or type(predecessors) is not list
            or len(rows) != len(safe['records']) or len(predecessors) != len(rows)):
        raise ContinuityError('continuity_invalid_selection')
    scope = {k: safe[k] for k in ('as_of', 'store_ids', 'api_profile', 'data_origin')}
    ids = {r['observation_id'] for r in safe['records']}
    links, anchors = [], {}
    for row, previous, record in zip(rows, predecessors, safe['records']):
        if public_record(row, **scope) != record:
            raise ContinuityError('continuity_selection_mismatch')
        prior = public_record(previous, **scope) if previous is not None else None
        if previous is not None and (previous['id'] >= row['id']
                or previous['store_id'] != row['store_id']):
            raise ContinuityError('continuity_invalid_predecessor')
        alias = _writer(row)
        prior_alias = _writer(previous) if prior else None
        link = {'observation_id': record['observation_id'],
                'record_sha256': record['record_sha256'], 'writer_id': alias,
                'predecessor_id': prior['observation_id'] if prior else None,
                'predecessor_sha256': prior['record_sha256'] if prior else None,
                'predecessor_writer_id': prior_alias}
        links.append({**link, 'link_sha256': _digest(link)})
        if prior and prior['observation_id'] not in ids:
            anchors[prior['observation_id']] = {'record': prior, 'writer_id': prior_alias}
    bundle = {'continuity_schema_version': 1, 'packet': safe,
              'anchors': [anchors[i] for i in sorted(anchors)], 'links': links,
              'continuity_semantics': SEMANTICS, 'source_claims_verified': False}
    return validate_continuity_bundle(bundle, as_of=as_of)


def validate_continuity_bundle(bundle, *, as_of):
    """Check exact hashes/graph/scope, without authenticating exporter claims."""
    try:
        if (type(bundle) is not dict or set(bundle) != _KEYS
                or type(bundle['continuity_schema_version']) is not int
                or bundle['continuity_schema_version'] != 1
                or bundle['continuity_semantics'] != SEMANTICS
                or bundle['source_claims_verified'] is not False):
            raise ContinuityError('continuity_invalid_format')
        if len(encoded(bundle)) > MAX_PACKET_BYTES:
            raise ContinuityError('continuity_too_large')
        packet = validate_packet(bundle['packet'], as_of=as_of)
        if (type(bundle['anchors']) is not list or len(bundle['anchors']) > 1000
                or type(bundle['links']) is not list
                or len(bundle['links']) != len(packet['records'])):
            raise ContinuityError('continuity_invalid_format')
        records = {r['observation_id']: r for r in packet['records']}
        writers, external, used = {}, set(), set()
        for anchor in bundle['anchors']:
            if type(anchor) is not dict or set(anchor) != {'record', 'writer_id'}:
                raise ContinuityError('continuity_invalid_anchor')
            validate_packet({**packet, 'records': [anchor['record']]}, as_of=as_of)
            identity = anchor['record']['observation_id']
            if identity in records:
                raise ContinuityError('continuity_duplicate_anchor')
            records[identity] = anchor['record']
            writers[identity] = _uuid5(anchor['writer_id'])
            external.add(identity)
        if [a['record']['observation_id'] for a in bundle['anchors']] != sorted(external):
            raise ContinuityError('continuity_noncanonical_anchors')
        seen, last = set(), {}
        for record, link in zip(packet['records'], bundle['links']):
            if (type(link) is not dict or set(link) != _LINK_KEYS
                    or link['observation_id'] != record['observation_id']
                    or link['record_sha256'] != record['record_sha256']
                    or type(link['link_sha256']) is not str
                    or re.fullmatch('[0-9a-f]{64}', link['link_sha256']) is None
                    or link['link_sha256'] != _digest({k:v for k,v in link.items() if k != 'link_sha256'})):
                raise ContinuityError('continuity_invalid_link')
            identity, store = record['observation_id'], record['store_id']
            writers[identity] = _uuid5(link['writer_id'])
            predecessor = link['predecessor_id']
            if predecessor is None:
                if (link['predecessor_sha256'] is not None
                        or link['predecessor_writer_id'] is not None or store in last):
                    raise ContinuityError('continuity_invalid_predecessor')
            else:
                _uuid5(predecessor)
                if predecessor not in external | seen:
                    raise ContinuityError('continuity_missing_predecessor')
                prior = records[predecessor]
                if (prior['store_id'] != store
                        or link['predecessor_sha256'] != prior['record_sha256']
                        or link['predecessor_writer_id'] != writers[predecessor]
                        or store in last and predecessor != last[store]):
                    raise ContinuityError('continuity_invalid_predecessor')
                used.add(predecessor)
            seen.add(identity)
            last[store] = identity
        if not external <= used:
            raise ContinuityError('continuity_unused_anchor')
        return json.loads(encoded(bundle))
    except ContinuityError:
        raise
    except (ReceiptError, PacketError, ValueError, TypeError, KeyError,
            OverflowError, RecursionError, UnicodeError):
        raise ContinuityError('continuity_invalid_format') from None


def select_continuity_packet(database, **scope):
    return _select_packet(database, **scope, with_continuity=True)


def read_continuity_bundle(path, *, as_of):
    from .receipts import _read_packet_value
    return validate_continuity_bundle(_read_packet_value(path), as_of=as_of)


def export_continuity_packet(database, destination, **scope):
    try:
        conflict = os.path.abspath(database) == os.path.abspath(destination)
    except (TypeError, ValueError, OSError):
        raise ContinuityError('packet_invalid_path') from None
    if conflict:
        raise ContinuityError('packet_path_conflict')
    bundle, local = select_continuity_packet(database, **scope)
    body = encoded(bundle)
    result = _write_packet(body, destination)
    return {**local, **result, 'continuity_schema_version': 1,
            'bundle_bytes': len(body), 'bundle_sha256': hashlib.sha256(body).hexdigest(),
            'network_performed': False, 'server_received': False, 'credentials_accessed': False,
            'source_claims_verified': False, 'eta_available': False,
            'output_requires_private_handling': True}


def _started(record):
    return record['timing'].get('request_started_at', record['recorded_at'])


def _queues(record):
    groups = record.get('display', {}).get('groupQueues', {}).get('groups')
    if not groups or any(groups[q]['presence'] != 'present' for q in QUEUE_NAMES):
        return None
    return {q: groups[q]['value'] for q in QUEUE_NAMES}


def _comparison(record, previous, link, local_date, max_gap_seconds=120):
    """Compare records from an already validated continuity bundle."""
    zone=ZoneInfo('Asia/Shanghai')
    state, interval, removed = 'insufficient', None, None
    if previous is not None:
        elapsed = (_time(record['recorded_at']) - _time(previous['recorded_at'])).total_seconds()
        if link['writer_id'] != link['predecessor_writer_id']:
            state = 'run_boundary'
        elif not record['ok'] or not previous['ok']:
            state = 'failed_observation_boundary'
        elif _time(_started(previous)).astimezone(zone).date().isoformat() != local_date:
            state = 'day_boundary'
        elif elapsed <= 0 or _time(_started(record)) < _time(previous['recorded_at']):
            state = 'time_order_or_duplicate'
        elif elapsed > max_gap_seconds:
            state = 'gap'
        else:
            before, after = _queues(previous), _queues(record)
            if before is None or after is None:
                state = 'missing_queue_fields'
            else:
                state, interval = 'comparable_display_sets', elapsed
                removed = {q: len(set(before[q]) - set(after[q])) for q in QUEUE_NAMES}
    return state,interval,removed


def project_continuity_bundle(bundle, *, as_of, hours, base_interval=60,
                              store_names=None, max_gap_seconds=120):
    """Annotate official curves; long/failed/restarted intervals yield no rate."""
    from .officialstats import project_packet
    if type(max_gap_seconds) is not int or not 1 <= max_gap_seconds <= 120:
        raise ContinuityError('continuity_invalid_gap')
    safe = validate_continuity_bundle(bundle, as_of=as_of)
    packet = safe['packet']
    result = project_packet(packet, as_of=as_of, hours=hours,
                            base_interval=base_interval, store_names=store_names)
    records = {r['observation_id']: r for r in packet['records']}
    records.update({a['record']['observation_id']: a['record'] for a in safe['anchors']})
    links = {v['observation_id']: v for v in safe['links']}
    by_day = defaultdict(list)
    zone = ZoneInfo('Asia/Shanghai')
    for record in packet['records']:
        day = _time(_started(record)).astimezone(zone).date().isoformat()
        by_day[(record['store_id'], day)].append(record)
    for key, selected in by_day.items():
        selected.sort(key=lambda r: (_started(r), r['recorded_at'], r['observation_id']))
        value = result['days'][key]
        totals = {q: 0 for q in QUEUE_NAMES}
        states = Counter()
        comparable = 0
        for row, record in zip(value['points'], selected):
            link = links[record['observation_id']]
            previous = records.get(link['predecessor_id'])
            state,interval,removed=_comparison(record,previous,link,key[1],max_gap_seconds)
            if state=='comparable_display_sets':
                comparable += 1
                for q in QUEUE_NAMES: totals[q] += removed[q]
            states[state] += 1
            row.update(observation_id=record['observation_id'], record_sha256=record['record_sha256'],
                       continuity={**link, 'source_claims_verified': False, 'semantics': SEMANTICS},
                       comparison_state=state, interval_seconds=interval, removed_labels=removed)
        value['continuity_semantics'] = SEMANTICS
        value['source_claims_verified'] = False
        value['summary'].update(continuity_states=dict(states), comparable_display_pairs=comparable,
                                comparison_predecessors_may_precede_selection=True,
                                display_removed_labels=totals if comparable else {q: None for q in QUEUE_NAMES})
    result['source_continuity_bundle'] = safe
    return result
