"""App timer tests: temporary job state, mocked inference, no real cron/alerts."""
from datetime import datetime, timedelta, timezone
import threading
from unittest import mock

from desk import crontab_sync, jobs, oneshot, runner
from tests.test_jobs import TempData


class AppTimers(TempData):
    def setUp(self):
        super().setUp()
        self._runner_patches = [
            mock.patch.object(runner, "PID_DIR", self.tmp_path / "pids"),
            mock.patch.object(runner, "ensure_dirs"),
            mock.patch.object(runner, "_write_active"), mock.patch.object(runner, "_clear_active"),
            mock.patch.object(runner, "_persist"),
            mock.patch.object(runner.alerts, "notify", side_effect=AssertionError("No real alerts")),
        ]
        for patcher in self._runner_patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    @property
    def tmp_path(self):
        from pathlib import Path
        return Path(self.tmp.name)

    def make_job(self, seconds_ago=0):
        now = datetime.now(timezone.utc) - timedelta(minutes=5, seconds=seconds_ago)
        with mock.patch.object(jobs, "_now", return_value=now):
            return jobs.create_job({"prompt": "fixture", "model": "stub", "preset": "once_5m", "alert": "off"})["job"]

    def test_registration_cancel_and_completion_never_touch_cron(self):
        with mock.patch.object(crontab_sync, "apply", side_effect=AssertionError("No cron")):
            job = jobs.create_job({"prompt": "fixture", "model": "stub", "preset": "once_5m", "alert": "off"})
            self.assertEqual(job["crontab"]["backend"], "desk")
            self.assertTrue(job["crontab"]["registered"])
            self.assertFalse(job["crontab"]["cron_changed"])
            ident = job["job"]["id"]
            jobs.update_job(ident, {"title": "changed"})
            jobs.disable_if_once(ident)
            jobs.delete_job(ident)

    def test_cron_preview_excludes_desk_even_with_old_cron_field(self):
        job = {"id": "desk", "schedule_backend": "desk", "enabled": True, "cron": "* * * * *"}
        self.assertNotIn("cron_wrap.sh", crontab_sync.managed_block([job]))
        self.assertEqual(crontab_sync.compose("", [job]), "")

    def test_due_filter_ignores_disabled_deleted_claimed_and_other_backends(self):
        job = self.make_job(seconds_ago=1)
        now = datetime.now(timezone.utc)
        self.assertTrue(oneshot.is_due(job, now))
        for patch in ({"enabled": False}, {"once_claimed_at": now.isoformat()}, {"fired_at": now.isoformat()}, {"schedule_backend": "cron"}, {"due_at": (now + timedelta(minutes=5)).isoformat()}):
            self.assertFalse(oneshot.is_due({**job, **patch}, now))
        self.assertFalse(oneshot.is_due({}, now))

    def test_early_runner_does_not_claim_or_execute(self):
        job = self.make_job(seconds_ago=-300)
        with mock.patch.object(runner, "_run_gated") as execute:
            result = runner.run_job(job["id"], scheduled=True)
        self.assertEqual(result["status"], "skipped")
        execute.assert_not_called()
        self.assertNotIn("once_claimed_at", jobs.get_job(job["id"]))

    def test_multiple_dispatchers_claim_once_and_preserve_result(self):
        job = self.make_job(seconds_ago=1)
        jobs.update_job(job["id"], {"alert": "always"})
        completed = threading.Event()
        def execute(ctx, wait):
            completed.set()
            return runner._record(ctx, "ok", task=runner.TaskResult(text="fixture result"))
        services = [oneshot.Service(poll_seconds=0.01), oneshot.Service(poll_seconds=0.01)]
        with mock.patch.object(crontab_sync, "apply", side_effect=AssertionError("No cron")), mock.patch.object(runner, "_run_gated", side_effect=execute) as inference, mock.patch.object(runner.alerts, "notify", return_value={"ok": True}) as notify:
            for service in services:
                service.start()
            try:
                self.assertTrue(completed.wait(3))
            finally:
                for service in services:
                    service.stop()
                    with service._guard:
                        workers = list(service._workers.values())
                    for worker in workers:
                        worker.join(timeout=3)
            self.assertEqual(inference.call_count, 1)
            self.assertEqual(notify.call_count, 1)
        stored = jobs.get_job(job["id"])
        self.assertFalse(stored["enabled"])
        self.assertIn("once_claimed_at", stored)
        self.assertIn("fired_at", stored)
        self.assertEqual(stored["last_run"]["status"], "ok")
        self.assertFalse(oneshot.is_due(stored, datetime.now(timezone.utc)))

    def test_restart_does_not_replay_claimed_job(self):
        job = self.make_job(seconds_ago=1)
        claimed, error = jobs.claim_desk_once(job["id"])
        self.assertIsNone(error)
        self.assertIn("once_claimed_at", claimed)
        for _ in range(2):
            service = oneshot.Service(execute=mock.Mock())
            scanned = threading.Event()
            def scan_and_signal():
                service.scan()
                scanned.set()
            with mock.patch.object(service, "_loop", side_effect=scan_and_signal):
                service.start()
                self.assertTrue(scanned.wait(3))
                service.stop()
            service.execute.assert_not_called()
        self.assertIsNone(jobs.next_run(jobs.get_job(job["id"])))

    def test_disabled_or_deleted_before_deadline_is_not_dispatched(self):
        first = self.make_job(seconds_ago=1)
        second = self.make_job(seconds_ago=1)
        jobs.update_job(first["id"], {"enabled": False})
        jobs.delete_job(second["id"])
        service = oneshot.Service(execute=mock.Mock())
        service.scan()
        service.execute.assert_not_called()

    def test_overdue_timer_uses_expiry_and_does_not_infer(self):
        job = self.make_job(seconds_ago=11 * 60)
        with mock.patch.object(runner, "_run_gated") as execute:
            result = runner.run_job(job["id"], scheduled=True)
        execute.assert_not_called()
        self.assertEqual(result["status"], "skipped")
        stored = jobs.get_job(job["id"])
        self.assertFalse(stored["enabled"])
        self.assertIn("fired_at", stored)

    def test_stop_prevents_new_dispatch(self):
        self.make_job(seconds_ago=1)
        service = oneshot.Service(execute=mock.Mock())
        service.stop()
        service.scan()
        service.execute.assert_not_called()
