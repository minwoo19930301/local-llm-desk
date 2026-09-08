"""Draft/manual connector roundtrips: no commands, network, or real state writes."""
import io
import shlex
import unittest
import urllib.error
from unittest.mock import Mock, patch
from urllib.parse import quote

from desk import connectors


class ParameterTemplates(unittest.TestCase):
    def test_hyphenated_cli_param_remains_one_quoted_argument(self):
        connector = connectors._validate({
            'kind': 'cli', 'name': 'fixture', 'command_template': 'echo {project-id}',
            'readonly': True, 'params': {'project-id': {'type': 'string', 'default': 'a b; $(never-execute)'}},
        })
        rendered = connectors.fill_template(connector['command_template'], connectors.default_args(connector), shlex.quote)
        self.assertEqual(shlex.split(rendered), ['echo', 'a b; $(never-execute)'])
        self.assertTrue(connector['readonly'])

    def test_hyphenated_http_param_uses_url_encoding(self):
        connector = connectors._validate({
            'kind': 'http', 'name': 'fixture', 'url_template': 'https://example.invalid/items/{item-id}',
            'params': {'item-id': {'type': 'string', 'default': 'a/b &c'}},
        })
        rendered = connectors.fill_template(connector['url_template'], connectors.default_args(connector), lambda value: quote(value, safe=''))
        self.assertEqual(rendered, 'https://example.invalid/items/a%2Fb%20%26c')

    def test_model_params_do_not_overwrite_environment_references(self):
        rendered = connectors.fill_template('${TOKEN} ${TOKEN:-fallback} $TOKEN {TOKEN} {item-id}',
                                             {'TOKEN': 'model-value', 'item-id': 'item'}, str)
        self.assertEqual(rendered, '${TOKEN} ${TOKEN:-fallback} $TOKEN model-value item')


class PublicHeaderReferences(unittest.TestCase):
    def public_header(self, value):
        return connectors.public({'kind': 'http', 'headers': {'Authorization': value}})['headers']['Authorization']

    def test_bearer_placeholder_and_bare_references_survive_public_view(self):
        for value in ('Bearer ${API_KEY}', 'Bearer $API_KEY', 'bearer ${API_KEY}', '${API_KEY}', '$API_KEY'):
            with self.subTest(value=value):
                self.assertEqual(self.public_header(value), value)

    def test_literal_secret_fragments_and_defaults_are_never_exposed(self):
        for value in ('Bearer fake-secret', 'Bearer fake-secret${API_KEY}', 'Bearer ${API_KEY}fake-secret',
                      '${API_KEY:-fake-secret}', 'Bearer ${API_KEY:-fake-secret}', 'Basic ${API_KEY}',
                      'custom-secret-prefix ${API_KEY}', 'Bearer ${KEY} ${OTHER_KEY}'):
            with self.subTest(value=value):
                self.assertEqual(self.public_header(value), '<redacted>')

    def test_public_view_does_not_resolve_real_environment_value(self):
        with patch.dict(connectors.os.environ, {'API_KEY': 'FAKE_TEST_SECRET'}):
            self.assertEqual(self.public_header('Bearer ${API_KEY}'), 'Bearer ${API_KEY}')


class HttpProbeAuthentication(unittest.TestCase):
    connector = {'kind': 'http', 'url_template': 'https://example.invalid/private?token=fake-secret'}

    def failure(self, code):
        return urllib.error.HTTPError(self.connector['url_template'], code, 'fixture', {}, io.BytesIO(b'private response'))

    def test_401_and_403_are_failures_not_successful_connection(self):
        for code in (401, 403):
            with self.subTest(code=code), patch.object(connectors.urllib.request, 'urlopen', side_effect=self.failure(code)) as request:
                result = connectors._test_http(self.connector)
            self.assertFalse(result['ok'])
            self.assertEqual(result['status'], code)
            self.assertIn('인증 정보 또는 접근 권한', result['error'])
            self.assertNotIn('fake-secret', result['url'])
            self.assertEqual(request.call_count, 1)

    def test_head_unsupported_then_get_unauthorized_reports_failure(self):
        with patch.object(connectors.urllib.request, 'urlopen', side_effect=[self.failure(405), self.failure(401)]) as request:
            result = connectors._test_http(self.connector)
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 401)
        self.assertEqual([call.args[0].get_method() for call in request.call_args_list], ['HEAD', 'GET'])

    def test_head_success_stays_success_without_get_or_command_execution(self):
        response = Mock(status=200)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(connectors.urllib.request, 'urlopen', return_value=response) as request:
            result = connectors._test_http(self.connector)
        self.assertTrue(result['ok'])
        self.assertEqual(result['status'], 200)
        self.assertEqual(request.call_count, 1)


if __name__ == '__main__':
    unittest.main()
