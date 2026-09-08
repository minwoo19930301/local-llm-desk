"""JSON state files (config.json, jobs.json) with a cross-process lock.

Writers must wrap load→modify→save in ``with locked():`` so the HTTP server
threads and the cron-spawned runner never lose each other's updates. The lock is
an ``fcntl.flock`` on ``data/.lock`` (released automatically if a process dies)
and is re-entrant within one thread.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from desk.paths import CONFIG_FILE, JOBS_FILE, LOCK_FILE, ensure_dirs

DEFAULT_ALERTS: dict[str, Any] = {"macos": True, "macos_mode": "notification", "sound": True, "webhook": ""}
DEFAULT_CONNECTIONS: dict[str, bool] = {"cli": False, "http": False, "chrome": False}
LOCK_TIMEOUT_S = 30.0

_local = threading.local()


@contextmanager
def locked(timeout: float = LOCK_TIMEOUT_S) -> Iterator[None]:
    """Hold the data-directory lock. Re-entrant for the same thread.

    Raises ``TimeoutError`` (Korean message) if another holder keeps it longer
    than ``timeout`` seconds.
    """
    depth = getattr(_local, "depth", 0)
    if depth:
        _local.depth = depth + 1
        try:
            yield
        finally:
            _local.depth -= 1
        return
    ensure_dirs()
    fh = open(LOCK_FILE, "a+", encoding="utf-8")
    try:
        _acquire(fh, timeout)
        _local.depth = 1
        try:
            yield
        finally:
            _local.depth = 0
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def _acquire(fh: Any, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError("데이터 파일이 다른 작업에 잠겨 있습니다. 잠시 후 다시 시도하세요.") from None
            time.sleep(0.05)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, payload: Any) -> None:
    """Atomic write: unique temp file in the same directory, then ``os.replace``."""
    ensure_dirs()
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False
    )
    try:
        with tmp:
            tmp.write(text)
        os.replace(tmp.name, path)
    except BaseException:
        try:
            os.unlink(tmp.name)
        except FileNotFoundError:
            pass
        raise


def load_config() -> dict[str, Any]:
    ensure_dirs()
    cfg = read_json(CONFIG_FILE, {})
    if not isinstance(cfg, dict):
        cfg = {}
    alerts = cfg.get("alerts") if isinstance(cfg.get("alerts"), dict) else {}
    cfg["alerts"] = {**DEFAULT_ALERTS, **alerts}
    cfg.setdefault("setup_done", False)
    cfg.setdefault("installed", [])
    cfg.setdefault("models", [])
    cfg.setdefault("connections", dict(DEFAULT_CONNECTIONS))
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    with locked():
        write_json(CONFIG_FILE, cfg)


def load_jobs() -> dict[str, Any]:
    ensure_dirs()
    data = read_json(JOBS_FILE, {"jobs": []})
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        data = {"jobs": []}
    return data


def save_jobs(data: dict[str, Any]) -> None:
    with locked():
        write_json(JOBS_FILE, data)
