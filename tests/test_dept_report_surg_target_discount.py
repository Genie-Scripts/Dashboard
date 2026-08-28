"""部門レポート「この期間の一手」: 週目標の暦補正割引（P3-1）の回帰テスト。

P1で triage.py に導入した adjusted_weekly_target（週目標 × 直近7暦日窓の営業日数/5）を
dept_report.py の r7_surg vs surg_targets 比較箇所（unit_meta の gap階級タグ・
_select_action_topic・KPIバッジ）へ適用した改修の検証。

  1. 祝日週（ハッピーマンデー・営業日4）: 割引後の目標に対する達成度で
     gap階級（_state["surg"]）・KPIバッジ(ok/tgt)が動くこと。
  2. 通常週（営業日5）: 割引が短絡で恒等になり、旧来と同一の gap階級・バッジになること。

build_dept_report_contexts の重い前処理・LLM呼び出しはフェイクに差し替える
（test_dept_report_leveling_skip.py と同じハーネス方式）。with_ai=False により
narrate_* は一切呼ばれない（定型文フォールバックのみ・oMLX不要）。

実行: リポジトリルートで
    python -m unittest discover -s tests -v
"""
import contextlib
import copy
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib import dept_report as dr  # noqa: E402
from app.lib.config import operational_days_between  # noqa: E402

# 整形外科（外科系・SURGERY_EVAL_DEPTS所属）1科のみの最小フィクスチャ。
DEPT_UNITS = [
    {"name": "整形外科", "room_per_week": 0.6, "retention": 0.90, "room_delta_4w": 0.1},
]
CAND = {"dept": [(u["name"], u["name"]) for u in DEPT_UNITS], "ward": []}

TARGETS = {
    "new_admission": {"dept": {}, "ward": {}},
    "inpatient": {"dept": {}, "ward": {}, "ward_beds": {}},
}
# 週目標20件・直近7日実績16件 → 素の達成率80%("目標を明確に下回っている"=poor)。
SURG_TARGETS = {"整形外科": 20}
R7_INP = {"by_dept": {}, "by_ward": {}}
R7_NADM = {"by_dept": {}, "by_ward": {}}
R7_SURG = {"by_dept": {"整形外科": 16}}

FAKE_PART = {"kind": "A", "name": "ダミー", "badge": None, "note": "", "is_dow": False,
             "_data": {"cur": [], "prev": [], "proj": None}, "_ref": 0, "_ref_label": "",
             "_unit": "", "_win": 1, "_color": "#000"}


def _wl(units):
    return {"units": copy.deepcopy(units), "total": {"retention": 0.8}}


def _run(base_date):
    """build_dept_report_contexts を最小フェイクで実行（診療科軸・整形外科のみ）。"""
    patches = [
        mock.patch.object(dr, "weekend_census_retention",
                          lambda adm, base_date, entity=None, weeks=8: _wl(DEPT_UNITS)),
        mock.patch.object(dr, "_dow_unit_candidates", lambda entity: ("col", CAND[entity])),
        mock.patch.object(dr, "build_dow_unit_detail", lambda *a, **k: {}),
        mock.patch.object(dr, "rolling7_inpatient_avg", lambda *a, **k: copy.deepcopy(R7_INP)),
        mock.patch.object(dr, "rolling7_new_admission", lambda *a, **k: copy.deepcopy(R7_NADM)),
        mock.patch.object(dr, "rolling7_surgery", lambda *a, **k: copy.deepcopy(R7_SURG)),
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
            adm=pd.DataFrame(), surg=pd.DataFrame(),
            targets=TARGETS, surg_targets=SURG_TARGETS,
            profit_monthly=pd.DataFrame(),
            base_date=base_date, generated_at=base_date,
            hospital_name="テスト病院", with_ai=False, axes=("dept",), quiet=True,
            profit_breakdown=None, delta_anchor=None, overrides=None)


def _seikei_ctx(contexts):
    return next(c for c in contexts if c["unit"] == "整形外科")


class TestHolidayWeekDiscountAppliesToGapLevel(unittest.TestCase):
    """ハッピーマンデー週（2026-01-18・直近7暦日窓の営業日=4）で割引が反映されること。"""

    BASE = pd.Timestamp("2026-01-18")

    def test_biz_days_precondition_is_four(self):
        self.assertEqual(
            operational_days_between(self.BASE - pd.Timedelta(days=6), self.BASE), 4)

    def test_gap_level_moves_from_poor_to_met(self):
        # 素の達成率 16/20=80% は "poor"（目標を明確に下回っている）。
        # 割引後目標 20*4/5=16 に対しては 16/16=100% で "met"（目標を達成している）。
        ctx = _seikei_ctx(_run(self.BASE))
        self.assertEqual(ctx["_state"]["surg"], "met")

    def test_kpi_badge_reflects_discounted_target(self):
        ctx = _seikei_ctx(_run(self.BASE))
        surg_kpi = ctx["kpis"][0]   # 外科系の1枚目=全麻/全手術KPI（lead=True）
        self.assertEqual(surg_kpi["val"], "16")
        self.assertIn("16", surg_kpi["tgt"])       # 割引後目標=16.0
        self.assertNotIn("20", surg_kpi["tgt"])    # 素の目標20はもう表示されない
        self.assertTrue(surg_kpi["ok"])            # 16>=16 で達成扱いに変わる

    def test_topic_still_forced_to_surgery(self):
        # 外科系は達成状況によらずトピックは常に手術に固定（2026-07-22発信方針）。
        # 割引適用後も他トピックへ移らないことの確認（移った場合は設計上の想定外）。
        ctx = _seikei_ctx(_run(self.BASE))
        self.assertEqual(ctx["move"]["topic"], "surgery")


class TestOrdinaryWeekIsIdentity(unittest.TestCase):
    """通常週（2026-07-19・営業日5）は短絡により割引前と完全に同一であること。"""

    BASE = pd.Timestamp("2026-07-19")

    def test_biz_days_precondition_is_five(self):
        self.assertEqual(
            operational_days_between(self.BASE - pd.Timedelta(days=6), self.BASE), 5)

    def test_gap_level_and_badge_match_undiscounted_target(self):
        ctx = _seikei_ctx(_run(self.BASE))
        # 16/20=80% は素の目標のままでも "poor"（割引が効かないため変化なし）。
        self.assertEqual(ctx["_state"]["surg"], "poor")
        surg_kpi = ctx["kpis"][0]
        self.assertIn("20", surg_kpi["tgt"])
        self.assertFalse(surg_kpi["ok"])

    def test_matches_identity_patched_run(self):
        """adjusted_weekly_target を identity(target->target) に monkeypatch した
        「割引導入前」相当の実行と、通常週では contexts が完全一致すること。"""
        with mock.patch.object(dr, "adjusted_weekly_target", side_effect=lambda t, bd: t):
            old_ctx = _seikei_ctx(_run(self.BASE))
        new_ctx = _seikei_ctx(_run(self.BASE))
        self.assertEqual(old_ctx["_state"], new_ctx["_state"])
        self.assertEqual(old_ctx["kpis"], new_ctx["kpis"])
        self.assertEqual(old_ctx["move"]["topic"], new_ctx["move"]["topic"])
        self.assertEqual(old_ctx["move"].get("surg_line"), new_ctx["move"].get("surg_line"))


if __name__ == "__main__":
    unittest.main()
