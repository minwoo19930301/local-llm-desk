"""Filesystem layout. Every module resolves paths from here, never from HOME or a user name."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATIC = ROOT / "static"
JOBS_FILE = DATA / "jobs.json"
CONFIG_FILE = DATA / "config.json"
CONNECTORS_FILE = DATA / "connectors.json"
LOCK_FILE = DATA / ".lock"
RUNS_DIR = DATA / "runs"
LOGS_DIR = DATA / "logs"
PID_DIR = DATA / "pids"
WORKSPACE_DIR = DATA / "workspace"
CRON_WRAP = ROOT / "desk" / "cron_wrap.sh"


def ensure_dirs() -> None:
    for path in (DATA, RUNS_DIR, LOGS_DIR, PID_DIR, WORKSPACE_DIR):
        path.mkdir(parents=True, exist_ok=True)
