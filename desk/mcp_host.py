"""MCP 클라이언트 (stdio · Streamable HTTP).

stdio 전송은 줄바꿈 구분 JSON-RPC 2.0 (한 줄에 메시지 하나, Content-Length 헤더 없음).
reader 스레드가 stdout 을 읽어 id 별 대기열로 응답을 전달하고, 서버→클라이언트 요청
(ping, roots/list)에는 즉시 답한다. 모든 대기는 timeout 을 가진다 — cron 에서 영원히
매달리지 않기 위해서다.
"""
from __future__ import annotations

import atexit
import itertools
import json
import os
import queue
import re
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, BinaryIO

from desk.paths import LOGS_DIR, ensure_dirs

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "free-ai-scheduler", "version": "2.0"}
INIT_TIMEOUT = 60.0
LIST_TIMEOUT = 30.0
CALL_TIMEOUT = 120.0
MAX_LIST_PAGES = 50
MAX_RESULT_CHARS = 8000
BASE_PATH = ("/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local" / "bin"), "/usr/bin", "/bin", "/usr/sbin", "/sbin")

_OPEN: dict[int, "MCPClient"] = {}
_OPEN_LOCK = threading.Lock()


class MCPError(RuntimeError):
    """MCP 서버가 오류를 돌려주거나 프로토콜이 어긋났다."""


class MCPTimeout(MCPError):
    """지정 시간 안에 응답이 없다."""


class MCPClosed(MCPError):
    """서버 프로세스/세션이 끊겼다."""


