"""연동(connector) 레지스트리: MCP 서버 · CLI 명령 · HTTP API · 스킬.

`data/connectors.json`을 읽고 쓴다. 이 맥에 이미 설정된 MCP 서버와 스킬을
자동 발견(discover)해서 후보로 돌려주며, env/헤더의 실제 값은 절대 저장하지 않고
키 이름만 남긴 뒤 실행 시점에 원본 파일(origin)에서 다시 읽는다(resolve_env).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from desk import state
from desk.paths import DATA, LOGS_DIR, ensure_dirs

try:  # tomllib은 3.11+ stdlib. 없으면 Codex 소스만 건너뛴다.
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

KINDS = ("mcp", "cli", "http", "skill")
SOURCES = ("manual", "claude-code", "claude-desktop", "codex", "cursor", "claude-plugin", "codex-plugin", "skill-dir")
TRANSPORTS = ("stdio", "http")
PARAM_TYPES = ("string", "integer", "boolean")
FILE = DATA / "connectors.json"
LEGACY_FILE = DATA / "mcp.json"
MAX_CANDIDATES = 200
HOME = Path.home()
SEOUL = ZoneInfo("Asia/Seoul")

_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_FRONT_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)


# ---------------------------------------------------------------- storage

def _now() -> str:
    return datetime.now(SEOUL).isoformat(timespec="seconds")


def log(line: str) -> None:
    """connectors 관련 진단을 data/logs/connectors.log 에 남긴다."""
    try:
        ensure_dirs()
        with (LOGS_DIR / "connectors.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{_now()} {line}\n")
    except OSError:
        pass


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path: Path, payload: Any) -> None:
    ensure_dirs()
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load() -> list[dict]:
    """등록된 연동 목록. 레거시 data/mcp.json 이 있으면 1회 이관한다."""
    ensure_dirs()
    with state.locked():
        data = _read_json(FILE) if FILE.exists() else None
        items = data.get("connectors") if isinstance(data, dict) else None
        items = [c for c in (items or []) if isinstance(c, dict) and c.get("id")]
        migrated = _migrate_legacy(items)
        if migrated:
            items.extend(migrated)
            save(items)
        return items


def save(items: list[dict]) -> None:
    _write_json(FILE, {"connectors": list(items)})


def get(cid: str) -> dict | None:
    return next((c for c in load() if c.get("id") == cid), None)


def _migrate_legacy(existing: list[dict]) -> list[dict]:
    """data/mcp.json 의 서버들을 kind=mcp/source=manual 로 옮기고 파일을 .migrated 로 바꾼다."""
    if not LEGACY_FILE.exists():
        return []
    cfg = _read_json(LEGACY_FILE)
    servers = (cfg.get("servers") or cfg.get("mcpServers") or {}) if isinstance(cfg, dict) else {}
    if isinstance(servers, list):
        servers = {str(r.get("name")): r for r in servers if isinstance(r, dict) and r.get("name")}
    out: list[dict] = []
    taken = {c.get("name") for c in existing}
    for name, spec in (servers.items() if isinstance(servers, dict) else []):
        if not isinstance(spec, dict) or name in taken or not spec.get("command"):
            continue
        try:
            out.append(_validate({
                "kind": "mcp", "name": name, "source": "manual", "transport": "stdio",
                "command": spec.get("command"), "args": spec.get("args") or [],
                "cwd": spec.get("cwd"), "env": spec.get("env") or {}, "note": "data/mcp.json 에서 이관",
            }))
        except ValueError as exc:
            log(f"legacy migrate skip {name}: {exc}")
    try:
        os.replace(LEGACY_FILE, LEGACY_FILE.with_suffix(".json.migrated"))
    except OSError as exc:
        log(f"legacy rename failed: {exc}")
    return out


# ---------------------------------------------------------------- validation

def sanitize_name(name: str, fallback: str = "") -> str:
    """도구 네임스페이스용 이름: ^[a-zA-Z0-9_-]{1,40}$. 비면 'c'+fallback 앞 6자."""
    clean = _NAME_RE.sub("", str(name or ""))[:40]
    return clean or ("c" + str(fallback or "")[:6])


def _new_id() -> str:
    return "c" + uuid.uuid4().hex[:8]


def _str(value: Any, default: str = "") -> str:
    return str(value).strip() if isinstance(value, (str, int, float)) else default


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return shlex.split(value)
    return [str(v) for v in (value or []) if v is not None] if isinstance(value, list) else []


def _str_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if str(k).strip()}


def _validate_params(raw: Any) -> dict[str, dict]:
    params: dict[str, dict] = {}
    if not isinstance(raw, dict):
        return params
    for name, spec in raw.items():
        key = _NAME_RE.sub("", str(name))
        if not key:
            raise ValueError(f"파라미터 이름이 올바르지 않습니다: {name}")
        spec = spec if isinstance(spec, dict) else {}
        ptype = _str(spec.get("type"), "string")
        if ptype not in PARAM_TYPES:
            ptype = "string"
        params[key] = {
            "type": ptype,
            "description": _str(spec.get("description")),
            "required": bool(spec.get("required")),
            "default": spec.get("default"),
        }
    return params


def _validate_mcp(con: dict, payload: dict) -> None:
    transport = _str(payload.get("transport"), "stdio") or "stdio"
    if payload.get("url") and not payload.get("command"):
        transport = "http"
    if transport not in TRANSPORTS:
        raise ValueError("transport 는 stdio 또는 http 여야 합니다.")
    con["transport"] = transport
    manual = con["source"] == "manual"
    if transport == "stdio":
        con["command"] = _str(payload.get("command"))
        if not con["command"]:
            raise ValueError("MCP 실행 명령을 적어 주세요.")
        con["args"] = _str_list(payload.get("args"))
        con["cwd"] = _str(payload.get("cwd")) or None
        env = _str_map(payload.get("env"))
        con["env_keys"] = sorted(set(env) | set(_str_list(payload.get("env_keys"))))
        if manual and env:
            con["env"] = env
    else:
        con["url"] = _str(payload.get("url"))
        if not con["url"].startswith(("http://", "https://")):
            raise ValueError("MCP 주소는 http(s) URL 이어야 합니다.")
        headers = _str_map(payload.get("headers"))
        con["headers_keys"] = sorted(set(headers) | set(_str_list(payload.get("headers_keys"))))
        if manual and headers:
            con["headers"] = headers
    con["tools_cache"] = [t for t in (payload.get("tools_cache") or []) if isinstance(t, dict)]
    con["tools_cached_at"] = payload.get("tools_cached_at") or None
    try:
        con["startup_timeout_sec"] = max(0, int(payload.get("startup_timeout_sec") or 0)) or None
    except (TypeError, ValueError):
        con["startup_timeout_sec"] = None


def _validate_cli(con: dict, payload: dict) -> None:
    con["description"] = _str(payload.get("description"))
    con["command_template"] = _str(payload.get("command_template"))
    if not con["command_template"]:
        raise ValueError("CLI 명령 템플릿을 적어 주세요.")
    con["params"] = _validate_params(payload.get("params"))
    con["readonly"] = bool(payload.get("readonly"))
    con["timeout"] = max(1, min(int(payload.get("timeout") or 60), 3600))


def _validate_http(con: dict, payload: dict) -> None:
    con["description"] = _str(payload.get("description"))
    con["method"] = _str(payload.get("method"), "GET").upper() or "GET"
    if con["method"] not in ("GET", "POST"):
        raise ValueError("HTTP 메서드는 GET 또는 POST 여야 합니다.")
    con["url_template"] = _str(payload.get("url_template"))
    if not con["url_template"].startswith(("http://", "https://")):
        raise ValueError("HTTP URL 템플릿은 http(s) 로 시작해야 합니다.")
    con["headers"] = _str_map(payload.get("headers"))
    con["body_template"] = _str(payload.get("body_template"))
    con["params"] = _validate_params(payload.get("params"))


def _validate_skill(con: dict, payload: dict) -> None:
    raw = _str(payload.get("path"))
    if not raw:
        raise ValueError("SKILL.md 경로를 적어 주세요.")
    path = Path(raw).expanduser()
    con["path"] = str(path.resolve()) if path.exists() else str(path)
    con["description"] = _str(payload.get("description"))
    con["plugin"] = _str(payload.get("plugin")) or None


_VALIDATORS: dict[str, Callable[[dict, dict], None]] = {
    "mcp": _validate_mcp, "cli": _validate_cli, "http": _validate_http, "skill": _validate_skill,
}


def _validate(payload: dict, existing: dict | None = None) -> dict:
    """payload 를 검증해 Connector 로 만든다. 잘못되면 ValueError(한국어)."""
    if not isinstance(payload, dict):
        raise ValueError("연동 정보가 올바르지 않습니다.")
    kind = _str(payload.get("kind")) or (existing or {}).get("kind")
    if kind not in KINDS:
        raise ValueError("연동 종류(kind)는 mcp / cli / http / skill 중 하나여야 합니다.")
    name = _str(payload.get("name"))
    if not name:
        raise ValueError("연동 이름을 적어 주세요.")
    if len(name) > 80:
        raise ValueError("연동 이름은 80자 이내여야 합니다.")
    source = _str(payload.get("source"), "manual") or "manual"
    if source not in SOURCES:
        raise ValueError("source 값이 올바르지 않습니다.")
    origin = payload.get("origin")
    if origin is not None and not (isinstance(origin, dict) and origin.get("file") and origin.get("key")):
        raise ValueError("origin 은 {file, key} 형태여야 합니다.")
    con: dict[str, Any] = {
        "id": (existing or {}).get("id") or _str(payload.get("id")) or _new_id(),
        "kind": kind,
        "name": name,
        "enabled": bool(payload.get("enabled", True)),
        "source": source,
        "origin": {"file": str(origin["file"]), "key": str(origin["key"])} if origin else None,
        "note": _str(payload.get("note"))[:500],
        "created_at": (existing or {}).get("created_at") or _str(payload.get("created_at")) or _now(),
    }
    _VALIDATORS[kind](con, payload)
    return con


def add(payload: dict) -> dict:
    """검증 후 id 를 부여해 저장한다."""
    with state.locked():
        items = load()
        con = _validate(payload)
        if any(c.get("id") == con["id"] for c in items):
            con["id"] = _new_id()
        items.append(con)
        save(items)
        return con


def update(cid: str, patch: dict) -> dict:
    """부분 갱신. 없으면 KeyError, 값이 틀리면 ValueError."""
    with state.locked():
        items = load()
        idx = next((i for i, c in enumerate(items) if c.get("id") == cid), None)
        if idx is None:
            raise KeyError(cid)
        merged = {**items[idx], **(patch if isinstance(patch, dict) else {})}
        merged.pop("candidate_key", None)
        con = _validate(merged, existing=items[idx])
        items[idx] = con
        save(items)
        return con


def remove(cid: str) -> None:
    """삭제. 없으면 KeyError(API 에서 404)."""
    with state.locked():
        items = load()
        kept = [c for c in items if c.get("id") != cid]
        if len(kept) == len(items):
            raise KeyError(cid)
        save(kept)


def public(con: dict) -> dict:
    """API 응답용 사본: env/headers 값 제거(키만), URL 쿼리(?key=…) 가림.

    kind 가 없는 dict 에도 안전하게 동작한다(http 연동만 headers 를 값 가린 채 유지).
    """
    out = {k: v for k, v in con.items() if k not in ("env", "headers")}
    if isinstance(con.get("env"), dict):
        out["env_keys"] = sorted(set(out.get("env_keys") or []) | set(con["env"]))
    if con.get("kind") == "http":
        out["headers"] = {k: _redact_value(v) for k, v in (con.get("headers") or {}).items()}
        out["url_template"] = redact_url(con.get("url_template") or "")
    elif isinstance(con.get("headers"), dict):
        out["headers_keys"] = sorted(set(out.get("headers_keys") or []) | set(con["headers"]))
    if isinstance(out.get("url"), str) and out["url"]:
        out["url"] = redact_url(out["url"])
    return out


def redact_url(url: str) -> str:
    """쿼리 문자열이 있으면 `?<redacted>` 로 가린다."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "<redacted>", ""))


