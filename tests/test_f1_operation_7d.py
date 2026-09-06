"""F1是正: 全麻「直近7日合計」の真の直近7暦日化・前年同期・先週の確定の回帰テスト。

旧: operation_week_total は月〜基準日の部分週（火曜ビルドなら2日分）を「直近7日合計」
として表示し、前年同期も365日前の同区間（日数の異なる窓）と比較していた（F1）。

対象（build_kpi_summary の operation_7d_* / operation_last_week_* を裏で支える新設ヘルパー）:
  - rolling7_surgery（既存関数の再利用）: 真の直近7暦日(date-6..date)合計
  - _fmt_range / _fmt_range_prevyear    : 期間ラベル整形（★F1で新設）
  - _last_complete_week                 : 基準日以前で最新の完全週（月〜日）（★F1で新設）

実行: リポジトリルートで
    OMLX_BASE_URL=http://127.0.0.1:9 .venv/bin/python -m pytest tests/test_f1_operation_7d.py -q
"""
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.metrics import (  # noqa: E402
    rolling7_surgery, _fmt_range, _fmt_range_prevyear, _last_complete_week,
    PREVYEAR_OFFSET_DAYS,
)


def _surg_df(dates_with_count):
    """[(date, count), ...] から全麻手術の最小フィクスチャを作る（1行=1件）。"""
    rows = []
    for d, n in dates_with_count:
        for _ in range(n):
            rows.append({"手術実施日": d, "全麻": True, "科_表示": True,
                        "実施診療科": "外科", "術数対象": True})
    return pd.DataFrame(rows, columns=["手術実施日", "全麻", "科_表示", "実施診療科", "術数対象"])


class TestTrue7DayWindow(unittest.TestCase):
    """火曜基準日でも直近7暦日(date-6..date)ぶんが合計されること
    （旧 operation_week_total は月〜火の2日分のみだった）。"""

    def test_tuesday_base_date_sums_full_7_days(self):
        base = pd.Timestamp("2026-09-08")   # 火曜
        self.assertEqual(base.weekday(), 1)
        window = pd.date_range(base - pd.Timedelta(days=6), base, freq="D")
        self.assertEqual(len(window), 7)
        surg = _surg_df([(d, 1) for d in window])
        r7 = rolling7_surgery(surg, base)
        self.assertEqual(r7["total"], 7)
        self.assertEqual(r7["start"], base - pd.Timedelta(days=6))


class TestPrevYearWindowMatchesWeekday(unittest.TestCase):
    """364日前にずらした直近7日窓は、曜日を揃えたまま7日ぶんの合計になること
    （365日前だと日数の異なる窓同士の比較になっていた＝F1本体のバグ）。"""

    def test_prev_window_is_7_days_and_weekday_aligned(self):
        base = pd.Timestamp("2026-09-08")
        prev_date = base - pd.Timedelta(days=PREVYEAR_OFFSET_DAYS)
        self.assertEqual(prev_date.weekday(), base.weekday())   # 364=52週で曜日一致
        prev_window = pd.date_range(prev_date - pd.Timedelta(days=6), prev_date, freq="D")
        self.assertEqual(len(prev_window), 7)
        self.assertEqual(prev_window[0].weekday(), (base - pd.Timedelta(days=6)).weekday())
        surg = _surg_df([(d, 2) for d in prev_window])
        r7_prev = rolling7_surgery(surg, prev_date)
        self.assertEqual(r7_prev["total"], 14)
        self.assertEqual(r7_prev["start"], prev_window[0])


class TestFmtRangeHelpers(unittest.TestCase):
    def test_fmt_range_same_year(self):
        self.assertEqual(
            _fmt_range(pd.Timestamp("2026-08-28"), pd.Timestamp("2026-09-03")), "8/28〜9/3")

    def test_fmt_range_prevyear_has_leading_year_on_start_only(self):
        self.assertEqual(
            _fmt_range_prevyear(pd.Timestamp("2025-08-29"), pd.Timestamp("2025-09-04")),
            "2025/8/29〜9/4")


class TestLastCompleteWeek(unittest.TestCase):
    """基準日が日曜ならその週。それ以外の平日は直前の月〜日になること。"""

    def test_sunday_base_date_uses_current_week(self):
        base = pd.Timestamp("2026-08-30")   # 日曜
        self.assertEqual(base.weekday(), 6)
        monday, sunday = _last_complete_week(base)
        self.assertEqual(monday, pd.Timestamp("2026-08-24"))
        self.assertEqual(sunday, base)

    def test_thursday_base_date_uses_prior_week(self):
        base = pd.Timestamp("2026-09-03")   # 木曜（通常週ビルド基準日と共通）
        self.assertEqual(base.weekday(), 3)
        monday, sunday = _last_complete_week(base)
        self.assertEqual(monday, pd.Timestamp("2026-08-24"))
        self.assertEqual(sunday, pd.Timestamp("2026-08-30"))


if __name__ == "__main__":
    unittest.main()
