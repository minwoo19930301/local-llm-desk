from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from desk import ollama_ctl
from desk.hardware import detect, model_plan
from desk.paths import DATA, PID_DIR
from desk.state import load_config


def _pid_alive(name: str) -> bool:
    pid_file = PID_DIR / f"{name}.pid"
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text().strip())
    except ValueError:
        return False
    import os

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _port_up(port: int) -> bool:
    import urllib.request

    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}", timeout=1.0)
        return True
    except Exception:
        return False


def _have_app(*names: str) -> bool:
    apps = Path("/Applications")
    return any((apps / name).exists() for name in names)


def _have_module(name: str) -> bool:
    import subprocess
    import sys

    try:
        proc = subprocess.run(
            [sys.executable, "-c", f"import {name}"],
            capture_output=True,
            timeout=4,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _status_model(have: bool, tight: bool) -> str:
    if have:
        return "받아 둠"
    if tight:
        return "램이 빠듯함"
    return "아직 안 받음"


def build() -> dict[str, Any]:
    hw = detect()
    plan = model_plan(hw)
    installed = {(m.get("name") or m.get("model") or "") for m in ollama_ctl.list_models()}
    models = []
    for item in plan["models"]:
        row = dict(item)
        have = item["id"] in installed
        row["kind"] = "model"
        row["name"] = item.get("name") or item["id"]
        row["installed"] = have
        row["selected"] = bool(item.get("pick")) and not have
        row["disabled"] = False
        row["status"] = _status_model(have, bool(item.get("tight")))
        disk = float(item["size_gb"])
        ram = round(disk + 1.5, 1) if disk >= 4 else round(max(3.0, disk + 1.5), 1)
        row["disk_gb"] = disk
        row["ram_gb"] = ram
        size_bit = f"저장 {disk}GB · 램 약 {ram}GB"
        row["line"] = " · ".join([p for p in (item.get("role") or "", size_bit, row["status"]) if p])
        models.append(row)

    extras = sorted(n for n in installed if n and n not in {m["id"] for m in models})
    for name in extras:
        models.append(
            {
                "id": name,
                "kind": "model",
                "name": name,
                "installed": True,
                "selected": False,
                "disabled": False,
                "status": "받아 둠",
                "line": f"이미 받아 둠",
            }
        )

    ollama_up = ollama_ctl.running()
    ollama_have = ollama_ctl.app_installed() or bool(ollama_ctl.binary())
    llama_have = bool(shutil.which("llama-cli") or shutil.which("llama-server") or shutil.which("llama-cpp"))
    mlx_have = _have_module("mlx")

    def pst(have: bool, running: bool | None = None) -> str:
        if running:
            return "지금 켜져 있음"
        if have:
            return "설치됨 · 꺼짐"
        return "아직 안 깔림"

    providers = [
        {
            "id": "ollama",
            "kind": "provider",
            "name": "Ollama",
            "line": f"CLI + 백그라운드 서버. 예약은 이걸 씀 · {pst(ollama_have, ollama_up)}",
            "installed": ollama_have,
            "selected": True,
            "disabled": False,
        },
        {
            "id": "llamacpp",
            "kind": "provider",
            "name": "llama.cpp",
            "line": f"옵션. 예약에는 안 씀 · {pst(llama_have)}",
            "installed": llama_have,
            "selected": False,
            "disabled": False,
        },
        {
            "id": "mlx",
            "kind": "provider",
            "name": "MLX",
            "line": f"옵션. Apple Silicon · {pst(mlx_have)}" if hw.get("chip_gen") in {"m1", "m2", "m3", "m4", "m5"} else "옵션. Apple Silicon 전용",
            "installed": mlx_have,
            "selected": False,
            "disabled": hw.get("chip_gen") not in {"m1", "m2", "m3", "m4", "m5"},
        },
    ]

    opencode = _have_app("OpenCode.app") or bool(shutil.which("opencode"))
    aider = bool(shutil.which("aider"))

    def ast(have: bool, running: bool = False) -> str:
        if running:
            return "지금 켜져 있음"
        if have:
            return "설치됨"
        return "아직 안 깔림"

    agents = [
        {"id": "opencode", "name": "OpenCode", "blurb": "MCP·CLI·하네스", "have": opencode, "auto": True},
        {"id": "aider", "name": "Aider", "blurb": "반복 스킬·코드 도구", "have": aider, "auto": True},
    ]
    agent_rows = []
    for a in agents:
        have = bool(a.get("have"))
        row = {
            "id": a["id"],
            "kind": "agent",
            "name": a["name"],
            "line": f"{a['blurb']} · {ast(have, bool(a.get('run')))}",
            "installed": have,
            "selected": False,
            "disabled": False,
            "auto": True,
        }
        agent_rows.append(row)

    cfg = load_config()
    return {
        "hardware": hw,
        "plan": plan,
        "providers": providers,
        "models": models,
        "agents": agent_rows,
        "legend": "저장 = 디스크에 남는 용량. 램 = 실행할 때 이 맥이 쓰는 메모리.",
        "ollama": {
            "running": ollama_up,
            "binary": ollama_ctl.binary(),
            "app": ollama_ctl.app_installed(),
        },
        "setup_done": bool(cfg.get("setup_done")),
        "data_dir": str(DATA),
    }
