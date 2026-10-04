"""Collector pause/resume and a real synthetic loopback provider handoff."""
import contextlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from sushiwait.bridge import receive_context
from sushiwait.cli import main
from sushiwait.client import QueryResult
from sushiwait.credentials import read_credentials_file
from test_bridge import NOW, auth, bundle, observation
from datetime import timedelta


class Clock:
    def __init__(self, callback=None): self.value=0;self.callback=callback;self.sleeps=[]
    def monotonic(self):return self.value
    def sleep(self,seconds):
        self.value+=seconds;self.sleeps.append(seconds)
        if self.callback:self.callback(self.value)


class CredentialRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.parent=Path(self.tmp.name).resolve();self.parent.chmod(0o700)
        self.context=self.parent/"context.json";self.db=self.parent/"observations.sqlite3"
        self.replace(bundle());self.requests=[];self.constructions=[]

    def replace(self,value):
        next_file=self.parent/"next-context.json"
        next_file.write_text(json.dumps(value));next_file.chmod(0o600);next_file.replace(self.context)

    def run_collect(self,clock,*,wait=3,samples=1,extra=None,http_ok=True,wall_clock=None):
        outer=self
        class Client:
            def __init__(self,context):self.context=context
            def fetch_store(self,store_id):
                outer.requests.append((clock.value,self.context,store_id))
                if not http_ok:return QueryResult(False,None,"http_auth_error",401,"start","end",1)
                now=NOW.isoformat()
                return QueryResult(True,{"id":int(store_id),"name":"合成门店","wait":20},None,200,now,now,1)
        def make_client(args,*,credentials):
            self.constructions.append(credentials)
            return Client(credentials)
        args=["collect","--api-profile","miniapp_gateway","--store-id","900001",
              "--credentials-file",str(self.context),"--db",str(self.db),"--samples",str(samples),
              "--interval","30","--wait-for-credentials",str(wait)]
        if extra:args+=extra
        output=io.StringIO()
        with contextlib.redirect_stdout(output), \
             patch("sushiwait.cli.client_for",side_effect=make_client), \
             patch("sushiwait.cli._utc_clock",side_effect=wall_clock or (lambda:NOW)), \
             patch("sushiwait.cli.time.monotonic",side_effect=clock.monotonic), \
             patch("sushiwait.cli.time.sleep",side_effect=clock.sleep):
            code=main(args)
        rows=[json.loads(line) for line in output.getvalue().splitlines()]
        for secret in ("old-synthetic-private-marker","new-synthetic-private-marker","synthetic-app-code-secret"):
            self.assertNotIn(secret,output.getvalue())
        return code,rows

    def fresh(self,revision=2):return bundle(revision,auth(NOW+timedelta(hours=1)))

    def test_expired_start_resumes_complete_new_context_and_keeps_sampling_interval(self):
        def update(t):
            if t==1:
                fresh=self.fresh();fresh["app_code"]="fresh-synthetic-code";self.replace(fresh)
        clock=Clock(update);code,rows=self.run_collect(clock,samples=2)
        self.assertEqual(code,0)
        self.assertEqual([t for t,_,_ in self.requests],[1,31])
        self.assertEqual([ctx.revision for _,ctx,_ in self.requests],[2,2])
        self.assertEqual(self.requests[0][1].app_code,"fresh-synthetic-code")
        self.assertEqual([r["event"] for r in rows if "event" in r],["credentials_paused","credentials_resumed"])
        self.assertEqual(sum(r.get("failure_phase")=="preflight" for r in rows),1)

    def test_default_expiry_still_stops_without_wait_or_http(self):
        clock=Clock();code,rows=self.run_collect(clock,wait=0)
        self.assertEqual(code,1);self.assertFalse(self.requests);self.assertFalse(clock.sleeps)
        self.assertEqual(rows[0]["error_code"],"auth_declared_expired")

    def test_timeout_never_constructs_client_and_does_not_repeat_failure_rows(self):
        clock=Clock();code,rows=self.run_collect(clock)
        self.assertEqual(code,1);self.assertEqual(clock.value,3)
        self.assertFalse(self.requests);self.assertFalse(self.constructions)
        self.assertEqual(rows[-1]["error_code"],"credentials_wait_timeout")
        self.assertEqual(sum(r.get("failure_phase")=="preflight" for r in rows),1)

    def test_corruption_during_pause_stops_with_safe_error(self):
        clock=Clock(lambda t:self.context.write_text("broken-private-marker") if t==1 else None)
        code,rows=self.run_collect(clock)
        self.assertEqual(code,1);self.assertFalse(self.requests)
        self.assertEqual(rows[-1]["error_code"],"credentials_file_invalid_json")

    def test_revision_rollback_during_pause_stops(self):
        self.replace(bundle(2))
        clock=Clock(lambda t:self.replace(self.fresh(1)) if t==1 else None)
        code,rows=self.run_collect(clock)
        self.assertEqual(code,1);self.assertFalse(self.requests)
        self.assertEqual(rows[-1]["error_code"],"credentials_revision_rollback")

    def test_same_authorization_with_new_revision_and_clock_rollback_cannot_resume(self):
        old=bundle()["authorization"]
        clock=Clock(lambda t:self.replace(bundle(2,old)) if t==1 else None)
        code,rows=self.run_collect(clock,wall_clock=lambda:NOW if clock.value==0 else NOW-timedelta(hours=2))
        self.assertEqual(code,1);self.assertFalse(self.requests)
        self.assertEqual(rows[-1]["error_code"],"credentials_wait_timeout")

    def test_new_valid_file_at_wait_deadline_does_not_send_query(self):
        clock=Clock(lambda t:self.replace(self.fresh()) if t==3 else None)
        code,rows=self.run_collect(clock)
        self.assertEqual(code,1);self.assertFalse(self.requests)
        self.assertEqual(rows[-1]["error_code"],"credentials_wait_timeout")

    def test_upstream_failure_after_resume_stops_without_another_pause(self):
        clock=Clock(lambda t:self.replace(self.fresh()) if t==1 else None)
        code,rows=self.run_collect(clock,http_ok=False)
        self.assertEqual(code,1);self.assertEqual(len(self.requests),1)
        self.assertEqual(rows[-1]["http_status"],401)
        self.assertEqual(sum(r.get("event")=="credentials_paused" for r in rows),1)

    def test_interval_expiry_resumes_then_waits_full_interval_without_catchup(self):
        self.replace(bundle(1,auth(NOW+timedelta(seconds=35),"short-synthetic-marker")))
        def update(t):
            if t==6:self.replace(self.fresh())
        clock=Clock(update)
        code,rows=self.run_collect(clock,samples=2,wall_clock=lambda:NOW+timedelta(seconds=clock.value))
        self.assertEqual(code,0)
        self.assertEqual([t for t,_,_ in self.requests],[0,36])
        self.assertEqual([c.revision for _,c,_ in self.requests],[1,2])
        self.assertEqual(sum(r.get("event")=="credentials_resumed" for r in rows),1)

    def test_invalid_wait_bounds_reject_before_opening_database(self):
        for seconds in (-1,601):
            with self.subTest(seconds=seconds):
                code,rows=self.run_collect(Clock(),wait=seconds)
                self.assertEqual(code,2);self.assertFalse(self.db.exists())
                self.assertEqual(rows[0]["error_code"],"invalid_credential_wait_bounds")
        out=io.StringIO()
        with contextlib.redirect_stdout(out):
            code=main(["collect","--store-id","900001","--wait-for-credentials","1","--db",str(self.db)])
        self.assertEqual(code,2);self.assertFalse(self.db.exists())

    def test_real_loopback_provider_unblocks_same_collector_process(self):
        session=self.parent/"bridge-session.json";ready=threading.Event();results=[];errors=[]
        # The real receiver clock must be independent of the collector's fake clock.
        import time
        real_monotonic=time.monotonic
        def provider():
            try:
                result=receive_context(credentials_file=self.context,session_file=session,revision=2,
                    store_ids=("900001",),seconds=5,on_ready=lambda _:ready.set())
                results.append(result)
            except Exception as error:errors.append(type(error).__name__)
        thread=threading.Thread(target=provider);thread.start()
        self.assertTrue(ready.wait(2))
        def post(t):
            if t!=1:return
            settings=json.loads(session.read_text());url=urlsplit(settings["url"])
            connection=http.client.HTTPConnection("127.0.0.1",url.port,timeout=2)
            connection.request("POST","/v1/context",body=json.dumps(observation()).encode(),headers={
                "Content-Type":"application/json","Authorization":"Bearer "+settings["token"]})
            response=connection.getresponse();self.assertEqual(response.status,200);response.read();connection.close()
        # cli and bridge normally share time; bind bridge clock explicitly here.
        with patch("sushiwait.bridge.time", wraps=time) as bridge_time:
            bridge_time.monotonic.side_effect=real_monotonic
            code,rows=self.run_collect(Clock(post))
        thread.join(3)
        self.assertFalse(thread.is_alive());self.assertFalse(errors)
        self.assertEqual(code,0);self.assertTrue(results[0]["committed"])
        self.assertEqual(self.requests[0][1].revision,2)
        self.assertEqual(read_credentials_file(self.context,api_profile="miniapp_gateway").revision,2)
        self.assertFalse(session.exists())
        self.assertEqual(sum(r.get("event")=="credentials_resumed" for r in rows),1)


if __name__ == "__main__":unittest.main()
