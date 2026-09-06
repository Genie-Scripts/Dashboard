"""build_kpi_summary(): A1(先週の確定+今週ここまで)/A7(年度進捗の営業日按分)/
A9(前年比チップの窓一致) で追加・変更したキーの回帰テスト（訴求力強化 Phase1 バッチ1a）。

実物の build_kpi_summary() を、単純化した合成データ（在院=平日/休日で固定値、
新入院=毎日固定値、全麻=営業日のみ固定件数）に対して直接呼び出し、期待値を
本テスト内で生データから独立に計算して突合する（モックは使わない）。

対象キー:
  - inpatient_fy_rate / operation_fy_rate / admission_fy_rate（営業日按分に統一）
  - fy_biz_days_elapsed / admission_fy_actual_total / admission_fy_biz_target
  - operation_prev_7d_biz_avg / inpatient_prev_7d_avg_wd / inpatient_prev_7d_avg_hd
  - inpatient/admission/operation の *_last_week_* / *_this_week_*（A1）

基準日: 2026-09-03（木・通常週。test_f1_operation_7d.py と共通の基準日）。
年度: 2026-04-01 起点（fy_biz_days_elapsed=106、暦日156）。
前年同期窓（364日前を終端とする7暦日窓 = 2025-08-29〜09-04）だけ、在院・全麻の
値を意図的に別値（700/650人・30件）にして、他の窓と取り違えていないことを検出する。

実行: リポジトリルートで
    OMLX_BASE_URL=http://127.0.0.1:9 .venv/bin/python -m pytest tests/test_kpi_summary_period_fy_yoy.py -q
"""
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.metrics import build_kpi_summary  # noqa: E402
from app.lib.config import (  # noqa: E402
    operational_days_between,
    is_operational_day,
    TARGET_INPATIENT_ALLDAY,
    TARGET_ADMISSION_WEEKLY,
    TARGET_GA_DAILY,
)

BASE = pd.Timestamp("2026-09-03")          # 木曜・通常週
FY_START = pd.Timestamp("2026-04-01")
RANGE_START = pd.Timestamp("2025-07-20")   # op_4w_prev_avg 等が参照する最古窓より前
MARK_START = BASE - pd.Timedelta(days=370)  # 2025-08-29（前年同期7日窓の始点）
MARK_END = BASE - pd.Timedelta(days=364)    # 2025-09-04（同終点）

INP_WD, INP_HD = 600, 550          # 既定の在院値（平日/休日）
INP_WD_MARK, INP_HD_MARK = 700, 650  # 前年同期窓だけ別値（窓の取り違え検出用）
NADM_DAILY = 50
GA_DAILY = TARGET_GA_DAILY          # 21件/営業日（既定）
GA_DAILY_MARK = 30                  # 前年同期窓だけ別値


def _build_fixture():
    days = pd.date_range(RANGE_START, BASE, freq="D")
    adm_rows = []
    surg_rows = []
    for d in days:
        biz = is_operational_day(d)
        marked = MARK_START <= d <= MARK_END
        if marked:
            inp_val = INP_WD_MARK if biz else INP_HD_MARK
        else:
            inp_val = INP_WD if biz else INP_HD
        adm_rows.append({
            "日付": d, "在院患者数": inp_val,
            "新入院患者数": NADM_DAILY, "新入院患者数_病棟": NADM_DAILY,
            "緊急入院患者数": 5, "退院合計": 45,
            "転入患者数": 2, "転出患者数": 2, "出入り負荷": 10,
            "科_表示": True, "病棟_表示": True,
            "診療科名": "内科", "病棟コード": "01A", "平日": biz,
        })
        if biz:
            ga_count = GA_DAILY_MARK if marked else GA_DAILY
            for _ in range(ga_count):
                surg_rows.append({
                    "手術実施日": d, "全麻": True, "科_表示": True,
                    "実施診療科": "外科", "術数対象": True,
                })
    adm = pd.DataFrame(adm_rows)
    surg = pd.DataFrame(surg_rows)
    return adm, surg


class BuildKpiSummaryFyYoyWeekWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        adm, surg = _build_fixture()
        cls.kpi = build_kpi_summary(adm, surg, BASE, {}, {})

    # ── A7: 年度進捗（営業日按分） ──

    def test_fy_biz_days_elapsed(self):
        expected = operational_days_between(FY_START, BASE)
        self.assertEqual(expected, 106)
        self.assertEqual(self.kpi["fy_biz_days_elapsed"], expected)

    def test_inpatient_fy_rate_uses_calendar_weighted_avg_vs_allday_target(self):
        # 在院はストック規約どおり暦日平均のまま（追加按分なし）
        n_total = (BASE - FY_START).days + 1
        n_biz = operational_days_between(FY_START, BASE)
        n_non = n_total - n_biz
        fy_avg_inp = round((n_biz * INP_WD + n_non * INP_HD) / n_total, 1)
        self.assertAlmostEqual(self.kpi["inpatient_fy_avg"], fy_avg_inp)
        expected_rate = round(fy_avg_inp / TARGET_INPATIENT_ALLDAY * 100, 1)
        self.assertAlmostEqual(self.kpi["inpatient_fy_rate"], expected_rate)

    def test_admission_fy_rate_is_business_day_prorated(self):
        n_total = (BASE - FY_START).days + 1
        n_biz = operational_days_between(FY_START, BASE)
        fy_nadm_sum = NADM_DAILY * n_total
        expected_target = round(TARGET_ADMISSION_WEEKLY / 5 * n_biz, 1)
        expected_rate = round(fy_nadm_sum / expected_target * 100, 1)
        self.assertEqual(self.kpi["admission_fy_actual_total"], fy_nadm_sum)
        self.assertAlmostEqual(self.kpi["admission_fy_biz_target"], expected_target)
        self.assertAlmostEqual(self.kpi["admission_fy_rate"], expected_rate)
        # 対照: 旧・暦日按分(fy_weeks)なら同じ入力でも異なる値になること
        # （A7是正の主旨＝暦日按分と営業日按分は一致しない）。
        fy_weeks_calendar = max(n_total / 7, 1)
        old_style_rate = round((fy_nadm_sum / fy_weeks_calendar) / TARGET_ADMISSION_WEEKLY * 100, 1)
        self.assertNotAlmostEqual(self.kpi["admission_fy_rate"], old_style_rate)

    def test_operation_fy_rate_no_extra_discount_needed(self):
        # fy_biz_avgは既に営業日tail平均のため、目標との比がそのまま達成率になる
        expected = round(self.kpi["operation_fy_avg"] / TARGET_GA_DAILY * 100, 1)
        self.assertAlmostEqual(self.kpi["operation_fy_rate"], expected)
        self.assertAlmostEqual(self.kpi["operation_fy_rate"], 100.0)  # GA_DAILY=目標そのもの

    # ── A9: 前年比チップの窓一致（364日前を終端とする7暦日窓） ──

    def test_operation_prev_7d_biz_avg_uses_correct_window_not_a_neighboring_one(self):
        # マーカー窓(2025-08-29〜09-04)だけ30件/日にしてあるため、隣接窓(21件/日)と
        # 取り違えていれば21.0が返り本テストで検出できる。
        self.assertAlmostEqual(self.kpi["operation_prev_7d_biz_avg"], float(GA_DAILY_MARK))
        self.assertNotAlmostEqual(self.kpi["operation_prev_7d_biz_avg"], float(GA_DAILY))

    def test_inpatient_prev_7d_avg_wd_hd_match_marker_window(self):
        self.assertAlmostEqual(self.kpi["inpatient_prev_7d_avg_wd"], float(INP_WD_MARK))
        self.assertAlmostEqual(self.kpi["inpatient_prev_7d_avg_hd"], float(INP_HD_MARK))

    # ── A1: 先週の確定＋今週ここまで（基準日=木曜） ──

    def test_last_week_range_is_prior_full_monday_to_sunday(self):
        self.assertEqual(self.kpi["inpatient_last_week_range"], "8/24〜8/30")
        self.assertEqual(self.kpi["admission_last_week_range"], "8/24〜8/30")

    def test_inpatient_last_week_avg(self):
        # 2026-08-24(月)〜08-30(日): 平日5日@600・休日2日@550
        expected = round((5 * INP_WD + 2 * INP_HD) / 7, 1)
        self.assertAlmostEqual(self.kpi["inpatient_last_week_avg"], expected)

    def test_admission_last_week_total(self):
        self.assertEqual(self.kpi["admission_last_week_total"], NADM_DAILY * 7)

    def test_this_week_range_and_biz_days_thursday_base(self):
        # 2026-08-31(月)〜09-03(木・基準日)＝営業日4
        self.assertEqual(self.kpi["inpatient_this_week_range"], "8/31〜9/3")
        self.assertEqual(self.kpi["inpatient_this_week_biz_days"], 4)
        self.assertEqual(self.kpi["admission_this_week_biz_days"], 4)
        self.assertEqual(self.kpi["operation_this_week_biz_days"], 4)

    def test_admission_this_week_partial_target_and_rate(self):
        biz_days = 4
        expected_target = round(TARGET_ADMISSION_WEEKLY / 5 * biz_days, 1)
        expected_total = NADM_DAILY * 4
        expected_rate = round(expected_total / expected_target * 100, 1)
        self.assertEqual(self.kpi["admission_this_week_total"], expected_total)
        self.assertAlmostEqual(self.kpi["admission_this_week_target"], expected_target)
        self.assertAlmostEqual(self.kpi["admission_this_week_rate"], expected_rate)

    def test_operation_this_week_partial_target_and_rate(self):
        biz_days = 4
        expected_target = round((GA_DAILY * 5) / 5 * biz_days, 1)
        expected_total = GA_DAILY * biz_days
        self.assertEqual(self.kpi["operation_this_week_total"], expected_total)
        self.assertAlmostEqual(self.kpi["operation_this_week_target"], expected_target)
        self.assertAlmostEqual(self.kpi["operation_this_week_rate"], 100.0)


