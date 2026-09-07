"""Ollama 함수 도구: 범용 도구(run_cli/http_request/chrome_open/read_file) + 연동 도구.

연동 도구 이름: cli__<연동>, http__<연동>, mcp__<연동>__<도구>. 이름→(연동, 원래 도구명)
역매핑과 허용 목록은 실행별 ToolContext 에 보관한다. 실패는 예외 대신
"도구 실패: ..." 문자열로 돌려준다(기존 규약).
"""
from __future__ import annotations

import copy
import hashlib
import http.client
import ipaddress
import json
import os
import shlex
import signal
import shutil
import socket
import stat
import sys
import tempfile
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping
from urllib.parse import quote, urlparse, urljoin

from desk import connectors as connectors_mod
from desk.paths import DATA

if TYPE_CHECKING:
    from desk.mcp_host import MCPClient

MAX_OUT = 8000
MAX_TOOLS = 48
MAX_NAME = 64
DESC_CAP = 200
SKILL_CHARS = 6000
SKILLS_TOTAL_CHARS = 16000
WORKSPACE = DATA / "workspace"
BLOCKED_HOME_DIRS = ("Library/Keychains", ".ssh", ".aws", ".gnupg", ".config/gh", ".netrc", ".codex", ".claude", ".claude.json", ".config/gogcli", ".openclaw")
LOCAL_HOSTS = ("localhost", "0.0.0.0", "broadcasthost")

@dataclass
class ToolContext:
    permission: str
    clients: dict[str, "MCPClient"]
    specifications: list[dict] = field(default_factory=list)
    targets: dict[str, tuple[dict, str, dict]] = field(default_factory=dict)
    allowed_names: frozenset[str] = frozenset()



# ---------------------------------------------------------------- specs

def _generic_tools(perm: str) -> list[dict[str, Any]]:
    tools = [
        _fn("run_cli", "로컬 명령을 실행합니다. 외부 프로그램은 macOS 격리 환경에서만 실행되며 파일·자격 증명·로컬 서비스 접근이 제한됩니다.",
            {"command": {"type": "string"}}, ["command"]),
        _fn("http_request", "HTTP API를 호출합니다. GET 또는 POST.",
            {"method": {"type": "string", "enum": ["GET", "POST"]}, "url": {"type": "string"},
             "body": {"type": "string", "description": "POST일 때 JSON 또는 텍스트"}}, ["url"]),
        _fn("chrome_open", "검증된 공개 URL의 페이지 텍스트를 가져옵니다. 브라우저 자동 탐색은 하지 않습니다.", {"url": {"type": "string"}}, ["url"]),
        _fn("read_file", "파일을 읽습니다.", {"path": {"type": "string"}}, ["path"]),
    ]
    if perm == "read":
        tools = [t for t in tools if t["function"]["name"] != "run_cli"]
    return tools


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


def _wanted_names(wanted: list[str], have_connectors: bool) -> set[str]:
    allow = set()
    if "cli" in wanted:
        allow.add("run_cli")
    if "http" in wanted:
        allow.add("http_request")
    if "chrome" in wanted:
        allow.add("chrome_open")
    if allow or have_connectors:
        allow.add("read_file")
    return allow


def _params_schema(params: dict) -> dict[str, Any]:
    props: dict[str, Any] = {}
    required: list[str] = []
    for name, spec in (params or {}).items():
        prop: dict[str, Any] = {"type": spec.get("type") or "string"}
        if spec.get("description"):
            prop["description"] = str(spec["description"])
        props[name] = prop
        if spec.get("required"):
            required.append(name)
    return {"type": "object", "properties": props, "required": required}


def _input_schema(schema: Any) -> dict[str, Any]:
    params = dict(schema) if isinstance(schema, dict) else {}
    params["type"] = "object"
    params.setdefault("properties", {})
    params.pop("$schema", None)
    if not isinstance(params.get("required"), list):
        params.pop("required", None)
    return params


def _con_slug(con: dict) -> str:
    return connectors_mod.sanitize_name(con.get("name") or "", str(con.get("id") or ""))


