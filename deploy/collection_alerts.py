"""Private, owner-only email or ServerChan alerts from saved status.

No account database, query credential or QR material is read. Delivery is
reserved durably before sending; an ambiguous response is not retried in a loop.
The monitor never restarts a failed collector or alters its journals.
"""
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
import hashlib
import html
import json
import os
from pathlib import Path
import re
import ssl
import smtplib
import urllib.error
import urllib.parse
import urllib.request

from sushiwait.credentials import _read_private_file
from sushiwait.calendar import date_features
from sushiwait.cadence import POLICY, next_sample_at

UTC = timezone.utc
CHINA = timezone(timedelta(hours=8))
STATISTICS_URL = 'https://124.222.57.236/statistics'
REMINDER_SECONDS = 6 * 3600
DAILY_LIMIT = 5
EMAIL_DAILY_LIMIT = 20
START_GRACE_SECONDS = 180
PROGRESS_GRACE_SECONDS = 120
ALERT_POLICY = 'business-window-v2'


def instant(value):
    at = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if at.utcoffset() is None:
        raise ValueError('alert_time_invalid')
    return at.astimezone(UTC)


def read_status(path):
    raw = _read_private_file(path)
    if len(raw) > 8192:
        raise ValueError('alert_status_invalid')
    value = json.loads(raw)
    if type(value) is not dict:
        raise ValueError('alert_status_invalid')
    return value


def business_window(now):
    """Independent expected window; a fresh 'closed' heartbeat cannot hide it."""
    local = now.astimezone(CHINA)
    features = date_features(local.isoformat(), as_of=local.isoformat())
    early = local.weekday() >= 5 or features['date_type'] == 'holiday'
    return (local.replace(hour=10 if early else 11, minute=30 if early else 0,
                          second=0, microsecond=0),
            local.replace(hour=22, minute=0, second=0, microsecond=0))


def condition(collection, projection, now):
    """Return fixed public categories, never arbitrary diagnostic strings."""
    if now.utcoffset() is None:
        raise ValueError('alert_time_invalid')
    try:
        start, end = business_window(now)
        if collection['state'] == 'needs_attention':
            return 'collection_stopped'
        if not start <= now < end:
            return 'closed'
        age = (now - instant(collection['at'])).total_seconds()
        if age < -60 or age > 120:
            return 'collection_heartbeat_stale'
        startup = ('starting' if (now-start).total_seconds() <= START_GRACE_SECONDS
                   else 'collection_not_started')
        if collection['state'] in {'closed', 'completed'}:
            return startup
        if collection['state'] not in {'starting', 'running'}:
            return 'collection_status_unavailable'
        if collection['state'] == 'starting':
            return startup
        if type(collection.get('failed_slots')) is not int or collection['failed_slots']:
            return 'collection_stopped'
        progress = collection.get('last_progress_at')
        if progress is None or collection.get('successful_slots') == 0:
            return startup
        progress = instant(progress)
        if progress < start:
            return startup
        if (now-progress).total_seconds() < -60:
            return 'collection_status_unavailable'
        cadence = collection.get('cadence')
        if cadence not in {None, POLICY}:
            return 'collection_status_unavailable'
        due = next_sample_at(progress) if cadence else progress + timedelta(seconds=600)
        if now > due + timedelta(seconds=PROGRESS_GRACE_SECONDS):
            return 'collection_progress_stale'
        if projection.get('state') != 'running':
            return 'public_sync_stopped'
        sync_age = (now - instant(projection['at'])).total_seconds()
        if sync_age < -60 or sync_age > 120:
            return 'public_sync_stopped'
        return 'healthy'
    except (KeyError, TypeError, ValueError, OverflowError):
        return 'collection_status_unavailable'


