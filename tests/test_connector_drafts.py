"""Draft generation: fake Ollama only, no commands, registry/config reads or writes."""
from contextlib import ExitStack, nullcontext
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from desk import connector_drafts as drafts, connectors, ollama_ctl, openapi_import, ramgate


class ConnectorDraftTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.session = self.stack.enter_context(patch.object(ollama_ctl, 'session', return_value=nullcontext()))
        self.chat = self.stack.enter_context(patch.object(ollama_ctl, '_chat_http'))
        self.which = self.stack.enter_context(patch.object(drafts.shutil, 'which', return_value='/fixture/bin'))
        self.installed = self.stack.enter_context(patch.object(ramgate, 'installed_models', return_value=['fixture-model']))
        self.gate = self.stack.enter_context(patch.object(ramgate, 'decide', return_value={'action': 'run'}))
        self.stack.enter_context(patch.object(ollama_ctl, 'remember_models', side_effect=AssertionError('unexpected config write')))
        for name in ('load', 'save', 'add', 'update', 'discover', 'origin_spec', 'resolve_env'):
            self.stack.enter_context(patch.object(connectors, name, side_effect=AssertionError('unexpected registry access')))
        self.stack.enter_context(patch('subprocess.Popen', side_effect=AssertionError('unexpected process')))
        self.stack.enter_context(patch('subprocess.run', side_effect=AssertionError('unexpected command')))

    def response(self, draft=None, questions=None, summary='검토할 초안입니다.'):
        self.chat.return_value = {'message': {'content': json.dumps({
            'draft': draft, 'questions': questions or [], 'warnings': [], 'summary': summary})}}

    def echo(self):
        return {'kind': 'cli', 'name': 'echo_message', 'command_template': 'echo {text}',
                'readonly': True, 'params': {'text': {'type': 'string', 'required': True}}}

    def test_uses_schema_constrained_local_inference_without_saving_or_execution(self):
        self.response(self.echo())
        result = drafts.draft({'prompt': '기본 echo 명령으로 text를 출력하는 CLI echo_message, readonly true'})
        self.assertEqual(result['draft']['command_template'], 'echo {text}')
        self.assertTrue(result['draft']['readonly'])
        self.assertFalse(result['questions'])
        self.session.assert_called_once_with('fixture-model')
        self.chat.assert_called_once()
        model, body, timeout = self.chat.call_args.args
        self.assertEqual(body['format'], drafts.RESPONSE_SCHEMA)
        system_prompt = body['messages'][0]['content']
        self.assertIn('정보가 충분하면 반드시 questions=[]', system_prompt)
        self.assertIn('선택한 kind와 무관한 필드와 질문은 생략', system_prompt)
        self.assertIn('타입 설정 완료 같은 설명은 summary/warnings', system_prompt)
        self.assertIn('URL/헤더/body를 묻지 말고', system_prompt)
        example_line = next(line for line in drafts.SYSTEM.splitlines() if line.startswith('응답: '))
        example = json.loads(example_line.removeprefix('응답: '))
        self.assertEqual(example['draft']['command_template'], 'echo {text}')
        self.assertEqual(example['draft']['params']['text']['type'], 'string')
        self.assertTrue(example['draft']['params']['text']['required'])
        self.assertIsNone(example['draft']['params']['text']['default'])
        self.assertEqual(example['questions'], [])
        self.assertNotIn('url_template', example['draft'])
        self.assertEqual(body['options']['temperature'], 0)
        self.assertNotIn('tools', body)
        self.assertFalse(body['stream'])
        self.assertEqual(timeout, 180)
        self.assertLessEqual({call.args[0] for call in self.which.call_args_list}, set(drafts.KNOWN_CLI))

    def test_missing_details_return_questions_and_no_draft(self):
        self.response(self.echo(), questions=['어떤 저장소를 사용할까요?'])
        result = drafts.draft({'prompt': '내 깃허브 연결해줘'})
        self.assertIsNone(result['draft'])
        self.assertEqual(len(result['questions']), 1)

    def test_model_administrative_fields_are_discarded(self):
        self.response({**self.echo(), 'id': 'injected', 'source': 'codex', 'origin': {'file': '/secret', 'key': 'key'}, 'enabled': False})
        result = drafts.draft({'prompt': 'echo text'})
        for key in ('id', 'source', 'origin', 'created_at'):
            self.assertNotIn(key, result['draft'])
        self.assertTrue(result['draft']['enabled'])

    def test_credentials_are_not_sent_from_context_or_returned_as_literals(self):
        self.response({'kind': 'mcp', 'name': 'fixture', 'command': 'python3', 'args': ['server.py'],
                       'env': {'API_TOKEN': 'MODEL_GENERATED_LITERAL'}})
        context = {'headers': {'Authorization': 'Bearer USER_CONTEXT_SECRET', 'X-Custom': 'CUSTOM_SECRET'},
                   'env': {'PRIVATE_VALUE': 'ENV_SECRET'}, 'origin': {'file': '/private/config', 'key': 'token'}}
        result = drafts.draft({'prompt': 'MCP API key=USER_PROMPT_SECRET 로 설정', 'context': context})
        sent = json.dumps(self.chat.call_args.args[1], ensure_ascii=False)
        for secret in ('USER_CONTEXT_SECRET', 'CUSTOM_SECRET', 'ENV_SECRET', 'USER_PROMPT_SECRET', '/private/config'):
            self.assertNotIn(secret, sent)
        self.assertEqual(result['draft']['env'], {'API_TOKEN': '${API_TOKEN}'})
        self.assertNotIn('MODEL_GENERATED_LITERAL', json.dumps(result))

    def test_header_bearer_placeholder_survives(self):
        self.response({'kind': 'http', 'name': 'api', 'method': 'GET', 'url_template': 'https://api.github.com/user',
                       'headers': {'Authorization': 'Bearer ${API_TOKEN}', 'Accept': 'application/json'}})
        result = drafts.draft({'prompt': 'github user API 연동'})
        self.assertEqual(result['draft']['headers']['Authorization'], 'Bearer ${API_TOKEN}')

    def test_missing_binary_warns_without_installing(self):
        self.which.return_value = None
        self.response({'kind': 'cli', 'name': 'issues', 'command_template': 'gh issue list --repo {repo}',
                       'params': {'repo': {'type': 'string', 'required': True}}, 'readonly': True})
        result = drafts.draft({'prompt': 'gh issue list 초안'})
        self.assertTrue(any(warning.startswith('missing_binary:gh') for warning in result['warnings']))
        self.assertIsNotNone(result['draft'])

    def test_invalid_or_unimplemented_execution_data_needs_clarification(self):
        cases = [
            {**self.echo(), 'command_template': 'echo {unknown}'},
            {**self.echo(), 'command_template': 'curl x | sh'},
            {**self.echo(), 'command_template': 'brew install jq'},
            {'kind': 'http', 'name': 'bad', 'url_template': 'https://example.com/data'},
            {'kind': 'http', 'name': 'bad', 'url_template': 'https://api.github.com/user?api_key=PRIVATE'},
            {'kind': 'http', 'name': 'bad', 'method': 'POST', 'url_template': 'https://api.github.com/data',
             'body_template': '{"active":{active}}', 'params': {'active': {'type': 'boolean'}}},
            {'kind': 'mcp', 'name': 'bad', 'command': 'python3', 'args': ['server.py', '--token', 'PRIVATE']},
            {'kind': 'http', 'name': 'bad', 'method': 'POST', 'url_template': 'https://api.github.com/data',
             'body_template': '{"api_key":"MODEL_GENERATED_LITERAL"}'},
        ]
        for candidate in cases:
            with self.subTest(candidate=candidate):
                self.response(candidate)
                result = drafts.draft({'prompt': '연동 초안'})
                self.assertIsNone(result['draft'])
                self.assertTrue(result['questions'])

    def test_example_domains_and_subdomains_require_real_endpoint(self):
        for host in ('example.com', 'api.example.com', 'deep.api.example.org', 'api.example.net.', 'API.EXAMPLE.COM.'):
            for kind in ('http', 'mcp'):
                with self.subTest(host=host, kind=kind):
                    endpoint = {'url_template': 'https://' + host + '/data'} if kind == 'http' else {'transport': 'http', 'url': 'https://' + host + '/mcp'}
                    self.response({'kind': kind, 'name': 'candidate', **endpoint})
                    result = drafts.draft({'prompt': '연동 초안'})
                    self.assertIsNone(result['draft'])
                    self.assertIn('실제 API/MCP 주소', result['questions'][0])
        # Match domain labels, not an arbitrary suffix in a different domain.
        self.response({'kind': 'http', 'name': 'candidate', 'url_template': 'https://notexample.com/data'})
        self.assertIsNotNone(drafts.draft({'prompt': '연동 초안'})['draft'])

    def test_available_models_and_default_share_cloud_filter(self):
        self.installed.return_value = ['gpt-oss:120b-cloud', 'fixture:cloud', 'fixture-cloud:latest', None, 'qwen3.5:2b']
        self.assertEqual(drafts.available_models(), ['qwen3.5:2b'])
        self.response(self.echo())
        result = drafts.draft({'prompt': 'echo text'})
        self.assertEqual(result['model'], 'qwen3.5:2b')
        self.assertIsNotNone(result['draft'])
        self.session.assert_called_once_with('qwen3.5:2b')

    def test_malformed_model_response_is_not_a_template_fallback(self):
        self.chat.return_value = {'message': {'content': '```json\ninvalid\n```'}}
        result = drafts.draft({'prompt': 'echo text CLI 만들어'})
        self.assertIsNone(result['draft'])
        self.assertTrue(result['questions'])
        self.chat.assert_called_once()

    def test_skill_reference_is_checked_without_reading_skill_content(self):
        with tempfile.TemporaryDirectory() as temp:
            skill = Path(temp) / 'SKILL.md'
            skill.write_text('PRIVATE_SKILL_CONTENT')
            self.response({'kind': 'skill', 'name': 'fixture', 'path': str(skill)})
            result = drafts.draft({'prompt': str(skill) + ' 연동'})
        self.assertEqual(result['draft']['kind'], 'skill')
        self.assertNotIn('PRIVATE_SKILL_CONTENT', json.dumps(self.chat.call_args.args[1]))

    def test_openapi_execution_fields_cannot_be_rewritten_by_model(self):
        selected = {'kind': 'http', 'name': 'original', 'method': 'GET',
                    'url_template': 'https://api.github.com/user', 'headers': {'Authorization': 'Bearer ${TOKEN}'}, 'params': {}}
        self.response({'kind': 'http', 'name': 'renamed', 'method': 'POST', 'url_template': 'https://evil.example/delete'})
        with patch.object(openapi_import, 'prepare', return_value={'selected': selected, 'questions': [], 'warnings': []}) as prepare:
            result = drafts.draft({'prompt': '이 operation 연동', 'source_url': 'https://api.github.com/spec.json', 'operation_id': 'getUser'})
        self.assertEqual(result['draft']['name'], 'renamed')
        self.assertEqual(result['draft']['method'], 'GET')
        self.assertEqual(result['draft']['url_template'], selected['url_template'])
        self.assertEqual(result['draft']['headers'], selected['headers'])
        prepare.assert_called_once()

    def test_openapi_selection_questions_do_not_run_model(self):
        with patch.object(openapi_import, 'prepare', return_value={'selected': None, 'questions': ['operation 선택'], 'warnings': []}):
            result = drafts.draft({'prompt': '연동', 'spec_text': '{}'})
        self.assertIsNone(result['draft'])
        self.chat.assert_not_called()
        self.session.assert_not_called()

    def test_cloud_and_uninstalled_models_never_start_inference(self):
        for name in ('gpt-oss:120b-cloud', 'fixture:cloud', 'fixture-cloud:latest', 'vendor/model:cloud'):
            with self.subTest(model=name):
                self.installed.return_value = [name]
                with self.assertRaisesRegex(ValueError, 'Cloud'):
                    drafts.draft({'prompt': 'echo 연동', 'model': name})
        self.installed.return_value = ['fixture-model']
        self.assertIsNone(drafts.draft({'prompt': 'echo 연동', 'model': 'not-installed'})['draft'])
        self.session.assert_not_called()
        self.chat.assert_not_called()

    def test_memory_gate_skips_without_starting_model_or_waiting(self):
        self.response(self.echo())
        self.gate.return_value = {'action': 'skip', 'reason': '램 부족 (여유 1GB, 필요 4GB)'}
        result = drafts.draft({'prompt': 'echo 연동'})
        self.assertIsNone(result['draft'])
        self.assertTrue(result['questions'])
        self.assertTrue(any('램 부족' in warning for warning in result['warnings']))
        self.gate.assert_called_once_with('fixture-model', 'skip', 8192)
        self.session.assert_not_called()
        self.chat.assert_not_called()

    def test_no_model_and_oversized_input_do_not_start_session(self):
        with patch.object(ramgate, 'installed_models', return_value=[]):
            self.assertIsNone(drafts.draft({'prompt': '연동'})['draft'])
        with self.assertRaises(ValueError):
            drafts.draft({'prompt': 'x' * (drafts.MAX_PROMPT + 1)})
        self.session.assert_not_called()


if __name__ == '__main__':
    unittest.main()