def _unique(name: str, taken: set[str]) -> str:
    if len(name) > MAX_NAME:
        name = name[: MAX_NAME - 7] + "_" + hashlib.sha1(name.encode()).hexdigest()[:6]
    base, n = name, 2
    while name in taken:
        suffix = f"_{n}"
        name, n = base[: MAX_NAME - len(suffix)] + suffix, n + 1
    taken.add(name)
    return name


def _mcp_allowed(tool: dict, perm: str) -> bool:
    """읽기 권한에서는 명시적으로 readOnlyHint=true인 도구만 허용한다."""
    if perm != "read":
        return True
    ann = tool.get("annotations") or {}
    return ann.get("readOnlyHint") is True and ann.get("destructiveHint") is not True


def _connector_tools(perm: str, cons: list[dict], mcp_clients: dict[str, "MCPClient"], taken: set[str], context: ToolContext) -> list[dict]:
    out: list[dict] = []
    for con in cons:
        if not con.get("enabled", True):
            continue
        kind = con.get("kind")
        if kind == "cli":
            name = _unique(f"cli__{_con_slug(con)}", taken)
            context.targets[name] = (con, "", {})
            out.append(_fn(name, (con.get("description") or con.get("name") or "")[:DESC_CAP],
                           **_split_schema(_params_schema(con.get("params") or {}))))
        elif kind == "http":
            name = _unique(f"http__{_con_slug(con)}", taken)
            context.targets[name] = (con, "", {})
            out.append(_fn(name, (con.get("description") or con.get("name") or "")[:DESC_CAP],
                           **_split_schema(_params_schema(con.get("params") or {}))))
        elif kind == "mcp":
            client = mcp_clients.get(str(con.get("id")))
            if client is None:
                continue
            out.extend(_mcp_tools(perm, con, client, taken, context))
    return out


def _split_schema(schema: dict) -> dict[str, Any]:
    return {"properties": schema["properties"], "required": schema.get("required") or []}


def _mcp_tools(perm: str, con: dict, client: "MCPClient", taken: set[str], context: ToolContext) -> list[dict]:
    try:
        tools = client.list_tools()
    except Exception as exc:
        _log(f"tools/list 실패 ({con.get('name')}): {exc}")
        tools = con.get("tools_cache") or []
    out: list[dict] = []
    prefix = f"mcp__{_con_slug(con)}__"
    for tool in tools:
        if not isinstance(tool, dict) or not tool.get("name") or not _mcp_allowed(tool, perm):
            continue
        name = _unique(prefix + connectors_mod.sanitize_name(tool["name"], "tool"), taken)
        context.targets[name] = (con, str(tool["name"]), copy.deepcopy(tool))
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": str(tool.get("description") or tool["name"])[:DESC_CAP],
                "parameters": _input_schema(tool.get("inputSchema")),
            },
        })
    return out


def build_context(permission: str, wanted: list[str], connectors: list[dict],
                  mcp_clients: dict[str, "MCPClient"]) -> ToolContext:
    """Snapshot this run's capabilities; another run cannot replace its dispatch map."""
    perm = permission or "workspace"
    if perm not in ("read", "workspace", "machine"):
        raise ValueError("권한 값이 올바르지 않습니다.")
    context = ToolContext(perm, dict(mcp_clients or {}))
    cons = [copy.deepcopy(c) for c in (connectors or []) if isinstance(c, dict)
            and c.get("kind") in ("cli", "http", "mcp") and c.get("enabled", True)]
    allow = _wanted_names(wanted or [], bool(cons))
    items = [t for t in _generic_tools(perm) if t["function"]["name"] in allow]
    taken = {t["function"]["name"] for t in items}
    items += _connector_tools(perm, cons, context.clients, taken, context)
    context.specifications = items[:MAX_TOOLS]
    context.allowed_names = frozenset(t["function"]["name"] for t in context.specifications)
    context.targets = {name: target for name, target in context.targets.items() if name in context.allowed_names}
    return context


