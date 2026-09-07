"""실행 직전 RAM 게이트.

NEED = W(모델 파일) + KV(num_ctx 비례) + C(연산 버퍼) + R(런너 고정비).
AVAIL = min(vm_stat 여유, kern.memorystatus_level% × 전체) − 1 GiB.
통과 조건: NEED × 1.10 <= AVAIL − margin. 자세한 근거는 research_ollamaRam.md §4.
모든 용량 단위는 GiB(1024³)이며 필드 이름은 관례상 ``_gb``.
"""
from __future__ import annotations

import json
import struct
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO
from zoneinfo import ZoneInfo

from desk import hardware, ollama_ctl

GIB = 1024**3
MIB = 1024**2
KV_FALLBACK_PER_TOKEN = 256 * 1024  # B/token. dense ≤32B GQA 모델을 덮는다
RUNNER_OVERHEAD = 400 * MIB
COMPUTE_MIN = 512 * MIB
PROJECTOR_EXTRA = 400 * MIB
AVAIL_MARGIN = 1.0 * GIB
GATE_FACTOR = 1.10
PRESSURE_BLOCK = 2  # kern.memorystatus_vm_pressure_level: 1 normal, 2 warn, 4 critical — 표시용 경고 기준, 차단 기준 아님
UNKNOWN_NEED_GB = 4.0
MANIFESTS = Path.home() / ".ollama" / "models" / "manifests"
BLOBS = Path.home() / ".ollama" / "models" / "blobs"
SEOUL = ZoneInfo("Asia/Seoul")

_shape_cache: dict[str, dict[str, int] | None] = {}


# --- 스냅샷 -----------------------------------------------------------------


def snapshot() -> dict[str, Any]:
    """지금 RAM 상태. 게이트가 실제로 쓰는 값은 ``avail_gb``."""
    total = _memsize()
    free = _vm_stat_free()
    level = _sysctl_int("kern.memorystatus_level")
    pressure = _sysctl_int("kern.memorystatus_vm_pressure_level")
    swap_used, swap_total = _swap_usage()
    avail = free
    if level is not None and total:
        avail = min(free, total * level / 100)
    avail = max(0, avail - AVAIL_MARGIN)
    return {
        "total_gb": _gb(total),
        "free_gb": _gb(free),
        "avail_gb": _gb(avail),
        "pressure_pct": level,
        "pressure_level": pressure,
        "swap_used_gb": _gb(swap_used),
        "swap_total_gb": _gb(swap_total),
        "swap_warn": bool(swap_total and swap_used / swap_total > 0.5),
        "loaded": _loaded(),
        "at": datetime.now(SEOUL).isoformat(timespec="seconds"),
    }


def _loaded() -> list[dict[str, Any]]:
    try:
        rows = ollama_ctl.loaded_models(timeout=2.0)
    except Exception:
        return []
    return [{"model": r.get("name") or r.get("model") or "", "size_gb": _gb(int(r.get("size") or 0))} for r in rows]


def _memsize() -> int:
    if sys.platform == "darwin":
        return _sysctl_int("hw.memsize") or 0
    return hardware._ram_bytes()


def _vm_stat_free() -> int:
    """free+inactive+purgeable+speculative 페이지 × 페이지 크기 (바이트)."""
    if sys.platform != "darwin":
        return int(hardware._ram_free_gb() * GIB)
    page = _sysctl_int("hw.pagesize") or 16384
    try:
        text = subprocess.check_output(["vm_stat"], text=True, timeout=5)
    except (subprocess.SubprocessError, OSError):
        return 0
    keys = ("Pages free", "Pages speculative", "Pages inactive", "Pages purgeable")
    pages = 0
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key.strip() in keys:
            digits = value.strip().rstrip(".").replace(",", "")
            pages += int(digits) if digits.isdigit() else 0
    return pages * page


