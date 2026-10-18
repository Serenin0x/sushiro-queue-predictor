"""Proactive authorization uses a fixed private broker; no real network calls."""
from io import BytesIO
import json
from pathlib import Path
import tempfile
import urllib.error
import unittest
from unittest.mock import Mock,patch
from sushiwait.authorizationlink import issue_link
from sushiwait.manualcollection import ManualError


class AuthorizationLinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.key=Path(self.tmp.name).resolve()/'key';self.key.write_bytes(b'k'*32);self.key.chmod(0o600)
        self.origin='https://124.222.57.236'

    def reply(self,value):
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        response.read.return_value=json.dumps(value).encode();return response

    def test_fixed_loopback_body_and_key_never_reach_browser(self):
        with patch('urllib.request.build_opener') as factory:
            factory.return_value.open.return_value=self.reply({'url':self.origin+'/recover/#'+'a'*43})
            self.assertEqual(issue_link(self.key,self.origin),'/recover/#'+'a'*43)
            request=factory.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url,'http://127.0.0.1:18910/internal/issue')
            self.assertEqual(request.data,b'{"verification":true}')
            self.assertEqual(request.get_header('X-sushiwait-internal'),(b'k'*32).hex())
            self.assertIsNone(factory.call_args.args[1].redirect_request(None,None,None,None,None,None))

    def test_redirect_foreign_url_oversize_and_unexpected_fields_rejected(self):
        for value in [{'url':'https://other.invalid/recover/#'+'a'*43}, {'url':self.origin+'/recover/#'+'a'*44},
                      {'url':self.origin+'/recover/#'+'a'*43,'secret':'private'}, {'url':self.origin+'/recover/?cap='+'a'*43}, {'url':9}]:
            with self.subTest(value=value),patch('urllib.request.build_opener') as factory:
                factory.return_value.open.return_value=self.reply(value)
                with self.assertRaisesRegex(ManualError,'unavailable'):issue_link(self.key,self.origin)
        with patch('urllib.request.build_opener') as factory:
            factory.return_value.open.return_value=self.reply({'url':'x'*1100})
            with self.assertRaises(ManualError):issue_link(self.key,self.origin)

    def test_non_auth_fault_and_unknown_response_are_not_retried(self):
        for code,reason in [(409,'blocked'),(503,'unavailable'),(302,'unavailable')]:
            with patch('urllib.request.build_opener') as factory:
                factory.return_value.open.side_effect=urllib.error.HTTPError('private',code,'private',{},BytesIO(b'private'))
                with self.assertRaisesRegex(ManualError,reason):issue_link(self.key,self.origin)
                self.assertEqual(factory.return_value.open.call_count,1)
