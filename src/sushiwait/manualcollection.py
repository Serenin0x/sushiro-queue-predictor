"""Owner-authenticated requests to the existing collector, never a second writer.

Only the collector may turn queued targets into source observations. Requests,
sessions and progress have a separate private durable database; source journals,
isolation, cadence and query allowances remain authoritative.
"""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from http.cookies import SimpleCookie
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
import urllib.request

from .credentials import _read_private_file
from .cadence import next_sample_at, phase_between

UTC = timezone.utc
COOKIE = '__Host-sushiwait-collection'
SESSION_SECONDS = 7 * 86400
JOB_SECONDS = 1800
IDENTITY = re.compile(r'[0-9a-f]{32}\Z')


class ManualError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def stamp(at):
    if not isinstance(at, datetime) or at.utcoffset() is None:
        raise ValueError('manual_clock_invalid')
    return at.astimezone(UTC).isoformat()


def instant(text):
    value = datetime.fromisoformat(text.replace('Z', '+00:00'))
    if value.utcoffset() is None:
        raise ValueError('manual_clock_invalid')
    return value


def settings(path):
    value = json.loads(_read_private_file(path))
    if (type(value) is not dict or set(value) != {'schema_version','queue_file','owner_username'}
            or value['schema_version'] != 1 or type(value['owner_username']) is not str
            or not 1 <= len(value['owner_username']) <= 64
            or any(ord(c) < 32 for c in value['owner_username'])
            or not Path(value['queue_file']).is_absolute()):
        raise ValueError('manual_settings_invalid')
    return value


def verify_owner(username, password):
    """Check the existing console password on one fixed loopback login route.

    No password/cookie is retained or returned; this grants only manual collection.
    """
    if type(password) is not str or not 1 <= len(password) <= 512:
        return False
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request('http://127.0.0.1:18081/login',
        data=json.dumps({'username':username,'password':password}).encode(),
        headers={'Content-Type':'application/json'}, method='POST')
    try:
        with opener.open(request, timeout=8) as response:
            raw = response.read(16385)
            cookie = SimpleCookie();cookie.load(response.headers.get('Set-Cookie',''))
            return (len(raw) <= 16384 and json.loads(raw).get('code') == 0
                    and bool(re.fullmatch(r'[A-Za-z0-9_-]{20,512}',cookie['yyb_session'].value)))
    except Exception:
        return False


def manual_capacity(fleet, now):
    """Reserve the rest of today's automatic schedule before advancing a sample.

    A manual request can use spare capacity left by actual timing/outages. When
    none is left it joins the next scheduled observation instead of exhausting
    the immutable daily allowance early.
    """
    end=instant(fleet.plan['windows'][0]['end'])-timedelta(seconds=16)
    rounds=fleet.plan['ordinary_query_limit']//len(fleet.plan['store_ids'])
    def demand(start):
        return max(0,math.ceil(phase_between(start,end,fleet.plan['cadence'])-1e-10)) if start<end else 0
    after=demand(next_sample_at(now,fleet.plan['cadence']))
    reserved={store:demand(max(now,instant(item[0]))) for store,item in fleet.value['stores'].items()}
    total=sum(reserved.values());remaining=fleet.plan['ordinary_query_limit']-fleet.value['issued']-1
    return {store for store,item in fleet.value['stores'].items()
            if rounds-item[1]-1>=after and remaining>=total-reserved[store]+after}


