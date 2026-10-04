"""받을 수 있는 모델 목록을 ollama.com에서 가져온다.

ollama.com에는 목록 API가 없어 /library 페이지(다운로드 수 순)를 읽는다.
태그 크기는 레지스트리 manifest(공식 v2 API)의 레이어 합으로 잰다.

고르는 규칙: 최근 MAX_AGE_DAYS일 안에 갱신된 로컬 모델(크기 태그가 있는 것) 중
임베딩·판정 전용을 빼고, 다운로드 수 순으로 이 맥 램에 들어가는 크기를 MAX_ROWS개까지.

설치 화면만 fresh=True로 부른다. 캐시가 TTL_S보다 오래됐으면 그때 새로 읽는다.
실패하면 마지막으로 읽은 목록, 그것도 없으면 hardware.ALL_MODELS(기본 목록)를 쓴다.
"""

from __future__ import annotations

import concurrent.futures
import html
import json
import os
import re
import threading
import urllib.request
from datetime import datetime, timezone
from typing import Any

from desk.paths import DATA, LOGS_DIR
from desk.state import read_json, write_json

LIBRARY_URL = "https://ollama.com/library?sort=popular"
MANIFEST_URL = "https://registry.ollama.ai/v2/library/{name}/manifests/{tag}"
MANIFEST_ACCEPT = "application/vnd.docker.distribution.manifest.v2+json"
USER_AGENT = "free-ai-scheduler"
CACHE_PATH = DATA / "library.json"
TTL_S = 10 * 60
MAX_AGE_DAYS = 180
MAX_FAMILIES = 20
MAX_ROWS = 20
MIN_ROWS = 4
PAGE_TIMEOUT_S = 10
MANIFEST_TIMEOUT_S = 6
MANIFEST_WORKERS = 16
MANIFEST_BUDGET_S = 8
SKIP_CAPS = {"embedding", "decision"}
SKIP_NAMES = ("embed", "guard", "ocr", "medgemma", "rerank")
RAM_TIERS = (8, 16, 18, 24, 32, 36, 48, 64, 96, 128, 192, 256, 512)
FAST_MAX_GB = 7.0

_CARD = re.compile(r'<a\s+href="/library/([a-z0-9][a-z0-9._-]*)"[^>]*>(.*?)</a>', re.S)
_PULLS = re.compile(r">\s*([\d.,]+)\s*([KMB]?)\s*</span>\s*<span[^>]*>(?:&nbsp;|\s)*Pulls", re.S)
_UPDATED = re.compile(r'title="([A-Z][a-z]{2} \d{1,2}, \d{4} \d{1,2}:\d{2} [AP]M UTC)"')
_CAP = re.compile(r"<span[^>]*bg-indigo-50[^>]*>\s*([^<]+?)\s*</span>")
_SIZE = re.compile(r"<span[^>]*bg-\[#ddf4ff\][^>]*>\s*([^<]+?)\s*</span>")
_SIZE_TAG = re.compile(r"e?\d+(?:\.\d+)?[bm]")
_PARAMS = re.compile(r"(\d+(?:\.\d+)?)([bm])")

_lock = threading.Lock()


# ── 페이지 읽기 ──────────────────────────────────────────────────────────────


