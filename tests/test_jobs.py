"""Unit checks for jobs / state / crontab_sync / http_api pure functions.

Run: python3 -m unittest tests.test_jobs
Writes go to a temp directory; crontab_sync.apply is stubbed so the real crontab
is never touched.
"""

from __future__ import annotations

import subprocess
import shlex
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from desk import connectors, crontab_sync, http_api, jobs, state


class TempData(unittest.TestCase):
    """Point state.py at a scratch directory and stub crontab writes."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(state, "ensure_dirs"),
            mock.patch.object(connectors, "load", return_value=[{"id": "c1"}]),
            mock.patch.object(state, "JOBS_FILE", base / "jobs.json"),
            mock.patch.object(state, "CONFIG_FILE", base / "config.json"),
            mock.patch.object(state, "LOCK_FILE", base / ".lock"),
            mock.patch.object(jobs.crontab_sync, "apply", lambda items: {"ok": True, "preview": "", "jobs": len(items)}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()


class NormalizeKnobs(unittest.TestCase):
    def test_defaults(self) -> None:
        knobs = jobs.normalize_knobs({})
        self.assertEqual(knobs["ram_policy"], "defer")
        self.assertEqual(knobs["defer_max_min"], 30)
        self.assertEqual(knobs["num_ctx"], 0)  # 0 = effort 기본 컨텍스트(runner 가 결정)
        self.assertEqual(jobs.normalize_knobs({"num_ctx": 8192})["num_ctx"], 8192)
        with self.assertRaises(ValueError):
            jobs.normalize_knobs({"num_ctx": 100})
        self.assertEqual(knobs["max_minutes"], 30)
        self.assertEqual(knobs["connectors"], [])
        self.assertEqual(knobs["fallback_model"], "")
        self.assertEqual(knobs["max_loops"], 4)

    def test_ranges(self) -> None:
        with self.assertRaises(ValueError):
            jobs.normalize_knobs({"defer_max_min": 0})
        with self.assertRaises(ValueError):
            jobs.normalize_knobs({"max_minutes": 721})
        with self.assertRaises(ValueError):
            jobs.normalize_knobs({"num_ctx": "abc"})
        with self.assertRaises(ValueError):
            jobs.normalize_knobs({"ram_policy": "nope"})
        with self.assertRaises(ValueError):
            jobs.normalize_knobs({"connectors": "c1"})
        self.assertEqual(jobs.normalize_knobs({"max_loops": 999})["max_loops"], 32)

    def test_connectors_dedupe(self) -> None:
        with mock.patch.object(connectors, "load", return_value=[{"id": "a"}, {"id": "b"}]):
            self.assertEqual(jobs.normalize_knobs({"connectors": ["a", " a ", "b", ""]})["connectors"], ["a", "b"])

    def test_unknown_connector_is_rejected(self) -> None:
        with mock.patch.object(connectors, "load", return_value=[]):
            with self.assertRaisesRegex(ValueError, "등록되지 않은 연동"):
                jobs.normalize_knobs({"connectors": ["missing"]})


class CreateUpdate(TempData):
    def _create(self, **extra: object) -> dict:
        payload = {"prompt": "hi", "model": "llama3.2:3b", "preset": "daily", "time": "09:30", **extra}
        return jobs.create_job(payload)["job"]

    def test_create_schema(self) -> None:
        job = self._create(connectors=["c1"], ram_policy="skip", num_ctx=8192)
        self.assertEqual(job["cron"], "30 9 * * *")
        self.assertEqual(job["connectors"], ["c1"])
        self.assertEqual(job["ram_policy"], "skip")
        self.assertEqual(job["num_ctx"], 8192)
        self.assertIn("schedule_label", job)
        self.assertTrue(job["enabled"])

    def test_create_rejects_bad_cron_and_provider(self) -> None:
        with self.assertRaises(ValueError):
            jobs.create_job({"prompt": "x", "model": "m", "preset": "cron", "cron": "99 99 * * *"})
        with self.assertRaises(ValueError):
            jobs.create_job({"prompt": "x", "model": "m", "provider": "openai"})

    def test_update_keeps_model_when_blank(self) -> None:
        job = self._create()
        updated = jobs.update_job(job["id"], {"model": ""})["job"]
        self.assertEqual(updated["model"], "llama3.2:3b")
        self.assertIs(jobs.update_job(job["id"], {"enabled": 0})["job"]["enabled"], False)
        with self.assertRaises(ValueError):
            jobs.update_job(job["id"], {"provider": "openai"})

    def test_once_not_rearmed_on_title_patch(self) -> None:
        job = jobs.create_job({"prompt": "x", "model": "m", "preset": "once_1m"})["job"]
        cron = job["cron"]
        self.assertTrue(job["once"])
        self.assertIsNotNone(job["once_at"])
        with mock.patch.object(jobs, "_now", return_value=jobs._now().replace(year=2031)):
            same = jobs.update_job(job["id"], {"title": "new", "preset": "once_1m", "time": "17:00", "repeat": "daily"})["job"]
        self.assertEqual(same["cron"], cron)

    def test_fired_once_label_and_reschedule(self) -> None:
        job = jobs.create_job({"prompt": "x", "model": "m", "preset": "once_1m"})["job"]
        self.assertIsNotNone(jobs.disable_if_once(job["id"]))
        fired = jobs.get_job(job["id"])
        self.assertFalse(fired["enabled"])
        self.assertEqual(jobs.schedule_label(fired), "한 번 실행함")
        again = jobs.update_job(job["id"], {"preset": "daily", "time": "08:00"})["job"]
        self.assertTrue(again["enabled"])
        self.assertEqual(again["cron"], "0 8 * * *")

    def test_schedule_only_changes_when_fields_differ(self) -> None:
        job = self._create()
        cron_changed = jobs.update_job(job["id"], {"preset": "daily", "time": "10:00"})["job"]
        self.assertEqual(cron_changed["cron"], "0 10 * * *")
        cron_job = jobs.update_job(job["id"], {"preset": "cron", "cron": "*/15 * * * *"})["job"]
        self.assertEqual(cron_job["cron"], "*/15 * * * *")

    def test_mark_last_run_and_delete(self) -> None:
        job = self._create()
        jobs.mark_last_run(job["id"], {"ok": True, "at": "x", "seconds": 1, "error": None, "status": "ok"})
        self.assertEqual(jobs.get_job(job["id"])["last_run"]["status"], "ok")
        jobs.delete_job(job["id"])
        self.assertIsNone(jobs.get_job(job["id"]))


class Locking(TempData):
    def test_concurrent_writers_do_not_lose_updates(self) -> None:
        def worker(n: int) -> None:
            jobs.create_job({"prompt": f"p{n}", "model": "m", "preset": "save"})

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(jobs.list_jobs()), 12)

    def test_locked_is_reentrant(self) -> None:
        with state.locked():
            with state.locked():
                state.save_config(state.load_config())
        self.assertEqual(state.load_config()["alerts"], {"macos": True, "sound": True, "webhook": ""})


class Crontab(unittest.TestCase):
    def _proc(self, code: int, out: str = "", err: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(["crontab"], code, out, err)

    def test_no_crontab_is_empty(self) -> None:
        with mock.patch.object(subprocess, "run", return_value=self._proc(1, err="crontab: no crontab for me")):
            self.assertEqual(crontab_sync.current_user_crontab(), "")

    def test_other_failures_are_none_and_apply_refuses(self) -> None:
        with mock.patch.object(subprocess, "run", return_value=self._proc(1, err="permission denied")):
            self.assertIsNone(crontab_sync.current_user_crontab())
            result = crontab_sync.apply([{"id": "a", "enabled": True, "cron": "0 9 * * *", "title": "t"}])
        self.assertFalse(result["ok"])
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("crontab", 8)):
            self.assertIsNone(crontab_sync.current_user_crontab())

    def test_unterminated_block_refuses_write(self) -> None:
        text = f"a\n{crontab_sync.BEGIN}\nx\n"
        self.assertTrue(crontab_sync.has_unterminated_block(text))
        with mock.patch.object(subprocess, "run", return_value=self._proc(0, out=text)) as run:
            result = crontab_sync.apply([{"id": "a", "enabled": True, "cron": "0 9 * * *", "title": "t"}])
            self.assertEqual(run.call_count, 1)
        self.assertFalse(result["ok"])

    def test_strip_and_compose(self) -> None:
        existing = f"0 1 * * * echo hi\n\n{crontab_sync.BEGIN}\nold\n{crontab_sync.END}\n"
        self.assertEqual(crontab_sync.strip_managed(existing), "0 1 * * * echo hi\n")
        new = crontab_sync.compose(existing, [{"id": "j1", "enabled": True, "cron": "0 9 * * *", "title": "50% off\x01"}])
        self.assertIn("0 1 * * * echo hi", new)
        self.assertIn("DESK_PYTHON=", new)
        self.assertIn("# 50 off", new)
        self.assertNotIn("%", new.split(crontab_sync.BEGIN)[1])
        self.assertEqual(crontab_sync.compose(existing, []), "0 1 * * * echo hi\n")

    def test_command_preserves_paths_with_spaces_quotes_and_percent(self) -> None:
        wrapper = Path("/tmp/Team's 50% projects/desk/cron_wrap.sh")
        python = "/tmp/Python 3/bin/python"
        with mock.patch.object(crontab_sync, "CRON_WRAP", wrapper), mock.patch.object(crontab_sync.sys, "executable", python):
            block = crontab_sync.managed_block([{"id": "abc", "enabled": True, "cron": "0 9 * * *"}])
        command = next(line for line in block.splitlines() if line.startswith("0 9 ")).split(maxsplit=5)[5]
        self.assertIn(r"\%", command)
        # cron removes the escape before passing the command to its shell.
        argv = shlex.split(command.replace(r"\%", "%"), comments=True)
        self.assertEqual(argv, [f"DESK_PYTHON={python}", str(wrapper), "abc"])

    def test_invalid_cron_skipped(self) -> None:
        block = crontab_sync.managed_block([{"id": "j", "enabled": True, "cron": "99 99 * * *", "title": "t"}])
        self.assertNotIn("99 99", block)
        self.assertTrue(crontab_sync.valid_cron("30 9 * * 1-5"))
        self.assertFalse(crontab_sync.valid_cron("a b c d e"))


class Csrf(unittest.TestCase):
    GOOD = {"X-Requested-With": "free-ai-scheduler", "Host": "127.0.0.1:8788"}

    def test_header_required(self) -> None:
        self.assertTrue(http_api.csrf_ok(self.GOOD, 8788))
        self.assertFalse(http_api.csrf_ok({"Host": "127.0.0.1:8788"}, 8788))

    def test_origin_and_host(self) -> None:
        self.assertTrue(http_api.csrf_ok({**self.GOOD, "Origin": "http://localhost:8788"}, 8788))
        self.assertFalse(http_api.csrf_ok({**self.GOOD, "Origin": "http://evil.example"}, 8788))
        self.assertFalse(http_api.csrf_ok({**self.GOOD, "Host": "evil.example:8788"}, 8788))
        self.assertFalse(http_api.csrf_ok({**self.GOOD, "Host": "127.0.0.1:9999"}, 8788))

    def test_no_options_handler(self) -> None:
        self.assertFalse(hasattr(http_api.Handler, "do_OPTIONS"))


class Redact(unittest.TestCase):
    def test_env_and_query_hidden(self) -> None:
        con = {"id": "c1", "env": {"TOKEN": "secret"}, "headers": {"Authorization": "x"}, "url": "https://a/b?key=1"}
        out = http_api.redact(con)
        self.assertNotIn("env", out)
        self.assertNotIn("headers", out)
        self.assertEqual(out["env_keys"], ["TOKEN"])
        self.assertEqual(out["url"], "https://a/b?<redacted>")
        self.assertNotIn("secret", str(out))


class Conflicts(unittest.TestCase):
    def test_overlap_detected(self) -> None:
        lanes = [
            {"job_id": "a", "avg_seconds": 120, "upcoming": ["2026-09-05T09:00:00+09:00"]},
            {"job_id": "b", "avg_seconds": 60, "upcoming": ["2026-09-05T09:01:00+09:00", "2026-09-05T12:00:00+09:00"]},
        ]
        found = http_api.conflicts(lanes)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["job_ids"], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
