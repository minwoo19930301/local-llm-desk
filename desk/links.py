"""Legacy generic-tool switches (CLI / HTTP / Chrome) stored in config.json.

MCP servers are no longer handled here; they live in the connectors registry
(``desk/connectors.py``).
"""

from __future__ import annotations

from typing import Any

from desk import ollama_ctl, tools
from desk.state import load_config, locked, save_config

KINDS = ("cli", "http", "chrome")


def as_map(raw: Any) -> dict[str, dict[str, Any]]:
    src = raw if isinstance(raw, dict) else {}
    out: dict[str, dict[str, Any]] = {}
    for key in KINDS:
        val = src.get(key)
        if isinstance(val, bool):
            out[key] = {"on": val, "source": ""}
        elif isinstance(val, dict):
            out[key] = {"on": bool(val.get("on")), "source": str(val.get("source") or "")}
        else:
            out[key] = {"on": False, "source": ""}
    return out


def current() -> dict[str, dict[str, Any]]:
    return as_map(load_config().get("connections"))


def save_one(kind: str, source: str, on: bool = True) -> dict[str, dict[str, Any]]:
    if kind not in KINDS:
        raise ValueError("연결 종류가 아닙니다.")
    with locked():
        cfg = load_config()
        con = as_map(cfg.get("connections"))
        con[kind] = {"on": on, "source": (source or "").strip()}
        cfg["connections"] = con
        save_config(cfg)
    return con


def test(kind: str, source: str) -> dict[str, Any]:
    """Probe one generic tool with the given command/URL and return its raw output."""
    kind = (kind or "").strip()
    source = (source or "").strip()
    if kind not in KINDS:
        raise ValueError("연결 종류가 아닙니다.")
    if not source:
        raise ValueError("실행할 소스나 명령을 적어 주세요.")
    if kind == "cli":
        return {"ok": True, "output": tools._run_cli(source, "machine")}
    if kind == "http":
        return {"ok": True, "output": tools._http("GET", source, None, "read")}
    return {"ok": True, "output": tools._chrome(source, "read")}


def probe_with_model(kind: str, source: str, model: str) -> dict[str, Any]:
    """Run the link, then ask the current model if it can use that result."""
    probed = test(kind, source)
    model = (model or "").strip() or ollama_ctl.default_model()
    if not model:
        return probed
    snippet = (probed.get("output") or "")[:800]
    with ollama_ctl.session():
        payload = ollama_ctl.chat(
            model,
            "아래는 방금 이 맥에서 실행한 연결 결과다. 이 연결을 자동화에 쓸 수 있으면 '연결 확인'이라고만 답해.\n\n"
            + snippet,
            effort="low",
        )
    probed["model"] = model
    probed["model_reply"] = ollama_ctl.extract_text(payload) or ""
    return probed
