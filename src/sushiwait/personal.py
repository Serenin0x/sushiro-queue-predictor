"""Private ordinary-ticket reads from the normally observed mainland gateway.

No creation, cancellation, identity probing, login, notification or prediction.
History IDs come only from this same client's successful current-ticket response.
Normal-client intake reads bounded headers, never request or response bodies.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import http.client
import json
import math
import os
import re
import secrets
import socket
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler, HTTPSHandler

from .auth import describe_authorization
from .capture import _open_parent, _check_parent, CaptureError
from .client import _RejectRedirects, _unique_json_object, _reject_json_constant, _has_business_error
from .credentials import _context, _read_private_file, _identity, _private_file, CredentialError
from .packets import _write_packet, PacketError
from .remote import _LABEL
from .surge import _read_summary, _app, SurgeError

ORIGIN = 'https://sapi.sushiro.com.cn'
STATUS_PATH = '/gateway/wechat/api_auth/2.0/ticket/status'
HISTORY_PATH = '/gateway/wechat/api_auth/2.0/ticket/statusHistory'
_APP_ID = re.compile(r'wx[a-f0-9]{16}\Z')
PURPOSE = 'personal_ticket_read_only'
MAX_BODY = 2 * 1024 * 1024
_HEADERS = {'authorization':'authorization', 'x-app-client':'app_client',
    'x-app-code':'app_code', 'user-agent':'user_agent', 'referer':'referer', 'content-type':'content_type'}
_FIELDS = {'schema_version','purpose','api_profile','revision','observed_at','app_id',*_HEADERS.values()}
_ERRORS = {'personal_context_invalid','personal_context_unsafe','personal_context_unavailable',
    'personal_auth_expired','personal_auth_expiring','personal_auth_unknown','personal_response_invalid',
    'personal_http_error','personal_network_error','personal_tls_error','personal_timeout',
    'personal_redirect_blocked','personal_response_too_large','personal_business_error',
    'personal_history_invalid','personal_output_exists','personal_output_unsafe','personal_write_failed',
    'personal_context_conflict','personal_intake_invalid','personal_intake_timeout',
    'personal_intake_ambiguous','personal_runtime_unavailable'}


class PersonalError(ValueError):
    def __init__(self, code, *, committed=False):
        self.error_code = code if code in _ERRORS else 'personal_response_invalid'
        self.committed = committed
        super().__init__(self.error_code)


def _clock(value=None):
    value = datetime.now(timezone.utc) if value is None else value
    if not isinstance(value,datetime) or value.utcoffset() is None:
        raise PersonalError('personal_context_invalid')
    return value.astimezone(timezone.utc)


def _time(value):
    try:
        if not isinstance(value,str) or len(value)>80: raise ValueError()
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.utcoffset() is None: raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (ValueError,TypeError,OverflowError):
        raise PersonalError('personal_context_invalid') from None


def _text(value, maximum=80):
    if (not isinstance(value,str) or not value or len(value)>maximum
            or any(ord(c)<32 or ord(c)==127 for c in value)):
        raise PersonalError('personal_response_invalid')
    return value


def _integer(value, lower=0, upper=1_000_000):
    if type(value) is not int or not lower<=value<=upper:
        raise PersonalError('personal_response_invalid')
    return value


def _guard(authorization, now=None):
    status=describe_authorization(authorization,now=_clock(now))
    if status['expired'] is True: raise PersonalError('personal_auth_expired')
    if status['expiry_source']!='unverified_claim' or status['remaining_seconds'] is None:
        raise PersonalError('personal_auth_unknown')
    if status['remaining_seconds']<=30: raise PersonalError('personal_auth_expiring')
    return status


@dataclass(frozen=True)
class PersonalContext:
    revision: int
    observed_at: str = field(repr=False)
    app_id: str = field(repr=False)
    authorization: str = field(repr=False)
    app_client: str = field(repr=False)
    app_code: str = field(repr=False)
    user_agent: str = field(repr=False)
    referer: str = field(repr=False)
    content_type: str = field(repr=False)

    def private_document(self):
        return {'schema_version':1,'purpose':PURPOSE,'api_profile':'personal_gateway',
            **{k:getattr(self,k) for k in ('revision','observed_at','app_id',*_HEADERS.values())}}


def context_from_document(value, *, now=None):
    now=_clock(now)
    try:
        if (not isinstance(value,dict) or set(value)!=_FIELDS
                or type(value['schema_version']) is not int or value['schema_version']!=1
                or value['purpose']!=PURPOSE or value['api_profile']!='personal_gateway'
                or type(value['revision']) is not int or not 1<=value['revision']<=1_000_000
                or not isinstance(value['app_id'],str) or not _APP_ID.fullmatch(value['app_id'])
                or _time(value['observed_at'])>now):
            raise PersonalError('personal_context_invalid')
        validated=_context('miniapp_gateway',None,value['authorization'],value)
        if (any(getattr(validated,k) is None for k in _HEADERS.values())
                or _app(validated.referer)!=value['app_id']):
            raise PersonalError('personal_context_invalid')
        return PersonalContext(value['revision'],value['observed_at'],value['app_id'],
            **{k:getattr(validated,k) for k in _HEADERS.values()})
    except (CredentialError,KeyError,TypeError,ValueError) as error:
        if isinstance(error,PersonalError): raise
        raise PersonalError('personal_context_invalid') from None


def read_personal_context(path, *, now=None):
    try:
        body=_read_private_file(path)
        value=json.loads(body.decode('utf8'),object_pairs_hook=_unique_json_object,
                         parse_constant=_reject_json_constant)
    except CredentialError as error:
        code=('personal_context_unsafe' if 'unsafe' in error.error_code or 'changed' in error.error_code
              else 'personal_context_unavailable')
        raise PersonalError(code) from None
    except (ValueError,UnicodeError,RecursionError):
        raise PersonalError('personal_context_invalid') from None
    return context_from_document(value,now=now)


def _ticket(value):
    if not isinstance(value,dict) or not {'TICKET_DETAIL','STORE_INFO','TICKET_TYPE'}<=set(value):
        raise PersonalError('personal_response_invalid')
    detail,store=value['TICKET_DETAIL'],value['STORE_INFO']
    if not isinstance(detail,dict) or not isinstance(store,dict):
        raise PersonalError('personal_response_invalid')
    required={'status','number','queueTime','storeId','checkedIn','tableType','numAdult',
              'numChild','ticketId','wait','queueDate'}
    if not required<=set(detail): raise PersonalError('personal_response_invalid')
    number=_text(detail['number'],24)
    store_id=_text(detail['storeId'],19)
    status=_text(detail['status'],32)
    if (not _LABEL.fullmatch(number) or not re.fullmatch(r'[1-9][0-9]{0,18}',store_id)
            or int(store_id)>2**63-1 or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,31}',status)
            or type(detail['checkedIn']) is not bool or type(store.get('id')) is not int
            or store['id']!=int(store_id)):
        raise PersonalError('personal_response_invalid')
    party={'numAdult':_integer(detail['numAdult'],0,100),'numChild':_integer(detail['numChild'],0,100)}
    if sum(party.values())<1: raise PersonalError('personal_response_invalid')
    ticket={'status':status,'number':number,'storeId':store_id,
        'ticketId':_integer(detail['ticketId'],1,2**63-1),'checkedIn':detail['checkedIn'],
        'tableType':_text(detail['tableType'],16),**party,
        'queueDate_raw':_text(detail['queueDate']),'queueTime_raw':_text(detail['queueTime']),
        'wait_raw':_integer(detail['wait'],-1),'wait_unit':'unknown',
        'ticket_type':_text(value['TICKET_TYPE'],32),
        'store_timezone_raw':_text(store['timezone']) if 'timezone' in store else None,
        'store_wait_values':{k:_integer(store[k],-1) for k in
            ('wait','waitingGroup','waitingGroupTable','waitingGroupCounter','waitingGroupPair') if k in store},
        'position_verified':False,'issued_time_verified':False}
    return ticket


def normalize_current(value):
    if isinstance(value,dict) and _has_business_error(value):
        raise PersonalError('personal_business_error')
    if not isinstance(value,dict) or not {'netTicket','reservationTicket'}<=set(value):
        raise PersonalError('personal_response_invalid')
    if value['reservationTicket'] is not None and not isinstance(value['reservationTicket'],dict):
        raise PersonalError('personal_response_invalid')
    return {'ordinary_ticket':None if value['netTicket'] is None else _ticket(value['netTicket']),
            'reservation_ticket_present':value['reservationTicket'] is not None}


def normalize_history(value):
    if isinstance(value,dict) and _has_business_error(value):
        raise PersonalError('personal_business_error')
    if not isinstance(value,dict) or not isinstance(value.get('ticketStatusHistory'),list) or len(value['ticketStatusHistory'])>256:
        raise PersonalError('personal_history_invalid')
    events=[]
    for row in value['ticketStatusHistory']:
        try:
            if not isinstance(row,dict): raise ValueError()
            status=_text(row['status'],32); raw=_text(row['timestamp'])
            if (not re.fullmatch(r'[A-Z][A-Z0-9_]{0,31}',status)
                    or not re.match(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}',raw)): raise ValueError()
            date=datetime.fromisoformat(raw.replace('Z','+00:00'))
            utc=None if date.utcoffset() is None else date.astimezone(timezone.utc).isoformat()
        except (PersonalError,KeyError,TypeError,ValueError,OverflowError):
            raise PersonalError('personal_history_invalid') from None
        events.append({'status_raw':status,'timestamp_raw':raw,
            'timezone_present':utc is not None,'timestamp_utc':utc,'event_meaning_verified':False})
    return events


class PersonalClient:
    """At most two single-attempt GETs, using one explicitly private own context."""
    def __init__(self, context, *, opener=None, clock=None, timeout=15):
        if not isinstance(context,PersonalContext) or type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<timeout<=15:
            raise PersonalError('personal_context_invalid')
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.context=context_from_document(context.private_document(),now=self.clock())
        self.opener,self.timeout=opener,timeout
        self.custom_transport=opener is not None

    def _get(self, path, ticket_id=None):
        if path not in (STATUS_PATH,HISTORY_PATH) or ((path==STATUS_PATH)!=(ticket_id is None)):
            raise PersonalError('personal_response_invalid')
        if ticket_id is not None:_integer(ticket_id,1,2**63-1)
        _guard(self.context.authorization,self.clock())
        url=ORIGIN+path+('?' + urlencode({'ticketId':str(ticket_id)}) if ticket_id is not None else '')
        headers={name:getattr(self.context,key) for name,key in _HEADERS.items()}
        headers.update({'Accept':'application/json','Accept-Encoding':'identity'})
        response=None
        try:
            if self.opener is None:
                self.opener=build_opener(ProxyHandler({}),_RejectRedirects(),HTTPSHandler(context=ssl.create_default_context()))
                self.opener.addheaders=[]
            response=self.opener.open(Request(url,headers=headers,method='GET'),timeout=self.timeout)
            if response.geturl()!=url or 300<=response.getcode()<400:
                raise PersonalError('personal_redirect_blocked')
            if response.getcode()!=200:raise PersonalError('personal_http_error')
            length=response.headers.get('Content-Length')
            if isinstance(length,str) and length.isdecimal() and int(length)>MAX_BODY:
                raise PersonalError('personal_response_too_large')
            body=response.read(MAX_BODY+1)
            if not isinstance(body,bytes) or len(body)>MAX_BODY:raise PersonalError('personal_response_too_large')
            return json.loads(body.decode('utf8'),object_pairs_hook=_unique_json_object,
                              parse_constant=_reject_json_constant)
        except HTTPError as error:
            code='personal_redirect_blocked' if 300<=error.code<400 else 'personal_http_error'
            error.close();raise PersonalError(code) from None
        except ssl.SSLError:raise PersonalError('personal_tls_error') from None
        except (TimeoutError,socket.timeout):raise PersonalError('personal_timeout') from None
        except URLError as error:
            code='personal_tls_error' if isinstance(error.reason,ssl.SSLError) else 'personal_network_error'
            raise PersonalError(code) from None
        except PersonalError:raise
        except (OSError,ValueError,UnicodeError,TypeError,RecursionError,http.client.HTTPException):
            raise PersonalError('personal_response_invalid') from None
        finally:
            if response is not None:
                try:response.close()
                except Exception:pass

    def snapshot(self, *, include_history=False):
        if type(include_history) is not bool:raise PersonalError('personal_context_invalid')
        started=_clock(self.clock())
        current=normalize_current(self._get(STATUS_PATH))
        ticket=current['ordinary_ticket'];events=None
        if include_history and ticket is not None:
            events=normalize_history(self._get(HISTORY_PATH,ticket['ticketId']))
        received=_clock(self.clock())
        if received<started:raise PersonalError('personal_response_invalid')
        return {'schema_version':1,'source':'personal_gateway_ticket_v1',
            'data_origin':'custom_transport_unverified' if self.custom_transport else 'live',
            'context_revision':self.context.revision,'started_at':started.isoformat(),'received_at':received.isoformat(),
            **current,'history':events,'history_requested':include_history,
            'http_gets':1+int(events is not None),'retries':0,'business_writes':0,
            'response_account_identity_verified':False,'source_freshness':'unknown',
            'verified_training_labels':0,'eta_available':False,'output_requires_private_handling':True}


def write_personal_snapshot(*, context_file, destination, include_history=False, client_factory=None):
    # Reject an existing/unsafe destination before touching the network.
    parent=None
    try:
        parent,name=_open_parent(destination,private=True)
        try:os.stat(name,dir_fd=parent,follow_symlinks=False)
        except FileNotFoundError:pass
        else:raise PersonalError('personal_output_exists')
    except CaptureError:raise PersonalError('personal_output_unsafe') from None
    finally:
        if parent is not None:os.close(parent)
    result=(client_factory or PersonalClient)(read_personal_context(context_file)).snapshot(include_history=include_history)
    body=json.dumps(result,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()
    if len(body)>131072:raise PersonalError('personal_response_too_large')
    try:publication=_write_packet(body,destination)
    except PacketError as error:
        code='personal_output_exists' if error.error_code=='packet_output_exists' else 'personal_write_failed'
        raise PersonalError(code,committed=error.committed) from None
    ticket=result['ordinary_ticket'];events=result['history']
    return {'ok':True,'artifact_written':True,'committed':True,
        'durability_confirmed':publication['durability_confirmed'],
        'ordinary_ticket_present':ticket is not None,'checked_in':ticket['checkedIn'] if ticket else None,
        'history_event_count':len(events) if events is not None else None,
        'history_has_naive_timestamps':any(not e['timezone_present'] for e in events) if events is not None else None,
        'http_gets':result['http_gets'],'business_writes':0,'retries':0,
        'verified_training_labels':0,'eta_available':False,'output_requires_private_handling':True}


def inspect_personal_summary(body, *, since, now, revision, app_id, previous=None):
    since,now=_clock(since),_clock(now)
    if (not 0<=(now-since).total_seconds()<=40 or type(revision) is not int or not 1<=revision<=1_000_000
            or (previous is not None and revision!=previous.revision+1)
            or not isinstance(app_id,str) or not _APP_ID.fullmatch(app_id)
            or (previous is not None and previous.app_id!=app_id)
            or not isinstance(body,bytes) or len(body)>4*1024*1024):
        raise PersonalError('personal_intake_invalid')
    try:value=json.loads(body.decode(),object_pairs_hook=_unique_json_object,parse_constant=_reject_json_constant)
    except (UnicodeError,ValueError,RecursionError):raise PersonalError('personal_intake_invalid') from None
    rows=value.get('recent-requests') if isinstance(value,dict) else None
    if not isinstance(rows,list) or len(rows)>200:raise PersonalError('personal_intake_invalid')
    candidates=[]
    for row in rows:
        if not isinstance(row,dict):continue
        url=row.get('URL');date=row.get('completedDate')
        if url!=ORIGIN+STATUS_PATH or type(date) not in (int,float) or not math.isfinite(date):continue
        matched=[]
        for offset in (0,978307200):
            try:parsed=datetime.fromtimestamp(date+offset,timezone.utc)
            except (ValueError,OverflowError,OSError):continue
            if since<=parsed<=now:matched.append(parsed)
        if (len(matched)!=1 or row.get('method')!='GET' or row.get('completed') is not True
                or row.get('failed') is not False or row.get('streamHasRequestBody') is not False):continue
        request,response=row.get('requestHeader'),row.get('responseHeader')
        if (not isinstance(request,str) or not isinstance(response,str) or len(request)>32768 or len(response)>32768
                or not re.match(r'^HTTP/[0-9.]+ 200(?:\s|$)',response)):continue
        lines=request.splitlines()
        if not lines or not re.fullmatch(r'GET ('+re.escape(STATUS_PATH)+'|'+re.escape(ORIGIN+STATUS_PATH)+r') HTTP/[0-9.]+',lines[0]):continue
        values={};bad=False
        for line in lines[1:]:
            if not line:break
            name,separator,v=line.partition(':');name=name.lower()
            if line.startswith((' ','\t')):bad=True
            if name in _HEADERS:
                if not separator or name in values:bad=True
                values[name]=v.strip(' \t')
        if bad or set(values)!=set(_HEADERS):continue
        document={'schema_version':1,'purpose':PURPOSE,'api_profile':'personal_gateway','revision':revision,
            'observed_at':matched[0].isoformat(),'app_id':app_id,**{_HEADERS[k]:v for k,v in values.items()}}
        try:
            context=context_from_document(document,now=now);_guard(context.authorization,now)
        except PersonalError:continue
        if previous is not None and context.authorization==previous.authorization:continue
        candidates.append((matched[0],context))
    if not candidates:return None
    latest=max(t for t,c in candidates);selected=[c for t,c in candidates if t==latest]
    if any(c!=selected[0] for c in selected):raise PersonalError('personal_intake_ambiguous')
    return selected[0]


def _commit_context(destination, context, previous):
    parent=temp_fd=None;temporary=None;committed=False;durability=False
    try:
        import fcntl
        if previous is not None and (context.revision!=previous.revision+1
                or context.app_id!=previous.app_id or context.authorization==previous.authorization):
            raise PersonalError('personal_context_conflict')
        parent,name=_open_parent(destination,private=True)
        fcntl.flock(parent,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:before=os.stat(name,dir_fd=parent,follow_symlinks=False)
        except FileNotFoundError:before=None
        if before is not None:
            if not _private_file(before) or previous is None or read_personal_context(destination)!=previous:
                raise PersonalError('personal_context_conflict')
        elif previous is not None:raise PersonalError('personal_context_conflict')
        body=json.dumps(context.private_document(),separators=(',',':'),allow_nan=False).encode()
        if len(body)>16384:raise PersonalError('personal_context_invalid')
        os.fsync(parent)
        temporary='.sushiwait-personal-'+secrets.token_hex(16)+'.tmp'
        temp_fd=os.open(temporary,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
        offset=0
        while offset<len(body):
            count=os.write(temp_fd,body[offset:])
            if count<=0:raise PersonalError('personal_write_failed')
            offset+=count
        os.fsync(temp_fd)
        info=os.fstat(temp_fd);named=os.stat(temporary,dir_fd=parent,follow_symlinks=False)
        if not _private_file(info) or _identity(info)!=_identity(named):raise PersonalError('personal_write_failed')
        _check_parent(destination,parent,private=True)
        try:current=os.stat(name,dir_fd=parent,follow_symlinks=False)
        except FileNotFoundError:current=None
        if (current is None)!=(before is None) or (before is not None and _identity(before)!=_identity(current)):
            raise PersonalError('personal_context_conflict')
        _guard(context.authorization)
        if previous is None:
            os.link(temporary,name,src_dir_fd=parent,dst_dir_fd=parent,follow_symlinks=False)
            committed=True
            os.unlink(temporary,dir_fd=parent);temporary=None
        else:
            os.replace(temporary,name,src_dir_fd=parent,dst_dir_fd=parent);committed=True;temporary=None
        try:os.fsync(parent);durability=True
        except OSError:pass
        return durability
    except PersonalError:raise
    except CaptureError:raise PersonalError('personal_context_unsafe',committed=committed) from None
    except (OSError,ValueError,TypeError,ImportError):raise PersonalError('personal_write_failed',committed=committed) from None
    finally:
        if temp_fd is not None:os.close(temp_fd)
        if temporary is not None and parent is not None:
            try:os.unlink(temporary,dir_fd=parent)
            except OSError:pass
        if parent is not None:os.close(parent)


def receive_personal_context(*, context_file, revision, app_id=None, seconds=35, on_ready=None):
    if type(seconds) is not int or not 1<=seconds<=40 or type(revision) is not int or not 1<=revision<=1_000_000:
        raise PersonalError('personal_intake_invalid')
    parent=None
    try:
        parent,name=_open_parent(context_file,private=True)
        try:os.stat(name,dir_fd=parent,follow_symlinks=False)
        except FileNotFoundError:previous=None
        else:previous=read_personal_context(context_file)
    except CaptureError:raise PersonalError('personal_context_unsafe') from None
    finally:
        if parent is not None:os.close(parent)
    if revision!=(previous.revision+1 if previous else 1):raise PersonalError('personal_context_conflict')
    if app_id is None and previous is not None:app_id=previous.app_id
    if not isinstance(app_id,str) or not _APP_ID.fullmatch(app_id):raise PersonalError('personal_intake_invalid')
    if previous is not None and app_id!=previous.app_id:raise PersonalError('personal_context_conflict')
    since=_clock();deadline=time.monotonic()+seconds
    if on_ready:on_ready({'event':'personal_surge_ready','seconds':seconds,'request_bodies_read':False,'response_bodies_read':False})
    while time.monotonic()<deadline:
        now=_clock()
        if (now-since).total_seconds()>seconds or now<since:break
        try:body=_read_summary(min(2,max(.01,deadline-time.monotonic())))
        except SurgeError:raise PersonalError('personal_runtime_unavailable') from None
        observed_now=_clock()
        if (time.monotonic()>=deadline or observed_now<since
                or (observed_now-since).total_seconds()>seconds):break
        context=inspect_personal_summary(body,since=since,now=observed_now,revision=revision,app_id=app_id,previous=previous)
        if context is not None:
            durable=_commit_context(context_file,context,previous)
            return {'ok':True,'committed':True,'durability_confirmed':durable,'context_revision':revision,
                'purpose':PURPOSE,'source':'normal_personal_status_summary','network_verified':False,
                'request_bodies_read':False,'response_bodies_read':False,'business_writes':0}
        time.sleep(min(.2,max(0,deadline-time.monotonic())))
    raise PersonalError('personal_intake_timeout')
