"""calendar_preview.py（P4 暦プレビュー）の回帰テスト（標準ライブラリ unittest・追加依存なし）。

対象:
  - build_week_preview   : 来週(翌週月〜日)の営業日数・連休情報
  - build_month_preview  : 来月の営業日数が標準(20日)からどれだけ乖離しているか
  - build_early_warning  : 2〜4週間後に迫る長連休(run_len>=4)の早期警戒
  - build_calendar_preview: 3層まとめ（全てNoneならNone）

祝日判定はjpholiday実カレンダー依存のため、想定と実際がズレた日付は実カレンダーに
合わせて調整している（config側は不可侵のため調整せず、テスト側の日付選定で対応）:
  - ⑤ 年末年始で来週・来月とも発火するケースは、2026-12-21だと翌月(2027-01)の
    営業日数が19日(標準20日との差=1)で来月層が発火しないため、週・月が実際に
    同時発火する 2027-01-01（元日）を使用。
  - ⑥ お盆で発火しないケースは、2026-08-07だと翌週(08-10〜08-16)に山の日(08-11)
    がかかり営業日4日で来週層が発火してしまうため、その影響が及ばない
    2026-08-14（お盆週内・翌週は平常週）を使用。

実行: リポジトリルートで
    python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

import pandas as pd

# リポジトリルートを import パスに追加（generate_html.py と同方式）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.calendar_preview import (  # noqa: E402
    build_week_preview,
    build_month_preview,
    build_early_warning,
    build_calendar_preview,
    _nearest_profile,
    _diff_vs_baseline,
)


class OrdinaryWeekTest(unittest.TestCase):
    """① 通常週（2026-07-03・翌週は平常5日、翌月も標準20日、直近に長連休もなし）"""

    def test_all_three_layers_none(self):
        base = pd.Timestamp("2026-07-03")
        self.assertIsNone(build_week_preview(base))
        self.assertIsNone(build_month_preview(base))
        self.assertIsNone(build_early_warning(base))
        self.assertIsNone(build_calendar_preview(base))


class HappyMondayWeekTest(unittest.TestCase):
    """② ハッピーマンデー週（2026-07-13基準・翌週は海の日で営業日4日）で来週層のみ発火"""

    def test_week_only_fires(self):
        base = pd.Timestamp("2026-07-13")
        week = build_week_preview(base)
        self.assertIsNotNone(week)
        self.assertEqual(week["biz_days"], 4)
        self.assertIsNone(build_month_preview(base))
        self.assertIsNone(build_early_warning(base))


class SilverWeekReportedByNextWeekTest(unittest.TestCase):
    """③ 2026-09-14(月)週の後半基準日(木曜)で、来週層が9/19-23(run=5)を報告する"""

    def test_week_preview_reports_run5_from_sep19(self):
        base = pd.Timestamp("2026-09-17")   # 2026-09-14週の木曜
        week = build_week_preview(base)
        self.assertIsNotNone(week)
        self.assertEqual(week["run_len"], 5)
        self.assertIn("9月19日", week["text"])
        self.assertIn("5日間", week["text"])


class EarlyWarningWindowBoundaryTest(unittest.TestCase):
    """④ 早期警戒(14〜28日窓)の境界: 08-29は9/19(あと3週)を捉え、08-10は捉えない"""

    def test_fires_three_weeks_before(self):
        early = build_early_warning(pd.Timestamp("2026-08-29"))
        self.assertIsNotNone(early)
        self.assertEqual(early["start"], "2026-09-19")
        self.assertEqual(early["run_len"], 5)
        self.assertEqual(early["weeks_ahead"], 3)
        self.assertEqual(early["chip"], ["連休", "9/19-23"])

    def test_does_not_fire_outside_window(self):
        self.assertIsNone(build_early_warning(pd.Timestamp("2026-08-10")))


class NewYearBothWeekAndMonthTest(unittest.TestCase):
    """⑤ 年末年始（2027-01-01・元日）で来週層・来月層とも発火する

    （2026-12-21は来月=2027-01の営業日数19日=標準との差1で来月層が発火しないため、
    実際に両方発火する元日基準へ調整。理由は本ファイル冒頭の注記を参照）。
    """

    def test_week_and_month_both_fire(self):
        base = pd.Timestamp("2027-01-01")
        self.assertIsNotNone(build_week_preview(base))
        self.assertIsNotNone(build_month_preview(base))


class ObonQuietWeekTest(unittest.TestCase):
    """⑥ お盆週（2026-08-14）は3層とも発火しない

    （2026-08-07だと翌週に山の日がかかり来週層が発火するため、その影響を受けない
    お盆週内の日付へ調整。理由は本ファイル冒頭の注記を参照）。
    """

    def test_all_three_layers_none(self):
        base = pd.Timestamp("2026-08-14")
        self.assertIsNone(build_calendar_preview(base))


class MonthDiffBoundaryTest(unittest.TestCase):
    """⑦ 来月層の境界: |翌月営業日数-20|==1で非発火・==2で発火"""

    def test_diff_one_does_not_fire(self):
        # 翌月=2026-09、営業日19日（標準20との差=1）
        self.assertIsNone(build_month_preview(pd.Timestamp("2026-08-07")))

    def test_diff_two_fires(self):
        # 翌月=2026-02、営業日18日（標準20との差=2）
        month = build_month_preview(pd.Timestamp("2026-01-05"))
        self.assertIsNotNone(month)
        self.assertEqual(month["biz_days"], 18)


class RunLenRoundingTest(unittest.TestCase):
    """⑧ run_len=7のような未定義キーでも下位キー(4)へ丸められ例外にならない"""

    def test_run7_rounds_down_to_bucket4_without_exception(self):
        self.assertEqual(_nearest_profile(7), _nearest_profile(4))
        self.assertEqual(_diff_vs_baseline(7), _diff_vs_baseline(4))

    def test_run10_rounds_down_to_bucket9_without_exception(self):
        self.assertEqual(_nearest_profile(10), _nearest_profile(9))


class JsonSerializableTest(unittest.TestCase):
    """⑨ build_calendar_preview の返り値は json.dumps 可能"""

    def test_json_dumps_succeeds(self):
        import json
        cp = build_calendar_preview(pd.Timestamp("2026-09-17"))
        self.assertIsNotNone(cp)
        json.dumps(cp, ensure_ascii=False)   # 例外が出ないことのみ確認


class WeekPreviewMachineReadableKeysTest(unittest.TestCase):
    """⑩ build_week_preview は発火時、機械可読キー3つ(biz_days/run_len/is_eve)を必ず含む"""

    def test_keys_present_on_biz_days_only_fire(self):
        week = build_week_preview(pd.Timestamp("2026-08-07"))
        self.assertIsNotNone(week)
        for key in ("biz_days", "run_len", "is_eve"):
            self.assertIn(key, week)

    def test_keys_present_on_run_holiday_fire(self):
        week = build_week_preview(pd.Timestamp("2026-07-13"))
        self.assertIsNotNone(week)
        for key in ("biz_days", "run_len", "is_eve"):
            self.assertIn(key, week)


if __name__ == "__main__":
    unittest.main()
