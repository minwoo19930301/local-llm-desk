"""Execution boundaries: no real credentials, remote services, or external agents."""
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from desk import tools


class ToolSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.patcher = patch.object(tools, 'WORKSPACE', self.workspace)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_unselected_generic_and_missing_context_fail_closed(self):
        context = tools.build_context('workspace', ['http'], [], {})
        with patch.object(tools, '_run_cli') as execute:
            for ctx in (context, None):
                result = tools.run('run_cli', {'command': 'echo test'}, 'workspace', {}, context=ctx)
                self.assertIn('선택되지 않은', result)
            execute.assert_not_called()

    def test_overlapping_contexts_keep_their_own_connector(self):
        a = {'id': 'a', 'kind': 'http', 'name': 'report', 'method': 'GET', 'url_template': 'https://a.invalid'}
        b = {'id': 'b', 'kind': 'http', 'name': 'report', 'method': 'POST', 'url_template': 'https://b.invalid'}
        ca = tools.build_context('workspace', [], [a], {})
        cb = tools.build_context('workspace', [], [b], {})
        a['url_template'] = 'https://mutated.invalid'
        with patch.object(tools, '_fetch', return_value='ok') as fetch:
            tools.run('http__report', {}, 'workspace', {}, context=ca)
            self.assertEqual(fetch.call_args.args[:2], ('GET', 'https://a.invalid'))
            tools.run('http__report', {}, 'workspace', {}, context=cb)
            self.assertEqual(fetch.call_args.args[:2], ('POST', 'https://b.invalid'))

    def test_read_mcp_requires_explicit_readonly_and_rechecks_dispatch(self):
        client = Mock()
        write_tool = {'name': 'send', 'annotations': {'readOnlyHint': False, 'destructiveHint': False}}
        read_tool = {'name': 'list', 'annotations': {'readOnlyHint': True}}
        client.list_tools.return_value = [write_tool, read_tool, {'name': 'unknown'}]
        con = {'id': 'm', 'kind': 'mcp', 'name': 'service'}
        ctx = tools.build_context('read', [], [con], {'m': client})
        self.assertEqual(ctx.allowed_names, {'read_file', 'mcp__service__list'})
        client.list_tools.return_value = [{'name': 'list', 'annotations': {'readOnlyHint': False}}]
        result = tools.run('mcp__service__list', {}, 'read', {}, context=ctx)
        self.assertIn('도구 실패', result)
        client.call.assert_not_called()

    def test_portable_commands_and_symlink_boundary(self):
        (self.workspace / 'note.txt').write_text('hello\nworld\n')
        outside = self.root / 'outside.txt'
        outside.write_text('OUTSIDE_FIXTURE')
        (self.workspace / 'link').symlink_to(outside)
        self.assertEqual(tools._run_cli('head note.txt', 'workspace'), 'hello\nworld')
        with self.assertRaises(RuntimeError):
            tools._run_cli('cat link', 'workspace')
        with patch.object(tools.sys, 'platform', 'unsupported'):
            for command in ["sh -c 'cat /etc/hosts'", "python -c 'print(1)'"]:
                with self.assertRaisesRegex(RuntimeError, 'sandbox-exec'):
                    tools._run_cli(command, 'workspace')

    @unittest.skipUnless(sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').exists(), 'macOS kernel sandbox')
    def test_sandbox_nested_code_cannot_read_or_write_outside_workspace(self):
        inside = self.workspace / 'inside.txt'
        outside = self.root / 'outside.txt'
        outside.write_text('OUTSIDE_FIXTURE')
        code = ('from pathlib import Path\n'
                f'p=Path({str(inside)!r})\np.write_text("inside")\nprint(p.read_text())\n'
                f'outside=Path({str(outside)!r})\n'
                'for action in (outside.read_text, lambda: outside.write_text("changed")):\n'
                ' try: action(); print("ESCAPED")\n'
                ' except PermissionError: print("DENIED")\n')
        result = tools.sandbox_run([sys.executable, '-I', '-S', '-c', code], 'workspace', 5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['inside', 'DENIED', 'DENIED'])
        self.assertEqual(outside.read_text(), 'OUTSIDE_FIXTURE')

    @unittest.skipUnless(sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').exists(), 'macOS kernel sandbox')
    def test_machine_sandbox_blocks_fixture_credentials_and_symlink(self):
        fakehome = self.root / 'fakehome'
        creds = fakehome / '.ssh'
        creds.mkdir(parents=True)
        secret = creds / 'fixture'
        secret.write_text('FAKE_SECRET')
        alias = fakehome / 'alias'
        alias.symlink_to(secret)
        code = ('from pathlib import Path\n'
                f'for p in {list(map(str, [secret, alias]))!r}:\n'
                ' try: print(Path(p).read_text())\n'
                ' except PermissionError: print("DENIED")\n')
        with patch.object(Path, 'home', return_value=fakehome):
            result = tools.sandbox_run([sys.executable, '-I', '-S', '-c', code], 'machine', 5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['DENIED', 'DENIED'])

    @unittest.skipUnless(sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').exists(), 'macOS kernel sandbox')
    def test_sandbox_blocks_local_control_socket(self):
        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(('127.0.0.1', 0)); server.listen()
        code = ('import socket\ns=socket.socket()\ns.settimeout(.2)\n'
                f'try:\n s.connect({server.getsockname()!r}); print("ESCAPED")\n'
                'except PermissionError: print("DENIED")\n')
        result = tools.sandbox_run([sys.executable, '-I', '-S', '-c', code], 'workspace', 5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'DENIED')
        profile = tools._sandbox_profile('workspace', self.workspace, self.root, True)
        self.assertIn('(require-not (remote tcp "localhost:11434"))', profile)

    @unittest.skipUnless(sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').exists(), 'macOS kernel sandbox')
    def test_sandbox_cannot_signal_an_unrelated_process(self):
        outside = subprocess.Popen([sys.executable, '-I', '-S', '-c', 'import time; time.sleep(30)'])
        try:
            code = ('import os, signal\n'
                    f'try:\n os.kill({outside.pid}, signal.SIGTERM); print("ESCAPED")\n'
                    'except PermissionError: print("DENIED")\n')
            result = tools.sandbox_run([sys.executable, '-I', '-S', '-c', code], 'workspace', 5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'DENIED')
            self.assertIsNone(outside.poll())
        finally:
            outside.terminate(); outside.wait(timeout=5)

    def test_runtime_roots_cover_only_selected_python_installation(self):
        prefix = self.root / 'hostedtoolcache/Python/3.12/arm64'
        interpreter = prefix / 'bin/python3'
        with patch.object(sys, 'executable', str(interpreter)), patch.object(sys, 'base_prefix', str(prefix)):
            roots = tools._runtime_paths_for(interpreter)
            self.assertIn(prefix, roots)
            unrelated = self.root / 'other/bin/custom'
            self.assertEqual(tools._runtime_paths_for(unrelated), [unrelated])
        version = self.root / 'Library/Frameworks/Python.framework/Versions/3.12'
        framework_python = version / 'Resources/Python.app/Contents/MacOS/Python'
        roots = tools._runtime_paths_for(framework_python)
        self.assertIn(version, roots)
        self.assertNotIn(version.parent, roots)
        self.assertNotIn(self.root / 'Library/Frameworks', roots)

    @unittest.skipUnless(sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').exists(), 'macOS kernel sandbox')
    def test_framework_runtime_is_readonly_and_does_not_open_siblings_or_credentials(self):
        version = self.root / 'Library/Frameworks/Python.framework/Versions/3.12'
        executable = version / 'bin/python3'
        runtime = version / 'lib/python3.12/fixture'
        credential = version / '.ssh/fixture'
        sibling = version.parent / '3.11/fixture'
        for path in (executable, runtime, credential, sibling):
            path.parent.mkdir(parents=True, exist_ok=True)
        runtime.write_text('RUNTIME_FIXTURE\n')
        credential.write_text('CREDENTIAL_FIXTURE\n')
        sibling.write_text('SIBLING_FIXTURE\n')
        script = '#!/bin/sh\n/bin/cat ' + shlex.quote(str(runtime)) + '\n'
        for denied in (credential, sibling):
            script += ('if /bin/cat ' + shlex.quote(str(denied)) + ' 2>/dev/null; '
                       'then /bin/echo ESCAPED; else /bin/echo DENIED; fi\n')
        script += ('if ( /bin/echo changed > ' + shlex.quote(str(runtime)) + ' ) 2>/dev/null; '
                   'then /bin/echo WROTE; else /bin/echo READ_ONLY; fi\n')
        executable.write_text(script)
        executable.chmod(0o700)
        with patch.object(Path, 'home', return_value=version):
            result = tools.sandbox_run([str(executable)], 'workspace', 5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['RUNTIME_FIXTURE', 'DENIED', 'DENIED', 'READ_ONLY'])
        self.assertEqual(runtime.read_text(), 'RUNTIME_FIXTURE\n')

    def test_agent_environment_cannot_replace_sandbox_environment(self):
        for env in ({'HOME': '/tmp'}, {'PATH': '/tmp'}, {'API_KEY': 'not-a-real-key'}):
            with self.assertRaisesRegex(RuntimeError, '환경 변수'):
                tools.sandbox_run(['echo'], 'workspace', 1, allow_ollama=True, extra_env=env)
        with self.assertRaisesRegex(RuntimeError, '로컬 기본 주소'):
            tools.sandbox_run(['echo'], 'workspace', 1, allow_ollama=True,
                              extra_env={'OLLAMA_API_BASE': 'https://external.invalid'})

    def test_numeric_alias_and_dns_private_addresses_are_blocked(self):
        with self.assertRaisesRegex(RuntimeError, 'localhost'):
            tools._destinations('http://2130706433:9876/')
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 80))]
        with patch.object(socket, 'getaddrinfo', return_value=answers):
            with self.assertRaisesRegex(RuntimeError, 'localhost'):
                tools._destinations('https://public-name.invalid/')
            self.assertEqual(tools._destinations('https://configured.invalid/', allow_local=True)[1], answers)

    def test_public_connection_uses_resolved_socket_without_second_dns_lookup(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.215.14', 80))]
        with patch.object(socket, 'getaddrinfo', return_value=addresses) as resolve:
            parsed, pinned = tools._destinations('http://public.invalid/')
            with patch.object(socket, 'socket') as create:
                connection = tools._pinned_connection(parsed, pinned)
                connection.connect()
                create.return_value.connect.assert_called_once_with(('93.184.215.14', 80))
                connection.close()
            self.assertEqual(resolve.call_count, 1)

    def test_redirect_to_loopback_is_blocked_before_second_connection(self):
        response = Mock(status=302)
        response.getheader.return_value = 'http://2130706433/'
        connection = Mock()
        connection.getresponse.return_value = response
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.215.14', 80))]
        original = socket.getaddrinfo
        def resolve(host, *args, **kwargs):
            return answers if host == 'public.invalid' else original(host, *args, **kwargs)
        with patch.object(socket, 'getaddrinfo', side_effect=resolve), patch.object(tools, '_pinned_connection', return_value=connection) as connect:
            with self.assertRaisesRegex(RuntimeError, 'localhost'):
                tools._fetch('GET', 'http://public.invalid', None, {})
            self.assertEqual(connect.call_count, 1)
            connection.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
