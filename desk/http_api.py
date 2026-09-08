"""HTTP API and static pages for Free AI Scheduler. Binds 127.0.0.1 only.

Routing is a small table of ``(method, regex, handler)``. Handlers return
``(status, payload)`` for JSON or ``None`` after streaming their own response.
Every non-GET request must pass ``csrf_ok`` (custom header + Origin/Host check);
there is deliberately no ``do_OPTIONS`` so browsers cannot complete a preflight.

Sibling modules written alongside this one (connectors, cronwhen, ramgate,
alerts) are imported lazily inside handlers so the server still starts if one
of them is missing.
"""

from __future__ import annotations

import json
import re
import threading
import traceback
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping

from desk import catalog, crontab_sync, installer, jobs as jobs_mod, links, ollama_ctl, runner
from desk.hardware import detect
from desk.paths import DATA, STATIC
from desk.state import load_config, locked, save_config

HOST = "127.0.0.1"
PORT = 8788
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "free-ai-scheduler"
CSRF_ERROR = {"error": "허용되지 않은 요청"}
PAGES = (
    "/",
    "/welcome",
    "/setup",
    "/setup/engine",
    "/setup/models",
    "/setup/connect",
    "/install",
    "/install/models",
    "/jobs",
    "/jobs/new",
    "/jobs/edit",
    "/auto",
    "/자동화",
    "/자동화/추가",
    "/자동화/수정",
    "/설치",
    "/설치/모델",
    "/connectors",
    "/연동",
    "/settings",
    "/설정",
    "/timeline",
    "/타임라인",
)
TIMELINE_HOURS = (1, 336)
RUNS_LIMIT = (1, 500)
DEFAULT_AVG_SECONDS = 90.0

_install_lock = threading.Lock()


# ── routing ──────────────────────────────────────────────────────────────────


@dataclass
class Request:
    handler: "Handler"
    path: str
    query: dict[str, list[str]]
    params: dict[str, str] = field(default_factory=dict)
    _body: dict[str, Any] | None = None

    @property
    def body(self) -> dict[str, Any]:
        if self._body is None:
            self._body = self.handler._read_json()
        return self._body

    def q(self, key: str, default: str = "") -> str:
        return (self.query.get(key) or [default])[0]


Result = tuple[int, Any] | None
Route = tuple[str, re.Pattern[str], Callable[[Request], Result]]
_ROUTES: list[Route] = []


def route(method: str, pattern: str) -> Callable[[Callable[[Request], Result]], Callable[[Request], Result]]:
    def deco(fn: Callable[[Request], Result]) -> Callable[[Request], Result]:
        _ROUTES.append((method, re.compile(pattern), fn))
        return fn

    return deco


def csrf_ok(headers: Mapping[str, str], port: int | None = None) -> bool:
    """First-party check for non-GET requests: custom header, allowed Host, allowed Origin when present."""
    port = port or PORT
    if (headers.get(CSRF_HEADER) or "").strip() != CSRF_VALUE:
        return False
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if (headers.get("Host") or "").strip().lower() not in hosts:
        return False
    origin = (headers.get("Origin") or "").strip().lower()
    return not origin or origin in {f"http://{h}" for h in hosts}


