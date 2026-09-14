"""
test_turnover_metrics.py — 回転3指標（在院=守り／新入院=攻め／期間III超え=退院促進）のテスト。

対象: app.lib.metrics.alos_proxy / reference_alos / turnover_metrics / format_turn_line
      app.lib.dept_report.build_dept_report_contexts の move dict への turn_line 混入

在院日数の短縮そのものを号令にしないという設計制約（config.TURN_HINTS）を踏まえ、
①state 3分岐（fill/turn/hold）、②nadm_gap の恒等式、③los_df 有無での mode 切替、
④format_turn_line の None→「—」・参考表示、を合成データで検証する。

実行: リポジトリルートで
    python -m pytest tests/test_turnover_metrics.py -q
"""
import contextlib
import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.metrics import (  # noqa: E402
    alos_proxy, reference_alos, turnover_metrics, format_turn_line,
)
from app.lib.config import TURN_HINTS  # noqa: E402


def _mk_adm(rows) -> pd.DataFrame:
    """metrics.build_daily_series が要求する最小列（日付・科_表示・病棟_表示・在院/退院/
    新入院 各列）を備えた合成 adm フレームを組み立てる。"""
    df = pd.DataFrame(rows)
    df["科_表示"] = True
    df["病棟_表示"] = True
    return df


def _steady_rows(start, days, census, discharge, nadm, dept="内科A", ward="W1"):
    """指定期間、毎日一定値（在院・退院・新入院）を積む行を返す。"""
    idx = pd.date_range(start, periods=days, freq="D")
    rows = []
    for d in idx:
        rows.append({
            "日付": d, "診療科名": dept, "病棟コード": ward,
            "在院患者数": census, "退院合計": discharge,
            "新入院患者数": nadm, "新入院患者数_病棟": nadm,
        })
    return rows


class AlosProxyTest(unittest.TestCase):
    def test_basic_identity(self):
        # 28日間・在院100人/日・退院10人/日 → 延べ2800人 / 退院280人 = 10.0日
        rows = _steady_rows("2026-08-01", 28, census=100, discharge=10, nadm=10)
        adm = _mk_adm(rows)
        v = alos_proxy(adm, pd.Timestamp("2026-08-28"), window_days=28,
                       group_col="診療科名", unit="内科A")
        self.assertAlmostEqual(v, 10.0)

    def test_zero_discharge_is_none(self):
        rows = _steady_rows("2026-08-01", 28, census=100, discharge=0, nadm=10)
        adm = _mk_adm(rows)
        v = alos_proxy(adm, pd.Timestamp("2026-08-28"), window_days=28,
                       group_col="診療科名", unit="内科A")
        self.assertIsNone(v)

    def test_group_filters_to_unit(self):
        # 他ユニット（外科A）の値に影響されないこと
        rows = (_steady_rows("2026-08-01", 28, census=100, discharge=10, nadm=10,
                             dept="内科A", ward="W1")
               + _steady_rows("2026-08-01", 28, census=500, discharge=5, nadm=3,
                              dept="外科A", ward="W2"))
        adm = _mk_adm(rows)
        v = alos_proxy(adm, pd.Timestamp("2026-08-28"), window_days=28,
                       group_col="診療科名", unit="内科A")
        self.assertAlmostEqual(v, 10.0)


class ReferenceAlosTest(unittest.TestCase):
    """当月を除く直近12完全月の月次alos_proxyの中央値になること。"""

    BASE = pd.Timestamp("2026-09-15")   # 当月=2026-09（除外対象）

    def _build(self):
        rows = []
        # 直近12完全月: 2025-09 〜 2026-08（月が進むほど alos が 22→11 と減っていく）
        month_starts = pd.date_range("2025-09-01", periods=12, freq="MS")
        for i, ms in enumerate(month_starts):
            alos = 22 - i   # 2025-09→22, 2025-10→21, ..., 2026-08→11
            days_in_month = (ms + pd.offsets.MonthEnd(0)).day
            rows += _steady_rows(ms, days_in_month, census=alos * 10, discharge=10, nadm=10)
        # 当月（2026-09・1〜15日）は極端な値にして「除外されていること」を検証する
        rows += _steady_rows("2026-09-01", 15, census=999 * 10, discharge=10, nadm=10)
        return _mk_adm(rows)

    def test_excludes_current_month_and_uses_median(self):
        adm = self._build()
        v = reference_alos(adm, self.BASE, group_col="診療科名", unit="内科A")
        # 12個の値 {11..22} の中央値 = (16+17)/2 = 16.5（999は除外される）
        self.assertAlmostEqual(v, 16.5)

    def test_month_with_zero_discharge_excluded_from_median(self):
        adm = self._build()
        # 直近1完全月（2026-08）の退院を0にして中央値対象から除外されることを確認
        mask = (adm["日付"] >= "2026-08-01") & (adm["日付"] <= "2026-08-31")
        adm.loc[mask, "退院合計"] = 0
        v = reference_alos(adm, self.BASE, group_col="診療科名", unit="内科A")
        # 残り11個 {12..22} の中央値 = 17
        self.assertAlmostEqual(v, 17.0)

    def test_no_history_returns_none(self):
        rows = _steady_rows("2026-09-01", 10, census=100, discharge=10, nadm=10)
        adm = _mk_adm(rows)
        v = reference_alos(adm, self.BASE, group_col="診療科名", unit="内科A")
        self.assertIsNone(v)


