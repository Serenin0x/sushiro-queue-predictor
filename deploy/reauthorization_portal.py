"""Owner-only mobile reauthorization broker; never exposes the YYB console.

Public capabilities live in URL fragments, not request paths or server logs.
Only fixed, original-account update routes are used on the loopback supplier.
Normal owner console login is forwarded once; passwords are never persisted.
"""
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from sushiwait.credentials import _read_private_file
from sushiwait.packets import encoded


def sibling(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


recovery = sibling('official_reauthorization')
UTC = timezone.utc
ORIGIN = 'https://124.222.57.236'
CAP_SECONDS = 24 * 3600
SESSION_SECONDS = 7 * 24 * 3600
TOKEN = re.compile(r'[A-Za-z0-9_-]{32}\Z')


class PortalError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


class Upstream:
    """Fixed loopback routes; remote message/cookies never reach the browser."""
    def __init__(self, root, owner_username, account_ref='1'):
        self.root, self.owner, self.ref = Path(root), owner_username, account_ref
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_args, **_kwargs):
                return None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def cookie(self):
        path = self.root / 'console-session.private.json'
        if not path.exists():
            raise PortalError('login_required', 401)
        value = recovery.read(path)
        if (set(value) != {'schema_version', 'cookie', 'until'} or value['schema_version'] != 1
                or type(value['cookie']) is not str
                or not re.fullmatch(r'[A-Za-z0-9_-]{20,512}', value['cookie'])
                or time.time() >= value['until']):
            raise PortalError('login_required', 401)
        return value['cookie']

    def request(self, path, data=None, *, authenticated=False, image=False):
        allowed = (path in {'/login', '/api/account-links'}
            or re.fullmatch(r'/account-link/[A-Za-z0-9_-]{32}/qr(?:/[A-Za-z0-9_-]{1,128}/(?:image|poll|confirm))?', path))
        if not allowed:
            raise PortalError('upstream_route_rejected')
        headers = {'Content-Type': 'application/json'}
        if authenticated:
            headers['Cookie'] = 'yyb_session=' + self.cookie()
        request = urllib.request.Request('http://127.0.0.1:18081' + path,
            data=encoded(data) if data is not None else None, headers=headers,
            method='POST' if data is not None else 'GET')
        try:
            response = self.opener.open(request, timeout=45)
        except urllib.error.HTTPError as error:
            if error.code == 401:
                raise PortalError('login_required', 401) from None
            if error.code == 410:
                raise PortalError('qr_expired', 410) from None
            if path == '/api/account-links' and error.code == 409:
                value = json.loads(error.read(16385))
                candidates = value.get('data', {}).get('existing', [])
                for item in candidates:
                    if item.get('kind') == 'update' and str(item.get('account_id')) == self.ref:
                        return {'url': item['url']}, None
            if error.code == 409:
                message = ''
                if path.endswith('/confirm'):
                    try:
                        message = json.loads(error.read(16385)).get('msg', '')
                    except (ValueError, UnicodeError):
                        pass
                if '请使用目标账号扫码' in message:
                    raise PortalError('account_mismatch', 409) from None
                raise PortalError('confirmation_not_ready', 409) from None
            raise PortalError('upstream_unavailable', 502) from None
        except OSError:
            raise PortalError('upstream_unavailable', 502) from None
        with response:
            raw = response.read(1048577 if image else 16385)
            if len(raw) > (1048576 if image else 16384):
                raise PortalError('upstream_invalid', 502)
            if image:
                mime = response.headers.get_content_type()
                if mime not in {'image/png', 'image/jpeg'}:
                    raise PortalError('upstream_invalid', 502)
                return raw, mime
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeError):
                raise PortalError('upstream_invalid', 502) from None
            if type(value) is not dict or value.get('code') != 0:
                raise PortalError('upstream_invalid', 502)
            return value.get('data'), response.headers.get('Set-Cookie')

    def login(self, password):
        if type(password) is not str or not 1 <= len(password) <= 512:
            raise PortalError('login_required', 401)
        _, header = self.request('/login', {'username': self.owner, 'password': password})
        cookie = SimpleCookie()
        try:
            cookie.load(header or '')
            value = cookie['yyb_session'].value
            if not re.fullmatch(r'[A-Za-z0-9_-]{20,512}', value):
                raise ValueError
        except Exception:
            raise PortalError('login_required', 401) from None
        atomic_json(self.root / 'console-session.private.json', {
            'schema_version': 1, 'cookie': value, 'until': time.time() + SESSION_SECONDS})

    def create_link(self):
        data, _ = self.request('/api/account-links',
            {'kind': 'update', 'ref': self.ref, 'ttl_seconds': 1800}, authenticated=True)
        match = re.fullmatch(r'http://127\.0\.0\.1:18081/account-link/([A-Za-z0-9_-]{32})', data.get('url', ''))
        if not match:
            raise PortalError('upstream_invalid', 502)
        return match[1]