class Handler(BaseHTTPRequestHandler):
    server_version = "free-ai-scheduler/2.0"
    disable_nagle_algorithm = True

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        if method != "GET" and not csrf_ok(self.headers):
            return self._json(403, CSRF_ERROR)
        for verb, pattern, fn in _ROUTES:
            match = pattern.fullmatch(path) if verb == method else None
            if match is None:
                continue
            req = Request(self, path, urllib.parse.parse_qs(parsed.query), match.groupdict())
            return self._run(fn, req)
        return self._json(404, {"error": "not found"})

    def _run(self, fn: Callable[[Request], Result], req: Request) -> None:
        try:
            result = fn(req)
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except KeyError as exc:
            return self._json(404, {"error": f"없는 항목입니다: {exc.args[0] if exc.args else ''}"})
        except ModuleNotFoundError as exc:
            return self._json(501, {"error": f"아직 준비되지 않은 모듈입니다: {exc.name}"})
        except Exception as exc:
            traceback.print_exc()
            return self._json(500, {"error": str(exc) or exc.__class__.__name__})
        if result is not None:
            code, payload = result
            self._json(code, payload)

    def _sse(self, factory: Callable[[dict[str, Any]], Any], body: dict[str, Any]) -> None:
        if not _install_lock.acquire(blocking=False):
            return self._json(409, {"error": "이미 진행 중"})
        events = None
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            events = iter(factory(body))
            for ev in events:
                if installer.cancelled() and ev.get("event") != "done":
                    ev = {"event": "done", "ok": False, "cancelled": True, "error": "취소했습니다."}
                self._event(ev)
                if ev.get("event") == "done":
                    break
        except (BrokenPipeError, ConnectionResetError):
            installer.request_cancel()
        except Exception as exc:
            try:
                self._event({"event": "done", "ok": False, "error": str(exc), "trace": traceback.format_exc()})
            except (BrokenPipeError, ConnectionResetError):
                installer.request_cancel()
        finally:
            try:
                if events is not None and hasattr(events, "close"):
                    events.close()
            finally:
                _install_lock.release()

    def _event(self, ev: dict[str, Any]) -> None:
        self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
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
        if not path.is_file():
            return self._json(404, {"error": "not found"})
        blob = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(blob)))
        if "javascript" in mime or mime.endswith("css") or "html" in mime:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)


# ── lazy sibling modules ─────────────────────────────────────────────────────


def _ramgate() -> Any:
    from desk import ramgate

    return ramgate


def _connectors() -> Any:
    from desk import connectors

    return connectors


def _cronwhen() -> Any:
    from desk import cronwhen

    return cronwhen


def _alerts() -> Any:
    from desk import alerts

    return alerts


def _soft(fn: Callable[[], Any]) -> Any:
    """Call an optional sibling; ``None`` (with a stderr trace) instead of failing the whole response."""
    try:
        return fn()
    except ModuleNotFoundError:
        return None
    except Exception:
        traceback.print_exc()
        return None


def _active() -> dict[str, Any] | None:
    """Currently running job (``None`` when idle or when active.json is unreadable)."""
    return _soft(runner.active)


def _int_query(raw: str, default: int, bounds: tuple[int, int]) -> int:
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(bounds[0], min(value, bounds[1]))


def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


def _parse_iso(text: Any) -> datetime | None:
    if not text:
        return None
    try:
        when = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return when if when.tzinfo else when.astimezone()


# ── pages & static ───────────────────────────────────────────────────────────


@route("GET", "|".join(re.escape(p) for p in PAGES))
def _page(req: Request) -> Result:
    req.handler._file(STATIC / "jobs.html", "text/html; charset=utf-8")
    return None


@route("GET", r"/static/(?P<rel>.+)")
def _static(req: Request) -> Result:
    root = STATIC.resolve()
    target = (root / req.params["rel"]).resolve()
    if not target.is_relative_to(root):
        return 404, {"error": "not found"}
    req.handler._file(target, _mime(target))
    return None


def _mime(path: Path) -> str:
    return {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".mp4": "video/mp4",
        ".ico": "image/x-icon",
    }.get(path.suffix, "application/octet-stream")


# ── status / ram / catalog ───────────────────────────────────────────────────


@route("GET", r"/api/status")
def _status_route(req: Request) -> Result:
    return 200, _status()


