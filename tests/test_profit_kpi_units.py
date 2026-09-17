"""
tests/test_profit_kpi_units.py — build_profit_kpi() の単位併存改修
(spec/改修プラン_粗利の単位あたり指標.md §9 #8) の回帰テスト。

改修内容:
  - 日次ペース系のキーを百万円へ一本化（`_mm` サフィックス付きにリネーム、
    旧キー `daily_pace` / `daily_target` / `hospital_daily_pace` 等は削除）。
  - 病院レベルは小数1桁、科レベルは小数2桁。
  - `pace_rate` / `gairai_pace_rate` / `nyuin_pace_rate` は Python 側で
    未丸めの日次ペース・日次目標から算出し、表示用の丸め後の値からは作らない。
  - 丸めで -0.0 になった値は 0.0 に正規化する。

合成フィクスチャのみを使う（実データ・ネットワーク・LLM は一切使わない）。

実行: リポジトリルートで
    .venv/bin/python -m unittest tests/test_profit_kpi_units.py -v
"""
import math
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.profit import build_profit_kpi  # noqa: E402
from app.lib.config import biz_days_in_month, calendar_days_in_month  # noqa: E402

BASE_MONTH = pd.Timestamp("2026-08-01")
BIZ = biz_days_in_month(BASE_MONTH)
CAL = calendar_days_in_month(BASE_MONTH)

# 整形C: pace_rate が「未丸め値の比」であって「2桁丸め後の値の比」ではないことを
# 検知するための意図的な半端な数値（千円）。
#   dp_raw = 100098/20/1000 = 5.0049 → 2桁丸め 5.00
#   dt_raw = 110102/20/1000 = 5.5051 → 2桁丸め 5.51
#   → 未丸め比=90.9% だが、丸め後の値どうしの比だと90.7%になる（乖離を検知）。
_C_PROFIT = 100098.0
_C_TARGET = 110102.0

# 皮膚D: 粗利をわずかに負にして daily_pace_mm / pace_rate が丸めで -0.0 になる
# ケースを意図的に作る（-1千円 / 20営業日 / 1000 = -0.00005 → round(.,2) = -0.0）。
_D_PROFIT = -1.0
_D_TARGET = 10000.0


def _row(name, profit, target, gairai=None, nyuin=None,
         gairai_tgt=None, nyuin_tgt=None, ach=100.0):
    return {
        "診療科名": name, "月": BASE_MONTH,
        "粗利": profit, "月次目標": target, "月次補正目標": target,
        "達成率": ach, "達成率_改定換算": ach, "前月比": np.nan,
        "当月営業日数": BIZ, "当月暦日数": CAL,
        "外来粗利": gairai, "入院粗利": nyuin,
        "外来目標": gairai_tgt, "入院目標": nyuin_tgt,
    }


def _build_profit_monthly() -> pd.DataFrame:
    rows = [
        _row("内科A", 200000.0, 180000.0, gairai=40000.0, nyuin=160000.0,
             gairai_tgt=35000.0, nyuin_tgt=145000.0, ach=111.1),
        _row("外科B", 121903.0, 115000.0, gairai=30000.0, nyuin=91903.0,
             gairai_tgt=25000.0, nyuin_tgt=90000.0, ach=106.0),
        _row("整形C", _C_PROFIT, _C_TARGET, ach=90.9),
        _row("皮膚D", _D_PROFIT, _D_TARGET, ach=-0.01),
    ]
    return pd.DataFrame(rows)


def _is_neg_zero(v) -> bool:
    return v is not None and v == 0.0 and math.copysign(1.0, v) == -1.0


