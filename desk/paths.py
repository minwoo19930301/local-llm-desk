from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATIC = ROOT / "static"
JOBS_FILE = DATA / "jobs.json"
CONFIG_FILE = DATA / "config.json"
RUNS_DIR = DATA / "runs"
LOGS_DIR = DATA / "logs"
CRON_WRAP = ROOT / "desk" / "cron_wrap.sh"
PID_DIR = DATA / "pids"


def ensure_dirs() -> None:
    for path in (DATA, RUNS_DIR, LOGS_DIR, PID_DIR):
        path.mkdir(parents=True, exist_ok=True)
