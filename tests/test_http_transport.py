"""HTTP transport deadlines and diagnostics, using only local fixture data."""
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from desk import tools


class HTTPTransportTests(unittest.TestCase):
    def setUp(self):
        self.stop = threading.Event()
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                try:
                    if self.path == '/headers':
                        self.wfile.write(b'HTTP/1.1 200 OK\r\nX-Drip: '); self.wfile.flush()
                        while not owner.stop.wait(.02):
                            self.wfile.write(b'x'); self.wfile.flush()
                        return
                    if self.path in ('/body', '/stall'):
                        self.send_response(200); self.send_header('Content-Length', '100000'); self.end_headers()
                        while not owner.stop.wait(.02):
                            if self.path == '/body':
                                self.wfile.write(b'x'); self.wfile.flush()
                        return
                    blob = b'LOCAL_FIXTURE'
                    self.send_response(200)
                    self.send_header('Content-Length', str(100 if self.path == '/truncated' else len(blob)))
                    self.end_headers(); self.wfile.write(blob); self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup_server)

    def cleanup_server(self):
        self.stop.set()
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def url(self, path):
        return f'http://127.0.0.1:{self.server.server_port}{path}'

    def test_normal_fixture_response(self):
        self.assertEqual(tools._fetch('GET', self.url('/ok'), None, {}, allow_local=True, timeout=1), 'LOCAL_FIXTURE')

    def test_slow_headers_and_body_have_total_deadline_with_stage(self):
        for path, stage in (('/headers', 'response headers'), ('/body', 'response body'), ('/stall', 'response body')):
            with self.subTest(path=path):
                started = time.monotonic()
                with self.assertRaisesRegex(TimeoutError, stage + ' timed out'):
                    tools._fetch('GET', self.url(path), None, {}, allow_local=True, timeout=.25)
                self.assertLess(time.monotonic() - started, .8)

    def test_truncated_http_body_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError, 'response body failed.*IncompleteRead'):
            tools._fetch('GET', self.url('/truncated'), None, {}, allow_local=True, timeout=1)

    def test_dns_wait_obeys_total_deadline(self):
        release = threading.Event()
        finished = threading.Event()
        original = socket.getaddrinfo
        def resolve(*args, **kwargs):
            try:
                release.wait(2)
                return original('127.0.0.1', 80, type=socket.SOCK_STREAM)
            finally:
                finished.set()
        try:
            with patch.object(socket, 'getaddrinfo', side_effect=resolve):
                started = time.monotonic()
                with self.assertRaisesRegex(TimeoutError, 'DNS timed out'):
                    tools._fetch('GET', 'http://fixture.invalid', None, {}, timeout=.1)
                self.assertLess(time.monotonic() - started, .5)
        finally:
            release.set()
            self.assertTrue(finished.wait(1))

    def test_http_tool_dispatch_propagates_remaining_budget(self):
        connector = {'kind': 'http', 'id': 'h', 'name': 'fixture', 'method': 'GET', 'url_template': 'https://fixture.invalid'}
        context = tools.build_context('workspace', ['http'], [connector], {})
        with patch.object(tools, '_fetch', return_value='fixture') as fetch:
            tools.run('http_request', {'url': 'https://fixture.invalid'}, 'workspace', {}, timeout=.3, context=context)
            self.assertEqual(fetch.call_args.kwargs['timeout'], .3)
            tools.run('http__fixture', {}, 'workspace', {}, timeout=.4, context=context)
            self.assertEqual(fetch.call_args.kwargs['timeout'], .4)

    def test_timeout_error_does_not_expose_query_parameters(self):
        connection = Mock()
        connection.connect.side_effect = TimeoutError('secret-sensitive inner exception')
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.215.14', 443))]
        with patch.object(socket, 'getaddrinfo', return_value=answers), patch.object(tools, '_pinned_connection', return_value=connection):
            with self.assertRaisesRegex(TimeoutError, 'TCP/TLS timed out') as ctx:
                tools._fetch('GET', 'https://fixture.invalid?key=FAKE_SECRET', None, {}, timeout=1)
        self.assertNotIn('FAKE_SECRET', str(ctx.exception))
        self.assertNotIn('secret-sensitive', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
