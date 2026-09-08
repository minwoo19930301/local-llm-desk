"""Local-only HTTP MCP deadline regressions, including partial SSE lines."""
import json
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from desk import mcp_host


class MCPHTTPTest(unittest.TestCase):
    def setUp(self):
        self.stop = threading.Event()
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                message = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                try:
                    if self.path == '/ok':
                        data = json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': {'content': [{'type': 'text', 'text': 'ok'}]}})
                        self.wfile.write(('data: '+data+'\n\n').encode()); self.wfile.flush()
                    else:
                        data = b': heartbeat\n\n' if self.path == '/heartbeat' else b'x'
                        while not owner.stop.is_set():
                            self.wfile.write(data); self.wfile.flush()
                            owner.stop.wait(.02)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup_server)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def cleanup_server(self):
        self.stop.set()
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def client(self, path):
        return mcp_host.MCPClient({'transport': 'http', 'url': f'http://127.0.0.1:{self.server.server_port}{path}'}, {})

    def test_configured_local_mcp_still_works(self):
        with patch.object(mcp_host.urllib.request, 'urlopen', self.opener.open):
            self.assertEqual(self.client('/ok').call('echo', {}, timeout=1), 'ok')

    def test_heartbeats_and_partial_lines_do_not_extend_total_deadline(self):
        with patch.object(mcp_host.urllib.request, 'urlopen', self.opener.open):
            for path in ('/heartbeat', '/partial'):
                with self.subTest(path=path):
                    started = time.monotonic()
                    with self.assertRaises(mcp_host.MCPTimeout):
                        self.client(path).call('slow', {}, timeout=.2)
                    self.assertLess(time.monotonic() - started, .8)


if __name__ == '__main__':
    unittest.main()
