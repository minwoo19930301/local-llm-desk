"""desk.library: ollama.com 목록 파싱·고르기·대체 경로. 네트워크는 쓰지 않는다."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desk import hardware, library  # noqa: E402

FIXTURE = (ROOT / "tests" / "fixtures" / "ollama_library.html").read_text(encoding="utf-8")
FIXTURE_DAY = datetime(2026, 10, 3, tzinfo=timezone.utc)


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(second=0, microsecond=0).isoformat()


def _fam(name: str, pulls: int, sizes_gb: dict[str, float], days_ago: float = 3, caps: tuple[str, ...] = ("tools",)) -> dict:
    return {"name": name, "pulls": pulls, "updated": _iso(days_ago), "caps": list(caps), "sizes": list(sizes_gb), "sizes_gb": dict(sizes_gb)}


def _card(fam: dict) -> str:
    """parse_library가 읽는 최소 카드 마크업(실제 페이지와 같은 클래스·속성)."""
    when = datetime.fromisoformat(fam["updated"])
    stamp = f"{when:%b} {when.day}, {when.year} {when.hour % 12 or 12}:{when:%M} {'AM' if when.hour < 12 else 'PM'} UTC"
    caps = "".join(f'<span class="rounded-md bg-indigo-50 px-2">{c}</span>' for c in fam["caps"])
    sizes = "".join(f'<span class="rounded-md bg-[#ddf4ff] px-2">{s}</span>' for s in fam["sizes"])
    return (
        f'<li class="flex items-baseline border-b"><a href="/library/{fam["name"]}" class="group w-full">'
        f"<div>{caps}{sizes}</div>"
        f'<p class="my-4"><span class="flex items-center"><span >{fam["pulls"]}</span>'
        f'<span class="hidden sm:flex">&nbsp;Pulls</span></span>'
        f'<span class="flex items-center" title="{stamp}"><span class="hidden sm:flex">Updated&nbsp;</span>'
        f"<span >3 days ago</span></span></p></a></li>"
    )


def _page(*fams: dict) -> bytes:
    return ("<ul>" + "".join(_card(f) for f in fams) + "</ul>").encode()


FOUR = [
    _fam("alpha", 9_000_000, {"2b": 2.0, "9b": 6.6, "27b": 17.4}),
    _fam("beta", 5_000_000, {"4b": 3.4, "12b": 8.0}),
]


class ParseTest(unittest.TestCase):
    def test_real_cards(self) -> None:
        fams = {f["name"]: f for f in library.parse_library(FIXTURE)}
        self.assertEqual(set(fams), {"qwen3.6", "llava", "nomic-embed-text", "deepseek-v4.1-flash"})
        self.assertEqual(fams["qwen3.6"]["sizes"], ["27b", "35b"])
        self.assertIn("tools", fams["qwen3.6"]["caps"])
        self.assertGreater(fams["qwen3.6"]["pulls"], 1_000_000)
        # 설명에 "Updated to version 1.6"이 있어도 갱신일은 title 속성에서 읽는다.
        self.assertTrue(fams["llava"]["updated"].startswith("2024-"))
        self.assertEqual(fams["deepseek-v4.1-flash"]["sizes"], [])  # 클라우드 전용
        self.assertEqual(fams["nomic-embed-text"]["caps"], ["embedding"])

    def test_eligible(self) -> None:
        fams = {f["name"]: f for f in library.parse_library(FIXTURE)}
        self.assertTrue(library._eligible(fams["qwen3.6"], FIXTURE_DAY))
        self.assertFalse(library._eligible(fams["llava"], FIXTURE_DAY))  # 180일 넘게 안 바뀜
        self.assertFalse(library._eligible(fams["nomic-embed-text"], FIXTURE_DAY))  # 임베딩
        self.assertFalse(library._eligible(fams["deepseek-v4.1-flash"], FIXTURE_DAY))  # 로컬 크기 없음


class SelectTest(unittest.TestCase):
    def test_min_ram_and_too_big(self) -> None:
        self.assertEqual([library.min_ram(s) for s in (4.7, 6.6, 20.4, 22.6)], [8, 16, 24, 32])
        self.assertTrue(library._too_big("397b", 24))
        self.assertFalse(library._too_big("27b", 24))
        self.assertFalse(library._too_big("e2b", 8))
        self.assertFalse(library._too_big("270m", 8))

    def test_fits_ram_roles_and_order(self) -> None:
        rows = library.select({"families": [_fam("big", 9, {"8b": 5.0, "70b": 40.0}), _fam("small", 1, {"4b": 5.0})]}, 24)
        self.assertEqual([r["id"] for r in rows], ["big:8b", "small:4b"])  # 같은 크기면 다운로드 수가 많은 쪽이 앞
        self.assertEqual(rows[0]["role"], "빠른 답")
        rows = library.select({"families": [_fam("x", 1, {"27b": 17.4})]}, 24)
        self.assertEqual((rows[0]["role"], rows[0]["min_ram"]), ("추론·코딩", 24))

    def test_row_cap_keeps_popular_families(self) -> None:
        fams = [_fam(f"m{i}", 100 - i, {f"{n}b": float(n) / 10 + 1 for n in range(1, 4)}) for i in range(10)]
        rows = library.select({"families": fams}, 24)
        self.assertEqual(len(rows), library.MAX_ROWS)
        self.assertNotIn("m9", {r["id"].split(":")[0] for r in rows})  # 가장 덜 받은 쪽이 잘린다

    def test_models_for_accepts_live_rows(self) -> None:
        rows = library.select({"families": FOUR}, 24)
        plan = hardware.model_plan({"ram_gb": 24, "ram_free_gb": 20, "chip_gen": "m3", "chip_class": "base"}, rows)
        self.assertEqual({m["id"] for m in plan["models"]}, {r["id"] for r in rows})
        self.assertTrue(plan["light"] and plan["strong"])


    def test_pick_prefers_popular_near_target(self) -> None:
        rows = [
            {"id": "rare:3b", "role": "빠른 답", "size_gb": 2.1, "min_ram": 8, "pulls": 500_000},
            {"id": "popular:2b", "role": "빠른 답", "size_gb": 2.7, "min_ram": 8, "pulls": 21_000_000},
            {"id": "big:27b", "role": "추론·코딩", "size_gb": 17.4, "min_ram": 24, "pulls": 1},
        ]
        plan = hardware.model_plan({"ram_gb": 24, "ram_free_gb": 5, "chip_gen": "m3", "chip_class": "base"}, rows)
        self.assertEqual(plan["light"], "popular:2b")
        builtin = [dict(r, pulls=0) for r in rows]  # 다운로드 수가 없으면 예전처럼 가장 가까운 것
        self.assertEqual(hardware.model_plan({"ram_gb": 24, "ram_free_gb": 5, "chip_gen": "m3", "chip_class": "base"}, builtin)["light"], "rare:3b")


class SourceTest(unittest.TestCase):
    """갱신 → 캐시 → 기본 목록 순서. 모든 테스트에서 실제 네트워크는 막는다."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        for patch in (
            mock.patch.object(library, "CACHE_PATH", self.dir / "library.json"),
            mock.patch.object(library, "LOGS_DIR", self.dir),
            mock.patch.object(library, "_get", side_effect=AssertionError("네트워크를 쓰면 안 됨")),
            mock.patch.dict(os.environ, {"DESK_LIBRARY_OFFLINE": ""}),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def _cache(self, minutes_ago: float, fams: list[dict]) -> None:
        fetched = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
        library.write_json(library.CACHE_PATH, {"fetched_at": fetched, "families": fams})

    def test_no_cache_and_offline_network_gives_builtin(self) -> None:
        with mock.patch.object(library, "_get", side_effect=urllib.error.URLError("down")):
            rows, src = library.models(24, fresh=True)
        self.assertEqual(src["source"], "builtin")
        self.assertTrue(src["error"])
        self.assertEqual(src["label"], "최신 목록을 못 읽어 기본 목록을 보여줍니다")
        self.assertEqual(rows, library.builtin(24))

    def test_stale_cache_used_when_refresh_fails(self) -> None:
        self._cache(120, FOUR)
        with mock.patch.object(library, "_get", side_effect=urllib.error.URLError("down")):
            rows, src = library.models(24, fresh=True)
        self.assertEqual(src["source"], "cache")
        self.assertIn("2시간 전 목록", src["label"])
        self.assertEqual(len(rows), 5)

    def test_recent_cache_skips_network(self) -> None:
        self._cache(2, FOUR)
        rows, src = library.models(24, fresh=True)
        library._get.assert_not_called()
        self.assertEqual((src["source"], src["label"], src["error"]), ("cache", "ollama.com 목록 · 2분 전 확인", None))

    def test_not_fresh_never_fetches(self) -> None:
        self._cache(600, FOUR)
        _, src = library.models(24)
        library._get.assert_not_called()
        self.assertEqual((src["source"], src["error"]), ("cache", None))

    def test_offline_env_never_fetches(self) -> None:
        with mock.patch.dict(os.environ, {"DESK_LIBRARY_OFFLINE": "1"}):
            _, src = library.models(24, fresh=True)
        library._get.assert_not_called()
        self.assertEqual((src["source"], src["error"]), ("builtin", None))

    def test_refresh_live_and_reuse_sizes(self) -> None:
        same = _fam("alpha", 9_000_000, {"2b": 2.0, "9b": 6.6, "27b": 17.4})
        self._cache(60, [same])
        page = _page(same, _fam("beta", 5_000_000, {"4b": 0, "12b": 0}))
        calls: list[tuple[str, str]] = []

        def manifest(name: str, tag: str) -> float:
            calls.append((name, tag))
            return {"4b": 3.4, "12b": 8.0}[tag]

        with mock.patch.object(library, "_get", return_value=page), mock.patch.object(library, "_manifest_gb", side_effect=manifest):
            rows, src = library.models(24, fresh=True)
        self.assertEqual(src["source"], "live")
        self.assertEqual(sorted(calls), [("beta", "12b"), ("beta", "4b")])  # alpha는 갱신일이 같아 다시 안 잰다
        self.assertEqual({r["id"]: r["size_gb"] for r in rows}["alpha:27b"], 17.4)
        self.assertEqual(library.read_cache()["families"][0]["name"], "alpha")

    def test_refresh_remeasures_when_family_updated(self) -> None:
        old = _fam("alpha", 9_000_000, {"2b": 2.0, "9b": 6.6, "27b": 17.4}, days_ago=30)
        self._cache(60, [old])
        page = _page(_fam("alpha", 9_000_000, {"2b": 0, "9b": 0, "27b": 0}, days_ago=1), _fam("beta", 1, {"4b": 0}))
        with mock.patch.object(library, "_get", return_value=page), mock.patch.object(library, "_manifest_gb", return_value=3.0) as m:
            library.models(24, fresh=True)
        self.assertEqual(m.call_count, 4)

    def test_slow_size_is_skipped_not_waited_for(self) -> None:
        fams = [_fam("alpha", 9, {"2b": 0, "9b": 0, "27b": 0}), _fam("beta", 5, {"4b": 0, "12b": 0})]
        real = {"2b": 2.0, "9b": 6.6, "27b": 17.4, "4b": 3.4, "12b": 8.0}

        def manifest(name: str, tag: str) -> float:
            if tag == "27b":
                time.sleep(1.0)
            return real[tag]

        with mock.patch.object(library, "_get", return_value=_page(*fams)), mock.patch.object(library, "_manifest_gb", side_effect=manifest), mock.patch.object(library, "MANIFEST_BUDGET_S", 0.2):
            start = time.monotonic()
            rows, src = library.models(24, fresh=True)
            took = time.monotonic() - start
        self.assertLess(took, 0.8)
        self.assertEqual(src["source"], "live")
        self.assertNotIn("alpha:27b", {r["id"] for r in rows})  # 늦은 크기는 이번에만 빠진다
        self.assertEqual(len(rows), 4)

    def test_thin_page_falls_back_without_overwriting_cache(self) -> None:
        self._cache(120, FOUR)
        before = library.CACHE_PATH.read_text()
        with mock.patch.object(library, "_get", return_value=_page(_fam("only", 1, {"4b": 0}))), mock.patch.object(library, "_manifest_gb", return_value=3.0):
            _, src = library.models(24, fresh=True)
        self.assertEqual(src["source"], "cache")
        self.assertIn("페이지 형식", src["error"])
        self.assertEqual(library.CACHE_PATH.read_text(), before)

    def test_cached_size(self) -> None:
        self._cache(5, FOUR)
        self.assertEqual(library.cached_size_gb("beta:12b"), 8.0)
        self.assertIsNone(library.cached_size_gb("gamma:1b"))


if __name__ == "__main__":
    unittest.main()
