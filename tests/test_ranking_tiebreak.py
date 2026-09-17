"""§9 #12是正: build_dept_ranking / build_ward_ranking / build_surgery_ranking の
同率タイブレーク非決定性の回帰テスト（標準ライブラリ unittest・追加依存なし）。

対象:
  - metrics.build_dept_ranking    (診療科名 昇順が第2キー)
  - metrics.build_ward_ranking    (病棟名 昇順が第2キー)
  - metrics.build_surgery_ranking (診療科名 昇順が第2キー)

改修前は同率（達成率 or 実績）のタイブレークが set/dict の走査順（＝
PYTHONHASHSEED依存）に委ねられており、日々のビルドでランキング順位が
入れ替わっていた（例: build_surgery_ranking は SURGERY_EVAL_DEPTS が set）。
改修後は主キー（達成率 or 実績）降順・第2キー（名前列）昇順の複合ソートで、
入力の走査順に依らず決定的な順序になることを保証する。

実行: リポジトリルートで
    python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib import metrics as mt  # noqa: E402

BASE = pd.Timestamp("2026-07-19")  # 通常週（営業日5、F3割引が恒等になる週）


def _adm_tied_depts(dept_a="内科", dept_b="外科", actual=10):
    """直近7暦日、2診療科（2病棟）とも在院/新入院ともに同値=同率タイになる adm を作る。"""
    dates = pd.date_range(BASE - pd.Timedelta(days=6), BASE, freq="D")
    rows = []
    for d in dates:
        rows.append({"日付": d, "在院患者数": actual, "新入院患者数": actual // 7 or 1,
                    "新入院患者数_病棟": actual // 7 or 1,
                    "科_表示": True, "診療科名": dept_a,
                    "病棟_表示": True, "病棟コード": "05A"})
        rows.append({"日付": d, "在院患者数": actual, "新入院患者数": actual // 7 or 1,
                    "新入院患者数_病棟": actual // 7 or 1,
                    "科_表示": True, "診療科名": dept_b,
                    "病棟_表示": True, "病棟コード": "06A"})
    return pd.DataFrame(rows)


def _adm_tied_depts_reversed():
    """同じデータを、行の構築順を入れ替えて作る（内科・外科の登場順を逆転）。"""
    df = _adm_tied_depts()
    return df.iloc[::-1].reset_index(drop=True)


DEPT_TARGETS = {"inpatient": {"dept": {"内科": 10, "外科": 10}, "ward": {}, "ward_beds": {}},
                "new_admission": {"dept": {}, "ward": {}}}
WARD_TARGETS = {"inpatient": {"dept": {}, "ward": {"05A": 10, "06A": 10}, "ward_beds": {}},
                "new_admission": {"dept": {}, "ward": {}}}


class BuildDeptRankingTiebreakTest(unittest.TestCase):
    """内科・外科が同率（達成率100%・実績10）でタイ → 診療科名昇順（内科→外科）。"""

    def _ranks(self, adm, sort_by):
        df = mt.build_dept_ranking(adm, BASE, DEPT_TARGETS, metric="inpatient",
                                   sort_by=sort_by)
        return list(zip(df["順位"], df["診療科"]))

    def test_achievement_tie_is_name_ascending_and_order_independent(self):
        forward = self._ranks(_adm_tied_depts(), "achievement")
        reversed_ = self._ranks(_adm_tied_depts_reversed(), "achievement")
        self.assertEqual(forward, reversed_)
        self.assertEqual(forward, [(1, "内科"), (2, "外科")])

    def test_actual_tie_is_name_ascending_and_order_independent(self):
        forward = self._ranks(_adm_tied_depts(), "actual")
        reversed_ = self._ranks(_adm_tied_depts_reversed(), "actual")
        self.assertEqual(forward, reversed_)
        self.assertEqual(forward, [(1, "内科"), (2, "外科")])


class BuildWardRankingTiebreakTest(unittest.TestCase):
    """05A（5階A病棟）・06A（6階A病棟）が同率でタイ → 病棟名昇順（5階A→6階A）。"""

    def _ranks(self, adm, sort_by):
        df = mt.build_ward_ranking(adm, BASE, WARD_TARGETS, metric="inpatient",
                                   sort_by=sort_by)
        return list(zip(df["順位"], df["病棟名"]))

    def test_achievement_tie_is_name_ascending_and_order_independent(self):
        forward = self._ranks(_adm_tied_depts(), "achievement")
        reversed_ = self._ranks(_adm_tied_depts_reversed(), "achievement")
        self.assertEqual(forward, reversed_)
        self.assertEqual(forward, [(1, "5階A病棟"), (2, "6階A病棟")])

    def test_actual_tie_is_name_ascending_and_order_independent(self):
        forward = self._ranks(_adm_tied_depts(), "actual")
        reversed_ = self._ranks(_adm_tied_depts_reversed(), "actual")
        self.assertEqual(forward, reversed_)
        self.assertEqual(forward, [(1, "5階A病棟"), (2, "6階A病棟")])


def _surg_tied_depts(count=3):
    """直近7暦日、2診療科とも術数対象=count件で同率タイになる surg を作る。"""
    dates = pd.date_range(BASE - pd.Timedelta(days=6), BASE, freq="D")[:count]
    rows = []
    for d in dates:
        rows.append({"手術実施日": d, "実施診療科": "内科", "全麻": True, "術数対象": True})
        rows.append({"手術実施日": d, "実施診療科": "外科", "全麻": True, "術数対象": True})
    return pd.DataFrame(rows)


SURG_TARGETS = {"内科": 3, "外科": 3}


class BuildSurgeryRankingTiebreakTest(unittest.TestCase):
    """SURGERY_EVAL_DEPTS の走査順（set由来）に依らず、同率タイは診療科名昇順になる。"""

    def _ranks(self, eval_depts_order, sort_by):
        with mock.patch.object(mt, "SURGERY_EVAL_DEPTS", eval_depts_order):
            df = mt.build_surgery_ranking(_surg_tied_depts(), BASE, SURG_TARGETS,
                                          sort_by=sort_by, period="7")
        return list(zip(df["順位"], df["診療科"]))

    def test_achievement_tie_is_name_ascending_and_order_independent(self):
        forward = self._ranks(["外科", "内科"], "achievement")
        reversed_ = self._ranks(["内科", "外科"], "achievement")
        self.assertEqual(forward, reversed_)
        self.assertEqual(forward, [(1, "内科"), (2, "外科")])

    def test_actual_tie_is_name_ascending_and_order_independent(self):
        forward = self._ranks(["外科", "内科"], "actual")
        reversed_ = self._ranks(["内科", "外科"], "actual")
        self.assertEqual(forward, reversed_)
        self.assertEqual(forward, [(1, "内科"), (2, "外科")])


if __name__ == "__main__":
    unittest.main()
