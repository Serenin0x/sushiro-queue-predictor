"""Per-query business deadline around the installed collector in this child.

The parent deadline alone would leave a closing-time scheduling race. This
wrapper gates each fetch locally, reserving 16 seconds before closing, without
generating an HTTP failure or fabricating a source observation.
"""
from datetime import datetime,timezone,timedelta
import sys

from sushiwait import cli


def guard_fetch(original,start,end, *, clock=None):
    clock=(lambda:datetime.now(timezone.utc)) if clock is None else clock
    def fetch(client,store_id):
        now=clock()
        if now<start:
            raise SystemExit(2)
        if now>=end-timedelta(seconds=16):
            raise SystemExit(0)
        return original(client,store_id)
    return fetch


def main():
    if len(sys.argv)<4 or sys.argv[3]!='collect':
        return 2
    try:
        start,end=map(datetime.fromisoformat,sys.argv[1:3])
        if start.utcoffset() is None or end.utcoffset() is None or not 0<(end-start).total_seconds()<=41400:
            return 2
    except (ValueError,TypeError):
        return 2
    # This process imports the installed wheel (-I); the source class is not
    # changed on disk or in the parent/supplier/statistics processes.
    cli.SushiroClient.fetch_store=guard_fetch(cli.SushiroClient.fetch_store,start,end)
    return cli.main(sys.argv[3:])


if __name__=='__main__':
    raise SystemExit(main())