def _swap_usage() -> tuple[int, int]:
    """vm.swapusage → (used, total) 바이트. 없으면 (0, 0)."""
    text = _sysctl("vm.swapusage")
    if not text:
        return 0, 0
    values: dict[str, int] = {}
    fields = text.split()  # "total = 20480.00M  used = 19311.31M  free = 1168.69M  (encrypted)"
    for idx, tok in enumerate(fields):
        if tok in ("total", "used") and idx + 2 < len(fields):
            values[tok] = _parse_size(fields[idx + 2])
    return values.get("used", 0), values.get("total", 0)


def _parse_size(text: str) -> int:
    units = {"K": 1024, "M": MIB, "G": GIB, "T": 1024**4}
    text = text.strip()
    if not text:
        return 0
    unit = text[-1].upper()
    if unit in units:
        try:
            return int(float(text[:-1]) * units[unit])
        except ValueError:
            return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def _sysctl(key: str) -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        return subprocess.check_output(["sysctl", "-n", key], text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
    except (subprocess.SubprocessError, OSError):
        return None


def _sysctl_int(key: str) -> int | None:
    text = _sysctl(key)
    if text is None:
        return None
    try:
        return int(text.split()[0])
    except (ValueError, IndexError):
        return None


def _gb(value: float | int) -> float:
    return round(value / GIB, 2)


# --- 모델 필요량 -------------------------------------------------------------


def model_need_gb(model: str, num_ctx: int = 4096) -> float:
    """모델을 num_ctx로 띄울 때 필요한 RAM(GiB). 크기를 모르면 4.0."""
    weights = _weight_bytes(model)
    if not weights:
        return UNKNOWN_NEED_GB
    return _gb(_need_bytes(model, weights, num_ctx))


def _need_bytes(model: str, weights: int, num_ctx: int) -> int:
    shape = _shape(model)
    num_ctx = max(1, int(num_ctx or 4096))
    if shape:
        kv = 2 * shape["n_layer"] * shape["n_head_kv"] * shape["head_dim"] * 2 * num_ctx
    else:
        kv = KV_FALLBACK_PER_TOKEN * num_ctx
    compute = max(COMPUTE_MIN, int(0.10 * weights))
    if _has_projector(model):
        compute += PROJECTOR_EXTRA
    if num_ctx > 8192 and shape and shape.get("n_head"):
        compute += shape["n_head"] * 512 * num_ctx * 4  # flash attention 꺼진 서버의 attention scratch
    return weights + kv + compute + RUNNER_OVERHEAD


def _weight_bytes(model: str) -> int:
    """/api/tags size → manifest 레이어 합 → hardware.ALL_MODELS size_gb → 0."""
    for row in _tags():
        if (row.get("name") or row.get("model")) == model:
            return int(row.get("size") or 0)
    manifest = _manifest(model)
    if manifest:
        layers = manifest.get("layers") or []
        config = manifest.get("config") or {}
        return sum(int(layer.get("size") or 0) for layer in layers) + int(config.get("size") or 0)
    for row in hardware.ALL_MODELS:
        if row["id"] == model:
            return int(float(row["size_gb"]) * 1000**3)
    return 0


def _tags() -> list[dict[str, Any]]:
    try:
        return ollama_ctl.list_models()
    except Exception:
        return []


def _manifest(model: str) -> dict[str, Any] | None:
    path = _manifest_path(model)
    if not path or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _manifest_path(model: str) -> Path | None:
    name, _, tag = model.partition(":")
    tag = tag or "latest"
    namespace, _, short = name.rpartition("/")
    if not MANIFESTS.is_dir():
        return None
    for host in MANIFESTS.iterdir():
        candidate = host / (namespace or "library") / short / tag
        if candidate.is_file():
            return candidate
    return None


def _has_projector(model: str) -> bool:
    manifest = _manifest(model)
    if not manifest:
        return False
    return any(str(layer.get("mediaType") or "").endswith(".projector") for layer in manifest.get("layers") or [])


def _shape(model: str) -> dict[str, int] | None:
    """KV 계산 입력 {n_layer, n_head_kv, head_dim, n_head}. /api/show → GGUF 헤더 → None."""
    if model in _shape_cache:
        return _shape_cache[model]
    shape = _shape_from_show(model) or _shape_from_gguf(model)
    _shape_cache[model] = shape
    return shape


def _shape_from_show(model: str) -> dict[str, int] | None:
    if not ollama_ctl.running():
        return None
    try:
        info = ollama_ctl.show_model(model).get("model_info") or {}
    except Exception:
        return None
    return _shape_from_meta(info)


def _shape_from_meta(meta: dict[str, Any]) -> dict[str, int] | None:
    arch = str(meta.get("general.architecture") or "")
    if not arch:
        return None

    def num(key: str) -> int:
        try:
            return int(meta.get(f"{arch}.{key}") or 0)
        except (TypeError, ValueError):
            return 0

    n_layer = num("block_count")
    n_head = num("attention.head_count")
    n_head_kv = num("attention.head_count_kv") or n_head
    head_dim = num("attention.key_length")
    if not head_dim and n_head:
        head_dim = num("embedding_length") // n_head
    if not (n_layer and n_head_kv and head_dim):
        return None
    return {"n_layer": n_layer, "n_head_kv": n_head_kv, "head_dim": head_dim, "n_head": n_head}


def _shape_from_gguf(model: str) -> dict[str, int] | None:
    manifest = _manifest(model)
    if not manifest:
        return None
    for layer in manifest.get("layers") or []:
        if str(layer.get("mediaType") or "").endswith(".model"):
            blob = BLOBS / str(layer.get("digest") or "").replace(":", "-")
            meta = _gguf_metadata(blob)
            return _shape_from_meta(meta) if meta else None
    return None


# --- GGUF 헤더 (메타데이터만, 텐서는 읽지 않는다) ------------------------------

_GGUF_SCALARS = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
_GGUF_STRING, _GGUF_ARRAY = 8, 9
_GGUF_KEEP = ("general.architecture", "block_count", "attention.head_count", "attention.head_count_kv", "attention.key_length", "embedding_length")
_GGUF_READ_CAP = 64 * MIB


def _gguf_metadata(path: Path) -> dict[str, Any] | None:
    """GGUF v2/v3 헤더의 스칼라 메타데이터. 실패하면 None."""
    try:
        with path.open("rb") as fh:
            if fh.read(4) != b"GGUF":
                return None
            version = struct.unpack("<I", fh.read(4))[0]
            if version < 2:
                return None
            fh.read(8)  # tensor_count
            count = struct.unpack("<Q", fh.read(8))[0]
            meta: dict[str, Any] = {}
            for _ in range(min(count, 4096)):
                if fh.tell() > _GGUF_READ_CAP:
                    break
                key = _gguf_string(fh)
                kind = struct.unpack("<I", fh.read(4))[0]
                value = _gguf_value(fh, kind)
                if any(key.endswith(k) for k in _GGUF_KEEP) and not isinstance(value, list):
                    meta[key] = value
            return meta or None
    except (OSError, struct.error, UnicodeDecodeError, ValueError):
        return None


def _gguf_string(fh: BinaryIO) -> str:
    length = struct.unpack("<Q", fh.read(8))[0]
    if length > 16 * MIB:
        raise ValueError("gguf string too long")
    return fh.read(length).decode("utf-8", errors="replace")


def _gguf_value(fh: BinaryIO, kind: int) -> Any:
    if kind in _GGUF_SCALARS:
        fmt = _GGUF_SCALARS[kind]
        return struct.unpack(fmt, fh.read(struct.calcsize(fmt)))[0]
    if kind == _GGUF_STRING:
        return _gguf_string(fh)
    if kind == _GGUF_ARRAY:
        item_kind = struct.unpack("<I", fh.read(4))[0]
        length = struct.unpack("<Q", fh.read(8))[0]
        if item_kind in _GGUF_SCALARS:
            fh.seek(length * struct.calcsize(_GGUF_SCALARS[item_kind]), 1)
            return []
        for _ in range(length):
            _gguf_value(fh, item_kind)
        return []
    raise ValueError(f"unknown gguf type {kind}")


# --- 설치 모델·판정 -------------------------------------------------------------


def installed_models() -> list[str]:
    """/api/tags 이름들, 서버가 없으면 manifests에서."""
    names = [str(r.get("name") or r.get("model") or "") for r in _tags()]
    names = [n for n in names if n]
    if names or not MANIFESTS.is_dir():
        return names
    for path in MANIFESTS.rglob("*"):
        if path.is_file():
            names.append(_model_from_manifest_path(path))
    return sorted(set(names))


def _model_from_manifest_path(path: Path) -> str:
    rel = path.relative_to(MANIFESTS).parts  # host / namespace / name / tag
    if len(rel) < 4:
        return path.name
    namespace, name, tag = rel[1], rel[2], rel[3]
    prefix = "" if namespace == "library" else f"{namespace}/"
    return f"{prefix}{name}:{tag}"


def decide(model: str, policy: str, num_ctx: int = 4096, fallback_model: str = "", margin_gb: float = 0.5) -> dict[str, Any]:
    """실행 직전 판정. action: run | wait | skip | downgrade."""
    snap = snapshot()
    need = model_need_gb(model, num_ctx)
    base = {"model": model, "free_gb": snap["free_gb"], "avail_gb": snap["avail_gb"], "need_gb": need, "pressure_level": snap["pressure_level"]}
    if any(row["model"] == model for row in snap["loaded"]):
        return {**base, "action": "run", "reason": "이미 로드된 모델"}
    if _fits(need, snap["avail_gb"], margin_gb):
        # 압력 레벨은 참고 정보로만 남긴다. 실제 여유가 있으면 막지 않는다 (27분 대기 사고 재발 방지).
        note = f" · 메모리 압력 높음 (레벨 {snap['pressure_level']})" if (snap["pressure_level"] or 0) >= PRESSURE_BLOCK else ""
        return {**base, "action": "run", "reason": f"여유 충분 (여유 {snap['avail_gb']}GB, 필요 {need}GB){note}"}
    shortage = f"램 부족 (여유 {snap['avail_gb']}GB, 필요 {need}GB)"
    if policy == "downgrade":
        smaller = _downgrade_target(model, num_ctx, fallback_model, snap["avail_gb"], margin_gb)
        if smaller:
            name, small_need = smaller
            return {**base, "action": "downgrade", "model": name, "need_gb": small_need, "reason": f"{shortage} → 더 작은 모델 {name} (필요 {small_need}GB)"}
        return {**base, "action": "skip", "reason": f"{shortage}, 들어가는 작은 모델도 없음"}
    if policy == "defer":
        return {**base, "action": "wait", "reason": shortage}
    return {**base, "action": "skip", "reason": shortage}


def _fits(need_gb: float, avail_gb: float, margin_gb: float) -> bool:
    return need_gb * GATE_FACTOR <= avail_gb - margin_gb


def _downgrade_target(model: str, num_ctx: int, fallback: str, avail_gb: float, margin_gb: float) -> tuple[str, float] | None:
    """fallback이 들어가면 그것, 아니면 설치 모델 중 들어가는 가장 큰 것."""
    installed = installed_models()
    fallback = (fallback or "").strip()
    if fallback and fallback != model and fallback in installed:
        need = model_need_gb(fallback, num_ctx)
        if _fits(need, avail_gb, margin_gb):
            return fallback, need
    candidates: list[tuple[float, str]] = []
    for name in installed:
        if name == model:
            continue
        need = model_need_gb(name, num_ctx)
        if _fits(need, avail_gb, margin_gb):
            candidates.append((need, name))
    if not candidates:
        return None
    need, name = max(candidates)
    return name, need
