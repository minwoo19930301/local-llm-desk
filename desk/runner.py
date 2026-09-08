"""작업 실행기. cron(`python3 desk/runner.py --job ID`)과 HTTP API 둘 다 여기로 온다.

한 번의 실행 = per-job pid 잠금 → RAM 게이트(미루기/건너뛰기/축소) → Ollama 세션 안에서 도구 루프
→ 실행 기록(data/runs/<job>.jsonl, 스키마는 CONTRACT §2.3) → last_run 갱신 → 알림.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import re
from collections import Counter
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Allow `python3 desk/runner.py` from cron.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desk import alerts, jobs as jobs_mod, ollama_ctl, ramgate  # noqa: E402
from desk.paths import DATA, LOGS_DIR, PID_DIR, RUNS_DIR, ensure_dirs  # noqa: E402

SEOUL = ZoneInfo("Asia/Seoul")
ACTIVE_FILE = RUNS_DIR / "active.json"
WORKSPACE = DATA / "workspace"
DEFAULT_MAX_MINUTES = 30
DEFAULT_DEFER_MIN = 30
DEFER_POLL_S = 30
ONCE_GRACE_MIN = 10
MAX_LOOPS = 32
TRIM_HEAD = 800
TRIM_TAIL = 800
RUNS_MAX_BYTES = 5 * 1024 * 1024
RUNS_KEEP_LINES = 500
STATUS_LABEL = {"ok": "성공", "fail": "실패", "skipped": "건너뜀", "deferred_timeout": "시간초과", "aborted": "중단됨", "empty": "빈 응답"}


# --- 실행 컨텍스트 ------------------------------------------------------------


@dataclass
class Budget:
    """벽시계 예산. 모든 Ollama 호출 timeout은 남은 시간과 effort timeout 중 작은 값."""

    deadline: float

    @classmethod
    def minutes(cls, minutes: int) -> "Budget":
        return cls(time.time() + max(1, minutes) * 60)

    def remaining(self) -> float:
        return self.deadline - time.time()

    def check(self) -> None:
        if self.remaining() <= 0:
            raise RuntimeError("시간 예산 초과")

    def chat_timeout(self, effort: str) -> float:
        self.check()
        return max(0.001, min(self.remaining(), float(ollama_ctl.effort_knobs(effort)["timeout"])))


@dataclass
class TaskResult:
    text: str = ""
    error: str | None = None
    tools_used: list[str] = field(default_factory=list)
    truncated: bool = False
    loops: int = 0

    def note_tool(self, name: str) -> None:
        if name not in self.tools_used:
            self.tools_used.append(name)


@dataclass
class RunContext:
    job_id: str
    title: str
    model: str
    prompt: str
    effort: str = "medium"
    agent: str = "chat"
    permission: str = "workspace"
    max_loops: int = 1
    tools: list[str] = field(default_factory=list)
    connector_ids: list[str] = field(default_factory=list)
    ram_policy: str = "defer"
    defer_max_min: int = DEFAULT_DEFER_MIN
    fallback_model: str = ""
    num_ctx: int = 0
    max_minutes: int = DEFAULT_MAX_MINUTES
    started: float = field(default_factory=time.time)
    pid: int = field(default_factory=os.getpid)

    @classmethod
    def from_job(cls, job: dict[str, Any]) -> "RunContext":
        tools = [t for t in (job.get("tools") or []) if t]
        effort = job.get("effort") or "medium"
        return cls(
            job_id=str(job.get("id") or ""),
            title=str(job.get("title") or job.get("id") or ""),
            model=(job.get("model") or "").strip() or ollama_ctl.default_model(),
            prompt=str(job.get("prompt") or ""),
            effort=effort,
            agent=(job.get("agent") or "chat").strip() or "chat",
            permission=job.get("permission") or "workspace",
            max_loops=_int(job.get("max_loops"), 12 if tools else 1, 1, MAX_LOOPS),
            tools=tools,
            connector_ids=[str(c) for c in (job.get("connectors") or []) if c],
            ram_policy=job.get("ram_policy") if job.get("ram_policy") in ("defer", "skip", "downgrade") else "defer",
            defer_max_min=_int(job.get("defer_max_min"), DEFAULT_DEFER_MIN, 1, 720),
            fallback_model=str(job.get("fallback_model") or "").strip(),
            num_ctx=_effective_num_ctx(job.get("num_ctx"), effort),
            max_minutes=_int(job.get("max_minutes"), DEFAULT_MAX_MINUTES, 1, 720),
        )

    @property
    def run_id(self) -> str:
        return f"{self.job_id}-{int(self.started)}"


def _int(raw: Any, default: int, lo: int, hi: int) -> int:
    try:
        value = int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        value = default
    return max(lo, min(value, hi))


def _effective_num_ctx(raw: Any, effort: str) -> int:
    """job.num_ctx가 있으면 그것, 없으면 effort별 기본(low 4096 / medium 8192 / high 16384)."""
    default = int(ollama_ctl.effort_knobs(effort)["num_ctx"])
    return _int(raw, default, 512, 262144) if raw not in (None, "", 0) else default


# --- 공개 진입점 ---------------------------------------------------------------


def try_prompt(
    model: str,
    prompt: str,
    title: str = "지금 실행",
    effort: str = "medium",
    agent: str = "chat",
    permission: str = "workspace",
    max_loops: int = 1,
    tools: list | None = None,
    connectors: list[str] | None = None,
    num_ctx: int | None = None,
    max_minutes: int = DEFAULT_MAX_MINUTES,
) -> dict:
    """crontab을 건드리지 않는 1회 실행. RAM 게이트는 정책을 무시하고 경고(ram_note)만 남긴다."""
    ensure_dirs()
    ctx = RunContext(
        job_id="try",
        title=title,
        model=(model or "").strip() or ollama_ctl.default_model(),
        prompt=(prompt or "").strip(),
        effort=effort,
        agent=(agent or "chat").strip() or "chat",
        permission=permission or "workspace",
        max_loops=_int(max_loops, 1, 1, MAX_LOOPS),
        tools=[t for t in (tools or []) if t],
        connector_ids=[str(c) for c in (connectors or []) if c],
        num_ctx=_effective_num_ctx(num_ctx, effort),
        max_minutes=_int(max_minutes, DEFAULT_MAX_MINUTES, 1, 720),
    )
    if not ctx.prompt:
        result = _record(ctx, "fail", error="시킬 일을 적으세요.")
        _persist(ctx.job_id, result)
        return result
    verdict = ramgate.decide(ctx.model, "defer", ctx.num_ctx)
    result = _execute(ctx, verdict, waited_s=0)
    result["ram_note"] = "" if verdict["action"] == "run" else verdict["reason"]
    _persist(ctx.job_id, result)
    return result


def run_job(job_id: str, scheduled: bool = False, wait: bool = True) -> dict:
    """cron/API에서 부르는 예약 실행. 항상 기록을 남기고 dict를 돌려준다.

    scheduled=True(cron 경유)면 예약 시각이 한참 지난 1회성(once) 작업은 돌리지 않고 끈다
    (맥이 잠들어 놓친 1회 예약이 내년에 다시 울리지 않게).
    """
    ensure_dirs()
    job = jobs_mod.get_job(job_id)
    if not job:
        ctx = RunContext(job_id=job_id, title=job_id, model="", prompt="")
        result = _record(ctx, "fail", error="자동화를 찾을 수 없습니다.")
        if not scheduled:
            result["alert"] = alerts.notify("Free AI Scheduler 실패", f"{job_id}: 없음", ok=False, status="fail", job_id=job_id, run_id=result["id"])
        _persist(job_id, result)
        return result
    ctx = RunContext.from_job(job)
    if scheduled and not job.get("enabled", True):
        result = _record(ctx, "skipped", error="비활성화된 자동화")
        _persist(job_id, result)
        return result
    lock = JobLock(job_id)
    if not lock.acquire():
        result = _record(ctx, "skipped", error="이미 실행 중")
        _persist(job_id, result)
        return result
    try:
        current = jobs_mod.get_job(job_id)
        if current is None:
            return _record(ctx, "skipped", error="삭제된 자동화")
        job = current
        if job.get("schedule_backend") == "desk":
            job, reason = jobs_mod.claim_desk_once(job_id, allow_early=not scheduled)
            if reason:
                return _record(ctx, "skipped", error=reason)
            ctx = RunContext.from_job(job)
        elif scheduled:
            job = jobs_mod.get_job(job_id)
            if not job or not job.get("enabled", True):
                return _record(ctx, "skipped", error="삭제되었거나 비활성화된 자동화")
            ctx = RunContext.from_job(job)
        stale = _stale_once_reason(job) if scheduled else None
        if stale:
            result = _record(ctx, "skipped", error=stale)
            _finish(job, ctx, result)
            return result
        _write_active(ctx)
        result = _run_gated(ctx, wait)
        _finish(job, ctx, result)
        return result
    finally:
        _clear_active(ctx.pid)
        lock.release()


def _stale_once_reason(job: dict[str, Any]) -> str | None:
    """once 작업의 once_at 이 ONCE_GRACE_MIN 분 넘게 지났으면 건너뛸 사유(한국어), 아니면 None."""
    if not job.get("once"):
        return None
    try:
        planned = datetime.fromisoformat(str(job.get("once_at") or ""))
    except ValueError:
        return None
    if planned.tzinfo is None:
        planned = planned.astimezone()
    late = datetime.now(SEOUL) - planned
    if late <= timedelta(minutes=ONCE_GRACE_MIN):
        return None
    return f"예약 시각({planned.astimezone(SEOUL):%m/%d %H:%M})이 {int(late.total_seconds() // 60)}분 지나 실행하지 않았습니다"


def _run_gated(ctx: RunContext, wait: bool = True) -> dict:
    """wait=True(예약 실행)면 램이 빌 때까지 기다리고, False(지금 실행)면 한 번만 판정해 바로 답한다."""
    if wait:
        verdict, waited_s = _wait_for_ram(ctx)
    else:
        verdict, waited_s = ramgate.decide(ctx.model, ctx.ram_policy, ctx.num_ctx, ctx.fallback_model), 0
    if verdict["action"] == "wait":
        if not wait:
            error = f"지금은 램이 부족합니다 (여유 {verdict['avail_gb']}GB, 필요 {verdict['need_gb']}GB). 예약 실행 때는 자동으로 기다립니다."
            return _record(ctx, "skipped", error=error, verdict=verdict, waited_s=0)
        error = f"{ctx.defer_max_min}분 기다렸지만 램이 부족합니다 (여유 {verdict['avail_gb']}GB, 필요 {verdict['need_gb']}GB)"
        return _record(ctx, "deferred_timeout", error=error, verdict=verdict, waited_s=waited_s)
    if verdict["action"] == "skip":
        error = f"램 부족으로 건너뜀 (여유 {verdict['avail_gb']}GB, 필요 {verdict['need_gb']}GB) · {verdict['reason']}"
        return _record(ctx, "skipped", error=error, verdict=verdict, waited_s=waited_s)
    return _execute(ctx, verdict, waited_s)


def _wait_for_ram(ctx: RunContext) -> tuple[dict, int]:
    """policy가 defer면 30초 간격으로 재평가. (판정, 기다린 초)."""
    began = time.time()
    while True:
        verdict = ramgate.decide(ctx.model, ctx.ram_policy, ctx.num_ctx, ctx.fallback_model)
        waited = int(time.time() - began)
        if verdict["action"] != "wait" or waited >= ctx.defer_max_min * 60:
            return verdict, waited
        _write_active(ctx, waiting=verdict["reason"])
        time.sleep(min(DEFER_POLL_S, max(1, ctx.defer_max_min * 60 - waited)))


def _execute(ctx: RunContext, verdict: dict, waited_s: int) -> dict:
    """게이트를 통과한 뒤 Ollama 세션 안에서 실제 작업을 돌린다."""
    model_used = verdict["model"] if verdict["action"] == "downgrade" else ctx.model
    budget = Budget.minutes(ctx.max_minutes)
    cons = _collect_connectors(ctx.connector_ids)
    warnings = _run_warnings(ctx, cons)
    try:
        with ollama_ctl.session(model_used):
            task = run_task(
                model=model_used,
                prompt=ctx.prompt,
                effort=ctx.effort,
                agent=ctx.agent,
                permission=ctx.permission,
                max_loops=ctx.max_loops,
                tools=ctx.tools,
                connectors=cons,
                num_ctx=ctx.num_ctx,
                budget=budget,
            )
    except Exception as exc:  # 실패도 기록으로 남긴다
        return _record(ctx, "fail", error=str(exc) or exc.__class__.__name__, verdict=verdict, waited_s=waited_s, model_used=model_used, warnings=warnings)
    if task.error is not None:
        return _record(ctx, "fail", task=task, error=task.error, verdict=verdict,
                       waited_s=waited_s, model_used=model_used, warnings=warnings)
    status, quality = _judge_output(task.text)
    warnings.extend(quality)
    return _record(ctx, status, task=task, verdict=verdict, waited_s=waited_s, model_used=model_used, warnings=warnings)


LIVE_INFO_RE = re.compile(r"오늘|최신|뉴스|날씨|지금|현재|실시간|어제|이번 주|주가|환율|속보")
_LIST_PREFIX_RE = re.compile(r"^\s*(\d+[.)]|[-*•])\s*")


def prompt_warning(prompt: str, tools: list, connector_ids: list) -> str:
    """도구·연동 없이 실시간 정보를 묻는 프롬프트면 경고문(한국어), 아니면 빈 문자열."""
    if tools or connector_ids:
        return ""
    if LIVE_INFO_RE.search(prompt or ""):
        return "도구·연동 없이 '오늘/최신/뉴스' 같은 실시간 정보를 물었습니다. 로컬 모델은 인터넷이 없어 답을 지어낼 수 있습니다."
    return ""


def _run_warnings(ctx: RunContext, cons: list[dict]) -> list[str]:
    """실행 전 경고: 등록 안 된 연동 id, 도구 없는 실시간 질문."""
    out: list[str] = []
    have = {c.get("id") for c in cons}
    missing = [cid for cid in ctx.connector_ids if cid not in have]
    if missing:
        out.append(f"등록되지 않은 연동을 건너뜀: {', '.join(missing)}")
    note = prompt_warning(ctx.prompt, ctx.tools, sorted(have))
    if note:
        out.append(note)
    return out


def _judge_output(text: str) -> tuple[str, list[str]]:
    """출력 품질. 빈 응답이면 status 'empty'; 번호만 다른 같은 문장이 3번 이상이면 경고."""
    body = (text or "").strip()
    if not body:
        return "empty", []
    lines = [_LIST_PREFIX_RE.sub("", ln.strip()) for ln in body.splitlines()]
    lines = [ln for ln in lines if len(ln) > 12]
    top = Counter(lines).most_common(1)
    if top and top[0][1] >= 3:
        return "ok", ["같은 문장이 반복됐습니다. 모델이 답을 몰라 지어낸 것일 수 있습니다."]
    return "ok", []


def _finish(job: dict, ctx: RunContext, result: dict) -> None:
    """기록·last_run·once 해제·알림. 알림 결과는 기록의 ``alert``에 남긴다."""
    status = result["status"]
    ok = status == "ok"
    if jobs_mod.should_alert(job, ok):
        if ok:
            wait = f" · 램 대기 {result.get('waited_s', 0) // 60}분" if result.get("waited_s", 0) >= 60 else ""
            body = f"{ctx.title} · {result['seconds']}s{wait}\n{(result.get('output') or '')[:120]}"
        else:
            body = f"{ctx.title}\n{result.get('error') or ''}"
        heading = f"Free AI Scheduler {STATUS_LABEL.get(status, status)}"
        result["alert"] = alerts.notify(heading, body, ok=ok, status=status, job_id=ctx.job_id, run_id=result["id"])
    _persist(ctx.job_id, result)
    jobs_mod.mark_last_run(ctx.job_id, {k: result.get(k) for k in ("ok", "at", "seconds", "error", "status", "model_used")})
    if job.get("once"):
        jobs_mod.disable_if_once(ctx.job_id)


# --- 연동 ---------------------------------------------------------------------


def _collect_connectors(ids: list[str]) -> list[dict]:
    """job.connectors에 해당하는 enabled 연동. 삭제된 것은 무시. (desk.connectors는 지연 import)"""
    if not ids:
        return []
    try:
        from desk import connectors
    except ImportError:
        return []
    wanted = set(ids)
    return [c for c in connectors.load() if c.get("id") in wanted and c.get("enabled", True)]


# --- 작업 본체 ------------------------------------------------------------------


def run_task(
    model: str,
    prompt: str,
    effort: str = "medium",
    agent: str = "chat",
    permission: str = "workspace",
    max_loops: int = 1,
    tools: list | None = None,
    connectors: list[dict] | None = None,
    num_ctx: int | None = None,
    budget: Budget | None = None,
) -> TaskResult:
    agent = (agent or "chat").strip() or "chat"
    budget = budget or Budget.minutes(DEFAULT_MAX_MINUTES)
    if agent in ("chat", "none", "글만", "이 앱"):
        return _desk_run(model, prompt, effort, permission, max_loops, tools, connectors, num_ctx, budget)
    if agent == "aider":
        argv = ["aider", "--yes", "--no-auto-commits", "--model", f"ollama_chat/{model}", "-m", prompt]
        return TaskResult(text=_run_cli("aider", argv, permission, budget,
                                       extra_env={"OLLAMA_API_BASE": ollama_ctl.OLLAMA_HOST}), tools_used=["aider"], loops=1)
    if agent == "opencode":
        # Inline config overrides user/project defaults; only the selected local
        # provider is enabled. No cloud credentials or global config are needed.
        config = {
            "enabled_providers": ["ollama"],
            "model": f"ollama/{model}",
            "small_model": f"ollama/{model}",
            "provider": {"ollama": {
                "npm": "@ai-sdk/openai-compatible", "name": "Ollama (local)",
                "options": {"baseURL": ollama_ctl.OLLAMA_HOST + "/v1"},
                "models": {model: {"name": model}},
            }},
        }
        argv = ["opencode", "run", "--model", f"ollama/{model}", prompt]
        return TaskResult(text=_run_cli("opencode", argv, permission, budget,
                                       extra_env={"OPENCODE_CONFIG_CONTENT": json.dumps(config)}), tools_used=["opencode"], loops=1)
    raise RuntimeError(f"에이전트 '{agent}'를 이 작업에서 아직 못 돌립니다.")


def _desk_run(
    model: str,
    prompt: str,
    effort: str,
    permission: str,
    max_loops: int,
    wanted: list | None,
    connectors: list[dict] | None,
    num_ctx: int | None,
    budget: Budget,
) -> TaskResult:
    from desk import tools

    wanted = [t for t in (wanted or []) if t and t != "mcp"]  # "mcp" 스위치는 연동으로 대체됨
    cons = list(connectors or [])
    ctx_len = int(num_ctx or ollama_ctl.effort_knobs(effort)["num_ctx"])
    if not wanted and not cons:
        payload = ollama_ctl.chat(model, prompt, effort=effort, num_ctx=ctx_len, timeout=budget.chat_timeout(effort))
        return TaskResult(text=ollama_ctl.extract_text(payload), loops=1)

    loops = _int(max_loops, 1, 1, MAX_LOOPS)
    skills = [c for c in cons if c.get("kind") == "skill"]
    mcp_cons = [c for c in cons if c.get("kind") == "mcp"]
    clients: dict = {}
    host = None
    try:
        if mcp_cons:
            from desk import mcp_host as host

            clients = host.open_for_job(mcp_cons)
        tool_context = tools.build_context(permission, wanted, cons, clients)
        spec = tool_context.specifications
        system = tools.system_prompt(permission, loops, skills) + _mcp_instructions(clients)
        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        result = TaskResult()
        for _ in range(loops):
            payload = ollama_ctl.chat_messages(model, messages, effort=effort, tools=spec, num_ctx=ctx_len, timeout=budget.chat_timeout(effort))
            if payload.get("tools_dropped"):
                raise RuntimeError("이 모델은 도구를 지원하지 않습니다. 도구를 끄거나 다른 모델을 고르세요.")
            result.loops += 1
            result.text = ollama_ctl.extract_text(payload)
            msg = payload.get("message") or {"role": "assistant", "content": result.text}
            messages.append(msg)
            calls = ollama_ctl.extract_tool_calls(payload)
            if not calls:
                return result
            for call in calls:
                budget.check()
                output = tools.run(call["name"], call.get("arguments") or {}, permission, clients,
                                   timeout=budget.remaining(), context=tool_context)
                result.note_tool(call["name"])
                if tool_context.failures:
                    failure = tool_context.failures[-1]
                    result.error = f"도구 '{failure.name}' 실패 ({failure.error_type}): {failure.message}"
                    result.text = ""
                    return result  # No model summary after missing/failed evidence.
                messages.append(_tool_message(call, output))
            if _trim_transcript(messages, ctx_len):
                result.truncated = True
        payload = ollama_ctl.chat_messages(model, messages, effort=effort, num_ctx=ctx_len, timeout=budget.chat_timeout(effort))
        result.loops += 1
        result.text = ollama_ctl.extract_text(payload)
        return result
    finally:
        if host is not None:
            host.close_clients(clients)  # 이 실행이 띄운 MCP 서버만 닫는다


def _mcp_instructions(clients: dict) -> str:
    """MCP 서버가 initialize 응답에 실어 준 instructions 를 시스템 프롬프트 뒤에 붙인다(서버당 2000자)."""
    blocks = []
    for client in clients.values():
        text = (getattr(client, "instructions", "") or "").strip()
        if text:
            blocks.append(f"\n\n## MCP 안내: {client.name}\n{text[:2000]}")
    return "".join(blocks)


def _tool_message(call: dict, output: str) -> dict:
    """Ollama 2025 스키마: role=tool + tool_name (+ tool_call_id가 있으면 함께)."""
    msg: dict[str, Any] = {"role": "tool", "tool_name": call["name"], "content": output}
    if call.get("id"):
        msg["tool_call_id"] = call["id"]
    return msg


def _trim_transcript(messages: list[dict], num_ctx: int) -> bool:
    """총 글자수가 num_ctx×3을 넘으면 오래된 tool 결과부터 head 800 + tail 800자로 줄인다."""
    limit = num_ctx * 3
    trimmed = False
    for msg in messages[:-1]:
        if _transcript_chars(messages) <= limit:
            break
        content = str(msg.get("content") or "")
        if msg.get("role") != "tool" or len(content) <= TRIM_HEAD + TRIM_TAIL:
            continue
        msg["content"] = content[:TRIM_HEAD] + "\n…(중간 생략)…\n" + content[-TRIM_TAIL:]
        trimmed = True
    return trimmed


def _transcript_chars(messages: list[dict]) -> int:
    return sum(len(str(m.get("content") or "")) for m in messages)


def _run_cli(name: str, argv: list[str], permission: str, budget: Budget, *, extra_env: dict[str, str] | None = None) -> str:
    """aider/opencode. 읽기 권한이면 거절, 작업 폴더는 권한에 따라, timeout은 남은 예산."""
    import shutil
    from desk.tools import sandbox_run

    if permission == "read":
        raise RuntimeError(f"읽기 권한에서는 {name}를 실행하지 않습니다.")
    if not shutil.which(argv[0]):
        raise RuntimeError(f"{name}가 이 맥에 없습니다. 설치에서 받으세요.")
    budget.check()
    try:
        proc = sandbox_run(argv, permission, timeout=budget.remaining(), allow_ollama=True, extra_env=extra_env)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("시간 예산 초과") from exc
    out = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
    if proc.returncode != 0:
        raise RuntimeError(_tail(out, 2000) or f"{name} 실패")
    return _tail(out, 4000) or "완료"


def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"...(앞 {len(text) - limit}자 생략)\n" + text[-limit:]


# --- 기록 --------------------------------------------------------------------


def _scrub_secrets(text: str, connector_ids: list[str]) -> str:
    """실행 중 사용된 연동의 실제 시크릿 값을 로그/응답에서 지운다(best-effort)."""
    if not text or not connector_ids:
        return text
    try:
        from desk import connectors
    except ImportError:
        return text
    secrets: set[str] = set()
    for con in _collect_connectors(connector_ids):
        try:
            secrets.update(v for v in connectors.resolve_env(con).values() if v)
            secrets.update(v for v in connectors.resolve_headers(con).values() if v)
        except Exception:
            continue
    for value in secrets:
        if len(value) < 6:  # 너무 짧은 값은 흔한 문자열과 겹쳐 오탐 위험이 크므로 건너뜀
            continue
        text = text.replace(value, "[REDACTED]")
    return text


def _record(
    ctx: RunContext,
    status: str,
    *,
    task: TaskResult | None = None,
    error: str | None = None,
    verdict: dict | None = None,
    waited_s: int = 0,
    model_used: str | None = None,
    warnings: list[str] | None = None,
) -> dict:
    """CONTRACT §2.3 한 줄 스키마. seconds는 램 대기를 뺀 실제 작업 시간, 대기는 waited_s."""
    task = task or TaskResult()
    verdict = verdict or {}
    now = datetime.now(SEOUL)
    output_text = _scrub_secrets(task.text, ctx.connector_ids) if status == "ok" else ""
    error_text = None if status == "ok" else _scrub_secrets(error or STATUS_LABEL.get(status, status), ctx.connector_ids)
    return {
        "id": ctx.run_id,
        "job_id": ctx.job_id,
        "title": ctx.title,
        "model": ctx.model,
        "ok": status == "ok",
        "output": output_text,
        "error": error_text,
        "seconds": round(max(0.0, time.time() - ctx.started - waited_s), 1),
        "at": now.isoformat(timespec="seconds"),
        "started_at": datetime.fromtimestamp(ctx.started, SEOUL).isoformat(timespec="seconds"),
        "status": status,
        "model_requested": ctx.model,
        "model_used": model_used or ctx.model,
        "ram_free_gb": verdict.get("free_gb"),
        "ram_avail_gb": verdict.get("avail_gb"),
        "ram_need_gb": verdict.get("need_gb"),
        "ram_action": verdict.get("action"),
        "waited_s": int(waited_s),
        "tools_used": list(task.tools_used),
        "truncated": bool(task.truncated),
        "loops": task.loops,
        "effort": ctx.effort,
        "agent": ctx.agent,
        "permission": ctx.permission,
        "tools": list(ctx.tools),
        "connectors": list(ctx.connector_ids),
        "num_ctx": ctx.num_ctx,
        "warnings": list(warnings or []),
        "alert": None,
    }


def _persist(job_id: str, result: dict) -> None:
    ensure_dirs()
    path = RUNS_DIR / f"{job_id}.jsonl"
    _rotate_jsonl(path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(result, ensure_ascii=False) + "\n")
    (RUNS_DIR / "latest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log = LOGS_DIR / f"job-{job_id}.log"
    _rotate_log(log)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\n=== {result['at']} {result.get('status') or ('ok' if result['ok'] else 'fail')} {result['seconds']}s ===\n")
        fh.write((result.get("output") or result.get("error") or "") + "\n")


def _rotate_jsonl(path: Path) -> None:
    """5MB를 넘으면 최근 500줄만 남긴다."""
    try:
        if path.stat().st_size <= RUNS_MAX_BYTES:
            return
    except FileNotFoundError:
        return
    lines = _tail_lines(path, RUNS_KEEP_LINES)
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def _rotate_log(path: Path) -> None:
    try:
        if path.stat().st_size <= RUNS_MAX_BYTES:
            return
    except FileNotFoundError:
        return
    data = path.read_bytes()[-RUNS_MAX_BYTES // 2 :]
    path.write_bytes(data)


def _tail_lines(path: Path, count: int) -> list[str]:
    """파일 끝에서 count줄만 읽는다(전체 파싱 회피)."""
    chunk = 64 * 1024
    buf = b""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            while pos > 0 and buf.count(b"\n") <= count:
                step = min(chunk, pos)
                pos -= step
                fh.seek(pos)
                buf = fh.read(step) + buf
    except OSError:
        return []
    lines = buf.decode("utf-8", errors="replace").splitlines()
    return [line for line in lines if line.strip()][-count:]


def _normalize_row(row: dict) -> dict:
    """옛 기록(status 없음)에도 status/model_used를 채워 준다."""
    row.setdefault("status", "ok" if row.get("ok") else "fail")
    row.setdefault("model_used", row.get("model") or "")
    row.setdefault("model_requested", row.get("model") or "")
    row["status_label"] = STATUS_LABEL.get(row["status"], row["status"])
    return row


def list_runs(job_id: str | None = None, limit: int = 40) -> list[dict]:
    """최근 실행 기록(최신순). job_id가 없으면 전체(단, try 기록은 제외)."""
    ensure_dirs()
    limit = max(1, int(limit or 40))
    if job_id:
        files = [RUNS_DIR / f"{job_id}.jsonl"]
    else:
        files = [p for p in sorted(RUNS_DIR.glob("*.jsonl")) if p.stem != "try"]
    keyed: list[tuple[str, int, dict]] = []
    for path in files:
        for index, line in enumerate(_tail_lines(path, limit)):
            try:
                row = _normalize_row(json.loads(line))
            except json.JSONDecodeError:
                continue
            keyed.append((row.get("at") or "", index, row))  # 같은 초에 여러 건이면 파일 뒤쪽이 최신
    keyed.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [row for _, _, row in keyed[:limit]]


def runs_since(hours: int) -> list[dict]:
    """최근 hours 시간 안의 기록(타임라인용)."""
    cutoff = (datetime.now(SEOUL) - timedelta(hours=max(1, int(hours)))).isoformat(timespec="seconds")
    return [r for r in list_runs(None, limit=2000) if (r.get("at") or "") >= cutoff]


# --- 진행 중 표시 · per-job 잠금 --------------------------------------------------


def active() -> dict | None:
    """지금 실행 중인 작업. pid가 죽어 있으면 파일을 지우고 None."""
    try:
        data = json.loads(ACTIVE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not _pid_alive(int(data.get("pid") or 0)):
        _clear_active(None)
        return None
    return data


def _write_active(ctx: RunContext, waiting: str = "") -> None:
    payload = {
        "job_id": ctx.job_id,
        "title": ctx.title,
        "model": ctx.model,
        "started_at": datetime.fromtimestamp(ctx.started, SEOUL).isoformat(timespec="seconds"),
        "pid": ctx.pid,
        "waiting": waiting,
    }
    ACTIVE_FILE.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def _clear_active(pid: int | None) -> None:
    """pid가 주어지면 그 pid가 쓴 파일일 때만 지운다(다른 실행의 표시를 지우지 않게)."""
    try:
        if pid is not None:
            data = json.loads(ACTIVE_FILE.read_text(encoding="utf-8"))
            if int(data.get("pid") or 0) != pid:
                return
        ACTIVE_FILE.unlink()
    except (OSError, json.JSONDecodeError, ValueError):
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class JobLock:
    """Stable flock inode excludes both HTTP threads and cron processes.

    The separate PID file records crashes; the lock file must never be unlinked.
    """

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.path = PID_DIR / f"job-{job_id}.pid"
        self.lock_path = PID_DIR / f"job-{job_id}.lock"
        self._handle = None
        self._owner_pid = None

    def acquire(self) -> bool:
        PID_DIR.mkdir(parents=True, exist_ok=True)
        if self._handle is not None:
            return False
        handle = self.lock_path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        try:
            previous = self._read()
            if previous and not _pid_alive(previous):
                self._record_aborted(previous)
            self.path.write_text(str(os.getpid()), encoding="utf-8")
        except BaseException:
            handle.close()
            raise
        self._handle = handle
        self._owner_pid = os.getpid()
        return True

    def release(self) -> None:
        if self._handle is None or self._owner_pid != os.getpid():
            return
        try:
            if self._read() == os.getpid():
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
        finally:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._handle.close()
                self._handle = None
                self._owner_pid = None

    def _read(self) -> int | None:
        try:
            return int(self.path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def _record_aborted(self, pid: int) -> None:
        try:
            started = self.path.stat().st_mtime
        except OSError:
            started = time.time()
        job = jobs_mod.get_job(self.job_id) or {}
        ctx = RunContext(job_id=self.job_id, title=str(job.get("title") or self.job_id), model=str(job.get("model") or ""), prompt="", started=started, pid=pid)
        _persist(self.job_id, _record(ctx, "aborted", error="중단됨 (이전 실행이 끝나지 않았습니다)"))
        _clear_active(pid)


# --- CLI ---------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    try:
        result = run_job(args.job, scheduled=True)
        print(json.dumps({"ok": result["ok"], "status": result.get("status"), "id": result["id"], "seconds": result["seconds"]}, ensure_ascii=False))
        return 0 if result["ok"] else 1
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
