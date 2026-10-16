"""Read saved status once per minute; no source API or supplier calls."""
from datetime import datetime, timezone
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import threading
import urllib.request

spec = importlib.util.spec_from_file_location('collection_alerts',
    Path(__file__).with_name('collection_alerts.py'))
alerts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(alerts)
Monitor, condition, read_status = alerts.Monitor, alerts.condition, alerts.read_status


def delivery_settings(root):
    selector = root / 'active-provider.json'
    if selector.exists():
        value = read_status(selector)
        if (set(value) != {'schema_version', 'provider'} or value['schema_version'] != 1
                or value['provider'] not in {'qq_smtp', 'serverchan_turbo'}):
            raise ValueError('alert_provider_invalid')
        use_mail = value['provider'] == 'qq_smtp'
    else:
        use_mail = (root / 'email.private.json').exists()
    # An explicit channel selection never falls back if its credential is lost.
    return (root / ('email-notification-state.json' if use_mail else 'notification-state.json'),
            read_status(root / ('email.private.json' if use_mail else 'serverchan.private.json')))


def recovery_link(root):
    """Local broker decides whether this exact incident requires owner login."""
    path = root / 'recovery-notifications.json'
    if not path.exists():
        return None
    try:
        value = read_status(path)
        if (set(value) != {'schema_version', 'internal_key_file'} or value['schema_version'] != 1
                or not Path(value['internal_key_file']).is_absolute()):
            raise ValueError('alert_recovery_config_invalid')
        key = alerts._read_private_file(Path(value['internal_key_file']))
        if len(key) != 32:
            raise ValueError('alert_recovery_config_invalid')
        request = urllib.request.Request('http://127.0.0.1:18910/internal/issue',
            data=b'{"verification":false}', method='POST', headers={
                'Content-Type': 'application/json', 'X-SUSHIWAIT-Internal': key.hex()})
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_args, **_kwargs): return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=5) as response:
            value = json.loads(response.read(1025))
        link = value.get('url')
        # Same strict validator as the message constructor; no arbitrary URL.
        alerts.message('incident', 'collection_stopped', datetime.now(timezone.utc), link)
        return link
    except Exception:
        # The basic incident alert is still useful if the broker is unavailable.
        return None


def current_condition(collection_root, now):
    """A broken sync status must not be misreported as a collector failure."""
    try:
        collection=read_status(collection_root/'collection-status.json')
    except Exception:
        return 'collection_status_unavailable'
    try:
        projection=read_status(collection_root/'projection-status.json')
    except Exception:
        projection={'state':'unavailable'}
    return condition(collection,projection,now)


def main():
    if len(sys.argv) != 3:
        return 2
    root, collection_root = map(Path, sys.argv[1:])
    if any(not p.is_absolute() or p.is_symlink() or p.stat().st_mode & 0o777 != 0o700
           for p in (root, collection_root)):
        return 2
    os.umask(0o077)
    lock = os.open(root / 'worker.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    try:
        state_file, config = delivery_settings(root)
        monitor = Monitor(state_file, config)
        while not stopped.is_set():
            now = datetime.now(timezone.utc)
            status = current_condition(collection_root, now)
            delivery = monitor.step(status, now,
                recovery_link=(lambda: recovery_link(root)) if status == 'collection_stopped' else None)
            print(json.dumps({'event': 'collection_alert_check', 'policy': alerts.ALERT_POLICY, 'condition': status,
                              'delivery': delivery}), flush=True)
            stopped.wait(60)
        return 0
    finally:
        os.close(lock)


if __name__ == '__main__':
    raise SystemExit(main())
