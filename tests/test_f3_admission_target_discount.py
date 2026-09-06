"""F3是正: 新入院の週目標にも営業日期待値の割引(adjusted_weekly_target)を適用した
改修の回帰テスト（P1で全麻のみに適用していたものの横展開）。

対象:
  - triage.score_departments        : 科別トリアージの新入院ターゲット（triage.py:333）
  - metrics.build_dept_ranking       : 診療科別ランキング「達成状況」（metrics.py:787,792）
  - metrics.build_ward_ranking       : 病棟別ランキング「達成状況」（dept版と同一UIの axis 違い）

  1. 祝日週（ハッピーマンデー・営業日4）: 割引後の目標に対する達成度で
     判定（status/rate/ranking）が動くこと。
  2. 通常週（営業日5）: 割引が短絡で恒等になり、旧来と同一の値になること。

実行: リポジトリルートで
    OMLX_BASE_URL=http://127.0.0.1:9 .venv/bin/python -m pytest tests/test_f3_admission_target_discount.py -q
"""
import contextlib
import sys
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib import triage as tg  # noqa: E402
from app.lib import metrics as mt  # noqa: E402
from app.lib.config import operational_days_between  # noqa: E402

HOLIDAY_BASE = pd.Timestamp("2026-01-18")   # 直近7暦日窓の営業日=4（成人の日）
ORDINARY_BASE = pd.Timestamp("2026-07-19")  # 直近7暦日窓の営業日=5

TARGETS = {"new_admission": {"dept": {"呼吸器内科": 20}, "ward": {}},
          "inpatient": {"dept": {}, "ward": {}, "ward_beds": {}}}
SURG_TARGETS = {}


# ════════════════════════════════════════
# score_departments（triage.py:333）
# ════════════════════════════════════════

def _run_score_departments(base_date):
    patches = [
        mock.patch.object(tg, "rolling7_new_admission",
                          lambda adm, d: {"by_dept": {"呼吸器内科": 16}, "by_ward": {}}),
        mock.patch.object(tg, "rolling7_surgery", lambda surg, d: {"by_dept": {}, "total": 0}),
        mock.patch.object(tg, "daily_inpatient", lambda adm, d: {"by_dept": {}, "by_ward": {}}),
        mock.patch.object(tg, "rolling28_surgery_dept", lambda surg, d: {"by_dept": {}}),
        mock.patch.object(tg, "_get_profit_rates", lambda pm: {}),
        mock.patch.object(tg, "_census_trend", lambda *a, **k: (None, None)),
    ]
    with contextlib.ExitStack() as st:
        for p in patches:
            st.enter_context(p)
        results = tg.score_departments(pd.DataFrame(), pd.DataFrame(),
                                       TARGETS, SURG_TARGETS, None, base_date)
    return next(r for r in results if r["name"] == "呼吸器内科")


class TestScoreDepartmentsHolidayWeekDiscount(unittest.TestCase):
    """ハッピーマンデー週（2026-01-18・直近7暦日窓の営業日=4）で割引が反映されること。"""

    def test_biz_days_precondition_is_four(self):
        self.assertEqual(operational_days_between(HOLIDAY_BASE - pd.Timedelta(days=6), HOLIDAY_BASE), 4)

    def test_adm_target_and_rate_are_discounted(self):
        # 割引後目標 20*4/5=16.0 に対し実績16 → 達成率100%（素の目標のままなら80%=未達）。
        # 内科系で在院データが空のため、北極星(inp)が測れず新入院がフォールバック採用される。
        rec = _run_score_departments(HOLIDAY_BASE)
        self.assertAlmostEqual(rec["adm_target"], 16.0)
        self.assertAlmostEqual(rec["adm_rate"], 100.0)
        self.assertTrue(rec["primary_is_fallback"])
        self.assertEqual(rec["primary_rate"], 100.0)
        self.assertEqual(rec["status_kind"], "ok")   # 割引前(80%)なら below になっていた

    def test_matches_identity_patched_run(self):
        with mock.patch.object(tg, "adjusted_weekly_target", side_effect=lambda t, bd: t):
            old_rec = _run_score_departments(HOLIDAY_BASE)
        new_rec = _run_score_departments(HOLIDAY_BASE)
        self.assertAlmostEqual(old_rec["adm_target"], 20.0)
        self.assertAlmostEqual(old_rec["adm_rate"], 80.0)
        self.assertAlmostEqual(new_rec["adm_target"], 16.0)
        self.assertAlmostEqual(new_rec["adm_rate"], 100.0)


class TestScoreDepartmentsOrdinaryWeekIsIdentity(unittest.TestCase):
    """通常週（2026-07-19・営業日5）は短絡により割引前と完全に同一であること。"""

    def test_biz_days_precondition_is_five(self):
        self.assertEqual(operational_days_between(ORDINARY_BASE - pd.Timedelta(days=6), ORDINARY_BASE), 5)

    def test_matches_identity_patched_run(self):
        with mock.patch.object(tg, "adjusted_weekly_target", side_effect=lambda t, bd: t):
            old_rec = _run_score_departments(ORDINARY_BASE)
        new_rec = _run_score_departments(ORDINARY_BASE)
        self.assertEqual(old_rec, new_rec)


# ════════════════════════════════════════
# score_wards（triage.py:391・score_departments:335と同型）
# ════════════════════════════════════════

WARD_TARGETS = {"new_admission": {"ward": {"04A": 20}},
                "inpatient": {"dept": {}, "ward": {}}}


def _run_score_wards(base_date):
    patches = [
        mock.patch.object(tg, "rolling7_new_admission",
                          lambda adm, d: {"by_dept": {}, "by_ward": {"04A": 16}}),
        mock.patch.object(tg, "daily_inpatient", lambda adm, d: {"by_dept": {}, "by_ward": {}}),
        mock.patch.object(tg, "_census_trend", lambda *a, **k: (None, None)),
    ]
    with contextlib.ExitStack() as st:
        for p in patches:
            st.enter_context(p)
        results = tg.score_wards(pd.DataFrame(), WARD_TARGETS, base_date)
    return next(r for r in results if r["ward_code"] == "04A")


class TestScoreWardsHolidayWeekDiscount(unittest.TestCase):
    """病棟トリアージの新入院目標も score_departments と同型で割引が反映されること
    （ハッピーマンデー週・直近7暦日窓の営業日=4）。"""

    def test_biz_days_precondition_is_four(self):
        self.assertEqual(operational_days_between(HOLIDAY_BASE - pd.Timedelta(days=6), HOLIDAY_BASE), 4)

    def test_adm_target_and_rate_are_discounted(self):
        # 割引後目標 20*4/5=16.0 に対し実績16 → 達成率100%（素の目標のままなら80%=未達）。
        rec = _run_score_wards(HOLIDAY_BASE)
        self.assertAlmostEqual(rec["adm_target"], 16.0)
        self.assertAlmostEqual(rec["adm_rate"], 100.0)

    def test_matches_identity_patched_run_shows_5_over_4_relationship(self):
        with mock.patch.object(tg, "adjusted_weekly_target", side_effect=lambda t, bd: t):
            old_rec = _run_score_wards(HOLIDAY_BASE)
        new_rec = _run_score_wards(HOLIDAY_BASE)
        self.assertAlmostEqual(old_rec["adm_target"], 20.0)
        self.assertAlmostEqual(new_rec["adm_target"], 16.0)
        # 割引後目標=生目標×4/5（＝生目標は割引後目標の5/4倍）
        self.assertAlmostEqual(old_rec["adm_target"], new_rec["adm_target"] * 5 / 4)
        self.assertAlmostEqual(new_rec["adm_rate"], old_rec["adm_rate"] * 5 / 4)


class TestScoreWardsOrdinaryWeekIsIdentity(unittest.TestCase):
    """通常週（2026-07-19・営業日5）は短絡により割引前と完全に同一であること。"""

    def test_biz_days_precondition_is_five(self):
        self.assertEqual(operational_days_between(ORDINARY_BASE - pd.Timedelta(days=6), ORDINARY_BASE), 5)

    def test_matches_identity_patched_run(self):
        with mock.patch.object(tg, "adjusted_weekly_target", side_effect=lambda t, bd: t):
            old_rec = _run_score_wards(ORDINARY_BASE)
        new_rec = _run_score_wards(ORDINARY_BASE)
        self.assertEqual(old_rec, new_rec)


# ════════════════════════════════════════
# build_dept_ranking / build_ward_ranking（metrics.py:787,792 とその ward 版）
# ════════════════════════════════════════

def _adm_df(base_date, dept="呼吸器内科", ward="04A", n=16):
    """直近7暦日窓のうち先頭n件ぶんの日に1件ずつ新入院を計上（実績=n）。"""
    dates = pd.date_range(base_date - pd.Timedelta(days=6), base_date, freq="D")
    rows = []
    for i in range(n):
        d = dates[i % len(dates)]
        rows.append({"日付": d, "新入院患者数": 1, "新入院患者数_病棟": 1, "在院患者数": 0,
                    "科_表示": True, "診療科名": dept,
                    "病棟_表示": True, "病棟コード": ward})
    return pd.DataFrame(rows)


