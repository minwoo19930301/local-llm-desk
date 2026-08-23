from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from desk import crontab_sync
from desk.state import load_jobs, save_jobs

SEOUL = ZoneInfo("Asia/Seoul")

PRESETS = {
    "save": None,
    "now": None,
    "daily_1700": "0 17 * * *",
    "daily_0900": "0 9 * * *",
    "weekdays_1700": "0 17 * * 1-5",
    "hourly": "0 * * * *",
    "every_5m": "*/5 * * * *",
    "once_1m": "once_1m",
}

PERMISSIONS = ("read", "workspace", "machine")
EFFORTS = ("low", "medium", "high")
ALERTS = ("always", "fail", "ok", "off")


def normalize_knobs(payload: dict[str, Any]) -> dict[str, Any]:
    perm = payload.get("permission") or "workspace"
    if perm not in PERMISSIONS:
        perm = "workspace"
    effort = payload.get("effort") or "medium"
    if effort not in EFFORTS:
        effort = "medium"
    try:
        loops = int(payload.get("max_loops") or 1)
    except (TypeError, ValueError):
        loops = 1
    loops = max(1, min(loops, 32))
    return {"permission": perm, "effort": effort, "max_loops": loops, "alert": normalize_alert(payload.get("alert", "always"))}


def normalize_alert(raw: Any) -> str:
    if raw is False:
        return "off"
    if raw is True or raw in (None, ""):
        return "always"
    val = str(raw)
    if val in ALERTS:
        return val
    if val in ("on", "true", "넣고"):
        return "always"
    return "always"


def should_alert(job: dict[str, Any], ok: bool) -> bool:
    mode = normalize_alert(job.get("alert", "always"))
    if mode == "off":
        return False
    if mode == "always":
        return True
    if mode == "fail":
        return not ok
    if mode == "ok":
        return ok
    return True


def list_jobs() -> list[dict[str, Any]]:
    return load_jobs()["jobs"]


def get_job(job_id: str) -> dict[str, Any] | None:
    for job in list_jobs():
        if job.get("id") == job_id:
            return job
    return None


def _title_from(payload: dict[str, Any], prompt: str) -> str:
    title = (payload.get("title") or "").strip()
    if title:
        return title
    first = (prompt.splitlines()[0] if prompt else "").strip()
    return first[:40] if first else "자동화"


def create_job(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("할 일을 적어야 합니다.")
    model = (payload.get("model") or "").strip() or "gemma4:12b"
    preset = payload.get("preset") or "save"
    knobs = normalize_knobs(payload)
    cron, once = resolve_schedule(preset, payload)
    job = {
        "id": uuid.uuid4().hex[:10],
        "title": _title_from(payload, prompt),
        "prompt": prompt,
        "model": model,
        "preset": preset,
        "repeat": payload.get("repeat") or "daily",
        "time": payload.get("time") or "17:00",
        "cron": cron,
        "once": once,
        "enabled": True,
        "alert": knobs["alert"],
        "permission": knobs["permission"],
        "effort": knobs["effort"],
        "max_loops": knobs["max_loops"],
        "created_at": _now().isoformat(timespec="seconds"),
        "last_run": None,
    }
    data = load_jobs()
    data["jobs"].append(job)
    save_jobs(data)
    sync = crontab_sync.apply(data["jobs"])
    return {"job": job, "crontab": sync}


def update_job(job_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    data = load_jobs()
    found = None
    for job in data["jobs"]:
        if job["id"] == job_id:
            found = job
            break
    if not found:
        raise KeyError(job_id)
    if "prompt" in patch:
        found["prompt"] = (patch.get("prompt") or "").strip()
        if not found["prompt"]:
            raise ValueError("할 일을 적어야 합니다.")
    if "title" in patch:
        found["title"] = _title_from(patch, found.get("prompt") or "")
    for key in ("model", "enabled"):
        if key in patch:
            found[key] = patch[key]
    if any(k in patch for k in ("permission", "effort", "max_loops", "alert")):
        knobs = normalize_knobs({**found, **patch})
        found.update(knobs)
    if any(k in patch for k in ("preset", "cron", "repeat", "time")):
        preset = patch.get("preset", found.get("preset") or "custom")
        found["preset"] = preset
        if "repeat" in patch:
            found["repeat"] = patch.get("repeat") or "daily"
        if "time" in patch:
            found["time"] = patch.get("time") or "17:00"
        found["cron"], found["once"] = resolve_schedule(preset, {**found, **patch})
    save_jobs(data)
    sync = crontab_sync.apply(data["jobs"])
    return {"job": found, "crontab": sync}


def delete_job(job_id: str) -> dict[str, Any]:
    data = load_jobs()
    data["jobs"] = [j for j in data["jobs"] if j["id"] != job_id]
    save_jobs(data)
    sync = crontab_sync.apply(data["jobs"])
    return {"ok": True, "crontab": sync}


def disable_if_once(job_id: str) -> None:
    data = load_jobs()
    changed = False
    for job in data["jobs"]:
        if job["id"] == job_id and job.get("once"):
            job["enabled"] = False
            job["cron"] = ""
            changed = True
    if changed:
        save_jobs(data)
        crontab_sync.apply(data["jobs"])


def mark_last_run(job_id: str, run: dict[str, Any]) -> None:
    data = load_jobs()
    for job in data["jobs"]:
        if job["id"] == job_id:
            job["last_run"] = run
            break
    save_jobs(data)


def resolve_schedule(preset: str, payload: dict[str, Any] | str | None = None) -> tuple[str, bool]:
    body: dict[str, Any]
    if isinstance(payload, dict):
        body = payload
        cron = (payload.get("cron") or "").strip()
    else:
        body = {}
        cron = (payload or "").strip()
    if preset in ("save", "now"):
        return "", False
    if preset == "once_1m":
        when = _now() + timedelta(minutes=1)
        return f"{when.minute} {when.hour} {when.day} {when.month} *", True
    if preset == "custom":
        if cron and _valid_cron(cron):
            return cron, False
        return cron_from_when(body.get("repeat") or "daily", body.get("time") or "17:00"), False
    mapped = PRESETS.get(preset)
    if not mapped:
        raise ValueError("일정을 다시 골라 주세요.")
    return mapped, False


def cron_from_when(repeat: str, hhmm: str) -> str:
    hour, minute = _parse_hhmm(hhmm)
    if repeat == "hourly":
        return f"{minute} * * * *"
    if repeat == "weekdays":
        return f"{minute} {hour} * * 1-5"
    if repeat == "weekend":
        return f"{minute} {hour} * * 0,6"
    return f"{minute} {hour} * * *"


def _parse_hhmm(hhmm: str) -> tuple[int, int]:
    text = (hhmm or "17:00").strip()
    parts = text.replace(".", ":").split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except ValueError as exc:
        raise ValueError("시각은 17:00처럼 적어 주세요.") from exc
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("시각은 17:00처럼 적어 주세요.")
    return hour, minute


def sample_payload(model: str) -> dict[str, Any]:
    return {
        "title": "로컬 LLM 연결 테스트",
        "prompt": "너는 이 Mac에서 도는 로컬 LLM이다. '로컬 자동화 연결 확인됨'이라고만 한 줄로 답해.",
        "model": model,
        "preset": "save",
        "alert": "always",
    }


def _valid_cron(expr: str) -> bool:
    parts = expr.split()
    return len(parts) == 5


def _now() -> datetime:
    return datetime.now(SEOUL)
