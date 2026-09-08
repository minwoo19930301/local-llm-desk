"""Failed evidence must stop inference; all tools, lifecycle and notifications are mocked."""
from contextlib import ExitStack, nullcontext
import socket
import subprocess
import unittest
from unittest.mock import Mock, patch

from desk import mcp_host, runner, tools


class ToolFailureTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(runner.ollama_ctl, 'session', return_value=nullcontext()))
        self.collect = self.stack.enter_context(patch.object(runner, '_collect_connectors', return_value=[]))
        self.stack.enter_context(patch.object(runner, '_scrub_secrets', side_effect=lambda text, _: text))
        self.notify = self.stack.enter_context(patch.object(runner.alerts, 'notify'))
        self.persist = self.stack.enter_context(patch.object(runner, '_persist'))
        self.stack.enter_context(patch.object(runner.jobs_mod, 'mark_last_run'))
        self.stack.enter_context(patch.object(runner.jobs_mod, 'disable_if_once'))
        self.stack.enter_context(patch.object(runner.jobs_mod, 'should_alert', return_value=True))
        self.ctx = runner.RunContext(job_id='fixture', title='fixture', model='fake-model',
                                     prompt='summarize fixture', tools=['http'], max_loops=3)
        self.chat = self.stack.enter_context(patch.object(runner.ollama_ctl, 'chat_messages'))

    def model_calls(self, name, arguments=None, extra_calls=None):
        call = {'function': {'name': name, 'arguments': arguments or {}}}
        first = {'message': {'role': 'assistant', 'content': 'UNVERIFIED_DRAFT',
                             'tool_calls': [call, *(extra_calls or [])]}}
        self.chat.side_effect = [first,
            {'message': {'role': 'assistant', 'content': 'FABRICATED_SUCCESS_THAT_MUST_NOT_BE_REQUESTED'}},
        ]
        return first

    def execute(self):
        return runner._execute(self.ctx, {'action': 'run', 'model': 'fake-model'}, 0)

    def assert_failed_once(self, record, name, message):
        self.assertEqual(record['status'], 'fail')
        self.assertFalse(record['ok'])
        self.assertEqual(record['output'], '')
        self.assertIn(message, record['error'])
        self.assertEqual(record['tools_used'], [name])
        self.assertEqual(record['loops'], 1)
        self.assertEqual(self.chat.call_count, 1)
        self.notify.assert_not_called()

    def test_http_connector_timeout_stops_remaining_calls_and_final_summary(self):
        con = {'id': 'feed', 'kind': 'http', 'name': 'fixture_feed', 'method': 'GET',
               'url_template': 'https://fixture.invalid/feed', 'enabled': True}
        self.collect.return_value = [con]
        self.ctx.connector_ids = ['feed']
        name = 'http__fixture_feed'
        self.model_calls(name, extra_calls=[{'function': {'name': name, 'arguments': {}}}])
        with patch.object(tools, '_fetch', side_effect=TimeoutError('fixture network timeout')) as fetch:
            record = self.execute()
        self.assert_failed_once(record, name, 'TimeoutError')
        fetch.assert_called_once()
        # Finalization receives and persists the failed record with tool evidence.
        runner._finish({'alert': 'all'}, self.ctx, record)
        self.persist.assert_called_once_with('fixture', record)
        self.assertFalse(self.notify.call_args.kwargs['ok'])
        self.assertEqual(self.notify.call_args.kwargs['status'], 'fail')
        self.assertNotIn('UNVERIFIED_DRAFT', self.notify.call_args.args[1])

    def test_http_error_status_is_a_structured_execution_failure(self):
        self.model_calls('http_request', {'url': 'https://fixture.invalid/'})
        response = Mock(status=503)
        connection = Mock()
        connection.getresponse.return_value = response
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.215.14', 443))]
        with patch.object(socket, 'getaddrinfo', return_value=addresses), patch.object(tools, '_pinned_connection', return_value=connection):
            record = self.execute()
        self.assert_failed_once(record, 'http_request', 'HTTP 503')
        connection.close.assert_called_once()

    def test_mcp_iserror_stops_inference(self):
        con = {'id': 'mcp', 'kind': 'mcp', 'name': 'fixture', 'transport': 'stdio', 'enabled': True}
        self.collect.return_value = [con]
        self.ctx.connector_ids = ['mcp']
        client = mcp_host.MCPClient(con, {})
        client.list_tools = Mock(return_value=[{'name': 'lookup', 'annotations': {'readOnlyHint': True}}])
        client.request = Mock(return_value={'isError': True, 'content': [{'type': 'text', 'text': 'fixture MCP unavailable'}]})
        self.model_calls('mcp__fixture__lookup')
        with patch.object(mcp_host, 'open_for_job', return_value={'mcp': client}), patch.object(mcp_host, 'close_clients') as close:
            record = self.execute()
        self.assert_failed_once(record, 'mcp__fixture__lookup', 'MCPError')
        close.assert_called_once()

    def test_unselected_tool_records_denial_without_executing(self):
        self.model_calls('run_cli', {'command': 'external-fixture'})
        with patch.object(tools, '_run_cli') as execute:
            record = self.execute()
        self.assert_failed_once(record, 'run_cli', '선택되지 않은')
        execute.assert_not_called()

    def test_selected_but_denied_post_records_failure(self):
        self.ctx.permission = 'read'
        self.model_calls('http_request', {'url': 'https://fixture.invalid/', 'method': 'POST'})
        with patch.object(tools, '_fetch') as fetch:
            record = self.execute()
        self.assert_failed_once(record, 'http_request', 'GET만')
        fetch.assert_not_called()

    def test_nonzero_cli_exit_records_failure_metadata(self):
        context = tools.build_context('workspace', ['cli'], [], {})
        with patch.object(tools, 'sandbox_run', return_value=subprocess.CompletedProcess(['fixture'], 7, 'fixture output', '')):
            output = tools.run('run_cli', {'command': 'external-fixture'}, 'workspace', {}, context=context)
        self.assertIsInstance(output, str)
        self.assertEqual(len(context.failures), 1)
        self.assertEqual(context.failures[0].name, 'run_cli')
        self.assertEqual(context.failures[0].error_type, 'RuntimeError')
        self.assertIn('CLI exit 7', context.failures[0].message)

    def test_error_looking_successful_text_is_not_classified_as_failure(self):
        first = self.model_calls('http_request', {'url': 'https://fixture.invalid/'})
        self.chat.side_effect = [first,
                                {'message': {'role': 'assistant', 'content': 'The fixture includes a quoted error message.'}}]
        with patch.object(tools, '_fetch', return_value='도구 실패: timeout is a literal example in this document'):
            record = self.execute()
        self.assertEqual(record['status'], 'ok')
        self.assertIsNone(record['error'])
        self.assertEqual(self.chat.call_count, 2)


if __name__ == '__main__':
    unittest.main()
