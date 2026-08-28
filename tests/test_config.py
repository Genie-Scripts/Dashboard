"""
day-type 基盤関数の回帰テスト（標準ライブラリ unittest・追加依存なし）。

対象: config.py の暦調整用 基盤関数（暦補正と学習ループ改修プラン P0）
  - day_type                 : "biz" | "hol_wd" | "sat" | "sun"
  - nonop_run_len             : d を含む連続非営業日ブロックの長さ
  - is_long_holiday_eve       : 連休前日フラグ
  - operational_days_between  : 両端含む営業日数

祝日に依存する日付は jpholiday で実際に祝日であることを assert してから使う
（ハードコード誤り防止）。

実行: リポジトリルートで
    python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

import jpholiday
import pandas as pd

# リポジトリルートを import パスに追加（generate_html.py と同方式）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.config import (  # noqa: E402
    day_type,
    nonop_run_len,
    is_long_holiday_eve,
    operational_days_between,
    is_operational_day,
)


def _old_is_biz(d: pd.Timestamp) -> bool:
    """metrics.py の旧 ga_rolling_biz_avg._is_biz を独立に再実装したもの
    （削除前の実装と同一ロジック）。config.is_operational_day との等価性検証用。"""
    if d.weekday() >= 5:
        return False
    if jpholiday.is_holiday(d.date()):
        return False
    if (d.month == 12 and d.day >= 29) or (d.month == 1 and d.day <= 3):
        return False
    return True


def _ref_nonop_run_len(d: pd.Timestamp) -> int:
    """_old_is_biz を基準に独立算出した非営業run長（nonop_run_len の期待値算出用）。"""
    if _old_is_biz(d):
        return 0
    start = d
    while not _old_is_biz(start - pd.Timedelta(days=1)):
        start -= pd.Timedelta(days=1)
    end = d
    while not _old_is_biz(end + pd.Timedelta(days=1)):
        end += pd.Timedelta(days=1)
    return (end - start).days + 1


class TestDayType(unittest.TestCase):
    def test_plain_weekday_is_biz(self):
        d = pd.Timestamp("2026-02-10")   # 火曜・祝日なし
        self.assertFalse(jpholiday.is_holiday(d.date()))
        self.assertEqual(day_type(d), "biz")

    def test_plain_saturday_is_sat(self):
        d = pd.Timestamp("2026-03-07")   # 土曜・祝日なし
        self.assertFalse(jpholiday.is_holiday(d.date()))
        self.assertEqual(day_type(d), "sat")

    def test_plain_sunday_is_sun(self):
        d = pd.Timestamp("2026-03-08")   # 日曜・祝日なし
        self.assertFalse(jpholiday.is_holiday(d.date()))
        self.assertEqual(day_type(d), "sun")

    def test_happy_monday_is_hol_wd(self):
        d = pd.Timestamp("2026-01-12")   # 成人の日（月曜）
        self.assertTrue(jpholiday.is_holiday(d.date()))
        self.assertEqual(d.weekday(), 0)
        self.assertEqual(day_type(d), "hol_wd")

    def test_new_year_weekdays_are_hol_wd(self):
        # 2025-12-29(月)〜2026-01-02(金) は平日だが年末年始特例で非営業 → hol_wd
        weekdays = pd.date_range("2025-12-29", "2026-01-02")
        for d in weekdays:
            self.assertLess(d.weekday(), 5, f"{d.date()} は前提が崩れている（平日でない）")
            self.assertEqual(day_type(d), "hol_wd", f"{d.date()}")
        # 年末年始レンジ末尾の 1/3 は土曜 → 祝日/特例より土日判定が優先されるので "sat"
        d_sat = pd.Timestamp("2026-01-03")
        self.assertEqual(d_sat.weekday(), 5)
        self.assertEqual(day_type(d_sat), "sat")

    def test_obon_weekdays_are_biz_not_holiday(self):
        # お盆は祝日でないので補正不要（重要仕様: 一律「連休扱い」にしない）
        for s in ("2026-08-13", "2026-08-14"):
            d = pd.Timestamp(s)
            self.assertFalse(jpholiday.is_holiday(d.date()), f"{s} が祝日化していないか確認")
            self.assertLess(d.weekday(), 5)
            self.assertEqual(day_type(d), "biz")

    def test_saturday_holiday_is_sat_not_hol_wd(self):
        d = pd.Timestamp("2024-11-23")   # 勤労感謝の日（土曜）
        self.assertTrue(jpholiday.is_holiday(d.date()))
        self.assertEqual(d.weekday(), 5)
        self.assertEqual(day_type(d), "sat")


class TestNonopRunLen(unittest.TestCase):
    def test_business_day_is_zero(self):
        self.assertEqual(nonop_run_len(pd.Timestamp("2026-02-10")), 0)

    def test_isolated_weekend_is_two_on_both_days(self):
        sat = pd.Timestamp("2026-03-07")
        sun = pd.Timestamp("2026-03-08")
        # 前後（金曜・月曜）が営業日であることを確認し「孤立」であることを保証
        self.assertTrue(is_operational_day(pd.Timestamp("2026-03-06")))
        self.assertTrue(is_operational_day(pd.Timestamp("2026-03-09")))
        self.assertEqual(nonop_run_len(sat), 2)
        self.assertEqual(nonop_run_len(sun), 2)

    def test_happy_monday_weekend_block_is_three_on_all_days(self):
        sat, sun, mon = (pd.Timestamp("2026-01-10"), pd.Timestamp("2026-01-11"),
                         pd.Timestamp("2026-01-12"))
        self.assertTrue(jpholiday.is_holiday(mon.date()))
        # 前後（金曜・火曜）が営業日であることを確認
        self.assertTrue(is_operational_day(pd.Timestamp("2026-01-09")))
        self.assertTrue(is_operational_day(pd.Timestamp("2026-01-13")))
        for d in (sat, sun, mon):
            self.assertEqual(nonop_run_len(d), 3, f"{d.date()}")

    def test_new_year_block_length_matches_independent_reference(self):
        # 2025年末〜2026年始の非営業ブロック（土日＋年末年始特例が連結）。
        # 期待値は独立再実装(_ref_nonop_run_len)から算出し、ハードコードしない。
        for s in ("2025-12-27", "2025-12-31", "2026-01-01", "2026-01-04"):
            d = pd.Timestamp(s)
            self.assertEqual(nonop_run_len(d), _ref_nonop_run_len(d), f"{s}")
        # ブロックが実際に「土日祝の単独ブロックより長い」ことも確認（年末年始特例の効果）
        self.assertGreater(nonop_run_len(pd.Timestamp("2025-12-31")), 2)


class TestIsLongHolidayEve(unittest.TestCase):
    def test_friday_before_three_day_weekend_is_true(self):
        fri = pd.Timestamp("2026-01-09")   # 翌週末は 土・日・月(祝) = run 3
        self.assertTrue(jpholiday.is_holiday(pd.Timestamp("2026-01-12").date()))
        self.assertTrue(is_operational_day(fri))
        self.assertEqual(nonop_run_len(pd.Timestamp("2026-01-10")), 3)
        self.assertTrue(is_long_holiday_eve(fri, min_run=3))

    def test_ordinary_friday_is_false(self):
        fri = pd.Timestamp("2026-03-06")   # 翌週末は孤立土日 = run 2
        self.assertTrue(is_operational_day(fri))
        self.assertEqual(nonop_run_len(pd.Timestamp("2026-03-07")), 2)
        self.assertFalse(is_long_holiday_eve(fri, min_run=3))

    def test_min_run_boundary(self):
        fri = pd.Timestamp("2026-03-06")   # 翌run=2
        self.assertTrue(is_long_holiday_eve(fri, min_run=2))
        self.assertFalse(is_long_holiday_eve(fri, min_run=3))

    def test_non_business_day_is_false(self):
        # 営業日でない日は eve 判定の対象外（定義上 False）
        self.assertFalse(is_long_holiday_eve(pd.Timestamp("2026-01-10"), min_run=1))


class TestOperationalDaysBetween(unittest.TestCase):
    def test_ordinary_week_is_five(self):
        self.assertEqual(
            operational_days_between("2026-03-02", "2026-03-06"), 5)  # 月〜金・祝日なし

    def test_happy_monday_week_is_four(self):
        self.assertTrue(jpholiday.is_holiday(pd.Timestamp("2026-01-12").date()))
        self.assertEqual(
            operational_days_between("2026-01-12", "2026-01-16"), 4)  # 月(祝)〜金

    def test_new_year_crossing_range(self):
        # 2025-12-24(水)〜2026-01-07(水): 12/24-26 と 1/5-7 の計6営業日
        self.assertEqual(
            operational_days_between("2025-12-24", "2026-01-07"), 6)


class TestEquivalenceWithOldIsBiz(unittest.TestCase):
    """is_operational_day が、削除した metrics.py 内ローカル関数 _is_biz と
    2024-2026 の全日で一致すること（挙動不変の証跡）。"""

    def test_all_days_2024_to_2026_match_old_is_biz(self):
        mismatches = []
        for d in pd.date_range("2024-01-01", "2026-12-31", freq="D"):
            expected = _old_is_biz(d)
            actual = is_operational_day(d)
            if expected != actual:
                mismatches.append((d.date(), expected, actual))
        self.assertEqual(mismatches, [], f"不一致: {mismatches[:10]}")


if __name__ == "__main__":
    unittest.main()