class TurnoverMetricsStateTest(unittest.TestCase):
    """state の3分岐（fill/turn/hold）と nadm_gap の恒等式。"""

    BASE = pd.Timestamp("2026-09-15")

    def _build_with_recent(self, recent_census, recent_nadm, ref_alos_value=10):
        rows = []
        # 直近12完全月: 一定の alos（=ref_alos_value）を持つ履歴
        month_starts = pd.date_range("2025-09-01", periods=12, freq="MS")
        for ms in month_starts:
            days_in_month = (ms + pd.offsets.MonthEnd(0)).day
            rows += _steady_rows(ms, days_in_month,
                                 census=ref_alos_value * 10, discharge=10, nadm=10)
        # 当月（直近7日窓を含む）
        rows += _steady_rows("2026-09-01", 15, census=recent_census,
                             discharge=10, nadm=recent_nadm)
        return _mk_adm(rows)

    def test_fill_state_when_census_below_ratio(self):
        # target=100・在院90（90 < 100*0.98=98）→ fill
        adm = self._build_with_recent(recent_census=90, recent_nadm=9, ref_alos_value=10)
        m = turnover_metrics(adm, self.BASE, census_target=100,
                             group_col="診療科名", unit="内科A")
        self.assertEqual(m["state"], "fill")
        self.assertEqual(m["census_gap"], 10.0)
        self.assertIn("まず床を埋める", m["hint"])

    def test_turn_state_when_census_ok_but_admission_short(self):
        # target=100・在院99（>=98=fill閾値未満なのでfillでない）・ref_alos=10 → 必要10.0
        # 実績新入院=8 → gap=2.0(>=0.5) → turn
        adm = self._build_with_recent(recent_census=99, recent_nadm=8, ref_alos_value=10)
        m = turnover_metrics(adm, self.BASE, census_target=100,
                             group_col="診療科名", unit="内科A")
        self.assertEqual(m["state"], "turn")
        self.assertAlmostEqual(m["nadm_required"], 10.0)
        self.assertAlmostEqual(m["nadm_per_day_7d"], 8.0)
        # 恒等式: nadm_gap = max(0, required - actual)
        self.assertAlmostEqual(m["nadm_gap"], round(m["nadm_required"] - m["nadm_per_day_7d"], 1))
        self.assertIn("空いた床に次を入れる", m["hint"])

    def test_hold_state_when_on_target(self):
        # target=100・在院99・新入院10（必要10.0とほぼ一致=gap0）→ hold
        adm = self._build_with_recent(recent_census=99, recent_nadm=10, ref_alos_value=10)
        m = turnover_metrics(adm, self.BASE, census_target=100,
                             group_col="診療科名", unit="内科A")
        self.assertEqual(m["state"], "hold")
        self.assertEqual(m["nadm_gap"], 0.0)
        self.assertEqual(m["hint"], TURN_HINTS["hold"])

    def test_none_census_target_never_raises_and_gap_fields_are_none(self):
        adm = self._build_with_recent(recent_census=99, recent_nadm=10, ref_alos_value=10)
        m = turnover_metrics(adm, self.BASE, census_target=None,
                             group_col="診療科名", unit="内科A")
        self.assertIsNone(m["census_gap"])
        self.assertIsNone(m["nadm_required"])
        self.assertIsNone(m["nadm_gap"])
        # 目標が無いので fill 判定はできず、nadm_gapもNoneなのでhold
        self.assertEqual(m["state"], "hold")

    def test_malformed_adm_falls_back_to_neutral_hold(self):
        # 想定列を欠く adm（例: 空DataFrame）でも例外にせず hold へ縮退する
        m = turnover_metrics(pd.DataFrame(), self.BASE, census_target=100,
                             group_col="診療科名", unit="内科A")
        self.assertEqual(m["state"], "hold")
        self.assertIsNone(m["census_7d"])
        self.assertEqual(m["mode"], "alos_proxy")

    def test_ward_axis_uses_ward_admission_column(self):
        """病棟軸（group_col="病棟コード"）は新入院患者数_病棟（転入含む）を使う。"""
        idx = pd.date_range("2026-09-09", periods=7, freq="D")
        rows = []
        for d in idx:
            rows.append({
                "日付": d, "診療科名": "内科A", "病棟コード": "W1",
                "在院患者数": 100, "退院合計": 10,
                "新入院患者数": 5, "新入院患者数_病棟": 8,   # 転入3人ぶん差がある
            })
        adm = _mk_adm(rows)
        m = turnover_metrics(adm, pd.Timestamp("2026-09-15"), census_target=None,
                             group_col="病棟コード", unit="W1")
        self.assertAlmostEqual(m["nadm_per_day_7d"], 8.0)   # 病棟軸は_病棟列(転入込み)を使う


