import io
import json
import unittest
import urllib.error
import urllib.request
from unittest import mock

from desk import http_api
from tests.http_fixture import isolated_server


class ApiIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = isolated_server()
        cls.base = cls.fixture.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.fixture.__exit__(None, None, None)

    def request(self, method, path, body=None, headers=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

    def test_job_create_edit_try_delete(self):
        headers = {"X-Requested-With": "free-ai-scheduler"}
        payload = {"title": "fixture", "prompt": "hello", "model": "fixture-model:latest", "preset": "save", "alert": "off", "effort": "low", "ram_policy": "skip", "max_minutes": 5, "num_ctx": 2048}
        code, result = self.request("POST", "/api/jobs", payload, headers)
        self.assertEqual(code, 200, result)
        ident = result["job"]["id"]
        code, result = self.request("PATCH", f"/api/jobs/{ident}", {"title": "changed", "max_minutes": 7}, headers)
        self.assertEqual(code, 200, result)
        self.assertEqual(result["job"]["max_minutes"], 7)
        code, result = self.request("POST", "/api/jobs/try", payload, headers)
        self.assertEqual(code, 200, result)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["output"], "격리된 테스트 응답")
        code, result = self.request("POST", "/api/jobs/try", {**payload, "prompt": "EMPTY_FIXTURE"}, headers)
        self.assertEqual(result["status"], "empty")
        self.assertFalse(result["ok"])
        self.assertEqual(self.request("DELETE", f"/api/jobs/{ident}", headers=headers)[0], 200)

    def test_csrf_and_traversal(self):
        self.assertEqual(self.request("POST", "/api/jobs", {})[0], 403)
        self.assertEqual(self.request("POST", "/api/jobs", {}, {"X-Requested-With": "free-ai-scheduler", "Origin": "http://evil.invalid"})[0], 403)
        self.assertEqual(self.request("GET", "/static/../desk/http_api.py")[0], 404)

    def test_status_does_not_start_ollama(self):
        with mock.patch.object(http_api.ollama_ctl, "start", side_effect=AssertionError("Must stay idle")):
            self.assertEqual(self.request("GET", "/api/status")[0], 200)

    def test_alert_mode_persists_without_sending(self):
        headers = {"X-Requested-With": "free-ai-scheduler"}
        from desk import alerts
        with mock.patch.object(alerts, "notify") as notify:
            code, result = self.request("POST", "/api/alerts", {"macos_mode": "dialog"}, headers)
            self.assertEqual(code, 200)
            self.assertEqual(result["alerts"]["macos_mode"], "dialog")
            self.assertEqual(self.request("POST", "/api/alerts", {"macos_mode": "unknown"}, headers)[0], 400)
            self.assertEqual(self.request("GET", "/api/status")[1]["alerts"]["macos_mode"], "dialog")
        notify.assert_not_called()

    def test_window_mode_persists_without_launch(self):
        from desk import alerts
        headers = {"X-Requested-With": "free-ai-scheduler"}
        with mock.patch.object(alerts, '_macos_window') as opened:
            code, result = self.request('POST', '/api/alerts', {'macos_mode': 'window'}, headers)
            self.assertEqual(code, 200)
            self.assertEqual(result['alerts']['macos_mode'], 'window')
            self.assertEqual(self.request('GET', '/api/status')[1]['alerts']['macos_mode'], 'window')
        opened.assert_not_called()

    def test_open_saved_result_does_not_rerun_job(self):
        from desk import alerts
        headers = {"X-Requested-With": "free-ai-scheduler"}
        receipt = {'ok': True, 'mode': 'window', 'submitted': True, 'opened': True, 'visible': None}
        with mock.patch.object(http_api.runner, 'get_run', return_value={'id': 'saved'}, create=True) as get_run, mock.patch.object(http_api.runner, 'run_job', side_effect=AssertionError('must not rerun')), mock.patch.object(alerts, '_macos_window', return_value=receipt) as opened:
            self.assertEqual(self.request('POST', '/api/runs/saved/open')[0], 403)
            opened.assert_not_called()
            code, result = self.request('POST', '/api/runs/saved/open', {}, headers)
            self.assertEqual(code, 200)
            self.assertEqual(result, receipt)
            get_run.assert_called_once_with('saved')
            opened.assert_called_once_with('saved')
            opened.reset_mock()
            get_run.return_value = None
            self.assertEqual(self.request('POST', '/api/runs/missing/open', {}, headers)[0], 404)
            opened.assert_not_called()

    def test_open_real_persisted_result_keeps_record_and_never_executes(self):
        from desk import alerts
        headers = {"X-Requested-With": "free-ai-scheduler"}
        run_id = 'windowfixture-1234567890'
        path = http_api.runner.RUNS_DIR / 'windowfixture.jsonl'
        original = json.dumps({'id': run_id, 'job_id': 'windowfixture', 'ok': True, 'status': 'ok', 'output': 'already saved'}) + '\n'
        path.write_text(original, encoding='utf-8')
        try:
            with mock.patch.object(http_api.runner, 'run_job', side_effect=AssertionError('must not rerun')), mock.patch.object(alerts, '_macos_window', return_value={'ok': True, 'submitted': True, 'visible': None}) as opened:
                code, result = self.request('POST', f'/api/runs/{run_id}/open', {}, headers)
                self.assertEqual(code, 200)
                self.assertTrue(result['submitted'])
                opened.assert_called_once_with(run_id)
                self.assertEqual(path.read_text(encoding='utf-8'), original)
        finally:
            path.unlink()


class InstallStreamCleanup(unittest.TestCase):
    def handler(self):
        handler = object.__new__(http_api.Handler)
        handler.wfile = io.BytesIO()
        for name in ("send_response", "send_header", "end_headers"):
            setattr(handler, name, mock.Mock())
        return handler

    def test_done_event_closes_generator(self):
        closed = []
        def events(body):
            try:
                yield {"event": "done", "ok": True}
            finally:
                closed.append(True)
        self.handler()._sse(events, {})
        self.assertEqual(closed, [True])
        self.assertFalse(http_api._install_lock.locked())

    def test_disconnected_response_releases_install_lock(self):
        handler = self.handler()
        handler.end_headers.side_effect = BrokenPipeError
        with mock.patch.object(http_api.installer, "request_cancel"):
            handler._sse(lambda _: iter(()), {})
        self.assertFalse(http_api._install_lock.locked())


if __name__ == "__main__":
    unittest.main()
