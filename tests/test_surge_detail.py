"""Store-bound normal intake; synthetic records, no native or restaurant queries."""
import contextlib
from datetime import timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sushiwait import surge
from sushiwait.cli import main
from sushiwait.credentials import read_credentials_file
from tests.test_surge import FIELDS, NOW, REF, URL, auth, home_row, previous, row, summary

DETAIL = "https://sapi.sushiro.com.cn/gateway/wechat/api/2.0/getStoreById?storeId=3030"


def detail(store="3030"):
    value = row()
    url = DETAIL.replace("3030", store)
    value["URL"] = url
    value["requestHeader"] = value["requestHeader"].replace(
        URL.removeprefix("https://sapi.sushiro.com.cn"),
        url.removeprefix("https://sapi.sushiro.com.cn"))
    return value


class DetailParserTests(unittest.TestCase):
    def inspect(self, rows=None, **changes):
        return surge.inspect_summary(summary([detail()] if rows is None else rows),
            **{"previous": previous(), "since": NOW-timedelta(seconds=1), "now": NOW,
               "query_source": "detail", "query_store_id": "3030", **changes})

    def test_explicit_detail_accepts_only_selected_store_in_mixed_sources(self):
        other = detail("3004")
        other["requestHeader"] = other["requestHeader"].replace(auth(), auth("other"))
        result = self.inspect([row(), home_row(), other, detail()])
        self.assertEqual(result.context.authorization, auth())
        self.assertEqual(result.received_at, NOW)
        for secret in (auth(), REF, "synthetic-code-secret", "ignored-cookie"):
            self.assertNotIn(secret, repr(result))
        with self.assertRaisesRegex(surge.SurgeError, "surge_candidate_not_found"):
            self.inspect([other])

    def test_missing_invalid_or_unrelated_store_selection_is_side_effect_free(self):
        for store in (None, True, 3030, "", "-1", "3.0", "３０３０", "1"*20, "3030\n"):
            with self.subTest(store=store), self.assertRaisesRegex(surge.SurgeError, "surge_invalid_input"):
                self.inspect(query_store_id=store)
        for source in ("directory", "home"):
            with self.assertRaisesRegex(surge.SurgeError, "surge_invalid_input"):
                self.inspect(query_source=source)

    def test_request_metadata_and_line_both_match_exact_store_and_path(self):
        for replacement in (DETAIL.replace("3030", "3004"), DETAIL+"&storeId=3030",
                            DETAIL+"&user=secret", DETAIL+"#secret",
                            DETAIL.replace("3030", "%33%30%33%30"),
                            DETAIL.replace("sapi.", "crm-cn-prd."),
                            DETAIL.replace(".cn/", ".cn:443/"),
                            DETAIL.replace("getStoreById", "initialize")):
            for field in ("URL", "requestHeader"):
                value = detail()
                if field == "URL":
                    value[field] = replacement
                else:
                    value[field] = value[field].replace(DETAIL.removeprefix("https://sapi.sushiro.com.cn"), replacement)
                with self.subTest(replacement=replacement, field=field), self.assertRaises(surge.SurgeError):
                    self.inspect([value])

    def test_detail_still_requires_current_success_new_same_app_complete_context(self):
        for mutate in (lambda v:v.update(completedDate=NOW.timestamp()-978307200-2),
                       lambda v:v.update(responseHeader="HTTP/1.1 401 Unauthorized"),
                       lambda v:v.update(method="POST"),
                       lambda v:v.update(streamHasRequestBody=True),
                       lambda v:v.update(requestHeader=v["requestHeader"].replace(auth(), auth("old"))),
                       lambda v:v.update(requestHeader=v["requestHeader"].replace(auth(), auth(expiry=NOW+timedelta(seconds=30)))),
                       lambda v:v.update(requestHeader=v["requestHeader"].replace(REF, REF.replace("wx0000000000000000", "wx1111111111111111"))),
                       lambda v:v.update(requestHeader=v["requestHeader"].replace("X-App-Code: synthetic-code-secret\r\n", ""))):
            value = detail(); mutate(value)
            with self.assertRaises(surge.SurgeError): self.inspect([value])


class DetailIntakeTests(unittest.TestCase):
    def test_atomic_detail_intake_preserves_guards_and_safe_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve(); parent.chmod(0o700)
            file = parent / "context.json"
            file.write_text(json.dumps({"schema_version":1,"api_profile":"miniapp_gateway",
                "revision":1,"authorization":auth("old"),**FIELDS})); file.chmod(0o600)
            with patch("sushiwait.surge.sys.platform", "darwin"), patch("sushiwait.surge._clock", return_value=NOW), \
                 patch("sushiwait.surge._read_summary", return_value=summary([detail()])), \
                 patch("socket.socket", side_effect=AssertionError("no network")):
                result = surge.receive_summary(credentials_file=file, revision=2, seconds=1,
                    query_source="detail", query_store_id="3030")
            self.assertEqual(read_credentials_file(file, api_profile="miniapp_gateway").revision, 2)
            self.assertEqual(result["credential_source"], "normal_detail_query_summary")
            self.assertFalse(result["network_verified"])
            self.assertTrue(result["durability_confirmed"])
            for secret in (auth(), REF, "synthetic-code-secret", str(file)):
                self.assertNotIn(secret, json.dumps(result))

    def test_invalid_selection_never_reads_native_or_private_context(self):
        with patch("sushiwait.surge.read_credentials_file") as private, patch("sushiwait.surge._read_summary") as native:
            with self.assertRaisesRegex(surge.SurgeError, "surge_invalid_input"):
                surge.receive_summary(credentials_file="/unused", revision=2, query_source="detail")
            private.assert_not_called(); native.assert_not_called()

    def test_both_cli_paths_forward_explicit_detail_store(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with patch("sushiwait.cli.receive_summary", return_value={"committed":True}) as receive:
                self.assertEqual(main(["context-surge", "--credentials-file", "/synthetic/private",
                    "--revision", "2", "--query-source", "detail", "--query-store-id", "3030"]), 0)
            with patch("sushiwait.cli.run_window", return_value={"durability_confirmed":True}) as window:
                self.assertEqual(main(["context-window", "--credentials-file", "/synthetic/main",
                    "--staged-file", "/synthetic/stage", "--revision", "2", "--collector-paused",
                    "--query-source", "detail", "--query-store-id", "3030"]), 0)
        for call in (receive.call_args, window.call_args):
            self.assertEqual(call.kwargs["query_source"], "detail")
            self.assertEqual(call.kwargs["query_store_id"], "3030")
