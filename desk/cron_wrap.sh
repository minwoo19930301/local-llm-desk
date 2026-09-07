#!/bin/zsh
# cron → runner 진입점. set -e 를 쓰지 않는다: Ollama 기동 실패도 runner 안에서 기록·알림되어야 한다.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"
cd "$ROOT" || exit 2
JOB_ID="${1:-}"
if [[ -z "$JOB_ID" ]]; then
  echo "usage: cron_wrap.sh JOB_ID" >&2
  exit 2
fi
# crontab 블록 상단의 DESK_PYTHON(서버가 쓰는 인터프리터)을 우선, 없으면 PATH의 python3
PYTHON="${DESK_PYTHON:-}"
if [[ -z "$PYTHON" || ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi
export PYTHONPATH="$ROOT"
mkdir -p "$ROOT/data/logs"
LOG="$ROOT/data/logs/cron.log"
# 실행 중에는 맥이 잠들지 않게 한다(caffeinate가 없으면 그냥 실행)
if command -v caffeinate >/dev/null 2>&1; then
  exec caffeinate -i -s "$PYTHON" "$ROOT/desk/runner.py" --job "$JOB_ID" >>"$LOG" 2>&1
fi
exec "$PYTHON" "$ROOT/desk/runner.py" --job "$JOB_ID" >>"$LOG" 2>&1