def specs(permission: str, wanted: list[str], connectors: list[dict],
          mcp_clients: dict[str, "MCPClient"]) -> list[dict]:
    """Display-only specifications. Use build_context to obtain executable capabilities."""
    return build_context(permission, wanted, connectors, mcp_clients).specifications


def system_prompt(permission: str, loops: int, skills: list[dict]) -> str:
    """기존 안내 문장 + 스킬 본문(스킬당 6000자, 합계 16000자)."""
    text = (
        "이 Mac에서 돌아가는 로컬 자동화다. 답이 짧으면 짧게 끝내고, "
        "CLI·HTTP API·Chrome·연동 도구가 필요하면 도구를 쓴다. "
        f"권한은 {permission}이다. 도구는 최대 {loops}번이다. "
        "도구 결과를 보고 마지막에 사람에게 줄 답을 쓴다."
    )
    budget = SKILLS_TOTAL_CHARS
    for con in skills or []:
        if con.get("kind") != "skill" or not con.get("enabled", True) or budget <= 0:
            continue
        body = connectors_mod.skill_text(con, limit=min(SKILL_CHARS, budget))
        if not body:
            continue
        block = f"\n\n## 스킬: {con.get('name')}\n{body}"
        budget -= len(block)
        text += block
    return text


# ---------------------------------------------------------------- run

def run(
    name: str,
    args: dict[str, Any],
    permission: str,
    mcp_clients: dict[str, "MCPClient"],
    timeout: float | None = None,
    *, context: ToolContext | None = None,
) -> str:
    """도구 하나 실행. 실패는 예외 대신 "도구 실패: ..." 문자열. timeout 은 MCP 호출에만 적용(남은 예산 클램프용)."""
    perm = permission or "workspace"
    args = args if isinstance(args, dict) else {}
    try:
        if context is None or name not in context.allowed_names:
            raise RuntimeError(f"선택되지 않은 도구입니다: {name}")
        if context.permission != perm:
            raise RuntimeError("도구 실행 권한이 실행 컨텍스트와 다릅니다.")
        if name == "run_cli":
            return _run_cli(str(args.get("command") or ""), perm)
        if name == "http_request":
            return _http(str(args.get("method") or "GET"), str(args.get("url") or ""), args.get("body"), perm)
        if name == "chrome_open":
            return _chrome(str(args.get("url") or ""), perm)
        if name == "read_file":
            return _read_file(str(args.get("path") or ""), perm)
        target = context.targets.get(name)
        if target is None:
            return f"모르는 도구: {name}"
        con, tool_name, tool_metadata = target
        if name.startswith("cli__"):
            return _run_cli_connector(con, args, perm)
        if name.startswith("http__"):
            return _run_http_connector(con, args, perm)
        if name.startswith("mcp__"):
            if not _mcp_allowed(tool_metadata, perm):
                raise RuntimeError("읽기 권한에서는 쓰기 MCP 도구를 실행하지 않습니다.")
            return _run_mcp_connector(con, tool_name, args, context.clients, timeout, perm)
        return f"모르는 도구: {name}"
    except Exception as exc:
        return f"도구 실패: {exc}"


def _run_cli_connector(con: dict, args: dict, perm: str) -> str:
    if perm == "read" and not con.get("readonly"):
        raise RuntimeError("읽기 권한에서는 이 CLI 연동을 실행하지 않습니다.")
    values = {**connectors_mod.default_args(con), **{k: v for k, v in args.items() if v is not None}}
    command = connectors_mod.fill_template(con.get("command_template") or "", values, shlex.quote)
    argv = shlex.split(command)
    if argv and argv[0] in _CLI_NAMES:
        return _restricted_cli(command, perm)
    return _cli_result(sandbox_run(argv, perm, int(con.get("timeout") or 60)))


