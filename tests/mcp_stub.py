#!/usr/bin/env python3
"""Minimal MCP server over stdio (newline-delimited JSON-RPC 2.0) for local tests.

Tools: echo(text) -> text, add(a, b) -> a+b, fail() -> isError result, slow(seconds) -> sleeps.
Run: python3 tests/mcp_stub.py
"""
from __future__ import annotations

import json
import sys
import time

TOOLS = [
    {
        "name": "echo",
        "description": "받은 글을 그대로 돌려준다.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "돌려줄 글"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "두 수를 더한다.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
    {
        "name": "fail",
        "description": "항상 실패한다 (isError 테스트).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "slow",
        "description": "seconds 만큼 기다린다 (timeout 테스트).",
        "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"}}},
    },
]


def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def result(ident, payload) -> None:
    send({"jsonrpc": "2.0", "id": ident, "result": payload})


def error(ident, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}})


def call_tool(name: str, args: dict) -> dict:
    if name == "echo":
        return {"content": [{"type": "text", "text": str(args.get("text", ""))}]}
    if name == "add":
        total = float(args.get("a", 0)) + float(args.get("b", 0))
        return {"content": [{"type": "text", "text": str(int(total) if total.is_integer() else total)}]}
    if name == "fail":
        return {"content": [{"type": "text", "text": "의도된 실패"}], "isError": True}
    if name == "slow":
        time.sleep(float(args.get("seconds", 1)))
        return {"content": [{"type": "text", "text": "done"}]}
    raise KeyError(name)


def main() -> int:
    sys.stderr.write("mcp_stub: started\n")
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            error(None, -32700, "parse error")
            continue
        method = msg.get("method")
        ident = msg.get("id")
        if method == "initialize":
            requested = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
            result(
                ident,
                {
                    "protocolVersion": requested,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "mcp-stub", "version": "0.1"},
                },
            )
            # server-initiated notification the client must tolerate
            send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "data": "hello"}})
        elif method == "notifications/initialized":
            continue
        elif method == "ping":
            result(ident, {})
        elif method == "tools/list":
            result(ident, {"tools": TOOLS})
        elif method == "tools/call":
            params = msg.get("params") or {}
            try:
                result(ident, call_tool(str(params.get("name") or ""), params.get("arguments") or {}))
            except KeyError:
                error(ident, -32602, f"unknown tool: {params.get('name')}")
        elif ident is None:
            continue  # unknown notification
        else:
            error(ident, -32601, f"method not found: {method}")
    sys.stderr.write("mcp_stub: stdin closed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
