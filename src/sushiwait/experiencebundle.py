"""Receive explicit browser drafts through the existing private intake ledger.

Every revision is validated before opening a writer. Each append keeps its own
first receipt transaction; a partial failure preserves the prefix for idempotent
retry. Browser record/export times never become server receipt times.
"""
from datetime import datetime, timezone
from .credentials import _read_private_file
from .intake import OutcomeIntakeStore, IntakeError
from .outcomes import _json, _time, validate_episode, MAX_INPUT_BYTES

PURPOSE = 'sushiwait_experience_revisions_v1'
SCOPE = ('episode_id', 'store_id', 'api_profile', 'data_origin', 'queue_type', 'party_size', 'table_type')


class ExperienceBundleError(ValueError):
    def __init__(self, code='experience_bundle_invalid', *, saved=0, commit_status='not_started'):
        self.error_code = code if code in {'experience_bundle_invalid', 'experience_bundle_input_unavailable',
            'experience_bundle_receive_failed'} else 'experience_bundle_invalid'
        self.saved = saved
        self.commit_status = commit_status
        super().__init__(self.error_code)


def validate_bundle(value, *, now=None, synthetic=False):
    clock = datetime.now(timezone.utc) if now is None else now
    try:
        if (type(value) is not dict or set(value) != {'schema_version','purpose','exported_at','revisions'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['purpose'] != PURPOSE or type(value['revisions']) is not list
                or not 1 <= len(value['revisions']) <= 7 or _time(value['exported_at']) > clock):
            raise ExperienceBundleError()
        exported = _time(value['exported_at']); records = []; previous = None
        for index, raw in enumerate(value['revisions']):
            record = validate_episode(raw, now=clock)
            if (record['revision'] != index+1 or record['api_profile'] != 'miniapp_gateway'
                    or record['data_origin'] != ('synthetic' if synthetic else 'self_reported')
                    or _time(record['recorded_at']) > exported):
                raise ExperienceBundleError()
            if previous is None:
                if len(record['events']) != 1:
                    raise ExperienceBundleError()
            elif (any(record[k] != previous[k] for k in SCOPE)
                    or _time(record['recorded_at']) < _time(previous['recorded_at'])
                    or len(record['events']) != len(previous['events'])+1
                    or record['events'][:-1] != previous['events']):
                raise ExperienceBundleError()
            previous = record; records.append(record)
        return records
    except ExperienceBundleError:
        raise
    except Exception:
        raise ExperienceBundleError() from None


def read_bundle(path, *, now=None, synthetic=False):
    try:
        body = _read_private_file(path)
        if len(body) > MAX_INPUT_BYTES:
            raise ExperienceBundleError()
        return validate_bundle(_json(body), now=now, synthetic=synthetic)
    except ExperienceBundleError:
        raise
    except Exception:
        raise ExperienceBundleError('experience_bundle_input_unavailable') from None


def receive_bundle(path, *, database, synthetic=False):
    records = read_bundle(path, synthetic=synthetic)
    saved = duplicates = 0
    try:
        with OutcomeIntakeStore(database) as intake:
            for record in records:
                result = intake.append(record)
                saved += result['committed'] is True
                duplicates += result['idempotent'] is True
    except IntakeError as error:
        raise ExperienceBundleError('experience_bundle_receive_failed', saved=saved,
            commit_status=error.commit_status) from None
    except Exception:
        raise ExperienceBundleError('experience_bundle_receive_failed', saved=saved,
            commit_status='unknown') from None
    return {'revision_count':len(records), 'new_revisions':saved,'duplicate_revisions':duplicates,
        'first_receipt_preserved':True,'intake_received':True,'review_accepted':False,
        'verified_training_labels':0,'eta_available':False,'network_performed':False,
        'provider_called':False,'business_writes':0,'notification_sent':False}
