from __future__ import annotations

import json
import subprocess
import urllib.request
from typing import Any

from desk.state import load_config


def notify(title: str, body: str, ok: bool = True) -> None:
    cfg = load_config()
    alerts = cfg.get("alerts") or {}
    if alerts.get("macos", True):
        _macos(title, body)
    webhook = (alerts.get("webhook") or "").strip()
    if webhook:
        _webhook(webhook, title, body, ok)


def _macos(title: str, body: str) -> None:
    safe_title = title.replace('"', '\\"')[:80]
    safe_body = body.replace('"', '\\"')[:180]
    script = (
        f'display notification "{safe_body}" with title "{safe_title}" '
        f'subtitle "로컬 LLM 데스크"'
    )
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


def _webhook(url: str, title: str, body: str, ok: bool) -> None:
    payload: dict[str, Any] = {
        "title": title,
        "text": body,
        "ok": ok,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass
