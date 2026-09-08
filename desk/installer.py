from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any

_cancel = threading.Event()


def request_cancel() -> None:
    _cancel.set()


def cancelled() -> bool:
    return _cancel.is_set()


def _check() -> None:
    if _cancel.is_set():
        raise RuntimeError("취소했습니다.")

from desk import ollama_ctl
from desk.hardware import detect, model_plan
from desk.paths import DATA, LOGS_DIR, PID_DIR, ensure_dirs
from desk.state import load_config, save_config


def allowed_models() -> set[str]:
    plan = model_plan(detect())
    ids = {m["id"] for m in plan["models"] if not m.get("skip")}
    ids.update(m.get("name") or m.get("model") or "" for m in ollama_ctl.list_models())
    ids.discard("")
    return ids


def setup(body: dict[str, Any]) -> Iterator[dict[str, Any]]:
    _cancel.clear()
    ensure_dirs()
    stage = str(body.get("stage") or "providers")
    try:
        if stage == "models":
            yield from _setup_models(body)
            return
        yield from _setup_providers(body)
    except Exception as exc:
        _cleanup_tmp()
        msg = str(exc)
        if cancelled() or "취소" in msg:
            yield {"event": "done", "ok": False, "cancelled": True, "error": "취소했습니다."}
            return
        yield {"event": "done", "ok": False, "ready": False, "error": msg}


def _provider_ready() -> bool:
    # Installation does not require a running background server.
    return bool(ollama_ctl.binary())


def _setup_providers(body: dict[str, Any]) -> Iterator[dict[str, Any]]:
    providers = list(body.get("providers") or ["ollama"])
    if not providers:
        yield {"event": "done", "ok": False, "error": "프로바이더를 고르세요."}
        return
    warnings: list[str] = []
    if "ollama" in providers:
        _check()
        # Migrate only Desk's obsolete login agent. This never starts Ollama.
        ollama_ctl.ensure_background()
        yield _log("Ollama", progress=4, stage="Ollama")
        for ev in _ensure_ollama():
            _check()
            yield ev
            if ev.get("event") == "error":
                _cleanup_tmp()
                yield {"event": "done", "ok": False, "error": ev.get("line") or "Ollama 설치 실패"}
                return
        if not _provider_ready():
            yield {"event": "done", "ok": False, "error": "Ollama 실행 파일을 확인하지 못했습니다. 다시 설치하세요."}
            return
        yield _log("Ollama 준비됨", progress=50, stage="Ollama")
    if "llamacpp" in providers:
        _check()
        yield _log("llama.cpp", progress=60, stage="llama.cpp")
        failed = False
        for ev in _install_llamacpp():
            yield ev
            if ev.get("event") == "error":
                warnings.append(ev.get("line") or "llama.cpp 실패")
                failed = True
                break
        if not failed:
            yield _log("llama.cpp 완료", progress=80, stage="llama.cpp")
    if "mlx" in providers:
        _check()
        yield _log("MLX", progress=85, stage="MLX")
        failed = False
        for ev in _install_mlx():
            yield ev
            if ev.get("event") == "error":
                warnings.append(ev.get("line") or "MLX 실패")
                failed = True
                break
        if not failed:
            yield _log("MLX 완료", progress=95, stage="MLX")
    ready = _provider_ready() if "ollama" in providers else True
    if "ollama" in providers and not ready:
        yield {"event": "done", "ok": False, "error": "Ollama 실행 파일이 없습니다. 다시 설치하세요."}
        return
    yield _log("완료", progress=100, stage="Ollama")
    yield {
        "event": "done",
        "ok": True,
        "provider_ready": ready,
        "ready": False,
        "warning": " · ".join(warnings),
    }


def _setup_models(body: dict[str, Any]) -> Iterator[dict[str, Any]]:
    if not _provider_ready():
        yield {"event": "done", "ok": False, "error": "먼저 프로바이더를 받으세요."}
        return
    with ollama_ctl.session(cancel=cancelled):
        allowed = allowed_models()
        models = [n for n in (body.get("models") or []) if n in allowed]
        agents = list(body.get("agents") or [])
        if not models and not agents:
            yield {"event": "done", "ok": False, "error": "모델을 하나 고르세요."}
            return
        n = max(1, len(models))
        for i, name in enumerate(models):
            _check()
            base = int(90 * i / n)
            if ollama_ctl.has_model(name):
                yield _log(f"{name} 있음", progress=int(90 * (i + 1) / n), stage=name)
                continue
            yield _log(f"{name} 받는 중", progress=base, stage=name)
            for line in ollama_ctl.pull_model(name):
                _check()
                pct = _pct(line)
                mapped = base + int((90 / n) * (pct / 100)) if pct is not None else None
                ev = _from_pipe(line, name, mapped)
                if ev:
                    yield ev
            yield _log(f"{name} 완료", progress=int(90 * (i + 1) / n), stage=name)
        for agent in agents:
            _check()
            if agent == "desk":
                continue
            if agent == "open-webui":
                for ev in _install_open_webui():
                    yield ev
                    if ev.get("event") == "error":
                        yield {"event": "done", "ok": False, "error": ev.get("line") or "에이전트 실패"}
                        return
            elif agent == "aider":
                for ev in _install_aider():
                    yield ev
                    if ev.get("event") == "error":
                        yield {"event": "done", "ok": False, "error": ev.get("line") or "에이전트 실패"}
                        return
            elif agent == "opencode":
                for ev in _install_opencode():
                    yield ev
                    if ev.get("event") == "error":
                        yield {"event": "done", "ok": False, "error": ev.get("line") or "에이전트 실패"}
                        return
            else:
                yield _log(f"이 화면에서 설치 불가: {agent}")
        have_models = bool(ollama_ctl.list_models())
        ollama_ctl.remember_models()
        from desk.state import locked
        with locked():
            cfg = load_config()
            cfg["setup_done"] = have_models
            save_config(cfg)
    yield _log("완료", progress=100)
    yield {"event": "done", "ok": True, "ready": have_models}


def _cleanup_tmp() -> None:
    # Each installation owns a TemporaryDirectory; never detach/delete a
    # predictable shared /tmp path that might belong to another process.
    pass


def remove(names: list[str]) -> Iterator[dict[str, Any]]:
    _cancel.clear()  # 이전 설치 취소 플래그가 삭제 스트림을 끊지 않게
    ensure_dirs()
    names = [n for n in names if n]
    if not names:
        yield _log("지울 모델을 고르세요.")
        yield {"event": "done", "ok": False}
        return
    try:
        with ollama_ctl.session(cancel=cancelled):
            for name in names:
                _check()
                if not ollama_ctl.has_model(name):
                    yield _log(f"{name} 없음")
                    continue
                ollama_ctl.remove_model(name)
                yield _log(f"{name} 지움")
            ollama_ctl.remember_models()
        yield {"event": "done", "ok": True}
    except Exception as exc:
        yield _log(str(exc))
        yield {"event": "done", "ok": False, "error": str(exc)}


def _install_llamacpp() -> Iterator[dict[str, Any]]:
    if shutil.which("llama-server") or shutil.which("llama-cli"):
        yield _log("llama.cpp 있음")
        return
    brew = shutil.which("brew")
    if brew:
        yield _log("llama.cpp 설치")
        proc = subprocess.Popen(
            [brew, "install", "llama.cpp"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            ev = _from_pipe(line.rstrip(), "llama.cpp")
            if ev:
                yield ev
        if proc.wait() != 0:
            yield {"event": "error", "line": "llama.cpp 설치 실패"}
            return
        yield _log("llama.cpp 완료")
        return
    yield _log("llama.cpp 공식 바이너리 받는 중. Homebrew는 쓰지 않습니다.")
    dest = DATA / "bin"
    dest.mkdir(parents=True, exist_ok=True)
    try:
        import json
        import urllib.request
        import zipfile

        req = urllib.request.Request(
            "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest",
            headers={"User-Agent": "local-llm-desk"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            rel = json.loads(resp.read().decode("utf-8"))
        asset = next(
            (
                a
                for a in (rel.get("assets") or [])
                if "macos" in a["name"].lower()
                and ("arm64" in a["name"].lower() or "apple" in a["name"].lower())
                and a["name"].endswith(".zip")
            ),
            None,
        )
        if not asset:
            yield {"event": "error", "line": "llama.cpp macOS 파일을 찾지 못했습니다."}
            return
        zip_path = "/tmp/llama.cpp.zip"
        urllib.request.urlretrieve(asset["browser_download_url"], zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(dest)
        for path in dest.rglob("*"):
            if path.is_file() and path.name.startswith("llama"):
                path.chmod(path.stat().st_mode | 0o111)
        yield _log("llama.cpp 완료")
    except Exception as exc:
        yield {"event": "error", "line": f"llama.cpp 설치 실패: {exc}"}


def _install_mlx() -> Iterator[dict[str, Any]]:
    proc = subprocess.run(
        [sys.executable, "-c", "import mlx"],
        capture_output=True,
        timeout=8,
    )
    if proc.returncode == 0:
        yield _log("MLX 있음")
        return
    yield _log("MLX 설치. Homebrew는 쓰지 않습니다.")
    inst = subprocess.Popen(
        [sys.executable, "-m", "pip", "install", "--user", "mlx", "mlx-lm"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert inst.stdout is not None
    for line in inst.stdout:
        ev = _from_pipe(line.rstrip(), "MLX")
        if ev:
            yield ev
    if inst.wait() != 0:
        yield {"event": "error", "line": "MLX 설치 실패"}
        return
    yield _log("MLX 완료")


def _ensure_ollama() -> Iterator[dict[str, Any]]:
    if ollama_ctl.binary():
        yield _log("Ollama 설치됨 · 실행할 때 켜집니다", progress=18, stage="Ollama")
        return
    if sys.platform == "darwin":
        for ev in _install_ollama_app():
            _check()
            yield ev
            if ev.get("event") == "error":
                return
    elif sys.platform == "win32":
        for ev in _install_ollama_windows():
            _check()
            yield ev
            if ev.get("event") == "error":
                return
    else:
        yield {"event": "error", "line": "이 OS의 Ollama 앱 설치는 아직 없습니다. ollama.com에서 받아 주세요."}
        return
    if not ollama_ctl.binary():
        yield {"event": "error", "line": "Ollama 실행 파일을 확인하지 못했습니다."}
        return
    yield _log("Ollama 설치됨 · 실행할 때 켜집니다", progress=18, stage="Ollama")


def _download_file(url: str, dest: str, stage: str) -> Iterator[dict[str, Any]]:
    import urllib.error
    import urllib.request

    yield _log(f"{stage} 받는 중", progress=1, stage=stage)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Free-AI-Scheduler"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            while True:
                _check()
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
                if total:
                    yield {"event": "log", "progress": max(1, min(99, int(got * 100 / total))), "stage": stage}
    except Exception:
        try:
            if os.path.isfile(dest):
                os.remove(dest)
        except OSError:
            pass
        raise


def _install_ollama_app() -> Iterator[dict[str, Any]]:
    from pathlib import Path

    import tempfile

    with tempfile.TemporaryDirectory(prefix="local-llm-desk-ollama-") as tmp:
        dmg = str(Path(tmp) / "Ollama.dmg")
        mnt = str(Path(tmp) / "mount")
        for ev in _download_file("https://ollama.com/download/Ollama.dmg", dmg, "Ollama"):
            yield ev
        _check()
        yield _log("Ollama 앱 설치", progress=70, stage="Ollama")
        attach = subprocess.run(
            ["hdiutil", "attach", dmg, "-nobrowse", "-mountpoint", mnt],
            capture_output=True,
            text=True,
        )
        if attach.returncode != 0:
            yield {"event": "error", "line": "Ollama 디스크 이미지를 열지 못했습니다."}
            return
        try:
            src = Path(mnt) / "Ollama.app"
            if not src.exists():
                yield {"event": "error", "line": "이미지에 Ollama.app이 없습니다."}
                return
            dest = Path("/Applications/Ollama.app")
            copy = subprocess.run(["ditto", str(src), str(dest)], capture_output=True, text=True)
            if copy.returncode != 0:
                dest = Path.home() / "Applications" / "Ollama.app"
                dest.parent.mkdir(parents=True, exist_ok=True)
                copy = subprocess.run(["ditto", str(src), str(dest)], capture_output=True, text=True)
                if copy.returncode != 0:
                    yield {"event": "error", "line": copy.stderr.strip() or "Ollama.app을 복사하지 못했습니다."}
                    return
            yield _log("Ollama 앱 설치됨", progress=90, stage="Ollama")
        finally:
            subprocess.run(["hdiutil", "detach", mnt, "-quiet"], capture_output=True)


def _install_ollama_windows() -> Iterator[dict[str, Any]]:
    setup = os.path.join(os.environ.get("TEMP") or "/tmp", "OllamaSetup.exe")
    for ev in _download_file("https://ollama.com/download/OllamaSetup.exe", setup, "Ollama"):
        yield ev
    _check()
    yield _log("Ollama 설치 프로그램 실행", progress=80, stage="Ollama")
    proc = subprocess.run([setup, "/VERYSILENT", "/NORESTART"], capture_output=True, text=True)
    if proc.returncode != 0:
        yield {"event": "error", "line": proc.stderr.strip() or "Ollama 설치 프로그램이 실패했습니다."}


def _install_open_webui() -> Iterator[dict[str, Any]]:
    uv = shutil.which("uv")
    if not uv:
        yield {"event": "error", "line": "uv 없음"}
        return
    yield _log("Open WebUI (Python 3.11)")
    proc = subprocess.run([uv, "python", "install", "3.11"], capture_output=True, text=True)
    if proc.returncode != 0:
        yield {"event": "error", "line": proc.stderr.strip() or "python 3.11 실패"}
        return
    data_dir = os.path.expanduser("~/.open-webui")
    os.makedirs(data_dir, exist_ok=True)
    log_path = LOGS_DIR / "open-webui.log"
    env = os.environ.copy()
    env["DATA_DIR"] = data_dir
    env["WEBUI_AUTH"] = "False"
    env["OLLAMA_BASE_URL"] = "http://127.0.0.1:11434"
    cmd = [uv, "x", "--python", "3.11", "open-webui@latest", "serve", "--port", "3000", "--host", "127.0.0.1"]
    with log_path.open("ab") as logf:
        proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, env=env, cwd=str(DATA), start_new_session=True)
    PID_DIR.mkdir(parents=True, exist_ok=True)
    (PID_DIR / "open-webui.pid").write_text(str(proc.pid), encoding="utf-8")
    deadline = time.time() + 420
    while time.time() < deadline:
        if proc.poll() is not None:
            yield {"event": "error", "line": "Open WebUI 종료"}
            return
        if _port_up(3000):
            (DATA / "open-webui.installed").write_text("1\n", encoding="utf-8")
            yield _log("Open WebUI http://127.0.0.1:3000")
            yield {"event": "open-url", "url": "http://127.0.0.1:3000"}
            return
        time.sleep(2)
    yield {"event": "error", "line": "Open WebUI 시간 초과"}


def _install_aider() -> Iterator[dict[str, Any]]:
    if shutil.which("aider"):
        yield _log("Aider 있음")
        return
    uv = shutil.which("uv")
    if not uv:
        yield {"event": "error", "line": "이 에이전트는 uv가 필요합니다. Homebrew 없이도 https://docs.astral.sh/uv 에서 받을 수 있습니다."}
        return
    yield _log("Aider 설치")
    proc = subprocess.Popen(
        [uv, "tool", "install", "aider-chat"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        yield _log(line.rstrip())
    if proc.wait() != 0:
        yield {"event": "error", "line": "Aider 설치 실패"}
        return
    yield _log("Aider 완료")


def _install_opencode() -> Iterator[dict[str, Any]]:
    from pathlib import Path

    if Path("/Applications/OpenCode.app").exists() or shutil.which("opencode"):
        yield _log("OpenCode 있음")
        return
    yield _log("OpenCode 설치")
    proc = subprocess.Popen(
        ["/bin/zsh", "-lc", "curl -fsSL https://opencode.ai/install | bash"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        yield _log(line.rstrip())
    if proc.wait() != 0:
        yield {"event": "error", "line": "OpenCode 설치 실패"}
        return
    yield _log("OpenCode 완료")


def _port_up(port: int) -> bool:
    import urllib.request

    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}", timeout=1.2)
        return True
    except Exception:
        return False


def _pct(line: str) -> int | None:
    found = re.search(r"(\d+(?:\.\d+)?)\s*%", line or "")
    if not found:
        return None
    return max(0, min(100, int(float(found.group(1)))))


def _is_noise(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return True
    if s.count("#") >= 2 or "#=#" in s:
        return True
    compact = re.sub(r"\s+", "", s)
    if re.fullmatch(r"[#=.\-]*\d+(?:\.\d+)?%", compact):
        return True
    return False


def _from_pipe(line: str, stage: str, progress: int | None = None) -> dict[str, Any] | None:
    pct = _pct(line)
    if _is_noise(line):
        if pct is None:
            return None
        return {"event": "log", "progress": progress if progress is not None else pct, "stage": stage}
    if pct is not None:
        return {"event": "log", "progress": progress if progress is not None else pct, "stage": stage}
    return _log(line, progress=progress, stage=stage)


def _log(line: str, progress: int | None = None, stage: str | None = None) -> dict[str, Any]:
    ev: dict[str, Any] = {"event": "log", "line": line}
    if progress is not None:
        ev["progress"] = progress
    if stage:
        ev["stage"] = stage
    return ev
