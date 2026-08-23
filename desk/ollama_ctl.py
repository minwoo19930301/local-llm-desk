from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Iterator

OLLAMA_HOST = "http://127.0.0.1:11434"


def _get(path: str, timeout: float = 3.0) -> Any:
    req = urllib.request.Request(OLLAMA_HOST + path, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def running() -> bool:
    try:
        _get("/api/tags", timeout=2.0)
        return True
    except Exception:
        return False


def PathApp():
    import os
    import sys
    from pathlib import Path

    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or ""
        return Path(local) / "Programs" / "Ollama" / "ollama.exe"
    home = Path.home() / "Applications" / "Ollama.app"
    system = Path("/Applications/Ollama.app")
    if system.exists():
        return system
    if home.exists():
        return home
    return system


def app_installed() -> bool:
    return PathApp().exists()


def binary() -> str | None:
    import os
    import sys
    from pathlib import Path

    found = shutil.which("ollama")
    if found and Path(found).is_file():
        return found
    cands: list[Path] = []
    if sys.platform == "win32":
        cands.append(PathApp())
    else:
        app = PathApp()
        cands.append(app / "Contents" / "Resources" / "ollama")
        cands.append(Path("/usr/local/bin/ollama"))
        cands.append(Path.home() / "Applications" / "Ollama.app" / "Contents" / "Resources" / "ollama")
    for path in cands:
        try:
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        except OSError:
            continue
    return None


def start() -> None:
    """Headless daemon. GUI 앱을 띄우지 않는다."""
    if running():
        return
    exe = binary()
    if not exe:
        raise RuntimeError("Ollama CLI가 없습니다.")
    subprocess.Popen(
        [exe, "serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def ensure_background() -> None:
    """로그인 후에도 예약이 돌도록 백그라운드에 붙인다."""
    start()
    wait_until_up(20)
    if sys.platform == "darwin":
        _install_launch_agent()


def _install_launch_agent() -> None:
    from pathlib import Path

    exe = binary()
    if not exe:
        return
    agents = Path.home() / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    label = "local-llm-desk.ollama"
    plist = agents / f"{label}.plist"
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{exe}</string>
    <string>serve</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/local-llm-ollama.log</string>
  <key>StandardErrorPath</key><string>/tmp/local-llm-ollama.err</string>
</dict>
</plist>
"""
    plist.write_text(body, encoding="utf-8")
    uid = os.getuid()
    target = f"gui/{uid}/{label}"
    subprocess.run(["launchctl", "bootout", target], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], capture_output=True)


def wait_until_up(seconds: int = 40) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if running():
            return True
        time.sleep(0.5)
    return running()


def list_models() -> list[dict[str, Any]]:
    if not running():
        return []
    data = _get("/api/tags", timeout=5.0)
    return data.get("models") or []


def has_model(name: str) -> bool:
    return any((m.get("name") or m.get("model") or "") == name for m in list_models())


def _should_stop() -> bool:
    try:
        from desk.installer import cancelled

        return cancelled()
    except Exception:
        return False


def remove_model(name: str) -> None:
    if not running():
        start()
        if not wait_until_up(20):
            raise RuntimeError("Ollama가 안 떠 있습니다.")
    body = json.dumps({"name": name}).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_HOST + "/api/delete",
        data=body,
        headers={"Content-Type": "application/json"},
        method="DELETE",
    )
    try:
        urllib.request.urlopen(req, timeout=30)
        return
    except urllib.error.HTTPError as exc:
        if exc.code not in (404, 405):
            raise RuntimeError(exc.read().decode("utf-8", errors="replace")[:240] or "모델 삭제 실패") from exc
    req = urllib.request.Request(
        OLLAMA_HOST + "/api/delete",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=30)


def pull_model(name: str) -> Iterator[str]:
    if not running():
        start()
        if not wait_until_up(40):
            raise RuntimeError("Ollama가 안 떠 있습니다.")
    body = json.dumps({"name": name, "stream": True}).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_HOST + "/api/pull",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=3600) as resp:
        while True:
            if _should_stop():
                yield "취소됨"
                return
            raw = resp.readline()
            if not raw:
                break
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            err = data.get("error")
            if err:
                raise RuntimeError(str(err))
            status = str(data.get("status") or "")
            total = data.get("total") or 0
            done = data.get("completed") or 0
            if total:
                pct = min(100, int(100 * done / total))
                yield f"{status} {pct}%".strip()
            elif status:
                yield status


EFFORT = {
    "low": {"think": False, "num_predict": 400, "timeout": 180},
    "medium": {"think": True, "num_predict": 1200, "timeout": 360},
    "high": {"think": True, "num_predict": 4096, "timeout": 720},
}


def chat(
    model: str,
    prompt: str,
    timeout: int | None = None,
    system: str | None = None,
    effort: str = "medium",
) -> dict[str, Any]:
    knobs = EFFORT.get(effort) or EFFORT["medium"]
    timeout = timeout if timeout is not None else int(knobs["timeout"])
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": knobs["think"],
        "options": {"temperature": 0.2, "num_predict": knobs["num_predict"]},
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_HOST + "/api/chat",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 404:
            raise RuntimeError(f"모델 '{model}'이 이 맥에 없습니다. 설치에서 받으세요.") from exc
        if "think" in detail.lower() or exc.code == 400:
            body.pop("think", None)
            data = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(
                OLLAMA_HOST + "/api/chat",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc2:
                if exc2.code == 404:
                    raise RuntimeError(f"모델 '{model}'이 이 맥에 없습니다. 설치에서 받으세요.") from exc2
                raise
        raise RuntimeError(f"Ollama HTTP {exc.code}: {detail[:240]}") from exc


def extract_text(payload: dict[str, Any]) -> str:
    msg = payload.get("message") or {}
    content = msg.get("content") or payload.get("response") or ""
    return str(content).strip()