def _slug(name: str) -> str:
    """로그 파일명용 ASCII 슬러그."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(name))[:60].strip("_") or "server"


def _redact(url: str) -> str:
    from desk.connectors import redact_url

    return redact_url(url)


def _resolve_url(con: dict) -> str:
    """origin 파일에서 쿼리(시크릿) 포함 실제 URL 을 다시 읽는다."""
    from desk.connectors import resolve_url

    return resolve_url(con)


def render_content(result: dict[str, Any], limit: int = MAX_RESULT_CHARS) -> str:
    """tools/call 결과의 content[] 를 한 문자열로. 이미지/리소스는 짧은 자리표시자."""
    parts: list[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind in ("image", "audio"):
            parts.append(f"[{kind} {item.get('mimeType', '?')}]")
        elif kind == "resource":
            res = item.get("resource") or {}
            text = res.get("text") if isinstance(res.get("text"), str) else None
            parts.append(f"[resource {res.get('uri', '')}]" + (f"\n{text}" if text else ""))
        elif kind == "resource_link":
            parts.append(f"[resource {item.get('uri', '')}]")
        else:
            parts.append(json.dumps(item, ensure_ascii=False)[:500])
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    return ("\n".join(parts).strip() or "(빈 결과)")[:limit]


def build_env(extra: dict[str, str] | None) -> dict[str, str]:
    """stdio 서버용 최소 환경: PATH(Homebrew 포함) · HOME · LANG 에 연동 env 를 덧씌운다."""
    path_parts = list(BASE_PATH) + [p for p in os.environ.get("PATH", "").split(":") if p]
    env = {
        "PATH": ":".join(dict.fromkeys(path_parts)),
        "HOME": str(Path.home()),
        "LANG": "en_US.UTF-8",
        "TERM": "dumb",
    }
    for key in ("USER", "LOGNAME", "TMPDIR", "SSH_AUTH_SOCK"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    env.update({str(k): str(v) for k, v in (extra or {}).items() if v is not None})
    return env


class MCPClient:
    """연동(kind=mcp) 하나에 대한 MCP 세션. stdio 또는 Streamable HTTP."""

    def __init__(self, con: dict, env: dict[str, str], timeout: float = 30.0, headers: dict[str, str] | None = None):
        self.con = con
        self.name = str(con.get("name") or "mcp")
        self.env = dict(env or {})
        self.headers = dict(headers or {})
        self.timeout = float(timeout)
        self.transport = str(con.get("transport") or "stdio")
        self.proc: subprocess.Popen | None = None
        self.protocol_version = PROTOCOL_VERSION
        self.server_info: dict[str, Any] = {}
        self.instructions = ""
        self._session_id: str | None = None
        self._url = str(con.get("url") or "")
        self._ids = itertools.count(1)
        self._pending: dict[Any, queue.Queue] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._log: BinaryIO | None = None
        self._tools: list[dict] | None = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """서버를 띄우고(stdio) initialize + notifications/initialized 까지 마친다."""
        ensure_dirs()
        self._log = open(LOGS_DIR / f"mcp-{_slug(self.name)}.log", "ab", buffering=0)
        try:
            if self.transport == "http":
                self._url = _resolve_url(self.con)
                if not self._url.startswith(("http://", "https://")):
                    raise MCPError(f"MCP 서버 '{self.name}' 주소가 비었습니다.")
            else:
                self._spawn()
            self._initialize()
        except BaseException:
            self.close()
            raise
        with _OPEN_LOCK:
            _OPEN[id(self)] = self

    def _spawn(self) -> None:
        cmd = [str(self.con.get("command") or "")] + [str(a) for a in (self.con.get("args") or [])]
        if not cmd[0]:
            raise MCPError(f"MCP 서버 '{self.name}' 명령이 비었습니다.")
        cwd = str(self.con.get("cwd") or Path.home())
        if not Path(cwd).is_dir():
            cwd = str(Path.home())
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._log,
                cwd=cwd,
                env=build_env(self.env),
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            raise MCPError(f"MCP 서버 '{self.name}' 실행 실패: {exc}") from exc
        self._reader = threading.Thread(target=self._reader_loop, name=f"mcp-{self.name}", daemon=True)
        self._reader.start()

    def _initialize(self) -> None:
        params = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO}
        result = self.request("initialize", params, timeout=max(self.timeout, INIT_TIMEOUT))
        self.protocol_version = str(result.get("protocolVersion") or PROTOCOL_VERSION)
        self.server_info = result.get("serverInfo") or {}
        self.instructions = str(result.get("instructions") or "")
        self.notify("notifications/initialized")
        self._log_line(f"initialized {self.server_info} protocol={self.protocol_version}")

    def close(self) -> None:
        """stdin 닫기 → wait(2s) → terminate → wait(2s) → kill. 좀비를 남기지 않는다."""
        with _OPEN_LOCK:
            _OPEN.pop(id(self), None)
        proc, self.proc = self.proc, None
        if proc is not None:
            self._shutdown(proc)
        self._fail_pending(MCPClosed(f"MCP 서버 '{self.name}' 세션이 닫혔습니다."))
        if self._reader is not None:
            self._reader.join(timeout=1.0)
            self._reader = None
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None

    def _shutdown(self, proc: subprocess.Popen) -> None:
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        for sig in (None, signal.SIGTERM, signal.SIGKILL):
            if sig is not None:
                _signal_group(proc, sig)
            try:
                proc.wait(timeout=2.0)
                break
            except subprocess.TimeoutExpired:
                continue
        try:
            if proc.stdout:
                proc.stdout.close()
        except OSError:
            pass

    # ------------------------------------------------------------ public API

    def list_tools(self) -> list[dict]:
        """tools/list (nextCursor 페이지네이션 포함). [{name, description, inputSchema, annotations}]"""
        if self._tools is not None:
            return self._tools
        tools: list[dict] = []
        cursor: str | None = None
        for _ in range(MAX_LIST_PAGES):
            params: dict[str, Any] = {"cursor": cursor} if cursor else {}
            result = self.request("tools/list", params, timeout=max(self.timeout, LIST_TIMEOUT))
            for tool in result.get("tools") or []:
                if isinstance(tool, dict) and tool.get("name"):
                    tools.append({
                        "name": str(tool["name"]),
                        "description": str(tool.get("description") or tool.get("title") or ""),
                        "inputSchema": tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {},
                        "annotations": tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {},
                    })
            cursor = result.get("nextCursor") or None
            if not cursor:
                break
        self._tools = tools
        return tools

    def call(self, tool: str, arguments: dict, timeout: float = CALL_TIMEOUT) -> str:
        """tools/call. content 를 문자열로 합쳐 돌려준다. isError 면 RuntimeError."""
        result = self.request("tools/call", {"name": tool, "arguments": arguments or {}}, timeout=timeout)
        text = render_content(result)
        if result.get("isError"):
            raise MCPError(f"{tool}: {text}")
        return text

    # ------------------------------------------------------------ JSON-RPC

    def request(self, method: str, params: dict | None, timeout: float) -> dict:
        """요청 하나를 보내고 같은 id 의 응답을 timeout 안에 받는다."""
        ident = next(self._ids)
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": ident, "method": method}
        if params is not None:
            msg["params"] = params
        if self.transport == "http":
            reply = self._http_roundtrip(msg, timeout, expect_id=ident)
        else:
            reply = self._stdio_roundtrip(msg, timeout)
        if "error" in reply:
            err = reply.get("error") or {}
            raise MCPError(f"MCP 서버 '{self.name}' 오류 ({method}): {err.get('message') or err} [{err.get('code')}]")
        if "result" not in reply or not isinstance(reply["result"], dict):
            raise MCPError(f"MCP 서버 '{self.name}' 응답 형식이 이상합니다 ({method}).")
        return reply["result"]

    def notify(self, method: str, params: dict | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if self.transport == "http":
            self._http_roundtrip(msg, min(self.timeout, 10.0), expect_id=None)
        else:
            self._send(msg)

    def _stdio_roundtrip(self, msg: dict, timeout: float) -> dict:
        ident = msg["id"]
        waiter: queue.Queue = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[ident] = waiter
        try:
            self._send(msg)
            reply = waiter.get(timeout=timeout)
        except queue.Empty:
            with self._pending_lock:
                self._pending.pop(ident, None)
            if msg["method"] != "initialize":
                self._try_notify("notifications/cancelled", {"requestId": ident, "reason": f"client timeout {timeout:.0f}s"})
            raise MCPTimeout(f"MCP 서버 '{self.name}' 응답 없음 ({timeout:.0f}s)")
        except BaseException:
            with self._pending_lock:
                self._pending.pop(ident, None)
            raise
        if isinstance(reply, Exception):
            raise reply
        return reply

    def _try_notify(self, method: str, params: dict) -> None:
        try:
            self.notify(method, params)
        except MCPError:
            pass

    def _send(self, msg: dict) -> None:
        line = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
        data = (line + "\n").encode("utf-8")
        with self._write_lock:
            proc = self.proc
            if proc is None or proc.stdin is None or proc.poll() is not None:
                raise MCPClosed(f"MCP 서버 '{self.name}'가 끊겼습니다. {self._stderr_tail()}".rstrip())
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise MCPClosed(f"MCP 서버 '{self.name}'에 쓸 수 없습니다: {exc}") from exc

    # ------------------------------------------------------------ reader thread

    def _reader_loop(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        try:
            for raw in proc.stdout:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._log_line(f"non-JSON stdout skipped: {raw[:200]!r}")
                    continue
                for item in (msg if isinstance(msg, list) else [msg]):
                    if isinstance(item, dict):
                        self._dispatch(item)
        except (OSError, ValueError):
            pass
        self._fail_pending(MCPClosed(f"MCP 서버 '{self.name}'가 끊겼습니다. {self._stderr_tail()}".rstrip()))

    def _dispatch(self, msg: dict) -> None:
        if "method" in msg:
            if msg.get("id") is not None:
                self._answer_server_request(msg)
            else:
                if msg["method"] == "notifications/tools/list_changed":
                    self._tools = None
                self._log_line(f"notification {msg['method']}")
            return
        with self._pending_lock:
            waiter = self._pending.pop(msg.get("id"), None)
        if waiter is None:
            self._log_line(f"orphan response id={msg.get('id')!r}")
            return
        waiter.put(msg)

    def _answer_server_request(self, msg: dict) -> None:
        method, ident = msg["method"], msg["id"]
        if method == "ping":
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": ident, "result": {}}
        elif method == "roots/list":
            reply = {"jsonrpc": "2.0", "id": ident, "result": {"roots": []}}
        else:
            reply = {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": f"Method not found: {method}"}}
        try:
            self._send(reply)
        except MCPError:
            pass

    def _fail_pending(self, exc: Exception) -> None:
        with self._pending_lock:
            pending, self._pending = self._pending, {}
        for waiter in pending.values():
            try:
                waiter.put_nowait(exc)
            except queue.Full:
                pass

    # ------------------------------------------------------------ Streamable HTTP

    def _http_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "Free-AI-Scheduler",
            **self.headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self.server_info:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    def _http_roundtrip(self, msg: dict, timeout: float, expect_id: Any) -> dict:
        deadline = time.monotonic() + max(0.001, timeout)
        url = self._url
        body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=self._http_headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self._session_id = sid
                if expect_id is None:
                    return {}
                return self._http_read_reply(resp, expect_id, timeout, deadline)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise MCPError(f"인증 필요: {_redact(url)}") from exc
            if expect_id is None and exc.code in (202, 204, 405):
                return {}
            detail = exc.read(300).decode("utf-8", errors="replace") if exc.fp else ""
            raise MCPError(f"MCP 서버 '{self.name}' HTTP {exc.code}: {detail}".strip()) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise MCPTimeout(f"MCP 서버 '{self.name}' 응답 없음 ({timeout:.0f}s)") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise MCPClosed(f"MCP 서버 '{self.name}' 연결 실패: {getattr(exc, 'reason', exc)}") from exc

    def _http_read_reply(self, resp: Any, expect_id: Any, timeout: float, deadline: float | None = None) -> dict:
        deadline = deadline if deadline is not None else time.monotonic() + timeout
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype == "text/event-stream":
            return self._read_sse(resp, expect_id, timeout, deadline)
        raw = b""
        for chunk in self._http_chunks(resp, deadline, timeout):
            raw += chunk
            if len(raw) > 8 * 1024 * 1024:
                raise MCPError("MCP 응답이 8 MiB 제한을 초과했습니다.")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MCPError(f"MCP 서버 '{self.name}' 응답이 JSON 이 아닙니다.") from exc
        reply = self._pick_reply(payload if isinstance(payload, list) else [payload], expect_id)
        if reply is None:
            raise MCPError(f"MCP 서버 '{self.name}' 응답에 id={expect_id} 가 없습니다.")
        return reply

    def _http_chunks(self, resp: Any, deadline: float, timeout: float):
        """One socket read per iteration, including partial SSE lines, within one deadline."""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPTimeout(f"MCP 서버 '{self.name}' 응답 없음 ({timeout:g}s)")
            sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
            if sock is not None:
                sock.settimeout(remaining)
            chunk = resp.read1(4096)
            if time.monotonic() >= deadline:
                raise MCPTimeout(f"MCP 서버 '{self.name}' 응답 없음 ({timeout:g}s)")
            if not chunk:
                return
            yield chunk

    def _read_sse(self, resp: Any, expect_id: Any, timeout: float, deadline: float | None = None) -> dict:
        """Heartbeats and partial lines do not reset the total response deadline."""
        deadline = deadline if deadline is not None else time.monotonic() + timeout
        data_lines: list[str] = []
        pending = b""
        size = 0
        try:
            for chunk in self._http_chunks(resp, deadline, timeout):
                pending += chunk
                size += len(chunk)
                if size > 8 * 1024 * 1024:
                    raise MCPError("MCP SSE 응답이 8 MiB 제한을 초과했습니다.")
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    text = line.decode("utf-8", errors="replace").rstrip("\r")
                    if text.startswith("data:"):
                        data_lines.append(text[5:].lstrip())
                    elif text == "" and data_lines:
                        reply = self._sse_event(data_lines, expect_id)
                        data_lines = []
                        if reply is not None:
                            return reply
        except (socket.timeout, TimeoutError) as exc:
            raise MCPTimeout(f"MCP 서버 '{self.name}' 응답 없음 ({timeout:g}s)") from exc
        if pending.startswith(b"data:"):
            data_lines.append(pending[5:].decode("utf-8", errors="replace").strip())
        if data_lines:
            reply = self._sse_event(data_lines, expect_id)
            if reply is not None:
                return reply
        raise MCPClosed(f"MCP 서버 '{self.name}' 스트림이 응답 없이 끝났습니다.")

    def _sse_event(self, data_lines: list[str], expect_id: Any) -> dict | None:
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            self._log_line(f"non-JSON SSE event skipped: {data_lines[:1]!r}")
            return None
        return self._pick_reply(payload if isinstance(payload, list) else [payload], expect_id)

    def _pick_reply(self, items: list[Any], expect_id: Any) -> dict | None:
        """응답 목록에서 id 일치 항목을 고른다. 알림은 로그, 서버 요청은 무시(HTTP 는 답할 채널이 없음)."""
        for item in items:
            if not isinstance(item, dict):
                continue
            if "method" in item:
                self._log_line(f"server message {item['method']} (http)")
                continue
            if item.get("id") == expect_id:
                return item
        return None

    # ------------------------------------------------------------ logging

    def _log_line(self, text: str) -> None:
        if self._log is None:
            return
        try:
            self._log.write(f"[client] {text}\n".encode("utf-8", errors="replace"))
        except (OSError, ValueError):
            pass

    def _stderr_tail(self, limit: int = 300) -> str:
        """서버 로그 파일 끝부분(진단용)."""
        try:
            path = LOGS_DIR / f"mcp-{_slug(self.name)}.log"
            data = path.read_bytes()[-limit:]
            tail = data.decode("utf-8", errors="replace").strip()
            return f"(로그: {tail[-limit:]})" if tail else ""
        except OSError:
            return ""


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        proc.send_signal(sig)


def _client_log(name: str, text: str) -> None:
    try:
        ensure_dirs()
        with (LOGS_DIR / f"mcp-{_slug(name)}.log").open("ab") as fh:
            fh.write(f"[client] {text}\n".encode("utf-8", errors="replace"))
    except OSError:
        pass


def open_for_job(cons: list[dict]) -> dict[str, MCPClient]:
    """kind=mcp & enabled 연동만 기동. 하나가 실패해도 나머지는 계속(실패는 로그, 결과에서 제외)."""
    from desk import connectors

    clients: dict[str, MCPClient] = {}
    for con in cons:
        if con.get("kind") != "mcp" or not con.get("enabled", True) or not con.get("id"):
            continue
        timeout = float(con.get("startup_timeout_sec") or 30.0)
        client = MCPClient(con, connectors.resolve_env(con), timeout=timeout, headers=connectors.resolve_headers(con))
        try:
            client.start()
        except Exception as exc:
            _client_log(client.name, f"start failed: {exc}")
            continue
        clients[str(con["id"])] = client
    return clients


def close_clients(clients: dict[str, MCPClient]) -> None:
    """open_for_job 결과만 닫는다(같은 프로세스의 다른 실행에 영향 없음)."""
    for client in list(clients.values()):
        try:
            client.close()
        except Exception as exc:
            _client_log(client.name, f"close failed: {exc}")
    clients.clear()


def close_all() -> None:
    """이 프로세스에서 열린 모든 MCP 세션을 닫는다."""
    with _OPEN_LOCK:
        clients = list(_OPEN.values())
    for client in clients:
        try:
            client.close()
        except Exception as exc:
            _client_log(client.name, f"close failed: {exc}")


atexit.register(close_all)
