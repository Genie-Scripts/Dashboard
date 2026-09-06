"""部門レポート「この期間の一手」: 新入院 週目標の暦補正割引（F3）の回帰テスト。

test_dept_report_surg_target_discount.py（全麻・外科系）の対＝新入院・内科系版。
adjusted_weekly_target を dept_report.py の r7_nadm vs targets["new_admission"] 比較箇所
（unit_meta の gap階級タグ・_select_action_topic・KPIバッジ）へ適用した改修の検証。

  1. 祝日週（ハッピーマンデー・営業日4）: 割引後の目標(20*4/5=16)に対しては実績16で
     不足0＝admissionトピックの足切りスコアを割り込み、主トピックが admission→leveling
     へ変わること（gap階級タグ・KPIバッジも同時に動く）。
  2. 通常週（営業日5）: 割引が短絡で恒等になり、旧来と同一のトピック・タグ・バッジになること。

build_dept_report_contexts の重い前処理・LLM呼び出しはフェイクに差し替える
（test_dept_report_surg_target_discount.py と同じハーネス方式）。with_ai=False により
narrate_* は一切呼ばれない（定型文フォールバックのみ・oMLX不要）。

実行: リポジトリルートで
    OMLX_BASE_URL=http://127.0.0.1:9 .venv/bin/python -m pytest tests/test_dept_report_admission_target_discount.py -q
"""
import contextlib
import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib import dept_report as dr  # noqa: E402
from app.lib.config import operational_days_between  # noqa: E402

# 呼吸器内科（内科系・非外科）1科のみの最小フィクスチャ。room_per_week=0で
# leveling スコアを常に0にし、admissionトピックの足切り可否だけで主トピックが
# 決まるようにする（room<=0.5 の _fallback_move は dd={} でも安全に完結する）。
DEPT_UNITS = [
    {"name": "呼吸器内科", "room_per_week": 0.0, "retention": 0.90, "room_delta_4w": 0.0},
]
CAND = {"dept": [(u["name"], u["name"]) for u in DEPT_UNITS], "ward": []}

TARGETS = {
    "new_admission": {"dept": {"呼吸器内科": 20}, "ward": {}},
    "inpatient": {"dept": {}, "ward": {}, "ward_beds": {}},
}
SURG_TARGETS = {}
R7_INP = {"by_dept": {}, "by_ward": {}}
# 週目標20人・直近7日実績16人 → 素の不足スコア(1-16/20=0.2)は足切り(0.12)を超えるが、
# 割引後目標16.0に対しては不足スコア0.0で足切りを割り込む。
R7_NADM = {"by_dept": {"呼吸器内科": 16}, "by_ward": {}}
R7_SURG = {"by_dept": {}}

FAKE_PART = {"kind": "A", "name": "ダミー", "badge": None, "note": "", "is_dow": False,
             "_data": {"cur": [], "prev": [], "proj": None}, "_ref": 0, "_ref_label": "",
             "_unit": "", "_win": 1, "_color": "#000"}


def _wl(units):
    return {"units": copy.deepcopy(units), "total": {"retention": 0.8}}


def _run(base_date):
    """build_dept_report_contexts を最小フェイクで実行（診療科軸・呼吸器内科のみ）。"""
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


def _resp_ctx(contexts):
    return next(c for c in contexts if c["unit"] == "呼吸器内科")


class TestHolidayWeekDiscountFlipsTopicAndTags(unittest.TestCase):
    """ハッピーマンデー週（2026-01-18・直近7暦日窓の営業日=4）で割引が反映されること。"""

    BASE = pd.Timestamp("2026-01-18")

    def test_biz_days_precondition_is_four(self):
        self.assertEqual(
            operational_days_between(self.BASE - pd.Timedelta(days=6), self.BASE), 4)

    def test_topic_flips_from_admission_to_leveling(self):
        # 割引後目標16.0に対し実績16で不足ゼロ＝admissionの足切りスコアを割り込み、
        # 主トピックが leveling（既定）へ落ちる。素の目標のままなら admission のまま。
        ctx = _resp_ctx(_run(self.BASE))
        self.assertEqual(ctx["move"]["topic"], "leveling")

    def test_gap_tag_reflects_discounted_target(self):
        ctx = _resp_ctx(_run(self.BASE))
        # 割引後は不足なし＝gap階級タグが "met"（目標達成）になる（素の目標なら"poor"）。
        self.assertEqual(ctx["_state"]["na"], "met")

    def test_kpi_badge_reflects_discounted_target(self):
        ctx = _resp_ctx(_run(self.BASE))
        nadm_kpi = next(k for k in ctx["kpis"] if k["label"] == "新入院")
        self.assertEqual(nadm_kpi["val"], "16")
        self.assertTrue(nadm_kpi["ok"])                 # 16>=16(割引後)で達成扱いに変わる
        self.assertIn("16", nadm_kpi["tgt"])             # 割引後目標を併記
        self.assertIn("20", nadm_kpi["tgt"])             # 生目標も併記（両方出す）

    def test_matches_identity_patched_run_shows_admission_topic(self):
        # 旧相当（identity patch=割引なし）では不足0.2が足切りを超え admission が主トピック。
        with mock.patch.object(dr, "adjusted_weekly_target", side_effect=lambda t, bd: t):
            old_ctx = _resp_ctx(_run(self.BASE))
        self.assertEqual(old_ctx["move"]["topic"], "admission")
        new_ctx = _resp_ctx(_run(self.BASE))
        self.assertEqual(new_ctx["move"]["topic"], "leveling")
        self.assertNotEqual(old_ctx["move"]["topic"], new_ctx["move"]["topic"])


class TestOrdinaryWeekIsIdentity(unittest.TestCase):
    """通常週（2026-07-19・営業日5）は短絡により割引前と完全に同一であること。"""

    BASE = pd.Timestamp("2026-07-19")

    def test_biz_days_precondition_is_five(self):
        self.assertEqual(
            operational_days_between(self.BASE - pd.Timedelta(days=6), self.BASE), 5)

    def test_topic_and_tags_are_undiscounted(self):
        ctx = _resp_ctx(_run(self.BASE))
        self.assertEqual(ctx["move"]["topic"], "admission")
        self.assertEqual(ctx["_state"]["na"], "poor")
        nadm_kpi = next(k for k in ctx["kpis"] if k["label"] == "新入院")
        self.assertFalse(nadm_kpi["ok"])
        self.assertIn("20", nadm_kpi["tgt"])
        self.assertNotIn("16", nadm_kpi["tgt"])   # 割引前後が同一なので併記しない

    def test_matches_identity_patched_run(self):
        with mock.patch.object(dr, "adjusted_weekly_target", side_effect=lambda t, bd: t):
            old_ctx = _resp_ctx(_run(self.BASE))
        new_ctx = _resp_ctx(_run(self.BASE))
        self.assertEqual(old_ctx["_state"], new_ctx["_state"])
        self.assertEqual(old_ctx["kpis"], new_ctx["kpis"])
        self.assertEqual(old_ctx["move"]["topic"], new_ctx["move"]["topic"])


if __name__ == "__main__":
    unittest.main()
