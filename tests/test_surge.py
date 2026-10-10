"""Synthetic normal directory summaries; no installed Surge or upstream calls."""
import base64
import copy
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sushiwait import surge
from sushiwait.cli import main
from sushiwait.credentials import _context, read_credentials_file

NOW = datetime.now(timezone.utc)
URL = "https://sapi.sushiro.com.cn/gateway/wechat/api/2.0/stores?latitude=1&longitude=1&numresults=10000"
REF = "https://servicewechat.com/wx0000000000000000/1/page-frame.html"
FIELDS = {"app_client": "synthetic-miniapp", "app_code": "synthetic-code-secret",
          "user_agent": "Synthetic Agent secret/1.0", "referer": REF, "content_type": "application/json"}


def auth(marker="new", expiry=None):
    claims = {"iat": int(NOW.timestamp()), "exp": int((expiry or NOW+timedelta(hours=1)).timestamp()), "private": "synthetic-"+marker+"-secret"}
    return "Bearer e30." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".c2ln"


def previous():
    return _context("miniapp_gateway", 1, auth("old"), FIELDS)


def row():
    names = {"authorization": "Authorization", "app_client": "X-App-Client", "app_code": "X-App-Code", "user_agent": "User-Agent", "referer": "Referer", "content_type": "Content-Type"}
    fields = {"authorization": auth(), **FIELDS}
    headers = "\r\n".join(names[k]+": "+v for k,v in fields.items())
    return {"URL": URL, "method": "GET", "completed": True, "failed": False,
            "streamHasRequestBody": False, "completedDate": NOW.timestamp()-978307200,
            "requestHeader": "GET "+URL.removeprefix("https://sapi.sushiro.com.cn")+" HTTP/1.1\r\n"+headers+"\r\nCookie: synthetic-ignored-cookie-secret\r\n\r\n",
            "responseHeader": "HTTP/1.1 200 OK\r\nSet-Cookie: synthetic-ignored-response-secret\r\n\r\n",
            "notes": ["synthetic-ignored-note-secret"]}


def summary(rows=None):
    return json.dumps({"recent-requests": [row()] if rows is None else rows,
                       "active-requests": [], "persistent-store": None}).encode()


def home_row():
    value = row()
    url = ("https://sapi.sushiro.com.cn/api/1.3/home?city=10&nonce=ABC123&ts="
           + str(int(NOW.timestamp())) + "&sign=" + "a" * 64)
    value["URL"] = url
    value["requestHeader"] = value["requestHeader"].replace(
        URL.removeprefix("https://sapi.sushiro.com.cn"),
        url.removeprefix("https://sapi.sushiro.com.cn"))
    return value