def _redact_value(value: str) -> str:
    """Keep a pure env reference, optionally prefixed by the literal Bearer scheme.

    Defaults and every other literal remain private: `${KEY:-secret}` must not
    expose its fallback, and `Bearer secret${KEY}` is not a safe placeholder.
    """
    text = value.strip()
    reference = text
    if text.lower().startswith("bearer "):
        reference = text[len("Bearer "):].strip()
    match = _VAR_RE.fullmatch(reference)
    if match and match.group(2) is None:
        return text
    return "<redacted>"


# ---------------------------------------------------------------- env resolution

def expand_vars(value: str, extra: dict[str, str] | None = None) -> str:
    """`${VAR}` / `${VAR:-default}` / `$VAR` 를 os.environ(+extra)으로 치환. 없으면 빈 문자열."""
    env = {**os.environ, **(extra or {})}

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(3)
        default = match.group(2)
        if env.get(name):
            return env[name]
        return default if default is not None else ""

    return _VAR_RE.sub(_sub, str(value))


def _load_config_file(path: Path) -> Any:
    if not path.is_file():
        return None
    if path.suffix == ".toml":
        if tomllib is None:
            return None
        try:
            return tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    return _read_json(path)


def _find_spec(data: Any, key: str, base: Path) -> dict | None:
    """설정 파일 데이터에서 서버 spec 을 찾는다. key 는 'name' 또는 '<path>::name'."""
    if not isinstance(data, dict):
        return None
    scope, _, name = key.rpartition("::")
    tables: list[Any] = []
    if scope and isinstance(data.get("projects"), dict):
        tables.append((data["projects"].get(scope) or {}).get("mcpServers"))
    servers = data.get("mcpServers")
    if isinstance(servers, str):  # plugin.json: "mcpServers": "./.mcp.json"
        nested = _load_config_file((base / servers).resolve())
        servers = nested.get("mcpServers", nested) if isinstance(nested, dict) else None
    tables += [servers, data.get("mcp_servers"), data.get("servers"), data]
    for table in tables:
        if isinstance(table, dict) and isinstance(table.get(name), dict):
            return table[name]
    return None


