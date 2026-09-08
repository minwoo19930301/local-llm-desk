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


if __name__ == '__main__':
    unittest.main()