class HomeSummaryTests(unittest.TestCase):
    def inspect(self, rows=None, **changes):
        return surge.inspect_summary(summary([home_row()] if rows is None else rows),
            **{"previous": previous(), "since": NOW-timedelta(seconds=1),
               "now": NOW, "query_source": "home", **changes})

    def test_success_is_explicit_and_contains_no_signed_parameters(self):
        candidate = self.inspect()
        self.assertEqual(candidate.context.authorization, auth())
        self.assertEqual(candidate.received_at, NOW)
        for secret in (auth(), REF, "ABC123", "a" * 64, "ignored-cookie"):
            self.assertNotIn(secret, repr(candidate))

    def test_sources_do_not_fall_back_or_select_other_kind_in_mixed_summary(self):
        with self.assertRaisesRegex(surge.SurgeError, "surge_candidate_not_found"):
            self.inspect([row()])
        with self.assertRaisesRegex(surge.SurgeError, "surge_candidate_not_found"):
            self.inspect(query_source="directory")
        other = row()
        other["requestHeader"] = other["requestHeader"].replace(auth(), auth("other"))
        self.assertEqual(self.inspect([other, home_row()]).context.authorization, auth())

    def test_unknown_source_is_rejected_before_processing_content(self):
        for source in (None, [], True, "auto", "login", "initialize"):
            with self.subTest(source=source), self.assertRaisesRegex(surge.SurgeError, "surge_invalid_input"):
                self.inspect(query_source=source)

    def test_only_observed_home_shape_is_accepted(self):
        original = home_row()["URL"]
        for url in (original.replace("sapi.", "crm-cn-prd."),
                    original.replace(".cn/", ".cn:443/"),
                    original.replace("/home?", "/initialize?"),
                    original.replace("city=10", "city=11"),
                    original.replace("nonce=ABC123", "nonce=ABC12"),
                    original.replace("nonce=ABC123", "nonce=%41BC123"),
                    original.replace("sign=" + "a" * 64, "sign=" + "a" * 63),
                    original + "&city=10", original + "&member_id=secret", original + "#fragment"):
            value = home_row(); value["URL"] = url
            with self.subTest(url=url), self.assertRaisesRegex(surge.SurgeError, "surge_candidate_not_found"):
                self.inspect([value])

    def test_request_line_cannot_supply_different_signed_request(self):
        for before, after in (("ABC123", "DEF456"), ("a" * 64, "b" * 64),
                              ("city=10", "city=11")):
            value = home_row()
            value["requestHeader"] = value["requestHeader"].replace(before, after)
            with self.subTest(before=before), self.assertRaises(surge.SurgeError):
                self.inspect([value])

    def test_signed_time_must_be_recent_and_not_in_future(self):
        original = str(int(NOW.timestamp()))
        for stamp in (str(int(NOW.timestamp()) - 61), str(int(NOW.timestamp()) + 1)):
            value = home_row()
            value["URL"] = value["URL"].replace(original, stamp)
            value["requestHeader"] = value["requestHeader"].replace(original, stamp)
            with self.subTest(stamp=stamp), self.assertRaises(surge.SurgeError):
                self.inspect([value])

    def test_home_still_requires_new_complete_same_app_successful_get(self):
        for change in (lambda v:v.update(method="POST"), lambda v:v.update(failed=True),
                       lambda v:v.update(streamHasRequestBody=True),
                       lambda v:v.update(responseHeader="HTTP/1.1 401 Unauthorized"),
                       lambda v:v.update(requestHeader=v["requestHeader"].replace(auth(),auth("old"))),
                       lambda v:v.update(requestHeader=v["requestHeader"].replace(REF,REF.replace("wx0000000000000000","wx1111111111111111"))),
                       lambda v:v.update(requestHeader=v["requestHeader"].replace("X-App-Code: synthetic-code-secret\r\n","")),
                       lambda v:v.update(requestHeader=v["requestHeader"].replace(auth(),auth(expiry=NOW+timedelta(seconds=30))))):
            value = home_row(); change(value)
            with self.assertRaises(surge.SurgeError): self.inspect([value])


