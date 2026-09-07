"""Ollama 서버 제어와 /api/chat 호출.

수명 규칙(research_ollamaRam.md §5): **우리가 start()로 띄운 서버만 끈다.**
사용자의 Ollama 앱 등 이미 떠 있던 서버는 건드리지 않고, 우리가 쓴 모델만 keep_alive:0 으로 언로드한다.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Iterator

from desk import state
from desk.paths import LOGS_DIR, PID_DIR

OLLAMA_HOST = "http://127.0.0.1:11434"
PID_FILE = PID_DIR / "ollama.json"
SERVE_LOG = LOGS_DIR / "ollama-serve.log"
KEEP_ALIVE = "2m"
SERVE_ENV = {
    "OLLAMA_KEEP_ALIVE": KEEP_ALIVE,
    "OLLAMA_MAX_LOADED_MODELS": "1",
    "OLLAMA_NUM_PARALLEL": "1",
    "OLLAMA_FLASH_ATTENTION": "1",
}

_proc: subprocess.Popen | None = None  # 우리가 띄운 서버 (좀비 방지용 참조)


# --- HTTP 헬퍼 ---------------------------------------------------------------


def _get(path: str, timeout: float = 3.0) -> Any:
    req = urllib.request.Request(OLLAMA_HOST + path, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(path: str, body: dict[str, Any], timeout: float = 10.0, method: str = "POST") -> Any:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(OLLAMA_HOST + path, data=data, headers={"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw.strip() else {}


def running() -> bool:
    try:
        _get("/api/tags", timeout=2.0)
        return True
    except Exception:
        return False


# --- 설치 위치 ---------------------------------------------------------------


def PathApp() -> Path:
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


# --- 서버 기동/종료 (소유권 추적) ------------------------------------------------


def start() -> bool:
    """헤드리스 서버를 띄운다. 이미 떠 있으면 False(남의 서버), 우리가 띄웠으면 True."""
    global _proc
    with state.locked():
        if running():
            return False
        exe = binary()
        if not exe:
            raise RuntimeError("Ollama CLI가 없습니다.")
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with SERVE_LOG.open("ab") as log:
            proc = subprocess.Popen(
                [exe, "serve"],
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env={**os.environ, **SERVE_ENV},
            )
        _proc = proc
        _write_record(proc.pid, exe)
    return True


def _write_record(pid: int, exe: str, refs: int = 0) -> None:
    PID_DIR.mkdir(parents=True, exist_ok=True)
    record = {"pid": pid, "lstart": _ps_lstart(pid), "exe": exe, "started_at": time.time(), "refs": refs}
    PID_FILE.write_text(json.dumps(record) + "\n", encoding="utf-8")


def _read_record() -> dict[str, Any] | None:
    try:
        data = json.loads(PID_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("pid") else None


def _clear_record() -> None:
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass


def _ps_lstart(pid: int) -> str:
    try:
        return subprocess.check_output(["ps", "-o", "lstart=", "-p", str(pid)], text=True, timeout=5).strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def _verify_record(record: dict[str, Any]) -> bool:
    """pid가 살아 있고 '우리 바이너리 serve, 같은 시작 시각, 세션 리더'인지 ps로 재확인."""
    pid = int(record["pid"])
    try:
        out = subprocess.check_output(["ps", "-o", "lstart=,pgid=,ppid=,args=", "-p", str(pid)], text=True, timeout=5).strip()
    except (subprocess.SubprocessError, OSError, ValueError):
        return False
    fields = out.split()
    if len(fields) < 8:
        return False
    lstart = " ".join(fields[:5])
    pgid, args = fields[5], " ".join(fields[7:])
    if record.get("lstart") and lstart != str(record["lstart"]).strip():
        return False
    if pgid != str(pid):
        return False
    return args.startswith(str(record.get("exe") or "")) and " serve" in args


def _owned_alive() -> bool:
    record = _read_record()
    if not record:
        return False
    if _verify_record(record):
        return True
    _clear_record()
    return False


def stop() -> bool:
    """우리가 띄운 서버만 끈다. 기록이 없거나 재검증에 실패하면 아무것도 죽이지 않는다."""
    global _proc
    with state.locked():
        record = _read_record()
        if not record or not _verify_record(record):
            _clear_record()
            return False
        pid = int(record["pid"])
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            _clear_record()
            return False
        deadline = time.time() + 8
        while time.time() < deadline and _alive(pid):
            time.sleep(0.2)
        if _alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if _proc is not None and _proc.pid == pid:
            try:
                _proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            _proc = None
        _clear_record()
    return True


def _adjust_refs(delta: int) -> int:
    """PID 기록의 활성 세션 카운터를 잠금 하에 증감한다. 기록이 없으면 0을 반환한다."""
    with state.locked():
        record = _read_record()
        if not record:
            return 0
        refs = max(0, int(record.get("refs") or 0) + delta)
        record["refs"] = refs
        try:
            PID_FILE.write_text(json.dumps(record) + "\n", encoding="utf-8")
        except OSError:
            pass
        return refs


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def unload(model: str) -> bool:
    """모델만 내린다(서버는 둔다). /api/ps에서 완전히 사라지면 True(최대 3초 대기)."""
    if not model or not running():
        return False
    try:
        _post("/api/generate", {"model": model, "keep_alive": 0}, timeout=20)
    except urllib.error.HTTPError:
        return False
    except Exception:
        return False
    deadline = time.time() + 3.0
    while True:
        if all((m.get("name") or m.get("model")) != model for m in loaded_models()):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.3)


def loaded_models(timeout: float = 3.0) -> list[dict[str, Any]]:
    """/api/ps — 지금 메모리에 올라간 모델들."""
    try:
        return list(_get("/api/ps", timeout=timeout).get("models") or [])
    except Exception:
        return []


def show_model(model: str) -> dict[str, Any]:
    """/api/show — GGUF 헤더 정보(model_info). 모델을 로드하지 않는다."""
    return _post("/api/show", {"model": model}, timeout=10)


@contextmanager
def session(model: str | None = None) -> Iterator[None]:
    """실행용 세션. 동시에 여러 잡이 세션을 열 수 있으므로 참조 카운트로 소유권을 추적하고,
    카운트가 0이 되고 로드된 모델도 없을 때만 우리가 띄운 서버를 끈다."""
    ours = start() or _owned_alive()
    if ours:
        _adjust_refs(1)
    if not wait_until_up(40):
        if ours:
            _adjust_refs(-1)
        raise RuntimeError("Ollama가 꺼져 있고 기동도 실패했습니다.")
    try:
        yield
    finally:
        unloaded = unload(model) if model else True
        if ours:
            remaining = _adjust_refs(-1)
            if remaining <= 0 and unloaded and not loaded_models():
                stop()


def ensure_background() -> None:
    """설치 단계에서만 쓴다. 레거시 LaunchAgent가 있으면 치우고 서버를 띄운다."""
    _remove_launch_agent()
    start()
    wait_until_up(20)


def _remove_launch_agent() -> None:
    if sys.platform != "darwin":
        return
    label = "local-llm-desk.ollama"
    plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    if not plist.exists():
        return
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True)
    try:
        plist.unlink()
    except FileNotFoundError:
        pass


def wait_until_up(seconds: int = 40) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if running():
            return True
        time.sleep(0.5)
    return running()


# --- 모델 목록 ---------------------------------------------------------------


def list_models() -> list[dict[str, Any]]:
    if not running():
        return []
    data = _get("/api/tags", timeout=5.0)
    return data.get("models") or []


def has_model(name: str) -> bool:
    return any((m.get("name") or m.get("model") or "") == name for m in list_models())


def default_model() -> str:
    models = list_models()
    if models:
        models = sorted(models, key=lambda m: int(m.get("size") or 0))
        name = (models[0].get("name") or models[0].get("model") or "").strip()
        if name:
            remember_models([m.get("name") or m.get("model") or "" for m in models])
            return name
    remembered = remembered_models()
    return remembered[0] if remembered else ""


def remembered_models() -> list[str]:
    try:
        from desk.state import load_config

        return [n for n in (load_config().get("models") or []) if n]
    except Exception:
        return []


def remember_models(names: list[str] | None = None) -> list[str]:
    """config.json의 models를 갱신한다. 값이 같으면 쓰지 않는다."""
    if names is None:
        names = [(m.get("name") or m.get("model") or "") for m in list_models()]
    names = [n for n in names if n]
    if not names:
        return remembered_models()
    try:
        from desk import state

        lock = getattr(state, "locked", None)
        with lock() if callable(lock) else nullcontext():
            cfg = state.load_config()
            if list(cfg.get("models") or []) != names:
                cfg["models"] = names
                state.save_config(cfg)
    except Exception:
        pass
    return names


def _should_stop() -> bool:
    try:
        from desk.installer import cancelled

        return cancelled()
    except Exception:
        return False


def _ensure_up(seconds: int) -> None:
    if running():
        return
    start()
    if not wait_until_up(seconds):
        raise RuntimeError("Ollama가 안 떠 있습니다.")


def remove_model(name: str) -> None:
    _ensure_up(20)
    body = {"name": name}
    try:
        _post("/api/delete", body, timeout=30, method="DELETE")
        return
    except urllib.error.HTTPError as exc:
        if exc.code not in (404, 405):
            raise RuntimeError(exc.read().decode("utf-8", errors="replace")[:240] or "모델 삭제 실패") from exc
    _post("/api/delete", body, timeout=30)


def pull_model(name: str) -> Iterator[str]:
    _ensure_up(40)
    body = json.dumps({"name": name, "stream": True}).encode("utf-8")
    req = urllib.request.Request(OLLAMA_HOST + "/api/pull", data=body, headers={"Content-Type": "application/json"}, method="POST")
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


# --- 채팅 -------------------------------------------------------------------

EFFORT = {
    "low": {"think": False, "num_predict": 400, "timeout": 180, "num_ctx": 4096},
    "medium": {"think": True, "num_predict": 1200, "timeout": 360, "num_ctx": 8192},
    "high": {"think": True, "num_predict": 4096, "timeout": 720, "num_ctx": 16384},
}


def effort_knobs(effort: str) -> dict[str, Any]:
    return EFFORT.get(effort) or EFFORT["medium"]


def chat(
    model: str,
    prompt: str,
    timeout: int | None = None,
    system: str | None = None,
    effort: str = "medium",
    tools: list[dict[str, Any]] | None = None,
    num_ctx: int | None = None,
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return chat_messages(model, messages, timeout=timeout, effort=effort, tools=tools, num_ctx=num_ctx)


def chat_messages(
    model: str,
    messages: list[dict[str, Any]],
    timeout: int | None = None,
    effort: str = "medium",
    tools: list[dict[str, Any]] | None = None,
    num_ctx: int | None = None,
    keep_alive: str | int = KEEP_ALIVE,
) -> dict[str, Any]:
    """/api/chat 한 번. 응답에 ``tools_dropped: True``가 있으면 모델이 도구를 거절해 도구 없이 재시도한 것."""
    knobs = effort_knobs(effort)
    timeout = timeout if timeout is not None else int(knobs["timeout"])
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": knobs["think"],
        "keep_alive": keep_alive,
        "options": {"temperature": 0.6, "repeat_penalty": 1.1, "num_predict": knobs["num_predict"], "num_ctx": int(num_ctx or knobs["num_ctx"])},
    }
    if tools:
        body["tools"] = tools
    return _chat_http(model, body, timeout)


def _chat_http(model: str, body: dict[str, Any], timeout: int) -> dict[str, Any]:
    try:
        return _post("/api/chat", body, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 404:
            raise RuntimeError(f"모델 '{model}'이 이 맥에 없습니다. 설치에서 받으세요.") from exc
        if exc.code != 400:
            raise RuntimeError(f"Ollama HTTP {exc.code}: {detail[:240]}") from exc
        # 400: think/tools를 지원하지 않는 모델 → 해당 옵션을 빼고 한 번 재시도
        retry = dict(body)
        retry.pop("think", None)
        dropped_tools = "tool" in detail.lower() and "tools" in retry
        if dropped_tools:
            retry.pop("tools", None)
        try:
            payload = _post("/api/chat", retry, timeout=timeout)
        except urllib.error.HTTPError as exc2:
            if exc2.code == 404:
                raise RuntimeError(f"모델 '{model}'이 이 맥에 없습니다. 설치에서 받으세요.") from exc2
            raise RuntimeError(f"Ollama HTTP {exc2.code}: {exc2.read().decode('utf-8', errors='replace')[:240]}") from exc2
        if dropped_tools:
            payload["tools_dropped"] = True
        return payload


def extract_text(payload: dict[str, Any]) -> str:
    msg = payload.get("message") or {}
    content = msg.get("content") or payload.get("response") or ""
    return str(content).strip()


def extract_tool_calls(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """[{"name", "arguments", "id"}] — id는 응답에 있을 때만 채운다."""
    msg = payload.get("message") or {}
    calls = msg.get("tool_calls") or payload.get("tool_calls") or []
    out: list[dict[str, Any]] = []
    for call in calls:
        fn = call.get("function") or call
        name = str(fn.get("name") or "")
        raw = fn.get("arguments") or call.get("arguments") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = {"value": raw}
        if name:
            out.append({"name": name, "arguments": raw if isinstance(raw, dict) else {}, "id": str(call.get("id") or "")})
    return out
