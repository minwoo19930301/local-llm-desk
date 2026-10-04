"""Ollama lifecycle regressions; all processes/network/state use isolated fakes."""
from __future__ import annotations

import os
import socket
import time
import urllib.request
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from desk import catalog, ollama_ctl as ctl, state


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.root = root
        for module, name, value in [
            (ctl, 'PID_DIR', root), (ctl, 'PID_FILE', root / 'ollama.json'),
            (ctl, 'SESSIONS_FILE', root / 'sessions.json'), (ctl, 'LOGS_DIR', root),
            (ctl, 'SERVE_LOG', root / 'serve.log'), (ctl, '_proc', None),
            (state, 'LOCK_FILE', root / '.lock'), (state, 'CONFIG_FILE', root / 'config.json'),
            (state, 'ensure_dirs', lambda: None),
        ]:
            self.stack.enter_context(patch.object(module, name, value))
        self.alive = False
        self.http_up = False
        self.spawn_count = 0
        self.stack.enter_context(patch.object(ctl, 'binary', return_value='/tmp/Ollama With Spaces/ollama'))
        self.stack.enter_context(patch.object(ctl, '_ps_lstart', side_effect=lambda pid: 'Tue Sep 8 10:00:00 2026' if pid == os.getpid() or self.alive else ''))
        self.stack.enter_context(patch.object(ctl, '_verify_record', side_effect=lambda rec: self.alive and rec['pid'] == 987654))
        self.stack.enter_context(patch.object(ctl, 'running', side_effect=lambda: self.http_up))
        self.stack.enter_context(patch.object(ctl.subprocess, 'Popen', side_effect=self.spawn))
        self.signals = self.stack.enter_context(patch.object(ctl.os, 'kill', side_effect=self.kill))
        self.unloads = self.stack.enter_context(patch.object(ctl, 'unload', return_value=True))
        self.stack.enter_context(patch.object(ctl, 'wait_until_up', side_effect=self.ready))

    def spawn(self, *args, **kwargs):
        self.spawn_count += 1
        self.alive = True
        owner = self
        class Process:
            pid = 987654
            def poll(self): return None if owner.alive else 0
            def wait(self, **kwargs): return 0
        return Process()

    def kill(self, pid, sig):
        if sig:
            self.alive = False
            self.http_up = False

    def ready(self, *args, **kwargs):
        if kwargs.get('cancel') and kwargs['cancel']():
            raise RuntimeError('취소했습니다.')
        self.http_up = True
        return True

    def test_single_job_stops_even_when_unload_api_unavailable(self):
        with ctl.session('llama:1b'):
            self.assertTrue(self.alive)
            self.assertEqual(ctl._read_record()['refs'], 1)
        self.assertFalse(self.alive)
        self.assertFalse(ctl.PID_FILE.exists())
        self.assertEqual(self.spawn_count, 1)

    def test_full_disk_during_release_still_stops_last_owned_server(self):
        with self.assertRaises(OSError):
            with ctl.session('a'):
                failing_write = patch.object(ctl, '_save_sessions', side_effect=OSError('disk full'))
                failing_write.start()
                self.addCleanup(failing_write.stop)
        self.assertFalse(self.alive)
        failing_write.stop()
        with ctl.session('a'):
            self.assertEqual(ctl._read_record()['refs'], 1)
        self.assertFalse(self.alive)

    def test_missing_ollama_is_not_started(self):
        with patch.object(ctl, 'binary', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'CLI'):
                with ctl.session('a'): pass
        self.assertEqual(self.spawn_count, 0)
        self.assertFalse(ctl.PID_FILE.exists())

    def test_startup_failure_cleans_process_and_lease(self):
        with patch.object(ctl, 'wait_until_up', return_value=False):
            with self.assertRaisesRegex(RuntimeError, '실패'):
                with ctl.session('a'): pass
        self.assertFalse(self.alive)
        self.assertEqual(ctl._live_sessions(), {})

    def test_cancelled_startup_cleans_process(self):
        with self.assertRaisesRegex(RuntimeError, '취소'):
            with ctl.session(cancel=lambda: True): pass
        self.assertFalse(self.alive)

    def test_exception_in_job_cleans_process(self):
        with self.assertRaises(ValueError):
            with ctl.session('a'):
                raise ValueError('inference failed')
        self.assertFalse(self.alive)

    def test_install_session_does_not_stop_active_job(self):
        with ctl.session('a'):
            with ctl.session():
                self.assertFalse(ctl.stop())
            self.assertTrue(self.alive)
            self.assertEqual(ctl._read_record()['refs'], 1)
            self.signals.assert_not_called()
        self.assertFalse(self.alive)

    def test_same_model_is_not_unloaded_while_another_job_uses_it(self):
        with ctl.session('a'):
            with ctl.session('a:latest'): pass
            self.unloads.assert_not_called()
            self.assertTrue(self.alive)
        self.assertFalse(self.alive)

    def test_external_server_is_never_killed(self):
        self.http_up = True
        with ctl.session('a'):
            with ctl.session('a:latest'): pass
            self.unloads.assert_not_called()
        self.assertEqual(self.spawn_count, 0)
        self.signals.assert_not_called()
        self.unloads.assert_called_once_with('a')
        self.assertTrue(self.http_up)

    def test_simultaneous_startup_has_one_process_and_two_leases(self):
        barrier = threading.Barrier(2)
        entered = threading.Barrier(2)
        errors = []
        def wait(*args, **kwargs):
            barrier.wait(timeout=5)
            self.http_up = True
            return True
        def run():
            try:
                with ctl.session('same'):
                    entered.wait(timeout=5)
            except BaseException as exc:
                errors.append(exc)
        with patch.object(ctl, 'wait_until_up', side_effect=wait):
            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(self.spawn_count, 1)
        self.assertFalse(self.alive)
        self.unloads.assert_not_called()

    def test_empty_inventory_clears_last_model_and_setup_flag(self):
        state.save_config({'models': ['a'], 'setup_done': True})
        with patch.object(ctl, 'model_inventory', return_value=[]):
            self.assertEqual(catalog.installed_sizes(), {})
        cfg = state.load_config()
        self.assertEqual(cfg['models'], [])
        self.assertFalse(cfg['setup_done'])

    def test_offline_inventory_preserves_cache_without_starting(self):
        state.save_config({'models': ['a'], 'setup_done': True})
        with patch.object(ctl, 'model_inventory', return_value=None):
            self.assertEqual(catalog.installed_sizes(), {'a': None})
        self.assertEqual(self.spawn_count, 0)
        self.assertFalse(self.alive)


