"""病院全体サマリー（hospital_summary.py）: 週目標の暦補正割引（P3-1横展開）の回帰テスト。

build_summary_context の診療科テーブル用データ（dept_rows の surg_actual/surg_target/
surg_rate）は metrics.build_surgery_ranking を経由するため、triage.py/dept_report.py と
同じ adjusted_weekly_target（週目標 × 直近7暦日窓の営業日数/5）を、この呼び出し1箇所の
入力（surg_targets 辞書）へ適用する改修の検証。build_surgery_ranking 自体は改変しない
（他の呼び出し元＝ランキング用途には影響させない）。

  1. 祝日週（ハッピーマンデー・営業日4）: 割引後の目標に対する達成度で
     surg_target/surg_rate が動くこと（dept_report版と同じ数値になること）。
  2. 通常週（営業日5）: 割引が短絡で恒等になり、旧来と同一の値になること。

build_hero_text・build_kpi_summary・build_ward/dept_ranking・discharge_dow_profile・
_flow_7d など、本改修と無関係な重い依存はフェイクに差し替える
（build_surgery_ranking だけは実物を使い、adjusted_weekly_target のみ monkeypatch で
「旧相当」と比較する）。

実行: リポジトリルートで
    python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib import hospital_summary as hs  # noqa: E402
from app.lib import triage as triage_mod  # noqa: E402
from app.lib.config import operational_days_between  # noqa: E402

TARGETS = {
    "new_admission": {"dept": {}, "ward": {}},
    "inpatient": {"dept": {}, "ward": {}, "ward_beds": {}},
}
# 週目標20件・直近7日実績16件 → 素の達成率80%。
SURG_TARGETS = {"整形外科": 20}


def _surg_df(base_date, n=16):
    """直近7暦日窓のうち先頭 n 件ぶんの日に1件ずつ全麻を計上（実績=n）。"""
    dates = pd.date_range(base_date - pd.Timedelta(days=6), base_date, freq="D")
    rows = []
    for i in range(n):
        d = dates[i % len(dates)]
        rows.append({"手術実施日": d, "実施診療科": "整形外科", "全麻": True, "術数対象": True})
    return pd.DataFrame(rows)


def _run(base_date):
    patches = [
        mock.patch.object(hs, "build_hero_text", lambda *a, **k: {"headline": "", "body": "", "chips": []}),
        mock.patch.object(hs.metrics, "build_kpi_summary", lambda *a, **k: {}),
        mock.patch.object(hs, "_ma_series", lambda *a, **k: {"dates": [], "cur": [], "prev": []}),
        mock.patch.object(hs, "_surg_series", lambda *a, **k: {"dates": [], "cur": [], "prev": []}),
        mock.patch.object(hs.metrics, "build_ward_ranking", lambda *a, **k: pd.DataFrame()),
        mock.patch.object(hs.metrics, "weekend_census_retention", lambda *a, **k: {"units": [], "total": {}}),
        mock.patch.object(hs, "_flow_7d", lambda *a, **k: {}),
        mock.patch.object(hs.metrics, "build_dept_ranking", lambda *a, **k: pd.DataFrame()),
        mock.patch.object(hs.metrics, "discharge_dow_profile", lambda *a, **k: {"redistribution": None}),
    ]
    from contextlib import ExitStack
    with ExitStack() as st:
        for p in patches:
            st.enter_context(p)
        surg = _surg_df(base_date)
        return hs.build_summary_context(pd.DataFrame(), surg, TARGETS, SURG_TARGETS, base_date,
                                        profit_monthly=None, profit_breakdown=None)


def _seikei_row(ctx):
    return next(r for r in ctx["dept_rows"] if r["name"] == "整形外科")


class TestHolidayWeekDiscountAppliesToSurgeryTable(unittest.TestCase):
    """ハッピーマンデー週（2026-01-18・直近7暦日窓の営業日=4）で割引が反映されること。"""

    BASE = pd.Timestamp("2026-01-18")

    def test_biz_days_precondition_is_four(self):
        self.assertEqual(
            operational_days_between(self.BASE - pd.Timedelta(days=6), self.BASE), 4)

    def test_surg_target_and_rate_are_discounted(self):
        # 割引後目標 20*4/5=16.0 に対し実績16 → 達成率100%（dept_report版と同じ値）。
        row = _seikei_row(_run(self.BASE))
        self.assertEqual(row["surg_actual"], 16)
        self.assertAlmostEqual(row["surg_target"], 16.0)
        self.assertAlmostEqual(row["surg_rate"], 100.0)

    def test_matches_identity_patched_run(self):
        """旧相当（identity patch）では素の目標20のまま・達成率80%になること
        （割引適用前後の差分が dept_report 検証と同じ方向であることの確認）。"""
        with mock.patch.object(triage_mod, "adjusted_weekly_target", side_effect=lambda t, bd: t):
            old_row = _seikei_row(_run(self.BASE))
        new_row = _seikei_row(_run(self.BASE))
        self.assertAlmostEqual(old_row["surg_target"], 20.0)
        self.assertAlmostEqual(old_row["surg_rate"], 80.0)
        self.assertAlmostEqual(new_row["surg_target"], 16.0)
        self.assertAlmostEqual(new_row["surg_rate"], 100.0)
        self.assertEqual(old_row["surg_actual"], new_row["surg_actual"])


class TestOrdinaryWeekIsIdentity(unittest.TestCase):
    """通常週（2026-07-19・営業日5）は短絡により割引前と完全に同一であること。"""

    BASE = pd.Timestamp("2026-07-19")

    def test_biz_days_precondition_is_five(self):
        self.assertEqual(
            operational_days_between(self.BASE - pd.Timedelta(days=6), self.BASE), 5)

    def test_surg_target_and_rate_are_undiscounted(self):
        row = _seikei_row(_run(self.BASE))
        self.assertAlmostEqual(row["surg_target"], 20.0)
        self.assertAlmostEqual(row["surg_rate"], 80.0)

    def test_matches_identity_patched_run(self):
        with mock.patch.object(triage_mod, "adjusted_weekly_target", side_effect=lambda t, bd: t):
            old_row = _seikei_row(_run(self.BASE))
        new_row = _seikei_row(_run(self.BASE))
        self.assertEqual(old_row, new_row)


if __name__ == "__main__":
    unittest.main()