def validate_config(value):
    if type(value) is dict and value.get('provider') == 'qq_smtp':
        fields = {'schema_version', 'provider', 'from_address', 'to_address',
                  'authorization_code', 'statistics_url'}
        address = value.get('from_address')
        if (set(value) != fields or value['schema_version'] != 1
                or type(address) is not str or not re.fullmatch(r'[A-Za-z0-9._+-]{1,64}@qq\.com', address)
                or value['to_address'] != address or value['statistics_url'] != STATISTICS_URL
                or type(value['authorization_code']) is not str
                or not re.fullmatch(r'[A-Za-z0-9]{16,128}', value['authorization_code'])):
            raise ValueError('alert_config_invalid')
        return value
    if (type(value) is not dict
            or set(value) != {'schema_version', 'provider', 'sendkey', 'statistics_url'}
            or value['schema_version'] != 1 or value['provider'] != 'serverchan_turbo'
            or value['statistics_url'] != STATISTICS_URL
            or type(value['sendkey']) is not str
            or not re.fullmatch(r'SCT[A-Za-z0-9_-]{16,252}', value['sendkey'])):
        raise ValueError('alert_config_invalid')
    return value


def message(kind, reason, now, recovery_link=None):
    labels = {
        'collection_stopped': '采集服务已暂停，需要检查授权或来源错误。',
        'collection_heartbeat_stale': '采集状态超过两分钟没有更新。',
        'collection_not_started': '开店已超过三分钟，今天仍未保存首组采集数据。',
        'collection_progress_stale': '营业时间内，超过计划采样时间两分钟仍未保存新样本。',
        'public_sync_stopped': '采集数据的网页同步需要检查。',
        'collection_status_unavailable': '暂时无法读取采集状态。',
    }
    if kind == 'recovered':
        title, description = 'SUSHIWAIT 采集已恢复', '新样本已保存，公共同步正在运行。'
    else:
        title = 'SUSHIWAIT 采集需要处理' if kind == 'incident' else 'SUSHIWAIT 采集仍待处理'
        description = labels[reason]
    stamp = now.astimezone(CHINA).strftime('%Y-%m-%d %H:%M:%S')
    body = f'{description}\n\n北京时间：{stamp}\n\n[查看统计与采样状态]({STATISTICS_URL})'
    if recovery_link is not None:
        if (kind not in {'incident', 'reminder'} or reason != 'collection_stopped'
                or type(recovery_link) is not str
                or not re.fullmatch(r'https://124\.222\.57\.236/recover/#[A-Za-z0-9_-]{43}', recovery_link)):
            raise ValueError('alert_recovery_link_invalid')
        body += '\n\n[重新授权并自动恢复采集](' + recovery_link + ')\n\n请勿转发此专用链接。'
    return title, body