class ManualCollection:
    def __init__(self, path, store_ids, *, clock=time.time):
        self.path, self.stores, self.clock = Path(path), tuple(store_ids), clock
        if (not self.path.is_absolute() or not 1 <= len(self.stores) <= 128
                or len(set(self.stores)) != len(self.stores)
                or any(not re.fullmatch(r'[1-9]\d{0,9}',s) for s in self.stores)):
            raise ValueError('manual_scope_invalid')
        with self.connect() as db:
            version=db.execute('PRAGMA user_version').fetchone()[0]
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if version not in (0,1) or (tables!={'sessions','logins','jobs','targets'} if version==1 else bool(tables)):
                raise ValueError('manual_database_schema')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sessions (digest TEXT PRIMARY KEY, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS logins (peer TEXT PRIMARY KEY, since REAL NOT NULL, attempts INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, store_id TEXT, created TEXT NOT NULL,
                    state TEXT NOT NULL, finished TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS targets (job_id TEXT NOT NULL, store_id TEXT NOT NULL,
                    state TEXT NOT NULL, received TEXT, started TEXT, error TEXT, PRIMARY KEY(job_id,store_id));
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_job ON jobs((1)) WHERE state='running';
                PRAGMA user_version=1;
            ''')

    @contextmanager
    def connect(self):
        parent = self.path.parent
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
                or info.st_uid != os.geteuid() or parent.is_symlink()):
            raise ValueError('manual_directory_unsafe')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_nlink != 1 or info.st_uid != os.geteuid()):
                raise ValueError('manual_database_unsafe')
        finally:
            os.close(fd)
        db = sqlite3.connect(self.path, timeout=.3)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback();raise
        finally:
            db.close()

    def login(self, peer, password, verify):
        # Reserve before contacting the console, including timeouts and bad passwords.
        key = hashlib.sha256(peer.encode()).hexdigest();now = self.clock()
        with self.connect() as db:
            row = db.execute('SELECT * FROM logins WHERE peer=?',(key,)).fetchone()
            since, count = (row['since'],row['attempts']) if row and now-row['since']<900 else (now,0)
            total = db.execute('SELECT coalesce(sum(attempts),0) FROM logins WHERE since>?',(now-900,)).fetchone()[0]
            if count >= 5 or total >= 10:
                raise ManualError('manual_login_limited',429)
            db.execute('INSERT OR REPLACE INTO logins VALUES (?,?,?)',(key,since,count+1))
        if not verify(password):
            raise ManualError('manual_login_failed',401)
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute('INSERT INTO sessions VALUES (?,?)',
                (hashlib.sha256(token.encode()).hexdigest(),now+SESSION_SECONDS))
            db.execute('DELETE FROM logins WHERE peer=?',(key,))
        return token

    def authenticate(self, token):
        if type(token) is not str or not re.fullmatch('[A-Za-z0-9_-]{43}',token):
            raise ManualError('manual_login_required',401)
        with self.connect() as db:
            row = db.execute('SELECT expires FROM sessions WHERE digest=?',
                (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if row is None or row[0] <= self.clock():
            raise ManualError('manual_login_required',401)

    def logout(self, token):
        with self.connect() as db:
            db.execute('DELETE FROM sessions WHERE digest=?',(hashlib.sha256(token.encode()).hexdigest(),))

    def _result(self, db, job):
        if job is None:return None
        rows = db.execute('SELECT * FROM targets WHERE job_id=? ORDER BY store_id',(job['id'],)).fetchall()
        return {'request_id':job['id'],'store_id':job['store_id'],'created_at':job['created'],
            'local_date':instant(job['created']).astimezone(__import__('zoneinfo').ZoneInfo('Asia/Shanghai')).date().isoformat(),
            'state':job['state'],'finished_at':job['finished'],'error_code':job['error'],
            'total':len(rows),'successful':sum(r['state']=='saved' for r in rows),
            'failed':sum(r['state']=='failed' for r in rows),'skipped':sum(r['state']=='skipped' for r in rows),
            'pending':sum(r['state']=='queued' for r in rows),
            'last_received_at':max((r['received'] for r in rows if r['received']),default=None),
            'targets':[{'store_id':r['store_id'],'state':r['state'],'received_at':r['received'],'sample_at':r['started'],'error_code':r['error']} for r in rows]}

    def status(self):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs ORDER BY state='running' DESC,created DESC LIMIT 1").fetchone()
            if row and row['state']=='running' and self.clock()-instant(row['created']).timestamp()>=JOB_SECONDS:
                now=stamp(datetime.fromtimestamp(self.clock(),UTC))
                db.execute("UPDATE targets SET state='skipped',error='manual_expired' WHERE job_id=? AND state='queued'",(row['id'],))
                db.execute("UPDATE jobs SET state='completed',finished=?,error='manual_expired' WHERE id=?",(now,row['id']))
                row=db.execute('SELECT * FROM jobs WHERE id=?',(row['id'],)).fetchone()
            return self._result(db,row)

    def submit(self, request_id, store_id, now, *, excluded=()):
        if type(request_id) is not str or not IDENTITY.fullmatch(request_id) or store_id is not None and store_id not in self.stores:
            raise ManualError('manual_invalid_request',400)
        with self.connect() as db:
            old = db.execute('SELECT * FROM jobs WHERE id=?',(request_id,)).fetchone()
            if old:
                if old['store_id'] != store_id:raise ManualError('manual_request_conflict')
                return self._result(db,old)
            active = db.execute("SELECT * FROM jobs WHERE state='running'").fetchone()
            if active:
                # An existing all-store job contains a single-store request too.
                if active['store_id'] is None or active['store_id'] == store_id:
                    return self._result(db,active)
                raise ManualError('manual_job_running')
            selected = [store_id] if store_id else list(self.stores)
            targets = [s for s in selected if s not in excluded]
            if not targets:raise ManualError('manual_no_available_stores')
            previous = db.execute('SELECT created FROM jobs ORDER BY created DESC LIMIT 1').fetchone()
            if previous and (now-instant(previous[0])).total_seconds()<30:
                raise ManualError('manual_recent_request',429)
            db.execute("INSERT INTO jobs VALUES (?,?,?,'running',NULL,NULL)",(request_id,store_id,stamp(now)))
            db.executemany("INSERT INTO targets (job_id,store_id,state) VALUES (?,?,'queued')",[(request_id,s) for s in targets])
            return self._result(db,db.execute('SELECT * FROM jobs WHERE id=?',(request_id,)).fetchone())

    def stop_active(self, reason, now):
        if reason not in {'collection_paused','business_closed','manual_expired'}:
            raise ValueError('manual_reason_invalid')
        with self.connect() as db:
            job = db.execute("SELECT * FROM jobs WHERE state='running'").fetchone()
            if job:
                db.execute("UPDATE targets SET state='skipped',error=? WHERE job_id=? AND state='queued'",(reason,job['id']))
                db.execute("UPDATE jobs SET state='completed',finished=?,error=? WHERE id=?",(stamp(now),reason,job['id']))

    def next_store(self, fleet, now):
        """Reconcile committed observations, then nominate one eligible target.

        The caller still uses Fleet.reserve/observe/reconcile and its one-in-flight
        pacing. This method cannot issue a network request or undo isolation.
        """
        with self.connect() as db:
            job = db.execute("SELECT * FROM jobs WHERE state='running'").fetchone()
            if job is None:return None
            reason = ('manual_expired' if (now-instant(job['created'])).total_seconds()>=JOB_SECONDS
                else 'business_closed' if now>=instant(fleet.plan['windows'][0]['end']) else None)
            # Bounded recovery after a crash between archive commit and progress commit.
            recent = fleet.db.db.execute("SELECT store_id,received_at,ok,json_extract(payload_json,'$.timing.request_started_at') FROM samples WHERE data_origin='live' "
                "AND api_profile='miniapp_gateway' ORDER BY id DESC LIMIT 512").fetchall()
            latest = {}
            for store,received,ok,started in recent:
                if store not in latest and instant(received)>=instant(job['created']):latest[store]=(received,ok,stamp(instant(started or received)))
            rounds = fleet.plan['ordinary_query_limit']//len(fleet.plan['store_ids'])
            targets = db.execute("SELECT store_id FROM targets WHERE job_id=? AND state='queued' ORDER BY rowid",(job['id'],)).fetchall()
            for row in targets:
                store = row[0];item = fleet.value['stores'].get(store)
                if store in latest:
                    received,ok,started = latest[store]
                    db.execute('UPDATE targets SET state=?,received=?,started=?,error=? WHERE job_id=? AND store_id=?',
                        ('saved' if ok else 'failed',received,started,None if ok else 'store_query_failed',job['id'],store))
                elif reason or item is None or item[2]>0 or item[1]>=rounds or fleet.value['issued']>=fleet.plan['ordinary_query_limit']:
                    code = reason or ('store_isolated' if item is None or item[2]>0 else 'query_budget_exhausted')
                    db.execute("UPDATE targets SET state='skipped',error=? WHERE job_id=? AND store_id=?",(code,job['id'],store))
            pending = db.execute("SELECT store_id FROM targets WHERE job_id=? AND state='queued' ORDER BY rowid",(job['id'],)).fetchall()
            if not pending:
                db.execute("UPDATE jobs SET state='completed',finished=?,error=? WHERE id=?",(stamp(now),reason,job['id']))
                return None
            limited = fleet.value['rate_limit_until']
            if limited and now<instant(limited):return None
            capacity=manual_capacity(fleet,now)
            target = next((row[0] for row in pending if row[0] in capacity),None)
            db.execute('UPDATE jobs SET error=? WHERE id=?',(None if target else 'awaiting_automatic_slot',job['id']))
            return target