def _status() -> dict[str, Any]:
    cfg = load_config()
    live = ollama_ctl.model_inventory()
    up = live is not None
    if live is not None:
        names = [n for n in ((m.get("name") or m.get("model") or "") for m in live) if n]
        if sorted(names) != sorted(ollama_ctl.remembered_models()):
            ollama_ctl.remember_models(names)
        models = live
    else:
        names = ollama_ctl.remembered_models()
        models = [{"name": n, "model": n} for n in names]
    ready = bool(names) or bool(cfg.get("setup_done") and cfg.get("installed"))
    return {
        "hardware": detect(),
        "ollama": {"running": up, "models": models},
        "ready": ready,
        "provider_ready": bool(up or ollama_ctl.app_installed() or ollama_ctl.binary()),
        "setup_done": bool(cfg.get("setup_done")),
        "alerts": cfg.get("alerts"),
        "connections": links.current(),
        "ram": _soft(lambda: _ramgate().snapshot()),
        "active": _active(),
        "data_dir": str(DATA),
        "port": PORT,
    }


@route("GET", r"/api/ram")
def _ram(req: Request) -> Result:
    rg = _ramgate()
    model = req.q("model").strip()
    num_ctx = _int_query(req.q("num_ctx"), jobs_mod.DEFAULT_NUM_CTX, jobs_mod.NUM_CTX_RANGE)
    out: dict[str, Any] = {"snapshot": rg.snapshot()}
    if model:
        out["need_gb"] = rg.model_need_gb(model, num_ctx)
        out["verdict"] = rg.decide(model, "defer", num_ctx)
    return 200, out


@route("GET", r"/api/catalog")
def _catalog(req: Request) -> Result:
    return 200, catalog.build()


# ── jobs ─────────────────────────────────────────────────────────────────────


@route("GET", r"/api/jobs")
def _jobs_list(req: Request) -> Result:
    jobs = jobs_mod.list_jobs()
    return 200, {"jobs": [jobs_mod.with_schedule(j) for j in jobs], "crontab": crontab_sync.render_preview(jobs)}


@route("POST", r"/api/jobs")
def _jobs_create(req: Request) -> Result:
    return 200, jobs_mod.create_job(req.body)


@route("PATCH", r"/api/jobs/(?P<id>[^/]+)")
def _jobs_update(req: Request) -> Result:
    return 200, jobs_mod.update_job(req.params["id"], req.body)


@route("DELETE", r"/api/jobs/(?P<id>[^/]+)")
def _jobs_delete(req: Request) -> Result:
    return 200, jobs_mod.delete_job(req.params["id"])


@route("POST", r"/api/jobs/try")
def _jobs_try(req: Request) -> Result:
    body = req.body
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("시킬 일을 적으세요.")
    knobs = jobs_mod.normalize_knobs(body)
    kwargs = {
        "title": (body.get("title") or "지금 실행").strip() or "지금 실행",
        "effort": knobs["effort"],
        "agent": (body.get("agent") or "chat").strip() or "chat",
        "permission": knobs["permission"],
        "max_loops": knobs["max_loops"],
        "tools": [t for t in (body.get("tools") or []) if t in jobs_mod.TOOL_KINDS],
        "connectors": knobs["connectors"],
        "num_ctx": knobs["num_ctx"],
        "max_minutes": knobs["max_minutes"],
    }
    model = (body.get("model") or "").strip()
    return 200, runner.try_prompt(model, prompt, **kwargs)


@route("POST", r"/api/jobs/sample")
def _jobs_sample(req: Request) -> Result:
    model = ollama_ctl.default_model()
    if not model:
        raise ValueError("먼저 설치에서 모델을 받으세요.")
    return 200, jobs_mod.create_job(jobs_mod.sample_payload(model))


@route("POST", r"/api/jobs/(?P<id>[^/]+)/run")
def _jobs_run(req: Request) -> Result:
    job_id = req.params["id"]
    if not jobs_mod.get_job(job_id):
        raise KeyError(job_id)
    return 200, runner.run_job(job_id, wait=False)


@route("GET", r"/api/runs")
def _runs(req: Request) -> Result:
    job_id = req.q("job").strip() or None
    limit = _int_query(req.q("limit"), 40, RUNS_LIMIT)
    return 200, {"runs": runner.list_runs(job_id, limit)}