def serverchan_send(config, title, body):
    """Only the owner's bound channel; never a recipient or group parameter."""
    data = urllib.parse.urlencode({'title': title, 'desp': body}).encode()
    request = urllib.request.Request(
        'https://sctapi.ftqq.com/' + config['sendkey'] + '.send', data=data,
        headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
    # Disable redirects so an unexpected Location cannot carry the private key
    # or message to another service. TLS validation always remains enabled.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context()), NoRedirect())
    with opener.open(request, timeout=15) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError('alert_delivery_unknown')
    value = json.loads(raw)
    if type(value) is not dict or type(value.get('code')) is not int or value['code'] != 0:
        raise ValueError('alert_delivery_not_accepted')
    return True


def smtp_message(config, title, body):
    """Fixed owner-to-self envelope. HTML only adds the fixed HTTPS links."""
    validate_config(config)
    message = EmailMessage()
    message['From'] = config['from_address']
    message['To'] = config['to_address']
    message['Subject'] = title
    message['Date'] = format_datetime(datetime.now(UTC))
    message.set_content(body)
    paragraphs = []
    for paragraph in body.split('\n\n'):
        match = re.fullmatch(r'\[([^\]\n]{1,64})\]\((https://124\.222\.57\.236/(?:statistics|recover/#[A-Za-z0-9_-]{43}))\)', paragraph)
        if match:
            paragraphs.append('<p><a href="'+html.escape(match[2],quote=True)+'">'+html.escape(match[1])+'</a></p>')
        else:
            paragraphs.append('<p>'+html.escape(paragraph).replace('\n','<br>')+'</p>')
    message.add_alternative('<html><body>'+''.join(paragraphs)+'</body></html>', subtype='html')
    return message


def qq_smtp_send(config, title, body):
    message = smtp_message(config, title, body)
    # SMTP only. No IMAP/POP or inbox access; no recipient/group override.
    with smtplib.SMTP_SSL('smtp.qq.com',465,timeout=15,context=ssl.create_default_context()) as client:
        client.login(config['from_address'], config['authorization_code'])
        refused = client.send_message(message, from_addr=config['from_address'], to_addrs=[config['to_address']])
        if refused:
            raise ValueError('alert_delivery_not_accepted')
    return True


def notify_send(config, title, body):
    validate_config(config)
    return qq_smtp_send(config,title,body) if config['provider']=='qq_smtp' else serverchan_send(config,title,body)


def save_state(path, value):
    pending = path.with_suffix('.pending')
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(json.dumps(value, sort_keys=True).encode())
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Monitor:
    """Call under one exclusive worker lock. No account access is needed."""
    def __init__(self, state_file, config, sender=notify_send):
        self.path = Path(state_file)
        self.config = validate_config(config)
        self.sender = sender
        self.daily_limit = EMAIL_DAILY_LIMIT if config['provider'] == 'qq_smtp' else DAILY_LIMIT
        identity = (config['sendkey'] if config['provider']=='serverchan_turbo'
                    else config['provider']+'\0'+config['from_address']+'\0'+config['to_address'])
        self.pin = hashlib.sha256(identity.encode()).hexdigest()
        self.state = {'schema_version': 1, 'channel_sha256': self.pin,
                      'incident_since': None, 'reason': None, 'last_attempt_at': None,
                      'day': None, 'attempts_today': 0, 'delivery': None}
        if self.path.exists():
            value = read_status(self.path)
            if (set(value) != set(self.state) or value['schema_version'] != 1
                    or value['channel_sha256'] != self.pin
                    or type(value['attempts_today']) is not int
                    or not 0 <= value['attempts_today'] <= self.daily_limit
                    or value['delivery'] not in {None, 'pending', 'accepted', 'unknown'}):
                raise ValueError('alert_state_invalid')
            for field in ('incident_since', 'last_attempt_at'):
                if value[field] is not None:
                    instant(value[field])
            self.state = value

    def step(self, status, now, recovery_link=None):
        day = now.astimezone(CHINA).date().isoformat()
        if self.state['day'] != day:
            self.state.update(day=day, attempts_today=0)
        if status in {'starting', 'closed'}:
            save_state(self.path, self.state)
            return 'idle'
        kind = None
        if status == 'healthy':
            if self.state['incident_since'] is not None:
                kind = 'recovered'
        elif self.state['incident_since'] is None:
            self.state.update(incident_since=now.isoformat(), reason=status)
            kind = 'incident'
        elif status in {'collection_stopped', 'collection_not_started'} and status != self.state['reason']:
            # A startup/heartbeat incident must not delay a later owner-action
            # alert (and its authorization link) for the six-hour reminder gap.
            self.state['reason'] = status
            kind = 'incident'
        elif self.state['last_attempt_at'] is None or (
                now - instant(self.state['last_attempt_at'])).total_seconds() >= REMINDER_SECONDS:
            kind = 'reminder'
        if kind is None or self.state['attempts_today'] >= self.daily_limit:
            save_state(self.path, self.state)
            return 'suppressed'
        # Issue a link only when a message is actually due, never every minute.
        link = recovery_link() if callable(recovery_link) and kind != 'recovered' else None
        title, body = message(kind, status, now, link)
        # Persist reservation first: process death or a timeout must not cause
        # a duplicate send every minute after restart.
        self.state.update(last_attempt_at=now.isoformat(), delivery='pending')
        self.state['attempts_today'] += 1
        if kind == 'recovered':
            self.state.update(incident_since=None, reason=None)
        save_state(self.path, self.state)
        try:
            if self.sender(self.config, title, body) is not True:
                raise ValueError('alert_delivery_unknown')
            self.state['delivery'] = 'accepted'
        except Exception:
            self.state['delivery'] = 'unknown'
        save_state(self.path, self.state)
        return self.state['delivery']
