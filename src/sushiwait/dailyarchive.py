"""Read exactly one selected date/month of daily-controller archives.

No database is opened and no collector or origin request is started by a read.
The paired result hash detects accidental modification; it is not a signature
from the restaurant and not an independent backup.
"""
from calendar import monthrange
from copy import deepcopy
from datetime import date
import hashlib
import math
import os
from pathlib import Path
import re
import stat

from .dailycontroller import read_daily_archive
from .remotetasks import RemoteTaskError, _json, _now, _at, _integer
from .remote import SOURCE, QUEUE_NAMES, _id, _time, _LABEL, _ERRORS
from .capture import _open_parent, _check_parent
from zoneinfo import ZoneInfo


def selected_date(value):
    if (type(value) is not str or not re.fullmatch(r'20[0-9]{2}-[0-9]{2}-[0-9]{2}', value)):
        raise RemoteTaskError('daily_archive_invalid_date')
    try:
        if date.fromisoformat(value).isoformat() != value: raise ValueError
    except ValueError:
        raise RemoteTaskError('daily_archive_invalid_date') from None
    return value


def legacy_reader_config(root, through_date, *, activation=None, daily_root=None):
    """Explicit old-export ownership ends before the new writer's local date.

    This only validates the declared paths and dates. It neither creates nor
    discovers an export directory: the first export may not exist yet.
    """
    if root is None and through_date is None: return None
    if root is None or through_date is None:
        raise RemoteTaskError('daily_legacy_reader_requires_root_and_cutoff')
    path = Path(root)
    if not path.is_absolute() or len(str(path)) > 400 or '..' in path.parts:
        raise RemoteTaskError('daily_legacy_reader_invalid_root')
    cutoff = selected_date(through_date)
    if activation is not None and cutoff >= _at(activation).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat():
        raise RemoteTaskError('daily_legacy_reader_overlaps_activation')
    if daily_root is not None:
        current = Path(daily_root).absolute()
        if path == current or path in current.parents or current in path.parents:
            raise RemoteTaskError('daily_legacy_reader_overlaps_daily_root')
    return {'root': str(path), 'through_date': cutoff}


