"""hospital_summary.py: 粗利予測達成率（P4診療科テーブル）の推計器切替の回帰テスト。

背景（spec/改修プラン_粗利の単位あたり指標.md P3）: build_hospital_report.py::main が
build_summary_context に profit_projection を渡していなかったため、実績まとめPDFのP4
だけダッシュボードと別推計器（OLSフォールバック）になっていた。本テストは
_dept_profit_proj / build_summary_context が profit_projection の有無で正しい経路
（渡した値 優先 → 無ければ従来のOLSフォールバック）を通ることを固定する。

実データ・ファイルI/O・常駐サーバには依存しない（密閉）。

実行: リポジトリルートで
    python -m pytest tests/test_hospital_summary_profit_projection_wiring.py -q
    python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib import hospital_summary as hs  # noqa: E402
from app.lib import profit_estimate  # noqa: E402

BASE_DATE = pd.Timestamp("2026-06-15")
DEPT = "内科"          # _dept_profit_proj 単体テスト用（表示対象科であるかは無関係）
DISPLAY_DEPT = "循環器内科"  # build_summary_context 経由テスト用（NADM_DISPLAY_DEPTS所属の実在科）


def _profit_monthly(tgt=1000.0, dept=DEPT):
    """診療科=dept・base_date所属月の月次目標のみ（単位=千円。/1000で百万円換算）。"""
    return pd.DataFrame([
        {"診療科名": dept, "月": BASE_DATE.to_period("M").to_timestamp(), "月次目標": tgt},
    ])


class DeptProfitProjPrefersProjectionTest(unittest.TestCase):
    """profit_projection に該当科の見込みがあれば、それを優先しOLSは呼ばない。"""

    def test_uses_projection_value_and_skips_ols(self):
        profit_projection = {"dept_million": {DEPT: 42.0}}
        with mock.patch.object(profit_estimate, "project_dept_monthend",
                               side_effect=AssertionError("OLSフォールバックが呼ばれた")):
            result = hs._dept_profit_proj(
                _profit_monthly(tgt=1000.0), estimators={"dummy": True},
                adm=pd.DataFrame(), surg=pd.DataFrame(), base_date=BASE_DATE, dept=DEPT,
                profit_projection=profit_projection)
        self.assertIsNotNone(result)
        self.assertEqual(result["proj"], 42.0)
        self.assertAlmostEqual(result["rate"], 42.0 / 1.0 * 100)  # tgt_m = 1000/1000 = 1.0百万円

    def test_projection_wins_over_ols_even_when_estimators_present(self):
        """OLS推計器が使える状態でも profit_projection があればそちらを使う。"""
        profit_projection = {"dept_million": {DEPT: 7.5}}
        with mock.patch.object(profit_estimate, "project_dept_monthend",
                               return_value={"value": 999.0}) as ols:
            result = hs._dept_profit_proj(
                _profit_monthly(tgt=1000.0), estimators={DEPT: object()},
                adm=pd.DataFrame(), surg=pd.DataFrame(), base_date=BASE_DATE, dept=DEPT,
                profit_projection=profit_projection)
            ols.assert_not_called()
        self.assertEqual(result["proj"], 7.5)


class DeptProfitProjFallsBackToOlsTest(unittest.TestCase):
    """profit_projection 未指定、または該当科が無い場合は従来のOLSへフォールバック（後方互換）。"""

    def test_no_profit_projection_arg_falls_back_to_ols(self):
        """引数を渡さない（既定 None）＝旧来の呼び出しと完全に同じ挙動。"""
        with mock.patch.object(profit_estimate, "project_dept_monthend",
                               return_value={"value": 12.3}) as ols:
            result = hs._dept_profit_proj(
                _profit_monthly(tgt=1000.0), estimators={DEPT: object()},
                adm=pd.DataFrame(), surg=pd.DataFrame(), base_date=BASE_DATE, dept=DEPT)
            ols.assert_called_once()
        self.assertEqual(result["proj"], 12.3)

    def test_profit_projection_without_dept_entry_falls_back_to_ols(self):
        """profit_projection はあるが dept_million に該当科が無い（allowlist外等）→ OLSへ。"""
        profit_projection = {"dept_million": {"別科": 99.0}}
        with mock.patch.object(profit_estimate, "project_dept_monthend",
                               return_value={"value": 5.0}) as ols:
            result = hs._dept_profit_proj(
                _profit_monthly(tgt=1000.0), estimators={DEPT: object()},
                adm=pd.DataFrame(), surg=pd.DataFrame(), base_date=BASE_DATE, dept=DEPT,
                profit_projection=profit_projection)
            ols.assert_called_once()
        self.assertEqual(result["proj"], 5.0)

    def test_empty_profit_projection_dict_falls_back_without_error(self):
        """profit_projection={} のような退化形でも例外にならずOLSへ（fail-soft）。"""
        with mock.patch.object(profit_estimate, "project_dept_monthend",
                               return_value=None):
            result = hs._dept_profit_proj(
                _profit_monthly(tgt=1000.0), estimators={DEPT: object()},
                adm=pd.DataFrame(), surg=pd.DataFrame(), base_date=BASE_DATE, dept=DEPT,
                profit_projection={})
        self.assertIsNone(result)  # OLSも推計不可 → 表は「—」（例外にはならない）


class DeptProfitProjFailSoftTest(unittest.TestCase):
    """粗利データが無い/欠けている環境でも例外にならず None を返す（従来の縮退挙動を維持）。"""

    def test_no_profit_monthly_returns_none_even_with_projection(self):
        result = hs._dept_profit_proj(
            None, estimators={}, adm=pd.DataFrame(), surg=pd.DataFrame(),
            base_date=BASE_DATE, dept=DEPT,
            profit_projection={"dept_million": {DEPT: 10.0}})
        self.assertIsNone(result)

    def test_empty_profit_monthly_returns_none(self):
        result = hs._dept_profit_proj(
            pd.DataFrame(), estimators={}, adm=pd.DataFrame(), surg=pd.DataFrame(),
            base_date=BASE_DATE, dept=DEPT, profit_projection=None)
        self.assertIsNone(result)

    def test_no_estimators_and_no_projection_returns_none_without_error(self):
        """profit_projection 無し・estimators も空（フィット失敗環境）→ 例外にならず None。"""
        result = hs._dept_profit_proj(
            _profit_monthly(tgt=1000.0), estimators={}, adm=pd.DataFrame(), surg=pd.DataFrame(),
            base_date=BASE_DATE, dept=DEPT, profit_projection=None)
        self.assertIsNone(result)


# ════════════════════════════════════════════════════════════
# build_summary_context 経由（実績まとめPDF全体の組立）での配線確認
# ════════════════════════════════════════════════════════════
TARGETS = {
    "new_admission": {"dept": {}, "ward": {}},
    "inpatient": {"dept": {}, "ward": {}, "ward_beds": {}},
}
SURG_TARGETS = {}


def _run_build_summary_context(profit_projection):
    """build_summary_context の重い依存（本テストと無関係）をフェイクに差し替え、
    profit_proj 配線だけを検証する（test_hospital_summary_surg_target_discount.py と同じ手法）。"""
    patches = [
        mock.patch.object(hs, "build_hero_text", lambda *a, **k: {"headline": "", "body": "", "chips": []}),
        mock.patch.object(hs.metrics, "build_kpi_summary", lambda *a, **k: {}),
        mock.patch.object(hs, "_ma_series", lambda *a, **k: {"dates": [], "cur": [], "prev": []}),
        mock.patch.object(hs, "_surg_series", lambda *a, **k: {"dates": [], "cur": [], "prev": []}),
        mock.patch.object(hs.metrics, "build_ward_ranking", lambda *a, **k: pd.DataFrame()),
        mock.patch.object(hs.metrics, "weekend_census_retention", lambda *a, **k: {"units": [], "total": {}}),
        mock.patch.object(hs, "_flow_7d", lambda *a, **k: {}),
        mock.patch.object(hs.metrics, "build_dept_ranking", lambda *a, **k: pd.DataFrame()),
        mock.patch.object(hs.metrics, "build_surgery_ranking", lambda *a, **k: pd.DataFrame()),
        mock.patch.object(hs.metrics, "discharge_dow_profile", lambda *a, **k: {"redistribution": None}),
        # fit_profit_estimators は profit_breakdown 無しでは呼ばれないが念のため固定。
        mock.patch.object(profit_estimate, "fit_profit_estimators", lambda *a, **k: {DISPLAY_DEPT: object()}),
        mock.patch.object(profit_estimate, "project_dept_monthend",
                          lambda *a, **k: {"value": 3.0}),  # OLSフォールバック時の既知値
    ]
    from contextlib import ExitStack
    with ExitStack() as st:
        for p in patches:
            st.enter_context(p)
        # profit_breakdown は非空であれば足りる（fit_profit_estimators 自体はモック済み）。
        # estimators を確実に非空にし、OLSフォールバック分岐（if not estimators: return None）
        # を素通りさせず project_dept_monthend まで到達させる。
        return hs.build_summary_context(
            pd.DataFrame(), pd.DataFrame(), TARGETS, SURG_TARGETS, BASE_DATE,
            profit_monthly=_profit_monthly(tgt=1000.0, dept=DISPLAY_DEPT),
            profit_breakdown=pd.DataFrame({"x": [1]}),
            profit_projection=profit_projection)


def _dept_row(ctx, dept=DISPLAY_DEPT):
    return next((r for r in ctx["dept_rows"] if r["name"] == dept), None)


class BuildSummaryContextWiringTest(unittest.TestCase):
    """build_hospital_report.py が profit_projection を渡すと、その値がP4に反映されること。"""

    def test_with_profit_projection_uses_projection_value(self):
        ctx = _run_build_summary_context({"dept_million": {DISPLAY_DEPT: 21.0}})
        row = _dept_row(ctx)
        self.assertIsNotNone(row)
        self.assertIsNotNone(row["profit_proj"])
        self.assertEqual(row["profit_proj"]["proj"], 21.0)  # OLSの固定値3.0ではない

    def test_without_profit_projection_falls_back_to_ols_fixed_value(self):
        """profit_projection 未指定（旧来のbuild_hospital_report呼び出し）は
        OLSフォールバック（project_dept_monthend）を通る＝後方互換。"""
        ctx = _run_build_summary_context(None)
        row = _dept_row(ctx)
        self.assertIsNotNone(row)
        self.assertIsNotNone(row["profit_proj"])
        self.assertEqual(row["profit_proj"]["proj"], 3.0)


if __name__ == "__main__":
    unittest.main()
