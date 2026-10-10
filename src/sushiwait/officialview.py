"""Read-only official statistics over an explicit public-field packet archive.

The archive writer is separate. Reads never create a database, contact an
origin, inspect a credential, infer collector liveness or fall back to CRM.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from .businesshours import BusinessHours
from .capture import _open_parent, _check_parent
from .credentials import _private_file, _read_private_file
from .monitoring import _stamp, _time
from .officialstats import project_packet, SOURCE
from .packets import build_packet, encoded, _file_identity
from .receipts import MAX_PACKET_BYTES, _CREATE
from .storage import _unique_object

_MONTH = re.compile(r'/api/v1/official/months/(20[0-9]{2}-(?:0[1-9]|1[0-2]))\Z')
_DAY = re.compile(r'/api/v1/official/stores/([1-9][0-9]{0,9})/days/(20[0-9]{2}-[0-9]{2}-[0-9]{2})\Z')
_START = "COALESCE(json_extract(record_json,'$.timing.request_started_at'),json_extract(record_json,'$.timing.checked_at'))"
_COLUMNS = ('observation_id', 'record_sha256', 'store_id', 'api_profile',
            'data_origin', 'recorded_at', 'ok', 'archived_at', 'record_json')
SELECTION_SCOPE = 'bounded_archived_public_observations'


class OfficialViewError(ValueError):
    pass


def _json(raw):
    def reject(_):
        raise ValueError('nonfinite')
    return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=reject)


def read_config(path):
    """Read one private configuration; it contains paths/rules, never auth."""
    try:
        value = _json(_read_private_file(path))
        if (type(value) is not dict or set(value) != {'schema_version', 'mode',
                'database_file', 'store_names', 'data_origin', 'hours', 'base_interval'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['mode'] != 'official_packet_archive_readonly'):
            raise ValueError
        return OfficialStatisticsView(value['database_file'], store_names=value['store_names'],
            data_origin=value['data_origin'], hours=value['hours'], base_interval=value['base_interval'])
    except Exception:
        raise OfficialViewError('official_view_invalid_config') from None


class OfficialStatisticsView:
    def __init__(self, database, *, store_names, data_origin, hours, base_interval=60):
        try:
            if (not isinstance(database, (str, Path)) or not Path(database).is_absolute()
                    or type(store_names) is not dict or not 1 <= len(store_names) <= 3
                    or any(type(s) is not str or not re.fullmatch('[1-9][0-9]{0,9}', s)
                           or int(s) > 2**31-1 for s in store_names)
                    or any(type(n) is not str or not 1 <= len(n) <= 100
                           or any(ord(c) < 32 for c in n) for n in store_names.values())
                    or data_origin not in {'live', 'synthetic', 'fixture'}
                    or type(base_interval) is not int or not 60 <= base_interval <= 3600):
                raise ValueError
            self.database = Path(os.path.abspath(database))
            self.names = dict(store_names)
            self.data_origin = data_origin
            self.hours = deepcopy(BusinessHours(hours).rules)
            self.base_interval = base_interval
        except Exception:
            raise OfficialViewError('official_view_invalid_config') from None

    def allowed(self, path):
        if type(path) is not str:
            return False
        if _MONTH.fullmatch(path):
            return True
        match = _DAY.fullmatch(path)
        if not match or match[1] not in self.names:
            return False
        try:
            date.fromisoformat(match[2])
            return True
        except ValueError:
            return False

    def _common(self, now):
        return {'daily_schema_version':1, 'source':SOURCE, 'api_profile':'miniapp_gateway',
            'data_origin':self.data_origin, 'generated_at':_stamp(now),
            'network_performed_by_read':False, 'eta_available':False,
            'selection_scope':SELECTION_SCOPE, 'archive_schema_version':1,
            'source_freshness':'unknown', 'collection_state':'not_proven_by_archive'}

    def _empty_day(self, store, day, now):
        return {**self._common(now), 'requested_store_id':store, 'local_date':day,
            'summary':None, 'points':[], 'returned_graph_points':0, 'graph_truncated':False,
            'labels_per_array_in_graph':3, 'full_source_arrays_persisted':False,
            'persistence_semantics':'validated_selected_records_in_packet_archive',
            'call_reference_semantics':'user_assumed_first_displayed_label',
            'first_label_is_confirmed_call':False, 'display_turnover_is_no_show_rate':False,
            'actual_called_count':None}

    def _guard(self, parent, name, identity, file_fd):
        _check_parent(self.database, parent, private=True)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        opened = os.fstat(file_fd)
        if (not _private_file(named) or not _private_file(opened)
                or _file_identity(named) != identity or _file_identity(opened) != identity):
            raise OfficialViewError('official_view_database_changed')

    def _day(self, db, store, day, now):
        # UTC canonical strings have a uniform date/time prefix. Boundary
        # prefixes omit Z so .microseconds and whole seconds compare correctly.
        lower = _stamp(_time(day+'T00:00:00+08:00')).removesuffix('Z')
        upper = _stamp(_time(day+'T00:00:00+08:00')+timedelta(days=1)).removesuffix('Z')
        condition = f"store_id=? AND api_profile=? AND data_origin=? AND {_START}>=? AND {_START}<?"
        params = [store, 'miniapp_gateway', self.data_origin, lower, upper]
        count, size = db.execute(f'SELECT count(*),coalesce(sum(length(CAST(record_json AS BLOB))),0) FROM archive_records WHERE {condition}', params).fetchone()
        if count > 1000 or size > MAX_PACKET_BYTES:
            raise OfficialViewError('official_view_day_exceeds_bound')
        if count == 0:
            return self._empty_day(store, day, now)
        rows = db.execute(f"SELECT {','.join(_COLUMNS)} FROM archive_records WHERE {condition} ORDER BY {_START},recorded_at,observation_id LIMIT 1001", params).fetchall()
        packet = build_packet([], as_of=_stamp(now), store_ids=[store],
            api_profile='miniapp_gateway', data_origin=self.data_origin)
        for row in rows:
            record = _json(row[-1])
            if (type(record) is not dict or any(record.get(k) != row[i]
                    for i,k in enumerate(_COLUMNS[:6])) or type(row[6]) is not int
                    or row[6] not in (0, 1) or record.get('ok') is not bool(row[6])
                    or _stamp(_time(row[7])) != row[7] or _time(row[7]) > now):
                raise OfficialViewError('official_view_archive_record_mismatch')
            packet['records'].append(record)
        projection = project_packet(packet, as_of=_stamp(now), hours=self.hours,
            base_interval=self.base_interval, store_names={store:self.names[store]})
        if set(projection['days']) != {(store, day)}:
            raise OfficialViewError('official_view_date_mismatch')
        result = projection['days'][(store, day)]
        # Unlike the pure projector, these selected records were actually read
        # and validated from the existing durable archive. No full-day claim.
        result.update(self._common(now), full_source_arrays_persisted=True,
            persistence_semantics='validated_selected_records_in_packet_archive')
        result['summary'].update(selection_scope=SELECTION_SCOPE,
            full_source_arrays_persisted=True,
            persistence_semantics='validated_selected_records_in_packet_archive')
        return result

    def read(self, path, *, now=None):
        if not self.allowed(path):
            raise OfficialViewError('official_view_unknown_route')
        clock = datetime.now(timezone.utc) if now is None else _time(now.isoformat())
        parent = file_fd = db = None
        deadline = time.monotonic()+5
        try:
            parent, name = _open_parent(self.database, private=True)
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            info = os.fstat(file_fd)
            identity = _file_identity(info)
            self._guard(parent, name, identity, file_fd)
            db = sqlite3.connect(self.database.as_uri()+'?mode=ro', uri=True, timeout=0.05)
            db.execute('PRAGMA query_only=ON')
            db.execute('PRAGMA trusted_schema=OFF')
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            db.execute('BEGIN')
            objects = db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
            if (db.execute('PRAGMA user_version').fetchone()[0] != 1 or len(objects) != 1
                    or objects[0][:2] != ('table','archive_records')
                    or ' '.join(objects[0][2].split()) != ' '.join(_CREATE.split())
                    or db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete'):
                raise OfficialViewError('official_view_database_schema')
            match = _DAY.fullmatch(path)
            if match:
                result = self._day(db, match[1], match[2], clock)
            else:
                month = _MONTH.fullmatch(path)[1]
                result = {**self._common(clock), 'month':month,
                    'configured_store_ids':list(self.names), 'store_names':dict(self.names),
                    'unavailable_store_ids':[], 'days':{}, 'timezone':'Asia/Shanghai',
                    'heatmap_semantics':'coverage_only_traffic_not_calibrated'}
                day = date.fromisoformat(month+'-01')
                while day.strftime('%Y-%m') == month:
                    for store in self.names:
                        if time.monotonic() >= deadline:
                            raise OfficialViewError('official_view_read_timeout')
                        summary = self._day(db, store, day.isoformat(), clock)['summary']
                        if summary is not None:
                            result['days'].setdefault(day.isoformat(), {})[store] = summary
                    day += timedelta(days=1)
            self._guard(parent, name, identity, file_fd)
            db.rollback()
            return result
        except OfficialViewError:
            raise
        except Exception:
            raise OfficialViewError('official_view_archive_unavailable') from None
        finally:
            if db is not None:
                db.close()
            for descriptor in (file_fd, parent):
                if descriptor is not None:
                    os.close(descriptor)

    def dispatch(self, path):
        if not self.allowed(path):
            return 404, encoded({'error_code':'official_view_unknown_route'}), 'application/json; charset=utf-8'
        try:
            value = self.read(path)
            body = encoded(value)
            bound = 8*1024*1024 if _MONTH.fullmatch(path) else 2*1024*1024
            if len(body) > bound:
                raise OfficialViewError('official_view_response_too_large')
            return 200, body, 'application/json; charset=utf-8'
        except OfficialViewError as error:
            return 503, encoded({'error_code':str(error), 'source':SOURCE,
                'network_performed_by_read':False, 'eta_available':False}), 'application/json; charset=utf-8'
