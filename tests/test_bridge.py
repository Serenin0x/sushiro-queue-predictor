"""Synthetic loopback handoff; never contacts Sushiro or a real runtime."""
import base64
from datetime import datetime, timedelta, timezone
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from sushiwait.bridge import BridgeError, MAX_OBSERVATION_BYTES, _create_session, inspect_observation, receive_context
from sushiwait.capture import CaptureError
from sushiwait.credentials import read_credentials_file

NOW = datetime.now(timezone.utc)
APP = "wx0000000000000000"  # Explicitly synthetic application identity.
REF = "https://servicewechat.com/" + APP + "/1/page-frame.html"


def auth(expiry, marker="new-synthetic-private-marker"):
    value = json.dumps({"exp": int(expiry.timestamp()), "iat": int((expiry-timedelta(hours=1)).timestamp()), "private": marker})
    return "Bearer e30." + base64.urlsafe_b64encode(value.encode()).decode().rstrip("=") + ".c2ln"


def bundle(revision=1, authorization=None):
    return {"schema_version":1,"api_profile":"miniapp_gateway","revision":revision,
        "authorization":authorization or auth(NOW-timedelta(minutes=1),"old-synthetic-private-marker"),
        "app_client":"miniapp","app_code":"synthetic-app-code-secret",
        "user_agent":"Synthetic Agent secret/1.0","referer":REF,"content_type":"application/json"}


def observation(authorization=None):
    values = bundle(authorization=authorization or auth(NOW+timedelta(hours=1)))
    names = {"authorization":"authorization","app_client":"x-app-client","app_code":"x-app-code",
             "user_agent":"user-agent","referer":"referer","content_type":"content-type"}
    return {"schema_version":1,"request":{"method":"GET",
        "url":"https://sapi.sushiro.com.cn/gateway/wechat/api/2.0/getStoreById?storeId=900001",
        "headers":[{"name":names[key],"value":values[key]} for key in names]},
        "response":{"status":200,"store":{"id":900001,"name":"合成门店"}}}