class TestDeptRankingHolidayWeekDiscount(unittest.TestCase):
    BASE = HOLIDAY_BASE

    def test_rate_status_and_adj_columns(self):
        adm = _adm_df(self.BASE, n=16)
        df = mt.build_dept_ranking(adm, self.BASE, TARGETS, metric="new_admission")
        row = df[df["診療科"] == "呼吸器内科"].iloc[0]
        self.assertEqual(row["目標"], 20)                 # 生目標は維持
        self.assertAlmostEqual(row["目標_adj"], 16.0)       # ★新設: 割引後目標
        self.assertEqual(row["biz_days"], 4)
        self.assertEqual(row["biz_days_full"], 5)
        self.assertAlmostEqual(row["達成率"], 100.0)        # 判定は割引後目標を使う
        self.assertEqual(row["status"], "ok")

    def test_inpatient_metric_has_no_adj_columns(self):
        # 在院はF3の対象外＝目標_adjを新設しない（既存の列構成のまま）。
        adm = _adm_df(self.BASE, n=16)
        df = mt.build_dept_ranking(adm, self.BASE, TARGETS, metric="inpatient")
        self.assertNotIn("目標_adj", df.columns)


class TestDeptRankingOrdinaryWeekIsIdentity(unittest.TestCase):
    BASE = ORDINARY_BASE

    def test_rate_matches_undiscounted_target(self):
        adm = _adm_df(self.BASE, n=16)
        df = mt.build_dept_ranking(adm, self.BASE, TARGETS, metric="new_admission")
        row = df[df["診療科"] == "呼吸器内科"].iloc[0]
        self.assertAlmostEqual(row["目標_adj"], 20.0)
        self.assertAlmostEqual(row["達成率"], 80.0)
        self.assertEqual(row["status"], "danger")


class TestWardRankingMirrorsDeptRanking(unittest.TestCase):
    """病棟別ランキングも dept 版と同じ割引ロジックを持つこと（同一UIの軸トグル）。"""

    def test_holiday_week_ward_target_is_discounted(self):
        targets = {"new_admission": {"ward": {"04A": 20}},
                  "inpatient": {"dept": {}, "ward": {}, "ward_beds": {}}}
        adm = _adm_df(HOLIDAY_BASE, ward="04A", n=16)
        df = mt.build_ward_ranking(adm, HOLIDAY_BASE, targets, metric="new_admission")
        row = df[df["病棟コード"] == "04A"].iloc[0]
        self.assertAlmostEqual(row["目標_adj"], 16.0)
        self.assertAlmostEqual(row["達成率"], 100.0)


# ════════════════════════════════════════
# dept_report._build_parts の「B: 新入院」バッジ（dept_report.py:1091 の対＝1081-1083）
# ════════════════════════════════════════

from app.lib import dept_report as dr  # noqa: E402


class TestBuildPartsAdmissionBadgeDiscount(unittest.TestCase):
    """_build_parts の新入院バッジ(parts["B"]["badge"])が割引後目標を使うこと
    （チャートの目標線=daily_na_tgt は生目標のまま=flat基準線・Cブロックの全麻と同型）。"""

    def _parts(self, base_date):
        adm = _adm_df(base_date, n=16)
        return dr._build_parts(adm, pd.DataFrame(), base_date, "dept", "呼吸器内科", "呼吸器内科",
                               dd=None, r7_inp={"by_dept": {}, "by_ward": {}},
                               r7_nadm={"by_dept": {"呼吸器内科": 16}, "by_ward": {}},
                               r7_surg={"by_dept": {}}, targets=TARGETS, surg_targets=SURG_TARGETS,
                               profit_series=None)

    def test_holiday_week_badge_uses_discounted_target(self):
        parts = self._parts(HOLIDAY_BASE)
        badge = parts["B"]["badge"]
        self.assertIsNotNone(badge)
        self.assertEqual(badge, ("達成率 100%", "ok"))   # 16/16(割引後)=100%（生目標なら80%=wr）
        # 目標線(flat)は生目標のまま(20/7≒2.9)。バッジのみ割引される。
        self.assertAlmostEqual(parts["B"]["_ref"], round(20 / 7, 1))

    def test_ordinary_week_badge_matches_undiscounted_target(self):
        parts = self._parts(ORDINARY_BASE)
        badge = parts["B"]["badge"]
        self.assertEqual(badge, ("達成率 80%", "wr"))


if __name__ == "__main__":
    unittest.main()