def _projection(value, store_id, day):
    """Validate the bounded public projection for either archive envelope."""
    if (type(value) is not dict or set(value) != {
            'daily_schema_version', 'source', 'requested_store_id', 'local_date', 'generated_at',
            'summary', 'points', 'returned_graph_points', 'graph_truncated', 'labels_per_array_in_graph',
            'full_source_arrays_persisted', 'call_reference_semantics', 'first_label_is_confirmed_call',
            'display_turnover_is_no_show_rate', 'actual_called_count', 'network_performed_by_read', 'eta_available'}
            or type(value['daily_schema_version']) is not int or value['daily_schema_version'] != 1
            or value['source'] != SOURCE or value['requested_store_id'] != store_id
            or value['local_date'] != day or value['eta_available'] is not False
            or value['network_performed_by_read'] is not False or value['first_label_is_confirmed_call'] is not False
            or value['actual_called_count'] is not None or value['display_turnover_is_no_show_rate'] is not False
            or value['call_reference_semantics'] != 'user_assumed_first_displayed_label'
            or value['labels_per_array_in_graph'] != 3 or value['full_source_arrays_persisted'] is not True
            or type(value['graph_truncated']) is not bool
            or type(value['points']) is not list or len(value['points']) > 2048
            or not _integer(value['returned_graph_points'], 0, 2048)
            or value['returned_graph_points'] != len(value['points'])):
        raise ValueError
    _time(value['generated_at'])
    summary = value['summary']
    counters = {'observations', 'successful_pairs', 'failed_pairs', 'scheduled_pause_slots',
        'recorded_http_attempts', 'group_successes', 'long_gap_boundaries', 'run_boundaries',
        'expected_background_slots_full_day', 'expected_background_slots_so_far',
        'observed_background_slots', 'returned_graph_points'}
    if summary is not None:
        if (type(summary) is not dict or set(summary) != counters | {
                'first_observation_at', 'last_observation_at', 'display_removed_labels', 'local_date',
                'date_type', 'calendar_status', 'rule_scope', 'source', 'business_hours_verified', 'store_id',
                'declared_intervals', 'observed_slot_fraction_so_far', 'slot_fraction_semantics',
                'graph_truncated', 'actual_called_count', 'no_show_rate', 'count_unit',
                'full_source_arrays_persisted', 'source_freshness'}
                or summary['store_id'] != store_id or summary['local_date'] != day
                or any(not _integer(summary[k]) for k in counters)
                or summary['observations'] < len(value['points'])
                or summary['observations'] != sum(summary[k] for k in ('successful_pairs', 'failed_pairs', 'scheduled_pause_slots'))
                or summary['actual_called_count'] is not None or summary['no_show_rate'] is not None
                or summary['count_unit'] != 'unknown' or summary['source_freshness'] != 'unknown'
                or summary['source'] != 'user_assumed' or summary['business_hours_verified'] is not False
                or summary['full_source_arrays_persisted'] is not True
                or summary['slot_fraction_semantics'] != 'successful_queue_response_in_declared_background_bin'
                or type(summary['graph_truncated']) is not bool): raise ValueError
        for key in ('first_observation_at', 'last_observation_at'): _time(summary[key])
        for key in ('date_type', 'calendar_status', 'rule_scope'):
            if type(summary[key]) is not str or not re.fullmatch('[a-z_]{1,64}', summary[key]): raise ValueError
        fraction = summary['observed_slot_fraction_so_far']
        if fraction is not None and (type(fraction) not in (int, float) or not math.isfinite(fraction) or not 0 <= fraction <= 1): raise ValueError
        if (type(summary['declared_intervals']) is not list or len(summary['declared_intervals']) > 16): raise ValueError
        for span in summary['declared_intervals']:
            if type(span) is not list or len(span) != 2 or _time(span[0]) >= _time(span[1]): raise ValueError
        removed = summary['display_removed_labels']
        if type(removed) is not dict or set(removed) != set(QUEUE_NAMES) or any(not _integer(n) for n in removed.values()): raise ValueError
    elif value['points']: raise ValueError
    for point in value['points']:
        if type(point) is not dict or set(point) != {
                'request_started_at', 'queue_received_at', 'count_received_at', 'pair_ok', 'scheduled_pause',
                'queues', 'call_reference_labels', 'display_sizes', 'count_raw', 'comparison_state',
                'interval_seconds', 'removed_labels', 'error_codes'}: raise ValueError
        if _time(point['request_started_at']).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat() != day: raise ValueError
        for key in ('queue_received_at', 'count_received_at'):
            if point[key] is not None: _time(point[key])
        if type(point['pair_ok']) is not bool or type(point['scheduled_pause']) is not bool: raise ValueError
        queues = point['queues']; references = point['call_reference_labels']; sizes = point['display_sizes']
        if queues is None:
            if references is not None or sizes is not None: raise ValueError
        else:
            if any(type(m) is not dict or set(m) != set(QUEUE_NAMES) for m in (queues, references, sizes)): raise ValueError
            for q in QUEUE_NAMES:
                if (type(queues[q]) is not list or len(queues[q]) > 3
                        or any(type(label) is not str or not _LABEL.fullmatch(label) for label in queues[q])
                        or references[q] != (queues[q][0] if queues[q] else None)
                        or not _integer(sizes[q], len(queues[q]), 100)): raise ValueError
        if point['count_raw'] is not None and not _integer(point['count_raw'], 0, 1000000): raise ValueError
        if point['comparison_state'] not in {'insufficient', 'gap', 'run_boundary', 'time_order_or_duplicate', 'comparable_display_sets'}: raise ValueError
        interval = point['interval_seconds']
        if interval is not None and (type(interval) not in (int, float) or not math.isfinite(interval)): raise ValueError
        removed = point['removed_labels']
        if removed is not None and (type(removed) is not dict or set(removed) != set(QUEUE_NAMES) or any(not _integer(n, 0, 100) for n in removed.values())): raise ValueError
        if (type(point['error_codes']) is not dict or not set(point['error_codes']) <= {'groupqueues', 'storequeuecount'}
                or any(e not in _ERRORS for e in point['error_codes'].values())): raise ValueError
    return value


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
    selected_date(day)
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
                ):
            raise ValueError
        return _projection(value, store_id, day)
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