class Ownership(unittest.TestCase):
    def test_padded_day_and_space_in_executable_are_valid(self):
        exe = '/Users/test/Applications/Ollama Desk.app/Contents/Resources/ollama'
        for day in (' 8', '18'):
            stamp = f'Tue Sep {day} 09:12:00 2026'
            record = {'pid': 1234, 'lstart': stamp, 'exe': exe}
            with patch.object(ctl.subprocess, 'check_output', return_value=f'{stamp} 1234 1 {exe} serve\n'):
                self.assertTrue(ctl._verify_record(record))
                self.assertFalse(ctl._verify_record({**record, 'exe': exe[:-1]}))
                self.assertFalse(ctl._verify_record({**record, 'lstart': ''}))
                self.assertFalse(ctl._verify_record({**record, 'pid': 4321}))

    def test_truncated_model_pull_is_not_success(self):
        with patch.object(ctl, '_ensure_up'), patch.object(ctl, '_pull_lines', return_value=iter([b'{"status":"pulling manifest"}\n'])):
            with self.assertRaisesRegex(RuntimeError, '연결이 끊겼습니다'):
                list(ctl.pull_model('a'))


class PullCancellation(unittest.TestCase):
    def test_stalled_response_is_cancelled_without_waiting_for_socket_timeout(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(3)
        address = listener.getsockname()
        headers_sent = threading.Event()
        release = threading.Event()
        cancelled = threading.Event()
        def serve():
            try:
                conn, _ = listener.accept()
                with conn:
                    conn.recv(4096)
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson\r\n\r\n")
                    headers_sent.set()
                    release.wait(3)
            finally:
                listener.close()
        server = threading.Thread(target=serve)
        server.start()
        def cancel_after_headers():
            headers_sent.wait(3)
            cancelled.set()
        canceller = threading.Thread(target=cancel_after_headers)
        canceller.start()
        request = urllib.request.Request(f"http://{address[0]}:{address[1]}/api/pull", data=b"{}")
        start = time.monotonic()
        try:
            with patch.object(ctl, '_should_stop', side_effect=cancelled.is_set):
                with self.assertRaisesRegex(RuntimeError, '취소'):
                    list(ctl._pull_lines(request))
            self.assertLess(time.monotonic() - start, 2)
        finally:
            release.set()
            server.join(timeout=4)
            canceller.join(timeout=4)


if __name__ == '__main__':
    unittest.main()