class BuildKpiSummarySundayBaseDateHasNoThisWeekTest(unittest.TestCase):
    """裁定: 月曜ビュー(基準日=日曜)は「今週ここまで」を出さない(None)。"""

    @classmethod
    def setUpClass(cls):
        sunday_base = pd.Timestamp("2026-08-30")
        adm, surg = _build_fixture()
        # フィクスチャは2026-09-03までしか無いため、日曜基準日を別途生成する。
        adm2, surg2 = _build_fixture()
        cls.kpi = build_kpi_summary(adm2, surg2, sunday_base, {}, {})

    def test_this_week_keys_are_none(self):
        for k in ("inpatient_this_week_avg", "inpatient_this_week_range",
                 "admission_this_week_total", "admission_this_week_range",
                 "operation_this_week_total", "operation_this_week_range",
                 "inpatient_this_week_biz_days", "admission_this_week_target",
                 "operation_this_week_rate"):
            self.assertIsNone(self.kpi[k], f"{k} が None でない（日曜=月曜ビューでは今週ここまでを出さない）")

    def test_last_week_still_present_and_equals_current_week(self):
        # 日曜基準日は「その週(月〜日)」がそのまま先週の確定になる
        self.assertEqual(self.kpi["admission_last_week_range"], "8/24〜8/30")


class TestPartialWeekTargetBoundaryOnTuesday(unittest.TestCase):
    """裁定3: 火曜（直近7暦日窓の営業日=1。月曜が成人の日で休診）でも
    按分目標比を出す（目標=週目標/5×1）。"""

    @classmethod
    def setUpClass(cls):
        tuesday_base = pd.Timestamp("2026-01-13")   # 月(01-12・成人の日)明けの火曜
        adm, surg = _build_fixture()
        cls.kpi = build_kpi_summary(adm, surg, tuesday_base, {}, {})

    def test_biz_days_is_one(self):
        self.assertEqual(self.kpi["admission_this_week_biz_days"], 1)

    def test_admission_target_is_weekly_target_over_five(self):
        expected = round(TARGET_ADMISSION_WEEKLY / 5 * 1, 1)
        self.assertAlmostEqual(self.kpi["admission_this_week_target"], expected)
        self.assertAlmostEqual(self.kpi["admission_this_week_target"], round(TARGET_ADMISSION_WEEKLY / 5, 1))

    def test_admission_this_week_total_counts_both_calendar_days(self):
        # 「今週ここまで」の実績は暦日ベースの累計（休診日にも入院実績はあり得る）。
        # 按分目標だけが営業日数(=1)を分母にする。
        self.assertEqual(self.kpi["admission_this_week_total"], NADM_DAILY * 2)

    def test_admission_this_week_rate(self):
        expected_target = round(TARGET_ADMISSION_WEEKLY / 5 * 1, 1)
        expected_rate = round((NADM_DAILY * 2) / expected_target * 100, 1)
        self.assertAlmostEqual(self.kpi["admission_this_week_rate"], expected_rate)


if __name__ == "__main__":
    unittest.main()