def origin_spec(con: dict) -> dict | None:
    """origin 파일을 다시 읽어 원본 서버 spec 을 돌려준다. 없으면 None."""
    origin = con.get("origin") or {}
    if not origin.get("file"):
        return None
    path = Path(str(origin["file"]))
    return _find_spec(_load_config_file(path), str(origin.get("key") or ""), path.parent)


def _plugin_root(con: dict) -> dict[str, str]:
    if con.get("source") in ("claude-plugin", "codex-plugin") and con.get("cwd"):
        return {"CLAUDE_PLUGIN_ROOT": str(con["cwd"]), "CODEX_PLUGIN_ROOT": str(con["cwd"])}
    return {}


def _expanded_map(raw: Any, con: dict) -> dict[str, str]:
    values = {k: expand_vars(v, _plugin_root(con)) for k, v in _str_map(raw).items()}
    return {k: v for k, v in values.items() if v}


def resolve_env(con: dict) -> dict[str, str]:
    """stdio MCP 의 env 실제 값: origin 파일(+manual 저장값)에서 읽어 ${VAR} 확장. 없으면 {}."""
    if con.get("kind") != "mcp":
        return {}
    spec = origin_spec(con) or {}
    env = _expanded_map(spec.get("env"), con)
    env.update(_expanded_map(con.get("env"), con))
    return env


