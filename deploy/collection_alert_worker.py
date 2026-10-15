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

spec = importlib.util.spec_from_file_location('collection_alerts',
    Path(__file__).with_name('collection_alerts.py'))
alerts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(alerts)
Monitor, condition, read_status = alerts.Monitor, alerts.condition, alerts.read_status


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
        monitor = Monitor(root / 'notification-state.json', read_status(root / 'serverchan.private.json'))
        while not stopped.is_set():
            now = datetime.now(timezone.utc)
            try:
                status = condition(read_status(collection_root / 'collection-status.json'),
                                   read_status(collection_root / 'projection-status.json'), now)
            except Exception:
                status = 'collection_status_unavailable'
            delivery = monitor.step(status, now)
            print(json.dumps({'event': 'collection_alert_check', 'condition': status,
                              'delivery': delivery}), flush=True)
            stopped.wait(60)
        return 0
    finally:
        os.close(lock)


if __name__ == '__main__':
    raise SystemExit(main())
