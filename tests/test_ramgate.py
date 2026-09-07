"""ramgate 순수 로직 테스트(시스템 호출·Ollama는 모두 대체). 실행: python3 -m unittest tests.test_ramgate"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk import ramgate  # noqa: E402

GIB = ramgate.GIB
LLAMA_SHAPE = {"n_layer": 28, "n_head_kv": 8, "head_dim": 128, "n_head": 24}
R1_SHAPE = {"n_layer": 28, "n_head_kv": 4, "head_dim": 128, "n_head": 28}


class FormulaTests(unittest.TestCase):
    def test_shape_from_show_meta(self) -> None:
        meta = {
            "general.architecture": "llama",
            "llama.block_count": 28,
            "llama.attention.head_count": 24,
            "llama.attention.head_count_kv": 8,
            "llama.attention.key_length": 128,
            "llama.embedding_length": 3072,
        }
        self.assertEqual(ramgate._shape_from_meta(meta), LLAMA_SHAPE)
        del meta["llama.attention.key_length"]
        self.assertEqual(ramgate._shape_from_meta(meta)["head_dim"], 128)  # embedding / head_count
        self.assertIsNone(ramgate._shape_from_meta({}))

    def test_need_matches_research_numbers(self) -> None:
        with mock.patch.object(ramgate, "_shape", return_value=LLAMA_SHAPE), mock.patch.object(ramgate, "_has_projector", return_value=False):
            need = ramgate._need_bytes("llama3.2:3b", 2_019_393_189, 4096) / GIB
        self.assertAlmostEqual(need, 3.21, delta=0.05)
        with mock.patch.object(ramgate, "_shape", return_value=R1_SHAPE), mock.patch.object(ramgate, "_has_projector", return_value=False):
            need = ramgate._need_bytes("deepseek-r1:7b", 4_683_075_440, 4096) / GIB
        self.assertAlmostEqual(need, 5.48, delta=0.05)

    def test_kv_fallback_when_shape_unknown(self) -> None:
        with mock.patch.object(ramgate, "_shape", return_value=None), mock.patch.object(ramgate, "_has_projector", return_value=False):
            small = ramgate._need_bytes("x", 2 * GIB, 4096)
            large = ramgate._need_bytes("x", 2 * GIB, 8192)
        self.assertEqual(large - small, ramgate.KV_FALLBACK_PER_TOKEN * 4096)

    def test_unknown_model_defaults_to_4(self) -> None:
        with mock.patch.object(ramgate, "_weight_bytes", return_value=0):
            self.assertEqual(ramgate.model_need_gb("nope:1b"), ramgate.UNKNOWN_NEED_GB)

    def test_parse_size_and_swap(self) -> None:
        self.assertEqual(ramgate._parse_size("1024.00M"), GIB)
        self.assertEqual(ramgate._parse_size("2G"), 2 * GIB)
        self.assertEqual(ramgate._parse_size("bad"), 0)
        with mock.patch.object(ramgate, "_sysctl", return_value="total = 20480.00M  used = 10240.00M  free = 10240.00M  (encrypted)"):
            used, total = ramgate._swap_usage()
        self.assertEqual((used, total), (10 * GIB, 20 * GIB))

    def test_model_name_from_manifest_path(self) -> None:
        base = ramgate.MANIFESTS
        self.assertEqual(ramgate._model_from_manifest_path(base / "registry.ollama.ai" / "library" / "llama3.2" / "3b"), "llama3.2:3b")
        self.assertEqual(ramgate._model_from_manifest_path(base / "hf.co" / "someone" / "model" / "q4"), "someone/model:q4")


def _snapshot(avail: float, pressure: int = 1, loaded: list[str] | None = None) -> dict:
    return {
        "total_gb": 24.0,
        "free_gb": avail + 1.0,
        "avail_gb": avail,
        "pressure_pct": 50,
        "pressure_level": pressure,
        "swap_used_gb": 0.0,
        "swap_total_gb": 0.0,
        "swap_warn": False,
        "loaded": [{"model": m, "size_gb": 1.0} for m in (loaded or [])],
        "at": "2026-09-04T20:00:00+09:00",
    }


NEEDS = {"big:35b": 26.0, "deepseek-r1:7b": 5.5, "llama3.2:3b": 3.2, "tiny:1b": 1.5}


class DecideTests(unittest.TestCase):
    def setUp(self) -> None:
        patches = [
            mock.patch.object(ramgate, "model_need_gb", side_effect=lambda m, n=4096: NEEDS.get(m, 4.0)),
            mock.patch.object(ramgate, "installed_models", return_value=["deepseek-r1:7b", "llama3.2:3b", "tiny:1b"]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_run_when_fits(self) -> None:
        with mock.patch.object(ramgate, "snapshot", return_value=_snapshot(avail=10.0)):
            verdict = ramgate.decide("deepseek-r1:7b", "skip")
        self.assertEqual(verdict["action"], "run")
        self.assertEqual(verdict["model"], "deepseek-r1:7b")

    def test_loaded_model_runs_even_when_tight(self) -> None:
        with mock.patch.object(ramgate, "snapshot", return_value=_snapshot(avail=0.5, pressure=4, loaded=["deepseek-r1:7b"])):
            self.assertEqual(ramgate.decide("deepseek-r1:7b", "skip")["action"], "run")

    def test_policies_when_short(self) -> None:
        with mock.patch.object(ramgate, "snapshot", return_value=_snapshot(avail=4.5)):
            self.assertEqual(ramgate.decide("deepseek-r1:7b", "skip")["action"], "skip")
            self.assertEqual(ramgate.decide("deepseek-r1:7b", "defer")["action"], "wait")
            verdict = ramgate.decide("deepseek-r1:7b", "downgrade")
        self.assertEqual(verdict["action"], "downgrade")
        self.assertEqual(verdict["model"], "llama3.2:3b")  # 들어가는 가장 큰 설치 모델
        self.assertEqual(verdict["need_gb"], 3.2)

    def test_downgrade_prefers_fallback_when_it_fits(self) -> None:
        with mock.patch.object(ramgate, "snapshot", return_value=_snapshot(avail=4.5)):
            verdict = ramgate.decide("deepseek-r1:7b", "downgrade", fallback_model="tiny:1b")
        self.assertEqual((verdict["action"], verdict["model"]), ("downgrade", "tiny:1b"))

    def test_downgrade_without_candidate_skips(self) -> None:
        with mock.patch.object(ramgate, "snapshot", return_value=_snapshot(avail=1.0)):
            verdict = ramgate.decide("big:35b", "downgrade")
        self.assertEqual(verdict["action"], "skip")
        self.assertIn("램 부족", verdict["reason"])

    def test_pressure_warns_but_runs_when_memory_fits(self) -> None:
        with mock.patch.object(ramgate, "snapshot", return_value=_snapshot(avail=20.0, pressure=2)):
            for policy in ("defer", "skip", "downgrade"):
                verdict = ramgate.decide("deepseek-r1:7b", policy)
                self.assertEqual(verdict["action"], "run")
                self.assertIn("메모리 압력 높음", verdict["reason"])

    def test_gate_factor_and_margin(self) -> None:
        self.assertTrue(ramgate._fits(3.2, 4.5, 0.5))  # 3.52 <= 4.0
        self.assertFalse(ramgate._fits(3.2, 4.0, 0.5))  # 3.52 > 3.5


if __name__ == "__main__":
    unittest.main()
