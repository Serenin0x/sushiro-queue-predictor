"""Local-only delivery, strict signed receipt checks and private confirmation."""
from __future__ import annotations

from datetime import datetime,timezone
import hashlib,hmac,http.client,json,os,re,socket,threading
from pathlib import Path

from .capture import CaptureError,_open_parent,_check_parent
from .credentials import _identity,_private_file
from .monitoring import _time,_stamp
from .packets import PacketError,_write_packet,encoded
from .pending import _scope
from .receipts import ReceiptError,read_packet
from .receiver import read_receiver_token,ReceiverError,_token
from .storage import _unique_object

MAX_RECEIPT_BYTES=256*1024
NETWORK_SECONDS=5
_KEYS={'ok','receipt_schema_version','packet_sha256','receiver_context','local_archive_commit_status',
       'durability_confirmed','inserted_records','duplicate_records','observations',
       'receiver_archive_received','remote_deployment_verified','source_claims_verified',
       'source_freshness','eta_available','verified_training_labels','receipt_hmac_sha256'}


class DeliveryError(RuntimeError):
    def __init__(self,error_code,*,confirmation_committed=False,receiver_commit_status='not_attempted'):
        super().__init__(error_code);self.error_code=error_code
        self.confirmation_committed=confirmation_committed;self.receiver_commit_status=receiver_commit_status


def validate_receipt(receipt,packet,token):
    """Verify the dedicated receiver key, exact packet and every observation."""
    try:
        _token(token)
        if (type(receipt) is not dict or set(receipt)!=_KEYS or receipt['ok'] is not True
                or type(receipt['receipt_schema_version']) is not int or receipt['receipt_schema_version']!=2
                or receipt['receiver_context']!='loopback_development'
                or receipt['local_archive_commit_status']!='committed'
                or receipt['durability_confirmed'] is not True or receipt['receiver_archive_received'] is not True
                or any(receipt[k] is not False for k in ('remote_deployment_verified','source_claims_verified','eta_available'))
                or receipt['source_freshness']!='unknown' or type(receipt['verified_training_labels']) is not int
                or receipt['verified_training_labels']!=0):
            raise DeliveryError('delivery_invalid_receipt')
        digest=receipt['receipt_hmac_sha256']
        expected=hmac.new(token.encode('ascii'),encoded({k:v for k,v in receipt.items() if k!='receipt_hmac_sha256'}),hashlib.sha256).hexdigest()
        if type(digest) is not str or re.fullmatch('[0-9a-f]{64}',digest) is None or not hmac.compare_digest(digest,expected):
            raise DeliveryError('delivery_receipt_hmac_mismatch')
        count=len(packet['records'])
        if (receipt['packet_sha256']!=hashlib.sha256(encoded(packet)).hexdigest()
                or any(type(receipt[k]) is not int or not 0<=receipt[k]<=count for k in ('inserted_records','duplicate_records'))
                or receipt['inserted_records']+receipt['duplicate_records']!=count
                or type(receipt['observations']) is not list
                or receipt['observations']!=[{k:r[k] for k in ('observation_id','record_sha256')} for r in packet['records']]):
            raise DeliveryError('delivery_receipt_packet_mismatch')
        return json.loads(encoded(receipt))
    except DeliveryError:raise
    except (ValueError,TypeError,KeyError,UnicodeError,ReceiverError,RecursionError):
        raise DeliveryError('delivery_invalid_receipt') from None


def _constant(_):raise ValueError('nonfinite')