def _run_http_connector(con: dict, args: dict, perm: str) -> str:
    method = str(con.get("method") or "GET").upper()
    if perm == "read" and method != "GET":
        raise RuntimeError("읽기 권한에서는 GET만 됩니다.")
    values = {**connectors_mod.default_args(con), **{k: v for k, v in args.items() if v is not None}}
    url = connectors_mod.fill_template(con.get("url_template") or "", values, lambda v: quote(v, safe=""))
    headers = {"User-Agent": "Free-AI-Scheduler"}
    headers.update({k: connectors_mod.expand_vars(v) for k, v in (con.get("headers") or {}).items()})
    body = None
    if method == "POST":
        body = connectors_mod.fill_template(con.get("body_template") or "", values, lambda v: json.dumps(v)[1:-1])
        headers.setdefault("Content-Type", "application/json")
    return _fetch(method, url, body, headers, allow_local=True)


def _run_mcp_connector(con: dict, tool_name: str, args: dict, mcp_clients: dict[str, "MCPClient"], timeout: float | None, perm: str = "workspace") -> str:
    client = mcp_clients.get(str(con.get("id")))
    if client is None:
        raise RuntimeError(f"MCP 서버 '{con.get('name')}'가 켜져 있지 않습니다.")
    if perm == "read":
        current = next((tool for tool in client.list_tools() if tool.get("name") == tool_name), None)
        if current is None or not _mcp_allowed(current, perm):
            raise RuntimeError("읽기 전용임이 확인된 MCP 도구만 실행합니다.")
    if timeout is None:
        return client.call(tool_name, args)
    return client.call(tool_name, args, timeout=max(0.001, float(timeout)))


# ---------------------------------------------------------------- generic tools

def _root_for(perm: str) -> Path:
    if perm == "machine":
        return Path.home()
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    return WORKSPACE


def _blocked_under_home(path: Path) -> bool:
    home = Path.home().resolve()
    for rel in BLOCKED_HOME_DIRS:
        blocked = home / rel
        if path == blocked or blocked in path.parents:
            return True
    return False


def _safe_path(raw: str, perm: str) -> Path:
    root = _root_for(perm).resolve()
    path = Path(raw).expanduser()
    path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    if perm == "machine":
        if _blocked_under_home(path):
            raise RuntimeError("자격 증명 폴더는 읽지 않습니다.")
        return path
    try:
        path.relative_to(root)
    except ValueError:
        raise RuntimeError("이 권한으로는 그 경로를 못 엽니다.") from None
    return path


_CLI_NAMES = frozenset(("echo", "pwd", "ls", "cat", "head", "wc"))


def _cli_tokens(command: str) -> list[str]:
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        raise RuntimeError(f"명령을 해석할 수 없습니다: {exc}") from exc
    if not tokens or tokens[0] not in _CLI_NAMES:
        raise RuntimeError("허용된 로컬 명령은 echo, pwd, ls, cat, head, wc입니다. 셸·외부 프로그램은 실행하지 않습니다.")
    return tokens


def _check_command_paths(command: str, perm: str) -> None:
    """Compatibility validator: reject external executables, then validate operands."""
    tokens = _cli_tokens(command)
    if tokens[0] not in ("echo", "pwd"):
        for token in tokens[1:]:
            if not token.startswith("-"):
                _safe_path(token, perm)


def _open_checked(raw: str, perm: str, directory: bool = False) -> int:
    """Resolve policy first, then walk components without following race-replaced links."""
    path = _safe_path(raw, perm)
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(path.parts[1:]):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(path.parts) - 2 or directory:
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        mode = os.fstat(fd).st_mode
        if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
            raise RuntimeError("일반 파일 또는 폴더만 읽습니다.")
        result, fd = fd, -1
        return result
    finally:
        if fd >= 0:
            os.close(fd)


def _read_bytes(raw: str, perm: str, limit: int = MAX_OUT * 2) -> bytes:
    fd = _open_checked(raw, perm)
    with os.fdopen(fd, "rb") as file:
        return file.read(limit)


