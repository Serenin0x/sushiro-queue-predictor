"""Fixed start boundary for the prepared, finite cloud supplier service.

This only checks the server clock. It does not inspect sessions, read private
initialization material, launch software or access any network endpoint.
"""
from datetime import datetime, timezone
import json

START = datetime.fromisoformat('2026-10-12T00:00:00+08:00')
END = datetime.fromisoformat('2026-10-14T22:00:00+08:00')


def allowed_start(at):
    if not isinstance(at, datetime) or at.utcoffset() is None:
        raise ValueError('cloud_clock_invalid')
    return START <= at < END


def main():
    permitted = allowed_start(datetime.now(timezone.utc))
    print(json.dumps({'event': 'cloud_supplier_start_boundary',
                      'allowed': permitted, 'source_queries_added': 0}))
    return 0 if permitted else 1


if __name__ == '__main__':
    raise SystemExit(main())
