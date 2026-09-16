"""stats_band.py（A6: 通常変動帯 ±1.5σ・表示専用）の回帰テスト。

対象:
  - census_spread_samples                : 在院の週次サンプル点での短期/長期MAスプレッド(%)
  - surgery_rate_spread_samples          : 全麻(病院合計)の週次サンプル点での28日窓レートスプレッド(%)
  - surgery_rate_spread_samples_by_dept  : 術数対象・診療科別の同スプレッド(%)（P2是正）
  - unit_sigma                           : サンプル不足(<MIN_SAMPLES)でNoneへ縮退
  - band_width_pct               : floor と 1.5σ の大小分岐
  - build_band                   : 戻り値スキーマ(sigma_pct/width_pct/floor_pct)

triage._ma_spread / _surgery_trend と同一式の複製であることの確認は本ファイルの
スコープ外（判定コード triage.py は読むだけで編集しない。式の一致は目視レビュー
で確認済み・spec/改修プラン_訴求力強化.md §7 A6 裁定）。

実行: リポジトリルートで
    OMLX_BASE_URL=http://127.0.0.1:9 .venv/bin/python -m pytest -q tests/test_stats_band.py
"""
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.stats_band import (  # noqa: E402
    CENSUS_FLOOR_PCT,
    SURGERY_FLOOR_PCT,
    MIN_SAMPLES,
    census_spread_samples,
    surgery_rate_spread_samples,
    surgery_rate_spread_samples_by_dept,
    unit_sigma,
    band_width_pct,
    build_band,
)


def _constant_census_series(base_date: pd.Timestamp, days: int, value: float = 100.0) -> pd.DataFrame:
    dates = pd.date_range(base_date - pd.Timedelta(days=days - 1), base_date, freq="D")
    return pd.DataFrame({"日付": dates, "値": [value] * len(dates)})


def _constant_surgery_df(base_date: pd.Timestamp, days: int) -> pd.DataFrame:
    """営業日1日あたり1件ずつ全麻手術がある想定の合成データ（土日祝は0件）。

    件/営業日レートが常に1.0で一定になるようにする（土日にも件数を積むと
    窓の切れ目で土日の混入数が変動し、意図しないスプレッドが出てしまうため）。
    """
    from app.lib.config import is_operational_day

    dates = pd.date_range(base_date - pd.Timedelta(days=days - 1), base_date, freq="D")
    biz_dates = [d for d in dates if is_operational_day(d)]
    return pd.DataFrame({"手術実施日": biz_dates, "全麻": [True] * len(biz_dates)})


class CensusSpreadSamplesTest(unittest.TestCase):
    def test_constant_series_yields_zero_spread_for_each_requested_week(self):
        base = pd.Timestamp("2026-06-01")
        series = _constant_census_series(base, days=120, value=100.0)
        samples = census_spread_samples(series, base, weeks=5)
        self.assertEqual(len(samples), 5)
        for v in samples:
            self.assertAlmostEqual(v, 0.0, places=6)

    def test_insufficient_history_yields_fewer_samples_than_requested_weeks(self):
        # 直近28日分しかデータがないため、遡るほど long_win(28日窓に14件必要)が
        # 不足して skip される → weeks=10 を要求しても全件は返らない。
        base = pd.Timestamp("2026-06-01")
        series = _constant_census_series(base, days=28, value=100.0)
        samples = census_spread_samples(series, base, weeks=10)
        self.assertLess(len(samples), 10)


class SurgeryRateSpreadSamplesTest(unittest.TestCase):
    def test_constant_rate_yields_zero_spread_for_each_requested_week(self):
        base = pd.Timestamp("2026-06-01")
        surg = _constant_surgery_df(base, days=250)
        samples = surgery_rate_spread_samples(surg, base, weeks=4)
        self.assertEqual(len(samples), 4)
        for v in samples:
            self.assertAlmostEqual(v, 0.0, places=6)

    def test_no_surgery_data_yields_no_samples(self):
        base = pd.Timestamp("2026-06-01")
        surg = pd.DataFrame({"手術実施日": pd.Series([], dtype="datetime64[ns]"),
                             "全麻": pd.Series([], dtype=bool)})
        samples = surgery_rate_spread_samples(surg, base, weeks=4)
        self.assertEqual(samples, [])


def _ophthalmology_type_surgery_df(base_date: pd.Timestamp, biz_days: int = 300) -> pd.DataFrame:
    """眼科型（全麻ほぼ0・全手術多数）の合成手術データ。

    - 術数対象は営業日ごとに4/5/6/5件を周期させる（眼科=全手術が術数対象）
    - 全麻は10営業日に1回だけ発生させる（眼科は全麻手術がほぼ無いことの再現）
    他科は含めない（病院合計=全麻固定の既存関数を眼科単科のデータへ流用したときの
    縮退を再現するため）。
    """
    from app.lib.config import is_operational_day

    start = base_date - pd.Timedelta(days=biz_days * 2)
    all_days = pd.date_range(start, base_date, freq="D")
    biz = [d for d in all_days if is_operational_day(d)]
    pattern = [4, 5, 6, 5]
    rows = []
    for i, d in enumerate(biz):
        for _ in range(pattern[i % len(pattern)]):
            rows.append({"手術実施日": d, "実施診療科": "眼科", "全麻": False, "術数対象": True})
        if i % 10 == 0:
            rows.append({"手術実施日": d, "実施診療科": "眼科", "全麻": True, "術数対象": True})
    return pd.DataFrame(rows)