@route("GET", r"/api/crontab")
def _crontab(req: Request) -> Result:
    installed = crontab_sync.current_user_crontab()
    return 200, {
        "preview": crontab_sync.render_preview(jobs_mod.list_jobs()),
        "installed": installed or "",
        "readable": installed is not None,
    }


# ── timeline ─────────────────────────────────────────────────────────────────


@route("GET", r"/api/timeline")
def _timeline_route(req: Request) -> Result:
    return 200, timeline(_int_query(req.q("hours"), 24, TIMELINE_HOURS))


def timeline(hours: int) -> dict[str, Any]:
    """Past quarter / future three-quarters of ``hours`` around now, one lane per job."""
    now = _now()
    start = now - timedelta(hours=hours * 0.25)
    end = now + timedelta(hours=hours * 0.75)
    runs = _runs_since(start, hours)
    lanes = [_lane(job, now, end, runs) for job in jobs_mod.list_jobs()]
    return {
        "now": _iso(now),
        "from": _iso(start),
        "to": _iso(end),
        "ram": _soft(lambda: _ramgate().snapshot()),
        "active": _active(),
        "lanes": lanes,
        "conflicts": conflicts(lanes),
    }


def _runs_since(start: datetime, hours: int) -> list[dict[str, Any]]:
    out = []
    for row in runner.runs_since(hours):
        at = _parse_iso(row.get("at"))
        if at and at >= start and row.get("job_id") != "try":
            out.append(row)
    return out


def _lane(job: dict[str, Any], now: datetime, end: datetime, runs: list[dict[str, Any]]) -> dict[str, Any]:
    history = [float(r.get("seconds") or 0) for r in runner.list_runs(job["id"], 10)]
    history = [s for s in history if s > 0]
    model = job.get("model") or ""
    num_ctx = int(job.get("num_ctx") or 0) or int(ollama_ctl.effort_knobs(job.get("effort") or "medium")["num_ctx"])
    return {
        "job_id": job["id"],
        "title": job.get("title") or job["id"],
        "model": model,
        "enabled": bool(job.get("enabled")),
        "ram_need_gb": _soft(lambda: _ramgate().model_need_gb(model, num_ctx)),
        "avg_seconds": round(sum(history) / len(history), 1) if history else DEFAULT_AVG_SECONDS,
        "upcoming": _upcoming(job, now, end),
        "runs": [_run_row(r) for r in runs if r.get("job_id") == job["id"]],
    }


def _upcoming(job: dict[str, Any], now: datetime, end: datetime) -> list[str]:
    cron = (job.get("cron") or "").strip()
    if not job.get("enabled") or not cron:
        return []
    return [_iso(t) for t in _cronwhen().next_runs(cron, now, count=50, horizon=end)]


def _run_row(run: dict[str, Any]) -> dict[str, Any]:
    ok = bool(run.get("ok"))
    return {
        "id": run.get("id"),
        "at": run.get("at"),
        "seconds": run.get("seconds") or 0,
        "status": run.get("status") or ("ok" if ok else "fail"),
        "model_used": run.get("model_used") or run.get("model") or "",
        "ok": ok,
    }


