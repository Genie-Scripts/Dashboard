"""month_projection.py（A8: 必要ペース・在院換算ヘルパー）の回帰テスト。

対象:
  - build_month_projection_payload の operation タイル: needed_total/needed_pace
    （全麻・残り営業日で目標到達に必要な件数/日ペース。達成見込みならNone）
  - build_month_projection_payload の admission タイル: needed_pace
    （新入院・月末までに必要な週ペース。達成見込みならNone）
  - _alos_28d: 28日在院平均÷日平均新入院（新入院0件でNone）

dept指定モードで小さな週目標を与え、手計算で追える値にして検証する
（病院全体の実目標(TARGET_GA_DAILY等)は値が大きく手計算に不向きなため）。

実行: リポジトリルートで
    OMLX_BASE_URL=http://127.0.0.1:9 .venv/bin/python -m pytest -q tests/test_month_projection_pace.py
"""
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.month_projection import (  # noqa: E402
    build_month_projection_payload,
    _alos_28d,
)

DEPT = "A科"
BASE_DATE = pd.Timestamp("2026-06-01")  # 月初(6月・biz_days_total=22・cal_days_total=30)
WINDOW_START = pd.Timestamp("2026-05-03")  # base_date - 29日（win30窓の下限）


def _adm_df(admission_on_base: float) -> pd.DataFrame:
    """WINDOW_START〜BASE_DATE の日次在院データ。新入院はbase_date当日だけ指定値、
    他日は5（MTD窓は月初=base_date当日のみなので、admission_on_baseだけがMTDに効く）。"""
    dates = pd.date_range(WINDOW_START, BASE_DATE, freq="D")
    adm_counts = [(admission_on_base if d == BASE_DATE else 5.0) for d in dates]
    return pd.DataFrame({
        "日付": dates,
        "在院患者数": [100.0] * len(dates),
        "新入院患者数": adm_counts,
        "診療科名": [DEPT] * len(dates),
    })


def _surg_df(surgery_count_on_base: int) -> pd.DataFrame:
    """base_date当日に指定件数の手術（術数対象=True）を積む。他日は0件。"""
    if surgery_count_on_base <= 0:
        return pd.DataFrame({
            "手術実施日": pd.Series([], dtype="datetime64[ns]"),
            "実施診療科": pd.Series([], dtype=str),
            "全麻": pd.Series([], dtype=bool),
            "術数対象": pd.Series([], dtype=bool),
        })
    dates = [BASE_DATE] * surgery_count_on_base
    return pd.DataFrame({
        "手術実施日": dates,
        "実施診療科": [DEPT] * surgery_count_on_base,
        "全麻": [True] * surgery_count_on_base,
        "術数対象": [True] * surgery_count_on_base,
    })


def _payload(admission_on_base: float, surgery_count_on_base: int) -> dict:
    return build_month_projection_payload(
        adm=_adm_df(admission_on_base),
        surg=_surg_df(surgery_count_on_base),
        profit_monthly=None,
        profit_hybrid_meta=None,
        profit_hybrid_hospital_series=None,
        base_date=BASE_DATE,
        dept=DEPT,
        dept_inpatient_target=100.0,
        dept_admission_weekly=14.0,   # adm_target = 14/7*30 = 60.0
        dept_operation_weekly=10.0,   # ga_target = 10/5*22 = 44.0
    )


class OperationNeededPaceTest(unittest.TestCase):
    def test_not_achieved_yields_positive_needed_total_and_pace(self):
        payload = _payload(admission_on_base=5.0, surgery_count_on_base=0)
        op = payload["operation"]
        # ga_target=44.0, ga_mtd=0 (base_date当日に手術0件) → remaining=44.0
        self.assertEqual(op["needed_total"], 44.0)
        # biz_days_remaining = 22 - 1(6/1のみ経過) = 21
        self.assertEqual(op["needed_pace"], round(44.0 / 21, 1))

    def test_achieved_yields_none(self):
        # ga_mtd=50 > ga_target=44.0 → 達成見込み
        payload = _payload(admission_on_base=5.0, surgery_count_on_base=50)
        op = payload["operation"]
        self.assertIsNone(op["needed_total"])
        self.assertIsNone(op["needed_pace"])

    def test_exact_boundary_zero_remaining_yields_none(self):
        # ga_mtd=44 == ga_target=44.0 → remaining=0（>0でないためNone、達成扱い）
        payload = _payload(admission_on_base=5.0, surgery_count_on_base=44)
        op = payload["operation"]
        self.assertIsNone(op["needed_total"])
        self.assertIsNone(op["needed_pace"])


class AdmissionNeededPaceTest(unittest.TestCase):
    def test_not_achieved_yields_positive_needed_pace(self):
        payload = _payload(admission_on_base=5.0, surgery_count_on_base=0)
        adm = payload["admission"]
        # adm_target=60.0, adm_mtd=5 → remaining=55, cal_days_remaining=29
        self.assertEqual(adm["needed_pace"], round(55.0 / (29 / 7), 1))

    def test_achieved_yields_none(self):
        # adm_mtd=70 > adm_target=60.0 → 達成見込み
        payload = _payload(admission_on_base=70.0, surgery_count_on_base=0)
        adm = payload["admission"]
        self.assertIsNone(adm["needed_pace"])

    def test_exact_boundary_zero_remaining_yields_none(self):
        # adm_mtd=60 == adm_target=60.0 → remaining=0 → None
        payload = _payload(admission_on_base=60.0, surgery_count_on_base=0)
        adm = payload["admission"]
        self.assertIsNone(adm["needed_pace"])


class Alos28dTest(unittest.TestCase):
    def test_zero_admissions_yields_none(self):
        dates = pd.date_range("2026-05-05", "2026-06-01", freq="D")  # 28日
        adm = pd.DataFrame({
            "日付": dates,
            "在院患者数": [200.0] * len(dates),
            "新入院患者数": [0.0] * len(dates),
        })
        self.assertIsNone(_alos_28d(adm, pd.Timestamp("2026-06-01")))

    def test_known_ratio_computes_alos(self):
        dates = pd.date_range("2026-05-05", "2026-06-01", freq="D")  # 28日
        adm = pd.DataFrame({
            "日付": dates,
            "在院患者数": [200.0] * len(dates),
            "新入院患者数": [10.0] * len(dates),
        })
        # census_avg=200.0, adm_avg=(10*28)/28=10.0 → ALOS=20.0
        self.assertEqual(_alos_28d(adm, pd.Timestamp("2026-06-01")), 20.0)


if __name__ == "__main__":
    unittest.main()
