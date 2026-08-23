#!/bin/zsh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
chmod +x "$ROOT/desk/cron_wrap.sh"
if ! curl -sf -m 2 http://127.0.0.1:11434/api/tags >/dev/null; then
  open -a Ollama >/dev/null 2>&1 || true
fi
PYTHON="$(command -v python3)"
exec "$PYTHON" "$ROOT/server.py"