def resolve_url(con: dict) -> str:
    """http MCP 의 실제 URL: origin 파일의 url(쿼리 포함)을 우선, 없으면 저장된 url."""
    spec = origin_spec(con) or {}
    return expand_vars(_str(spec.get("url")) or _str(con.get("url")), _plugin_root(con))


def resolve_headers(con: dict) -> dict[str, str]:
    """http MCP / http 연동의 헤더 실제 값(${ENV} 확장). 없으면 {}."""
    spec = origin_spec(con) or {}
    headers = _expanded_map(spec.get("headers"), con)
    headers.update(_expanded_map(con.get("headers"), con))
    return headers


# ---------------------------------------------------------------- skills

def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """`---` 사이의 `key: value` 를 파싱해 (meta, 본문) 을 돌려준다."""
    match = _FRONT_RE.match(text)
    if not match:
        return {}, text
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line or line.startswith((" ", "\t", "-")):
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            meta[key.strip().lower()] = value
    return meta, text[match.end():]


def _read_skill(path: Path) -> tuple[dict[str, str], str] | None:
    try:
        return parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def skill_text(con: dict, limit: int = 6000) -> str:
    """SKILL.md 본문(frontmatter 제거)을 limit 자로 잘라 돌려준다."""
    if con.get("kind") != "skill" or not con.get("path"):
        return ""
    parsed = _read_skill(Path(str(con["path"])))
    if parsed is None:
        return ""
    body = parsed[1].strip()
    return body[:limit] + ("\n…(잘림)" if len(body) > limit else "")


