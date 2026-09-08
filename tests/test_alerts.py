"""Notification receipts must not mistake OS submission for user acknowledgement."""
import subprocess
import unittest
from unittest.mock import Mock, patch

from desk import alerts


class AlertReceipts(unittest.TestCase):
    def test_disabled_macos_never_opens_dialog(self):
        with patch.object(alerts, 'load_config', return_value={'alerts': {'macos': False, 'macos_mode': 'dialog'}}), \
             patch.object(alerts.subprocess, 'run') as run:
            self.assertIsNone(alerts.notify('title', 'body')['macos'])
        run.assert_not_called()

    def test_banner_receipt_does_not_claim_visible(self):
        with patch.object(alerts.subprocess, 'run', return_value=Mock(returncode=0, stdout='', stderr='')):
            receipt = alerts._macos('title', 'body', True, 'run', False)
        self.assertTrue(receipt['submitted'])
        self.assertIsNone(receipt['visible'])

    def test_dialog_receipts_and_argument_safety(self):
        title = 'title " & do shell script "unsafe'
        body = '1. first\nhttps://example.invalid/1\n2. second\n3. third'
        for output, acknowledged, expired in [('acknowledged', True, False), ('expired', False, True)]:
            with self.subTest(output=output), \
                 patch.object(alerts, 'load_config', return_value={'alerts': {'macos': True, 'macos_mode': 'dialog', 'sound': False}}), \
                 patch.object(alerts.subprocess, 'run', return_value=Mock(returncode=0, stdout=output, stderr='')) as run:
                receipt = alerts.notify(title, body)['macos']
            self.assertTrue(receipt['ok'])
            self.assertEqual(receipt['acknowledged'], acknowledged)
            self.assertEqual(receipt['expired'], expired)
            argv = run.call_args.args[0]
            self.assertEqual(argv[-3:], ['--', title, body])
            self.assertNotIn(title, ' '.join(argv[:-3]))
            self.assertNotIn(body, ' '.join(argv[:-3]))
            self.assertNotIn('display notification', ' '.join(argv))
            self.assertNotIn('sound name', ' '.join(argv))
            self.assertIn('giving up after 45', ' '.join(argv))
            self.assertEqual(run.call_args.kwargs['timeout'], 50)

    def test_dialog_timeout_or_invalid_receipt_is_not_acknowledged(self):
        for outcome in [subprocess.TimeoutExpired('osascript', 50), Mock(returncode=0, stdout='unexpected', stderr='')]:
            kwargs = {'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': outcome}
            with patch.object(alerts.subprocess, 'run', **kwargs):
                receipt = alerts._macos_dialog('title', 'body', False)
            self.assertFalse(receipt['ok'])
            self.assertFalse(receipt['acknowledged'])


class WindowReceipts(unittest.TestCase):
    def test_window_opens_only_fixed_local_result_url_with_encoded_identifier(self):
        run_id = 'saved &next=https://example.invalid/evil#fragment'
        with patch.object(alerts, 'load_config', return_value={'alerts': {'macos': True, 'macos_mode': 'window'}}), patch.object(alerts.subprocess, 'run', return_value=Mock(returncode=0, stderr='')) as run:
            receipt = alerts.notify('ignored', 'https://example.invalid/not-opened', run_id=run_id)['macos']
        self.assertEqual(run.call_args.args[0], ['open', 'http://127.0.0.1:8788/jobs?run=saved%20%26next%3Dhttps%3A%2F%2Fexample.invalid%2Fevil%23fragment'])
        self.assertEqual(run.call_args.kwargs['timeout'], 5)
        self.assertTrue(receipt['opened'])
        self.assertTrue(receipt['submitted'])
        self.assertIsNone(receipt['visible'])
        self.assertNotIn('acknowledged', receipt)

    def test_window_disabled_or_test_without_saved_run_never_opens_browser(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled), patch.object(alerts, 'load_config', return_value={'alerts': {'macos': enabled, 'macos_mode': 'window'}}), patch.object(alerts.subprocess, 'run') as run:
                receipt = alerts.test()['macos']
            run.assert_not_called()
            if enabled:
                self.assertFalse(receipt['ok'])
                self.assertFalse(receipt['submitted'])
                self.assertIn('저장된 작업 결과', receipt['stderr'])
            else:
                self.assertIsNone(receipt)

    def test_window_failed_open_and_timeout_are_not_submission(self):
        for outcome in (Mock(returncode=1, stderr='no GUI session'), subprocess.TimeoutExpired('open', 5), OSError('missing opener')):
            kwargs = {'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': outcome}
            with self.subTest(outcome=outcome), patch.object(alerts.subprocess, 'run', **kwargs):
                receipt = alerts._macos_window('saved')
            self.assertFalse(receipt['ok'])
            self.assertFalse(receipt['opened'])
            self.assertFalse(receipt['submitted'])
            self.assertIsNone(receipt['visible'])


if __name__ == '__main__':
    unittest.main()
