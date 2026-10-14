"""Read saved public day JSON and write a shadow report to stdout. No network."""
import argparse
import hashlib
import json
from pathlib import Path

from sushiwait.displayreplay import POLICY, replay, summarize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archives', type=Path, nargs='+')
    parser.add_argument('--queue', choices=['ordinary', 'reservation'], default='ordinary')
    parser.add_argument('--max-gap-seconds', type=int, default=660)
    args = parser.parse_args()
    if len(args.archives) > 100:
        parser.error('at most 100 saved days')
    reports, identities, digests = [], set(), []
    for path in args.archives:
        with path.open('rb') as stream:
            body = stream.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024:
            parser.error('archive too large')
        report = replay(json.loads(body), queue=args.queue, max_gap_seconds=args.max_gap_seconds)
        identity = (report['store_id'], report['local_date'])
        if identity in identities:
            parser.error('duplicate store day')
        identities.add(identity)
        digests.append({'store_id': identity[0], 'local_date': identity[1],
                        'sha256': hashlib.sha256(body).hexdigest()})
        reports.append(report)
    print(json.dumps({'policy': POLICY, 'input_sha256': digests, 'reports': reports,
                      'summary': summarize([c for r in reports for c in r['cases']])},
                     ensure_ascii=False, allow_nan=False, indent=2))


if __name__ == '__main__':
    main()
