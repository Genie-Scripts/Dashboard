"""
指標関数の回帰防止テスト（標準ライブラリ unittest・追加依存なし）。

対象:
  - build_prevyear_ma_series   : 昨年同期 28日暦日MA（在院・新入院・部門別の年度比較線）
  - build_prevyear_weekly_series: 昨年同期 週次合計（部門別 全麻チャート）
  - build_biz_ma30_series      : 全麻 30営業平日MA（病院全体／prev_year アライン）

実行: リポジトリルートで
    python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

# リポジトリルートを import パスに追加（generate_html.py と同方式）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.metrics import (  # noqa: E402
    build_prevyear_ma_series,
    build_prevyear_weekly_series,
    build_biz_ma30_series,
    weekend_census_retention,
    PREVYEAR_OFFSET_DAYS,
)

BASE = pd.Timestamp("2026-06-03")


def _daily(start, end, value_fn):
    idx = pd.date_range(start, end, freq="D")
    return pd.DataFrame({"日付": idx, "値": [value_fn(d, i) for i, d in enumerate(idx)]})


class TestPrevyearMaSeries(unittest.TestCase):
    def test_empty_input(self):
        empty = pd.DataFrame(columns=["日付", "値"])
        self.assertEqual(build_prevyear_ma_series(empty, BASE), {"dates": [], "values": []})
        self.assertEqual(build_prevyear_ma_series(None, BASE), {"dates": [], "values": []})

    def test_aligns_last_date_to_base(self):
        s = _daily("2024-01-01", "2026-06-03", lambda d, i: 500)
        out = build_prevyear_ma_series(s, BASE, window=28)
        self.assertTrue(out["dates"])
        # 末尾は基準日に揃う（前年データ <= base-365 を +365 でアライン）
        self.assertEqual(out["dates"][-1], "2026-06-03")

    def test_constant_series_gives_constant_ma(self):
        s = _daily("2024-01-01", "2026-06-03", lambda d, i: 42)
        out = build_prevyear_ma_series(s, BASE, window=28)
        # 定数系列なら 28日MA も一定値（min_periods=1 でも全点 42）
        self.assertTrue(all(abs(v - 42.0) < 1e-9 for v in out["values"]))

    def test_window_uses_calendar_28_days(self):
        # 直近28日だけ 100、それ以前は 0。末尾(基準日)の28日MAは 100。
        cut = BASE - pd.Timedelta(days=365)  # 前年の基準日相当
        s = _daily("2024-01-01", "2025-06-03",
                   lambda d, i: 100 if d > cut - pd.Timedelta(days=28) else 0)
        out = build_prevyear_ma_series(s, BASE, window=28)
        self.assertAlmostEqual(out["values"][-1], 100.0, places=6)

    def test_explicit_offset_param(self):
        s = _daily("2024-01-01", "2025-06-03", lambda d, i: 10)
        out = build_prevyear_ma_series(s, BASE, window=28, offset_days=365)
        # 入力最終日 2025-06-03 + 365 = 2026-06-03
        self.assertEqual(out["dates"][-1], "2026-06-03")
        self.assertEqual(len(out["dates"]), len(out["values"]))

    def test_default_offset_is_52_weeks(self):
        # 既定オフセットは 364 日（52週=曜日合わせ）
        self.assertEqual(PREVYEAR_OFFSET_DAYS, 364)
        s = _daily("2024-01-01", "2025-06-10", lambda d, i: 10)
        default = build_prevyear_ma_series(s, BASE)
        w52 = build_prevyear_ma_series(s, BASE, offset_days=364)
        cal = build_prevyear_ma_series(s, BASE, offset_days=365)
        # 既定 == 52週(364)、暦日(365)とは先頭日が異なる
        self.assertEqual(default["dates"], w52["dates"])
        self.assertNotEqual(default["dates"][0], cal["dates"][0])
        # いずれも末尾は基準日に揃う（shifted_base がオフセットに追従するため）
        self.assertEqual(default["dates"][-1], "2026-06-03")
        self.assertEqual(cal["dates"][-1], "2026-06-03")


class TestPrevyearWeeklySeries(unittest.TestCase):
    def test_empty_input(self):
        empty = pd.DataFrame(columns=["日付", "値"])
        self.assertEqual(build_prevyear_weekly_series(empty, BASE), {"dates": [], "values": []})

    def test_weekday_only_weekly_sum(self):
        # 平日4件/日・週末0 → 週次合計は 4*5 = 20 件/週、28日平滑後も 20
        s = _daily("2024-01-01", "2026-06-03",
                   lambda d, i: 4 if d.weekday() < 5 else 0)
        out = build_prevyear_weekly_series(s, BASE)
        self.assertEqual(out["dates"][-1], "2026-06-03")
        self.assertAlmostEqual(out["values"][-1], 20.0, places=6)

    def test_units_are_weekly_not_daily(self):
        # 毎日1件 → 7日合計=7、平滑後も 7（件/週）
        s = _daily("2024-01-01", "2026-06-03", lambda d, i: 1)
        out = build_prevyear_weekly_series(s, BASE)
        self.assertAlmostEqual(out["values"][-1], 7.0, places=6)


class TestBizMa30PrevAlign(unittest.TestCase):
    def _surg(self, start, end):
        idx = pd.date_range(start, end, freq="D")
        rows = []
        for d in idx:
            n = 5 if d.weekday() < 5 else 0  # 平日5件
            for _ in range(n):
                rows.append({"手術実施日": d, "全麻": True})
        return pd.DataFrame(rows)

    def test_prev_year_last_aligns_to_base(self):
        surg = self._surg("2024-01-01", "2026-06-03")
        prev = build_biz_ma30_series(surg, BASE, prev_year=True)
        self.assertTrue(prev["dates"])
        # 前年系列の末尾は基準日付近に揃う（営業平日のみのため厳密一致でなく<=base）
        self.assertLessEqual(pd.Timestamp(prev["dates"][-1]), BASE)
        self.assertEqual(len(prev["dates"]), len(prev["values"]))

    def test_empty_surgery(self):
        empty = pd.DataFrame(columns=["手術実施日", "全麻"])
        self.assertEqual(build_biz_ma30_series(empty, BASE), {"dates": [], "values": []})


def _census_adm(base, ward_levels):
    """直近8完全週(月〜日)の日次在院 adm を合成。
    ward_levels: {病棟コード: (平日在院, 土日在院)}（各日1行）。"""
    monday = base - pd.Timedelta(days=base.weekday())
    idx = pd.date_range(monday - pd.Timedelta(days=56), monday - pd.Timedelta(days=1), freq="D")
    rows = []
    for d in idx:
        we = d.weekday() >= 5
        for w, (wk, wknd) in ward_levels.items():
            rows.append({"日付": d, "病棟_表示": True, "病棟コード": w,
                         "在院患者数": (wknd if we else wk)})
    return pd.DataFrame(rows)


class TestWeekendCensusRetention(unittest.TestCase):
    """週末(土日)在院ディップ：維持率＝土日÷平日、のびしろ＝(平日−土日)×2。"""

    def test_retention_and_room(self):
        r = weekend_census_retention(_census_adm(BASE, {"W1": (100, 80)}),
                                     BASE, entity="ward", weeks=8)
        u = r["units"][0]
        self.assertEqual(u["name"], "W1")
        self.assertAlmostEqual(u["weekday_avg"], 100.0)
        self.assertAlmostEqual(u["weekend_avg"], 80.0)
        self.assertAlmostEqual(u["retention"], 0.8)
        self.assertAlmostEqual(u["room_per_week"], 40.0)      # (100-80)*2
        self.assertAlmostEqual(r["total"]["retention"], 0.8)

    def test_sorted_by_room_desc_and_min_filter(self):
        r = weekend_census_retention(
            _census_adm(BASE, {"BIG": (100, 70), "MID": (50, 44), "SMALL": (3, 1)}),
            BASE, entity="ward", weeks=8, min_weekday_avg=5.0)
        names = [u["name"] for u in r["units"]]
        self.assertEqual(names, ["BIG", "MID"])               # room降順・SMALLは平日<5で除外

    def test_room_clipped_when_weekend_higher(self):
        # 週末の方が在院が高い＝お手本（維持率>100%・のびしろ0）
        r = weekend_census_retention(_census_adm(BASE, {"MODEL": (80, 90)}),
                                     BASE, entity="ward", weeks=8)
        u = r["units"][0]
        self.assertEqual(u["room_per_week"], 0.0)
        self.assertGreater(u["retention"], 1.0)

    def test_census_delta_constant_is_zero(self):
        # 在院サマリ バッジ用フィールド: 定常な在院では census_delta_4w=0
        r = weekend_census_retention(_census_adm(BASE, {"W1": (100, 80)}),
                                     BASE, entity="ward", weeks=8)
        u = r["units"][0]
        self.assertIn("census_delta_4w", u)
        self.assertAlmostEqual(u["census_delta_4w"], 0.0)

    def test_census_delta_4w_tracks_weekday_change(self):
        # 平日在院が 前4週90 → 直近4週110 なら census_delta_4w=+20
        monday = BASE - pd.Timedelta(days=BASE.weekday())
        mid = monday - pd.Timedelta(days=28)   # 直近半=[mid,end] / 前半=[start,mid)
        idx = pd.date_range(monday - pd.Timedelta(days=56), monday - pd.Timedelta(days=1), freq="D")
        rows = []
        for d in idx:
            we = d.weekday() >= 5
            wk = 110 if d >= mid else 90
            rows.append({"日付": d, "病棟_表示": True, "病棟コード": "W1",
                         "在院患者数": (80 if we else wk)})
        r = weekend_census_retention(pd.DataFrame(rows), BASE, entity="ward", weeks=8)
        u = r["units"][0]
        self.assertAlmostEqual(u["census_delta_4w"], 20.0)

    def test_empty_input(self):
        empty = pd.DataFrame(columns=["日付", "病棟_表示", "病棟コード", "在院患者数"])
        r = weekend_census_retention(empty, BASE, entity="ward")
        self.assertEqual(r["units"], [])


if __name__ == "__main__":
    unittest.main()
