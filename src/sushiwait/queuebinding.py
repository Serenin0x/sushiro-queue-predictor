"""Declared display bindings; mapping is not authentication of call events."""

POLICY = 'ordinary_mixed_reservation_separate_v1'
_FIELDS = {'ordinary': 'mixedQueue', 'reservation': 'reservationQueue'}


def field_for_queue(queue_type):
    if type(queue_type) is not str or queue_type not in _FIELDS:
        raise ValueError('unknown_queue_type')
    return _FIELDS[queue_type]


def require_bound_context(context):
    if (type(context) is not dict or context.get('schema_version') != 2
            or type(context.get('schema_version')) is not int
            or context.get('queue_binding_policy') != POLICY):
        raise ValueError('queue_binding_required')
    return context
