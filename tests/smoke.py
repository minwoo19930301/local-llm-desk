"""통합 스모크 테스트 (CONTRACT §4 1~6, Ollama 모델·사용자 crontab 없이 돌 수 있는 범위).

실행: python3 -m unittest tests.smoke   또는   python3 tests/smoke.py

- 모듈 임포트, cronwhen, ramgate, connectors.discover(비밀값 유출 검사), MCP stdio 왕복(tests/mcp_stub.py)
- HTTP 서버를 8799 포트 서브프로세스로 띄워 API 형태·CSRF·연동 import/삭제·작업 생성(preset save)/삭제를 확인
- 알림 테스트(POST /api/alerts/test)는 실제 알림을 띄우므로 호출하지 않고 alerts.test 가 callable 인지만 본다
- data/jobs.json · data/connectors.json 은 바이트 단위로 백업하고 끝나면 원복(없던 파일은 지운다)
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desk import alerts, connectors, cronwhen, crontab_sync, mcp_host, ramgate  # noqa: E402
from desk.paths import CONNECTORS_FILE, DATA, JOBS_FILE  # noqa: E402

SEOUL = ZoneInfo("Asia/Seoul")
STUB = ROOT / "tests" / "mcp_stub.py"
PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"
CSRF = {"X-Requested-With": "free-ai-scheduler"}
HOME = Path.home()


# --- 보조 -------------------------------------------------------------------


class FileBackup:
    """파일 바이트를 기억해 두고 restore() 로 되돌린다. 없던 파일은 지운다."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.existed = path.exists()
        self.data = path.read_bytes() if self.existed else b""

    def restore(self) -> None:
        if self.existed:
            self.path.write_bytes(self.data)
        elif self.path.exists():
            self.path.unlink()


