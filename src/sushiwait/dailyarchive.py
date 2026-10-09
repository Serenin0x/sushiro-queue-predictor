"""Read exactly one selected date/month of daily-controller archives.

No database is opened and no collector or origin request is started by a read.
The paired result hash detects accidental modification; it is not a signature
from the restaurant and not an independent backup.
"""
from calendar import monthrange
from copy import deepcopy
from datetime import date
import hashlib
import os
from pathlib import Path
import re
import stat

from .dailycontroller import read_daily_archive
from .remotetasks import RemoteTaskError, _json, _now
from .remote import SOURCE, _id
from .capture import _open_parent, _check_parent


def selected_month(value):
    if type(value) is not str or not re.fullmatch(r'20[0-9]{2}-[0-9]{2}', value):
        raise RemoteTaskError('daily_archive_invalid_month')
    try:
        year, month = map(int, value.split('-'))
        return [f'{value}-{i:02d}' for i in range(1, monthrange(year, month)[1]+1)]
    except ValueError:
        raise RemoteTaskError('daily_archive_invalid_month') from None


def read_day(root, store_id, day):
    _id(store_id)
    if (type(day) is not str or not re.fullmatch(r'20[0-9]{2}-[0-9]{2}-[0-9]{2}', day)
            or date.fromisoformat(day).isoformat() != day):
        raise RemoteTaskError('daily_archive_invalid_date')
    folder = Path(root)/day
    manifest_loaded = False
    try:
        parent, _ = _open_parent(Path(root)/'controller.json', private=True)
        try:
            info = os.stat(day, dir_fd=parent, follow_symlinks=False)
            if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
                    or info.st_uid != os.geteuid()):
                raise ValueError
            _check_parent(Path(root)/'controller.json', parent, private=True)
        finally:
            os.close(parent)
        result = _json(read_daily_archive(folder/'result.json'))
        manifest_loaded = True
        raw = read_daily_archive(folder/'projection.json')
        value = _json(raw)
        if (type(result) is not dict or type(result.get('daily_controller_schema_version')) is not int
                or result.get('daily_controller_schema_version') != 1
                or result.get('source') != SOURCE or result.get('store_id') != store_id
                or result.get('local_date') != day
                or result.get('projection_sha256') != hashlib.sha256(raw).hexdigest()
                or result.get('official_requests_added_by_archive') != 0
                or result.get('independent_backup') is not False
                or result.get('actual_call_verified') is not False or result.get('eta_available') is not False
                or type(value) is not dict or set(value) != {
                    'daily_schema_version', 'source', 'requested_store_id', 'local_date', 'generated_at',
                    'summary', 'points', 'returned_graph_points', 'graph_truncated', 'labels_per_array_in_graph',
                    'full_source_arrays_persisted', 'call_reference_semantics', 'first_label_is_confirmed_call',
                    'display_turnover_is_no_show_rate', 'actual_called_count', 'network_performed_by_read',
                    'eta_available'}
                or type(value['daily_schema_version']) is not int or value['daily_schema_version'] != 1
                or value['source'] != SOURCE or value['requested_store_id'] != store_id
                or value['local_date'] != day or value['eta_available'] is not False
                or value['network_performed_by_read'] is not False
                or value['first_label_is_confirmed_call'] is not False
                or value['actual_called_count'] is not None
                or value['display_turnover_is_no_show_rate'] is not False
                or value['call_reference_semantics'] != 'user_assumed_first_displayed_label'
                or type(value['points']) is not list or len(value['points']) > 2048
                or value['returned_graph_points'] != len(value['points'])
                or value['summary'] is not None and (
                    value['summary'].get('store_id') != store_id or value['summary'].get('local_date') != day)):
            raise ValueError
        return value
    except FileNotFoundError:
        if manifest_loaded:
            raise RemoteTaskError('daily_archive_unavailable_or_changed') from None
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise RemoteTaskError('daily_archive_unavailable_or_changed') from None


def _archive_identity(root, day):
    parent, _ = _open_parent(Path(root)/day/'projection.json', private=True)
    try:
        values=[]
        for name in ('projection.json','result.json'):
            info=os.stat(name,dir_fd=parent,follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o600
                    or info.st_uid!=os.geteuid()):
                raise RemoteTaskError('daily_archive_unavailable_or_changed')
            values.append((info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,
                           info.st_ctime_ns,info.st_mode,info.st_uid))
        _check_parent(Path(root)/day/'projection.json',parent,private=True)
        return tuple(values)
    finally:
        os.close(parent)


def read_month(root, store_id, month, *, now, _cache=None):
    _id(store_id)
    days = []; missing = []; unavailable = []
    for day in selected_month(month):
        try:
            key=(str(Path(root).absolute()),store_id,day)
            cached=_cache.get(key) if _cache is not None else None
            identity=None
            if _cache is not None:
                try:identity=_archive_identity(root,day)
                except (OSError,ValueError):pass
            if cached is not None and identity==cached[0]:
                summary=deepcopy(cached[1]);_cache.move_to_end(key)
            else:
                value = read_day(root, store_id, day)
                summary=value['summary']
                if _cache is not None and identity is not None:
                    # Only retain summaries: a month read cannot grow the live
                    # process into a collection of full-day graph objects.
                    if _archive_identity(root,day)!=identity:
                        raise RemoteTaskError('daily_archive_unavailable_or_changed')
                    _cache[key]=(identity,deepcopy(summary));_cache.move_to_end(key)
                    while len(_cache)>62:_cache.popitem(last=False)
            if summary is not None:
                days.append(summary)
        except FileNotFoundError:
            missing.append(day)
        except RemoteTaskError:
            unavailable.append(day)
    return {'daily_schema_version': 1, 'source': SOURCE, 'requested_store_id': store_id,
        'timezone': 'Asia/Shanghai', 'generated_at': _now(now), 'month': month, 'days': days,
        'missing_archive_dates': missing, 'unavailable_archive_dates': unavailable,
        'heatmap_semantics': 'coverage_only_traffic_not_calibrated', 'network_performed_by_read': False,
        'archive_hash_verified': True, 'actual_called_count': None, 'eta_available': False,
        'verified_training_labels': 0}
