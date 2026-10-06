"""Authenticated, bounded loopback-only public-packet receiver for development."""
from __future__ import annotations

from datetime import datetime,timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
import os
from pathlib import Path
import re
import socket
import threading
import time

from .capture import CaptureError,_open_parent,_check_parent
from .credentials import _private_file,_identity
from .packets import encoded
from .receipts import MAX_PACKET_BYTES,PacketArchive,ReceiptError,validate_packet
from .storage import _unique_object

_TOKEN = re.compile(r'[A-Za-z0-9_-]{32,128}\Z')
_PATH = '/v1/public-observations'


class ReceiverError(RuntimeError):
    def __init__(self,error_code):
        super().__init__(error_code);self.error_code=error_code


def _token(value):
    if type(value) is not str or _TOKEN.fullmatch(value) is None:
        raise ReceiverError('receiver_invalid_token')
    return value


def read_receiver_token(path):
    """Read only a dedicated private token file; never a query context."""
    parent_fd=file_fd=None
    try:
        parent_fd,name=_open_parent(path,private=True)
        file_fd=os.open(name,os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,dir_fd=parent_fd)
        initial=os.fstat(file_fd)
        if not _private_file(initial) or not 32<=initial.st_size<=130:
            raise ReceiverError('receiver_token_unsafe')
        body=os.read(file_fd,131)
        _check_parent(path,parent_fd,private=True)
        final=os.stat(name,dir_fd=parent_fd,follow_symlinks=False)
        if (_identity(initial)!=_identity(final) or _identity(initial)!=_identity(os.fstat(file_fd))
                or len(body)!=initial.st_size):
            raise ReceiverError('receiver_token_changed')
        return _token(body.decode('ascii').removesuffix('\n').removesuffix('\r'))
    except ReceiverError:
        raise
    except (OSError,CaptureError,ValueError,TypeError,UnicodeError):
        raise ReceiverError('receiver_token_unavailable') from None
    finally:
        for fd in (file_fd,parent_fd):
            if fd is not None:os.close(fd)


def _constant(_):
    raise ValueError('nonfinite')


def receive_packet(body,database,*,now):
    """Validate and archive a whole batch; acknowledgement follows commit."""
    try:
        if type(body) is not bytes or not 0<len(body)<=MAX_PACKET_BYTES:
            raise ReceiptError('packet_too_large' if type(body) is bytes and len(body)>MAX_PACKET_BYTES else 'packet_invalid_format')
        packet=json.loads(body.decode('utf8'),object_pairs_hook=_unique_object,parse_constant=_constant)
        packet=validate_packet(packet,as_of=now.isoformat())
        if not packet['records']:
            raise ReceiptError('receiver_empty_packet')
    except ReceiptError:
        raise
    except (ValueError,TypeError,UnicodeError,RecursionError,AttributeError):
        raise ReceiptError('packet_invalid_format') from None
    with PacketArchive(database) as archive:
        result=archive.append(packet,as_of=now.isoformat())
    if result['local_archive_commit_status']!='committed' or not result['durability_confirmed']:
        raise ReceiptError('receiver_archive_unconfirmed',commit_status=result['local_archive_commit_status'])
    return {'receipt_schema_version':1,'packet_sha256':hashlib.sha256(encoded(packet)).hexdigest(),
            'receiver_context':'loopback_development','local_archive_commit_status':'committed',
            'durability_confirmed':True,'inserted_records':result['inserted_records'],
            'duplicate_records':result['duplicate_records'],
            'observations':[{'observation_id':record['observation_id'],'record_sha256':record['record_sha256']} for record in packet['records']],
            'receiver_archive_received':True,'remote_deployment_verified':False,
            'source_claims_verified':False,'source_freshness':'unknown',
            'eta_available':False,'verified_training_labels':0}