def _restricted_cli(command: str, perm: str) -> str:
    """Small in-process command grammar; no shell, executable lookup, or subprocess."""
    name, *args = _cli_tokens(command)
    if name == "echo":
        return " ".join(args)[:MAX_OUT] or "(출력 없음)"
    if name == "pwd":
        if args:
            raise RuntimeError("pwd에는 인자를 지정하지 않습니다.")
        return str(_root_for(perm))
    option = ""
    if args and args[0].startswith("-"):
        option = args.pop(0)
        allowed = {"ls": {"-a", "-l", "-la", "-al"}, "wc": {"-l", "-w", "-c"},
                   "cat": {"--"}, "head": {"--"}}
        if option not in allowed.get(name, set()):
            raise RuntimeError("이 명령의 옵션은 지원하지 않습니다.")
    if any(a.startswith("-") for a in args):
        raise RuntimeError("옵션 대신 ./파일명으로 경로를 지정하세요.")
    if name == "ls":
        if len(args) > 1:
            raise RuntimeError("ls에는 폴더 하나만 지정하세요.")
        fd = _open_checked(args[0] if args else ".", perm, directory=True)
        try:
            entries = sorted(os.listdir(fd))
            if "a" not in option:
                entries = [entry for entry in entries if not entry.startswith(".")]
            return "\n".join(entries)[:MAX_OUT] or "(빈 폴더)"
        finally:
            os.close(fd)
    if len(args) != 1:
        raise RuntimeError("파일 하나를 지정하세요.")
    data = _read_bytes(args[0], perm)
    text = data.decode("utf-8", errors="replace")
    if name == "head":
        text = "\n".join(text.splitlines()[:10])
    elif name == "wc":
        if len(data) == MAX_OUT * 2:
            raise RuntimeError("wc는 16000바이트 미만의 파일만 지원합니다.")
        counts = {"-l": data.count(b"\n"), "-w": len(text.split()), "-c": len(data)}
        text = str(counts[option]) if option else f"{counts['-l']} {counts['-w']} {counts['-c']}"
    return text[:MAX_OUT] or "(출력 없음)"


def _run_cli(command: str, perm: str) -> str:
    if perm == "read":
        raise RuntimeError("읽기 권한에서는 범용 명령을 실행하지 않습니다.")
    argv = shlex.split(command)
    if argv and argv[0] in _CLI_NAMES:
        return _restricted_cli(command, perm)
    return _cli_result(sandbox_run(argv, perm, 45))


def _cli_result(proc: subprocess.CompletedProcess) -> str:
    output = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
    if proc.returncode:
        output += f"\nexit {proc.returncode}"
    return output[-MAX_OUT:] or "(출력 없음)"


def _sandbox_profile(permission: str, workspace: Path, scratch: Path, allow_ollama: bool, runtime_paths: list[Path] | None = None) -> str:
    def subpath(path: Path | str) -> str:
        return "(subpath " + json.dumps(str(Path(path).resolve())) + ")"

    def except_paths(paths: list[Path | str]) -> str:
        return "(require-all " + " ".join("(require-not " + subpath(p) + ")" for p in paths) + ")"

    # Metadata traversal is allowed; file contents and writes remain constrained.
    rules = ["(version 1)", "(deny default)",
             "(allow process-exec process-fork sysctl-read file-read-metadata file-read-data file-write* network-outbound)",
             "(allow signal (target same-sandbox))",
             '(allow mach-lookup (global-name "com.apple.mDNSResponder"))',
             "(deny appleevent-send)",
             '(deny process-exec (literal "/usr/bin/sudo") (literal "/usr/bin/su") (literal "/bin/su"))',
             '(deny mach-lookup (require-not (global-name "com.apple.mDNSResponder")))',
             '(deny network-outbound (remote unix-socket (require-not (path-literal "/private/var/run/mDNSResponder"))))']
    loopback = '(remote ip "localhost:*")'
    if allow_ollama:
        loopback = '(require-all ' + loopback + ' (require-not (remote tcp "localhost:11434")))'
    rules.append('(deny network-outbound ' + loopback + ')')
    blocked = [Path.home() / p for p in BLOCKED_HOME_DIRS] + [Path('/Library/Keychains')]
    rules.append('(deny file-read-data file-read-metadata file-write* ' + ' '.join(subpath(p) for p in blocked) + ')')
    if permission != 'machine':
        runtimes = ["/System", "/usr", "/bin", "/sbin", "/opt/homebrew", "/Library/Apple", "/private/var/db/dyld", "/dev"]
        read_filter = except_paths([workspace, scratch, *runtimes, *(runtime_paths or [])])
        # Root directory enumeration is needed by dyld; this does not grant file contents below it.
        read_filter = '(require-all ' + read_filter + ' (require-not (literal "/")))'
        rules.append('(deny file-read-data ' + read_filter + ')')
        write_paths = [scratch, '/dev/null', '/dev/fd']
        if permission != 'read':
            write_paths.append(workspace)
        rules.append('(deny file-write* ' + except_paths(write_paths) + ')')
    return '\n'.join(rules)


