"""cronwhen 순수 함수 테스트. 실행: python3 -m unittest tests.test_cronwhen"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk import cronwhen  # noqa: E402

SEOUL = ZoneInfo("Asia/Seoul")
FRIDAY_EVENING = datetime(2026, 9, 4, 20, 30, tzinfo=SEOUL)


class ParseTests(unittest.TestCase):
    def test_valid_forms(self) -> None:
        for expr in ("30 9 * * 1-5", "*/15 * * * *", "0 0 31 * *", "0 0 * * 7", "5/15 * * * *", "0 9 * jan-mar mon,fri", "1-59/2 * * * *"):
            self.assertTrue(cronwhen.is_valid(expr), expr)

    def test_invalid_forms(self) -> None:
        for expr in ("99 * * * *", "a b c d e", "* * * * */0", "* * * *", "60 * * * *", "0 24 * * *", "5-3 * * * *", "* * * * 8", "0 0 0 * *", "", "0 0 * 13 *"):
            self.assertFalse(cronwhen.is_valid(expr), expr)
            self.assertIsNone(cronwhen.parse(expr), expr)

    def test_sunday_seven_equals_zero(self) -> None:
        self.assertEqual(cronwhen.parse("0 0 * * 7")[4], {0})
        self.assertEqual(cronwhen.parse("0 0 * * 0,7")[4], {0})

    def test_step_from_start(self) -> None:
        self.assertEqual(cronwhen.parse("5/15 * * * *")[0], {5, 20, 35, 50})


class NextRunsTests(unittest.TestCase):
    def test_weekday_morning(self) -> None:
        runs = cronwhen.next_runs("30 9 * * 1-5", FRIDAY_EVENING, 3)
        self.assertEqual([r.isoformat() for r in runs], ["2026-09-07T09:30:00+09:00", "2026-09-08T09:30:00+09:00", "2026-09-09T09:30:00+09:00"])

    def test_every_15_minutes(self) -> None:
        runs = cronwhen.next_runs("*/15 * * * *", FRIDAY_EVENING, 4)
        gaps = {(b - a).total_seconds() for a, b in zip(runs, runs[1:])}
        self.assertEqual(gaps, {900.0})
        self.assertGreater(runs[0], FRIDAY_EVENING)

    def test_31st_only_in_long_months(self) -> None:
        runs = cronwhen.next_runs("0 0 31 * *", FRIDAY_EVENING, 6)
        self.assertEqual([r.strftime("%m-%d") for r in runs], ["10-31", "12-31", "01-31", "03-31", "05-31", "07-31"])

    def test_dom_dow_or_rule(self) -> None:
        runs = cronwhen.next_runs("0 0 1,15 * 1", datetime(2026, 9, 4, tzinfo=SEOUL), 3)
        self.assertEqual([r.strftime("%m-%d") for r in runs], ["09-07", "09-14", "09-15"])

    def test_horizon_and_naive_start(self) -> None:
        runs = cronwhen.next_runs("0 * * * *", datetime(2026, 9, 4, 20, 30), horizon=datetime(2026, 9, 4, 22, 0, tzinfo=SEOUL))
        self.assertEqual([r.hour for r in runs], [21, 22])
        self.assertEqual(runs[0].tzinfo, SEOUL)

    def test_impossible_date_returns_empty(self) -> None:
        self.assertEqual(cronwhen.next_runs("0 0 30 2 *", FRIDAY_EVENING), [])
        self.assertEqual(cronwhen.next_runs("bad", FRIDAY_EVENING), [])


class DescribeTests(unittest.TestCase):
    def test_labels(self) -> None:
        cases = {
            "0 17 * * *": "매일 17:00",
            "30 9 * * 1-5": "평일 09:30",
            "*/5 * * * *": "5분마다",
            "0 * * * *": "매시 0분",
            "0 */6 * * *": "6시간마다",
            "* * * * *": "매분",
            "0 9 * * 0,6": "주말 09:00",
            "0 9 * * 1": "매주 월요일 09:00",
            "0 9 1 * *": "매월 1일 09:00",
            "30 8 25 12 *": "12월 25일 08:30",
            "15 9 1 * 1": "15 9 1 * 1",
            "bad": "bad",
        }
        for expr, label in cases.items():
            self.assertEqual(cronwhen.describe(expr), label, expr)


if __name__ == "__main__":
    unittest.main()
