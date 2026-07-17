"""
2026-06診療報酬改定 換算（profit_estimate.py）のユニットテスト（合成データのみ）。

対象:
  - adjust_profit_for_fee_revision: 改定前行への区分別係数の適用・非破壊性
  - 二重換算防止（構造テスト）: monthend_projection_total / compute_projection_calibration
    が build_hybrid_payload に生の profit_breakdown を渡す（自らは換算しない）こと、
    及び fit_profit_estimators のゲート（予測対象月が改定日以後のときだけ適用）
  - _calib_cache_entry_reusable: 月次比キャッシュの再利用可否判定
  - _recency_actual_adjusted: 改定前実績の改定後スケール換算（内訳あり/fallback）

実行: リポジトリルートで
    python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.config import FEE_REVISION_PROFIT_UPLIFT  # noqa: E402
from app.lib import profit_estimate  # noqa: E402
from app.lib.profit_estimate import (  # noqa: E402
    adjust_profit_for_fee_revision,
    _calib_cache_entry_reusable,
    _recency_actual_adjusted,
    monthend_projection_total,
    compute_projection_calibration,
    fit_profit_estimators,
    project_dept_monthend,
    _deadjust_display_series,
)
from app.lib.dept_report import (  # noqa: E402
    _prev_needs_revision_adjust,
    _revision_adjusted_prev,
    _unit_profit_series,
)

FEE_REVISION_TS = pd.Timestamp("2026-06-01")
UPLIFT_GAIRAI = FEE_REVISION_PROFIT_UPLIFT["外来"]
UPLIFT_NYUIN = FEE_REVISION_PROFIT_UPLIFT["入院"]


def _synthetic_pb() -> pd.DataFrame:
    """2診療科 × 月{04,05,06} × 区分{外来,入院} の合成 profit_breakdown。"""
    rows = []
    for dept in ("内科A", "外科B"):
        for month in ("2026-04-01", "2026-05-01", "2026-06-01"):
            rows.append({"診療科名": dept, "月": pd.Timestamp(month),
                        "区分": "外来", "粗利": 1000.0})
            rows.append({"診療科名": dept, "月": pd.Timestamp(month),
                        "区分": "入院", "粗利": 2000.0})
    return pd.DataFrame(rows)


class TestAdjustHelper(unittest.TestCase):
    def setUp(self):
        self.pb = _synthetic_pb()

    def test_pre_revision_gairai_scaled(self):
        out = adjust_profit_for_fee_revision(self.pb)
        pre_gairai = out[(out["月"] < FEE_REVISION_TS) & (out["区分"] == "外来")]
        self.assertTrue(len(pre_gairai) > 0)
        for v in pre_gairai["粗利"]:
            self.assertAlmostEqual(v, 1000.0 * UPLIFT_GAIRAI, places=6)

    def test_pre_revision_nyuin_scaled(self):
        out = adjust_profit_for_fee_revision(self.pb)
        pre_nyuin = out[(out["月"] < FEE_REVISION_TS) & (out["区分"] == "入院")]
        self.assertTrue(len(pre_nyuin) > 0)
        for v in pre_nyuin["粗利"]:
            self.assertAlmostEqual(v, 2000.0 * UPLIFT_NYUIN, places=6)

    def test_post_revision_month_unchanged(self):
        out = adjust_profit_for_fee_revision(self.pb)
        post = out[out["月"] == FEE_REVISION_TS]
        self.assertTrue(len(post) > 0)
        for _, r in post.iterrows():
            expected = 1000.0 if r["区分"] == "外来" else 2000.0
            self.assertAlmostEqual(r["粗利"], expected, places=6)

    def test_columns_preserved(self):
        out = adjust_profit_for_fee_revision(self.pb)
        self.assertEqual(list(out.columns), list(self.pb.columns))

    def test_profit_dtype_is_float(self):
        pb_int = self.pb.copy()
        pb_int["粗利"] = pb_int["粗利"].astype(int)
        out = adjust_profit_for_fee_revision(pb_int)
        self.assertEqual(out["粗利"].dtype, np.float64)

    def test_input_not_mutated(self):
        before = self.pb.copy()
        _ = adjust_profit_for_fee_revision(self.pb)
        pd.testing.assert_frame_equal(self.pb, before)

    def test_none_passthrough(self):
        self.assertIsNone(adjust_profit_for_fee_revision(None))

    def test_empty_passthrough(self):
        empty = self.pb.iloc[0:0]
        out = adjust_profit_for_fee_revision(empty)
        self.assertEqual(len(out), 0)

    def test_missing_kubun_column_passthrough(self):
        no_kubun = self.pb.drop(columns=["区分"])
        out = adjust_profit_for_fee_revision(no_kubun)
        pd.testing.assert_frame_equal(out, no_kubun)


def _long_history_pb(dept: str, months: list, gairai: float, nyuin: float) -> pd.DataFrame:
    """月ごとに区分別粗利が一定の合成 pb（複数月の履歴・区分列なし可）。"""
    rows = []
    for m in months:
        rows.append({"診療科名": dept, "月": pd.Timestamp(m),
                    "区分": "外来", "粗利": gairai})
        rows.append({"診療科名": dept, "月": pd.Timestamp(m),
                    "区分": "入院", "粗利": nyuin})
    return pd.DataFrame(rows)


class TestNoDoubleAdjustment(unittest.TestCase):
    """build_hybrid_payload をスタブ化し、内部チェーンが profit_breakdown を
    自ら換算せず leaf（build_hybrid_payload）に委ねていることを確認する。"""

    def setUp(self):
        # 6ヶ月以上の履歴が必要（min_history 既定=6）。2025-12〜2026-06 の7ヶ月。
        months = ["2025-12-01", "2026-01-01", "2026-02-01", "2026-03-01",
                 "2026-04-01", "2026-05-01", "2026-06-01"]
        self.raw_pb = _long_history_pb("内科A", months, gairai=500.0, nyuin=2000.0)
        self.tiny_adm = pd.DataFrame({"日付": [pd.Timestamp("2026-07-01")]})
        self.tiny_surg = pd.DataFrame({"手術実施日": [pd.Timestamp("2026-07-01")]})

    def _expected_raw_slice(self, month_start: str) -> pd.DataFrame:
        cutoff = pd.Timestamp(month_start)
        pb = self.raw_pb.copy()
        pb["月"] = pd.to_datetime(pb["月"])
        return pb[pb["月"] < cutoff].sort_values(["診療科名", "月", "区分"]).reset_index(drop=True)

    def test_monthend_projection_total_passes_raw_pb(self):
        captured = {}

        def fake_build_hybrid_payload(profit_breakdown, surg, base_date, adm=None, **kwargs):
            captured["pb"] = profit_breakdown.copy()
            return {"meta": {"latest_mtdblend_total": 100.0}}

        with patch.object(profit_estimate, "build_hybrid_payload",
                          side_effect=fake_build_hybrid_payload):
            result = monthend_projection_total(
                self.raw_pb, surg=self.tiny_surg, adm=self.tiny_adm, month_start="2026-06-01")

        self.assertEqual(result, 100.0)
        self.assertIn("pb", captured)
        got = captured["pb"].copy()
        got["月"] = pd.to_datetime(got["月"])
        got = got.sort_values(["診療科名", "月", "区分"]).reset_index(drop=True)
        expected = self._expected_raw_slice("2026-06-01")
        pd.testing.assert_frame_equal(got[["診療科名", "月", "区分", "粗利"]],
                                      expected[["診療科名", "月", "区分", "粗利"]])

    def test_compute_projection_calibration_passes_raw_pb(self):
        captured_list = []

        def fake_build_hybrid_payload(profit_breakdown, surg, base_date, adm=None, **kwargs):
            captured_list.append(profit_breakdown.copy())
            return {"meta": {"latest_mtdblend_total": 100.0}}

        with patch.object(profit_estimate, "build_hybrid_payload",
                          side_effect=fake_build_hybrid_payload):
            cal = compute_projection_calibration(
                self.raw_pb, self.tiny_surg, self.tiny_adm, "2026-07-01",
                known_ratios={}, k=1)

        self.assertEqual(cal["n_months"], 1)
        self.assertEqual(len(captured_list), 1)
        got = captured_list[0].copy()
        got["月"] = pd.to_datetime(got["月"])
        got = got.sort_values(["診療科名", "月", "区分"]).reset_index(drop=True)
        expected = self._expected_raw_slice("2026-06-01")
        pd.testing.assert_frame_equal(got[["診療科名", "月", "区分", "粗利"]],
                                      expected[["診療科名", "月", "区分", "粗利"]])


def _const_daily_adm(dept: str, start, end, census: float, new_adm: float) -> pd.DataFrame:
    idx = pd.date_range(start, end, freq="D")
    return pd.DataFrame({
        "日付": idx,
        "診療科名": dept,
        "在院患者数": census,
        "新入院患者数": new_adm,
    })


def _const_daily_surg(dept: str, start, end, n_nyuin: int, n_gairai: int) -> pd.DataFrame:
    idx = pd.date_range(start, end, freq="D")
    rows = []
    for d in idx:
        for _ in range(n_nyuin):
            rows.append({"手術実施日": d, "実施診療科": dept, "入外区分": "入院"})
        for _ in range(n_gairai):
            rows.append({"手術実施日": d, "実施診療科": dept, "入外区分": "外来"})
    return pd.DataFrame(rows)


class TestFitProfitEstimatorsGate(unittest.TestCase):
    """予測対象月（最新確報月+1ヶ月）が改定日以後のときだけ改定換算が乗る。"""

    DEPT = "外科A"

    def setUp(self):
        # ドライバーは一定の日次レート（Oct2025〜May2026 を一括カバー）
        self.adm = _const_daily_adm(self.DEPT, "2025-10-01", "2026-05-31",
                                    census=50.0, new_adm=5.0)
        self.surg = _const_daily_surg(self.DEPT, "2025-10-01", "2026-05-31",
                                      n_nyuin=2, n_gairai=1)

    def _pb(self, months: list) -> pd.DataFrame:
        return _long_history_pb(self.DEPT, months, gairai=500.0, nyuin=2000.0)

    def test_gate_on_scales_by_uplift(self):
        # 最新確報月=2026-05 → 予測対象2026-06 → ゲートON（全6ヶ月が改定前）
        months = ["2025-12-01", "2026-01-01", "2026-02-01",
                 "2026-03-01", "2026-04-01", "2026-05-01"]
        pb = self._pb(months)
        est_true = fit_profit_estimators(pb, self.adm, self.surg, fee_revision_adjust=True)
        est_false = fit_profit_estimators(pb, self.adm, self.surg, fee_revision_adjust=False)

        g_true = est_true[self.DEPT]["gairai"]
        g_false = est_false[self.DEPT]["gairai"]
        n_true = est_true[self.DEPT]["nyuin"]
        n_false = est_false[self.DEPT]["nyuin"]

        self.assertAlmostEqual(g_true["alpha"], g_false["alpha"] * UPLIFT_GAIRAI, places=4)
        self.assertAlmostEqual(g_true["beta"], g_false["beta"] * UPLIFT_GAIRAI, places=4)
        self.assertAlmostEqual(n_true["d"], n_false["d"] * UPLIFT_NYUIN, places=4)
        self.assertAlmostEqual(n_true["e"], n_false["e"] * UPLIFT_NYUIN, places=4)
        self.assertAlmostEqual(n_true["f"], n_false["f"] * UPLIFT_NYUIN, places=4)

    def test_gate_off_leaves_coefficients_unchanged(self):
        # 最新確報月=2026-03 → 予測対象2026-04 → ゲートOFF
        months = ["2025-10-01", "2025-11-01", "2025-12-01",
                 "2026-01-01", "2026-02-01", "2026-03-01"]
        pb = self._pb(months)
        est_true = fit_profit_estimators(pb, self.adm, self.surg, fee_revision_adjust=True)
        est_false = fit_profit_estimators(pb, self.adm, self.surg, fee_revision_adjust=False)

        g_true = est_true[self.DEPT]["gairai"]
        g_false = est_false[self.DEPT]["gairai"]
        n_true = est_true[self.DEPT]["nyuin"]
        n_false = est_false[self.DEPT]["nyuin"]

        self.assertAlmostEqual(g_true["alpha"], g_false["alpha"], places=9)
        self.assertAlmostEqual(g_true["beta"], g_false["beta"], places=9)
        self.assertAlmostEqual(n_true["d"], n_false["d"], places=9)
        self.assertAlmostEqual(n_true["e"], n_false["e"], places=9)
        self.assertAlmostEqual(n_true["f"], n_false["f"], places=9)


class TestCacheReusePredicate(unittest.TestCase):
    METRIC = "latest_mtdblend_total"
    MODEL_REV = "feerev@2026-06-01(入院:1.144,外来:1.053)"

    def _cached(self, **overrides):
        base = {"proj": 100.0, "actual": 95.0, "metric": self.METRIC,
               "model_rev": self.MODEL_REV}
        base.update(overrides)
        return base

    def test_model_rev_missing_false(self):
        cached = {"proj": 100.0, "actual": 95.0, "metric": self.METRIC}
        self.assertFalse(_calib_cache_entry_reusable(cached, 95.0, self.METRIC, self.MODEL_REV))

    def test_model_rev_mismatch_false(self):
        cached = self._cached(model_rev="feerev@2026-06-01(入院:1.2,外来:1.05)")
        self.assertFalse(_calib_cache_entry_reusable(cached, 95.0, self.METRIC, self.MODEL_REV))

    def test_metric_mismatch_false(self):
        cached = self._cached(metric="latest_projection_total")
        self.assertFalse(_calib_cache_entry_reusable(cached, 95.0, self.METRIC, self.MODEL_REV))

    def test_actual_drift_beyond_tolerance_false(self):
        cached = self._cached(actual=95.001)  # 1e-6 を超えるドリフト
        self.assertFalse(_calib_cache_entry_reusable(cached, 95.0, self.METRIC, self.MODEL_REV))

    def test_exact_match_true(self):
        cached = self._cached()
        self.assertTrue(_calib_cache_entry_reusable(cached, 95.0, self.METRIC, self.MODEL_REV))

    def test_cached_none_false(self):
        self.assertFalse(_calib_cache_entry_reusable(None, 95.0, self.METRIC, self.MODEL_REV))

    def test_proj_missing_false(self):
        cached = {"actual": 95.0, "metric": self.METRIC, "model_rev": self.MODEL_REV}
        self.assertFalse(_calib_cache_entry_reusable(cached, 95.0, self.METRIC, self.MODEL_REV))


class TestRecencyActualAdjusted(unittest.TestCase):
    def test_uses_breakdown_when_available(self):
        g_act, n_act = 300.0, 700.0
        out = _recency_actual_adjusted(1000.0, g_act, n_act, pred_gairai_share=0.5)
        expected = g_act * UPLIFT_GAIRAI + n_act * UPLIFT_NYUIN
        self.assertAlmostEqual(out, expected, places=6)

    def test_fallback_blend_weight_zero(self):
        # w=0 → 全額 入院係数
        out = _recency_actual_adjusted(1000.0, None, None, pred_gairai_share=0.0)
        self.assertAlmostEqual(out, 1000.0 * UPLIFT_NYUIN, places=6)

    def test_fallback_blend_weight_one(self):
        # w=1 → 全額 外来係数
        out = _recency_actual_adjusted(1000.0, None, None, pred_gairai_share=1.0)
        self.assertAlmostEqual(out, 1000.0 * UPLIFT_GAIRAI, places=6)

    def test_fallback_blend_weight_half(self):
        out = _recency_actual_adjusted(1000.0, None, None, pred_gairai_share=0.5)
        expected = 1000.0 * (0.5 * UPLIFT_GAIRAI + 0.5 * UPLIFT_NYUIN)
        self.assertAlmostEqual(out, expected, places=6)

    def test_nan_gairai_falls_back_to_blend(self):
        out = _recency_actual_adjusted(1000.0, float("nan"), 700.0, pred_gairai_share=0.3)
        expected = 1000.0 * (0.3 * UPLIFT_GAIRAI + 0.7 * UPLIFT_NYUIN)
        self.assertAlmostEqual(out, expected, places=6)

    def test_pred_gairai_share_clipped_below(self):
        out = _recency_actual_adjusted(1000.0, None, None, pred_gairai_share=-0.5)
        self.assertAlmostEqual(out, 1000.0 * UPLIFT_NYUIN, places=6)

    def test_pred_gairai_share_clipped_above(self):
        out = _recency_actual_adjusted(1000.0, None, None, pred_gairai_share=1.5)
        self.assertAlmostEqual(out, 1000.0 * UPLIFT_GAIRAI, places=6)


class TestDeadjustDisplaySeries(unittest.TestCase):
    """_deadjust_display_series: 表示用 de-adjust（改定前区間の割り戻し）。"""

    DATES = ["2026-05-30", "2026-05-31", "2026-06-01", "2026-06-02"]

    def _full_series(self):
        # values_total / ols_total / values_projection_total / values_mtd_total /
        # values_blend_total は元値のまま渡す（関数側で外来+入院から再合成される）。
        return {
            "dates": self.DATES,
            "values_gairai": [100.0, 200.0, 300.0, 400.0],
            "values_nyuin":  [1000.0, 2000.0, 3000.0, 4000.0],
            "values_total":  [1100.0, 2200.0, 3300.0, 4400.0],
            "ols_gairai": [10.0, 20.0, 30.0, 40.0],
            "ols_nyuin":  [100.0, 200.0, 300.0, 400.0],
            "ols_total":  [110.0, 220.0, 330.0, 440.0],
            "values_projection_gairai": [11.0, 21.0, 31.0, 41.0],
            "values_projection_nyuin":  [101.0, 201.0, 301.0, 401.0],
            "values_projection_total":  [112.0, 222.0, 332.0, 442.0],
            "values_mtd_gairai": [12.0, 22.0, 32.0, 42.0],
            "values_mtd_nyuin":  [102.0, 202.0, 302.0, 402.0],
            "values_mtd_total":  [114.0, 224.0, 334.0, 444.0],
            "values_blend_gairai": [13.0, 23.0, 33.0, 43.0],
            "values_blend_nyuin":  [103.0, 203.0, 303.0, 403.0],
            "values_blend_total":  [116.0, 226.0, 336.0, 446.0],
        }

    def test_pre_revision_gairai_scaled(self):
        series = self._full_series()
        _deadjust_display_series(series, self.DATES)
        # 2026-05-30, 2026-05-31 は改定前 → 1/UPLIFT_GAIRAI 倍
        self.assertAlmostEqual(series["values_gairai"][0], round(100.0 / UPLIFT_GAIRAI, 2), places=2)
        self.assertAlmostEqual(series["values_gairai"][1], round(200.0 / UPLIFT_GAIRAI, 2), places=2)

    def test_pre_revision_nyuin_scaled(self):
        series = self._full_series()
        _deadjust_display_series(series, self.DATES)
        self.assertAlmostEqual(series["values_nyuin"][0], round(1000.0 / UPLIFT_NYUIN, 2), places=2)
        self.assertAlmostEqual(series["values_nyuin"][1], round(2000.0 / UPLIFT_NYUIN, 2), places=2)

    def test_post_revision_unchanged(self):
        series = self._full_series()
        before_g = series["values_gairai"][2:]
        before_n = series["values_nyuin"][2:]
        _deadjust_display_series(series, self.DATES)
        # 2026-06-01, 2026-06-02 は改定後 → 不変
        self.assertEqual(series["values_gairai"][2:], before_g)
        self.assertEqual(series["values_nyuin"][2:], before_n)

    def test_total_is_recomposed_sum_not_single_factor_division(self):
        series = self._full_series()
        _deadjust_display_series(series, self.DATES)
        expected_g0 = round(100.0 / UPLIFT_GAIRAI, 2)
        expected_n0 = round(1000.0 / UPLIFT_NYUIN, 2)
        expected_total0 = round(expected_g0 + expected_n0, 2)
        self.assertAlmostEqual(series["values_total"][0], expected_total0, places=2)
        # 単一係数（外来 or 入院のどちらか）で元 total を割った値とは異なることを確認
        # （外来・入院で係数が違うケースなので、単一係数割りとは一致しないはず）
        self.assertNotAlmostEqual(series["values_total"][0], round(1100.0 / UPLIFT_GAIRAI, 2), places=2)
        self.assertNotAlmostEqual(series["values_total"][0], round(1100.0 / UPLIFT_NYUIN, 2), places=2)

    def test_month_boundary_may31_scaled_june01_not(self):
        series = self._full_series()
        _deadjust_display_series(series, self.DATES)
        self.assertAlmostEqual(series["values_gairai"][1], round(200.0 / UPLIFT_GAIRAI, 2), places=2)
        self.assertEqual(series["values_gairai"][2], 300.0)

    def test_none_values_preserved(self):
        series = self._full_series()
        series["values_gairai"] = [None, 200.0, None, 400.0]
        series["values_nyuin"] = [1000.0, None, 3000.0, None]
        series["values_total"] = [1100.0, 2200.0, 3300.0, 4400.0]
        _deadjust_display_series(series, self.DATES)
        self.assertIsNone(series["values_gairai"][0])
        self.assertIsNone(series["values_gairai"][2])
        self.assertIsNone(series["values_nyuin"][1])
        self.assertIsNone(series["values_nyuin"][3])
        # total は g, n のどちらかが None のときは None になる
        self.assertIsNone(series["values_total"][0])
        self.assertIsNone(series["values_total"][1])

    def test_missing_keys_no_exception_and_only_present_keys_processed(self):
        series = {
            "dates": self.DATES,
            "values_gairai": [100.0, 200.0, 300.0, 400.0],
            "values_nyuin":  [1000.0, 2000.0, 3000.0, 4000.0],
            "values_total":  [1100.0, 2200.0, 3300.0, 4400.0],
            # ols_* / values_projection_* / values_mtd_* / values_blend_* は無い
        }
        try:
            _deadjust_display_series(series, self.DATES)
        except Exception as exc:  # pragma: no cover
            self.fail(f"unexpected exception: {exc}")
        self.assertNotIn("ols_gairai", series)
        self.assertNotIn("values_blend_total", series)
        self.assertAlmostEqual(series["values_gairai"][0], round(100.0 / UPLIFT_GAIRAI, 2), places=2)

    def test_ols_series_processed(self):
        series = self._full_series()
        _deadjust_display_series(series, self.DATES)
        expected_g0 = round(10.0 / UPLIFT_GAIRAI, 2)
        expected_n0 = round(100.0 / UPLIFT_NYUIN, 2)
        self.assertAlmostEqual(series["ols_gairai"][0], expected_g0, places=2)
        self.assertAlmostEqual(series["ols_nyuin"][0], expected_n0, places=2)
        self.assertAlmostEqual(series["ols_total"][0], round(expected_g0 + expected_n0, 2), places=2)

    def test_projection_series_processed(self):
        series = self._full_series()
        _deadjust_display_series(series, self.DATES)
        expected_g0 = round(11.0 / UPLIFT_GAIRAI, 2)
        expected_n0 = round(101.0 / UPLIFT_NYUIN, 2)
        self.assertAlmostEqual(series["values_projection_gairai"][0], expected_g0, places=2)
        self.assertAlmostEqual(series["values_projection_nyuin"][0], expected_n0, places=2)
        self.assertAlmostEqual(series["values_projection_total"][0], round(expected_g0 + expected_n0, 2), places=2)

    def test_mtd_series_processed(self):
        series = self._full_series()
        _deadjust_display_series(series, self.DATES)
        expected_g0 = round(12.0 / UPLIFT_GAIRAI, 2)
        expected_n0 = round(102.0 / UPLIFT_NYUIN, 2)
        self.assertAlmostEqual(series["values_mtd_gairai"][0], expected_g0, places=2)
        self.assertAlmostEqual(series["values_mtd_nyuin"][0], expected_n0, places=2)
        self.assertAlmostEqual(series["values_mtd_total"][0], round(expected_g0 + expected_n0, 2), places=2)

    def test_blend_series_processed(self):
        series = self._full_series()
        _deadjust_display_series(series, self.DATES)
        expected_g0 = round(13.0 / UPLIFT_GAIRAI, 2)
        expected_n0 = round(103.0 / UPLIFT_NYUIN, 2)
        self.assertAlmostEqual(series["values_blend_gairai"][0], expected_g0, places=2)
        self.assertAlmostEqual(series["values_blend_nyuin"][0], expected_n0, places=2)
        self.assertAlmostEqual(series["values_blend_total"][0], round(expected_g0 + expected_n0, 2), places=2)


class TestPrevYearRevisionAdjust(unittest.TestCase):
    """dept_report.py の粗利チャート前年線・改定換算（memory: project_dept_report_pdf）。

    対象: _prev_needs_revision_adjust / _revision_adjusted_prev / _unit_profit_series
    """

    def test_needs_adjust_post_revision_month_true(self):
        self.assertTrue(_prev_needs_revision_adjust("2026-06-01"))

    def test_needs_adjust_pre_revision_month_false(self):
        # 当年月自体が改定前 → 前年も改定前で物差しが揃っている
        self.assertFalse(_prev_needs_revision_adjust("2026-05-01"))

    def test_needs_adjust_next_year_may_true(self):
        # m=2027-05 の前年同月=2026-05は改定前 → 換算が要る
        self.assertTrue(_prev_needs_revision_adjust("2027-05-01"))

    def test_needs_adjust_next_year_june_false_expired(self):
        # m=2027-06 の前年同月=2026-06は改定後 → 物差しが揃い期限切れ
        self.assertFalse(_prev_needs_revision_adjust("2027-06-01"))

    def test_needs_adjust_december_true(self):
        self.assertTrue(_prev_needs_revision_adjust("2026-12-01"))

    def test_adjusted_prev_uses_breakdown(self):
        pm = pd.Timestamp("2025-06-01")
        gmap = {pm: 10000.0}
        nmap = {pm: 20000.0}
        out = _revision_adjusted_prev(gmap, nmap, pm)
        expected = round((10000.0 * UPLIFT_GAIRAI + 20000.0 * UPLIFT_NYUIN) / 1000, 1)
        self.assertAlmostEqual(out, expected, places=6)

    def test_adjusted_prev_missing_breakdown_returns_none(self):
        pm = pd.Timestamp("2025-06-01")
        self.assertIsNone(_revision_adjusted_prev({}, {}, pm))
        self.assertIsNone(_revision_adjusted_prev({pm: 10000.0}, {}, pm))

    def test_adjusted_prev_nan_returns_none(self):
        pm = pd.Timestamp("2025-06-01")
        gmap = {pm: float("nan")}
        nmap = {pm: 20000.0}
        self.assertIsNone(_revision_adjusted_prev(gmap, nmap, pm))


def _synthetic_profit_monthly(with_breakdown: bool = True) -> pd.DataFrame:
    """1診療科・2025-04〜2026-06の月次 profit_monthly（合成データのみ）。

    粗利=30000（千円）一定・目標=25000・達成率=95.0 で、内訳(外来粗利/入院粗利)は
    with_breakdown=True のときだけ列を持たせる（全月 5000/15000 一定）。
    """
    months = pd.date_range("2025-04-01", "2026-06-01", freq="MS")
    rows = []
    for m in months:
        row = {"診療科名": "内科A", "月": m, "粗利": 30000.0,
              "月次目標": 25000.0, "達成率": 95.0}
        if with_breakdown:
            row["外来粗利"] = 5000.0
            row["入院粗利"] = 15000.0
        rows.append(row)
    return pd.DataFrame(rows)


class TestUnitProfitSeriesRevisionAdjust(unittest.TestCase):
    """_unit_profit_series の結合テスト（見込みスロットは estimators=None, adm=None で作らせない）。"""

    def setUp(self):
        self.base_date = pd.Timestamp("2026-06-15")

    def test_2026_06_prev_is_adjusted(self):
        pm_data = _synthetic_profit_monthly(with_breakdown=True)
        out = _unit_profit_series(pm_data, "内科A", self.base_date,
                                  estimators=None, adm=None, surg=None)
        self.assertTrue(out["prev_adjusted"])
        idx = out["dates"].index("6月")
        expected = round((5000.0 * UPLIFT_GAIRAI + 15000.0 * UPLIFT_NYUIN) / 1000, 1)
        self.assertAlmostEqual(out["prev"][idx], expected, places=6)

    def test_pre_revision_month_prev_is_raw(self):
        pm_data = _synthetic_profit_monthly(with_breakdown=True)
        out = _unit_profit_series(pm_data, "内科A", self.base_date,
                                  estimators=None, adm=None, surg=None)
        idx = out["dates"].index("5月")
        self.assertAlmostEqual(out["prev"][idx], 30.0, places=6)

    def test_missing_breakdown_columns_prev_raw_and_not_adjusted(self):
        pm_data = _synthetic_profit_monthly(with_breakdown=False)
        out = _unit_profit_series(pm_data, "内科A", self.base_date,
                                  estimators=None, adm=None, surg=None)
        self.assertFalse(out["prev_adjusted"])
        idx = out["dates"].index("6月")
        self.assertAlmostEqual(out["prev"][idx], 30.0, places=6)


class TestProjectDeptMonthendIntegration(unittest.TestCase):
    """project_dept_monthend が recency ループを通っても月末見込みを返せること。

    リグレッション: recency ループ内のローカル変数が外側の pred（当月見込み・千円）を
    dict で上書きし、`pred * factor` が TypeError になった。ヘルパー単体のテストでは
    捕まらず make reports だけが落ちたため、結合パスをここで固定する。
    """

    DEPT = "外科A"
    # 月ごとに在院・粗利を振る。定数だと分散0で r2=None になり、推計器の品質ゲート
    # （r2 None → 見込みを出さない）に阻まれて結合パスを通れない。
    MONTHS = ["2025-12-01", "2026-01-01", "2026-02-01",
              "2026-03-01", "2026-04-01", "2026-05-01"]
    CENSUS = {"2025-12-01": 40.0, "2026-01-01": 46.0, "2026-02-01": 52.0,
              "2026-03-01": 44.0, "2026-04-01": 58.0, "2026-05-01": 50.0}

    def setUp(self):
        adm_parts, surg_parts, pb_rows, pm_rows = [], [], [], []
        for m in self.MONTHS + ["2026-06-01"]:
            ms = pd.Timestamp(m)
            me = ms + pd.offsets.MonthEnd(0)
            cen = self.CENSUS.get(m, 50.0)
            adm_parts.append(_const_daily_adm(self.DEPT, ms, me,
                                              census=cen, new_adm=cen / 10.0))
            surg_parts.append(_const_daily_surg(self.DEPT, ms, me,
                                               n_nyuin=2, n_gairai=1))
            if m in self.CENSUS:   # 確報は 2026-05 まで
                nyuin = cen * 40.0
                gairai = cen * 10.0
                pb_rows += [
                    {"診療科名": self.DEPT, "月": ms, "区分": "外来", "粗利": gairai},
                    {"診療科名": self.DEPT, "月": ms, "区分": "入院", "粗利": nyuin},
                ]
                pm_rows.append({"診療科名": self.DEPT, "月": ms,
                                "粗利": gairai + nyuin,
                                "外来粗利": gairai, "入院粗利": nyuin})
        self.adm = pd.concat(adm_parts, ignore_index=True)
        self.surg = pd.concat(surg_parts, ignore_index=True)
        self.pb = pd.DataFrame(pb_rows)
        self.pm = pd.DataFrame(pm_rows)
        self.est = fit_profit_estimators(self.pb, self.adm, self.surg)

    def _run(self, profit_monthly):
        return project_dept_monthend(self.est, self.adm, self.surg,
                                     pd.Timestamp("2026-06-15"), self.DEPT,
                                     profit_monthly=profit_monthly)

    def test_returns_numeric_value_with_breakdown(self):
        out = self._run(self.pm)
        self.assertIsNotNone(out)
        self.assertIsInstance(out["value"], float)
        self.assertGreater(out["value"], 0)
        self.assertIsInstance(out["factor"], float)

    def test_returns_numeric_value_without_breakdown(self):
        # 内訳列なし → _recency_actual_adjusted の blend fallback を通る
        out = self._run(self.pm.drop(columns=["外来粗利", "入院粗利"]))
        self.assertIsNotNone(out)
        self.assertIsInstance(out["value"], float)
        self.assertGreater(out["value"], 0)

    def test_recency_loop_does_not_clobber_projection(self):
        # profit_monthly なし（recency ループを通らない）と比べ、factor 以外の
        # 構造が壊れていないこと＝ループが外側 pred を破壊していないこと
        with_pm = self._run(self.pm)
        without_pm = self._run(None)
        self.assertIsNotNone(without_pm)
        self.assertAlmostEqual(without_pm["factor"], 1.0, places=9)
        # factor で割り戻せば同じ当月見込みに戻る
        self.assertAlmostEqual(with_pm["value"] / with_pm["factor"],
                               without_pm["value"], places=1)


if __name__ == "__main__":
    unittest.main()
