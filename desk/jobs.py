"""Job records in ``data/jobs.json``: validation, schedule resolution, locked writes.

Every write goes through ``state.locked()`` and re-reads the file inside the
lock, so HTTP threads and the cron-spawned runner never clobber each other.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from desk import crontab_sync, ollama_ctl
from desk.state import load_jobs, locked, save_jobs

PRESETS = {
    "save": None,
    "now": None,
    "every_1m": "*/1 * * * *",
    "every_5m": "*/5 * * * *",
    "every_15m": "*/15 * * * *",
    "hourly": "0 * * * *",
    "every_6h": "0 */6 * * *",
    "once_1m": "once_1m",
}
WHEN_PRESETS = ("daily", "weekdays", "weekend", "custom")
TOOL_KINDS = ("cli", "http", "chrome", "file", "mail", "mcp")

PERMISSIONS = ("read", "workspace", "machine")
EFFORTS = ("low", "medium", "high")
ALERTS = ("always", "fail", "ok", "off")
RAM_POLICIES = ("defer", "skip", "downgrade")

DEFAULT_DEFER_MAX_MIN = 30
DEFAULT_MAX_MINUTES = 30
DEFAULT_NUM_CTX = 4096  # /api/ram 조회 기본값. job.num_ctx 는 0 = "effort 기본값"(§3.13, runner 가 결정)
MINUTES_RANGE = (1, 720)
NUM_CTX_RANGE = (512, 262144)
LOOPS_RANGE = (1, 32)

# Keys managed by normalize_knobs; update_job re-normalizes when any of them is patched.
KNOB_KEYS = (
    "permission",
    "effort",
    "max_loops",
    "alert",
    "connectors",
    "ram_policy",
    "defer_max_min",
    "fallback_model",
    "num_ctx",
    "max_minutes",
)
SCHEDULE_KEYS = ("preset", "cron", "repeat", "time")


def normalize_knobs(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and default the per-job knobs (§2.2). Raises ValueError with a Korean message."""
    perm = payload.get("permission") or "workspace"
    effort = payload.get("effort") or "medium"
    policy = payload.get("ram_policy") or "defer"
    if policy not in RAM_POLICIES:
        raise ValueError("램 정책은 미루기·건너뛰기·축소 중 하나여야 합니다.")
    return {
        "permission": perm if perm in PERMISSIONS else "workspace",
        "effort": effort if effort in EFFORTS else "medium",
        "max_loops": _int_in_range(payload.get("max_loops"), 4, LOOPS_RANGE, "도구 반복 횟수", clamp=True),
        "alert": normalize_alert(payload.get("alert", "always")),
        "connectors": _connector_ids(payload.get("connectors")),
        "ram_policy": policy,
        "defer_max_min": _int_in_range(payload.get("defer_max_min"), DEFAULT_DEFER_MAX_MIN, MINUTES_RANGE, "미루기 최대 시간(분)"),
        "fallback_model": str(payload.get("fallback_model") or "").strip(),
        "num_ctx": _num_ctx(payload.get("num_ctx")),
        "max_minutes": _int_in_range(payload.get("max_minutes"), DEFAULT_MAX_MINUTES, MINUTES_RANGE, "최대 실행 시간(분)"),
    }


def _int_in_range(raw: Any, default: int, bounds: tuple[int, int], label: str, clamp: bool = False) -> int:
    if raw in (None, ""):
        return default
    if isinstance(raw, bool):
        raise ValueError(f"{label}은(는) 숫자여야 합니다.")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{label}은(는) 숫자여야 합니다.") from None
    low, high = bounds
    if clamp:
        return max(low, min(value, high))
    if not low <= value <= high:
        raise ValueError(f"{label}은(는) {low}~{high} 사이여야 합니다.")
    return value


def _num_ctx(raw: Any) -> int:
    """0 (또는 비움) = effort 기본 컨텍스트를 쓴다. 값을 주면 NUM_CTX_RANGE 안이어야 한다."""
    if raw in (None, "", 0, "0"):
        return 0
    return _int_in_range(raw, 0, NUM_CTX_RANGE, "컨텍스트 길이")


def _connector_ids(raw: Any) -> list[str]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list) or not all(isinstance(c, str) for c in raw):
        raise ValueError("연동은 연동 id 목록이어야 합니다.")
    seen: list[str] = []
    for cid in raw:
        cid = cid.strip()
        if cid and cid not in seen:
            seen.append(cid)
    if seen:
        try:
            from desk import connectors as _con
            known = {c.get("id") for c in _con.load()}
        except Exception:
            known = None
        if known is not None:
            missing = [c for c in seen if c not in known]
            if missing:
                raise ValueError(f"등록되지 않은 연동: {', '.join(missing)}. 연동 탭에서 먼저 추가하세요.")
    return seen


