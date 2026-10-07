"""Synthetic own-ticket contracts; no official calls or installed native apps."""
import base64
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from sushiwait import personal as m
from sushiwait.cli import main

NOW = datetime.now(timezone.utc)
APP_ID = 'wx0000000000000000'
REF = 'https://servicewechat.com/' + APP_ID + '/1/page-frame.html'


def document(marker='new', *, revision=1, expiry=None):
    claims={'iat':int((NOW-timedelta(minutes=1)).timestamp()),
            'exp':int((expiry or NOW+timedelta(hours=1)).timestamp()),'synthetic':marker}
    auth='Bearer e30.'+base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')+'.c2ln'
    return {'schema_version':1,'purpose':m.PURPOSE,'api_profile':'personal_gateway',
        'revision':revision,'observed_at':(NOW-timedelta(seconds=1)).isoformat(),'app_id':APP_ID,
        'authorization':auth,'app_client':'synthetic-client','app_code':'synthetic-private-code',
        'user_agent':'Synthetic Agent','referer':REF,'content_type':'application/json'}


def current():
    return {'netTicket':{'TICKET_TYPE':'MOBILE','TICKET_DETAIL':{
        'number':'A001','storeId':'900001','ticketId':900002,'status':'WAITING',
        'checkedIn':True,'tableType':'T','numAdult':1,'numChild':0,
        'queueDate':'synthetic-date-raw','queueTime':'synthetic-time-raw','wait':31,
        'phoneNumber':'synthetic-private-phone'},'STORE_INFO':{
        'id':900001,'timezone':'Asia/Shanghai','wait':45,'waitingGroup':17,
        'waitingGroupTable':12,'waitingGroupCounter':5,'waitingGroupPair':0,
        'memberCode':'synthetic-private-member'}},'reservationTicket':None,
        'headers':{'Authorization':'synthetic-private-extra'}}


def history():
    return {'ticketStatusHistory':[{'status':'WAITING','timestamp':'2026-10-07T19:00:00',
                                   'extra':'synthetic-private-extra'}]}


def row(doc=None, at=None):
    doc=document() if doc is None else doc
    headers='\r\n'.join(k+': '+doc[v] for k,v in m._HEADERS.items())
    return {'URL':m.ORIGIN+m.STATUS_PATH,'method':'GET','completed':True,'failed':False,
        'streamHasRequestBody':False,'completedDate':(at or NOW).timestamp(),
        'requestHeader':'GET '+m.STATUS_PATH+' HTTP/1.1\r\n'+headers+'\r\n\r\n',
        'responseHeader':'HTTP/1.1 200 OK\r\n\r\n'}


def summary(rows):
    return json.dumps({'recent-requests':rows}).encode()


class Response:
    def __init__(self, url, value, *, code=200, length=None):
        self.url,self.code,self.closed=url,code,False
        self.body=value if isinstance(value,bytes) else json.dumps(value).encode()
        self.headers={} if length is None else {'Content-Length':str(length)}
    def geturl(self):return self.url
    def getcode(self):return self.code
    def read(self,size):return self.body[:size]
    def close(self):self.closed=True


class Transport:
    def __init__(self, values):self.values=list(values);self.requests=[];self.responses=[]
    def open(self,request,timeout):
        self.requests.append(request)
        value=self.values.pop(0)
        if isinstance(value,Exception):raise value
        response=Response(request.full_url,value)
        self.responses.append(response);return response


class PersonalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.parent=Path(self.temp.name).resolve();self.parent.chmod(0o700)
        self.context_path=self.parent/'personal.json'
    def context(self, **kwargs):return m.context_from_document(document(**kwargs),now=NOW)
    def save(self, doc=None):
        self.context_path.write_text(json.dumps(document() if doc is None else doc));self.context_path.chmod(0o600)
    def inspect(self, rows, **kwargs):
        return m.inspect_personal_summary(summary(rows),since=NOW-timedelta(seconds=2),now=NOW,revision=1,app_id=APP_ID,**kwargs)
    def client(self, transport, clock=None):
        return m.PersonalClient(self.context(),opener=transport,clock=clock or (lambda:NOW))

    def test_own_number_retains_label_and_unknown_wait_not_rank(self):
        ticket=m.normalize_current(current())['ordinary_ticket']
        self.assertEqual(ticket['number'],'A001');self.assertTrue(ticket['checkedIn'])
        self.assertNotEqual(ticket['wait_raw'],ticket['store_wait_values']['wait'])
        self.assertEqual(ticket['wait_unit'],'unknown');self.assertFalse(ticket['position_verified'])
        self.assertFalse(ticket['issued_time_verified'])

    def test_personal_output_allowlist_drops_phone_member_headers_and_unknown_fields(self):
        text=json.dumps(m.normalize_current(current()))+json.dumps(m.normalize_history(history()))
        for value in ('synthetic-private-phone','synthetic-private-member','synthetic-private-extra'):
            self.assertNotIn(value,text)

    def test_no_ticket_and_reservation_presence_do_not_invent_ordinary_ticket(self):
        self.assertEqual(m.normalize_current({'netTicket':None,'reservationTicket':{}}),
            {'ordinary_ticket':None,'reservation_ticket_present':True})
        with self.assertRaises(m.PersonalError):m.normalize_current({'netTicket':None})

    def test_invalid_ticket_identity_and_types_reject_whole_record(self):
        for key,value in [('storeId','900003'),('checkedIn',1),('ticketId',True),('number','13800138000'),
                          ('numAdult',0),('wait',None),('status','private status')]:
            data=current();data['netTicket']['TICKET_DETAIL'][key]=value
            with self.subTest(key=key),self.assertRaises(m.PersonalError):m.normalize_current(data)

    def test_unknown_date_time_strings_are_preserved_without_time_label(self):
        data=current();data['netTicket']['TICKET_DETAIL'].update(queueDate='1720000000000',queueTime='epoch:123')
        ticket=m.normalize_current(data)['ordinary_ticket']
        self.assertEqual(ticket['queueTime_raw'],'epoch:123');self.assertFalse(ticket['issued_time_verified'])

    def test_history_naive_time_has_no_assumed_timezone_or_verified_label(self):
        event=m.normalize_history(history())[0]
        self.assertIsNone(event['timestamp_utc']);self.assertFalse(event['timezone_present'])
        self.assertFalse(event['event_meaning_verified'])

    def test_history_explicit_offset_is_converted_and_order_preserved(self):
        data=history();data['ticketStatusHistory']=[{'status':'CALLED','timestamp':'2026-10-07T20:00:00+08:00'},
            {'status':'WAITING','timestamp':'2026-10-07T19:00:00+08:00'}]
        events=m.normalize_history(data)
        self.assertEqual(events[0]['timestamp_utc'],'2026-10-07T12:00:00+00:00')
        self.assertEqual(events[1]['status_raw'],'WAITING');self.assertFalse(events[0]['event_meaning_verified'])

    def test_bad_and_unbounded_history_rejects_all(self):
        for value in [None,{}, {'ticketStatusHistory':[{'status':'WAITING','timestamp':'2026-10-07'}]},
                      {'ticketStatusHistory':history()['ticketStatusHistory']*257}]:
            with self.subTest(value=type(value)),self.assertRaises(m.PersonalError):m.normalize_history(value)

    def test_history_id_only_comes_from_same_successful_current_response(self):
        transport=Transport([current(),history()]);result=self.client(transport).snapshot(include_history=True)
        self.assertEqual([r.full_url for r in transport.requests],
            [m.ORIGIN+m.STATUS_PATH,m.ORIGIN+m.HISTORY_PATH+'?ticketId=900002'])
        self.assertTrue(all(r.get_method()=='GET' for r in transport.requests))
        self.assertEqual(result['http_gets'],2);self.assertEqual(result['business_writes'],0)
        self.assertEqual(result['data_origin'],'custom_transport_unverified')
        self.assertFalse(result['eta_available']);self.assertTrue(all(r.closed for r in transport.responses))

    def test_empty_status_does_not_query_arbitrary_history(self):
        transport=Transport([{'netTicket':None,'reservationTicket':{}}])
        result=self.client(transport).snapshot(include_history=True)
        self.assertEqual(len(transport.requests),1);self.assertIsNone(result['history'])

    def test_expired_unknown_and_margin_stop_before_any_http(self):
        for auth in ['Bearer synthetic-opaque',document(expiry=NOW-timedelta(seconds=1))['authorization'],
                     document(expiry=NOW+timedelta(seconds=30))['authorization']]:
            doc=document();doc['authorization']=auth
            transport=Transport([current()]);client=m.PersonalClient(m.context_from_document(doc,now=NOW),
                opener=transport,clock=lambda:NOW)
            with self.assertRaises(m.PersonalError):client.snapshot()
            self.assertEqual(transport.requests,[])

    def test_expiry_between_requests_stops_before_history(self):
        clock=[NOW];transport=Transport([current(),history()])
        original=transport.open
        def query(*args,**kwargs):
            response=original(*args,**kwargs);clock[0]=NOW+timedelta(hours=2);return response
        transport.open=query
        with self.assertRaisesRegex(m.PersonalError,'personal_auth_expired'):
            self.client(transport,lambda:clock[0]).snapshot(include_history=True)
        self.assertEqual(len(transport.requests),1)

    def test_http_error_has_one_attempt_and_no_original_error_text(self):
        transport=Transport([HTTPError(m.ORIGIN+m.STATUS_PATH,401,'synthetic-private-failure',{},None)])
        with self.assertRaises(m.PersonalError) as caught:self.client(transport).snapshot()
        self.assertEqual(str(caught.exception),'personal_http_error');self.assertEqual(len(transport.requests),1)

    def test_tls_and_network_errors_are_fixed_and_not_retried(self):
        for failure,code in [(ssl.SSLError('synthetic-private-failure'),'personal_tls_error'),
            (URLError(ssl.SSLError('synthetic-private-failure')),'personal_tls_error'),
            (URLError('synthetic-private-failure'),'personal_network_error'),
            (TimeoutError('synthetic-private-failure'),'personal_timeout')]:
            transport=Transport([failure])
            with self.subTest(code=code),self.assertRaisesRegex(m.PersonalError,code):self.client(transport).snapshot()
            self.assertEqual(len(transport.requests),1)

    def test_malformed_duplicate_nonfinite_business_and_large_json_stop(self):
        for body in [b'private-invalid-json',b'{"netTicket":null,"netTicket":null}',
            b'{"netTicket":NaN}',b'x'*(m.MAX_BODY+1),{'error':'synthetic-private-failure'}]:
            transport=Transport([body])
            with self.subTest(body=type(body)),self.assertRaises(m.PersonalError):self.client(transport).snapshot()
            self.assertEqual(len(transport.requests),1);self.assertTrue(transport.responses[0].closed)

    def test_redirect_and_large_content_length_close_response(self):
        for url,length,code in [(m.ORIGIN+'/elsewhere',None,200),(m.ORIGIN+m.STATUS_PATH,m.MAX_BODY+1,200),
                                (m.ORIGIN+m.STATUS_PATH,None,302)]:
            response=Response(url,current(),length=length,code=code)
            transport=Transport([]);transport.open=lambda *a,**k:response
            with self.assertRaises(m.PersonalError):self.client(transport).snapshot()
            self.assertTrue(response.closed)

    def test_request_uses_complete_context_and_no_extra_identity_parameters(self):
        transport=Transport([current()]);self.client(transport).snapshot()
        request=transport.requests[0]
        self.assertEqual(request.full_url,m.ORIGIN+m.STATUS_PATH)
        self.assertIsNone(request.data);self.assertEqual(request.get_header('X-app-code'),'synthetic-private-code')
        self.assertEqual(request.get_header('Content-type'),'application/json')

    def test_default_transport_uses_tls_verification_no_proxy_and_no_redirect(self):
        transport=Transport([current()])
        with patch.object(m,'build_opener',return_value=transport) as build:
            result=m.PersonalClient(self.context(),clock=lambda:NOW).snapshot()
        handlers=build.call_args.args
        self.assertEqual(handlers[0].proxies,{})
        self.assertIsInstance(handlers[1],m._RejectRedirects)
        self.assertEqual(handlers[2]._context.verify_mode,ssl.CERT_REQUIRED)
        self.assertTrue(handlers[2]._context.check_hostname);self.assertEqual(result['data_origin'],'live')

    def test_context_profile_purpose_app_future_extra_and_header_injection_rejected(self):
        changes=[{'api_profile':'miniapp_gateway'},{'purpose':'public_query'},
            {'referer':REF.replace(APP_ID,'wx1111111111111111')},
            {'observed_at':(NOW+timedelta(seconds=1)).isoformat()},{'extra':'private'},
            {'app_code':'value\r\nInjected: private'},{'revision':True}]
        for change in changes:
            value=document();value.update(change)
            with self.subTest(change=list(change)),self.assertRaises(m.PersonalError):m.context_from_document(value,now=NOW)

    def test_context_repr_has_no_headers_or_personal_times(self):
        text=repr(self.context())
        for key in m._HEADERS.values():self.assertNotIn(document()[key],text)
        self.assertNotIn(document()['observed_at'],text)

    def test_private_reader_rejects_permissions_symlink_hardlink_duplicates(self):
        self.save();self.assertEqual(m.read_personal_context(self.context_path,now=NOW),self.context())
        self.context_path.chmod(0o644)
        with self.assertRaises(m.PersonalError):m.read_personal_context(self.context_path)
        self.context_path.chmod(0o600);other=self.parent/'other';os.link(self.context_path,other)
        with self.assertRaises(m.PersonalError):m.read_personal_context(self.context_path)
        other.unlink();other.symlink_to(self.context_path)
        with self.assertRaises(m.PersonalError):m.read_personal_context(other)
        self.context_path.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaises(m.PersonalError):m.read_personal_context(self.context_path)

    def test_output_is_private_atomic_and_terminal_summary_has_no_ticket_or_headers(self):
        self.save();transport=Transport([current(),history()]);output=self.parent/'snapshot.json'
        result=m.write_personal_snapshot(context_file=self.context_path,destination=output,include_history=True,
            client_factory=lambda ctx:m.PersonalClient(ctx,opener=transport,clock=lambda:NOW))
        self.assertEqual(output.stat().st_mode&0o777,0o600)
        self.assertEqual(json.loads(output.read_text())['ordinary_ticket']['number'],'A001')
        text=json.dumps(result)
        for secret in ['A001','900001','900002','synthetic-private-code',document()['authorization']]:
            self.assertNotIn(secret,text)
        self.assertTrue(result['history_has_naive_timestamps']);self.assertEqual(result['http_gets'],2)

    def test_existing_or_unsafe_output_blocks_before_http(self):
        self.save();output=self.parent/'snapshot.json';output.write_text('keep')
        with patch.object(m,'PersonalClient',side_effect=AssertionError('network')):
            with self.assertRaisesRegex(m.PersonalError,'personal_output_exists'):
                m.write_personal_snapshot(context_file=self.context_path,destination=output)
            output.unlink();self.parent.chmod(0o755)
            with self.assertRaisesRegex(m.PersonalError,'personal_output_unsafe'):
                m.write_personal_snapshot(context_file=self.context_path,destination=output)
        self.parent.chmod(0o700)

    def test_current_summary_only_exact_read_only_route_and_both_epochs(self):
        candidate=self.inspect([row()]);self.assertEqual(candidate.authorization,self.context().authorization)
        self.assertEqual(datetime.fromisoformat(candidate.observed_at),NOW)
        value=row();value['completedDate']-=978307200
        self.assertEqual(self.inspect([value]).authorization,self.context().authorization)
        for change in [{'URL':m.ORIGIN+m.STATUS_PATH+'?wechatId=other'},
            {'URL':m.ORIGIN+m.HISTORY_PATH+'?ticketId=900002'}, {'method':'POST'},
            {'failed':True},{'streamHasRequestBody':True},{'responseHeader':'HTTP/1.1 401 Unauthorized'},
            {'completedDate':(NOW+timedelta(seconds=1)).timestamp()}]:
            value=row();value.update(change)
            with self.subTest(change=list(change)):self.assertIsNone(self.inspect([value]))

    def test_summary_does_not_assemble_split_headers_or_accept_duplicate_and_folded_headers(self):
        a,b=row(),row()
        a['requestHeader']=a['requestHeader'].replace('content-type: application/json\r\n','')
        b['requestHeader']=b['requestHeader'].replace('x-app-code: synthetic-private-code\r\n','')
        self.assertIsNone(self.inspect([a,b]))
        for extra in ['authorization: private\r\n',' folded-private\r\n']:
            value=row();value['requestHeader']=value['requestHeader'].replace('\r\n\r\n','\r\n'+extra+'\r\n')
            self.assertIsNone(self.inspect([value]))

    def test_summary_latest_context_and_ambiguous_tie(self):
        a=row(document('old'),NOW-timedelta(seconds=1));b=row()
        self.assertEqual(self.inspect([a,b]).authorization,self.context().authorization)
        a['completedDate']=b['completedDate']
        with self.assertRaisesRegex(m.PersonalError,'personal_intake_ambiguous'):self.inspect([a,b])

    def test_summary_previous_auth_cannot_be_reissued_as_new_revision(self):
        old=self.context();same=row(document(revision=2))
        result=m.inspect_personal_summary(summary([same]),since=NOW-timedelta(seconds=2),now=NOW,
            revision=2,app_id=APP_ID,previous=old)
        self.assertIsNone(result)
        new=row(document('newer',revision=2))
        result=m.inspect_personal_summary(summary([new]),since=NOW-timedelta(seconds=2),now=NOW,
            revision=2,app_id=APP_ID,previous=old)
        self.assertEqual(result.revision,2)

    def test_summary_bounds_and_duplicate_json_fail_without_original_text(self):
        for body in [b'private-invalid',b'{"recent-requests":[],"recent-requests":[]}',
                     summary([row()]*201),b'x'*(4*1024*1024+1)]:
            with self.assertRaises(m.PersonalError) as caught:
                m.inspect_personal_summary(body,since=NOW,now=NOW,revision=1,app_id=APP_ID)
            self.assertEqual(str(caught.exception),'personal_intake_invalid')

    def test_atomic_context_commit_and_update_preserve_separate_old_purpose_file(self):
        self.assertTrue(m._commit_context(self.context_path,self.context(),None))
        self.assertEqual(m.read_personal_context(self.context_path,now=NOW),self.context())
        new=self.context(marker='newer',revision=2)
        self.assertTrue(m._commit_context(self.context_path,new,self.context()))
        self.assertEqual(m.read_personal_context(self.context_path,now=NOW),new)
        self.assertEqual(self.context_path.stat().st_mode&0o777,0o600)
        self.assertEqual(list(self.parent.glob('*.tmp')),[])

    def test_changed_existing_context_is_not_overwritten_and_expired_new_is_not_created(self):
        self.save(document('other'));before=self.context_path.read_bytes()
        with self.assertRaisesRegex(m.PersonalError,'personal_context_conflict'):
            m._commit_context(self.context_path,self.context(marker='newer',revision=2),self.context())
        self.assertEqual(self.context_path.read_bytes(),before)
        self.context_path.unlink()
        with self.assertRaisesRegex(m.PersonalError,'personal_auth_expired'):
            m._commit_context(self.context_path,self.context(expiry=NOW-timedelta(seconds=1)),None)
        self.assertFalse(self.context_path.exists());self.assertEqual(list(self.parent.glob('*.tmp')),[])

    def test_directory_durability_failure_reports_already_committed(self):
        original=os.fsync;count=[0]
        def fsync(fd):
            count[0]+=1
            if count[0]==3:raise OSError('synthetic-private-failure')
            return original(fd)
        with patch.object(m.os,'fsync',side_effect=fsync):
            self.assertFalse(m._commit_context(self.context_path,self.context(),None))
        self.assertTrue(self.context_path.exists())

    def test_new_intake_requires_explicit_app_binding_before_native_read(self):
        with patch.object(m,'_read_summary',side_effect=AssertionError('native')) as read:
            with self.assertRaisesRegex(m.PersonalError,'personal_intake_invalid'):
                m.receive_personal_context(context_file=self.context_path,revision=1)
        read.assert_not_called();self.assertFalse(self.context_path.exists())

    def test_context_update_cannot_change_app_binding(self):
        self.save();before=self.context_path.read_bytes()
        with patch.object(m,'_read_summary',side_effect=AssertionError('native')) as read:
            with self.assertRaisesRegex(m.PersonalError,'personal_context_conflict'):
                m.receive_personal_context(context_file=self.context_path,revision=2,app_id='wx1111111111111111')
        read.assert_not_called();self.assertEqual(self.context_path.read_bytes(),before)

    def test_intake_uses_bounded_summary_only_and_does_not_enable_native_settings(self):
        since=NOW-timedelta(seconds=1)
        with patch.object(m,'_clock',side_effect=lambda value=None:value or since),\
             patch.object(m,'_read_summary',return_value=summary([row(at=since)])) as read,\
             patch.object(m,'_commit_context',return_value=True) as commit,\
             patch('subprocess.Popen',side_effect=AssertionError('native mutation')),\
             patch('socket.socket',side_effect=AssertionError('upstream')):
            result=m.receive_personal_context(context_file=self.context_path,revision=1,app_id=APP_ID,seconds=35)
        self.assertTrue(result['committed']);self.assertFalse(result['response_bodies_read'])
        self.assertEqual(read.call_count,1);self.assertLessEqual(read.call_args.args[0],2)
        self.assertEqual(commit.call_count,1)

    def test_intake_read_that_finishes_after_deadline_cannot_commit(self):
        mono=[0]
        def read(timeout):mono[0]=41;return summary([row()])
        with patch.object(m.time,'monotonic',side_effect=lambda:mono[0]),\
             patch.object(m,'_read_summary',side_effect=read),patch.object(m,'_commit_context') as commit:
            with self.assertRaisesRegex(m.PersonalError,'personal_intake_timeout'):
                m.receive_personal_context(context_file=self.context_path,revision=1,app_id=APP_ID,seconds=40)
        commit.assert_not_called();self.assertFalse(self.context_path.exists())

    def test_cli_offline_expiry_failure_and_interrupt_are_sanitized(self):
        self.save()
        with patch('socket.socket',side_effect=AssertionError('network')),\
             contextlib.redirect_stdout(io.StringIO()) as output:
            code=main(['personal-auth-status','--context-file',str(self.context_path)])
        result=json.loads(output.getvalue());self.assertEqual(code,0);self.assertFalse(result['network_performed'])
        self.assertNotIn(document()['authorization'],output.getvalue())
        with patch('sushiwait.cli.receive_personal_context',side_effect=KeyboardInterrupt()),\
             contextlib.redirect_stdout(io.StringIO()) as output:
            code=main(['personal-context-surge','--context-file',str(self.context_path),'--revision','2'])
        self.assertEqual(code,130);self.assertEqual(json.loads(output.getvalue())['error_code'],'personal_interrupted')


if __name__=='__main__':unittest.main()
