"""triage.py P2 ステップ6（判定への配線）の回帰テスト。

対象:
  - _triage_status               : trend_dir=None のとき watch へ昇格しないこと（連休明けの保証）
  - _census_trend / _surgery_trend: unit_sigma.arrow_threshold を正しい kind/unit で呼ぶこと
  - score_departments/score_wards : 内科系=dept_census、外科系=dept_surgery(dept/surg付き)、
                                    病棟=ward_census で _census_trend/_surgery_trend を呼ぶこと
  - SURGERY_TREND_MIN_28D=40 が全麻ノイズ除去ゲートとして効いていること

実行: リポジトリルートで
    .venv/bin/python -m pytest -q tests/test_triage_p2_wiring.py
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib import triage as tg  # noqa: E402

BASE = pd.Timestamp("2026-03-06")   # 通常週（祝日なし・biz7=5・biz28=20）


# ════════════════════════════════════════
# _triage_status: trend_dir=None は watch へ昇格しない
# ════════════════════════════════════════

class TriageStatusNoneGuardTest(unittest.TestCase):
    def test_none_trend_dir_does_not_escalate_to_watch(self):
        # 達成中(rate>=90)・trend_dir=None（連休明け等で判定保留）→ ok のまま
        status_kind, priority = tg._triage_status(95.0, None)
        self.assertEqual(status_kind, "ok")
        self.assertEqual(priority, "low")

    def test_down_trend_dir_does_escalate_to_watch(self):
        # 対照: down なら watch へ昇格すること（None特有の挙動でないことの確認）
        status_kind, priority = tg._triage_status(95.0, "down")
        self.assertEqual(status_kind, "watch")
        self.assertEqual(priority, "mid")

    def test_none_trend_dir_below_threshold_is_plain_below(self):
        # 未達(<90)・trend_dir=None → below（改善傾向による優先度降格は起きない）
        status_kind, priority = tg._triage_status(70.0, None)
        self.assertEqual(status_kind, "below")
        self.assertEqual(priority, "high")


# ════════════════════════════════════════
# _census_trend / _census_trend_from_series: kind/unit を正しく arrow_threshold へ渡す
# ════════════════════════════════════════

def _flat_census_series(base_date, value=100.0, days_back=34):
    dates = pd.date_range(base_date - pd.Timedelta(days=days_back), base_date, freq="D")
    return pd.DataFrame({"日付": dates, "値": [value] * len(dates)})


class CensusTrendArrowThresholdWiringTest(unittest.TestCase):
    def test_dept_census_kind_and_unit_forwarded(self):
        series = _flat_census_series(BASE)
        with mock.patch.object(tg, "build_daily_series", return_value=series), \
             mock.patch.object(tg, "arrow_threshold", return_value=3.0) as m:
            tg._census_trend(pd.DataFrame(), BASE, "診療科名", "テスト内科", kind="dept_census")
        m.assert_called_once_with("dept_census", "テスト内科")

    def test_ward_census_kind_and_unit_forwarded(self):
        series = _flat_census_series(BASE)
        with mock.patch.object(tg, "build_daily_series", return_value=series), \
             mock.patch.object(tg, "arrow_threshold", return_value=3.0) as m:
            tg._census_trend(pd.DataFrame(), BASE, "病棟コード", "04A", kind="ward_census")
        m.assert_called_once_with("ward_census", "04A")


class SurgeryTrendArrowThresholdWiringTest(unittest.TestCase):
    def test_dept_surgery_kind_and_dept_forwarded(self):
        with mock.patch.object(tg, "arrow_threshold", return_value=15.0) as m, \
             mock.patch.object(tg, "rolling28_surgery_dept",
                               return_value={"by_dept": {"整形外科": 45}}):
            tg._surgery_trend(45, 45, BASE, dept="整形外科", surg=pd.DataFrame())
        m.assert_called_once_with("dept_surgery", "整形外科")

    def test_no_dept_surg_falls_back_to_fixed_threshold_behavior(self):
        # 後方互換: dept/surg 省略時は unit_sigma を一切呼ばない（P1までの挙動のまま）
        with mock.patch.object(tg, "arrow_threshold") as m:
            spread, direction = tg._surgery_trend(45, 30, BASE)
        m.assert_not_called()
        self.assertIsNotNone(spread)
        self.assertEqual(direction, tg._trend_dir(spread, tg.SURGERY_TREND_PT))

    def test_min_28d_gate_is_40(self):
        # SURGERY_TREND_MIN_28D は P2 で 8→40 に変更済み
        self.assertEqual(tg.SURGERY_TREND_MIN_28D, 40)
        spread, direction = tg._surgery_trend(39, 39, BASE)
        self.assertIsNone(spread)
        self.assertIsNone(direction)
        spread, direction = tg._surgery_trend(40, 40, BASE)
        self.assertIsNotNone(spread)


# ════════════════════════════════════════
# score_departments / score_wards: 呼び分けの配線（内科系=census/dept_census、
# 外科系=surgery/dept_surgery+surg、病棟=census/ward_census）
# ════════════════════════════════════════

TARGETS = {"new_admission": {"dept": {}, "ward": {}},
          "inpatient": {"dept": {}, "ward": {}, "ward_beds": {}}}
SURG_TARGETS = {}


class ScoreDepartmentsKindWiringTest(unittest.TestCase):
    def test_internal_dept_uses_census_trend_with_dept_census_kind(self):
        surg_df = pd.DataFrame()
        with mock.patch.object(tg, "rolling7_new_admission",
                               lambda adm, d: {"by_dept": {}, "by_ward": {}}), \
             mock.patch.object(tg, "rolling7_surgery", lambda surg, d: {"by_dept": {}, "total": 0}), \
             mock.patch.object(tg, "daily_inpatient", lambda adm, d: {"by_dept": {}, "by_ward": {}}), \
             mock.patch.object(tg, "rolling28_surgery_dept", lambda surg, d: {"by_dept": {}}), \
             mock.patch.object(tg, "_get_profit_rates", lambda pm: {}), \
             mock.patch.object(tg, "_census_trend", return_value=(None, None)) as m_census, \
             mock.patch.object(tg, "_surgery_trend", return_value=(None, None)):
            tg.score_departments(pd.DataFrame(), surg_df, TARGETS, SURG_TARGETS, None, BASE)

        calls = {c.args[3]: c.kwargs for c in m_census.call_args_list}
        self.assertIn("総合内科", calls)   # 内科系（非外科）
        self.assertEqual(calls["総合内科"].get("kind"), "dept_census")

    def test_surgery_dept_uses_surgery_trend_with_dept_and_surg(self):
        surg_df = pd.DataFrame({"手術実施日": pd.Series([], dtype="datetime64[ns]")})
        with mock.patch.object(tg, "rolling7_new_admission",
                               lambda adm, d: {"by_dept": {}, "by_ward": {}}), \
             mock.patch.object(tg, "rolling7_surgery", lambda surg, d: {"by_dept": {}, "total": 0}), \
             mock.patch.object(tg, "daily_inpatient", lambda adm, d: {"by_dept": {}, "by_ward": {}}), \
             mock.patch.object(tg, "rolling28_surgery_dept", lambda surg, d: {"by_dept": {}}), \
             mock.patch.object(tg, "_get_profit_rates", lambda pm: {}), \
             mock.patch.object(tg, "_census_trend", return_value=(None, None)), \
             mock.patch.object(tg, "_surgery_trend", return_value=(None, None)) as m_surg:
            tg.score_departments(pd.DataFrame(), surg_df, TARGETS, SURG_TARGETS, None, BASE)

        calls = {c.kwargs.get("dept"): c.kwargs for c in m_surg.call_args_list}
        self.assertIn("整形外科", calls)   # 外科系
        self.assertIs(calls["整形外科"].get("surg"), surg_df)


class ScoreWardsKindWiringTest(unittest.TestCase):
    def test_ward_uses_census_trend_with_ward_census_kind(self):
        with mock.patch.object(tg, "rolling7_new_admission",
                               lambda adm, d: {"by_dept": {}, "by_ward": {}}), \
             mock.patch.object(tg, "daily_inpatient", lambda adm, d: {"by_dept": {}, "by_ward": {}}), \
             mock.patch.object(tg, "_census_trend", return_value=(None, None)) as m_census:
            tg.score_wards(pd.DataFrame(), TARGETS, BASE)

        calls = {c.args[3]: c.kwargs for c in m_census.call_args_list}
        self.assertIn("04A", calls)
        self.assertEqual(calls["04A"].get("kind"), "ward_census")


if __name__ == "__main__":
    unittest.main()