# ---------------------------------------------------------------- discovery

def _candidate(kind: str, source: str, name: str, origin_file: Path | str, origin_key: str, **fields: Any) -> dict:
    con: dict[str, Any] = {
        "kind": kind, "name": name, "enabled": True, "source": source,
        "origin": {"file": str(origin_file), "key": origin_key},
        "note": fields.pop("note", ""),
    }
    con.update(fields)
    con["candidate_key"] = f"{source}:{origin_file}:{origin_key}"
    return con


def _mcp_candidate(source: str, name: str, spec: dict, origin_file: Path, key: str,
                   cwd: str | None = None, note: str = "") -> dict | None:
    """mcpServers 항목 하나를 후보로. env/headers 는 키 이름만 남긴다."""
    if not isinstance(spec, dict):
        return None
    url = _str(spec.get("url"))
    if spec.get("type") in ("http", "sse") or (url and not spec.get("command")):
        if not url:
            return None
        # 쿼리(?key=…)는 시크릿일 수 있으니 저장하지 않고 실행 시 origin 에서 다시 읽는다(resolve_url).
        return _candidate("mcp", source, name, origin_file, key, transport="http", url=redact_url(url),
                          headers_keys=sorted(_str_map(spec.get("headers"))), note=note,
                          tools_cache=[], tools_cached_at=None)
    command = _str(spec.get("command"))
    if not command:
        return None
    spec_cwd = _str(spec.get("cwd"))
    if spec_cwd and not Path(spec_cwd).is_absolute():
        spec_cwd = str((Path(cwd) if cwd else HOME).joinpath(spec_cwd).resolve())
    return _candidate("mcp", source, name, origin_file, key, transport="stdio", command=command,
                      args=_str_list(spec.get("args")), cwd=spec_cwd or cwd or None,
                      env_keys=sorted(_str_map(spec.get("env"))), note=note,
                      tools_cache=[], tools_cached_at=None)


def _skill_candidate(source: str, skill_md: Path, plugin: str | None = None) -> dict | None:
    try:
        real = skill_md.resolve(strict=True)
    except OSError:
        return None
    parsed = _read_skill(real)
    if parsed is None:
        return None
    meta = parsed[0]
    name = meta.get("name") or real.parent.name
    return _candidate("skill", source, name, real, name, path=str(real),
                      description=meta.get("description", "")[:500], plugin=plugin,
                      note=(f"플러그인 {plugin}" if plugin else ""))