def _read_confirmation(path):
    parent_fd=file_fd=None
    try:
        parent_fd,name=_open_parent(path,private=True)
        file_fd=os.open(name,os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,dir_fd=parent_fd)
        initial=os.fstat(file_fd)
        if not _private_file(initial) or not 0<initial.st_size<=MAX_RECEIPT_BYTES:
            raise DeliveryError('delivery_confirmation_unsafe')
        chunks=[];remaining=MAX_RECEIPT_BYTES+1
        while remaining:
            part=os.read(file_fd,remaining)
            if not part:break
            chunks.append(part);remaining-=len(part)
        body=b''.join(chunks)
        _check_parent(path,parent_fd,private=True)
        final=os.stat(name,dir_fd=parent_fd,follow_symlinks=False)
        if (_identity(initial)!=_identity(final) or _identity(initial)!=_identity(os.fstat(file_fd))
                or len(body)!=initial.st_size):
            raise DeliveryError('delivery_confirmation_changed')
        return json.loads(body.decode('utf8'),object_pairs_hook=_unique_object,parse_constant=_constant)
    except DeliveryError:raise
    except (OSError,CaptureError,ValueError,TypeError,UnicodeError,RecursionError):
        raise DeliveryError('delivery_confirmation_invalid') from None
    finally:
        for fd in (file_fd,parent_fd):
            if fd is not None:os.close(fd)


def check_confirmation(packet_path,confirmation_path,token_path,*,as_of):
    try:
        packet=read_packet(packet_path,as_of=as_of)
        token=read_receiver_token(token_path)
        confirmation=_read_confirmation(confirmation_path)
        if (type(confirmation) is not dict or set(confirmation)!={'confirmation_schema_version','received_at','receiver_host','receiver_port','scope_sha256','receipt'}
                or type(confirmation['confirmation_schema_version']) is not int or confirmation['confirmation_schema_version']!=1
                or confirmation['receiver_host']!='127.0.0.1' or type(confirmation['receiver_port']) is not int
                or not 1<=confirmation['receiver_port']<=65535 or confirmation['scope_sha256']!=_scope(packet)
                or _stamp(_time(confirmation['received_at']))!=confirmation['received_at']
                or not _time(packet['as_of'])<=_time(confirmation['received_at'])<=_time(as_of)):
            raise DeliveryError('delivery_confirmation_invalid')
        receipt=validate_receipt(confirmation['receipt'],packet,token)
        return {'structure_validated':True,'receipt_hmac_verified':True,'confirmed_record_count':len(packet['records']),
                'packet_sha256':receipt['packet_sha256'],'historical_confirmation_only':True,'network_performed':False,
                'query_credentials_accessed':False,'remote_deployment_verified':False,'source_claims_verified':False,
                'verified_training_labels':0,'eta_available':False}
    except DeliveryError:raise
    except (ReceiptError,ReceiverError,ValueError,TypeError,KeyError,UnicodeError,RecursionError):
        raise DeliveryError('delivery_confirmation_invalid') from None


def _exchange(body,token,port):
    connection=http.client.HTTPConnection('127.0.0.1',port,timeout=2)
    lock=threading.Lock();expired=threading.Event();active=[None]
    def cutoff():
        expired.set()
        with lock:
            if active[0] is not None:
                try:active[0].shutdown(socket.SHUT_RDWR)
                except OSError:pass
    timer=threading.Timer(NETWORK_SECONDS,cutoff);timer.daemon=True;timer.start()
    attempted=False
    try:
        connection.connect()
        with lock:
            active[0]=connection.sock
            if expired.is_set():raise DeliveryError('delivery_network_deadline')
        attempted=True
        connection.request('POST','/v1/public-observations',body,
                           headers={'Content-Type':'application/json','Authorization':'Bearer '+token})
        response=connection.getresponse()
        headers=response.getheaders()
        values=lambda name:[v for k,v in headers if k.lower()==name]
        lengths=values('content-length')
        if (response.status!=200 or values('transfer-encoding') or values('set-cookie') or values('location')
                or values('content-type')!=['application/json'] or len(lengths)!=1
                or re.fullmatch('[1-9][0-9]{0,5}',lengths[0]) is None or int(lengths[0])>MAX_RECEIPT_BYTES):
            raise DeliveryError('delivery_receiver_rejected',receiver_commit_status='unknown')
        length=int(lengths[0]);reply=response.read(length+1)
        if expired.is_set():raise DeliveryError('delivery_network_deadline',receiver_commit_status='unknown')
        if len(reply)!=length:raise DeliveryError('delivery_incomplete_receipt',receiver_commit_status='unknown')
        return json.loads(reply.decode('utf8'),object_pairs_hook=_unique_object,parse_constant=_constant)
    except DeliveryError:raise
    except (OSError,http.client.HTTPException,ValueError,TypeError,UnicodeError,RecursionError):
        raise DeliveryError('delivery_network_deadline' if expired.is_set() else 'delivery_transport_failed',
                            receiver_commit_status='unknown' if attempted else 'not_attempted') from None
    finally:
        timer.cancel();timer.join(timeout=1)
        connection.close()


