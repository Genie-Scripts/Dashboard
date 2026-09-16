"""
day-type 基盤関数（新設2述語）の回帰テスト（標準ライブラリ unittest・追加依存なし）。

対象: config.py の暦調整用 基盤関数（P2 判定の統計是正の下地）
  - is_isolated_weekend         : nonop_run_len(d) == 2 の土日
  - is_holiday_adjacent_weekday : 営業日だが前日/翌日が長さ3以上の非営業runに属する

祝日に依存する日付は jpholiday で実際に祝日/非祝日であることを assert してから使う
（ハードコード誤り防止）。data/ の実データは一切読まない（合成・カレンダー計算のみ）。

実行: リポジトリルートで
    .venv/bin/python -m pytest -q tests/test_daytype_predicates.py
"""
import sys
import unittest
from pathlib import Path

import jpholiday
import pandas as pd

# リポジトリルートを import パスに追加（generate_html.py と同方式）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.config import (  # noqa: E402
    is_isolated_weekend,
    is_holiday_adjacent_weekday,
    is_operational_day,
    nonop_run_len,
)


class TestIsIsolatedWeekend(unittest.TestCase):
    def test_isolated_weekend_is_true_on_both_days(self):
        # 2026-09-05(土)/06(日) は前後(09-04金・09-07月)が営業日の孤立土日。
        sat, sun = pd.Timestamp("2026-09-05"), pd.Timestamp("2026-09-06")
        self.assertFalse(jpholiday.is_holiday(sat.date()))
        self.assertFalse(jpholiday.is_holiday(sun.date()))
        self.assertTrue(is_operational_day(sat - pd.Timedelta(days=1)))
        self.assertTrue(is_operational_day(sun + pd.Timedelta(days=1)))
        self.assertEqual(nonop_run_len(sat), 2)
        self.assertEqual(nonop_run_len(sun), 2)
        self.assertTrue(is_isolated_weekend(sat))
        self.assertTrue(is_isolated_weekend(sun))

    def test_weekday_is_false(self):
        self.assertFalse(is_isolated_weekend(pd.Timestamp("2026-09-04")))

    def test_silver_week_five_day_run_is_not_isolated(self):
        # 2026-09-19(土)〜23(水・秋分の日) は run=5 の5連休（敬老の日・国民の休日・秋分の日）。
        sat, sun = pd.Timestamp("2026-09-19"), pd.Timestamp("2026-09-20")
        self.assertFalse(jpholiday.is_holiday(sat.date()))
        self.assertFalse(jpholiday.is_holiday(sun.date()))
        self.assertTrue(jpholiday.is_holiday(pd.Timestamp("2026-09-21").date()))
        self.assertTrue(jpholiday.is_holiday(pd.Timestamp("2026-09-22").date()))
        self.assertTrue(jpholiday.is_holiday(pd.Timestamp("2026-09-23").date()))
        self.assertEqual(nonop_run_len(sat), 5)
        self.assertEqual(nonop_run_len(sun), 5)
        self.assertFalse(is_isolated_weekend(sat))
        self.assertFalse(is_isolated_weekend(sun))

    def test_happy_monday_weekend_block_is_not_isolated(self):
        # 2026-01-10(土)/11(日)/12(月・成人の日) は run=3。
        sat, sun = pd.Timestamp("2026-01-10"), pd.Timestamp("2026-01-11")
        self.assertTrue(jpholiday.is_holiday(pd.Timestamp("2026-01-12").date()))
        self.assertEqual(nonop_run_len(sat), 3)
        self.assertEqual(nonop_run_len(sun), 3)
        self.assertFalse(is_isolated_weekend(sat))
        self.assertFalse(is_isolated_weekend(sun))

    def test_golden_week_weekend_block_is_not_isolated(self):
        # 2026-05-02(土)〜06(水・振替休日) は run=5 のGW後半。
        sat, sun = pd.Timestamp("2026-05-02"), pd.Timestamp("2026-05-03")
        self.assertEqual(nonop_run_len(sat), 5)
        self.assertEqual(nonop_run_len(sun), 5)
        self.assertFalse(is_isolated_weekend(sat))
        self.assertFalse(is_isolated_weekend(sun))

    def test_new_year_weekend_block_is_not_isolated(self):
        # 2025-12-27(土)〜2026-01-04(日) は年末年始特例日込みで run=9。
        sat = pd.Timestamp("2025-12-27")
        self.assertEqual(nonop_run_len(sat), 9)
        self.assertFalse(is_isolated_weekend(sat))

    def test_obon_saturday_is_isolated_weekend(self):
        # 2026-08-15(土)/16(日) はお盆だが祝日でなく、run=2の孤立土日。
        sat, sun = pd.Timestamp("2026-08-15"), pd.Timestamp("2026-08-16")
        self.assertFalse(jpholiday.is_holiday(sat.date()))
        self.assertFalse(jpholiday.is_holiday(sun.date()))
        self.assertEqual(nonop_run_len(sat), 2)
        self.assertTrue(is_isolated_weekend(sat))
        self.assertTrue(is_isolated_weekend(sun))


