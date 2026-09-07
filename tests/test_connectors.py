"""연동/MCP 클라이언트 검증 (stdlib unittest).

실행: python3 -m unittest tests.test_connectors -v
- MCPClient 를 tests/mcp_stub.py 에 붙여 initialize → tools/list → tools/call → timeout → close 를 확인한다.
- connectors.discover() 가 이 맥의 설정에서 후보를 찾고 env 값을 절대 노출하지 않는지 확인한다.
- tools.specs/run/system_prompt 의 이름 규칙과 권한 규칙을 확인한다.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desk import connectors, mcp_host, tools  # noqa: E402

STUB = ROOT / "tests" / "mcp_stub.py"


def stub_connector(name: str = "stub") -> dict:
    return {
        "id": "cstub0001", "kind": "mcp", "name": name, "enabled": True, "source": "manual", "origin": None,
        "transport": "stdio", "command": sys.executable, "args": [str(STUB)], "cwd": str(ROOT), "env_keys": [],
    }


class MCPStdioTest(unittest.TestCase):
    def test_full_roundtrip(self) -> None:
        client = mcp_host.MCPClient(stub_connector(), {}, timeout=10)
        client.start()
        proc = client.proc
        try:
            self.assertEqual(client.protocol_version, "2025-06-18")
            names = {t["name"] for t in client.list_tools()}
            self.assertEqual(names, {"echo", "add", "fail", "slow"})
            # 초기화 직후 서버가 보낸 notifications/message 를 건너뛰고 올바른 응답을 받는다
            self.assertEqual(client.call("echo", {"text": "안녕"}), "안녕")
            self.assertEqual(client.call("add", {"a": 2, "b": 3}), "5")
            with self.assertRaises(RuntimeError):
                client.call("fail", {})
            t0 = time.time()
            with self.assertRaises(mcp_host.MCPTimeout) as ctx:
                client.call("slow", {"seconds": 3}, timeout=1)
            self.assertLess(time.time() - t0, 2.5)
            self.assertIn("응답 없음", str(ctx.exception))
        finally:
            client.close()
        self.assertIsNone(client.proc)
        self.assertIsNotNone(proc.poll(), "close() 후에도 프로세스가 살아 있음")

    def test_bad_command_raises_and_cleans_up(self) -> None:
        con = stub_connector("missing")
        con["command"] = "/nonexistent/binary"
        client = mcp_host.MCPClient(con, {}, timeout=5)
        with self.assertRaises(RuntimeError):
            client.start()
        self.assertIsNone(client.proc)

    def test_open_for_job_skips_failures(self) -> None:
        good = stub_connector("good")
        bad = {**stub_connector("bad"), "id": "cbad00001", "command": "/nonexistent/binary"}
        disabled = {**stub_connector("off"), "id": "coff00001", "enabled": False}
        clients = mcp_host.open_for_job([good, bad, disabled, {"kind": "skill", "id": "cskill"}])
        try:
            self.assertEqual(set(clients), {"cstub0001"})
        finally:
            mcp_host.close_clients(clients)
        self.assertEqual(clients, {})


class ToolsTest(unittest.TestCase):
    def test_specs_and_run_with_mcp(self) -> None:
        con = stub_connector("Stub 서버")
        clients = mcp_host.open_for_job([con])
        try:
            cli = {"id": "ccli00001", "kind": "cli", "name": "echo-cli", "enabled": True, "command_template": "echo {text}",
                   "params": {"text": {"type": "string", "required": True, "description": "글"}}, "readonly": True, "timeout": 10}
            spec = tools.specs("workspace", ["cli"], [con, cli], clients)
            names = [t["function"]["name"] for t in spec]
            self.assertIn("run_cli", names)
            self.assertIn("read_file", names)
            self.assertIn("cli__echo-cli", names)
            self.assertIn("mcp__Stub__add", names)
            add_spec = next(t for t in spec if t["function"]["name"] == "mcp__Stub__add")
            self.assertEqual(add_spec["function"]["parameters"]["required"], ["a", "b"])
            self.assertEqual(tools.run("mcp__Stub__add", {"a": 1, "b": 2}, "workspace", clients), "3")
            self.assertTrue(tools.run("mcp__Stub__fail", {}, "workspace", clients).startswith("도구 실패:"))
            self.assertEqual(tools.run("cli__echo-cli", {"text": "hi there"}, "read", clients), "hi there")
            self.assertTrue(tools.run("nope", {}, "workspace", clients).startswith("모르는 도구"))
        finally:
            mcp_host.close_clients(clients)

    def test_read_permission_rules(self) -> None:
        spec = tools.specs("read", ["cli", "http"], [], {})
        names = {t["function"]["name"] for t in spec}
        self.assertNotIn("run_cli", names)
        self.assertIn("http_request", names)
        self.assertIn("도구 실패", tools.run("run_cli", {"command": "ls"}, "read", {}))
        self.assertIn("localhost", tools.run("http_request", {"url": "http://127.0.0.1:8788/api/status"}, "workspace", {}))
        self.assertEqual(tools.specs("workspace", [], [], {}), [])
        self.assertEqual([t["function"]["name"] for t in tools.specs("workspace", [], [{"kind": "cli", "id": "x", "name": "x",
                                                                                          "command_template": "ls"}], {})][:1], ["read_file"])

    def test_workspace_cli_blocks_outside_paths(self) -> None:
        self.assertIn("도구 실패", tools.run("run_cli", {"command": "cat ~/.ssh/id_rsa"}, "workspace", {}))
        self.assertIn("도구 실패", tools.run("run_cli", {"command": "ls /etc"}, "workspace", {}))
        self.assertIn("도구 실패", tools.run("run_cli", {"command": "cat ~/.ssh/config"}, "machine", {}))
        self.assertNotIn("도구 실패", tools.run("run_cli", {"command": "echo ok"}, "workspace", {}))

    def test_system_prompt_with_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "SKILL.md"
            skill.write_text("---\nname: demo\ndescription: 데모\n---\n\n# 본문\n\n" + "x" * 7000, encoding="utf-8")
            con = {"kind": "skill", "name": "demo", "enabled": True, "path": str(skill)}
            text = tools.system_prompt("read", 8, [con])
            self.assertIn("## 스킬: demo", text)
            self.assertIn("# 본문", text)
            self.assertNotIn("description: 데모", text)
            self.assertLess(len(text), 6000 + 400)


class DiscoverTest(unittest.TestCase):
    def test_discover_on_this_mac(self) -> None:
        cands = connectors.discover()
        by_source: dict[str, list[dict]] = {}
        for c in cands:
            by_source.setdefault(c["source"], []).append(c)
        codex = {c["name"] for c in by_source.get("codex", [])}
        claude = {c["name"]: c for c in by_source.get("claude-code", [])}
        self.assertTrue({"kakaopay", "node_repl"} <= codex, f"codex candidates: {codex}")
        self.assertIn("dingtalk", claude)
        self.assertEqual(claude["dingtalk"]["transport"], "http")
        self.assertIn("pricing-brain", claude)
        self.assertTrue(any(c["kind"] == "skill" for c in cands))
        dump = json.dumps(cands, ensure_ascii=False)
        self.assertNotIn("<redacted>", dump.replace("?<redacted>", ""))  # 후보엔 값 자체가 없어야 한다
        for c in cands:
            self.assertNotIn("env", c)
            self.assertNotIn("headers", c)
        self.assertEqual(claude["dingtalk"]["url"].split("?")[1], "<redacted>")
        self.assertIn("?key=", connectors.resolve_url(claude["dingtalk"]))  # 실행 시에만 origin 에서 복원
        kakaopay = next(c for c in by_source["codex"] if c["name"] == "kakaopay")
        self.assertIn("KAKAOPAY_SECRET_KEY", kakaopay["env_keys"])
        # env 값 유출 확인: 실제 값이 dump 에 없어야 한다
        env = connectors.resolve_env(kakaopay)
        for value in env.values():
            if len(value) >= 6:
                self.assertNotIn(value, dump)
        self.assertLessEqual(len(cands), connectors.MAX_CANDIDATES)
        kinds = [c["kind"] for c in cands]
        self.assertEqual(kinds, sorted(kinds, key=connectors.KINDS.index))

    def test_public_redacts(self) -> None:
        con = {"id": "c1", "kind": "mcp", "name": "d", "transport": "http", "url": "https://x.y/z?key=SECRET",
               "headers": {"Authorization": "Bearer abc"}}
        pub = connectors.public(con)
        self.assertEqual(pub["url"], "https://x.y/z?<redacted>")
        self.assertNotIn("headers", pub)
        self.assertEqual(pub["headers_keys"], ["Authorization"])

    def test_expand_vars(self) -> None:
        os.environ["DESK_TEST_VAR"] = "v1"
        self.assertEqual(connectors.expand_vars("${DESK_TEST_VAR}"), "v1")
        self.assertEqual(connectors.expand_vars("${DESK_NOPE:-dflt}"), "dflt")
        self.assertEqual(connectors.expand_vars("${DESK_NOPE}"), "")
        self.assertEqual(connectors.expand_vars("${CLAUDE_PLUGIN_ROOT}/x", {"CLAUDE_PLUGIN_ROOT": "/p"}), "/p/x")

    def test_sanitize_and_frontmatter(self) -> None:
        self.assertEqual(connectors.sanitize_name("한글 name!"), "name")
        self.assertEqual(connectors.sanitize_name("한글", "c1a2b3c4"), "cc1a2b3")
        meta, body = connectors.parse_frontmatter('---\nname: a\ndescription: "b: c"\nrefs:\n  - x\n---\nBODY')
        self.assertEqual(meta, {"name": "a", "description": "b: c"})
        self.assertEqual(body, "BODY")


class RegistryTest(unittest.TestCase):
    """data/ 를 임시 폴더로 바꿔 add/update/remove/이관을 확인한다."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        data = Path(self.tmp.name)
        self._saved = (connectors.FILE, connectors.LEGACY_FILE)
        connectors.FILE = data / "connectors.json"
        connectors.LEGACY_FILE = data / "mcp.json"

    def tearDown(self) -> None:
        connectors.FILE, connectors.LEGACY_FILE = self._saved
        self.tmp.cleanup()

    def test_crud_and_migration(self) -> None:
        connectors.LEGACY_FILE.write_text(json.dumps({"servers": {"old": {"command": "python3", "args": ["x.py"], "env": {"K": "V"}}}}))
        items = connectors.load()
        self.assertEqual([c["name"] for c in items], ["old"])
        self.assertEqual(items[0]["env"], {"K": "V"})
        self.assertTrue(connectors.LEGACY_FILE.with_suffix(".json.migrated").exists())
        self.assertNotIn("env", connectors.public(items[0]))
        con = connectors.add({"kind": "cli", "name": "gh-issues", "command_template": "gh issue list --repo {repo}",
                              "params": {"repo": {"required": True}}})
        self.assertTrue(con["id"].startswith("c"))
        con = connectors.update(con["id"], {"enabled": False})
        self.assertFalse(con["enabled"])
        with self.assertRaises(ValueError):
            connectors.add({"kind": "http", "name": "bad", "url_template": "ftp://x"})
        with self.assertRaises(ValueError):
            connectors.add({"kind": "nope", "name": "x"})
        with self.assertRaises(KeyError):
            connectors.update("cmissing", {})
        connectors.remove(con["id"])
        self.assertEqual(len(connectors.load()), 1)

    def test_import_candidate_and_test(self) -> None:
        cands = connectors.discover()
        skill = next(c for c in cands if c["kind"] == "skill")
        con = connectors.import_candidate(skill["candidate_key"])
        self.assertEqual(con["kind"], "skill")
        self.assertTrue(connectors.test(con["id"])["ok"])
        self.assertNotIn(skill["candidate_key"], {c["candidate_key"] for c in connectors.discover()})
        cli = connectors.add({"kind": "cli", "name": "ls", "command_template": "ls {dir}", "params": {"dir": {"default": "."}}})
        self.assertTrue(connectors.test(cli["id"])["ok"])
        stub = connectors.add(stub_connector())
        res = connectors.test(stub["id"])
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(res["tools"]), 4)
        self.assertEqual(len(connectors.get(stub["id"])["tools_cache"]), 4)


if __name__ == "__main__":
    unittest.main()
