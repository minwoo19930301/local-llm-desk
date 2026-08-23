from __future__ import annotations

import platform
import re
import shutil
import subprocess
import sys
from typing import Any

# size_gb: ollama.com library Q4 기준. min_ram: 이 이하면 목록에서 뺌.
ALL_MODELS: list[dict[str, Any]] = [
    {"id": "llama3.2:1b", "role": "빠른 답", "size_gb": 1.3, "min_ram": 8},
    {"id": "llama3.2:3b", "role": "빠른 답", "size_gb": 2.0, "min_ram": 8},
    {"id": "qwen3.5:2b", "role": "빠른 답", "size_gb": 2.7, "min_ram": 8},
    {"id": "qwen3.5:4b", "role": "빠른 답", "size_gb": 3.4, "min_ram": 8},
    {"id": "deepseek-r1:7b", "role": "추론·코딩", "size_gb": 4.7, "min_ram": 8},
    {"id": "qwen2.5-coder:7b", "role": "추론·코딩", "size_gb": 4.7, "min_ram": 8},
    {"id": "qwen3.5:9b", "role": "빠른 답", "size_gb": 6.6, "min_ram": 16},
    {"id": "gemma4:e2b", "role": "빠른 답", "size_gb": 7.2, "min_ram": 16},
    {"id": "gemma4:12b", "role": "빠른 답", "size_gb": 7.6, "min_ram": 16},
    {"id": "qwen2.5-coder:14b", "role": "추론·코딩", "size_gb": 9.0, "min_ram": 16},
    {"id": "deepseek-r1:14b", "role": "추론·코딩", "size_gb": 9.0, "min_ram": 16},
    {"id": "gemma4:e4b", "role": "빠른 답", "size_gb": 9.6, "min_ram": 16},
    {"id": "qwen3.6:27b", "role": "추론·코딩", "size_gb": 17.0, "min_ram": 24},
    {"id": "qwen3.5:27b", "role": "추론·코딩", "size_gb": 17.0, "min_ram": 24},
    {"id": "gemma4:26b", "role": "추론·코딩", "size_gb": 18.0, "min_ram": 24},
    {"id": "glm-4.7-flash", "role": "추론·코딩", "size_gb": 19.0, "min_ram": 24},
    {"id": "gemma4:31b", "role": "추론·코딩", "size_gb": 20.0, "min_ram": 24},
    {"id": "deepseek-r1:32b", "role": "추론·코딩", "size_gb": 20.0, "min_ram": 24},
    {"id": "qwen3.6:35b", "role": "추론·코딩", "size_gb": 24.0, "min_ram": 48},
]


def _sysctl(key: str) -> str:
    return subprocess.check_output(["sysctl", "-n", key], text=True).strip()


def parse_chip(brand: str) -> tuple[str, str]:
    low = brand.lower()
    gen = "unknown"
    for token in ("m5", "m4", "m3", "m2", "m1"):
        if re.search(rf"\b{token}\b", low):
            gen = token
            break
    klass = "base"
    if "ultra" in low:
        klass = "ultra"
    elif "max" in low:
        klass = "max"
    elif "pro" in low:
        klass = "pro"
    return gen, klass


def usable_ram(ram_gb: int, gen: str, klass: str) -> int:
    """Unified memory left for weights after macOS + 브라우저."""
    if ram_gb <= 8:
        head = 3
    elif ram_gb <= 16:
        head = 5
    else:
        head = 6
    if gen in {"m1", "m2"}:
        head += 1
    if klass == "base" and ram_gb <= 18:
        head += 1
    return max(3, ram_gb - head)


def _ram_bytes() -> int:
    if sys.platform == "darwin":
        return int(_sysctl("hw.memsize"))
    if sys.platform == "win32":
        import ctypes

        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        info = MemoryStatusEx()
        info.dwLength = ctypes.sizeof(MemoryStatusEx)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(info))
        return int(info.ullTotalPhys)
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 8 * 1024**3


