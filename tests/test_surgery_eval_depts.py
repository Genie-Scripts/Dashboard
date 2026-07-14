"""
眼科「全手術」評価軸 回帰テスト（標準ライブラリ unittest・追加依存なし）。

対象: 眼科だけ手術KPIの評価軸を「全身麻酔手術(GA)」→「全手術（術数対象）」に
切替える改修（config.ALLSURG_NORTH_STAR_DEPTS / preprocess.術数対象 列）が、
  (a) 診療科別ローリング集計（rolling7_surgery）で眼科=全手術件数・他科=GA件数になること
  (b) 診療科別ランキング（build_surgery_ranking）に眼科（週目標あり）が出て達成率が出ること
  (c) 病院全体の手術KPI（ga_rolling_biz_avg・全麻基準）が眼科データの有無で変化しないこと
     （回帰境界＝北極星: 21件/日・全麻基準は絶対に変えない）
を保証する。

実行: リポジトリルートで
    python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

import pandas as pd

# リポジトリルートを import パスに追加（generate_html.py と同方式）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.metrics import (  # noqa: E402
    rolling7_surgery,
    build_surgery_ranking,
    ga_rolling_biz_avg,
)

BASE = pd.Timestamp("2026-06-03")   # 水曜（既存テストと同じ基準日）


def _rows(dept, dates, ga_flags):
    """(dept, [日付], [全麻bool]) から手術行のリストを作る。術数対象は preprocess.py と
    同じ規則（眼科=全行True、他科=全麻のみTrue）で手組みする。"""
    is_eye = dept == "眼科"
    out = []
    for d, ga in zip(dates, ga_flags):
        out.append({
            "手術実施日": d, "実施診療科": dept, "全麻": ga,
            "術数対象": True if is_eye else ga,
        })
    return out


class TestRolling7SurgeryEvalDepts(unittest.TestCase):
    """(a) rolling7_surgery: 眼科=全手術件数、他科(12科)=GA件数のまま。"""

    def test_eye_counts_all_surgeries_ga_dept_counts_ga_only(self):
        win_dates = [BASE - pd.Timedelta(days=i) for i in range(7)]   # 直近7暦日
        rows = []
        # 眼科: 5件中 GA=1件のみ。全手術基準なら5、GA基準なら1のはず。
        rows += _rows("眼科", win_dates[:5], [True, False, False, False, False])
        # 整形外科（SURGERY_DISPLAY_DEPTS）: 5件中 GA=3件。GA基準を維持すべき。
        rows += _rows("整形外科", win_dates[:5], [True, True, True, False, False])
        # 窓外（8日前）は集計対象外（両科とも）
        rows += _rows("眼科", [BASE - pd.Timedelta(days=8)], [True])
        rows += _rows("整形外科", [BASE - pd.Timedelta(days=8)], [True])

        surg = pd.DataFrame(rows)
        r = rolling7_surgery(surg, BASE)

        self.assertEqual(r["by_dept"]["眼科"], 5)        # 全手術件数（GA/非GA問わず全行）
        self.assertEqual(r["by_dept"]["整形外科"], 3)     # GA件数のみ（不変）
        # 病院合計(total)は術数対象でなく全麻を維持（眼科の非GA分は含まれない）
        self.assertEqual(r["total"], 4)                  # 眼科GA1 + 整形GA3


class TestSurgeryRankingIncludesEye(unittest.TestCase):
    """(b) build_surgery_ranking: 眼科（週目標106）が出て達成率が算出される。"""

    def test_eye_row_present_with_achievement_rate(self):
        win_dates = [BASE - pd.Timedelta(days=i) for i in range(7)]
        rows = []
        # 眼科: 全手術件数=7件（週目標106に対しては未達だが達成率は算出されるはず）
        rows += _rows("眼科", win_dates, [False] * 7)
        # 整形外科: GA件数=7件
        rows += _rows("整形外科", win_dates, [True] * 7)
        surg = pd.DataFrame(rows)

        surg_targets = {"整形外科": 21, "眼科": 106}
        df = build_surgery_ranking(surg, BASE, surg_targets, period="7")

        eye = df[df["診療科"] == "眼科"].iloc[0]
        self.assertEqual(eye["実績"], 7)
        self.assertEqual(eye["週目標"], 106)
        self.assertIsNotNone(eye["達成率"])
        self.assertAlmostEqual(eye["達成率"], round(7 / 106 * 100, 1))

        seikei = df[df["診療科"] == "整形外科"].iloc[0]
        self.assertEqual(seikei["実績"], 7)
        self.assertIsNotNone(seikei["達成率"])


class TestHospitalGaUnaffectedByEye(unittest.TestCase):
    """(c) 病院全体の手術KPI（全麻・営業平日基準）は眼科データの有無で変化しないこと。"""

    def _biz_ga_surg(self, start, end, dept="整形外科", n_per_day=5):
        """平日のみ n_per_day 件の全麻手術（病院全体の全麻件数の元データ）。"""
        idx = pd.date_range(start, end, freq="D")
        rows = []
        for d in idx:
            if d.weekday() >= 5:
                continue
            for _ in range(n_per_day):
                rows.append({"手術実施日": d, "実施診療科": dept, "全麻": True,
                             "術数対象": True})
        return rows

    def test_eye_non_ga_volume_does_not_change_hospital_avg(self):
        base_rows = self._biz_ga_surg("2026-05-01", BASE)
        surg_without_eye = pd.DataFrame(base_rows)

        # 眼科の全手術（非GA）を大量に追加（現実の眼科＝全麻ほぼ無しを模す）
        eye_rows = []
        idx = pd.date_range("2026-05-01", BASE, freq="D")
        for d in idx:
            for _ in range(10):
                eye_rows.append({"手術実施日": d, "実施診療科": "眼科", "全麻": False,
                                 "術数対象": True})
        surg_with_eye = pd.concat([surg_without_eye, pd.DataFrame(eye_rows)],
                                  ignore_index=True)

        r_without = ga_rolling_biz_avg(surg_without_eye, BASE, window=7)
        r_with = ga_rolling_biz_avg(surg_with_eye, BASE, window=7)

        self.assertEqual(r_without["avg"], r_with["avg"])
        self.assertEqual(r_without["total"], r_with["total"])
        self.assertEqual(r_without["biz_days"], r_with["biz_days"])


if __name__ == "__main__":
    unittest.main()
