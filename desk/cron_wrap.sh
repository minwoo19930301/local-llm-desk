#!/bin/zsh
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"
cd "$ROOT"
JOB_ID="${1:-}"
if [[ -z "$JOB_ID" ]]; then
  echo "usage: cron_wrap.sh JOB_ID" >&2
  exit 2
fi
PYTHON="$(command -v python3)"
export PYTHONPATH="$ROOT"
"$PYTHON" - <<'PY'
from desk.ollama_ctl import start, wait_until_up
start()
raise SystemExit(0 if wait_until_up(40) else 1)
PY
exec "$PYTHON" "$ROOT/desk/runner.py" --job "$JOB_ID"
