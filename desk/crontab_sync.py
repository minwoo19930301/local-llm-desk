"""Keep the user's crontab in sync with enabled jobs.

Only the block between ``BEGIN`` and ``END`` markers is ours; everything else in
the crontab is preserved verbatim. We never write when we could not read the
existing crontab reliably.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from typing import Any

from desk.paths import CRON_WRAP

BEGIN = "# BEGIN LOCAL-LLM-DESK"
END = "# END LOCAL-LLM-DESK"
CRON_TIMEOUT_S = 8
TITLE_MAX = 60

_NO_CRONTAB = re.compile(r"no crontab for", re.IGNORECASE)
_FIELD = re.compile(r"^(\*|\d+)(-\d+)?(/\d+)?(,(\*|\d+)(-\d+)?(/\d+)?)*$")
_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def valid_cron(expr: str) -> bool:
    """Five-field cron validation. Uses ``cronwhen`` when present, else a strict local check."""
    text = (expr or "").strip()
    if not text:
        return False
    try:
        from desk import cronwhen
    except ImportError:
        return _basic_cron_ok(text)
    return bool(cronwhen.is_valid(text))


def _basic_cron_ok(expr: str) -> bool:
    parts = expr.split()
    if len(parts) != 5:
        return False
    for part, (low, high) in zip(parts, _FIELD_RANGES):
        if not _FIELD.match(part):
            return False
        for num in re.findall(r"\d+", part):
            if not low <= int(num) <= high:
                return False
        if "/0" in part:
            return False
    return True


def current_user_crontab() -> str | None:
    """Return the crontab text, ``""`` when the user has none, ``None`` on any failure."""
    try:
        proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=CRON_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode == 0:
        return proc.stdout
    if _NO_CRONTAB.search(proc.stderr or "") or _NO_CRONTAB.search(proc.stdout or ""):
        return ""
    return None


def _comment(title: str) -> str:
    """Cron treats ``%`` as newline; strip it and control characters from the trailing comment."""
    clean = re.sub(r"[%\x00-\x1f\x7f]", " ", title or "")
    return re.sub(r"\s+", " ", clean).strip()[:TITLE_MAX]


def managed_block(jobs: list[dict[str, Any]]) -> str:
    lines = [
        BEGIN,
        "SHELL=/bin/zsh",
        "PATH=/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    ]
    for job in jobs:
        cron = (job.get("cron") or "").strip()
        if not job.get("enabled") or not valid_cron(cron):
            continue
        command = f"DESK_PYTHON={shlex.quote(sys.executable)} {shlex.quote(str(CRON_WRAP))} {shlex.quote(str(job['id']))}"
        # cron processes percent signs before the shell, even inside quotes.
        command = command.replace("%", r"\%")
        lines.append(f"{cron} {command}  # {_comment(job.get('title') or '')}")
    lines.append(END)
    return "\n".join(lines) + "\n"


def render_preview(jobs: list[dict[str, Any]]) -> str:
    return managed_block(jobs)


def has_unterminated_block(text: str) -> bool:
    lines = [ln.strip() for ln in text.splitlines()]
    return BEGIN in lines and END not in lines


def compose(existing: str, jobs: list[dict[str, Any]]) -> str:
    """Existing crontab minus our block, plus a fresh block when any job is scheduled."""
    stripped = strip_managed(existing)
    block = managed_block(jobs)
    scheduled = any(j.get("enabled") and valid_cron(j.get("cron") or "") for j in jobs)
    base = stripped.rstrip()
    if not scheduled:
        new = base + ("\n" if base else "")
    else:
        new = (base + "\n\n" if base else "") + block
    return new if new.endswith("\n") or not new else new + "\n"


def apply(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    block = managed_block(jobs)
    existing = current_user_crontab()
    if existing is None:
        return {
            "ok": False,
            "error": "현재 crontab을 읽지 못해 예약을 건드리지 않았습니다. 시스템 설정에서 cron 권한을 확인하세요.",
            "preview": block,
        }
    if has_unterminated_block(existing):
        return {
            "ok": False,
            "error": f"crontab에 '{BEGIN}'만 있고 '{END}'가 없어 쓰기를 멈췄습니다. crontab -e 로 정리하세요.",
            "preview": block,
        }
    new = compose(existing, jobs)
    if new.strip() == existing.strip():
        return {"ok": True, "preview": block, "installed": existing, "unchanged": True}
    try:
        proc = subprocess.run(["crontab", "-"], input=new, capture_output=True, text=True, timeout=CRON_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError):
        return {"ok": False, "error": "일정 등록이 응답하지 않습니다. 시스템 설정에서 cron 권한을 확인하세요.", "preview": block}
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout or "일정 등록 실패").strip(), "preview": block}
    return {"ok": True, "preview": block, "installed": current_user_crontab() or ""}


def strip_managed(text: str) -> str:
    out: list[str] = []
    skipping = False
    for line in text.splitlines():
        if line.strip() == BEGIN:
            skipping = True
            continue
        if line.strip() == END:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + ("\n" if out else "")
