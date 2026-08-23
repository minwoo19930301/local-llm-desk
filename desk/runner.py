from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Allow `python3 desk/runner.py` from cron.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desk import alerts, jobs as jobs_mod, ollama_ctl  # noqa: E402
from desk.paths import LOGS_DIR, RUNS_DIR, ensure_dirs  # noqa: E402

SEOUL = ZoneInfo("Asia/Seoul")


def try_prompt(model: str, prompt: str, title: str = "지금 실행", effort: str = "medium") -> dict:
    """Run once without touching crontab."""
    ensure_dirs()
    started = time.time()
    model = (model or "").strip() or "gemma4:12b"
    prompt = (prompt or "").strip()
    if not prompt:
        return _fail("try", "시킬 일을 적으세요.", started=started, title=title, model=model, seconds=0)
    try:
        if not ollama_ctl.running():
            ollama_ctl.start()
            if not ollama_ctl.wait_until_up(40):
                raise RuntimeError("Ollama가 꺼져 있고 기동도 실패했습니다.")
        payload = ollama_ctl.chat(model=model, prompt=prompt, effort=effort)
        text = ollama_ctl.extract_text(payload) or "(빈 응답)"
        elapsed = round(time.time() - started, 1)
        result = {
            "id": f"try-{int(started)}",
            "job_id": "try",
            "title": title,
            "model": model,
            "ok": True,
            "output": text,
            "error": None,
            "seconds": elapsed,
            "at": datetime.now(SEOUL).isoformat(timespec="seconds"),
        }
        _persist("try", result)
        return result
    except Exception as exc:
        elapsed = round(time.time() - started, 1)
        result = _fail("try", str(exc), started=started, title=title, model=model, seconds=elapsed)
        _persist("try", result)
        return result


def run_job(job_id: str) -> dict:
    ensure_dirs()
    job = jobs_mod.get_job(job_id)
    if not job:
        result = _fail(job_id, "자동화를 찾을 수 없습니다.", started=time.time())
        alerts.notify("로컬 자동화 실패", f"{job_id}: 없음", ok=False)
        return result

    started = time.time()
    title = job.get("title") or job_id
    model = job.get("model") or "gemma4:12b"
    prompt = job.get("prompt") or ""
    effort = job.get("effort") or "medium"

    try:
        if not ollama_ctl.running():
            ollama_ctl.start()
            if not ollama_ctl.wait_until_up(40):
                raise RuntimeError("Ollama가 꺼져 있고 기동도 실패했습니다.")
        payload = ollama_ctl.chat(model=model, prompt=prompt, effort=effort)
        text = ollama_ctl.extract_text(payload) or "(빈 응답)"
        elapsed = round(time.time() - started, 1)
        result = {
            "id": f"{job_id}-{int(started)}",
            "job_id": job_id,
            "title": title,
            "model": model,
            "ok": True,
            "output": text,
            "error": None,
            "seconds": elapsed,
            "at": datetime.now(SEOUL).isoformat(timespec="seconds"),
        }
        _persist(job_id, result)
        jobs_mod.mark_last_run(job_id, {k: result[k] for k in ("ok", "at", "seconds", "error")})
        if job.get("once"):
            jobs_mod.disable_if_once(job_id)
        if jobs_mod.should_alert(job, True):
            alerts.notify("로컬 자동화 성공", f"{title} · {elapsed}s\n{text[:120]}", ok=True)
        return result
    except Exception as exc:
        elapsed = round(time.time() - started, 1)
        result = _fail(job_id, str(exc), started=started, title=title, model=model, seconds=elapsed)
        _persist(job_id, result)
        jobs_mod.mark_last_run(job_id, {k: result[k] for k in ("ok", "at", "seconds", "error")})
        if job.get("once"):
            jobs_mod.disable_if_once(job_id)
        if jobs_mod.should_alert(job, False):
            alerts.notify("로컬 자동화 실패", f"{title}\n{exc}", ok=False)
        return result


def _fail(job_id: str, error: str, started: float, title: str = "", model: str = "", seconds: float | None = None) -> dict:
    return {
        "id": f"{job_id}-{int(started)}",
        "job_id": job_id,
        "title": title or job_id,
        "model": model,
        "ok": False,
        "output": "",
        "error": error,
        "seconds": seconds if seconds is not None else round(time.time() - started, 1),
        "at": datetime.now(SEOUL).isoformat(timespec="seconds"),
    }


def _persist(job_id: str, result: dict) -> None:
    ensure_dirs()
    path = RUNS_DIR / f"{job_id}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(result, ensure_ascii=False) + "\n")
    latest = RUNS_DIR / "latest.json"
    latest.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log = LOGS_DIR / f"job-{job_id}.log"
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\n=== {result['at']} ok={result['ok']} {result['seconds']}s ===\n")
        fh.write((result.get("output") or result.get("error") or "") + "\n")


def list_runs(job_id: str | None = None, limit: int = 40) -> list[dict]:
    ensure_dirs()
    rows: list[dict] = []
    files = [RUNS_DIR / f"{job_id}.jsonl"] if job_id else sorted(RUNS_DIR.glob("*.jsonl"))
    for path in files:
        if not path.exists() or path.name == "latest.json":
            continue
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        except Exception:
            continue
    rows.sort(key=lambda r: r.get("at") or "", reverse=True)
    return rows[:limit]


def _timeout(model: str) -> int:
    if any(x in model for x in ("27b", "26b", "32b", "14b")):
        return 600
    return 240


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    try:
        result = run_job(args.job)
        print(json.dumps({"ok": result["ok"], "id": result["id"], "seconds": result["seconds"]}, ensure_ascii=False))
        return 0 if result["ok"] else 1
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
