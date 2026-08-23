from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from desk.paths import CONFIG_FILE, JOBS_FILE, ensure_dirs


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def write_json(path: Path, payload: Any) -> None:
    ensure_dirs()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_config() -> dict[str, Any]:
    ensure_dirs()
    cfg = read_json(CONFIG_FILE, {})
    cfg.setdefault("alerts", {"macos": True, "webhook": ""})
    cfg.setdefault("setup_done", False)
    cfg.setdefault("installed", [])
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    write_json(CONFIG_FILE, cfg)


def load_jobs() -> dict[str, Any]:
    ensure_dirs()
    data = read_json(JOBS_FILE, {"jobs": []})
    if "jobs" not in data or not isinstance(data["jobs"], list):
        data = {"jobs": []}
    return data


def save_jobs(data: dict[str, Any]) -> None:
    write_json(JOBS_FILE, data)