def conflicts(lanes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Points where scheduled runs of different jobs overlap within their expected durations."""
    spans: list[tuple[datetime, datetime, str]] = []
    for lane in lanes:
        width = timedelta(seconds=float(lane.get("avg_seconds") or DEFAULT_AVG_SECONDS))
        for iso in lane.get("upcoming") or []:
            start = _parse_iso(iso)
            if start:
                spans.append((start, start + width, lane["job_id"]))
    spans.sort()
    found: dict[str, set[str]] = {}
    for i, (start, end, jid) in enumerate(spans):
        for other_start, _other_end, other in spans[i + 1 :]:
            if other_start >= end:
                break
            if other != jid:
                found.setdefault(_iso(max(start, other_start)), set()).update({jid, other})
    return [{"at": at, "job_ids": sorted(ids)} for at, ids in sorted(found.items())]


# ── connectors ───────────────────────────────────────────────────────────────


def redact(con: dict[str, Any]) -> dict[str, Any]:
    """API view of a connector (``connectors.public``): env/header *names* only, URL query hidden."""
    return _connectors().public(con)


@route("GET", r"/api/connectors")
def _connectors_list(req: Request) -> Result:
    con = _connectors()
    return 200, {"connectors": [redact(c) for c in con.load()], "discovered": [redact(c) for c in con.discover()]}


@route("POST", r"/api/connectors")
def _connectors_add(req: Request) -> Result:
    con = _connectors()
    body = req.body
    if body.get("import"):
        return 200, {"connector": redact(con.import_candidate(str(body["import"])))}
    if not body.get("kind"):
        raise ValueError("연동 종류(kind)를 골라 주세요.")
    return 200, {"connector": redact(con.add(body))}


@route("PATCH", r"/api/connectors/(?P<id>[^/]+)")
def _connectors_update(req: Request) -> Result:
    return 200, {"connector": redact(_connectors().update(req.params["id"], req.body))}


@route("DELETE", r"/api/connectors/(?P<id>[^/]+)")
def _connectors_remove(req: Request) -> Result:
    _connectors().remove(req.params["id"])
    return 200, {"ok": True}


@route("POST", r"/api/connectors/(?P<id>[^/]+)/test")
def _connectors_test(req: Request) -> Result:
    return 200, _connectors().test(req.params["id"])


# ── legacy generic tool switches ─────────────────────────────────────────────


@route("POST", r"/api/connections/test")
def _connections_test(req: Request) -> Result:
    body = req.body
    return 200, links.probe_with_model(str(body.get("id") or ""), str(body.get("source") or ""), str(body.get("model") or ""))


@route("POST", r"/api/connections")
def _connections_save(req: Request) -> Result:
    body = req.body
    con = links.save_one(str(body.get("id") or ""), str(body.get("source") or ""), on=bool(body.get("on", True)))
    return 200, {"connections": con}


# ── alerts ───────────────────────────────────────────────────────────────────


def _clean_webhook(raw: Any) -> str:
    url = str(raw or "").strip()
    if url and not url.startswith(("http://", "https://")):
        raise ValueError("웹훅 주소는 http:// 또는 https:// 로 시작해야 합니다.")
    return url


@route("POST", r"/api/alerts")
def _alerts_save(req: Request) -> Result:
    body = req.body
    with locked():
        cfg = load_config()
        alerts = cfg["alerts"]
        for key in ("macos", "sound"):
            if key in body:
                alerts[key] = bool(body[key])
        if "webhook" in body:
            alerts["webhook"] = _clean_webhook(body["webhook"])
        save_config(cfg)
    return 200, {"alerts": alerts}


@route("POST", r"/api/alerts/test")
def _alerts_test(req: Request) -> Result:
    return 200, _alerts().test()


# ── install (SSE) ────────────────────────────────────────────────────────────


@route("POST", r"/api/install")
def _install(req: Request) -> Result:
    req.handler._sse(installer.setup, req.body)
    return None


@route("POST", r"/api/install/cancel")
def _install_cancel(req: Request) -> Result:
    installer.request_cancel()
    return 200, {"ok": True}


@route("POST", r"/api/models/remove")
def _models_remove(req: Request) -> Result:
    req.handler._sse(lambda b: installer.remove(b.get("models") or b.get("names") or []), req.body)
    return None


# ── entry ────────────────────────────────────────────────────────────────────


def serve(open_browser: bool = True) -> None:
    from desk import oneshot
    from desk.paths import ensure_dirs

    ensure_dirs()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    print(f"Free AI Scheduler  {url}", flush=True)
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    oneshot.start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstop", flush=True)
    finally:
        oneshot.stop()
        httpd.server_close()
