"""Official month/day routes over an explicitly selected continuity archive."""
from collections import Counter
from zoneinfo import ZoneInfo

from .continuity import SEMANTICS, _comparison
from .continuityarchive import SCHEMA, singleton_bundle
from .monitoring import _time
from .observations import QUEUE_NAMES
from .officialview import OfficialStatisticsView


class ContinuityStatisticsView(OfficialStatisticsView):
    _schema = SCHEMA

    def _dependencies(self, db, rows):
        marks=','.join('?' for _ in rows)
        # Include exact predecessor columns/bodies as well as current claims.
        # A later link or any changed dependency cannot reuse a prior result.
        return db.execute(f"SELECT c.observation_id,c.writer_id,c.claimed_at,c.link_sha256,CASE WHEN length(CAST(c.link_json AS BLOB))<=4096 THEN c.link_json END,c.linked_at,"
            "p.observation_id,p.record_sha256,p.store_id,p.api_profile,p.data_origin,p.recorded_at,p.ok,p.archived_at,CASE WHEN length(CAST(p.record_json AS BLOB))<=65536 THEN p.record_json END,"
            "a.writer_id,a.claimed_at FROM continuity_claims c "
            "LEFT JOIN archive_records p ON p.observation_id=json_extract(CASE WHEN length(CAST(c.link_json AS BLOB))<=4096 THEN c.link_json END,'$.predecessor_id') "
            "LEFT JOIN continuity_claims a ON a.observation_id=p.observation_id "
            f"WHERE c.observation_id IN ({marks}) ORDER BY c.observation_id",[r[0] for r in rows]).fetchall()

    def _common(self, now):
        return {**super()._common(now), 'archive_kind': 'public_records_with_reported_continuity',
                'continuity_archive_schema_version': 1, 'source_claims_verified': False}

    def _project(self, db, packet, store, now):
        result = super()._project(db, packet, store, now)
        states = Counter()
        totals = {q: 0 for q in QUEUE_NAMES}
        comparable = 0
        # Base projection uses this exact ordering, including duplicate times.
        records = sorted(packet['records'], key=lambda r:
            (r['timing'].get('request_started_at', r['recorded_at']),
             r['recorded_at'], r['observation_id']))
        for index, record in enumerate(records):
            started = record['timing'].get('request_started_at', record['recorded_at'])
            day = _time(started).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
            points = result['days'][(store, day)]['points']
            # The day hook receives one day. Position is stable even where the
            # timestamps/labels duplicate and cannot serve as record identity.
            point = points[index]
            point.update(observation_id=record['observation_id'], record_sha256=record['record_sha256'])
            association = singleton_bundle(db, record, packet=packet)
            if association is not None:
                bundle, linked_at = association
                # singleton_bundle has already verified both records, exact
                # columns, writers, times and hashes. Reuse that validation;
                # rebuilding a complete one-point day here repeated all work.
                link=bundle['links'][0]
                previous=bundle['anchors'][0]['record'] if bundle['anchors'] else None
                state,interval,removed=_comparison(record,previous,link,day)
                point.update(continuity={**link,'source_claims_verified':False,'semantics':SEMANTICS},
                    comparison_state=state,interval_seconds=interval,removed_labels=removed)
                point['continuity_received_at'] = linked_at
            state = point['comparison_state']
            states[state] += 1
            if state == 'comparable_display_sets':
                comparable += 1
                for q in QUEUE_NAMES: totals[q] += point['removed_labels'][q]
        for value in result['days'].values():
            value.update(continuity_semantics=SEMANTICS, source_claims_verified=False)
            value['summary'].update(continuity_states=dict(states), comparable_display_pairs=comparable,
                comparison_predecessors_may_precede_selection=True,
                display_removed_labels=totals if comparable else {q: None for q in QUEUE_NAMES})
        return result
