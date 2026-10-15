"""SMTP transport boundary using a fake client; no real mail is sent."""
from copy import deepcopy
from pathlib import Path
import importlib.util
import json
import tempfile
import unittest
from unittest.mock import patch
from tests.test_collection_alerts import alerts, NOW

CONFIG={'schema_version':1,'provider':'qq_smtp','from_address':'synthetic-owner@qq.com',
        'to_address':'synthetic-owner@qq.com','authorization_code':'syntheticTest1234',
        'statistics_url':alerts.STATISTICS_URL}


class EmailAlertTests(unittest.TestCase):
    def test_only_owner_to_self_and_fixed_provider_is_accepted(self):
        self.assertEqual(alerts.validate_config(CONFIG),CONFIG)
        for change in ({'to_address':'somebody-else@qq.com'},{'from_address':'bad\nBcc: x@qq.com'},
                       {'from_address':'owner@example.com'}, {'smtp_host':'example.com'},
                       {'authorization_code':'QQ-password-with-spaces'}):
            with self.assertRaises(ValueError): alerts.validate_config({**CONFIG,**change})

    def test_multipart_contains_clickable_same_host_link_and_no_secret(self):
        link='https://124.222.57.236/recover/#'+'A'*43
        title,body=alerts.message('incident','collection_stopped',NOW,link)
        message=alerts.smtp_message(CONFIG,title,body)
        self.assertEqual(message['To'],CONFIG['to_address'])
        self.assertIn(link,message.get_body(preferencelist=('plain',)).get_content())
        self.assertIn('href="'+link+'"',message.get_body(preferencelist=('html',)).get_content())
        self.assertNotIn(CONFIG['authorization_code'],str(message))
        self.assertNotIn('<script>',alerts.smtp_message(CONFIG,title,'<script>bad</script>').get_body(preferencelist=('html',)).get_content())

    def test_tls_fixed_host_auth_and_single_envelope_recipient(self):
        with patch.object(alerts.smtplib,'SMTP_SSL') as connect:
            client=connect.return_value.__enter__.return_value
            client.send_message.return_value={}
            self.assertTrue(alerts.notify_send(CONFIG,'Synthetic test','no real delivery'))
            self.assertEqual(connect.call_args.args,('smtp.qq.com',465))
            self.assertTrue(connect.call_args.kwargs['context'].check_hostname)
            client.login.assert_called_once_with(CONFIG['from_address'],CONFIG['authorization_code'])
            self.assertEqual(client.send_message.call_args.kwargs['to_addrs'],[CONFIG['to_address']])
            self.assertEqual(client.method_calls[0][0],'login')

    def test_delivery_failure_is_durable_and_never_repeated_minutely(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(alerts.smtplib,'SMTP_SSL',side_effect=TimeoutError):
            path=Path(temp).resolve()/'mail-state.json'
            monitor=alerts.Monitor(path,CONFIG)
            self.assertEqual(monitor.step('collection_stopped',NOW),'unknown')
            restarted=alerts.Monitor(path,CONFIG)
            self.assertEqual(restarted.step('collection_stopped',NOW),'suppressed')

    def test_code_rotation_does_not_reset_same_recipient_state(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp).resolve()/'mail-state.json'
            monitor=alerts.Monitor(path,CONFIG,lambda *_:True)
            monitor.step('collection_stopped',NOW)
            changed={**CONFIG,'authorization_code':'differentCode123'}
            restarted=alerts.Monitor(path,changed,lambda *_:True)
            self.assertEqual(restarted.state['attempts_today'],1)
            self.assertEqual(restarted.step('collection_stopped',NOW),'suppressed')

    def test_mail_does_not_inherit_retired_free_wechat_five_message_limit(self):
        from datetime import timedelta
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp).resolve()/'mail-state.json'
            sent=[]
            monitor=alerts.Monitor(path,CONFIG,lambda *args:sent.append(args) is None)
            for index in range(22):
                monitor.step('collection_stopped' if index % 2 == 0 else 'healthy',
                             NOW+timedelta(minutes=index))
            self.assertEqual(len(sent),20)
            restarted=alerts.Monitor(path,CONFIG,lambda *_:True)
            self.assertEqual(restarted.state['attempts_today'],20)
            self.assertEqual(restarted.step('collection_stopped',NOW+timedelta(minutes=30)),'suppressed')

    def test_explicit_email_channel_never_falls_back_to_retired_wechat(self):
        spec=importlib.util.spec_from_file_location('worker',
            Path(__file__).resolve().parents[1]/'deploy/collection_alert_worker.py')
        worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve()
            def put(name,value):
                p=root/name;p.write_text(json.dumps(value));p.chmod(0o600)
            put('active-provider.json',{'schema_version':1,'provider':'qq_smtp'})
            put('email.private.json',CONFIG)
            path,config=worker.delivery_settings(root)
            self.assertEqual(path.name,'email-notification-state.json')
            self.assertEqual(config,CONFIG)
            (root/'email.private.json').unlink()
            with self.assertRaises(ValueError):worker.delivery_settings(root)


if __name__=='__main__':unittest.main()