class TestIsHolidayAdjacentWeekday(unittest.TestCase):
    def test_business_day_before_silver_week_is_true(self):
        # 2026-09-18(金) の翌日から run=5 の5連休が始まる。
        fri = pd.Timestamp("2026-09-18")
        self.assertTrue(is_operational_day(fri))
        self.assertEqual(nonop_run_len(fri + pd.Timedelta(days=1)), 5)
        self.assertTrue(is_holiday_adjacent_weekday(fri))

    def test_business_day_after_silver_week_is_true(self):
        # 2026-09-24(木) の前日まで run=5 の5連休。
        thu = pd.Timestamp("2026-09-24")
        self.assertTrue(is_operational_day(thu))
        self.assertEqual(nonop_run_len(thu - pd.Timedelta(days=1)), 5)
        self.assertTrue(is_holiday_adjacent_weekday(thu))

    def test_business_days_around_golden_week_are_true(self):
        fri, wed = pd.Timestamp("2026-05-01"), pd.Timestamp("2026-05-07")
        self.assertTrue(is_holiday_adjacent_weekday(fri))
        self.assertTrue(is_holiday_adjacent_weekday(wed))

    def test_business_days_around_new_year_are_true(self):
        fri, mon = pd.Timestamp("2025-12-26"), pd.Timestamp("2026-01-05")
        self.assertTrue(is_holiday_adjacent_weekday(fri))
        self.assertTrue(is_holiday_adjacent_weekday(mon))

    def test_business_days_around_happy_monday_are_true(self):
        # 2026-01-09(金)/13(火) は run=3 のハッピーマンデー前後。
        fri, tue = pd.Timestamp("2026-01-09"), pd.Timestamp("2026-01-13")
        self.assertTrue(is_holiday_adjacent_weekday(fri))
        self.assertTrue(is_holiday_adjacent_weekday(tue))

    def test_business_days_around_isolated_weekend_are_false(self):
        # 孤立土日(run=2)は min_run=3 未満のため、前後平日は非該当。
        fri, mon = pd.Timestamp("2026-09-04"), pd.Timestamp("2026-09-07")
        self.assertEqual(nonop_run_len(pd.Timestamp("2026-09-05")), 2)
        self.assertFalse(is_holiday_adjacent_weekday(fri))
        self.assertFalse(is_holiday_adjacent_weekday(mon))

    def test_obon_weekdays_are_true_bizdays_not_holiday_adjacent(self):
        # 2026-08-13(木)/14(金) は祝日ではない営業日。直後の孤立土日(run=2)は
        # min_run=3未満のため、14(金)は holiday_adjacent には該当しない。
        thu, fri = pd.Timestamp("2026-08-13"), pd.Timestamp("2026-08-14")
        self.assertFalse(jpholiday.is_holiday(thu.date()))
        self.assertFalse(jpholiday.is_holiday(fri.date()))
        self.assertTrue(is_operational_day(thu))
        self.assertTrue(is_operational_day(fri))
        self.assertFalse(is_holiday_adjacent_weekday(thu))
        self.assertFalse(is_holiday_adjacent_weekday(fri))

    def test_ordinary_midweek_day_is_false(self):
        self.assertFalse(is_holiday_adjacent_weekday(pd.Timestamp("2026-02-10")))

    def test_non_business_day_is_false(self):
        # 土日祝そのものは営業日でないため無条件で False。
        self.assertFalse(is_holiday_adjacent_weekday(pd.Timestamp("2026-01-12")))


if __name__ == "__main__":
    unittest.main()