def _mcp_table(data: Any) -> dict[str, dict]:
    """`{name: spec}` 또는 `{"mcpServers": {...}}` 둘 다 받는다."""
    if not isinstance(data, dict):
        return {}
    table = data.get("mcpServers", data)
    return {str(k): v for k, v in table.items() if isinstance(v, dict)} if isinstance(table, dict) else {}


def _from_claude_code() -> list[dict]:
    path = HOME / ".claude.json"
    data = _read_json(path) if path.exists() else None
    if not isinstance(data, dict):
        return []
    out: list[dict] = []
    for name, spec in _mcp_table(data.get("mcpServers") or {}).items():
        out.append(_mcp_candidate("claude-code", name, spec, path, name))
    projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
    for proj, pdata in projects.items():
        if not isinstance(pdata, dict):
            continue
        for name, spec in _mcp_table(pdata.get("mcpServers") or {}).items():
            out.append(_mcp_candidate("claude-code", name, spec, path, f"{proj}::{name}", cwd=proj,
                                      note=f"프로젝트 {proj}"))
        mcp_json = Path(proj) / ".mcp.json"
        for name, spec in _mcp_table(_read_json(mcp_json) if mcp_json.is_file() else None).items():
            out.append(_mcp_candidate("claude-code", name, spec, mcp_json, f"{proj}::{name}", cwd=proj,
                                      note=f"프로젝트 .mcp.json ({proj})"))
    return [c for c in out if c]


def _from_json_file(source: str, path: Path) -> list[dict]:
    data = _read_json(path) if path.is_file() else None
    if not isinstance(data, dict):
        return []
    out = [_mcp_candidate(source, name, spec, path, name) for name, spec in _mcp_table(data.get("mcpServers")).items()]
    return [c for c in out if c]


def _from_codex() -> list[dict]:
    path = HOME / ".codex" / "config.toml"
    data = _load_config_file(path)
    if not isinstance(data, dict) or not isinstance(data.get("mcp_servers"), dict):
        return []
    out: list[dict] = []
    for name, spec in data["mcp_servers"].items():
        if not isinstance(spec, dict):
            continue
        note = "Codex에서 비활성" if spec.get("enabled") is False else ""
        cand = _mcp_candidate("codex", str(name), spec, path, str(name), note=note)
        if cand and spec.get("startup_timeout_sec"):
            cand["startup_timeout_sec"] = int(spec["startup_timeout_sec"])
        if cand:
            out.append(cand)
    return out


def _plugin_dirs_claude() -> list[Path]:
    data = _read_json(HOME / ".claude" / "plugins" / "installed_plugins.json")
    plugins = data.get("plugins") if isinstance(data, dict) else None
    dirs: list[Path] = []
    for entries in (plugins or {}).values():
        for entry in entries if isinstance(entries, list) else []:
            install = _str((entry or {}).get("installPath")) if isinstance(entry, dict) else ""
            if install and Path(install).is_dir():
                dirs.append(Path(install))
    return dirs


def _plugin_servers(plugin_dir: Path, manifest_rel: str) -> list[tuple[Path, str, dict]]:
    """(origin_file, name, spec) 목록: <dir>/.mcp.json 과 manifest 의 mcpServers 둘 다 확인."""
    found: dict[str, tuple[Path, str, dict]] = {}
    mcp_json = plugin_dir / ".mcp.json"
    for name, spec in _mcp_table(_read_json(mcp_json) if mcp_json.is_file() else None).items():
        found[name] = (mcp_json, name, spec)
    manifest = plugin_dir / manifest_rel
    data = _read_json(manifest) if manifest.is_file() else None
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if isinstance(servers, str):
        target = (plugin_dir / servers).resolve()
        if target != mcp_json.resolve():
            for name, spec in _mcp_table(_read_json(target) if target.is_file() else None).items():
                found.setdefault(name, (manifest, name, spec))
    elif isinstance(servers, dict):
        for name, spec in _mcp_table(servers).items():
            found.setdefault(name, (manifest, name, spec))
    return list(found.values())


