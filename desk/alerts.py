"""macOS 알림·웹훅. 채널별 결과 dict를 항상 돌려준다(예외를 삼키지 않는다)."""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from urllib.parse import quote
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from desk.state import load_config

SEOUL = ZoneInfo("Asia/Seoul")
APP_URL = "http://127.0.0.1:8788"
TITLE_MAX = 80
BODY_MAX = 180
DIALOG_BODY_MAX = 2400
DIALOG_SECONDS = 45


def notify(
    title: str,
    body: str,
    ok: bool = True,
    status: str = "",
    job_id: str = "",
    run_id: str = "",
    kind: str = "run",
) -> dict[str, Any]:
    """설정된 채널로 보낸다. 반환: {"macos": {...}|None, "webhook": {...}|None}."""
    alerts = load_config().get("alerts") or {}
    result: dict[str, Any] = {"macos": None, "webhook": None}
    if alerts.get("macos", True):
        if alerts.get("macos_mode") == "window":
            result["macos"] = _macos_window(run_id)
        elif alerts.get("macos_mode") == "dialog":
            result["macos"] = _macos_dialog(title, body, sound=bool(alerts.get("sound", True)))
        else:
            result["macos"] = _macos(title, body, ok=ok, kind=kind, sound=bool(alerts.get("sound", True)))
    webhook = str(alerts.get("webhook") or "").strip()
    if webhook:
        payload = {
            "title": title,
            "text": body,
            "ok": ok,
            "status": status or ("ok" if ok else "fail"),
            "kind": kind,
            "job_id": job_id,
            "run_id": run_id,
            "url": f"{APP_URL}/jobs?run={run_id}" if run_id else f"{APP_URL}/jobs",
            "at": datetime.now(SEOUL).isoformat(timespec="seconds"),
        }
        result["webhook"] = _webhook(webhook, payload)
    return result


def test() -> dict[str, Any]:
    """설정 화면의 '알림 테스트'."""
    now = datetime.now(SEOUL).strftime("%H:%M:%S")
    return notify("Free AI Scheduler 테스트", f"알림이 정상 동작합니다 · {now}", ok=True, status="ok", kind="test")


def _clean(text: str, limit: int) -> str:
    """이스케이프 전에 자르고 줄바꿈을 ' / '로 접는다."""
    flat = " / ".join(part.strip() for part in (text or "").splitlines() if part.strip())
    return flat[:limit]


def _macos(title: str, body: str, ok: bool, kind: str, sound: bool) -> dict[str, Any]:
    """텍스트는 AppleScript 소스에 넣지 않고 argv로 넘긴다(따옴표·백슬래시 안전)."""
    subtitle = "테스트" if kind == "test" else ("완료" if ok else "실패")
    statement = f'display notification (item 2 of argv) with title (item 1 of argv) subtitle "{subtitle}"'
    if sound:
        statement += ' sound name "default"'
    argv = ["osascript", "-e", "on run argv", "-e", statement, "-e", "end run", "--", _clean(title, TITLE_MAX), _clean(body, BODY_MAX)]
    try:
        proc = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=15)
    except (subprocess.SubprocessError, OSError) as exc:
        return {"ok": False, "code": None, "stderr": str(exc)[:200]}
    # Exit zero means accepted by macOS, not that a banner or sound reached the user.
    return {"ok": proc.returncode == 0, "mode": "notification", "submitted": proc.returncode == 0,
            "visible": None, "code": proc.returncode, "stderr": (proc.stderr or "").strip()[:200]}


def _macos_dialog(title: str, body: str, sound: bool) -> dict[str, Any]:
    """Opt-in foreground result window, bounded even when nobody acknowledges it.

    The job's Ollama session has already ended. Its lock stays held until this
    window closes (at most 45 seconds); once claims prevent replay on restart.
    No other application's UI or global notification preferences are changed.
    """
    statements = ["on run argv", "activate"]
    if sound:
        statements.append("beep 1")
    statements.extend([
        f'set answer to display dialog (item 2 of argv) with title (item 1 of argv) '
        f'buttons {{"확인"}} default button "확인" giving up after {DIALOG_SECONDS}',
        'if gave up of answer then', 'return "expired"', 'end if',
        'return "acknowledged"', 'end run',
    ])
    argv = ["osascript"]
    for statement in statements:
        argv.extend(["-e", statement])
    argv.extend(["--", _clean(title, TITLE_MAX), str(body or "")[:DIALOG_BODY_MAX]])
    try:
        proc = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=DIALOG_SECONDS + 5)
    except (subprocess.SubprocessError, OSError) as exc:
        return {"ok": False, "mode": "dialog", "acknowledged": False, "expired": False,
                "code": None, "stderr": str(exc)[:200]}
    receipt = (proc.stdout or "").strip()
    success = proc.returncode == 0 and receipt in ("acknowledged", "expired")
    return {"ok": success, "mode": "dialog", "acknowledged": success and receipt == "acknowledged",
            "expired": success and receipt == "expired", "code": proc.returncode,
            "stderr": (proc.stderr or "").strip()[:200]}


def _macos_window(run_id: str) -> dict[str, Any]:
    """Ask Launch Services to open only this Desk's already-persisted result.

    Exit zero is an accepted open request, not evidence that a browser window
    was visible or that the user read it. Never opens model-provided URLs.
    """
    receipt: dict[str, Any] = {
        "ok": False, "mode": "window", "opened": False, "submitted": False,
        "visible": None, "code": None, "stderr": "",
    }
    if not run_id:
        receipt["stderr"] = "브라우저 결과 창은 저장된 작업 결과가 있어야 열 수 있습니다. 실행 기록에서 결과를 여세요."
        return receipt
    url = APP_URL + "/jobs?run=" + quote(str(run_id), safe="")
    receipt["url"] = url
    try:
        proc = subprocess.run(["open", url], check=False, capture_output=True, text=True, timeout=5)
    except (subprocess.SubprocessError, OSError) as exc:
        receipt["stderr"] = str(exc)[:200]
        return receipt
    submitted = proc.returncode == 0
    receipt.update(ok=submitted, opened=submitted, submitted=submitted,
                   code=proc.returncode, stderr=(proc.stderr or "").strip()[:200])
    return receipt


def _webhook(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return {"ok": True, "status": int(resp.status), "error": None}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": int(exc.code), "error": exc.reason if isinstance(exc.reason, str) else str(exc.reason)}
    except urllib.error.URLError as exc:
        return {"ok": False, "status": None, "error": str(exc.reason)[:200]}
    except (OSError, ValueError) as exc:
        return {"ok": False, "status": None, "error": str(exc)[:200]}