class SummaryParserTests(unittest.TestCase):
    def inspect(self, body=None, **kwargs):
        return surge.inspect_summary(summary() if body is None else body, previous=previous(),
                                     since=NOW-timedelta(seconds=1), now=NOW, **kwargs)

    def test_success_keeps_complete_context_private_and_reference_epoch(self):
        value = self.inspect()
        self.assertEqual(value.context.authorization, auth())
        self.assertEqual(value.received_at, NOW)
        for secret in (auth(), REF, "synthetic-code-secret", "ignored-cookie", "ignored-note"):
            self.assertNotIn(secret, repr(value))

    def test_different_origins_paths_and_queries_are_ignored(self):
        for url in (URL.replace("sapi.", "crm-cn-prd."), URL+"&latitude=1", URL+"#fragment",
                    URL.replace("latitude=1", "latitude=2"), URL.replace("stores?", "initialize?"),
                    URL.replace(".cn/", ".cn:443/"), "\n"+URL, URL+"&unexpected=1"):
            with self.subTest(url=url):
                value=row();value["URL"]=url
                with self.assertRaisesRegex(surge.SurgeError, "surge_candidate_not_found"):
                    self.inspect(summary([value]))

    def test_request_line_must_match_directory_origin_and_parameters(self):
        value=row();value["requestHeader"]=value["requestHeader"].replace("latitude=1", "latitude=2")
        with self.assertRaises(surge.SurgeError): self.inspect(summary([value]))

    def test_incomplete_failed_non_get_or_request_body_is_not_success(self):
        for key, value in (("completed",False),("failed",True),("method","POST"),
                           ("streamHasRequestBody",True),("responseHeader","HTTP/1.1 401 Unauthorized")):
            data=row();data[key]=value
            with self.subTest(key=key), self.assertRaises(surge.SurgeError):self.inspect(summary([data]))

    def test_observed_unix_time_and_sixty_second_window(self):
        data=row();data["completedDate"]=NOW.timestamp()
        self.assertEqual(self.inspect(summary([data])).received_at,NOW)
        with self.assertRaisesRegex(surge.SurgeError,"surge_invalid_input"):
            surge.inspect_summary(summary(),previous=previous(),since=NOW-timedelta(seconds=61),now=NOW)

    def test_pre_window_future_boolean_and_unix_epoch_times_are_ignored(self):
        for date in (NOW.timestamp()-978307200-2, NOW.timestamp()-978307200+2, True, NOW.timestamp()+2):
            data=row();data["completedDate"]=date
            with self.subTest(date=date), self.assertRaises(surge.SurgeError):self.inspect(summary([data]))

    def test_all_six_headers_must_come_from_one_record(self):
        a,b=row(),row()
        a["requestHeader"]=a["requestHeader"].replace("X-App-Code: synthetic-code-secret\r\n", "")
        b["requestHeader"]=b["requestHeader"].replace("Content-Type: application/json\r\n", "")
        with self.assertRaisesRegex(surge.SurgeError,"surge_context_incomplete"):self.inspect(summary([a,b]))

    def test_duplicate_and_folded_headers_are_rejected(self):
        for extra in ("authorization: synthetic-duplicate-secret\r\n", " folded-private-secret\r\n"):
            data=row();data["requestHeader"]=data["requestHeader"].replace("\r\n\r\n", "\r\n"+extra+"\r\n")
            with self.subTest(extra=extra), self.assertRaisesRegex(surge.SurgeError,"surge_context_invalid"):
                self.inspect(summary([data]))

    def test_wrong_app_and_unchanged_authorization_are_distinct(self):
        for old,new,code in ((REF,REF.replace("wx0000000000000000","wx1111111111111111"),"surge_app_mismatch"),
                             (auth(),auth("old"),"surge_context_unchanged")):
            data=row();data["requestHeader"]=data["requestHeader"].replace(old,new)
            with self.assertRaisesRegex(surge.SurgeError,code):self.inspect(summary([data]))

    def test_unknown_expiry_expired_and_margin_do_not_commit(self):
        for value in ("Bearer synthetic-opaque-secret",auth(expiry=NOW-timedelta(seconds=1)),auth(expiry=NOW+timedelta(seconds=30))):
            data=row();data["requestHeader"]=data["requestHeader"].replace(auth(),value)
            with self.assertRaisesRegex(surge.SurgeError,"surge_auth_guard_stop"):self.inspect(summary([data]))

    def test_latest_complete_context_is_selected_and_ties_require_equality(self):
        a,b=row(),row();a["completedDate"]-=0.5
        a["requestHeader"]=a["requestHeader"].replace(auth(),auth("older"))
        self.assertEqual(self.inspect(summary([a,b])).context.authorization,auth())
        a["completedDate"]=b["completedDate"]
        with self.assertRaisesRegex(surge.SurgeError,"surge_context_ambiguous"):self.inspect(summary([a,b]))

    def test_observed_two_hundred_recent_rows_are_supported_with_a_bound(self):
        self.assertEqual(self.inspect(summary([row()] * 200)).context.authorization, auth())
        with self.assertRaisesRegex(surge.SurgeError, "surge_summary_too_large"):
            self.inspect(summary([row()] * 201))

    def test_bounded_json_failure_contains_no_original_input(self):
        for body in (b'private-invalid-json',b'{"recent-requests":[],"recent-requests":[]}',b'{"recent-requests":NaN}',summary([row()]*201),b'x'*(surge._MAX_BYTES+1)):
            with self.assertRaises(surge.SurgeError) as caught:self.inspect(body)
            self.assertIsNone(caught.exception.__context__)
            self.assertNotIn("private-invalid-json",repr(caught.exception))


class SummaryIntakeTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.parent=Path(self.directory.name).resolve();self.parent.chmod(0o700)
        self.file=self.parent/"context.json"
        self.seed(1,auth("old"))
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("sushiwait.surge.sys.platform","darwin"))
        self.stack.enter_context(patch("sushiwait.surge._clock",return_value=NOW))
        self.stack.enter_context(patch("socket.socket",side_effect=AssertionError("no network")))
        self.stack.enter_context(patch("sushiwait.client.SushiroClient.__init__",side_effect=AssertionError("no upstream client")))

    def seed(self,revision,authorization):
        self.file.write_text(json.dumps({"schema_version":1,"api_profile":"miniapp_gateway","revision":revision,"authorization":authorization,**FIELDS}))
        self.file.chmod(0o600)

    def test_atomic_commit_reports_source_without_independent_acceptance(self):
        ready=[]
        with patch("sushiwait.surge._read_summary",return_value=summary()):
            result=surge.receive_summary(credentials_file=self.file,revision=2,seconds=1,on_ready=ready.append)
        self.assertEqual(read_credentials_file(self.file,api_profile="miniapp_gateway").authorization,auth())
        self.assertTrue(result["committed"]);self.assertTrue(result["durability_confirmed"])
        self.assertFalse(result["response_body_inspected"]);self.assertFalse(result["network_verified"])
        self.assertEqual(result["server_acceptance"],"unverified")
        self.assertEqual(result["credential_source"],"normal_directory_query_summary")
        self.assertEqual(ready[0]["event"],"surge_ready")
        text=json.dumps(result)
        for secret in (auth(),REF,"synthetic-code-secret",str(self.file)):
            self.assertNotIn(secret,text)

    def test_current_writer_change_stops_before_reading_summary(self):
        with patch("sushiwait.surge._read_summary") as reader:
            with self.assertRaisesRegex(surge.SurgeError,"surge_current_context_changed"):
                surge.receive_summary(credentials_file=self.file,revision=3,seconds=1,on_ready=lambda _:self.seed(2,auth("other")))
            reader.assert_not_called()
        self.assertEqual(read_credentials_file(self.file,api_profile="miniapp_gateway").revision,2)

    def test_home_atomic_intake_reuses_private_revision_guards_and_redacts_signed_query(self):
        ready = []
        with patch("sushiwait.surge._read_summary", return_value=summary([home_row()])):
            result = surge.receive_summary(credentials_file=self.file, revision=2,
                seconds=1, query_source="home", on_ready=ready.append)
        self.assertEqual(result["query_source"], "home")
        self.assertEqual(result["credential_source"], "normal_home_query_summary")
        self.assertEqual(ready[0]["query_source"], "home")
        self.assertEqual(read_credentials_file(self.file,api_profile="miniapp_gateway").revision,2)
        self.assertEqual(result["server_acceptance"], "unverified")
        for secret in (auth(), REF, "ABC123", "a" * 64, str(self.file)):
            self.assertNotIn(secret, json.dumps([result, ready]))

    def test_unknown_source_never_reads_private_file_or_native_summary(self):
        with patch("sushiwait.surge.read_credentials_file") as private, patch("sushiwait.surge._read_summary") as native:
            for source in (None, [], "auto"):
                with self.assertRaisesRegex(surge.SurgeError, "surge_invalid_input"):
                    surge.receive_summary(credentials_file=self.file, revision=2, query_source=source)
            private.assert_not_called(); native.assert_not_called()

    def test_invalid_bounds_and_unsafe_file_do_not_invoke_runtime(self):
        with patch("sushiwait.surge._read_summary") as reader:
            for seconds,revision in ((True,2),(0,2),(61,2),(1,True),(1,1)):
                with self.subTest(seconds=seconds,revision=revision),self.assertRaises(surge.SurgeError):
                    surge.receive_summary(credentials_file=self.file,revision=revision,seconds=seconds)
            self.file.chmod(0o644)
            with self.assertRaises(Exception):surge.receive_summary(credentials_file=self.file,revision=2,seconds=1)
            reader.assert_not_called()

    def test_missing_new_context_times_out_and_preserves_file(self):
        before=self.file.read_bytes()
        with patch("sushiwait.surge._read_summary",return_value=summary([])),patch("sushiwait.surge.time.monotonic",side_effect=[0,0,0,0,2]),patch("sushiwait.surge.time.sleep"):
            with self.assertRaisesRegex(surge.SurgeError,"surge_window_timeout"):
                surge.receive_summary(credentials_file=self.file,revision=2,seconds=1)
        self.assertEqual(self.file.read_bytes(),before)


class SummaryReaderTests(unittest.TestCase):
    def read_program(self,program,timeout=1):
        real=subprocess.Popen
        with patch("sushiwait.surge.sys.platform","darwin"),patch("sushiwait.surge.os.path.isfile",return_value=True),patch("sushiwait.surge.os.access",return_value=True),patch("sushiwait.surge.subprocess.Popen",side_effect=lambda _,**kw:real([sys.executable,"-c",program],**kw)):
            return surge._read_summary(timeout)

    def test_reader_keeps_stdout_only_and_reaps_normal_process(self):
        self.assertEqual(self.read_program("import sys;sys.stderr.write('ignored-private-secret');print('{}')"),b'{}\n')

    def test_large_output_failure_and_timeout_are_safe_and_reaped(self):
        for program,timeout,code in (("import sys;sys.stdout.write('x'*4194305)",1,"surge_summary_too_large"),
                                     ("import sys;sys.stderr.write('private-secret');sys.exit(1)",1,"surge_summary_read_failed"),
                                     ("import time;time.sleep(1)",0.02,"surge_summary_read_failed")):
            with self.subTest(program=program),self.assertRaisesRegex(surge.SurgeError,code):
                self.read_program(program,timeout)


class SummaryCliTests(unittest.TestCase):
    def test_explicit_home_cli_passes_selection(self):
        with patch("sushiwait.cli.receive_summary", return_value={"committed":True}) as receiver, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["context-surge", "--credentials-file", "/synthetic/private",
                                  "--revision", "2", "--query-source", "home"]), 0)
        self.assertEqual(receiver.call_args.kwargs["query_source"], "home")

    def test_help_and_dispatch_keep_source_and_no_secrets(self):
        output=io.StringIO()
        value={"committed":True,"server_acceptance":"unverified","network_performed":False}
        with patch("sushiwait.cli.receive_summary",return_value=value) as receiver,contextlib.redirect_stdout(output):
            code=main(["context-surge","--credentials-file","/synthetic/private","--revision","2","--seconds","1"])
        self.assertEqual(code,0);self.assertEqual(json.loads(output.getvalue()),{"ok":True,**value})
        self.assertEqual(receiver.call_args.kwargs["revision"],2)

    def test_runtime_rejection_is_fixed_and_offline(self):
        output=io.StringIO()
        with patch("sushiwait.cli.receive_summary",side_effect=surge.SurgeError("surge_unavailable")),contextlib.redirect_stdout(output):
            code=main(["context-surge","--credentials-file","/synthetic/private","--revision","2"])
        self.assertEqual(code,1);self.assertEqual(json.loads(output.getvalue()),{"ok":False,"error_code":"surge_unavailable","network_performed":False,"external_network_performed":False})


if __name__ == "__main__": unittest.main()