def _legacy_identity(root, store_id, day):
    parent = _private_directory(Path(root)/day)
    try:
        values = []
        for name in ('manifest.json', 'store-'+store_id+'.json'):
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.geteuid()):
                raise RemoteTaskError('daily_archive_unavailable_or_changed')
            values.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                info.st_ctime_ns, info.st_mode, info.st_uid))
        _check_parent(Path(root)/day/'manifest.json', parent, private=True)
        return tuple(values)
    finally:
        os.close(parent)


def _private_directory(path):
    """Preserve ENOENT for not-yet-exported folders; reject unsafe traversal."""
    absolute = Path(path).absolute()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
    descriptor = os.open(absolute.anchor, flags)
    try:
        for component in absolute.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor); descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise RemoteTaskError('daily_archive_unavailable_or_changed')
        _check_parent(absolute/'reader-sentinel', descriptor, private=True)
        return descriptor
    except BaseException as error:
        os.close(descriptor)
        if isinstance(error, (OSError, ValueError)) and not isinstance(error, (FileNotFoundError, RemoteTaskError)):
            raise RemoteTaskError('daily_archive_unavailable_or_changed') from None
        raise


def read_legacy_day(root, store_id, day):
    """Read one finite-trial export with its original receipt/export times."""
    _id(store_id); selected_date(day)
    folder = Path(root)/day
    loaded = False
    try:
        for path in (Path(root), folder):
            parent = _private_directory(path); os.close(parent)
        try:
            raw_manifest = read_daily_archive(folder/'manifest.json')
        except FileNotFoundError:
            # A projection with no manifest is an incomplete export, not a
            # day on which zero people were called.
            try: read_daily_archive(folder/('store-'+store_id+'.json'))
            except FileNotFoundError: raise
            raise RemoteTaskError('daily_archive_unavailable_or_changed')
        loaded = True
        manifest = _json(raw_manifest)
        if (type(manifest) is not dict or set(manifest) != {
                'local_date', 'exported_at', 'stores', 'official_requests_added_by_export',
                'active_database_opened', 'independent_backup', 'full_day_source_quality_verified'}
                or manifest['local_date'] != day or type(manifest['official_requests_added_by_export']) is not int
                or manifest['official_requests_added_by_export'] != 0
                or manifest['active_database_opened'] is not False or manifest['independent_backup'] is not False
                or manifest['full_day_source_quality_verified'] is not False
                or type(manifest['stores']) is not list or not 1 <= len(manifest['stores']) <= 16): raise ValueError
        exported = _time(manifest['exported_at'])
        seen = set(); row = None
        for item in manifest['stores']:
            if (type(item) is not dict or set(item) not in (
                    {'store_id', 'name', 'state', 'points', 'complete_observed_projection', 'sha256', 'daily_quality'},
                    {'store_id', 'name', 'error_type'})
                    or _id(item['store_id']) in seen or type(item['name']) is not str
                    or not 1 <= len(item['name']) <= 160): raise ValueError
            seen.add(item['store_id'])
            if item['store_id'] == store_id: row = item
        if row is None or 'error_type' in row: raise ValueError
        if (row['state'] not in {'saved', 'already_saved'} or not _integer(row['points'], 0, 2048)
                or type(row['complete_observed_projection']) is not bool
                or type(row['sha256']) is not str or not re.fullmatch('[0-9a-f]{64}', row['sha256'])): raise ValueError
        before = _legacy_identity(root, store_id, day)
        raw = read_daily_archive(folder/('store-'+store_id+'.json'))
        value = _projection(_json(raw), store_id, day)
        summary = value['summary']
        complete = not value['graph_truncated'] and len(value['points']) == (summary['observations'] if summary else 0)
        if (hashlib.sha256(raw).hexdigest() != row['sha256'] or row['points'] != len(value['points'])
                or row['complete_observed_projection'] != complete or row['daily_quality'] != summary
                or exported < _time(value['generated_at'])
                or _legacy_identity(root, store_id, day) != before
                or read_daily_archive(folder/'manifest.json') != raw_manifest): raise ValueError
        return {**value, 'archive_lineage': {'kind': 'finite_trial_daily_export',
            'exported_at': manifest['exported_at'], 'hash_verified': True,
            'complete_observed_projection': complete, 'independent_backup': False,
            'full_day_source_quality_verified': False}}
    except FileNotFoundError:
        if loaded: raise RemoteTaskError('daily_archive_unavailable_or_changed') from None
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise RemoteTaskError('daily_archive_unavailable_or_changed') from None


