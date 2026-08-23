from __future__ import annotations

import subprocess
from typing import Any

from desk.paths import CRON_WRAP

BEGIN = "# BEGIN LOCAL-LLM-DESK"
END = "# END LOCAL-LLM-DESK"


def current_user_crontab() -> str:
    try:
        proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=8)
    except subprocess.TimeoutExpired:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout


def managed_block(jobs: list[dict[str, Any]]) -> str:
    lines = [
        BEGIN,
        "SHELL=/bin/zsh",
        "PATH=/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    ]
    for job in jobs:
        if not job.get("enabled"):
            continue
        cron = (job.get("cron") or "").strip()
        if not cron:
            continue
        jid = job["id"]
        title = (job.get("title") or "").replace("\n", " ")
        lines.append(f"{cron} {CRON_WRAP} {jid}  # {title}")
    lines.append(END)
    return "\n".join(lines) + "\n"


def render_preview(jobs: list[dict[str, Any]]) -> str:
    return managed_block(jobs)


def apply(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    existing = current_user_crontab()
    stripped = strip_managed(existing)
    block = managed_block(jobs)
    enabled = [j for j in jobs if j.get("enabled") and (j.get("cron") or "").strip()]
    if not enabled:
        new = stripped.rstrip() + ("\n" if stripped.strip() else "")
    else:
        base = stripped.rstrip()
        new = (base + "\n\n" if base else "") + block
    if not new.endswith("\n"):
        new += "\n"
    try:
        proc = subprocess.run(["crontab", "-"], input=new, capture_output=True, text=True, timeout=8)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "일정 등록이 응답하지 않습니다. 시스템 설정에서 cron 권한을 확인하세요.", "preview": block}
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": (proc.stderr or proc.stdout or "일정 등록 실패").strip(),
            "preview": block,
        }
    return {"ok": True, "preview": block, "installed": current_user_crontab()}


def strip_managed(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    skipping = False
    for line in lines:
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