def _plugin_description(plugin_dir: Path, manifest_rel: str) -> tuple[str, str]:
    manifest = plugin_dir / manifest_rel
    data = _read_json(manifest) if manifest.is_file() else None
    if not isinstance(data, dict):
        return plugin_dir.name, ""
    return _str(data.get("name")) or plugin_dir.name, _str(data.get("description"))[:300]


def _from_plugins(source: str, dirs: list[Path], manifest_rel: str) -> list[dict]:
    out: list[dict] = []
    for pdir in dirs:
        pname, desc = _plugin_description(pdir, manifest_rel)
        for origin_file, name, spec in _plugin_servers(pdir, manifest_rel):
            cand = _mcp_candidate(source, name, spec, origin_file, name, cwd=str(pdir), note=desc or f"플러그인 {pname}")
            if cand:
                out.append(cand)
        for skill_md in sorted((pdir / "skills").glob("*/SKILL.md")):
            cand = _skill_candidate(source, skill_md, plugin=pname)
            if cand:
                out.append(cand)
    return out


def _plugin_dirs_codex() -> list[Path]:
    root = HOME / ".codex" / "plugins"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".") and p.name != "cache")


def _from_skill_dirs() -> list[dict]:
    out: list[dict] = []
    for base in (HOME / ".claude" / "skills", HOME / ".codex" / "skills"):
        if not base.is_dir():
            continue
        for skill_md in sorted(base.glob("*/SKILL.md")):
            cand = _skill_candidate("skill-dir", skill_md)
            if cand:
                out.append(cand)
    return out


