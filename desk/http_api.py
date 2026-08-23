from __future__ import annotations

import json
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from desk import catalog, crontab_sync, installer, jobs as jobs_mod, ollama_ctl, runner
from desk.hardware import detect
from desk.paths import STATIC
from desk.state import load_config, save_config

HOST = "127.0.0.1"
PORT = 8788
_install_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "local-llm-desk/1.0"
    disable_nagle_algorithm = True

    def log_message(self, fmt: str, *args: Any) -> None:
        sys_stderr_write = super().log_message
        sys_stderr_write(fmt, *args)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        if path in ("/", "/jobs", "/자동화", "/auto"):
            return self._file(STATIC / "jobs.html", "text/html; charset=utf-8")
        if path.startswith("/static/"):
            rel = path[len("/static/") :]
            target = (STATIC / rel).resolve()
            if not str(target).startswith(str(STATIC.resolve())):
                return self._json(404, {"error": "not found"})
            return self._file(target, _mime(target))
        if path == "/api/status":
            return self._json(200, _status())
        if path == "/api/catalog":
            return self._json(200, catalog.build())
        if path == "/api/jobs":
            return self._json(200, {"jobs": jobs_mod.list_jobs(), "crontab": crontab_sync.render_preview(jobs_mod.list_jobs())})
        if path == "/api/runs":
            qs = urllib.parse.parse_qs(parsed.query)
            job_id = (qs.get("job") or [None])[0]
            return self._json(200, {"runs": runner.list_runs(job_id)})
        if path == "/api/crontab":
            return self._json(
                200,
                {
                    "preview": crontab_sync.render_preview(jobs_mod.list_jobs()),
                    "installed": crontab_sync.current_user_crontab(),
                },
            )
        return self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        if path == "/api/install":
            return self._sse(installer.setup)
        if path == "/api/install/cancel":
            installer.request_cancel()
            return self._json(200, {"ok": True})
        if path == "/api/models/remove":
            return self._sse(lambda body: installer.remove(body.get("models") or body.get("names") or []))
        body = self._read_json()
        if path == "/api/jobs":
            try:
                result = jobs_mod.create_job(body)
                return self._json(200, result)
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
        if path == "/api/jobs/try":
            prompt = (body.get("prompt") or "").strip()
            model = (body.get("model") or "").strip()
            title = (body.get("title") or "지금 실행").strip()
            if not prompt:
                return self._json(400, {"error": "시킬 일을 적으세요."})
            return self._json(
                200,
                runner.try_prompt(
                    model,
                    prompt,
                    title=title,
                    effort=(body.get("effort") or "medium"),
                ),
            )
        if path == "/api/jobs/sample":
            hw = detect()
            from desk.hardware import model_plan

            plan = model_plan(int(hw["ram_gb"]))
            try:
                result = jobs_mod.create_job(jobs_mod.sample_payload(plan["light"]))
                return self._json(200, result)
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
        if path.startswith("/api/jobs/") and path.endswith("/run"):
            job_id = path[len("/api/jobs/") : -len("/run")]
            result = runner.run_job(job_id)
            return self._json(200, result)
        if path == "/api/alerts":
            cfg = load_config()
            alerts = cfg.setdefault("alerts", {"macos": True, "webhook": ""})
            if "macos" in body:
                alerts["macos"] = bool(body["macos"])
            if "webhook" in body:
                alerts["webhook"] = str(body["webhook"] or "")
            save_config(cfg)
            return self._json(200, {"alerts": alerts})
        return self._json(404, {"error": "not found"})

    def do_PATCH(self) -> None:
        path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        if path.startswith("/api/jobs/"):
            job_id = path.split("/")[-1]
            body = self._read_json()
            try:
                return self._json(200, jobs_mod.update_job(job_id, body))
            except KeyError:
                return self._json(404, {"error": "job not found"})
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
        return self._json(404, {"error": "not found"})

    def do_DELETE(self) -> None:
        path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        if path.startswith("/api/jobs/"):
            job_id = path.split("/")[-1]
            return self._json(200, jobs_mod.delete_job(job_id))
        return self._json(404, {"error": "not found"})

    def _sse(self, factory) -> None:
        body = self._read_json()
        if not _install_lock.acquire(blocking=False):
            return self._json(409, {"error": "이미 진행 중"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.wfile.write(b": connected\n\n")
        self.wfile.flush()
        try:
            for ev in factory(body):
                if installer.cancelled() and ev.get("event") != "done":
                    ev = {"event": "done", "ok": False, "cancelled": True, "error": "취소했습니다."}
                payload = json.dumps(ev, ensure_ascii=False)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                if ev.get("event") == "done":
                    break
        except BrokenPipeError:
            installer.request_cancel()
        except Exception as exc:
            err = json.dumps({"event": "done", "ok": False, "error": str(exc), "trace": traceback.format_exc()}, ensure_ascii=False)
            try:
                self.wfile.write(f"data: {err}\n\n".encode("utf-8"))
                self.wfile.flush()
            except BrokenPipeError:
                installer.request_cancel()
        finally:
            _install_lock.release()

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _json(self, code: int, payload: Any) -> None:
        blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)

    def _file(self, path: Path, mime: str) -> None:
        if not path.exists() or not path.is_file():
            return self._json(404, {"error": "not found"})
        blob = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)


def _status() -> dict[str, Any]:
    cfg = load_config()
    models = ollama_ctl.list_models()
    return {
        "hardware": detect(),
        "ollama": {
            "running": ollama_ctl.running(),
            "models": models,
        },
        "ready": bool(models),
        "setup_done": bool(cfg.get("setup_done")),
        "alerts": cfg.get("alerts"),
        "port": PORT,
    }


def _mime(path: Path) -> str:
    return {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon",
    }.get(path.suffix, "application/octet-stream")


def serve(open_browser: bool = True) -> None:
    from desk.paths import ensure_dirs

    ensure_dirs()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    print(f"local-llm-desk  {url}", flush=True)
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstop", flush=True)
        httpd.server_close()