class TurnoverMetricsLosDfModeTest(unittest.TestCase):
    """los_df の有無で mode が over_iii / alos_proxy に切り替わること。"""

    BASE = pd.Timestamp("2026-09-15")

    def _adm(self):
        rows = _steady_rows("2026-08-01", 45, census=100, discharge=10, nadm=10)
        return _mk_adm(rows)

    def test_no_los_df_uses_alos_proxy_mode(self):
        m = turnover_metrics(self._adm(), self.BASE, census_target=100,
                             group_col="診療科名", unit="内科A", los_df=None)
        self.assertEqual(m["mode"], "alos_proxy")
        self.assertIsNone(m["over_iii"])

    def test_empty_los_df_uses_alos_proxy_mode(self):
        m = turnover_metrics(self._adm(), self.BASE, census_target=100,
                             group_col="診療科名", unit="内科A",
                             los_df=pd.DataFrame(columns=["日付", "診療科名", "期間III超え患者数"]))
        self.assertEqual(m["mode"], "alos_proxy")

    def test_los_df_with_matching_unit_uses_over_iii_mode(self):
        idx = pd.date_range("2026-09-02", periods=14, freq="D")
        los = pd.DataFrame({
            "日付": idx, "診療科名": ["内科A"] * 14,
            "期間III超え患者数": [10] * 7 + [6] * 7,
        })
        m = turnover_metrics(self._adm(), self.BASE, census_target=100,
                             group_col="診療科名", unit="内科A", los_df=los)
        self.assertEqual(m["mode"], "over_iii")
        self.assertEqual(m["over_iii"], 6)
        self.assertEqual(m["over_iii_prev"], 10)

    def test_los_df_without_matching_unit_falls_back_to_alos_proxy(self):
        idx = pd.date_range("2026-09-02", periods=7, freq="D")
        los = pd.DataFrame({
            "日付": idx, "診療科名": ["外科A"] * 7,   # 対象ユニットと一致しない
            "期間III超え患者数": [10] * 7,
        })
        m = turnover_metrics(self._adm(), self.BASE, census_target=100,
                             group_col="診療科名", unit="内科A", los_df=los)
        self.assertEqual(m["mode"], "alos_proxy")


class FormatTurnLineTest(unittest.TestCase):
    def test_all_none_renders_dash(self):
        m = {"census_7d": None, "census_target": None, "nadm_per_day_7d": None,
            "nadm_required": None, "nadm_gap": None, "alos_28d": None,
            "alos_prev_28d": None, "mode": "alos_proxy"}
        line = format_turn_line(m)
        self.assertEqual(
            line,
            "回転：在院 —／目標—・新入院/日 —／必要—（あと—）・在院日数 —日（参考）")

    def test_alos_proxy_mode_shows_reference_suffix(self):
        m = {"census_7d": 572, "census_target": 575, "nadm_per_day_7d": 51.6,
            "nadm_required": 55.3, "nadm_gap": 3.7, "alos_28d": 11.1,
            "alos_prev_28d": 10.4, "mode": "alos_proxy"}
        line = format_turn_line(m)
        self.assertIn("在院日数 11.1日（参考）", line)
        # PDFは A4 1枚/部門。既存の数値行は実測41〜45字。天地余白を6mm詰めてこの長さまで収まる。
        self.assertLessEqual(len(line), 56, f"回転行が長すぎる（{len(line)}字）: {line}")
        self.assertIn("在院 572／目標575", line)
        self.assertIn("新入院/日 51.6／必要55.3（あと3.7）", line)

    def test_over_iii_mode_shows_period3_count(self):
        m = {"census_7d": 572, "census_target": 575, "nadm_per_day_7d": 51.6,
            "nadm_required": 55.3, "nadm_gap": 3.7, "over_iii": 48, "over_iii_prev": 44,
            "mode": "over_iii"}
        line = format_turn_line(m)
        self.assertEqual(
            line,
            "回転：在院 572／目標575・新入院/日 51.6／必要55.3（あと3.7）・"
            "期間III超え 48人")