def _all_candidates() -> list[dict]:
    collectors: list[Callable[[], list[dict]]] = [
        _from_claude_code,
        lambda: _from_json_file("claude-desktop", HOME / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"),
        _from_codex,
        lambda: _from_json_file("cursor", HOME / ".cursor" / "mcp.json"),
        lambda: _from_plugins("claude-plugin", _plugin_dirs_claude(), ".claude-plugin/plugin.json"),
        lambda: _from_plugins("codex-plugin", _plugin_dirs_codex(), ".codex-plugin/plugin.json"),
        _from_skill_dirs,
    ]
    out: list[dict] = []
    for collect in collectors:
        try:
            out.extend(collect())
        except Exception as exc:  # 한 소스가 깨져도 나머지는 계속
            log(f"discover source failed ({collect.__name__}): {exc}")
    return out


def discover() -> list[dict]:
    """아직 등록되지 않은 연동 후보. kind→source→name 순, 최대 200개. env/헤더 값은 없다."""
    registered = {(str(c["origin"].get("file")), str(c["origin"].get("key"))) for c in load() if c.get("origin")}
    seen_keys: set[str] = set()
    seen_paths: set[str] = set()
    out: list[dict] = []
    for cand in _all_candidates():
        origin = (str(cand["origin"]["file"]), str(cand["origin"]["key"]))
        if origin in registered or cand["candidate_key"] in seen_keys:
            continue
        if cand["kind"] == "skill":
            if cand["path"] in seen_paths:
                continue
            seen_paths.add(cand["path"])
        seen_keys.add(cand["candidate_key"])
        out.append(cand)
    kind_rank = {k: i for i, k in enumerate(KINDS)}
    source_rank = {s: i for i, s in enumerate(SOURCES)}
    out.sort(key=lambda c: (kind_rank.get(c["kind"], 9), source_rank.get(c["source"], 9), c["name"].lower()))
    if len(out) > MAX_CANDIDATES:
        log(f"discover: {len(out)} candidates, capped to {MAX_CANDIDATES}")
        out = out[:MAX_CANDIDATES]
    return out


def import_candidate(candidate_key: str) -> dict:
    """discover() 결과 중 하나를 등록한다. 없으면 KeyError."""
    cand = next((c for c in discover() if c["candidate_key"] == candidate_key), None)
    if cand is None:
        raise KeyError(candidate_key)
    payload = {k: v for k, v in cand.items() if k != "candidate_key"}
    return add(payload)


# ---------------------------------------------------------------- test

def test(cid: str) -> dict:
    """연동을 실제로 확인한다. mcp 는 기동→tools/list 후 tools_cache 갱신."""
    con = get(cid)
    if con is None:
        raise KeyError(cid)
    return {"mcp": _test_mcp, "cli": _test_cli, "http": _test_http, "skill": _test_skill}[con["kind"]](con)


def _test_mcp(con: dict) -> dict:
    from desk import mcp_host

    started = time.time()
    client = mcp_host.MCPClient(con, resolve_env(con), headers=resolve_headers(con))
    try:
        client.start()
        tools = client.list_tools()
    except Exception as exc:
        return {"ok": False, "tools": [], "error": str(exc), "seconds": round(time.time() - started, 2)}
    finally:
        client.close()
    try:
        update(con["id"], {"tools_cache": tools, "tools_cached_at": _now()})
    except (KeyError, ValueError) as exc:
        log(f"tools_cache update failed for {con['id']}: {exc}")
    return {"ok": True, "tools": tools, "error": None, "seconds": round(time.time() - started, 2)}


def _test_cli(con: dict) -> dict:
    try:
        first = shlex.split(con.get("command_template") or "")[0]
    except (ValueError, IndexError):
        return {"ok": False, "detail": "명령 템플릿을 해석할 수 없습니다."}
    found = shutil.which(first, path=_cli_path())
    if not found:
        return {"ok": False, "detail": f"'{first}' 명령을 이 맥에서 찾을 수 없습니다."}
    return {"ok": True, "detail": found}


def _cli_path() -> str:
    """cron 환경에서도 Homebrew 명령이 잡히도록 PATH 를 보강한다."""
    parts = ["/opt/homebrew/bin", "/usr/local/bin", str(HOME / ".local" / "bin"), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    parts += [p for p in os.environ.get("PATH", "").split(":") if p]
    return ":".join(dict.fromkeys(parts))


def default_args(con: dict) -> dict[str, Any]:
    """params 의 default 값 모음(없으면 빈 문자열)."""
    return {name: (spec.get("default") if spec.get("default") is not None else "")
            for name, spec in (con.get("params") or {}).items()}


def fill_template(template: str, args: dict[str, Any], quoter: Callable[[str], str]) -> str:
    """`{이름}` 자리를 quoter 로 감싼 값으로 치환한다."""
    def _sub(match: re.Match[str]) -> str:
        return quoter(str(args.get(match.group(1), "")))

    # `${ENV}` belongs to environment expansion, never model-supplied args.
    return re.sub(r"(?<!\$)\{([A-Za-z0-9_-]+)\}", _sub, template)


def _test_http(con: dict) -> dict:
    url = fill_template(con.get("url_template") or "", default_args(con), lambda v: quote(v, safe=""))
    headers = {"User-Agent": "Free-AI-Scheduler", **resolve_headers(con)}
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=10) as resp:
                return {"ok": True, "status": resp.status, "url": redact_url(url)}
        except urllib.error.HTTPError as exc:
            if method == "HEAD" and exc.code in (405, 501):
                continue
            if exc.code in (401, 403):
                return {"ok": False, "status": exc.code, "url": redact_url(url),
                        "error": f"HTTP {exc.code}: 인증 정보 또는 접근 권한을 확인하세요."}
            return {"ok": exc.code < 500, "status": exc.code, "url": redact_url(url)}
        except Exception as exc:
            return {"ok": False, "status": None, "error": str(exc), "url": redact_url(url)}
    return {"ok": False, "status": None, "error": "응답 없음"}


def _test_skill(con: dict) -> dict:
    path = Path(str(con.get("path") or ""))
    parsed = _read_skill(path) if path.is_file() else None
    if parsed is None:
        return {"ok": False, "name": con.get("name"), "description": "", "error": "SKILL.md 파일이 없습니다."}
    meta = parsed[0]
    return {"ok": True, "name": meta.get("name") or con.get("name"), "description": meta.get("description", "")}