def http(method: str, path: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, Any]:
    """(status, json). HTTPError 도 (status, json) 으로 돌려준다."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        with exc:
            raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"error": raw}


def _looks_like_path(value: str) -> bool:
    """env 값이 이 맥에 실제로 있는 경로면 비밀이 아니다(origin.file 경로와 겹쳐 오탐한다)."""
    return value.startswith(("/", "~")) and Path(value).expanduser().exists()


def real_secret_values() -> set[str]:
    """이 맥의 MCP 설정에 실제로 들어 있는 env/헤더 값(6자 이상, 경로 제외). API 응답에 하나라도 나오면 유출."""
    values: set[str] = set()
    for cand in connectors.discover():
        if cand.get("kind") != "mcp":
            continue
        for value in {**connectors.resolve_env(cand), **connectors.resolve_headers(cand)}.values():
            if len(value) >= 6 and value not in os.environ.values() and not _looks_like_path(value):
                values.add(value)
    return values


def stub_connector(name: str = "smoke-stub") -> dict:
    return {
        "id": "csmoke01", "kind": "mcp", "name": name, "enabled": True, "source": "manual", "origin": None,
        "transport": "stdio", "command": sys.executable, "args": [str(STUB)], "cwd": str(ROOT), "env_keys": [],
    }


# --- 1. 임포트 ---------------------------------------------------------------


class ImportsTest(unittest.TestCase):
    def test_all_modules_import(self) -> None:
        import importlib

        for name in ("connectors", "mcp_host", "tools", "cronwhen", "ramgate", "runner", "http_api", "jobs",
                     "alerts", "state", "crontab_sync", "links", "catalog", "installer", "hardware", "ollama_ctl", "paths"):
            importlib.import_module(f"desk.{name}")

    def test_alerts_test_is_callable(self) -> None:
        self.assertTrue(callable(alerts.test))  # 실제 호출은 알림을 띄우므로 하지 않는다


# --- 2. cronwhen ---------------------------------------------------------------


class CronwhenTest(unittest.TestCase):
    def test_weekday_0930(self) -> None:
        now = datetime.now(SEOUL)
        first = cronwhen.next_runs("30 9 * * 1-5", now, count=1)[0]
        self.assertGreater(first, now)
        self.assertEqual((first.hour, first.minute), (9, 30))
        self.assertLess(first.weekday(), 5)
        self.assertEqual(first.tzinfo.key, "Asia/Seoul")  # type: ignore[union-attr]

    def test_every_15_minutes(self) -> None:
        runs = cronwhen.next_runs("*/15 * * * *", datetime.now(SEOUL), count=8)
        gaps = {int((b - a).total_seconds()) for a, b in zip(runs, runs[1:])}
        self.assertEqual(gaps, {900})

    def test_day_31_only(self) -> None:
        runs = cronwhen.next_runs("0 0 31 * *", datetime(2026, 9, 4, tzinfo=SEOUL), count=6)
        self.assertEqual([r.day for r in runs], [31] * 6)
        self.assertEqual([r.month for r in runs], [10, 12, 1, 3, 5, 7])

    def test_validity_and_describe(self) -> None:
        self.assertTrue(cronwhen.is_valid("30 9 * * 1-5"))
        for bad in ("99 * * * *", "a b c d e", "* * * *", "* * * * */0"):
            self.assertFalse(cronwhen.is_valid(bad), bad)
        self.assertEqual(cronwhen.describe("0 17 * * *"), "매일 17:00")
        self.assertEqual(cronwhen.describe("30 9 * * 1-5"), "평일 09:30")
        self.assertEqual(cronwhen.describe("*/5 * * * *"), "5분마다")


# --- 3. ramgate ----------------------------------------------------------------


class RamgateTest(unittest.TestCase):
    def test_snapshot_shape(self) -> None:
        snap = ramgate.snapshot()
        for key in ("total_gb", "free_gb", "avail_gb", "pressure_pct", "pressure_level", "swap_used_gb", "swap_total_gb", "swap_warn", "loaded", "at"):
            self.assertIn(key, snap)
        self.assertGreater(snap["free_gb"], 0)
        self.assertGreater(snap["total_gb"], snap["free_gb"])
        self.assertIsInstance(snap["loaded"], list)

    def test_need_gb(self) -> None:
        need = ramgate.model_need_gb("llama3.2:3b")
        self.assertTrue(2.5 <= need <= 4.5, need)  # ≈3.5GB (W+KV+C+R)
        self.assertGreater(ramgate.model_need_gb("llama3.2:3b", 16384), need)
        self.assertEqual(ramgate.model_need_gb("no-such-model:0b"), 4.0)

    def test_decide_big_model(self) -> None:
        verdict = ramgate.decide("qwen3.6:35b", "downgrade")
        for key in ("action", "model", "free_gb", "avail_gb", "need_gb", "reason"):
            self.assertIn(key, verdict)
        self.assertIn(verdict["action"], ("downgrade", "skip"))
        self.assertTrue(verdict["reason"])
        self.assertEqual(ramgate.decide("qwen3.6:35b", "skip")["action"], "skip")


# --- 4. connectors.discover -------------------------------------------------------


class DiscoverTest(unittest.TestCase):
    def test_discover_contents_and_no_secrets(self) -> None:
        cands = connectors.discover()
        self.assertTrue(cands)
        for cand in cands:
            self.assertNotIn("env", cand)
            self.assertNotIn("headers", cand)
            self.assertIn("candidate_key", cand)
            self.assertEqual(cand["candidate_key"], f"{cand['source']}:{cand['origin']['file']}:{cand['origin']['key']}")
        by_source: dict[str, set[str]] = {}
        for cand in cands:
            by_source.setdefault(cand["source"], set()).add(cand["name"])
        if (HOME / ".codex" / "config.toml").exists():
            self.assertTrue({"kakaopay", "node_repl"} <= by_source.get("codex", set()), by_source.get("codex"))
        if (HOME / ".claude.json").exists():
            dingtalk = [c for c in cands if c["source"] == "claude-code" and c["name"] == "dingtalk"]
            self.assertTrue(dingtalk)
            self.assertEqual(dingtalk[0]["transport"], "http")
            self.assertTrue(dingtalk[0]["url"].endswith("?<redacted>") or "?" not in dingtalk[0]["url"])
        dump = json.dumps(cands, ensure_ascii=False)
        for value in real_secret_values():
            self.assertNotIn(value, dump)


# --- 5. MCP stdio 왕복 -------------------------------------------------------------


class MCPStdioTest(unittest.TestCase):
    def test_roundtrip_with_stub(self) -> None:
        client = mcp_host.MCPClient(stub_connector(), {}, timeout=10)
        client.start()
        proc = client.proc
        try:
            self.assertEqual(client.protocol_version, "2025-06-18")
            self.assertEqual({t["name"] for t in client.list_tools()}, {"echo", "add", "fail", "slow"})
            self.assertEqual(client.call("echo", {"text": "안녕"}), "안녕")
            self.assertEqual(client.call("add", {"a": 2, "b": 3}), "5")
            with self.assertRaises(RuntimeError):
                client.call("fail", {})
            began = time.time()
            with self.assertRaises(mcp_host.MCPTimeout):
                client.call("slow", {"seconds": 3}, timeout=1)
            self.assertLess(time.time() - began, 2.5)
        finally:
            client.close()
        self.assertIsNotNone(proc.poll(), "close() 후에도 stub 프로세스가 살아 있음")

    def test_open_for_job_and_tools_dispatch(self) -> None:
        from desk import tools

        con = stub_connector()
        clients = mcp_host.open_for_job([con, {**con, "id": "cbad", "name": "bad", "command": "/nonexistent/bin"}])
        try:
            self.assertEqual(set(clients), {con["id"]})
            spec = tools.specs("workspace", [], [con], clients)
            names = {t["function"]["name"] for t in spec}
            self.assertIn("mcp__smoke-stub__add", names)
            self.assertEqual(tools.run("mcp__smoke-stub__add", {"a": 1, "b": 2}, "workspace", clients, timeout=30), "3")
            self.assertTrue(tools.run("mcp__smoke-stub__fail", {}, "workspace", clients).startswith("도구 실패"))
        finally:
            mcp_host.close_clients(clients)
        self.assertEqual(clients, {})


# --- 6. HTTP 서버 ---------------------------------------------------------------


class HttpApiTest(unittest.TestCase):
    """8799 포트에 서버를 띄워 API 형태를 확인한다. 사용자 포트(8788)와 crontab 은 건드리지 않는다."""

    proc: subprocess.Popen | None = None
    backups: list[FileBackup] = []
    log_path = Path(os.environ.get("DESK_SMOKE_LOG") or (ROOT / "data" / "logs" / "smoke-server.log"))

    @classmethod
    def setUpClass(cls) -> None:
        cls.backups = [FileBackup(JOBS_FILE), FileBackup(CONNECTORS_FILE)]
        cls.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(cls.log_path, "ab")
        cls.proc = subprocess.Popen(
            [sys.executable, "-c", f"from desk import http_api; http_api.PORT={PORT}; http_api.serve(open_browser=False)"],
            cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        log.close()
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if http("GET", "/api/status")[0] == 200:
                    return
            except (urllib.error.URLError, OSError):
                pass
            if cls.proc.poll() is not None:
                break
            time.sleep(0.3)
        cls.tearDownClass()
        raise RuntimeError(f"서버가 {PORT} 포트에서 올라오지 않았습니다 (로그: {cls.log_path})")

    @classmethod
    def tearDownClass(cls) -> None:
        proc, cls.proc = cls.proc, None
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(5)
        for backup in cls.backups:
            backup.restore()

    def test_status(self) -> None:
        code, data = http("GET", "/api/status")
        self.assertEqual(code, 200)
        for key in ("hardware", "ollama", "ready", "alerts", "ram", "active", "data_dir", "port"):
            self.assertIn(key, data)
        self.assertEqual(data["port"], PORT)
        self.assertEqual(data["data_dir"], str(DATA))
        self.assertEqual(set(data["alerts"]), {"macos", "sound", "webhook"})
        self.assertGreater(data["ram"]["free_gb"], 0)

    def test_ram(self) -> None:
        code, data = http("GET", "/api/ram?model=llama3.2:3b&num_ctx=8192")
        self.assertEqual(code, 200)
        self.assertIn("snapshot", data)
        self.assertGreater(data["need_gb"], 0)
        self.assertIn(data["verdict"]["action"], ("run", "wait", "skip", "downgrade"))
        code, data = http("GET", "/api/ram")
        self.assertEqual(code, 200)
        self.assertNotIn("need_gb", data)

    def test_jobs_and_timeline(self) -> None:
        code, data = http("GET", "/api/jobs")
        self.assertEqual(code, 200)
        self.assertIn("jobs", data)
        self.assertIn("crontab", data)
        for job in data["jobs"]:
            self.assertIn("schedule_label", job)
            self.assertIn("next_run", job)
        code, tl = http("GET", "/api/timeline?hours=24")
        self.assertEqual(code, 200)
        for key in ("now", "from", "to", "ram", "active", "lanes", "conflicts"):
            self.assertIn(key, tl)
        self.assertEqual(len(tl["lanes"]), len(data["jobs"]))
        span = datetime.fromisoformat(tl["to"]) - datetime.fromisoformat(tl["from"])
        self.assertEqual(int(span.total_seconds()), 24 * 3600)
        code, runs = http("GET", "/api/runs?limit=5")
        self.assertEqual(code, 200)
        self.assertLessEqual(len(runs["runs"]), 5)
        for row in runs["runs"]:
            self.assertIn("status", row)
            self.assertIn("status_label", row)
            self.assertNotEqual(row.get("job_id"), "try")

    def test_connectors_list_has_no_secrets(self) -> None:
        code, data = http("GET", "/api/connectors")
        self.assertEqual(code, 200)
        self.assertIn("connectors", data)
        self.assertIn("discovered", data)
        dump = json.dumps(data, ensure_ascii=False)
        for con in data["connectors"] + data["discovered"]:
            self.assertNotIn("env", con)
            if con.get("kind") != "http":
                self.assertNotIn("headers", con)
        for value in real_secret_values():
            self.assertNotIn(value, dump)

    def test_csrf_and_import_delete_roundtrip(self) -> None:
        code, data = http("POST", "/api/connectors", {"import": "x"})
        self.assertEqual(code, 403)
        self.assertEqual(data, {"error": "허용되지 않은 요청"})
        code, data = http("POST", "/api/connectors", {"import": "x"}, {**CSRF, "Origin": "http://evil.example"})
        self.assertEqual(code, 403)
        code, data = http("POST", "/api/connectors", {}, CSRF)
        self.assertEqual(code, 400)

        _, listing = http("GET", "/api/connectors")
        discovered = listing["discovered"]
        if not discovered:
            self.skipTest("이 맥에서 발견된 연동 후보가 없습니다")
        pick = next((c for c in discovered if c["kind"] == "skill"), discovered[0])
        code, data = http("POST", "/api/connectors", {"import": pick["candidate_key"]}, CSRF)
        self.assertEqual(code, 200, data)
        con = data["connector"]
        self.assertTrue(con["id"].startswith("c"))
        self.assertEqual(con["name"], pick["name"])
        self.assertNotIn("env", con)
        self.assertNotIn("candidate_key", con)

        _, listing = http("GET", "/api/connectors")
        self.assertIn(con["id"], {c["id"] for c in listing["connectors"]})
        self.assertNotIn(pick["candidate_key"], {c["candidate_key"] for c in listing["discovered"]})

        code, data = http("PATCH", f"/api/connectors/{con['id']}", {"enabled": False}, CSRF)
        self.assertEqual(code, 200)
        self.assertFalse(data["connector"]["enabled"])
        code, data = http("POST", f"/api/connectors/{con['id']}/test", None, CSRF)
        self.assertEqual(code, 200)
        self.assertIn("ok", data)

        code, data = http("DELETE", f"/api/connectors/{con['id']}", None, CSRF)
        self.assertEqual(code, 200)
        self.assertEqual(data, {"ok": True})
        code, _ = http("DELETE", f"/api/connectors/{con['id']}", None, CSRF)
        self.assertEqual(code, 404)
        code, _ = http("POST", "/api/connectors/nope/test", None, CSRF)
        self.assertEqual(code, 404)

    def test_job_create_and_delete_without_cron(self) -> None:
        """preset=save 는 cron 줄이 없어 crontab 이 바뀌지 않는다(관리 블록이 이미 있으면 건너뜀)."""
        existing = crontab_sync.current_user_crontab()
        if existing and crontab_sync.BEGIN in existing:
            self.skipTest("사용자 crontab 에 관리 블록이 있어 작업 생성 스모크를 건너뜁니다")
        code, data = http("POST", "/api/jobs", {"prompt": "smoke", "title": "smoke", "model": "smoke-model", "preset": "save",
                                                 "ram_policy": "skip", "max_minutes": 5, "num_ctx": 2048, "connectors": ["cnope"]}, CSRF)
        self.assertEqual(code, 200, data)
        job = data["job"]
        try:
            self.assertEqual(job["preset"], "save")
            self.assertEqual(job["cron"], "")
            self.assertEqual(job["schedule_label"], "예약 없음")
            self.assertIsNone(job["next_run"])
            self.assertEqual((job["ram_policy"], job["max_minutes"], job["num_ctx"], job["connectors"]), ("skip", 5, 2048, ["cnope"]))
            self.assertTrue(data["crontab"]["ok"], data["crontab"])
            code, bad = http("POST", "/api/jobs", {"prompt": "x", "model": "m", "preset": "cron", "cron": "99 * * * *"}, CSRF)
            self.assertEqual(code, 400)
            self.assertIn("cron", bad["error"])
            code, bad = http("PATCH", f"/api/jobs/{job['id']}", {"defer_max_min": 0}, CSRF)
            self.assertEqual(code, 400)
            code, patched = http("PATCH", f"/api/jobs/{job['id']}", {"enabled": False}, CSRF)
            self.assertEqual(code, 200)
            self.assertFalse(patched["job"]["enabled"])
            self.assertEqual(patched["job"]["preset"], "save")
            code, patched = http("PATCH", f"/api/jobs/{job['id']}", {"num_ctx": 0}, CSRF)
            self.assertEqual(code, 200)
            self.assertEqual(patched["job"]["num_ctx"], 0)  # 0 = effort 기본(low 4096 / medium 8192 / high 16384)
            code, tl = http("GET", "/api/timeline?hours=6")
            self.assertEqual(code, 200)
            lane = next(l for l in tl["lanes"] if l["job_id"] == job["id"])
            self.assertEqual(lane["upcoming"], [])
            self.assertGreater(lane["ram_need_gb"], 0)
            code, _ = http("POST", "/api/jobs/nope/run", None, CSRF)
            self.assertEqual(code, 404)
        finally:
            code, gone = http("DELETE", f"/api/jobs/{job['id']}", None, CSRF)
        self.assertEqual(code, 200)
        self.assertTrue(gone["ok"])
        self.assertEqual(crontab_sync.current_user_crontab(), existing)

    def test_pages_and_static_guard(self) -> None:
        for path in ("/jobs", "/connectors", "/settings", "/timeline"):
            req = urllib.request.Request(BASE + path)
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("text/html", resp.headers.get("Content-Type", ""))
        code, _ = http("GET", "/static/../desk/http_api.py")
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
