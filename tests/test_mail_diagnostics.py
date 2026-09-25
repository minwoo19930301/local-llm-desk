import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from desk import mail, tools, runner

class MailTests(unittest.TestCase):
    def test_missing_account(self):
        with patch.object(mail, 'config', return_value={}), patch.object(mail, '_existing_credentials', return_value=(None, None)):
            with self.assertRaisesRegex(RuntimeError, '먼저 등록'): mail.headers()

    def test_existing_env_parser_does_not_execute(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.env'
            path.write_text('NAVER_MAIL_USERNAME=test\nNAVER_MAIL_PASSWORD="$(do-not-run)"\nOTHER=ignore\n')
            self.assertEqual(mail._existing_credentials(path), ('test', '$(do-not-run)'))

    def test_existing_credentials_reused(self):
        pop = MagicMock(); pop.stat.return_value = (0, 0)
        with patch.object(mail, 'config', return_value={}), patch.object(mail, '_existing_credentials', return_value=('test', 'secret')), patch.object(mail, '_keychain') as key, patch.object(mail.poplib, 'POP3_SSL', return_value=pop):
            self.assertTrue(mail.headers()['ok'])
        key.assert_not_called(); pop.user.assert_called_once_with('test'); pop.pass_.assert_called_once_with('secret')

    def test_headers_only(self):
        pop = MagicMock(); pop.stat.return_value = (2, 100)
        pop.top.return_value = (b'+OK', [b'Subject: test', b'From: a@example.com'], 40)
        with patch.object(mail,'config',return_value={'account':'test@naver.com'}), patch.object(mail,'_keychain',return_value='test-secret'), patch.object(mail.poplib,'POP3_SSL',return_value=pop):
            result = mail.headers(1)
        self.assertEqual(result['headers'][0]['Subject'], 'test')
        pop.top.assert_called_once_with(2,0)
        pop.retr.assert_not_called(); pop.dele.assert_not_called(); pop.quit.assert_called_once()

    def test_protocol_error_hides_credentials(self):
        pop=MagicMock(); pop.user.side_effect=mail.poplib.error_proto('test-secret')
        with patch.object(mail,'config',return_value={'account':'test@naver.com'}), patch.object(mail,'_keychain',return_value='test-secret'), patch.object(mail.poplib,'POP3_SSL',return_value=pop):
            with self.assertRaisesRegex(RuntimeError,'POP3 인증') as exc: mail.headers()
        self.assertNotIn('test-secret',str(exc.exception)); pop.quit.assert_called_once()

    def test_password_not_in_config(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(mail,'DATA',Path(tmp)), patch.object(mail,'_keychain') as key:
            mail.save('test@naver.com','test-secret')
            self.assertNotIn('test-secret',(Path(tmp)/'mail.json').read_text())
            key.assert_called_once_with('save','test@naver.com','test-secret')

    def test_invalid_account(self):
        with self.assertRaises(ValueError): mail.save('other@example.com','secret')

class ToolTests(unittest.TestCase):
    def test_cli_real_success_and_failure(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(tools,'WORKSPACE',Path(tmp)):
            self.assertEqual(tools.run('run_cli',{'command':'printf tool-ok'},'workspace',{}),'tool-ok')
            self.assertTrue(tools.run('run_cli',{'command':'exit 7'},'workspace',{}).startswith('도구 실패:'))

    def test_browser_open_does_not_claim_reading(self):
        with patch.object(tools.subprocess,'run',return_value=MagicMock(returncode=0)):
            result=tools.run('chrome_open',{'url':'https://mail.naver.com/'},'workspace',{})
        self.assertIn('확인하지 못했습니다',result)

    def test_selected_tools(self):
        names=lambda opts:{x['function']['name'] for x in tools.specs('workspace',opts,[],{})}
        self.assertEqual(names(['file']),{'read_file'})
        self.assertIn('mail_headers',names(['mail']))
        self.assertNotIn('mail_headers',names(['chrome']))

    def test_failed_tool_trace(self):
        responses=[{'message':{'role':'assistant','content':'','tool_calls':[{'function':{'name':'run_cli','arguments':{'command':'exit 7'}}}]}}, {'message':{'role':'assistant','content':'Done'}}]
        with tempfile.TemporaryDirectory() as tmp, patch.object(tools,'WORKSPACE',Path(tmp)), patch.object(runner.ollama_ctl,'chat_messages',side_effect=responses):
            result=runner.run_task('test','run command',tools=['cli'],max_loops=4)
        self.assertEqual(result.tool_errors,1); self.assertFalse(result.tool_trace[0]['ok'])

    def test_unselected_tool_is_not_executed(self):
        responses=[{"message":{"role":"assistant","content":"","tool_calls":[{"function":{"name":"run_cli","arguments":{"command":"echo forbidden"}}}]}}, {"message":{"role":"assistant","content":"Done"}}]
        with patch.object(runner.ollama_ctl,"chat_messages",side_effect=responses), patch.object(tools,"run") as dispatch:
            result=runner.run_task("test","read file",tools=["file"],max_loops=4)
        dispatch.assert_not_called(); self.assertEqual(result.tool_errors,1)

    def test_textual_tool_call_is_incomplete(self):
        response={'message':{'role':'assistant','content':'{"name":"run_cli","parameters":{"command":"pwd"}}'}}
        with patch.object(runner.ollama_ctl,'chat_messages',return_value=response):
            result=runner.run_task('test','run command',tools=['cli'],max_loops=4)
        self.assertTrue(result.incomplete)

if __name__ == '__main__': unittest.main()