class _Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.0'

    def setup(self):
        self.request.settimeout(min(2.0,max(0.05,self.server.deadline-time.monotonic())))
        super().setup()

    def log_message(self,*_):
        # Base logging can include URLs, authorization mistakes and raw errors.
        pass

    def _reply(self,status,value):
        body=encoded(value)
        self.close_connection=True
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.end_headers()
        if getattr(self,'command',None)!='HEAD':self.wfile.write(body)

    def send_error(self,code,message=None,explain=None):
        self._reply(code,{'ok':False,'error_code':'receiver_http_rejected'})

    def do_POST(self):
        try:
            if self.path!=_PATH:
                self._reply(404,{'ok':False,'error_code':'receiver_path_rejected'});return
            headers=self.headers
            authorization=headers.get_all('Authorization',[])
            if (len(authorization)!=1 or not authorization[0].isascii() or len(authorization[0])>135
                    or not hmac.compare_digest(authorization[0], 'Bearer '+self.server.client_token)):
                self._reply(401,{'ok':False,'error_code':'receiver_unauthorized'});return
            if (headers.get_all('Host',[])!=[f'127.0.0.1:{self.server.server_port}']
                    or headers.get_all('Transfer-Encoding',[]) or headers.get_all('Expect',[])
                    or headers.get_all('Origin',[]) or headers.get_all('Cookie',[])):
                self._reply(400,{'ok':False,'error_code':'receiver_headers_rejected'});return
            lengths=headers.get_all('Content-Length',[])
            types=headers.get_all('Content-Type',[])
            if (len(lengths)!=1 or re.fullmatch(r'[1-9][0-9]{0,7}',lengths[0]) is None
                    or len(types)!=1 or types[0].lower() not in {'application/json','application/json; charset=utf-8'}):
                self._reply(400,{'ok':False,'error_code':'receiver_headers_rejected'});return
            length=int(lengths[0])
            if length>MAX_PACKET_BYTES:
                self._reply(413,{'ok':False,'error_code':'packet_too_large'});return
            body=self.rfile.read(length)
            if len(body)!=length:
                self._reply(400,{'ok':False,'error_code':'receiver_body_incomplete'});return
            result=receive_packet(body,self.server.database,now=self.server.clock())
            reply={'ok':True,**result,'receipt_schema_version':2}
            reply['receipt_hmac_sha256']=hmac.new(self.server.client_token.encode('ascii'),encoded(reply),hashlib.sha256).hexdigest()
            self._reply(200,reply)
            self.server.successful_receipts+=1
        except ReceiptError as error:
            status=409 if error.error_code=='archive_observation_conflict' else (503 if error.error_code.startswith('archive_') or error.commit_status!='not_started' else 400)
            self._reply(status,{'ok':False,'error_code':error.error_code,
                'local_archive_commit_status':error.commit_status,'durability_confirmed':False})
        except (OSError,ValueError,TypeError):
            # A lost reply never creates a success receipt. Archive commitment
            # may already have happened; callers must retry the same packet.
            self.server.reply_failures+=1

    def do_GET(self):self._reply(405,{'ok':False,'error_code':'receiver_method_rejected'})
    do_HEAD=do_GET
    do_PUT=do_GET
    do_DELETE=do_GET
    do_OPTIONS=do_GET
    do_PATCH=do_GET


class _Server(HTTPServer):
    allow_reuse_address=False

    def get_request(self):
        connection,address=super().get_request()
        with self.active_lock:
            if time.monotonic()>=self.deadline:
                connection.close();raise OSError('receiver_deadline')
            self.active=connection
        return connection,address

    def shutdown_request(self,request):
        try:super().shutdown_request(request)
        finally:
            with self.active_lock:
                if self.active is request:self.active=None
            self.completed_connections+=1

    def handle_error(self,*_):
        self.reply_failures+=1


def validate_receiver_limits(*,port,seconds,max_requests):
    if (type(port) is not int or not 0<=port<=65535 or type(seconds) is not int or not 1<=seconds<=60
            or type(max_requests) is not int or not 1<=max_requests<=100):
        raise ReceiverError('receiver_invalid_limits')


def run_receiver(database,token,*,port=0,seconds=30,max_requests=10,on_ready=None,clock=None):
    """Listen on 127.0.0.1 only, with a separate active-socket deadline guard."""
    validate_receiver_limits(port=port,seconds=seconds,max_requests=max_requests)
    token=_token(token)
    fd=None
    try:
        fd,_=_open_parent(database,private=True)
    except (CaptureError,ValueError,TypeError,OSError):
        raise ReceiverError('receiver_database_unsafe') from None
    finally:
        if fd is not None:os.close(fd)
    server=timer=None
    try:
        server=_Server(('127.0.0.1',port),_Handler)
        server.database=Path(database);server.client_token=token
        server.clock=clock or (lambda:datetime.now(timezone.utc))
        server.deadline=time.monotonic()+seconds
        server.completed_connections=server.successful_receipts=server.reply_failures=0
        server.active=None;server.active_lock=threading.Lock();stopped=threading.Event()

        def deadline():
            stopped.set()
            server.socket.close()
            with server.active_lock:
                if server.active is not None:
                    try:server.active.shutdown(socket.SHUT_RDWR)
                    except OSError:pass

        timer=threading.Timer(seconds,deadline);timer.daemon=True;timer.start()
        if on_ready is not None:
            on_ready({'event':'loopback_packet_receiver_ready','bound_port':server.server_port,
                      'seconds':seconds,'max_requests':max_requests,'remote_deployment_verified':False})
        while not stopped.is_set() and server.completed_connections<max_requests:
            remaining=server.deadline-time.monotonic()
            if remaining<=0:break
            server.timeout=min(0.25,remaining);server.handle_request()
        return {'event':'loopback_packet_receiver_finished','completed_connections':server.completed_connections,
                'successful_receipts':server.successful_receipts,'reply_failures':server.reply_failures,
                'stop_reason':'request_limit' if server.completed_connections>=max_requests else 'deadline',
                'remote_deployment_verified':False,'query_credentials_accessed':False,
                'outbound_network_performed':False,'verified_training_labels':0,'eta_available':False}
    except ReceiverError:
        raise
    except (OSError,ValueError,TypeError):
        raise ReceiverError('receiver_local_service_failed') from None
    finally:
        if timer is not None:timer.cancel();timer.join(timeout=1)
        if server is not None:
            active=getattr(server,'active',None)
            if active is not None:
                try:active.shutdown(socket.SHUT_RDWR)
                except OSError:pass
                active.close()
            server.server_close()