class SurgeryRateSpreadSamplesByDeptTest(unittest.TestCase):
    """P2是正: 眼科型データで全麻基準(既存)と術数対象基準(新設)のσが異なることを確認。"""

    def test_ga_basis_and_target_basis_sigma_differ_for_ophthalmology_type_data(self):
        base = pd.Timestamp("2026-06-01")
        surg = _ophthalmology_type_surgery_df(base)

        ga_samples = surgery_rate_spread_samples(surg, base, weeks=52)
        dept_samples = surgery_rate_spread_samples_by_dept(surg, base, "眼科", weeks=52)

        ga_sigma = unit_sigma(ga_samples)
        dept_sigma = unit_sigma(dept_samples)

        # 両基準ともサンプルは確保できる(縮退しない)が、全麻がほぼ0の眼科では
        # 全麻固定基準のレートが不安定になり、術数対象基準よりσが大きく出る。
        self.assertIsNotNone(ga_sigma)
        self.assertIsNotNone(dept_sigma)
        self.assertNotEqual(ga_sigma, dept_sigma)
        self.assertGreater(ga_sigma, dept_sigma * 5)

    def test_constant_rate_yields_zero_spread_for_each_requested_week(self):
        base = pd.Timestamp("2026-06-01")
        from app.lib.config import is_operational_day
        dates = pd.date_range(base - pd.Timedelta(days=249), base, freq="D")
        biz_dates = [d for d in dates if is_operational_day(d)]
        surg = pd.DataFrame({
            "手術実施日": biz_dates,
            "実施診療科": ["眼科"] * len(biz_dates),
            "全麻": [False] * len(biz_dates),
            "術数対象": [True] * len(biz_dates),
        })
        samples = surgery_rate_spread_samples_by_dept(surg, base, "眼科", weeks=4)
        self.assertEqual(len(samples), 4)
        for v in samples:
            self.assertAlmostEqual(v, 0.0, places=6)

    def test_other_dept_rows_are_excluded(self):
        base = pd.Timestamp("2026-06-01")
        from app.lib.config import is_operational_day
        dates = pd.date_range(base - pd.Timedelta(days=249), base, freq="D")
        biz_dates = [d for d in dates if is_operational_day(d)]
        surg = pd.DataFrame({
            "手術実施日": list(biz_dates) * 2,
            "実施診療科": ["眼科"] * len(biz_dates) + ["外科"] * len(biz_dates),
            "全麻": [False] * len(biz_dates) + [True] * len(biz_dates),
            "術数対象": [True] * len(biz_dates) + [True] * len(biz_dates),
        })
        samples = surgery_rate_spread_samples_by_dept(surg, base, "眼科", weeks=4)
        self.assertEqual(len(samples), 4)
        for v in samples:
            self.assertAlmostEqual(v, 0.0, places=6)

    def test_no_surgery_data_yields_no_samples(self):
        base = pd.Timestamp("2026-06-01")
        surg = pd.DataFrame({
            "手術実施日": pd.Series([], dtype="datetime64[ns]"),
            "実施診療科": pd.Series([], dtype=str),
            "術数対象": pd.Series([], dtype=bool),
        })
        samples = surgery_rate_spread_samples_by_dept(surg, base, "眼科", weeks=4)
        self.assertEqual(samples, [])


class UnitSigmaTest(unittest.TestCase):
    def test_below_min_samples_returns_none(self):
        self.assertLess(7, MIN_SAMPLES)
        self.assertIsNone(unit_sigma([1.0] * 7))

    def test_at_min_samples_computes_stdev(self):
        samples = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
        self.assertEqual(len(samples), MIN_SAMPLES)
        expected = round(float(pd.Series(samples).std(ddof=1)), 2)
        self.assertEqual(unit_sigma(samples), expected)


class BandWidthPctTest(unittest.TestCase):
    def test_none_sigma_returns_floor(self):
        self.assertEqual(band_width_pct(None, CENSUS_FLOOR_PCT), CENSUS_FLOOR_PCT)

    def test_small_sigma_stays_at_floor(self):
        # 1.5 * 1.0 = 1.5 < floor(3.0) → floor が勝つ
        self.assertEqual(band_width_pct(1.0, CENSUS_FLOOR_PCT), CENSUS_FLOOR_PCT)

    def test_large_sigma_exceeds_floor(self):
        # 1.5 * 4.0 = 6.0 > floor(3.0) → 1.5σ が勝つ
        self.assertEqual(band_width_pct(4.0, CENSUS_FLOOR_PCT), 6.0)


class BuildBandTest(unittest.TestCase):
    def test_census_kind_uses_census_floor(self):
        band = build_band("census", None)
        self.assertEqual(band, {"sigma_pct": None, "width_pct": CENSUS_FLOOR_PCT, "floor_pct": CENSUS_FLOOR_PCT})

    def test_surgery_rate_kind_uses_surgery_floor(self):
        band = build_band("surgery_rate", None)
        self.assertEqual(band["floor_pct"], SURGERY_FLOOR_PCT)
        self.assertEqual(band["width_pct"], SURGERY_FLOOR_PCT)

    def test_schema_keys(self):
        band = build_band("census", 10.0)
        self.assertEqual(set(band.keys()), {"sigma_pct", "width_pct", "floor_pct"})
        self.assertEqual(band["sigma_pct"], 10.0)
        self.assertEqual(band["width_pct"], 15.0)  # 1.5*10.0


if __name__ == "__main__":
    unittest.main()