def _legacy_selection(root, store_id, day, legacy):
    if legacy is None or day > legacy['through_date']: return False
    # The cutoff is ownership, not a rule for silently preferring one copy.
    # Reject even a partial new archive on a date owned by the old export.
    try:
        parent = _private_directory(Path(root)); os.close(parent)
        parent = _private_directory(Path(root)/day)
    except FileNotFoundError:
        return True
    try:
        for name in ('projection.json', 'result.json'):
            try: os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError: continue
            raise RemoteTaskError('daily_legacy_archive_date_conflict')
        _check_parent(Path(root)/day/'result.json', parent, private=True)
    finally: os.close(parent)
    return True


def read_selected_day(root, store_id, day, *, legacy=None):
    _id(store_id); selected_date(day)
    if legacy is not None:
        if type(legacy) is not dict or set(legacy) != {'root', 'through_date'}:
            raise RemoteTaskError('daily_legacy_reader_invalid_config')
        legacy = legacy_reader_config(legacy['root'], legacy['through_date'], daily_root=root)
    try:
        if _legacy_selection(root, store_id, day, legacy):
            return read_legacy_day(legacy['root'], store_id, day)
        return read_day(root, store_id, day)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError) as error:
        if isinstance(error, FileNotFoundError): raise
        if isinstance(error, RemoteTaskError): raise
        raise RemoteTaskError('daily_archive_unavailable_or_changed') from None


def read_month(root, store_id, month, *, now, _cache=None, legacy=None):
    _id(store_id)
    if legacy is not None:
        if type(legacy) is not dict or set(legacy) != {'root', 'through_date'}:
            raise RemoteTaskError('daily_legacy_reader_invalid_config')
        legacy = legacy_reader_config(legacy['root'], legacy['through_date'], daily_root=root)
    days = []; missing = []; unavailable = []; origins = {}
    for day in selected_month(month):
        try:
            old = _legacy_selection(root, store_id, day, legacy)
            key=(str(Path(root).absolute()),store_id,day,
                (legacy['root'], legacy['through_date']) if legacy is not None else None)
            cached=_cache.get(key) if _cache is not None else None
            identity=None
            identify = (lambda: _legacy_identity(legacy['root'], store_id, day)) if old else (lambda: _archive_identity(root, day))
            if _cache is not None:
                try:identity=identify()
                except (OSError,ValueError):pass
            if cached is not None and identity==cached[0]:
                summary=deepcopy(cached[1]);_cache.move_to_end(key)
            else:
                value = read_selected_day(root, store_id, day, legacy=legacy)
                summary=value['summary']
                if _cache is not None and identity is not None:
                    # Only retain summaries: a month read cannot grow the live
                    # process into a collection of full-day graph objects.
                    if identify()!=identity:
                        raise RemoteTaskError('daily_archive_unavailable_or_changed')
                    _cache[key]=(identity,deepcopy(summary));_cache.move_to_end(key)
                    while len(_cache)>62:_cache.popitem(last=False)
            if summary is not None:
                days.append(summary)
                origins[day] = 'finite_trial_daily_export' if old else 'daily_controller'
        except FileNotFoundError:
            missing.append(day)
        except RemoteTaskError:
            unavailable.append(day)
    return {'daily_schema_version': 1, 'source': SOURCE, 'requested_store_id': store_id,
        'timezone': 'Asia/Shanghai', 'generated_at': _now(now), 'month': month, 'days': days,
        'missing_archive_dates': missing, 'unavailable_archive_dates': unavailable,
        'archive_origins': origins,
        'heatmap_semantics': 'coverage_only_traffic_not_calibrated', 'network_performed_by_read': False,
        'archive_hash_verified': True, 'actual_called_count': None, 'eta_available': False,
        'verified_training_labels': 0}