# ════════════════════════════════════════════════════════════
# dept_report.build_dept_report_contexts の move dict への混入
# ════════════════════════════════════════════════════════════
from app.lib import dept_report as dr  # noqa: E402

DEPT_UNITS = [
    {"name": "呼吸器内科", "room_per_week": 0.0, "retention": 0.90, "room_delta_4w": 0.0},
]
CAND = {"dept": [(u["name"], u["name"]) for u in DEPT_UNITS], "ward": []}
TARGETS = {
    "new_admission": {"dept": {}, "ward": {}},
    "inpatient": {"dept": {}, "ward": {}, "ward_beds": {}},
}
FAKE_PART = {"kind": "A", "name": "ダミー", "badge": None, "note": "", "is_dow": False,
             "_data": {"cur": [], "prev": [], "proj": None}, "_ref": 0, "_ref_label": "",
             "_unit": "", "_win": 1, "_color": "#000"}


def _wl(units):
    return {"units": copy.deepcopy(units), "total": {"retention": 0.8}}


def _run_contexts(adm=None, los_df=None):
    """build_dept_report_contexts を最小フェイクで実行（診療科軸・呼吸器内科のみ）。
    turnover_metrics 自体はフェイクにしない（実データ不足時の hold 縮退込みで検証する）。
    """
    patches = [
        mock.patch.object(dr, "weekend_census_retention",
                          lambda adm, base_date, entity=None, weeks=8: _wl(DEPT_UNITS)),
        mock.patch.object(dr, "_dow_unit_candidates", lambda entity: ("col", CAND[entity])),
        mock.patch.object(dr, "build_dow_unit_detail", lambda *a, **k: {}),
        mock.patch.object(dr, "rolling7_inpatient_avg",
                          lambda *a, **k: {"by_dept": {}, "by_ward": {}}),
        mock.patch.object(dr, "rolling7_new_admission",
                          lambda *a, **k: {"by_dept": {"呼吸器内科": 16}, "by_ward": {}}),
        mock.patch.object(dr, "rolling7_surgery", lambda *a, **k: {"by_dept": {}}),
        mock.patch.object(dr, "build_dept_ranking", lambda *a, **k: pd.DataFrame()),
        mock.patch.object(dr, "build_surgery_ranking", lambda *a, **k: pd.DataFrame()),
        mock.patch.object(dr, "_build_parts", lambda *a, **k: {"A": dict(FAKE_PART)}),
        mock.patch.object(dr, "render_trend_svg", lambda *a, **k: ""),
        mock.patch.object(dr, "_unit_profit_series", lambda *a, **k: None),
        mock.patch.object(dr, "_q_planned_mix", lambda *a, **k: None),
        mock.patch.object(dr, "_q_or_load", lambda *a, **k: None),
        mock.patch.object(dr, "_q_surg_dow_shape", lambda *a, **k: None),
        mock.patch.object(dr, "_q_surg_urgency_mix", lambda *a, **k: None),
        mock.patch.object(dr, "_q_holiday_week", lambda *a, **k: None),
        mock.patch.object(dr, "narrate_leveling_actions", lambda *a, **k: a[0] if a else None),
    ]
    with contextlib.ExitStack() as st:
        for p in patches:
            st.enter_context(p)
        return dr.build_dept_report_contexts(
            adm=(adm if adm is not None else pd.DataFrame()), surg=pd.DataFrame(),
            targets=TARGETS, surg_targets={},
            profit_monthly=pd.DataFrame(),
            base_date=pd.Timestamp("2026-09-15"), generated_at=pd.Timestamp("2026-09-15"),
            hospital_name="テスト病院", with_ai=False, axes=("dept",), quiet=True,
            profit_breakdown=None, delta_anchor=None, overrides=None, los_df=los_df)


class DeptReportTurnLineTest(unittest.TestCase):
    def test_move_dict_has_turn_line_fields(self):
        contexts = _run_contexts()
        ctx = next(c for c in contexts if c["unit"] == "呼吸器内科")
        move = ctx["move"]
        self.assertIn("turn_line", move)
        self.assertIn("turn_state", move)
        self.assertIn("turn_hint", move)
        self.assertTrue(move["turn_line"].startswith("回転："))
        # adm が空で在院目標も未設定 → データ不足の安全側縮退で hold
        self.assertEqual(move["turn_state"], "hold")

    def test_los_df_param_is_accepted(self):
        # los_df を渡しても例外にならないこと（配線の疎通確認）
        los_df = pd.DataFrame(columns=["日付", "診療科名", "期間III超え患者数"])
        contexts = _run_contexts(los_df=los_df)
        self.assertTrue(contexts)


if __name__ == "__main__":
    unittest.main()