@unittest.skipUnless(os.name == "posix", "POSIX private files")
class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name).resolve(); self.parent.chmod(0o700)
        self.context = self.parent/"context.json"; self.session = self.parent/"session.json"
        self.seed()

    def seed(self, value=None):
        self.context.write_text(json.dumps(value or bundle())); self.context.chmod(0o600)

    def inspect(self, value):
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        return inspect_observation(body,expected_app=APP,store_ids=("900001",),now=NOW)

    def test_minimal_success_is_safe_metadata_and_not_live_snapshot(self):
        report = self.inspect(observation()).public_report()
        self.assertTrue(report["candidates"][0]["importable"])
        self.assertEqual(report["candidates"][0]["store_id"],"900001")
        self.assertFalse(report["network_verified"])
        self.assertFalse(report["network_performed"])
        self.assertEqual(report["server_acceptance"],"unverified")
        text = json.dumps(report)
        for secret in ("synthetic-app-code-secret","new-synthetic-private-marker",REF,"Synthetic Agent"):
            self.assertNotIn(secret,text)

    def test_other_hosts_methods_stores_query_and_response_identity_are_rejected(self):
        mutations = [lambda v:v["request"].update(method="POST"),
            lambda v:v["request"].update(url=v["request"]["url"].replace("sapi.","crm-cn-prd.")),
            lambda v:v["request"].update(url=v["request"]["url"]+"&other=1"),
            lambda v:v["request"].update(url=v["request"]["url"]+"#fragment"),
            lambda v:v["request"].update(url=v["request"]["url"].replace("900001","900002")),
            lambda v:v["response"]["store"].update(id=900002),
            lambda v:v["response"].update(status=401),lambda v:v["response"].update(status=True)]
        for change in mutations:
            with self.subTest(change=mutations.index(change)):
                value=observation();change(value)
                with self.assertRaises((BridgeError,CaptureError)):self.inspect(value)

    def test_unknown_private_fields_and_partial_context_are_rejected(self):
        mutations = [lambda v:v.update(private="do-not-import"),
            lambda v:v["request"].update(postData={}),
            lambda v:v["response"]["store"].update(private_account="do-not-import"),
            lambda v:v["request"]["headers"].pop(),
            lambda v:v["request"]["headers"].append({"name":"Cookie","value":"do-not-import"}),
            lambda v:v["request"]["headers"][0].update(value=""),
            lambda v:v["request"]["headers"][1].update(name="Authorization"),
            lambda v:v["request"]["headers"][4].update(value=REF.replace(APP,"wx1111111111111111"))]
        for i,change in enumerate(mutations):
            with self.subTest(change=i):
                value=observation();change(value)
                with self.assertRaises((BridgeError,CaptureError)):self.inspect(value)

    def test_json_bounds_and_safe_exception(self):
        for value in (b"secret-invalid-json",b'{"schema_version":1,"schema_version":1}',
                      b'{"value":NaN}',b"x"*(MAX_OBSERVATION_BYTES+1)):
            with self.subTest(length=len(value)):
                with self.assertRaises((BridgeError,CaptureError)) as raised:self.inspect(value)
                self.assertNotIn("secret-invalid",str(raised.exception))
                self.assertIsNone(raised.exception.__context__)

    def run_receiver(self, actions, *, seconds=3, diagnostics=False, expect_timeout=False):
        outcomes=[]; failures=[]; workers=[];ready_rows=[]
        def ready(meta):
            ready_rows.append(meta)
            settings=json.loads(self.session.read_text());parts=urlsplit(settings["url"])
            self.assertEqual(self.session.stat().st_mode&0o777,0o600)
            def work():
                try:
                    for value, headers, method, path in actions:
                        connection=http.client.HTTPConnection("127.0.0.1",parts.port,timeout=2)
                        body=json.dumps(value).encode()
                        merged={"Content-Type":"application/json","Authorization":"Bearer "+settings["token"],**headers}
                        connection.request(method,path,body=body,headers=merged)
                        response=connection.getresponse();data=json.loads(response.read())
                        outcomes.append((response.status,data));connection.close()
                except Exception as e:failures.append(type(e).__name__)
            thread=threading.Thread(target=work);thread.start();workers.append(thread)
        with patch("sushiwait.client.SushiroClient.__init__",side_effect=AssertionError("no upstream client")), \
             patch("urllib.request.build_opener",side_effect=AssertionError("no upstream opener")):
            try:
                result=receive_context(credentials_file=self.context,session_file=self.session,
                    revision=2,store_ids=("900001",),seconds=seconds,on_ready=ready,
                    diagnostics=diagnostics)
            except BridgeError as error:
                if not expect_timeout:
                    raise
                self.assertEqual(error.error_code,"bridge_timeout")
                result=error
        for thread in workers:thread.join(3)
        self.assertFalse(failures);self.assertTrue(all(not t.is_alive() for t in workers))
        self.assertNotIn("token",json.dumps(ready_rows));self.assertFalse(self.session.exists())
        return result,outcomes

    def test_real_loopback_commit_cleanup_and_no_upstream(self):
        result,outcomes=self.run_receiver([(observation(),{},"POST","/v1/context")])
        self.assertEqual(outcomes,[(200,{"ok":True})])
        self.assertTrue(result["committed"]);self.assertTrue(result["session_cleanup_confirmed"])
        self.assertFalse(result["external_network_performed"])
        self.assertEqual(result["server_acceptance"],"unverified")
        self.assertEqual(read_credentials_file(self.context,api_profile="miniapp_gateway").revision,2)
        self.assertEqual(self.context.stat().st_mode&0o777,0o600)
        self.assertNotIn("captured_at",result)
        self.assertIn("relay_received_at",result)
        self.assertNotIn("synthetic-app-code-secret",json.dumps(result))
        self.assertNotIn("diagnostics",result)

    def test_opt_in_diagnostics_separates_no_delivery_from_rejected_context(self):
        before=self.context.read_bytes()
        no_delivery,_=self.run_receiver([],seconds=1,diagnostics=True,expect_timeout=True)
        self.assertEqual(no_delivery.diagnostics,{"local_connections":0,"authenticated_deliveries":0,
            "validated_observations":0,"rejected_observations":0,"last_rejection":None})
        expired=auth(NOW-timedelta(seconds=60),"different-expired-marker")
        rejected,outcomes=self.run_receiver([
            (observation(bundle()["authorization"]),{},"POST","/v1/context"),
            (observation(expired),{},"POST","/v1/context")],
            seconds=1,diagnostics=True,expect_timeout=True)
        self.assertEqual([row[0] for row in outcomes],[422,422])
        self.assertEqual(rejected.diagnostics,{"local_connections":2,"authenticated_deliveries":2,
            "validated_observations":2,"rejected_observations":2,
            "last_rejection":"capture_auth_expired"})
        self.assertEqual(self.context.read_bytes(),before)
        self.assertFalse(self.session.exists())
        text=json.dumps(rejected.diagnostics)
        for value in (expired,REF,"different-expired-marker","synthetic-app-code-secret",str(self.parent)):
            self.assertNotIn(value,text)

    def test_diagnostics_distinguishes_connections_authentication_and_payloads(self):
        actions=[(observation(),{"Authorization":"Bearer wrong"},"POST","/v1/context"),
                 (observation(),{"Content-Type":"text/plain"},"POST","/v1/context"),
                 (observation(),{},"POST","/v1/context")]
        result,outcomes=self.run_receiver(actions,diagnostics=True)
        self.assertEqual([row[0] for row in outcomes],[403,400,200])
        self.assertEqual(result["diagnostics"],{"local_connections":3,"authenticated_deliveries":2,
            "validated_observations":1,"rejected_observations":0,"last_rejection":None})
        self.assertTrue(result["session_cleanup_confirmed"])

    def test_diagnostics_rejected_payload_uses_fixed_safe_code(self):
        bad=observation();bad["response"]["store"]["private_account"]="private-canary"
        result,_=self.run_receiver([(bad,{},"POST","/v1/context"),
                                  (observation(),{},"POST","/v1/context")],diagnostics=True)
        self.assertEqual(result["diagnostics"],{"local_connections":2,"authenticated_deliveries":2,
            "validated_observations":1,"rejected_observations":1,
            "last_rejection":"bridge_invalid_input"})
        self.assertNotIn("private-canary",json.dumps(result))

    def test_wrong_auth_origin_host_and_path_cannot_commit(self):
        actions=[(observation(),headers,"POST",path) for headers,path in
            [({"Authorization":"Bearer wrong"},"/v1/context"),({"Origin":"https://evil.invalid"},"/v1/context"),
             ({"Host":"evil.invalid"},"/v1/context"),({},"/other")]]
        actions.append((observation(),{},"POST","/v1/context"))
        _,outcomes=self.run_receiver(actions)
        self.assertEqual([x[0] for x in outcomes],[403,403,403,403,200])

    def test_same_context_and_expired_new_context_do_not_commit(self):
        expired=auth(NOW-timedelta(seconds=60),"different-expired-marker")
        actions=[(observation(bundle()["authorization"]),{},"POST","/v1/context"),
                 (observation(expired),{},"POST","/v1/context"),
                 (observation(),{},"POST","/v1/context")]
        _,outcomes=self.run_receiver(actions)
        self.assertEqual([x[0] for x in outcomes],[422,422,200])
        self.assertEqual(outcomes[0][1]["error_code"],"bridge_context_unchanged")
        self.assertEqual(outcomes[1][1]["error_code"],"capture_auth_expired")

    def test_timeout_removes_session_and_keeps_old_context(self):
        before=self.context.read_bytes()
        with self.assertRaises(BridgeError) as raised:
            receive_context(credentials_file=self.context,session_file=self.session,
                            revision=2,store_ids=("900001",),seconds=1)
        self.assertEqual(raised.exception.error_code,"bridge_timeout")
        self.assertFalse(self.session.exists());self.assertEqual(self.context.read_bytes(),before)

    def test_session_existing_file_and_symlink_are_not_overwritten(self):
        self.session.write_text("existing-private-marker");self.session.chmod(0o600)
        for kind in ("regular","symlink"):
            if kind=="symlink":self.session.unlink();self.session.symlink_to(self.context)
            before=self.context.read_bytes()
            with self.assertRaises(BridgeError) as raised:
                receive_context(credentials_file=self.context,session_file=self.session,
                                revision=2,store_ids=("900001",),seconds=1)
            self.assertEqual(raised.exception.error_code,"bridge_session_unsafe")
            self.assertEqual(self.context.read_bytes(),before)
            if kind=="regular":self.assertEqual(self.session.read_text(),"existing-private-marker")

    def test_changed_session_file_is_preserved_and_cleanup_reported(self):
        workers=[];failures=[]
        def ready(_):
            settings=json.loads(self.session.read_text());parts=urlsplit(settings["url"])
            replacement=self.parent/"replacement-session.json"
            replacement.write_text("new-private-file");replacement.chmod(0o600);replacement.replace(self.session)
            def work():
                try:
                    connection=http.client.HTTPConnection("127.0.0.1",parts.port,timeout=2)
                    connection.request("POST","/v1/context",body=json.dumps(observation()).encode(),headers={
                        "Content-Type":"application/json","Authorization":"Bearer "+settings["token"]})
                    response=connection.getresponse();response.read();connection.close()
                except Exception as error:failures.append(type(error).__name__)
            thread=threading.Thread(target=work);thread.start();workers.append(thread)
        result=receive_context(credentials_file=self.context,session_file=self.session,
            revision=2,store_ids=("900001",),seconds=3,on_ready=ready)
        for worker in workers:worker.join(3)
        self.assertFalse(failures);self.assertFalse(result["session_cleanup_confirmed"])
        self.assertEqual(self.session.read_text(),"new-private-file")

    def test_session_write_failure_preserves_concurrent_replacement(self):
        replacement=self.parent/"replacement-session.json"
        replacement.write_text("replacement-must-survive");replacement.chmod(0o600)
        def fail_after_replace(fd, data):
            replacement.replace(self.session)
            raise OSError("synthetic write failure")
        with patch("sushiwait.bridge.os.write",side_effect=fail_after_replace):
            with self.assertRaises(BridgeError) as raised:
                _create_session(self.session,{"synthetic":True})
        self.assertEqual(raised.exception.error_code,"bridge_session_unsafe")
        self.assertEqual(self.session.read_text(),"replacement-must-survive")

    def test_partial_session_write_failure_removes_only_own_file(self):
        original_write=os.write
        def partial_then_fail(fd, data):
            original_write(fd,data[:5])
            raise OSError("synthetic partial write failure")
        with patch("sushiwait.bridge.os.write",side_effect=partial_then_fail):
            with self.assertRaises(BridgeError):_create_session(self.session,{"synthetic":True})
        self.assertFalse(self.session.exists())

    def test_receiver_invalid_headers_size_and_method_then_valid_context(self):
        actions=[(observation(),{"Content-Type":"text/plain"},"POST","/v1/context"),
                 (observation(),{},"GET","/v1/context"),
                 (observation(),{"Content-Length":str(MAX_OBSERVATION_BYTES+1)},"POST","/v1/context"),
                 (observation(),{},"POST","/v1/context")]
        _,outcomes=self.run_receiver(actions)
        self.assertEqual([x[0] for x in outcomes],[400,501,400,200])

    def test_bounds_revision_and_profile_rejected_before_bind(self):
        options=[{"seconds":0},{"seconds":61},{"revision":1},{"store_ids":()},
                 {"store_ids":("900001","900002","900003","900004")},{"session_file":self.context},
                 {"diagnostics":1},{"diagnostics":"true"}]
        for option in options:
            args=dict(credentials_file=self.context,session_file=self.session,revision=2,store_ids=("900001",),seconds=1)
            args.update(option)
            with self.subTest(option=list(option)),patch("sushiwait.bridge.HTTPServer.__init__",side_effect=AssertionError("must not bind")):
                with self.assertRaises((BridgeError,CaptureError)):receive_context(**args)


if __name__ == "__main__":unittest.main()