def normalize_alert(raw: Any) -> str:
    if raw is False:
        return "off"
    if raw is True or raw in (None, ""):
        return "always"
    val = str(raw)
    if val in ALERTS:
        return val
    return "always"


def should_alert(job: dict[str, Any], ok: bool) -> bool:
    mode = normalize_alert(job.get("alert", "always"))
    if mode == "off":
        return False
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


def _clean_model(raw: Any) -> str:
    return str(raw or "").strip()


def _check_provider(raw: Any) -> str:
    provider = str(raw or "ollama").strip() or "ollama"
    if provider != "ollama":
        raise ValueError("예약은 Ollama만 씁니다.")
    return provider


def _clean_tools(raw: Any) -> list[str]:
    return [t for t in (raw or []) if t in TOOL_KINDS]


def create_job(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("할 일을 적어야 합니다.")
    model = _clean_model(payload.get("model")) or ollama_ctl.default_model()
    if not model:
        raise ValueError("먼저 설치에서 모델을 받으세요.")
    provider = _check_provider(payload.get("provider"))
    preset = payload.get("preset") or "save"
    knobs = normalize_knobs(payload)
    job: dict[str, Any] = {
        "id": uuid.uuid4().hex[:10],
        "title": _title_from(payload, prompt),
        "prompt": prompt,
        "provider": provider,
        "model": model,
        "agent": (payload.get("agent") or "chat").strip() or "chat",
        "tools": _clean_tools(payload.get("tools")),
        "preset": preset,
        "repeat": payload.get("repeat") or "daily",
        "time": payload.get("time") or "17:00",
        **_schedule_fields(preset, payload),
        "enabled": bool(payload.get("enabled", True)),
        **knobs,
        "created_at": _now().isoformat(timespec="seconds"),
        "last_run": None,
    }
    with locked():
        data = load_jobs()
        data["jobs"].append(job)
        save_jobs(data)
        sync = crontab_sync.apply(data["jobs"])
    return {"job": with_schedule(job), "crontab": sync}


def update_job(job_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    with locked():
        data = load_jobs()
        found = next((j for j in data["jobs"] if j.get("id") == job_id), None)
        if not found:
            raise KeyError(job_id)
        _apply_patch(found, patch)
        save_jobs(data)
        sync = crontab_sync.apply(data["jobs"])
    return {"job": with_schedule(found), "crontab": sync}


def _apply_patch(found: dict[str, Any], patch: dict[str, Any]) -> None:
    if "prompt" in patch:
        found["prompt"] = (patch.get("prompt") or "").strip()
        if not found["prompt"]:
            raise ValueError("할 일을 적어야 합니다.")
    if "title" in patch:
        found["title"] = _title_from(patch, found.get("prompt") or "")
    if "model" in patch:
        model = _clean_model(patch.get("model")) or _clean_model(found.get("model"))
        if not model:
            raise ValueError("모델을 골라 주세요.")
        found["model"] = model
    if "provider" in patch:
        found["provider"] = _check_provider(patch.get("provider"))
    if "enabled" in patch:
        found["enabled"] = bool(patch["enabled"])
    if "agent" in patch:
        found["agent"] = (patch.get("agent") or "chat").strip() or "chat"
    if "tools" in patch:
        found["tools"] = _clean_tools(patch.get("tools"))
    if any(k in patch for k in KNOB_KEYS):
        found.update(normalize_knobs({**found, **patch}))
    if any(k in patch for k in SCHEDULE_KEYS):
        _apply_schedule_patch(found, patch)


def _apply_schedule_patch(found: dict[str, Any], patch: dict[str, Any]) -> None:
    """Re-resolve the schedule only when it really changed (never re-arm once_1m on unrelated edits)."""
    preset = patch.get("preset", found.get("preset") or "custom")
    if not _schedule_changed(found, patch, preset):
        return
    found["preset"] = preset
    if "repeat" in patch:
        found["repeat"] = patch.get("repeat") or "daily"
    if "time" in patch:
        found["time"] = patch.get("time") or "17:00"
    found.update(_schedule_fields(preset, {**found, **patch}))
    if found.pop("fired_at", None) and "enabled" not in patch:
        found["enabled"] = True


def _schedule_changed(found: dict[str, Any], patch: dict[str, Any], preset: str) -> bool:
    if preset != found.get("preset"):
        return True
    if preset in PRESETS:
        return False
    if preset in ("cron", "custom"):
        if "cron" in patch and (patch.get("cron") or "").strip() != (found.get("cron") or "").strip():
            return True
    if preset in WHEN_PRESETS:
        for key, default in (("repeat", "daily"), ("time", "17:00")):
            if key in patch and (patch.get(key) or default) != (found.get(key) or default):
                return True
    return False


def delete_job(job_id: str) -> dict[str, Any]:
    with locked():
        data = load_jobs()
        data["jobs"] = [j for j in data["jobs"] if j.get("id") != job_id]
        save_jobs(data)
        sync = crontab_sync.apply(data["jobs"])
    return {"ok": True, "crontab": sync}


def _patch_job(job_id: str, mutate: Callable[[dict[str, Any]], bool]) -> bool:
    """Partial update under the lock: re-read, mutate one job, save only if changed."""
    with locked():
        data = load_jobs()
        for job in data["jobs"]:
            if job.get("id") == job_id and mutate(job):
                save_jobs(data)
                return True
    return False


def disable_if_once(job_id: str) -> dict[str, Any] | None:
    """Turn a fired one-shot off and drop its cron line. Returns the crontab sync result when changed."""

    def mutate(job: dict[str, Any]) -> bool:
        if not job.get("once"):
            return False
        job["enabled"] = False
        job["cron"] = ""
        job["fired_at"] = _now().isoformat(timespec="seconds")
        return True

    with locked():
        if not _patch_job(job_id, mutate):
            return None
        return crontab_sync.apply(list_jobs())


def mark_last_run(job_id: str, run: dict[str, Any]) -> None:
    def mutate(job: dict[str, Any]) -> bool:
        job["last_run"] = run
        return True

    _patch_job(job_id, mutate)


def _schedule_fields(preset: str, body: dict[str, Any]) -> dict[str, Any]:
    cron, once = resolve_schedule(preset, body)
    once_at = (_now() + timedelta(minutes=1)).isoformat(timespec="seconds") if once else None
    return {"cron": cron, "once": once, "once_at": once_at}


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
    if preset in WHEN_PRESETS:
        if preset == "custom" and cron:
            if not _valid_cron(cron):
                raise ValueError("cron 식이 올바르지 않습니다. 분 시 일 월 요일 다섯 칸, 예: 30 9 * * 1-5")
            return cron, False
        repeat = "daily" if preset == "daily" else (preset if preset in ("weekdays", "weekend") else (body.get("repeat") or "daily"))
        return cron_from_when(repeat, body.get("time") or "17:00"), False
    if preset == "cron":
        if not _valid_cron(cron):
            raise ValueError("cron 식이 올바르지 않습니다. 분 시 일 월 요일 다섯 칸, 예: 30 9 * * 1-5")
        return cron, False
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


def schedule_label(job: dict[str, Any]) -> str:
    """Korean summary of when the job runs, e.g. '매일 17:00', '한 번 실행함', '예약 없음'."""
    cron = (job.get("cron") or "").strip()
    if job.get("once"):
        if not job.get("enabled") or not cron:
            return "한 번 실행함"
        when = _parse_iso(job.get("once_at"))
        return f"{when.month}/{when.day} {when:%H:%M} 한 번" if when else "1분 뒤 한 번"
    if not cron:
        return "예약 없음"
    try:
        from desk import cronwhen
    except ImportError:
        return cron
    return cronwhen.describe(cron)


def next_run(job: dict[str, Any], start: datetime | None = None) -> datetime | None:
    """Next scheduled time for an enabled job with a cron line, else None."""
    cron = (job.get("cron") or "").strip()
    if not job.get("enabled") or not cron:
        return None
    try:
        from desk import cronwhen
    except ImportError:
        return None
    runs = cronwhen.next_runs(cron, start or _now(), count=1)
    return runs[0] if runs else None


def with_schedule(job: dict[str, Any]) -> dict[str, Any]:
    """Copy of the job with ``schedule_label`` and ``next_run`` (iso or None) for the API."""
    when = next_run(job)
    return {**job, "schedule_label": schedule_label(job), "next_run": when.isoformat(timespec="seconds") if when else None}


def _valid_cron(expr: str) -> bool:
    return crontab_sync.valid_cron(expr)


def _parse_iso(text: Any) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text))
    except ValueError:
        return None


def _now() -> datetime:
    """Local wall-clock time (tz-aware). cron uses the system zone, so schedule math must too."""
    return datetime.now().astimezone()
