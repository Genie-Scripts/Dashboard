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

    def test_offset_is_365_days(self):
        s = _daily("2024-01-01", "2025-06-03", lambda d, i: 10)
        out = build_prevyear_ma_series(s, BASE, window=28, offset_days=365)
        # 入力最終日 2025-06-03 + 365 = 2026-06-03
        self.assertEqual(out["dates"][-1], "2026-06-03")
        self.assertEqual(len(out["dates"]), len(out["values"]))


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


if __name__ == "__main__":
    unittest.main()
