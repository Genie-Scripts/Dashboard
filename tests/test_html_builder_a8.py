"""A8-8.3(在院の必要ペース)のユニットテスト。

`html_builder._build_inpatient_pace()` が
  - 未達時のみ返す（達成/超過はNone）
  - 禁止語（退院を早める類の示唆）を一切含まない
  - 主文80字以内・全体120字以内
  - 「維持率」「新入院換算」の両語彙を含む（主=週末在院維持率換算・副=新入院換算）
ことを合成データで確認する。実在の診療科名・病棟名は使わない（PUBLICリポ配慮）。
"""
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.html_builder import _build_inpatient_pace, INPATIENT_PACE_BANNED_TERMS

BASE = pd.Timestamp("2026-09-03")


def _synth_adm(base: pd.Timestamp, weekday_census=600, weekend_census=550,
              daily_admissions=80) -> pd.DataFrame:
    """weekend_census_retention（直近8完全週）と _alos_28d（直近28日）の両方を
    満たす最小合成 adm（単一病棟W1・単一行/日）。"""
    monday = base - pd.Timedelta(days=base.weekday())
    idx = pd.date_range(monday - pd.Timedelta(days=56), base, freq="D")
    rows = []
    for d in idx:
        we = d.weekday() >= 5
        rows.append({
            "日付": d, "病棟_表示": True, "病棟コード": "W1",
            "在院患者数": weekend_census if we else weekday_census,
            "新入院患者数": daily_admissions,
        })
    return pd.DataFrame(rows)


class TestBuildInpatientPace(unittest.TestCase):
    def test_none_when_achieved_or_zero_gap(self):
        adm = _synth_adm(BASE)
        self.assertIsNone(_build_inpatient_pace(adm, BASE, {"inpatient_gap": 0}))
        self.assertIsNone(_build_inpatient_pace(adm, BASE, {"inpatient_gap": 4.0}))

    def test_none_when_gap_missing(self):
        adm = _synth_adm(BASE)
        self.assertIsNone(_build_inpatient_pace(adm, BASE, {}))

    def test_text_has_no_banned_terms(self):
        adm = _synth_adm(BASE)
        text = _build_inpatient_pace(adm, BASE, {"inpatient_gap": -8.0})
        self.assertIsNotNone(text)
        for term in INPATIENT_PACE_BANNED_TERMS:
            self.assertNotIn(term, text, f"禁止語が含まれている: {term}")
        self.assertNotIn("退院", text)   # 「退院」を含む語がそもそも一切出ない

    def test_text_has_both_retention_and_admission_vocab(self):
        adm = _synth_adm(BASE)
        text = _build_inpatient_pace(adm, BASE, {"inpatient_gap": -8.0})
        self.assertIn("維持率", text)          # 主文=週末在院維持率換算
        self.assertIn("新入院換算", text)      # 副文=新入院換算（括弧補足）

    def test_length_within_budget(self):
        adm = _synth_adm(BASE)
        text = _build_inpatient_pace(adm, BASE, {"inpatient_gap": -20.0})
        main = text.split("（", 1)[0]
        self.assertLessEqual(len(main), 80, f"主文が80字を超えている: {len(main)}字")
        self.assertLessEqual(len(text), 120, f"全体が120字を超えている: {len(text)}字")

    def test_none_when_retention_unavailable(self):
        """在院データが無ければ weekend_census_retention が空返却→None（無害縮退）。"""
        empty = pd.DataFrame(columns=["日付", "病棟_表示", "病棟コード", "在院患者数", "新入院患者数"])
        self.assertIsNone(_build_inpatient_pace(empty, BASE, {"inpatient_gap": -5.0}))


if __name__ == "__main__":
    unittest.main()