class TestProfitKpiUnits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profit_monthly = _build_profit_monthly()
        cls.kpi = build_profit_kpi(cls.profit_monthly, base_month=BASE_MONTH)
        cls.by_name = {}
        for d in cls.kpi["top3"] + cls.kpi["bottom3"]:
            cls.by_name.setdefault(d["name"], d)

    # ---- (a) 旧キーが一切残っていない ----
    def test_old_hospital_keys_absent(self):
        old_keys = [
            "hospital_daily_pace", "hospital_daily_target",
            "hospital_gairai_daily_pace", "hospital_nyuin_daily_pace",
            "hospital_gairai_daily_target", "hospital_nyuin_daily_target",
        ]
        for k in old_keys:
            self.assertNotIn(k, self.kpi, f"旧キー {k} が残存")
        # 新キーは存在すること
        for k in ["hospital_daily_pace_mm", "hospital_daily_target_mm",
                  "hospital_gairai_daily_pace_mm", "hospital_nyuin_daily_pace_mm",
                  "hospital_gairai_daily_target_mm", "hospital_nyuin_daily_target_mm"]:
            self.assertIn(k, self.kpi, f"新キー {k} が無い")

    def test_old_dept_keys_absent(self):
        old_keys = ["daily_pace", "daily_target", "gairai_daily_pace",
                    "nyuin_daily_pace", "gairai_daily_target", "nyuin_daily_target"]
        for d in self.kpi["top3"] + self.kpi["bottom3"]:
            for k in old_keys:
                self.assertNotIn(k, d, f"{d['name']} に旧キー {k} が残存")

    # ---- (b) 病院合計: pace * 営業日数 ≈ 月合計 ----
    def test_hospital_pace_times_biz_days_matches_total(self):
        pace = self.kpi["hospital_daily_pace_mm"]
        total = self.kpi["hospital_total"]
        self.assertAlmostEqual(pace * BIZ, total, delta=0.05)

    # ---- (c) 科レベル: daily_pace_mm * 営業日数 ≈ 実績 ----
    def test_dept_pace_times_biz_days_matches_actual(self):
        d = self.by_name["内科A"]
        self.assertAlmostEqual(d["daily_pace_mm"] * d["biz_days"], d["actual"],
                                delta=0.005 * d["biz_days"])

    # ---- (d) 入院: nyuin_daily_pace_mm * 暦日数 ≈ 入院粗利 ----
    def test_dept_nyuin_pace_times_cal_days_matches_nyuin_actual(self):
        d = self.by_name["内科A"]
        nyuin_actual_mm = 160000.0 / 1000  # 入院粗利(千円) → 百万円
        self.assertAlmostEqual(d["nyuin_daily_pace_mm"] * d["cal_days"], nyuin_actual_mm,
                                delta=0.005 * d["cal_days"])

    # ---- (e) -0.0 が一切残っていない ----
    def test_no_negative_zero_hospital(self):
        pace_keys = [
            "hospital_daily_pace_mm", "hospital_daily_target_mm",
            "hospital_gairai_daily_pace_mm", "hospital_nyuin_daily_pace_mm",
            "hospital_gairai_daily_target_mm", "hospital_nyuin_daily_target_mm",
        ]
        for k in pace_keys:
            v = self.kpi.get(k)
            self.assertFalse(_is_neg_zero(v), f"{k} が -0.0 のまま")

    def test_no_negative_zero_dept(self):
        dept_pace_keys = ["daily_pace_mm", "daily_target_mm", "pace_rate",
                           "gairai_daily_pace_mm", "gairai_daily_target_mm", "gairai_pace_rate",
                           "nyuin_daily_pace_mm", "nyuin_daily_target_mm", "nyuin_pace_rate"]
        for d in self.kpi["top3"] + self.kpi["bottom3"]:
            for k in dept_pace_keys:
                v = d.get(k)
                self.assertFalse(_is_neg_zero(v), f"{d['name']}.{k} が -0.0 のまま")

    def test_negative_zero_case_is_actually_hit(self):
        # 皮膚D は「素の丸めでは -0.0 になる」ケース自体が起きていることを確認した上で、
        # 正規化後は符号無しの 0.0 になっていることを検証する（縮退テスト防止）。
        d = self.by_name["皮膚D"]
        raw_dp = _D_PROFIT / d["biz_days"] / 1000
        self.assertTrue(_is_neg_zero(round(raw_dp, 2)),
                         "テストケースが縮退している（素の丸めで -0.0 が発生していない）")
        self.assertEqual(d["daily_pace_mm"], 0.0)
        self.assertFalse(_is_neg_zero(d["daily_pace_mm"]))

    # ---- (f) pace_rate は未丸め値の比。2桁丸め後の値どうしの比とは異なる ----
    def test_pace_rate_uses_unrounded_values(self):
        d = self.by_name["整形C"]
        dp_raw = _C_PROFIT / BIZ / 1000
        dt_raw = _C_TARGET / BIZ / 1000
        true_rate = round(dp_raw / dt_raw * 100, 1)
        naive_rate = round(round(dp_raw, 2) / round(dt_raw, 2) * 100, 1)
        self.assertNotEqual(true_rate, naive_rate,
                             "テストケースが縮退している（丸め有無で差が出ていない）")
        self.assertEqual(d["pace_rate"], true_rate)
        self.assertNotEqual(d["pace_rate"], naive_rate)


if __name__ == "__main__":
    unittest.main()