def deliver_local(packet_path,token_path,confirmation_path,*,port,clock=None):
    """Send to one explicit loopback port, then atomically save a checked receipt."""
    committed=False;receiver_status='not_attempted';parent_fd=None
    try:
        if type(port) is not int or not 1<=port<=65535:
            raise DeliveryError('delivery_invalid_port')
        if os.path.abspath(confirmation_path) in {os.path.abspath(packet_path),os.path.abspath(token_path)}:
            raise DeliveryError('delivery_path_conflict')
        parent_fd,name=_open_parent(confirmation_path,private=True)
        try:os.stat(name,dir_fd=parent_fd,follow_symlinks=False)
        except FileNotFoundError:pass
        else:raise DeliveryError('delivery_confirmation_exists')
        _check_parent(confirmation_path,parent_fd,private=True)
        now=clock or (lambda:datetime.now(timezone.utc))
        started=_time(now().isoformat())
        packet=read_packet(packet_path,as_of=_stamp(started))
        if not packet['records']:raise DeliveryError('delivery_empty_packet')
        token=read_receiver_token(token_path)
        reply=_exchange(encoded(packet),token,port)
        receiver_status='unknown'
        receipt=validate_receipt(reply,packet,token)
        receiver_status='reported_committed'
        received=_time(now().isoformat())
        if received<started:raise DeliveryError('delivery_clock_reversed',receiver_commit_status=receiver_status)
        confirmation={'confirmation_schema_version':1,'received_at':_stamp(received),'receiver_host':'127.0.0.1',
                      'receiver_port':port,'scope_sha256':_scope(packet),'receipt':receipt}
        body=encoded(confirmation)
        if len(body)>MAX_RECEIPT_BYTES:raise DeliveryError('delivery_confirmation_too_large',receiver_commit_status=receiver_status)
        result=_write_packet(body,confirmation_path);committed=result['committed']
        return {**result,'confirmation_saved':True,'receipt_hmac_verified':True,
                'receiver_commit_status':receiver_status,'confirmed_record_count':len(packet['records']),
                'packet_sha256':receipt['packet_sha256'],'network_performed':True,
                'network_scope':'loopback_only','query_credentials_accessed':False,
                'remote_deployment_verified':False,'source_claims_verified':False,'source_freshness':'unknown',
                'verified_training_labels':0,'eta_available':False}
    except DeliveryError as error:
        if receiver_status!='not_attempted' and error.receiver_commit_status=='not_attempted':
            raise DeliveryError(error.error_code,confirmation_committed=error.confirmation_committed,receiver_commit_status=receiver_status) from None
        raise
    except PacketError as error:
        raise DeliveryError('delivery_confirmation_publish_unconfirmed',confirmation_committed=error.committed,receiver_commit_status=receiver_status) from None
    except (ReceiptError,ReceiverError,CaptureError,OSError,ValueError,TypeError,AttributeError):
        raise DeliveryError('delivery_local_operation_failed',confirmation_committed=committed,receiver_commit_status=receiver_status) from None
    finally:
        if parent_fd is not None:os.close(parent_fd)
