"""Model / provider / agent catalog for the install screens and model pickers."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from desk import ollama_ctl
from desk.hardware import detect, model_plan, org_of, role_label
from desk.paths import DATA
from desk.state import load_config

GIB = 1024**3
APPLE_SILICON = {"m1", "m2", "m3", "m4", "m5"}


def _have_app(*names: str) -> bool:
    apps = Path("/Applications")
    return any((apps / name).exists() for name in names)


def _have_module(name: str) -> bool:
    try:
        proc = subprocess.run([sys.executable, "-c", f"import {name}"], capture_output=True, timeout=4)
        return proc.returncode == 0
    except Exception:
        return False


def _status_model(have: bool, tight_ram: bool, tight_disk: bool) -> str:
    if have:
        return "받아 둠"
    if tight_disk:
        return "지금 저장이 빠듯함"
    if tight_ram:
        return "지금 램이 빠듯함"
    return "아직 안 받음"


def installed_sizes() -> dict[str, float | None]:
    """Installed model name → size in GB (exact from /api/tags; None when only remembered)."""
    live = ollama_ctl.model_inventory()
    if live is not None:
        sizes = {(m.get("name") or m.get("model") or ""): round(int(m.get("size") or 0) / GIB, 1) for m in live}
        sizes.pop("", None)
        names = sorted(sizes)
        if names != sorted(ollama_ctl.remembered_models()):
            ollama_ctl.remember_models(names)
        return sizes
    return {n: None for n in ollama_ctl.remembered_models()}


def ram_need_gb(model: str, size_gb: float | None) -> float:
    """RAM a model needs at the default context; exact via ramgate when available."""
    try:
        from desk import ramgate

        return round(float(ramgate.model_need_gb(model)), 1)
    except Exception:
        disk = float(size_gb or 0)
        return round(disk + 1.5, 1) if disk >= 4 else round(max(3.0, disk + 1.5), 1)


def _model_row(item: dict[str, Any], sizes: dict[str, float | None]) -> dict[str, Any]:
    """Catalog row; installed models use their exact tag size instead of the hand-typed table value."""
    have = item["id"] in sizes
    disk = float(sizes.get(item["id"]) or item["size_gb"])
    ram = ram_need_gb(item["id"], disk) if have else float(item.get("ram_need") or ram_need_gb(item["id"], disk))
    tight_ram = bool(item.get("tight_ram"))
    tight_disk = bool(item.get("tight_disk"))
    status = _status_model(have, tight_ram, tight_disk)
    row = {
        **item,
        "kind": "model",
        "name": item.get("name") or item["id"],
        "installed": have,
        "selected": bool(item.get("pick")),
        "disabled": False,
        "org": item.get("org") or "",
        "org_color": item.get("org_color") or "",
        "org_logo": item.get("org_logo") or "",
        "status": status,
        "size_gb": disk,
        "disk_gb": disk,
        "ram_gb": ram,
        "ram_need": ram,
    }
    size_bit = f"저장 {disk}GB · 지금 램 약 {ram}GB"
    row["line"] = " · ".join(p for p in (role_label(item.get("role") or ""), row["org"], size_bit, status) if p)
    return row


def _extra_row(name: str, size_gb: float | None, hw: dict[str, Any]) -> dict[str, Any]:
    """Installed model outside hardware.ALL_MODELS: same size/RAM fields from the exact tag size."""
    org, color, logo = org_of(name)
    ram = ram_need_gb(name, size_gb)
    free = float(hw.get("ram_free_gb") or 0)
    size_bit = f"저장 {size_gb}GB · 지금 램 약 {ram}GB" if size_gb else f"지금 램 약 {ram}GB"
    return {
        "id": name,
        "kind": "model",
        "name": name,
        "installed": True,
        "selected": False,
        "disabled": False,
        "status": "받아 둠",
        "org": org,
        "org_color": color,
        "org_logo": logo,
        "size_gb": size_gb,
        "disk_gb": size_gb,
        "ram_gb": ram,
        "ram_need": ram,
        "tight_ram": bool(free) and ram > free,
        "tight_disk": False,
        "line": f"{size_bit} · 이미 받아 둠",
    }


def _providers(hw: dict[str, Any]) -> list[dict[str, Any]]:
    ollama_have = ollama_ctl.app_installed() or bool(ollama_ctl.binary())
    llama_have = bool(shutil.which("llama-cli") or shutil.which("llama-server") or shutil.which("llama-cpp"))
    mlx_have = _have_module("mlx")
    silicon = hw.get("chip_gen") in APPLE_SILICON

    def pst(have: bool) -> str:
        return "설치됨 · 꺼짐" if have else "아직 안 깔림"

    return [
        {"id": "ollama", "kind": "provider", "name": "Ollama", "line": "모델을 돌리는 엔진", "installed": ollama_have, "selected": True, "disabled": False},
        {"id": "llamacpp", "kind": "provider", "name": "llama.cpp", "line": f"옵션. 예약에는 안 씀 · {pst(llama_have)}", "installed": llama_have, "selected": False, "disabled": False},
        {
            "id": "mlx",
            "kind": "provider",
            "name": "MLX",
            "line": f"옵션. Apple Silicon · {pst(mlx_have)}" if silicon else "옵션. Apple Silicon 전용",
            "installed": mlx_have,
            "selected": False,
            "disabled": not silicon,
        },
    ]


def _agents() -> list[dict[str, Any]]:
    agents = [
        ("opencode", "OpenCode", "MCP·CLI·하네스", _have_app("OpenCode.app") or bool(shutil.which("opencode"))),
        ("aider", "Aider", "반복 스킬·코드 도구", bool(shutil.which("aider"))),
    ]
    return [
        {
            "id": aid,
            "kind": "agent",
            "name": name,
            "line": f"{blurb} · {'설치됨' if have else '아직 안 깔림'}",
            "installed": have,
            "selected": False,
            "disabled": False,
            "auto": True,
        }
        for aid, name, blurb, have in agents
    ]


def build() -> dict[str, Any]:
    hw = detect()
    plan = model_plan(hw)
    sizes = installed_sizes()
    models = [_model_row(item, sizes) for item in plan["models"]]
    known = {m["id"] for m in models}
    models.extend(_extra_row(name, sizes[name], hw) for name in sorted(sizes) if name and name not in known)
    cfg = load_config()
    return {
        "hardware": hw,
        "plan": plan,
        "providers": _providers(hw),
        "models": models,
        "agents": _agents(),
        "legend": "숫자는 이 모델이 쓰는 디스크·램입니다. 빠듯함은 지금 남은 용량 기준입니다.",
        "ollama": {
            "running": ollama_ctl.running(),
            "binary": ollama_ctl.binary(),
            "app": ollama_ctl.app_installed(),
        },
        "setup_done": bool(cfg.get("setup_done")),
        "data_dir": str(DATA),
    }
