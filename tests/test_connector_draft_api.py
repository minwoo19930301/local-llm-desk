"""The HTTP draft/import path is separate from registration and execution."""
import json
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from desk import connectors, http_api, ollama_ctl, runner
from tests.http_fixture import isolated_server


class ConnectorDraftAPI(unittest.TestCase):
    def setUp(self):
        self.fixture = isolated_server()
        self.base = self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)

    def request(self, method, path, body=None, csrf=True):
        headers = {'Content-Type': 'application/json'}
        if csrf:
            headers['X-Requested-With'] = 'free-ai-scheduler'
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     method=method, headers=headers)
        try:
            response = urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.code, json.load(response)

    def test_draft_then_explicit_registration_preserves_parameters(self):
        generated = {'kind': 'cli', 'name': 'echo_fixture', 'command_template': 'echo {text}', 'readonly': True,
                     'params': {'text': {'type': 'string', 'required': True, 'default': 'hello'}}}
        response = {'message': {'content': json.dumps({'draft': generated, 'summary': '초안', 'warnings': [], 'questions': []})}}
        with patch.object(ollama_ctl, '_chat_http', return_value=response) as inference:
            code, result = self.request('POST', '/api/connectors/draft', {'prompt': 'echo 연동', 'model': 'fixture-model:latest'})
        self.assertEqual(code, 200)
        self.assertEqual(connectors.load(), [])
        self.assertEqual(runner.list_runs(), [])
        inference.assert_called_once()
        code, saved = self.request('POST', '/api/connectors', result['draft'])
        self.assertEqual(code, 200)
        registered = connectors.get(saved['connector']['id'])
        self.assertEqual(registered['command_template'], 'echo {text}')
        self.assertEqual(registered['params']['text']['default'], 'hello')
        self.assertEqual(runner.list_runs(), [])

    def test_csrf_validation_and_busy_draft_do_not_invoke_model(self):
        with patch.object(ollama_ctl, '_chat_http') as inference:
            self.assertEqual(self.request('POST', '/api/connectors/draft', {'prompt': 'echo'}, csrf=False)[0], 403)
            self.assertEqual(self.request('POST', '/api/connectors/draft', {'prompt': ''})[0], 400)
            http_api._connector_draft_lock.acquire()
            try:
                self.assertEqual(self.request('POST', '/api/connectors/draft', {'prompt': 'echo'})[0], 409)
            finally:
                http_api._connector_draft_lock.release()
        inference.assert_not_called()
        self.assertEqual(connectors.load(), [])

    def test_openapi_import_only_reads_pasted_document(self):
        document = {'openapi': '3.0.3', 'info': {'title': 'Fixture', 'version': '1'},
                    'servers': [{'url': 'https://api.example.invalid'}],
                    'paths': {'/posts': {'get': {'operationId': 'latestPosts', 'responses': {'200': {'description': 'OK'}}}}}}
        with patch.object(ollama_ctl, '_chat_http') as inference:
            code, result = self.request('POST', '/api/connectors/openapi', {'spec_text': json.dumps(document)})
        self.assertEqual(code, 200)
        self.assertEqual(result['selected']['url_template'], 'https://api.example.invalid/posts')
        self.assertEqual(connectors.load(), [])
        self.assertEqual(runner.list_runs(), [])
        inference.assert_not_called()

    def test_options_never_start_ollama(self):
        with patch.object(ollama_ctl, 'start', side_effect=AssertionError('must stay idle')):
            code, result = self.request('GET', '/api/connectors/options')
        self.assertEqual(code, 200)
        self.assertIn('models', result)
        self.assertIn('provider_ready', result)


if __name__ == '__main__':
    unittest.main()
