#!/bin/zsh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
chmod +x "$ROOT/desk/cron_wrap.sh"
PYTHON="$(command -v python3 || true)"
if [[ -z "$PYTHON" ]] || ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
  echo "Python 3.10 이상이 필요합니다. https://www.python.org/downloads/macos/ 에서 설치한 뒤 다시 실행하세요." >&2
  exit 1
fi
# Opening the scheduler never launches Ollama. Execution sessions start it on demand.
exec "$PYTHON" "$ROOT/server.py"
