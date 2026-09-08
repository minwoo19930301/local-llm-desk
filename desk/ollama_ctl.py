"""Ollama 서버 제어와 /api/chat 호출.

수명 규칙(research_ollamaRam.md §5): **우리가 start()로 띄운 서버만 끈다.**
사용자의 Ollama 앱 등 이미 떠 있던 서버는 건드리지 않고, 우리가 쓴 모델만 keep_alive:0 으로 언로드한다.
"""
from __future__ import annotations

import json
import os
import queue
import socket
import threading
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Callable, Iterator

from desk import state
from desk.paths import LOGS_DIR, PID_DIR

OLLAMA_HOST = "http://127.0.0.1:11434"
PID_FILE = PID_DIR / "ollama.json"
SESSIONS_FILE = PID_DIR / "ollama-sessions.json"
SERVE_LOG = LOGS_DIR / "ollama-serve.log"
KEEP_ALIVE = "2m"
SERVE_ENV = {
    "OLLAMA_HOST": "127.0.0.1:11434",
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
        return str(Path(found).resolve())
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
                return str(path.resolve())
        except OSError:
            continue
    return None


# --- 서버 기동/종료 (소유권 추적) ------------------------------------------------


def start() -> bool:
    """헤드리스 서버를 띄운다. 이미 떠 있으면 False(남의 서버), 우리가 띄웠으면 True."""
    global _proc
    with state.locked():
        # Another process may have spawned the server before its HTTP socket is ready.
        if _owned_alive() or running():
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
        try:
            _write_record(proc.pid, exe)
        except BaseException:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            _proc = None
            raise
    return True


def _write_record(pid: int, exe: str, refs: int = 0) -> None:
    PID_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _ps_lstart(pid)
    if not stamp:
        raise RuntimeError("Ollama 프로세스 소유권을 확인하지 못했습니다.")
    record = {"pid": pid, "lstart": stamp, "exe": exe, "started_at": time.time(), "refs": refs}
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
        return " ".join(subprocess.check_output(["ps", "-o", "lstart=", "-p", str(pid)], text=True, timeout=5).split())
    except (subprocess.SubprocessError, OSError):
        return ""


def _verify_record(record: dict[str, Any]) -> bool:
    """pid가 살아 있고 '우리 바이너리 serve, 같은 시작 시각, 세션 리더'인지 ps로 재확인."""
    try:
        pid = int(record["pid"])
        if pid <= 1:
            return False
        out = subprocess.check_output(["ps", "-o", "lstart=,pgid=,ppid=,args=", "-p", str(pid)], text=True, timeout=5).strip()
    except (subprocess.SubprocessError, OSError, ValueError, TypeError, KeyError):
        return False
    # Keep the command intact: application paths can contain spaces.
    fields = out.split(maxsplit=7)
    if len(fields) != 8:
        return False
    lstart = " ".join(fields[:5])
    stamp = " ".join(str(record.get("lstart") or "").split())
    exe = str(record.get("exe") or "")
    return bool(stamp and exe) and lstart == stamp and fields[5] == str(pid) and fields[7] == exe + " serve"


def _owned_alive() -> bool:
    record = _read_record()
    if not record:
        return False
    if _verify_record(record):
        return True
    _clear_record()
    return False


def stop() -> bool:
    """Stop only a verified Desk server with no active sessions."""
    with state.locked():
        if _live_sessions():
            return False
        return _stop_owned()


def _stop_owned() -> bool:
    """Caller holds state.locked() and has already established there are no users."""
    global _proc
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

def _live_sessions() -> dict[str, Any]:
    """Read leases under state.locked(); discard clients that exited or reused a PID."""
    leases = state.read_json(SESSIONS_FILE, {})
    if not isinstance(leases, dict):
        return {}
    live = {}
    server = _server_key()
    for token, lease in leases.items():
        try:
            pid = int(lease["pid"])
            stamp = str(lease["lstart"])
        except (KeyError, TypeError, ValueError):
            continue
        if lease.get("server") and lease["server"] != server:
            continue
        if stamp and _ps_lstart(pid) == stamp:
            live[token] = lease
    return live


def _server_key() -> str:
    record = _read_record()
    if not record:
        return ""
    return f"{record['pid']}:{record.get('lstart')}:{record.get('started_at')}"


def _save_sessions(leases: dict[str, Any]) -> None:
    PID_DIR.mkdir(parents=True, exist_ok=True)
    state.write_json(SESSIONS_FILE, leases)
    record = _read_record()
    if record:
        record["refs"] = len(leases)
        state.write_json(PID_FILE, record)


def _model_key(model: str | None) -> str:
    name = model or ""
    return name if not name or ":" in name.rsplit("/", 1)[-1] else name + ":latest"


def _alive(pid: int) -> bool:
    if _proc is not None and _proc.pid == pid and _proc.poll() is not None:
        return False
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
def session(model: str | None = None, *, cancel: Callable[[], bool] | None = None) -> Iterator[None]:
    """Acquire a cross-process lease, then release/unload only after the last user.

    Startup and lease acquisition share the same lock as shutdown. HTTP readiness
    is awaited outside it, so concurrent jobs can share a starting server.
    """
    token = uuid.uuid4().hex
    ours = False
    with state.locked():
        start()
        ours = _owned_alive()
        leases = _live_sessions()
        stamp = _ps_lstart(os.getpid())
        if not stamp:
            if ours and not leases:
                stop()
            raise RuntimeError("실행 세션의 프로세스를 확인하지 못했습니다.")
        leases[token] = {"pid": os.getpid(), "lstart": stamp, "model": _model_key(model), "server": _server_key() if ours else ""}
        try:
            _save_sessions(leases)
        except BaseException:
            leases.pop(token, None)
            try:
                _save_sessions(leases)
            finally:
                if ours and not leases:
                    _stop_owned()
            raise
    try:
        if not wait_until_up(40, cancel=cancel):
            raise RuntimeError("Ollama가 꺼져 있고 기동도 실패했습니다.")
        if cancel and cancel():
            raise RuntimeError("취소했습니다.")
        yield
    finally:
        with state.locked():
            leases = _live_sessions()
            leases.pop(token, None)
            try:
                _save_sessions(leases)
            finally:
                # A full disk must not prevent process cleanup after a pull.
                # We hold the same lock as acquisition, so this lease snapshot
                # remains authoritative even if persisting its removal failed.
                if ours and not leases:
                    _stop_owned()
                elif model and not any(not lease.get("model") or lease["model"] == _model_key(model) for lease in leases.values()):
                    unload(model)


def ensure_background() -> None:
    """Compatibility helper: remove Desk's obsolete auto-start agent only."""
    _remove_launch_agent()


def _remove_launch_agent() -> None:
    if sys.platform != "darwin":
        return
    label = "local-llm-desk.ollama"
    plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    if not plist.exists():
        return
    target = f"gui/{os.getuid()}/{label}"
    loaded = subprocess.run(["launchctl", "print", target], capture_output=True, timeout=10)
    if loaded.returncode == 0:
        removed = subprocess.run(["launchctl", "bootout", target], capture_output=True, timeout=10)
        if removed.returncode != 0:
            raise RuntimeError("기존 Desk Ollama 자동 실행을 해제하지 못했습니다.")
    # Keep a reviewable backup outside LaunchAgents before removing its login trigger.
    backups = PID_DIR.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    shutil.copy2(plist, backups / f"{label}.{time.time_ns()}.plist")
    plist.unlink()


def wait_until_up(seconds: int = 40, *, cancel: Callable[[], bool] | None = None) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if cancel and cancel():
            raise RuntimeError("취소했습니다.")
        if running():
            return True
        time.sleep(0.5)
    return running()


# --- 모델 목록 ---------------------------------------------------------------


def model_inventory() -> list[dict[str, Any]] | None:
    """Authoritative tags, including an empty list; None means unavailable. Never starts Ollama."""
    try:
        return list(_get("/api/tags", timeout=5.0).get("models") or [])
    except Exception:
        return None


def list_models() -> list[dict[str, Any]]:
    return model_inventory() or []


def has_model(name: str) -> bool:
    return any((m.get("name") or m.get("model") or "") == name for m in list_models())


def default_model() -> str:
    models = model_inventory()
    if models == []:
        remember_models([])
        return ""
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
        live = model_inventory()
        if live is None:
            return remembered_models()
        names = [(m.get("name") or m.get("model") or "") for m in live]
    names = [n for n in names if n]
    try:
        from desk import state

        lock = getattr(state, "locked", None)
        with lock() if callable(lock) else nullcontext():
            cfg = state.load_config()
            if list(cfg.get("models") or []) != names:
                cfg["models"] = names
                if not names:
                    cfg["setup_done"] = False
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
    raise RuntimeError("Ollama 실행 세션이 없습니다.")


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


def _pull_lines(req: urllib.request.Request) -> Iterator[bytes]:
    """Read downloads off the request thread so cancellation can close their socket."""
    events: queue.Queue = queue.Queue(maxsize=32)
    done = threading.Event()
    holder: list[Any] = []

    def publish(value: Any) -> None:
        while not done.is_set():
            try:
                events.put(value, timeout=0.2)
                return
            except queue.Full:
                pass

    def read() -> None:
        try:
            # Bound connect/header waits too; body reads can be long and are
            # interrupted through the response socket by the session owner.
            with urllib.request.urlopen(req, timeout=30) as resp:
                holder.append(resp)
                while not done.is_set():
                    raw = resp.readline()
                    if not raw:
                        break
                    publish(raw)
        except Exception as exc:
            publish(exc)
        finally:
            publish(None)

    worker = threading.Thread(target=read, name="ollama-pull", daemon=True)
    worker.start()
    try:
        while True:
            if _should_stop():
                raise RuntimeError("취소했습니다.")
            try:
                value = events.get(timeout=0.2)
            except queue.Empty:
                continue
            if value is None:
                break
            if isinstance(value, Exception):
                raise value
            yield value
    finally:
        done.set()
        if holder:
            # urllib returns HTTPResponse; shutdown interrupts a blocked read
            # without waiting on the buffered reader's close lock.
            raw = getattr(getattr(holder[0], "fp", None), "raw", None)
            sock = getattr(raw, "_sock", None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        worker.join(timeout=1)


def pull_model(name: str) -> Iterator[str]:
    _ensure_up(40)
    body = json.dumps({"name": name, "stream": True}).encode("utf-8")
    req = urllib.request.Request(OLLAMA_HOST + "/api/pull", data=body, headers={"Content-Type": "application/json"}, method="POST")
    success = False
    for raw in _pull_lines(req):
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            continue
        err = data.get("error")
        if err:
            raise RuntimeError(str(err))
        status = str(data.get("status") or "")
        success = success or status == "success"
        total = data.get("total") or 0
        completed = data.get("completed") or 0
        if total:
            pct = min(100, int(100 * completed / total))
            yield f"{status} {pct}%".strip()
        elif status:
            yield status
    if not success:
        raise RuntimeError("모델 다운로드가 완료되기 전에 연결이 끊겼습니다.")


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
