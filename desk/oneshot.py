"""App-owned one-shot dispatcher. No cron entries, model calls, or notifications here.

The runner atomically claims each persisted job before execution. A claimed job
is never replayed after a restart, including when a previous process crashed.
"""
from __future__ import annotations

from datetime import datetime
import threading
from typing import Callable

from desk import jobs, runner


def is_due(job: dict, now: datetime) -> bool:
    if job.get("schedule_backend") != "desk" or not job.get("once") or not job.get("enabled"):
        return False
    if job.get("once_claimed_at") or job.get("fired_at"):
        return False
    due = jobs._parse_iso(job.get("due_at") or job.get("once_at"))
    if due is None:
        return False
    if due.tzinfo is None:
        due = due.astimezone()
    return due <= now


class Service:
    def __init__(self, poll_seconds: float = 1.0, execute: Callable | None = None):
        self.poll_seconds = poll_seconds
        self.execute = execute or runner.run_job
        self._stop = threading.Event()
        self._guard = threading.Lock()
        self._workers: dict[str, threading.Thread] = {}
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="desk-one-shots", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def scan(self, now: datetime | None = None) -> None:
        if self._stop.is_set():
            return
        now = now or datetime.now().astimezone()
        for job in jobs.list_jobs():
            ident = job.get("id")
            if not ident or not is_due(job, now):
                continue
            with self._guard:
                if ident in self._workers:
                    continue
                worker = threading.Thread(target=self._dispatch, args=(ident,), name=f"desk-once-{ident}", daemon=True)
                self._workers[ident] = worker
                worker.start()

    def _dispatch(self, ident: str) -> None:
        try:
            # Recheck cancellation/deletion after scan. Runner checks and claims
            # fresh state again under its job lock before any model execution.
            if not self._stop.is_set() and is_due(jobs.get_job(ident) or {}, datetime.now().astimezone()):
                self.execute(ident, scheduled=True)
        finally:
            with self._guard:
                self._workers.pop(ident, None)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan()
            except Exception:
                # Polling must never send alerts. Persisted jobs remain available
                # for the next scan; claimed runs are deliberately not replayed.
                pass
            self._stop.wait(self.poll_seconds)


_service: Service | None = None


def start() -> Service:
    global _service
    if _service is None:
        _service = Service()
    _service.start()
    return _service


def stop() -> None:
    global _service
    if _service is not None:
        _service.stop()
        _service = None


def running() -> bool:
    return bool(_service and _service.running())