def detect() -> dict[str, Any]:
    ram_gb = round(_ram_bytes() / (1024**3))
    if sys.platform == "darwin":
        chip = _sysctl("machdep.cpu.brand_string")
        ver = subprocess.check_output(["sw_vers", "-productVersion"], text=True).strip()
        arch = subprocess.check_output(["uname", "-m"], text=True).strip()
    else:
        chip = platform.processor() or platform.machine() or sys.platform
        ver = platform.version()
        arch = platform.machine()
    gen, klass = parse_chip(chip)
    disk = shutil.disk_usage("/")
    usable = usable_ram(ram_gb, gen, klass)
    return {
        "chip": chip,
        "chip_gen": gen,
        "chip_class": klass,
        "arch": arch,
        "ram_gb": ram_gb,
        "usable_gb": usable,
        "os": sys.platform,
        "macos": ver,
        "disk_free_gb": round(disk.free / (1024**3)),
        "disk_total_gb": round(disk.total / (1024**3)),
        "band": ram_band(ram_gb),
        "recommend_label": f"{chip} · 램 {ram_gb}GB 기준",
    }


def ram_band(ram_gb: int) -> str:
    if ram_gb >= 48:
        return "48gb"
    if ram_gb >= 36:
        return "36gb"
    if ram_gb >= 24:
        return "24gb"
    if ram_gb >= 16:
        return "16gb"
    return "8gb"


def models_for(hw: dict[str, Any] | int) -> list[dict[str, Any]]:
    if isinstance(hw, int):
        hw = {"ram_gb": hw, "chip_gen": "m3", "chip_class": "base", "usable_gb": usable_ram(hw, "m3", "base")}
    ram = int(hw["ram_gb"])
    usable = int(hw.get("usable_gb") or usable_ram(ram, hw.get("chip_gen") or "m3", hw.get("chip_class") or "base"))
    out: list[dict[str, Any]] = []
    for src in ALL_MODELS:
        m = dict(src)
        if m.get("skip"):
            continue
        if ram < int(m.get("min_ram") or 8):
            continue
        size = float(m["size_gb"])
        if size > ram * 0.92:
            continue
        m["tight"] = size > usable
        out.append(m)
    _mark_picks(out, usable)
    return out


def _mark_picks(models: list[dict[str, Any]], usable: int) -> None:
    def nearest(rows: list[dict[str, Any]], target: float) -> dict[str, Any]:
        return min(rows, key=lambda m: abs(float(m["size_gb"]) - target))

    fast = [m for m in models if m.get("role") == "빠른 답" and not m.get("skip") and not m.get("tight")]
    smart = [m for m in models if m.get("role") == "추론·코딩" and not m.get("skip") and not m.get("tight")]
    if not smart:
        smart = [m for m in models if m.get("role") == "추론·코딩" and not m.get("skip")]
    if fast:
        nearest(fast, min(8.0, usable * 0.45))["pick"] = True
    if smart:
        nearest(smart, usable * 0.9)["pick"] = True


def model_plan(hw: dict[str, Any] | int) -> dict[str, Any]:
    if isinstance(hw, int):
        models = models_for(hw)
        ram = hw
        label = f"램 {ram}GB 기준"
    else:
        models = models_for(hw)
        ram = int(hw["ram_gb"])
        label = hw.get("recommend_label") or f"램 {ram}GB 기준"
    runnable = [m for m in models if not m.get("skip")]
    lights = [m for m in runnable if m.get("role") == "빠른 답"]
    strongs = [m for m in runnable if m.get("role") == "추론·코딩"]
    light = next((m for m in lights if m.get("pick")), lights[0] if lights else runnable[0])
    strong = next((m for m in strongs if m.get("pick")), strongs[-1] if strongs else light)
    return {
        "light": light["id"],
        "strong": strong["id"],
        "light_size": f"{light['size_gb']}GB",
        "strong_size": f"{strong['size_gb']}GB",
        "models": models,
        "label": label,
    }
