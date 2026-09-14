"""
profit_unit.py（粗利タブ「数量×単価」分解ブロック）のユニットテスト（合成データのみ）。

対象:
  - build_profit_unit_payload の decomp: 数量効果+単価効果 が Δ粗利実績に一致すること
    （丸め誤差 ±0.1百万円以内）
  - marginal（限界人日単価）の発散防止ガード（|Δ延患者数|<50 → None）
  - alos（平均在院日数の近似）が app.lib.metrics.alos_proxy を実際に再利用しているか
  - app.lib.profit.build_profit_monthly の 達成率_改定換算（2026-06診療報酬改定の
    換算後達成率・表示専用参考値）が改定前月では既存の達成率と一致すること

実行: リポジトリルートで
    .venv/bin/python -m pytest tests/test_profit_unit.py -q
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib import profit_unit as PU  # noqa: E402
from app.lib.profit_unit import build_profit_unit_payload, _MIN_DELTA_PATIENT_DAYS  # noqa: E402
from app.lib.metrics import alos_proxy as real_alos_proxy  # noqa: E402
from app.lib.profit import build_profit_monthly  # noqa: E402
from app.lib.month_projection import calendar_days_in_month, STD_CAL_DAYS_PER_MONTH  # noqa: E402


DEPT = "内科A"


def _adm_month(dept: str, month_start: str, census: float, disch: float) -> pd.DataFrame:
    """1か月ぶんの一定値 adm 行（1診療科・在院/退院合計 一定・表示対象）。"""
    ms = pd.Timestamp(month_start)
    me = ms + pd.offsets.MonthEnd(0)
    idx = pd.date_range(ms, me, freq="D")
    return pd.DataFrame({
        "日付": idx, "診療科名": dept, "在院患者数": census,
        "退院合計": disch, "科_表示": True,
    })


def _pb_month(dept: str, month_start: str, nyuin: float, gairai: float = 1000.0) -> pd.DataFrame:
    ms = pd.Timestamp(month_start)
    return pd.DataFrame([
        {"診療科名": dept, "月": ms, "区分": "入院", "粗利": nyuin},
        {"診療科名": dept, "月": ms, "区分": "外来", "粗利": gairai},
    ])


class TestDecompIdentity(unittest.TestCase):
    """数量効果 + 単価効果 = Δ粗利実績（前年同月比）が丸め誤差 ±0.1百万円以内で一致する。"""

    def setUp(self):
        months = pd.date_range("2024-08-01", "2026-08-01", freq="MS")
        adm_parts, pb_parts = [], []
        for i, m in enumerate(months):
            census = 480.0 + 3.0 * i + (10.0 if i % 5 == 0 else 0.0)   # 変動あり
            disch = max(1.0, census / 11.0)
            nyuin = 40000.0 + 137.0 * i + (900.0 if i % 4 == 0 else 0.0)
            adm_parts.append(_adm_month(DEPT, m, census, disch))
            pb_parts.append(_pb_month(DEPT, m, nyuin))
        self.adm = pd.concat(adm_parts, ignore_index=True)
        self.pb = pd.concat(pb_parts, ignore_index=True)

    def test_identity_holds_for_all_computed_months(self):
        out = build_profit_unit_payload(self.pb, self.adm,
                                        base_date=pd.Timestamp("2026-08-31"), months=13)
        decomp = out["global"]["decomp"]
        n_checked = 0
        for vol, price, actual in zip(decomp["volume_effect"], decomp["price_effect"],
                                      decomp["actual_delta"]):
            if vol is None:
                continue
            n_checked += 1
            # ★仕様の許容誤差は±0.1百万円「以内」。round()後の浮動小数表現誤差
            #   （例: 1.7 が内部的に 1.7000000000000002 になる）を吸収する微小epsilonを足す。
            self.assertAlmostEqual(vol + price, actual, delta=0.1 + 1e-9)
        self.assertGreater(n_checked, 0, "分解が1件も計算されていない（フィクスチャ不備）")

    def test_dept_block_identity_also_holds(self):
        out = build_profit_unit_payload(self.pb, self.adm,
                                        base_date=pd.Timestamp("2026-08-31"), months=13)
        decomp = out["by_dept"][DEPT]["decomp"]
        checked = [(v, p, a) for v, p, a in zip(decomp["volume_effect"], decomp["price_effect"],
                                                decomp["actual_delta"]) if v is not None]
        self.assertGreater(len(checked), 0)
        for vol, price, actual in checked:
            self.assertAlmostEqual(vol + price, actual, delta=0.1 + 1e-9)


class TestMarginalGuard(unittest.TestCase):
    """marginal（限界人日単価）は |Δ延患者数| < 50 人日なら None（発散防止）。"""

    def _payload(self, census_prev: float, census_curr: float):
        # 2026-07・2026-08 はともに31日 → 延患者数差 = (curr-prev)*31（整数の丁度で境界比較しやすい）
        adm = pd.concat([
            _adm_month(DEPT, "2026-07-01", census_prev, 40.0),
            _adm_month(DEPT, "2026-08-01", census_curr, 40.0),
        ], ignore_index=True)
        pb = pd.concat([
            _pb_month(DEPT, "2026-07-01", 45000.0),
            _pb_month(DEPT, "2026-08-01", 44000.0),
        ], ignore_index=True)
        out = build_profit_unit_payload(pb, adm, base_date=pd.Timestamp("2026-08-31"), months=2)
        return out["global"]["marginal"]

    def test_constant_guard_value_is_50(self):
        self.assertEqual(_MIN_DELTA_PATIENT_DAYS, 50.0)

    def test_below_threshold_returns_none(self):
        # Δ census = 1 → Δ延患者数 = 31 (< 50) → None
        m = self._payload(census_prev=500.0, census_curr=501.0)
        self.assertEqual(m["delta_patient_days"], 31.0)
        self.assertIsNone(m["value"])
        self.assertIn("算出していません", m["caption"])

    def test_above_threshold_returns_value(self):
        # Δ census = 2 → Δ延患者数 = 62 (>= 50) → 算出される
        m = self._payload(census_prev=500.0, census_curr=502.0)
        self.assertEqual(m["delta_patient_days"], 62.0)
        self.assertIsNotNone(m["value"])
        expected = round((44000.0 - 45000.0) / 62.0, 1)
        self.assertAlmostEqual(m["value"], expected, places=6)
        self.assertIn("千円", m["caption"])


class TestAlosProxyReuse(unittest.TestCase):
    """alos は独自再実装ではなく metrics.alos_proxy をそのまま呼び出している。"""

    def setUp(self):
        months = pd.date_range("2025-08-01", "2026-08-01", freq="MS")
        adm_parts, pb_parts = [], []
        for i, m in enumerate(months):
            census = 500.0 + 5.0 * i
            adm_parts.append(_adm_month(DEPT, m, census, disch=max(1.0, census / 11.0)))
            pb_parts.append(_pb_month(DEPT, m, 40000.0 + 100.0 * i))
        self.adm = pd.concat(adm_parts, ignore_index=True)
        self.pb = pd.concat(pb_parts, ignore_index=True)
        self.base_date = pd.Timestamp("2026-08-31")

    def test_alos_proxy_is_called(self):
        with patch.object(PU, "alos_proxy", wraps=real_alos_proxy) as spy:
            build_profit_unit_payload(self.pb, self.adm, base_date=self.base_date, months=3)
        self.assertGreater(spy.call_count, 0)

    def test_alos_value_matches_direct_alos_proxy_call(self):
        out = build_profit_unit_payload(self.pb, self.adm, base_date=self.base_date, months=3)
        dept_alos = out["by_dept"][DEPT]["alos"]
        latest_month = pd.Timestamp(dept_alos["months"][-1] + "-01")
        month_end = latest_month + pd.offsets.MonthEnd(0)
        days = (month_end - latest_month).days + 1
        expected = real_alos_proxy(self.adm, month_end, window_days=days,
                                   group_col="診療科名", unit=DEPT)
        self.assertAlmostEqual(dept_alos["values"][-1], round(expected, 1), places=6)


class TestRevisionAdjustedAchievementRate(unittest.TestCase):
    """profit.build_profit_monthly の 達成率_改定換算（改定換算後達成率・表示専用参考値）。"""

    def setUp(self):
        months = ["2026-04-01", "2026-05-01", "2026-06-01", "2026-08-01"]
        pb_rows, pm_rows = [], []
        for m in months:
            ms = pd.Timestamp(m)
            gairai, nyuin = 5000.0, 15000.0
            pb_rows += [
                {"診療科名": DEPT, "月": ms, "区分": "外来", "粗利": gairai},
                {"診療科名": DEPT, "月": ms, "区分": "入院", "粗利": nyuin},
            ]
            pm_rows.append({"診療科名": DEPT, "月": ms, "粗利": gairai + nyuin})
        self.pb = pd.DataFrame(pb_rows)
        self.pd_data = pd.DataFrame(pm_rows)
        self.targets = pd.DataFrame({"診療科名": [DEPT], "月次目標": [18000.0]})
        self.targets_bd = pd.DataFrame({
            "診療科名": [DEPT, DEPT], "区分": ["外来", "入院"], "月次目標": [6000.0, 12000.0],
        })

    def test_pre_revision_month_rate_unchanged(self):
        out = build_profit_monthly(self.pd_data, self.targets,
                                   profit_breakdown=self.pb,
                                   profit_targets_breakdown=self.targets_bd)
        row = out[out["月"] == "2026-05-01"].iloc[0]
        self.assertFalse(pd.isna(row["達成率"]))
        self.assertAlmostEqual(row["達成率_改定換算"], row["達成率"], places=9)

    def test_post_revision_month_rate_differs_when_uplift_not_one(self):
        from app.lib.config import FEE_REVISION_PROFIT_UPLIFT
        out = build_profit_monthly(self.pd_data, self.targets,
                                   profit_breakdown=self.pb,
                                   profit_targets_breakdown=self.targets_bd)
        row = out[out["月"] == "2026-08-01"].iloc[0]
        f_g = FEE_REVISION_PROFIT_UPLIFT.get("外来", 1.0)
        f_n = FEE_REVISION_PROFIT_UPLIFT.get("入院", 1.0)
        if f_g == 1.0 and f_n == 1.0:
            self.skipTest("係数が両方1.0のため換算後も同値になる自明ケース")
        self.assertNotAlmostEqual(row["達成率_改定換算"], row["達成率"], places=6)


class TestLatestBlock(unittest.TestCase):
    """latest（確報バンド）: 最新月の確報4値・前年同月・前年同月比・入院目標達成率。

    census/disch を月内一定値にすると alos_proxy = census/disch（日数で相殺）に単純化
    でき、hand-computed 値ではなく本体と同じ計算式で期待値を作れる（本テストファイルの
    既存流儀＝test_alos_value_matches_direct_alos_proxy_call に合わせる）。
    """

    def setUp(self):
        # 2025-08 と 2026-08 の2か月ぶんだけ用意（他の月は latest の対象外＝
        # profit_map の直接 get() で拾うだけなので month_list の連続性は不要）。
        self.census_prev, self.disch_prev = 500.0, 45.0
        self.census_cur, self.disch_cur = 560.0, 48.0
        self.profit_prev_千円 = 42000.0
        self.profit_cur_千円 = 47000.0
        self.adm = pd.concat([
            _adm_month(DEPT, "2025-08-01", self.census_prev, self.disch_prev),
            _adm_month(DEPT, "2026-08-01", self.census_cur, self.disch_cur),
        ], ignore_index=True)
        self.pb = pd.concat([
            _pb_month(DEPT, "2025-08-01", self.profit_prev_千円),
            _pb_month(DEPT, "2026-08-01", self.profit_cur_千円),
        ], ignore_index=True)
        self.base_date = pd.Timestamp("2026-08-31")

    def test_latest_values_match_hand_computed(self):
        out = build_profit_unit_payload(self.pb, self.adm, base_date=self.base_date, months=13)
        latest = out["global"]["latest"]
        self.assertEqual(latest["month"], "2026-08")

        pd_cur = self.census_cur * 31    # 2026-08 は31日
        pd_prev = self.census_prev * 31  # 2025-08 も31日
        expected_profit_mm = round(self.profit_cur_千円 / 1000.0, 1)
        expected_ppd = round(self.profit_cur_千円 * 1000.0 / pd_cur)
        expected_alos = round(self.census_cur / self.disch_cur, 1)

        self.assertAlmostEqual(latest["profit_mm"], expected_profit_mm, places=6)
        self.assertEqual(latest["patient_days"], round(pd_cur))
        self.assertEqual(latest["ppd"], expected_ppd)
        self.assertAlmostEqual(latest["alos"], expected_alos, places=6)

        expected_py_profit_mm = round(self.profit_prev_千円 / 1000.0, 1)
        expected_py_ppd = round(self.profit_prev_千円 * 1000.0 / pd_prev)
        expected_py_alos = round(self.census_prev / self.disch_prev, 1)
        self.assertIsNotNone(latest["prev_year"])
        self.assertAlmostEqual(latest["prev_year"]["profit_mm"], expected_py_profit_mm, places=6)
        self.assertEqual(latest["prev_year"]["patient_days"], round(pd_prev))
        self.assertEqual(latest["prev_year"]["ppd"], expected_py_ppd)
        self.assertAlmostEqual(latest["prev_year"]["alos"], expected_py_alos, places=6)

        expected_yoy_profit = round(
            (self.profit_cur_千円 - self.profit_prev_千円) / self.profit_prev_千円 * 100, 1)
        expected_yoy_pd = round((pd_cur - pd_prev) / pd_prev * 100, 1)
        expected_yoy_ppd = round((expected_ppd - expected_py_ppd) / expected_py_ppd * 100, 1)
        self.assertAlmostEqual(latest["yoy_pct"]["profit"], expected_yoy_profit, places=6)
        self.assertAlmostEqual(latest["yoy_pct"]["patient_days"], expected_yoy_pd, places=6)
        self.assertAlmostEqual(latest["yoy_pct"]["ppd"], expected_yoy_ppd, places=6)

    def test_no_prev_year_data_yields_none(self):
        # 2026-08のみ（前年同月データなし）
        adm = _adm_month(DEPT, "2026-08-01", self.census_cur, self.disch_cur)
        pb = _pb_month(DEPT, "2026-08-01", self.profit_cur_千円)
        out = build_profit_unit_payload(pb, adm, base_date=self.base_date, months=13)
        latest = out["global"]["latest"]
        self.assertIsNone(latest["prev_year"])
        self.assertIsNone(latest["yoy_pct"]["profit"])
        self.assertIsNone(latest["yoy_pct"]["patient_days"])
        self.assertIsNone(latest["yoy_pct"]["ppd"])

    def test_no_targets_breakdown_yields_none_target(self):
        out = build_profit_unit_payload(self.pb, self.adm, base_date=self.base_date, months=13,
                                        profit_targets_breakdown=None)
        latest = out["global"]["latest"]
        self.assertIsNone(latest["target_mm"])
        self.assertIsNone(latest["achievement_pct"])

    def test_target_mm_matches_calendar_adjusted_formula(self):
        targets_bd = pd.DataFrame({
            "診療科名": [DEPT, "外科B"],
            "区分": ["入院", "入院"],
            "月次目標": [40000.0, 20000.0],   # 千円。全科合計=60000千円
        })
        out = build_profit_unit_payload(self.pb, self.adm, base_date=self.base_date, months=13,
                                        profit_targets_breakdown=targets_bd)
        latest = out["global"]["latest"]
        cal_days = calendar_days_in_month(pd.Timestamp("2026-08-01"))
        expected_target_mm = round(60000.0 * cal_days / STD_CAL_DAYS_PER_MONTH / 1000.0, 1)
        self.assertAlmostEqual(latest["target_mm"], expected_target_mm, places=6)
        expected_achievement = round(latest["profit_mm"] / expected_target_mm * 100, 1)
        self.assertAlmostEqual(latest["achievement_pct"], expected_achievement, places=6)

        # by_dept: 全科合計ではなく該当科（40000千円）だけの目標で計算されること
        dept_latest = out["by_dept"][DEPT]["latest"]
        expected_dept_target_mm = round(40000.0 * cal_days / STD_CAL_DAYS_PER_MONTH / 1000.0, 1)
        self.assertAlmostEqual(dept_latest["target_mm"], expected_dept_target_mm, places=6)


class TestProjection(unittest.TestCase):
    """当月（進行中）の粗利/人日 見込み。global にのみ付与し by_dept には付与しない。"""

    def setUp(self):
        self.adm = pd.concat([
            _adm_month(DEPT, "2025-08-01", 500.0, 45.0),
            _adm_month(DEPT, "2026-08-01", 560.0, 48.0),
            _adm_month(DEPT, "2026-09-01", 560.0, 48.0),   # 進行中月（9月）
        ], ignore_index=True)
        self.pb = pd.concat([
            _pb_month(DEPT, "2025-08-01", 42000.0),
            _pb_month(DEPT, "2026-08-01", 47000.0),
        ], ignore_index=True)
        self.meta = {"latest_mtdblend_nyuin": 50.0}   # 百万円

    def test_elapsed_days_below_3_yields_none(self):
        out = build_profit_unit_payload(self.pb, self.adm, base_date=pd.Timestamp("2026-09-02"),
                                        months=13, profit_hybrid_meta=self.meta)
        self.assertIsNone(out["global"]["projection"])

    def test_hybrid_meta_none_yields_none_without_raising(self):
        out = build_profit_unit_payload(self.pb, self.adm, base_date=pd.Timestamp("2026-09-13"),
                                        months=13, profit_hybrid_meta=None)
        self.assertIsNone(out["global"]["projection"])

    def test_projection_absent_from_by_dept(self):
        out = build_profit_unit_payload(self.pb, self.adm, base_date=pd.Timestamp("2026-09-13"),
                                        months=13, profit_hybrid_meta=self.meta)
        self.assertGreater(len(out["by_dept"]), 0, "フィクスチャ不備（by_dept が空）")
        for blk in out["by_dept"].values():
            self.assertNotIn("projection", blk)

    def test_no_targets_breakdown_yields_none_target(self):
        out = build_profit_unit_payload(self.pb, self.adm, base_date=pd.Timestamp("2026-09-13"),
                                        months=13, profit_hybrid_meta=self.meta,
                                        profit_targets_breakdown=None)
        proj = out["global"]["projection"]
        self.assertIsNone(proj["target_mm"])
        self.assertIsNone(proj["achievement_pct"])

    def test_target_mm_uses_projection_month_not_latest_confirmed_month(self):
        """Phase3: 見込みタイルの目標比は前月(確報月=8月)の latest.target_mm を流用せず、
        当月（進行中月=9月）の暦日数で補正した目標を _inpatient_target_mm へ計算し直すこと
        （8月と9月は暦日数が違うため target_mm が異なるはず）。
        """
        targets_bd = pd.DataFrame({
            "診療科名": [DEPT, "外科B"],
            "区分": ["入院", "入院"],
            "月次目標": [40000.0, 20000.0],   # 千円。全科合計=60000千円
        })
        out = build_profit_unit_payload(self.pb, self.adm, base_date=pd.Timestamp("2026-09-13"),
                                        months=13, profit_hybrid_meta=self.meta,
                                        profit_targets_breakdown=targets_bd)
        proj = out["global"]["projection"]
        cal_days_sep = calendar_days_in_month(pd.Timestamp("2026-09-01"))
        expected_target_mm = round(60000.0 * cal_days_sep / STD_CAL_DAYS_PER_MONTH / 1000.0, 1)
        self.assertAlmostEqual(proj["target_mm"], expected_target_mm, places=6)
        expected_achievement = round(proj["profit_mm"] / expected_target_mm * 100, 1)
        self.assertAlmostEqual(proj["achievement_pct"], expected_achievement, places=6)

        # 確報(latest.target_mm)とは異なる値であること（=前月の値を流用していない証拠）
        latest_target_mm = out["global"]["latest"]["target_mm"]
        self.assertNotEqual(proj["target_mm"], latest_target_mm)


class TestPrevMonthPendingS2(unittest.TestCase):
    """Phase4 S2（確報遅れ）: 入院粗利の最新確報月(anchor_month=A)が前月(P)より古い状態を、
    profit_breakdown から1か月落としたフィクスチャで再現する（実データは A==P の S1
    にしかならず再現できないため）。

    P月の「見込み（確報待ち）」は新規推計器を作らず、既存の日次系列
    hospital_series["values_final_nyuin"] の P月末日の値をそのまま抽出するだけである
    こと（＝値が推計器の出力とビット一致すること）を確認する。
    """

    def setUp(self):
        # 入院粗利の確報は 2026-08 まで（9月分は未確報＝意図的に欠落させたフィクスチャ）。
        self.pb = _pb_month(DEPT, "2026-08-01", 47000.0)
        # adm: 8月(確報基準月A)・9月(確報待ちP・在院550人/日で固定)・10月(進行中D・13日まで)。
        adm_oct_full = _adm_month(DEPT, "2026-10-01", 540.0, 46.0)
        self.adm = pd.concat([
            _adm_month(DEPT, "2026-08-01", 560.0, 48.0),
            _adm_month(DEPT, "2026-09-01", 550.0, 47.0),
            adm_oct_full[adm_oct_full["日付"] <= pd.Timestamp("2026-10-13")],
        ], ignore_index=True)
        # hospital_series: 校正済み日次系列。9/30時点で45.0百万円に達するよう作り込み、
        # 「新規推計ではなく既存系列の値をそのまま使う」ことをビット一致で確認できるようにする。
        dates = [d.strftime("%Y-%m-%d") for d in pd.date_range("2026-08-01", "2026-10-13")]

        def _val(d):
            ts = pd.Timestamp(d)
            if ts.month == 8:
                return 40.0
            if ts.month == 9:
                return round(40.0 + 5.0 * ts.day / 30.0, 2)
            return 45.0   # 10月ぶんはこのテストの関心外（同値で埋めるだけ）

        self.hospital_series = {"dates": dates, "values_final_nyuin": [_val(d) for d in dates]}
        self.base_date = pd.Timestamp("2026-10-13")

    def test_s2_prev_month_pending_uses_series_value_not_new_estimator(self):
        out = build_profit_unit_payload(
            self.pb, self.adm, base_date=self.base_date, months=13,
            hospital_series=self.hospital_series,
        )
        pending = out["global"]["prev_month_pending"]
        self.assertIsNotNone(pending, "S2(確報遅れ)なのに prev_month_pending が付かない")
        self.assertEqual(pending["month"], "2026-09")
        self.assertEqual(pending["confirmed_month"], "2026-08")
        self.assertAlmostEqual(pending["profit_mm"], 45.0, places=6)

        expected_patient_days = 550.0 * 30   # 2026-09は30日
        expected_ppd = round(45.0 * 1_000_000.0 / expected_patient_days)
        self.assertEqual(pending["patient_days"], round(expected_patient_days))
        self.assertEqual(pending["ppd"], expected_ppd)

    def test_s1_no_pending_when_confirmed_reaches_prev_month(self):
        # 確報が前月分まで揃っている通常時（S1: A(9月)==P(9月)）は None のまま。
        pb_s1 = pd.concat([
            _pb_month(DEPT, "2026-08-01", 47000.0),
            _pb_month(DEPT, "2026-09-01", 48000.0),
        ], ignore_index=True)
        out = build_profit_unit_payload(
            pb_s1, self.adm, base_date=self.base_date, months=13,
            hospital_series=self.hospital_series,
        )
        self.assertIsNone(out["global"]["prev_month_pending"])

    def test_no_hospital_series_yields_none_without_raising(self):
        out = build_profit_unit_payload(
            self.pb, self.adm, base_date=self.base_date, months=13,
            hospital_series=None,
        )
        self.assertIsNone(out["global"]["prev_month_pending"])


class TestProjectionNoneWhenAnchorEqualsCurrentMonth(unittest.TestCase):
    """Phase4 S3: anchor_month(入院粗利の最新確報月)が当月そのものと一致する
    （通常はあり得ない）状態では、見込み自体を作らない（projection=None）。"""

    def test_anchor_equals_current_month_yields_none_projection(self):
        adm = _adm_month(DEPT, "2026-09-01", 560.0, 48.0)
        pb = _pb_month(DEPT, "2026-09-01", 47000.0)   # 確報が当月ぶんまで入っている異常系
        meta = {"latest_mtdblend_nyuin": 50.0}
        out = build_profit_unit_payload(pb, adm, base_date=pd.Timestamp("2026-09-13"),
                                        months=13, profit_hybrid_meta=meta)
        self.assertIsNone(out["global"]["projection"])


class TestProjectionDeadband(unittest.TestCase):
    """vs_prev_pct のデッドバンド境界（±1.5%）で direction が up/flat/down/flat になること。

    延患者数見込み（_projected_patient_days_this_month）を1,000,000人日に固定すると
    ppd = round(profit_mm) になり、vs_prev_pct をきれいな境界値に作り込める
    （TestAlosProxyReuse と同じ「本体の依存関数をpatchして値を作り込む」流儀）。
    """

    def _direction_for(self, profit_mm_raw: float, latest_ppd: float):
        adm = pd.DataFrame({"日付": [pd.Timestamp("2026-09-13")], "在院患者数": [0.0]})
        with patch.object(PU, "_projected_patient_days_this_month", return_value=1_000_000.0):
            return PU._build_projection(adm, pd.Timestamp("2026-09-13"),
                                        {"latest_mtdblend_nyuin": profit_mm_raw}, latest_ppd)

    def test_boundary_up(self):
        out = self._direction_for(101500.0, 100000.0)
        self.assertEqual(out["vs_prev_pct"], 1.5)
        self.assertEqual(out["direction"], "up")

    def test_boundary_flat_just_below_up(self):
        out = self._direction_for(101400.0, 100000.0)
        self.assertEqual(out["vs_prev_pct"], 1.4)
        self.assertEqual(out["direction"], "flat")

    def test_boundary_down(self):
        out = self._direction_for(98500.0, 100000.0)
        self.assertEqual(out["vs_prev_pct"], -1.5)
        self.assertEqual(out["direction"], "down")

    def test_boundary_flat_just_above_down(self):
        out = self._direction_for(98600.0, 100000.0)
        self.assertEqual(out["vs_prev_pct"], -1.4)
        self.assertEqual(out["direction"], "flat")


class TestProjectionCalibratedPriority(unittest.TestCase):
    """_build_projection の分子は校正済み latest_final_nyuin を優先し、無ければ
    未校正の latest_mtdblend_nyuin にフォールバックすること（Phase 1 不具合修正）。
    """

    def _profit_mm_for(self, meta: dict) -> float:
        adm = pd.DataFrame({"日付": [pd.Timestamp("2026-09-13")], "在院患者数": [0.0]})
        with patch.object(PU, "_projected_patient_days_this_month", return_value=1_000_000.0):
            out = PU._build_projection(adm, pd.Timestamp("2026-09-13"), meta, latest_ppd=None)
        return out["profit_mm"]

    def test_prefers_latest_final_nyuin_when_present(self):
        meta = {"latest_final_nyuin": 51.0, "latest_mtdblend_nyuin": 50.0}
        self.assertEqual(self._profit_mm_for(meta), 51.0)

    def test_falls_back_to_mtdblend_when_final_absent(self):
        meta = {"latest_mtdblend_nyuin": 50.0}
        self.assertEqual(self._profit_mm_for(meta), 50.0)


class TestBuildDetailJsonProfitTargetsPurity(unittest.TestCase):
    """build_detail_json は profit_targets_breakdown をキーワード引数で受け取るだけで、

    自らファイル（load_profit_targets_breakdown）を読まない（テストゲートの密閉化・
    同型事故の再発防止＝cwd次第で実データを暗黙に読む経路を作らない）。

    build_detail_json のフル呼び出しには adm/surg/targets/surg_targets 等の大がかりな
    合成データ準備が要る（build_kpi_summary 単体だけでも14か月分の日次データ規模。
    tests/test_kpi_summary_period_fy_yoy.py 参照）ため、ここでは
      (a) html_builder モジュールが load_profit_targets_breakdown を一切
          import/参照していないこと（静的な保証＝実行時にファイルを読みようが無い）
      (b) build_detail_json のシグネチャに profit_targets_breakdown キーワード引数が
          あり、既定値が None であること
    を検証する。「未指定なら target_mm/achievement_pct が None になる」という値レベル
    の保証は、本ファイルの TestLatestBlock.test_no_targets_breakdown_yields_none_target
    （build_profit_unit_payload を直接呼ぶ既存テスト）に委ねる。
    """

    def test_html_builder_does_not_import_the_loader(self):
        from app.lib import html_builder
        self.assertFalse(
            hasattr(html_builder, "load_profit_targets_breakdown"),
            "build_detail_json 内で load_profit_targets_breakdown を直接呼べる状態＝"
            "cwd次第で実データを読みうる状態に戻っている",
        )

    def test_build_detail_json_signature_has_default_none_kwarg(self):
        import inspect
        from app.lib.html_builder import build_detail_json
        sig = inspect.signature(build_detail_json)
        self.assertIn("profit_targets_breakdown", sig.parameters)
        self.assertIsNone(sig.parameters["profit_targets_breakdown"].default)


if __name__ == "__main__":
    unittest.main()