def atomic_json(path, value):
    pending = path.with_suffix('.pending')
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(encoded(value)); stream.flush(); os.fsync(stream.fileno())
    os.replace(pending, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Broker:
    def __init__(self, root, collection_root, owner_username, *, upstream=None, clock=time.time):
        self.root, self.collection_root = Path(root), Path(collection_root)
        if any(not p.is_absolute() or p.is_symlink() or not p.is_dir()
               or p.stat().st_mode & 0o777 != 0o700 for p in (self.root, self.collection_root)):
            raise ValueError('portal_directory_unsafe')
        self.clock = clock
        self.lock = threading.RLock()
        self.upstream = upstream or Upstream(root, owner_username)
        self.receipts = self.root / 'receipts'
        self.receipts.mkdir(mode=0o700, exist_ok=True)
        if self.receipts.is_symlink() or self.receipts.stat().st_mode & 0o777 != 0o700:
            raise ValueError('portal_directory_unsafe')
        self.key = _read_private_file(self.root / 'receipt-signing.key')
        if len(self.key) != 32:
            raise ValueError('portal_signing_key_invalid')
        path = self.root / 'portal.sqlite3'
        if path.is_symlink() or path.exists() and path.stat().st_mode & 0o777 != 0o600:
            raise ValueError('portal_database_unsafe')
        self.db = sqlite3.connect(path, check_same_thread=False)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS capabilities ('
            'id TEXT PRIMARY KEY, token_hash TEXT UNIQUE, token TEXT, created REAL, expires REAL,'
            'block_hash TEXT, mode TEXT, state TEXT, link TEXT, sid TEXT, qr_at REAL,'
            'creates INTEGER DEFAULT 0, confirms INTEGER DEFAULT 0, login_attempts INTEGER DEFAULT 0,'
            'receipt_id TEXT, confirmed_at REAL)')
        self.db.commit()

    def close(self):
        self.db.close()

    def issue(self, *, verification=False):
        with self.lock:
            block = recovery.active_block(self.collection_root)
            if verification and block and not recovery.requires_scan(self.collection_root, block):
                raise PortalError('reauthorization_not_required', 409)
            if not verification and not recovery.requires_scan(self.collection_root, block):
                raise PortalError('reauthorization_not_required', 409)
            target = block['sha256'] if block else None
            mode = 'verify' if verification and block is None else 'incident'
            now = self.clock()
            row = self.db.execute('SELECT * FROM capabilities WHERE block_hash IS ? AND mode=? '
                'AND expires>? AND state NOT IN (?,?) ORDER BY created DESC LIMIT 1',
                (target, mode, now, 'authorized', 'resumed')).fetchone()
            if row is None:
                token, identity = secrets.token_urlsafe(32), uuid.uuid4().hex
                self.db.execute('INSERT INTO capabilities '
                    '(id,token_hash,token,created,expires,block_hash,mode,state) VALUES (?,?,?,?,?,?,?,?)',
                    (identity, hashlib.sha256(token.encode()).hexdigest(), token, now,
                     now + CAP_SECONDS, target, mode, 'issued'))
                self.db.commit()
            else:
                token = row['token']
            return ORIGIN + '/recover/#' + token

    def cap(self, token):
        if type(token) is not str or not re.fullmatch('[A-Za-z0-9_-]{43}', token):
            raise PortalError('link_invalid', 410)
        row = self.db.execute('SELECT * FROM capabilities WHERE token_hash=?',
                             (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if row is None or self.clock() >= row['expires']:
            raise PortalError('link_expired', 410)
        return row

    def update(self, identity, **values):
        allowed = {'state', 'link', 'sid', 'qr_at', 'creates', 'confirms', 'login_attempts', 'receipt_id', 'confirmed_at'}
        if not set(values) <= allowed:
            raise ValueError('portal_field_invalid')
        self.db.execute('UPDATE capabilities SET ' + ','.join(k+'=?' for k in values) + ' WHERE id=?',
                        (*values.values(), identity))
        self.db.commit()

    def status(self, token):
        with self.lock:
            row = self.cap(token)
            state = row['state']
            next_open = None
            if state == 'authorized' and row['block_hash']:
                if recovery.active_block(self.collection_root) is None:
                    saved = recovery.read(self.collection_root / 'collection-status.json')
                    if (saved.get('state') == 'running' and saved.get('last_progress_at')
                            and datetime.fromisoformat(saved['last_progress_at'].replace('Z', '+00:00')).timestamp() >= row['confirmed_at']):
                        state = 'resumed'
                        self.update(row['id'], state=state)
                    elif saved.get('state') == 'closed' and saved.get('next_open_at'):
                        state, next_open = 'scheduled', saved['next_open_at']
            try:
                self.upstream.cookie()
                login = False
            except PortalError:
                login = True
            return {'state': state, 'login_required': login, 'verification': row['mode'] == 'verify',
                    'expires_at': row['expires'], 'next_open_at': next_open}

    def login(self, token, password):
        with self.lock:
            row = self.cap(token)
            if row['login_attempts'] >= 5:
                raise PortalError('login_rate_limited', 429)
            self.update(row['id'], login_attempts=row['login_attempts'] + 1)
            self.upstream.login(password)
            return {'ok': True}

    def qr(self, token):
        with self.lock:
            row = self.cap(token)
            if row['state'] in {'authorized', 'resumed', 'confirmation_unknown'}:
                raise PortalError('authorization_already_completed', 409)
            if row['sid'] and row['qr_at'] > self.clock() - 150:
                return {'ready': True, 'image_url': '/recover/image/' + row['id'],
                        'expires_at': min(row['expires'], row['qr_at'] + 180)}
            if row['creates'] >= 12:
                raise PortalError('qr_rate_limited', 429)
            link = row['link'] or self.upstream.create_link()
            try:
                data, _ = self.upstream.request('/account-link/' + link + '/qr', {})
            except PortalError as error:
                if error.code != 'qr_expired':
                    raise
                link = self.upstream.create_link()
                data, _ = self.upstream.request('/account-link/' + link + '/qr', {})
            sid = data.get('session_id')
            if type(sid) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,128}', sid):
                raise PortalError('upstream_invalid', 502)
            self.update(row['id'], state='qr_pending', link=link, sid=sid,
                        qr_at=self.clock(), creates=row['creates'] + 1)
            return {'ready': True, 'image_url': '/recover/image/' + row['id'],
                    'expires_at': min(row['expires'], self.clock() + 180)}

    def image_ticket(self, identity):
        with self.lock:
            if not re.fullmatch('[0-9a-f]{32}', identity):
                raise PortalError('not_found', 404)
            row = self.db.execute('SELECT * FROM capabilities WHERE id=?', (identity,)).fetchone()
            if (row is None or row['state'] != 'qr_pending' or not row['qr_at']
                    or self.clock() >= min(row['expires'], row['qr_at'] + 180)):
                raise PortalError('qr_expired', 410)
            return self.image(row['token'])

    def image(self, token):
        with self.lock:
            row = self.cap(token)
            if row['state'] != 'qr_pending' or not row['link'] or not row['sid']:
                raise PortalError('qr_expired', 410)
            return self.upstream.request('/account-link/' + row['link'] + '/qr/' + row['sid'] + '/image', image=True)

    def poll(self, token):
        with self.lock:
            row = self.cap(token)
            if row['state'] in {'authorized', 'resumed', 'confirmation_unknown'}:
                return self.status(token)
            if row['state'] != 'qr_pending' or not row['link'] or not row['sid']:
                raise PortalError('qr_expired', 410)
            prefix = '/account-link/' + row['link'] + '/qr/' + row['sid']
            data, _ = self.upstream.request(prefix + '/poll')
            state = data.get('status')
            if state not in {'pending', 'scanned', 'authorized', 'confirmed', 'expired', 'cancelled', 'unknown'}:
                raise PortalError('upstream_invalid', 502)
            if state not in {'authorized', 'confirmed'}:
                if state in {'expired', 'cancelled', 'unknown'}:
                    self.update(row['id'], state='issued', sid=None, qr_at=None)
                return {'state': state}
            # A confirmation may already have been accepted if the response was
            # lost. Keep that uncertainty; never issue a fresh login silently.
            if row['confirms'] >= 12:
                raise PortalError('confirmation_not_ready', 409)
            self.update(row['id'], state='confirmation_unknown', confirms=row['confirms'] + 1)
            try:
                account, _ = self.upstream.request(prefix + '/confirm', {})
            except PortalError as error:
                if error.code == 'confirmation_not_ready':
                    self.update(row['id'], state='qr_pending')
                elif error.code == 'account_mismatch':
                    self.update(row['id'], state='issued', sid=None, qr_at=None)
                raise
            if type(account) is not dict or str(account.get('id')) != '1':
                raise PortalError('account_mismatch', 409)
            receipt_id = uuid.uuid4().hex
            if row['block_hash']:
                block = recovery.active_block(self.collection_root)
                if not block or block['sha256'] != row['block_hash']:
                    raise PortalError('incident_changed', 409)
                payload = {'schema_version': 1, 'receipt_id': receipt_id, 'account_ref': '1',
                    'block_sha256': row['block_hash'],
                    'confirmed_at': datetime.fromtimestamp(self.clock(), UTC).isoformat()}
                recovery.put_once(self.receipts / (receipt_id + '.json'), recovery.signed_receipt(payload, self.key))
            self.update(row['id'], state='authorized', receipt_id=receipt_id, confirmed_at=self.clock())
            return self.status(token)


def handler(broker, internal_key):
    assets = Path(__file__).with_name('recovery_web')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, body, mime='application/json; charset=utf-8', attachment=None):
            raw = body if isinstance(body, bytes) else encoded(body)
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            if attachment:
                self.send_header('Content-Disposition', 'attachment; filename="'+attachment+'"')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers(); self.wfile.write(raw)

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if path.startswith(('/recover/image/', '/recover/download/')):
                match = re.fullmatch(r'/recover/(?:image|download)/([0-9a-f]{32})', path)
                if not match:
                    return self.reply(404, {'error': 'not_found'})
                try:
                    download = path.startswith('/recover/download/')
                    raw, mime = broker.image_ticket(match[1])
                    filename = ('sushiwait-authorization.' + ('jpg' if mime == 'image/jpeg' else 'png')) if download else None
                    return self.reply(200, raw, mime, attachment=filename)
                except PortalError as error:
                    return self.reply(error.status, {'error': error.code})
                except Exception:
                    return self.reply(503, {'error': 'portal_unavailable'})
            mapping = {'/recover/': ('index.html', 'text/html; charset=utf-8'),
                       '/recover/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                       '/recover/style.css': ('style.css', 'text/css; charset=utf-8')}
            if path not in mapping:
                return self.reply(404, {'error': 'not_found'})
            name, mime = mapping[path]
            return self.reply(200, (assets / name).read_bytes(), mime)

        def do_POST(self):
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 4096:
                    raise PortalError('invalid_request')
                value = json.loads(self.rfile.read(length))
                if type(value) is not dict:
                    raise PortalError('invalid_request')
                if self.path == '/internal/issue':
                    supplied = self.headers.get('X-SUSHIWAIT-Internal', '')
                    if not hmac.compare_digest(supplied, internal_key.hex()):
                        raise PortalError('not_found', 404)
                    if set(value) != {'verification'} or type(value['verification']) is not bool:
                        raise PortalError('invalid_request')
                    return self.reply(200, {'url': broker.issue(verification=value['verification'])})
                if (self.headers.get('Origin') != ORIGIN
                        or self.headers.get('X-Forwarded-Proto') != 'https'):
                    raise PortalError('invalid_origin', 403)
                if self.path not in {'/recover/api/status', '/recover/api/login', '/recover/api/qr',
                                     '/recover/api/image', '/recover/api/poll'}:
                    raise PortalError('not_found', 404)
                expected = {'cap', 'password'} if self.path.endswith('/login') else {'cap'}
                if set(value) != expected:
                    raise PortalError('invalid_request')
                if self.path.endswith('/status'):
                    result = broker.status(value['cap'])
                elif self.path.endswith('/login'):
                    result = broker.login(value['cap'], value['password'])
                elif self.path.endswith('/qr'):
                    result = broker.qr(value['cap'])
                elif self.path.endswith('/image'):
                    body, mime = broker.image(value['cap'])
                    return self.reply(200, body, mime)
                else:
                    result = broker.poll(value['cap'])
                return self.reply(200, result)
            except PortalError as error:
                return self.reply(error.status, {'error': error.code})
            except Exception:
                return self.reply(503, {'error': 'portal_unavailable'})
    return Handler


def main():
    import sys
    if len(sys.argv) != 2:
        return 2
    root = Path(sys.argv[1])
    os.umask(0o077)
    if not root.is_absolute() or root.is_symlink() or root.stat().st_mode & 0o777 != 0o700:
        return 2
    value = recovery.read(root / 'portal-config.json')
    if (set(value) != {'schema_version', 'owner_username', 'collection_root'}
            or value['schema_version'] != 1 or type(value['owner_username']) is not str
            or not 1 <= len(value['owner_username']) <= 64):
        return 2
    internal_key = _read_private_file(root / 'internal.key')
    if len(internal_key) != 32:
        return 2
    broker = Broker(root, value['collection_root'], value['owner_username'])
    server = ThreadingHTTPServer(('127.0.0.1', 18910), handler(broker, internal_key))
    server.daemon_threads = True
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    try:
        server.serve_forever(poll_interval=1)
    finally:
        server.server_close(); broker.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