def _runtime_paths_for(executable: Path) -> list[Path]:
    """Read-only runtime roots for the selected executable, never its arbitrary parent."""
    executable = executable.resolve()
    paths = [executable]
    if executable == Path(sys.executable).resolve():
        # setup-python may install an interpreter/stdlib outside Homebrew or /usr.
        paths.append(Path(sys.base_prefix).resolve())
    for parent in executable.parents:
        if parent.name == 'Python.framework':
            relative = executable.relative_to(parent)
            if len(relative.parts) >= 3 and relative.parts[0] == 'Versions':
                paths.append(parent / 'Versions' / relative.parts[1])
            break
    uv_root = Path.home() / '.local/share/uv/tools'
    try:
        relative = executable.relative_to(uv_root)
        paths += [uv_root / relative.parts[0], Path.home() / '.local/share/uv/python']
    except ValueError:
        pass
    for parent in executable.parents:
        if parent.suffix == '.app':
            paths.append(parent)
            break
    return list(dict.fromkeys(paths))


def sandbox_run(argv: list[str], permission: str, timeout: float, *, allow_ollama: bool = False,
                extra_env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess:
    """Execute argv in a macOS kernel sandbox; unsupported platforms fail closed.

    Network to local control services and IPC is denied; agent runners may opt in
    only to Ollama's standard loopback port. Children inherit the sandbox.
    """
    extra_env = dict(extra_env or {})
    if any(key not in ('OPENCODE_CONFIG_CONTENT', 'OLLAMA_API_BASE') for key in extra_env):
        raise RuntimeError('허용되지 않은 CLI 환경 변수입니다.')
    if extra_env and not allow_ollama:
        raise RuntimeError('로컬 에이전트 설정은 Ollama 허용 실행에서만 전달합니다.')
    if 'OLLAMA_API_BASE' in extra_env and extra_env['OLLAMA_API_BASE'].rstrip('/') != 'http://127.0.0.1:11434':
        raise RuntimeError('Ollama 주소는 로컬 기본 주소여야 합니다.')
    if permission not in ('read', 'workspace', 'machine') or not argv:
        raise RuntimeError('명령 또는 권한이 올바르지 않습니다.')
    if sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file():
        raise RuntimeError('외부 CLI는 macOS sandbox-exec가 필요합니다. 제한된 로컬 명령만 사용할 수 있습니다.')
    executable = shutil.which(argv[0], path=connectors_mod._cli_path())
    if not executable:
        raise RuntimeError('실행 파일을 찾을 수 없습니다: ' + argv[0])
    if Path(executable).name in ('sudo', 'su'):
        raise RuntimeError('권한 상승 명령은 실행하지 않습니다.')
    workspace = _root_for(permission).resolve()
    with tempfile.TemporaryDirectory(prefix='desk-cli-') as temp:
        scratch = Path(temp).resolve()
        resolved_executable = Path(executable).resolve()
        runtime_paths = _runtime_paths_for(resolved_executable)
        profile = _sandbox_profile(permission, workspace, scratch, allow_ollama, runtime_paths)
        env = {'PATH': connectors_mod._cli_path(), 'HOME': str(scratch), 'TMPDIR': str(scratch),
               'LANG': 'en_US.UTF-8', 'TERM': 'dumb', **extra_env}
        command = ['/usr/bin/sandbox-exec', '-p', profile, str(Path(executable).resolve()), *argv[1:]]
        proc = subprocess.Popen(command, cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            stdout, stderr = proc.communicate(timeout=max(.001, timeout))
        except BaseException:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            raise
        finally:
            # A shell may exit after leaving background descendants. Do not retain them.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def _is_local_host(host: str) -> bool:
    host = (host or "").strip("[]").lower().rstrip(".")
    if not host or host in LOCAL_HOSTS or host.endswith(".localhost"):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not addr.is_global or bool(getattr(addr, "ipv4_mapped", None) and not addr.ipv4_mapped.is_global)


def _destinations(url: str, allow_local: bool = False) -> tuple[Any, list]:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise RuntimeError("인증 정보가 없는 http(s) URL만 됩니다.")
    if not allow_local and _is_local_host(parsed.hostname):
        raise RuntimeError("localhost 또는 사설 주소는 도구로 호출하지 않습니다.")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    if not addresses or (not allow_local and any(_is_local_host(item[4][0]) for item in addresses)):
        raise RuntimeError("localhost 또는 사설 주소로 해석되는 주소는 호출하지 않습니다.")
    return parsed, addresses


def _pinned_connection(parsed: Any, addresses: list) -> http.client.HTTPConnection:
    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port, timeout=20)

    def connect_validated(_address: Any, timeout: float = 20, source_address: Any = None) -> socket.socket:
        last = None
        for family, socktype, proto, _, destination in addresses:
            sock = socket.socket(family, socktype, proto)
            try:
                sock.settimeout(timeout)
                sock.connect(destination)  # Already-resolved sockaddr: no second DNS lookup.
                return sock
            except OSError as exc:
                last = exc
                sock.close()
        raise last or OSError("연결 가능한 주소가 없습니다.")

    connection._create_connection = connect_validated
    return connection


def _fetch(method: str, url: str, body: Any, headers: dict[str, str], allow_local: bool = False) -> str:
    data = None
    headers = dict(headers)
    if method == "POST":
        raw = body if isinstance(body, str) else json.dumps(body or {})
        data = str(raw).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    for _ in range(6):
        parsed, addresses = _destinations(url, allow_local)
        connection = _pinned_connection(parsed, addresses)
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308) and response.getheader("Location"):
                target = urljoin(url, response.getheader("Location"))
                next_parsed = urlparse(target)
                if (parsed.scheme, parsed.hostname, parsed.port) != (next_parsed.scheme, next_parsed.hostname, next_parsed.port):
                    headers = {k: v for k, v in headers.items() if k.lower() not in ("authorization", "cookie", "proxy-authorization", "host")}
                if response.status == 303 or (response.status in (301, 302) and method == "POST"):
                    method, data = "GET", None
                    headers = {k: v for k, v in headers.items() if k.lower() not in ("content-type", "content-length")}
                url = target
                continue
            if response.status >= 400:
                raise RuntimeError(f"HTTP {response.status}")
            return response.read(MAX_OUT * 2).decode("utf-8", errors="replace")[:MAX_OUT]
        finally:
            connection.close()
    raise RuntimeError("HTTP 리디렉션 횟수를 초과했습니다.")


def _http(method: str, url: str, body: Any, perm: str) -> str:
    method = (method or "GET").upper()
    if method not in ("GET", "POST"):
        raise RuntimeError("GET 또는 POST만 됩니다.")
    if perm == "read" and method != "GET":
        raise RuntimeError("읽기 권한에서는 GET만 됩니다.")
    return _fetch(method, url, body, {"User-Agent": "Free-AI-Scheduler"})


def _chrome(url: str, perm: str) -> str:
    # Browser navigation would resolve again and follow unvalidated redirects.
    page = _http("GET", url, None, perm)
    return f"주소에서 가져온 텍스트: {url}\n\n{page[:4000]}"


def _read_file(raw: str, perm: str) -> str:
    return _read_bytes(raw, perm).decode("utf-8", errors="replace")[:MAX_OUT]


def _log(text: str) -> None:
    connectors_mod.log(f"tools: {text}")
