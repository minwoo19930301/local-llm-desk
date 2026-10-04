"""Runner regressions: temporary locks and mocked jobs/Ollama/persistence only."""
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from desk import runner


class JobLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patch = mock.patch.object(runner, "PID_DIR", Path(self.tmp.name))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_same_process_contender_cannot_release_owner(self):
        owner, contender = runner.JobLock("same"), runner.JobLock("same")
        self.assertTrue(owner.acquire())
        try:
            self.assertFalse(contender.acquire())
            contender.release()
            self.assertFalse(runner.JobLock("same").acquire())
        finally:
            owner.release()
        self.assertTrue(contender.acquire())
        contender.release()

    def test_simultaneous_threads_have_one_owner(self):
        barrier = threading.Barrier(2)
        locks = [runner.JobLock("same"), runner.JobLock("same")]
        def acquire(lock):
            barrier.wait(timeout=5)
            return lock.acquire()
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(acquire, locks))
            self.assertEqual(sorted(results), [False, True])
        finally:
            for lock in locks:
                lock.release()

    def test_separate_process_cannot_acquire_until_release(self):
        child = """
import sys
from pathlib import Path
from desk import runner
runner.PID_DIR = Path(sys.argv[1])
lock = runner.JobLock('same')
acquired = lock.acquire()
print('true' if acquired else 'false')
if acquired:
    lock.release()
"""
        def child_acquires():
            proc = subprocess.run([sys.executable, "-c", child, self.tmp.name], cwd=str(Path(runner.__file__).resolve().parent.parent), capture_output=True, text=True, timeout=10, check=True)
            return json.loads(proc.stdout)
        owner = runner.JobLock("same")
        self.assertTrue(owner.acquire())
        try:
            self.assertFalse(child_acquires())
        finally:
            owner.release()
        self.assertTrue(child_acquires())


class RunJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.job = {"id": "demo", "title": "demo", "model": "stub", "prompt": "hello", "alert": "off", "enabled": True, "cron": "0 9 * * *"}
        patches = {
            "PID_DIR": Path(self.tmp.name), "ensure_dirs": None,
            "_write_active": None, "_clear_active": None, "_persist": None,
            "_finish": None, "_run_gated": None,
        }
        self.mocks = {}
        for name, value in patches.items():
            patcher = mock.patch.object(runner, name, value) if value is not None else mock.patch.object(runner, name)
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)
        self.mocks["_run_gated"].return_value = {"ok": True, "status": "ok"}
        patcher = mock.patch.object(runner.jobs_mod, "get_job", side_effect=lambda _: dict(self.job))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(runner.alerts, "notify", side_effect=AssertionError("Unexpected notification"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_disabled_scheduled_job_does_not_execute(self):
        self.job["enabled"] = False
        result = runner.run_job("demo", scheduled=True)
        self.mocks["_run_gated"].assert_not_called()
        self.assertEqual(result["status"], "skipped")
        self.assertFalse(result["ok"])

    def test_explicit_manual_run_still_allows_disabled_job(self):
        self.job["enabled"] = False
        runner.run_job("demo", scheduled=False)
        self.mocks["_run_gated"].assert_called_once()

    def test_lock_is_held_through_finalization(self):
        def finish(*args):
            contender = runner.JobLock("demo")
            try:
                self.assertFalse(contender.acquire())
            finally:
                contender.release()
        self.mocks["_finish"].side_effect = finish
        runner.run_job("demo")
        self.mocks["_finish"].assert_called_once()


class EmptyOutputTests(unittest.TestCase):
    def test_empty_plain_model_response_is_not_success(self):
        ctx = runner.RunContext(job_id="demo", title="demo", model="stub", prompt="hello")
        with mock.patch.object(runner.ollama_ctl, "session", return_value=nullcontext()), mock.patch.object(runner.ollama_ctl, "chat", return_value={"message": {"role": "assistant", "content": ""}}), mock.patch.object(runner, "_collect_connectors", return_value=[]):
            result = runner._execute(ctx, {"action": "run", "model": "stub"}, 0)
        self.assertEqual(result["status"], "empty")
        self.assertFalse(result["ok"])
        self.assertEqual(result["output"], "")

    def test_empty_final_summary_after_tool_budget_is_not_success(self):
        from desk import tools
        ctx = runner.RunContext(job_id="demo", title="demo", model="stub", prompt="hello", tools=["http"])
        call = {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "http_get", "arguments": {"url": "https://example.invalid"}}}]}}
        empty = {"message": {"role": "assistant", "content": ""}}
        replies = [call, empty]
        with mock.patch.object(runner.ollama_ctl, "session", return_value=nullcontext()), mock.patch.object(runner.ollama_ctl, "chat_messages", side_effect=replies) as chat, mock.patch.object(runner, "_collect_connectors", return_value=[]), mock.patch.object(tools, "run", return_value="fixture") as dispatch:
            result = runner._execute(ctx, {"action": "run", "model": "stub"}, 0)
        self.assertEqual(result["status"], "empty")
        self.assertFalse(result["ok"])
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(chat.call_count, 2)  # one selected tool iteration plus final summary

    def test_empty_model_response_with_tools_is_not_success(self):
        from desk import tools
        ctx = runner.RunContext(job_id="demo", title="demo", model="stub", prompt="hello", tools=["http"])
        with mock.patch.object(runner.ollama_ctl, "session", return_value=nullcontext()), mock.patch.object(runner.ollama_ctl, "chat_messages", return_value={"message": {"role": "assistant", "content": ""}}), mock.patch.object(runner, "_collect_connectors", return_value=[]), mock.patch.object(tools, "specs", return_value=[]), mock.patch.object(tools, "system_prompt", return_value=""):
            result = runner._execute(ctx, {"action": "run", "model": "stub"}, 0)
        self.assertEqual(result["status"], "empty")
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