def parse_library(page: str) -> list[dict[str, Any]]:
    """/library 페이지 → [{name, pulls, updated, caps, sizes}]. 다운로드 수나 갱신 시각을 못 읽은 카드는 버린다."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name, body in _CARD.findall(page):
        pulls = _PULLS.search(body)
        updated = _UPDATED.search(body)
        if name in seen or not pulls or not updated:
            continue
        seen.add(name)
        sizes = (html.unescape(s).strip().lower() for s in _SIZE.findall(body))
        out.append(
            {
                "name": name,
                "pulls": _count(pulls.group(1), pulls.group(2)),
                "updated": _parse_time(updated.group(1)),
                "caps": [html.unescape(c).strip().lower() for c in _CAP.findall(body)],
                "sizes": [s for s in sizes if _SIZE_TAG.fullmatch(s)],
            }
        )
    return out


def _count(number: str, unit: str) -> int:
    return int(float(number.replace(",", "")) * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[unit])


def _parse_time(text: str) -> str:
    return datetime.strptime(text, "%b %d, %Y %I:%M %p UTC").replace(tzinfo=timezone.utc).isoformat()


def _eligible(fam: dict[str, Any], now: datetime) -> bool:
    """크기 태그가 없으면 클라우드 전용이다."""
    if not fam["sizes"] or SKIP_CAPS & set(fam["caps"]):
        return False
    if any(word in fam["name"] for word in SKIP_NAMES):
        return False
    return (now - datetime.fromisoformat(fam["updated"])).days <= MAX_AGE_DAYS


# ── 고르기 ───────────────────────────────────────────────────────────────────


def min_ram(size_gb: float) -> int:
    """이 크기를 돌릴 수 있는 가장 작은 램 등급. 가중치 + 1.5GB가 램의 92% 안에 들어야 한다."""
    return next((tier for tier in RAM_TIERS if size_gb + 1.5 <= tier * 0.92), 1024)


def _too_big(tag: str, ram_gb: int) -> bool:
    """파라미터 수로 봐도 확실히 안 들어가는 태그는 manifest를 읽지 않는다. e2b 같은 실효 파라미터 태그는 읽는다."""
    m = _PARAMS.fullmatch(tag)
    if not m:
        return False
    billions = float(m.group(1)) / (1000 if m.group(2) == "m" else 1)
    return billions * 0.55 + 1.5 > ram_gb * 0.92


def select(data: dict[str, Any], ram_gb: int) -> list[dict[str, Any]]:
    """캐시 → 이 맥에 맞는 행. 다운로드 수 순으로 MAX_ROWS개를 고른 뒤 크기 순으로 정렬한다."""
    rows: list[dict[str, Any]] = []
    for fam in data.get("families") or []:
        for tag in fam.get("sizes") or []:
            size = (fam.get("sizes_gb") or {}).get(tag)
            if not size or min_ram(size) > ram_gb:
                continue
            rows.append(
                {
                    "id": f"{fam['name']}:{tag}",
                    "role": "빠른 답" if size < FAST_MAX_GB else "추론·코딩",
                    "size_gb": size,
                    "min_ram": min_ram(size),
                    "pulls": fam.get("pulls") or 0,
                }
            )
    rows = rows[:MAX_ROWS]
    rows.sort(key=lambda r: (r["size_gb"], -r["pulls"]))
    return rows


def builtin(ram_gb: int) -> list[dict[str, Any]]:
    from desk.hardware import ALL_MODELS

    return [dict(m) for m in ALL_MODELS if int(m.get("min_ram") or 8) <= ram_gb]


# ── 갱신 ─────────────────────────────────────────────────────────────────────


def refresh(ram_gb: int) -> dict[str, Any]:
    """페이지를 읽고 크기를 재서 캐시에 쓴다. 갱신 시각이 그대로인 모델은 지난번 크기를 다시 쓴다."""
    page = _get(LIBRARY_URL, PAGE_TIMEOUT_S).decode("utf-8", "replace")
    now = datetime.now(timezone.utc)
    fams = sorted((f for f in parse_library(page) if _eligible(f, now)), key=lambda f: -f["pulls"])[:MAX_FAMILIES]
    previous = {f["name"]: f for f in (read_cache() or {}).get("families") or []}
    todo: list[tuple[dict[str, Any], str]] = []
    for fam in fams:
        prev = previous.get(fam["name"]) or {}
        known = (prev.get("sizes_gb") or {}) if prev.get("updated") == fam["updated"] else {}
        fam["sizes_gb"] = {}
        for tag in fam["sizes"]:
            if _too_big(tag, ram_gb):
                continue
            if known.get(tag):
                fam["sizes_gb"][tag] = known[tag]
            else:
                todo.append((fam, tag))
    _measure(todo)
    data = {"fetched_at": now.isoformat(timespec="seconds"), "families": fams}
    found = len(select(data, ram_gb))
    if found < MIN_ROWS:
        raise RuntimeError(f"고를 수 있는 모델이 {found}개뿐입니다. ollama.com 페이지 형식이 바뀌었을 수 있습니다.")
    write_json(CACHE_PATH, data)
    return data


def _measure(todo: list[tuple[dict[str, Any], str]]) -> None:
    """태그 크기를 동시에 잰다. MANIFEST_BUDGET_S 안에 못 잰 태그는 이번 목록에서 빠지고 다음 갱신 때 다시 잰다."""
    if not todo:
        return
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=MANIFEST_WORKERS)
    futures = {pool.submit(_manifest_gb, fam["name"], tag): (fam, tag) for fam, tag in todo}
    done, late = concurrent.futures.wait(futures, timeout=MANIFEST_BUDGET_S)
    pool.shutdown(wait=False, cancel_futures=True)
    for future in done:
        fam, tag = futures[future]
        size = future.result()
        if size:
            fam["sizes_gb"][tag] = size
    if late:
        _log(f"크기 {len(late)}개를 {MANIFEST_BUDGET_S}초 안에 못 읽어 이번 목록에서 뺐습니다")


def _manifest_gb(name: str, tag: str) -> float | None:
    try:
        raw = _get(MANIFEST_URL.format(name=name, tag=tag), MANIFEST_TIMEOUT_S, accept=MANIFEST_ACCEPT)
        layers = json.loads(raw).get("layers") or []
    except Exception as exc:
        _log(f"{name}:{tag} 크기 못 읽음: {exc}")
        return None
    total = sum(int(layer.get("size") or 0) for layer in layers)
    return round(total / 1e9, 1) if total else None


def _get(url: str, timeout: float, accept: str = "") -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
        return resp.read()


# ── 바깥에서 쓰는 것 ─────────────────────────────────────────────────────────


def models(ram_gb: int, fresh: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """(행, 출처). fresh면 캐시가 TTL보다 오래됐을 때 새로 읽는다. 행은 hardware.ALL_MODELS와 같은 모양이다."""
    cache = read_cache()
    error = ""
    if fresh and not offline() and _age_s(cache) > TTL_S:
        with _lock:
            cache = read_cache()
            if _age_s(cache) > TTL_S:
                try:
                    cache = refresh(ram_gb)
                    return select(cache, ram_gb), _source("live", cache)
                except Exception as exc:
                    _log(f"목록 갱신 실패: {exc}")
                    error = str(exc) or exc.__class__.__name__
    rows = select(cache, ram_gb) if cache else []
    if len(rows) >= MIN_ROWS:
        return rows, _source("cache", cache, error)
    return builtin(ram_gb), _source("builtin", None, error)


def cached_size_gb(model: str) -> float | None:
    """캐시에 있는 태그 크기(GB). 네트워크는 쓰지 않는다."""
    name, _, tag = model.partition(":")
    for fam in (read_cache() or {}).get("families") or []:
        if fam.get("name") == name:
            return (fam.get("sizes_gb") or {}).get(tag or "latest")
    return None


def read_cache() -> dict[str, Any] | None:
    data = read_json(CACHE_PATH, None)
    return data if isinstance(data, dict) and data.get("fetched_at") else None


def offline() -> bool:
    return os.environ.get("DESK_LIBRARY_OFFLINE") == "1"


def _age_s(cache: dict[str, Any] | None) -> float:
    if not cache:
        return float("inf")
    try:
        fetched = datetime.fromisoformat(cache["fetched_at"])
    except (KeyError, TypeError, ValueError):
        return float("inf")
    return (datetime.now(timezone.utc) - fetched).total_seconds()


def _source(kind: str, cache: dict[str, Any] | None, error: str = "") -> dict[str, Any]:
    fetched = cache.get("fetched_at") if cache else None
    if kind == "live":
        label = "ollama.com 최신 목록 · 방금 확인"
    elif kind == "cache" and not error:
        label = f"ollama.com 목록 · {_ago(_age_s(cache))} 확인"
    elif kind == "cache":
        label = f"최신 목록을 못 읽어 {_ago(_age_s(cache))} 목록을 보여줍니다"
    elif error:
        label = "최신 목록을 못 읽어 기본 목록을 보여줍니다"
    else:
        label = "기본 목록"
    return {"source": kind, "fetched_at": fetched, "error": error or None, "label": label}


def _ago(seconds: float) -> str:
    if seconds < 60:
        return "방금"
    if seconds < 3600:
        return f"{int(seconds // 60)}분 전"
    if seconds < 48 * 3600:
        return f"{int(seconds // 3600)}시간 전"
    return f"{int(seconds // 86400)}일 전"


def _log(line: str) -> None:
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOGS_DIR / "library.log", "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')} {line}\n")
    except OSError:
        pass
