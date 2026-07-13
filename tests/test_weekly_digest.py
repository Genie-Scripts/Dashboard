"""週次ダイジェスト（B3）のユニットテスト。

対象（§6.6・純関数部分のみ）:
  - build_kpi_rows : build_kpi_summary/build_kpi_snapshot の戻り値からのWoW再計算
  - render_txt      : メール貼付用プレーンテキストの整形
  - _fmt_improvement_txt : 改善トピックのテキスト整形

実データ・ファイルI/Oには依存しない（合成dictのみ使用）。

実行: リポジトリルートで
    python -m pytest tests/ -q
"""
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import build_weekly_digest as bwd


def _kpi(inp_avg7d=554.7, inp_target=582.8,
        adm_actual7d=370, adm_target=379.2, adm_rate7d=97.6,
        op_avg=20.5, op_target=21, op_rate=97.6):
    return {
        "inpatient_avg_7d": inp_avg7d,
        "inpatient_target_allday": inp_target,
        "admission_actual_7d": adm_actual7d,
        "admission_target_weekly": adm_target,
        "admission_rate_7d": adm_rate7d,
        "operation_daily_avg": op_avg,
        "operation_target": op_target,
        "operation_rate": op_rate,
    }


def _snap(or_util=72.3, emergency=41):
    return {
        "operation": {"or_util_7d": or_util},
        "admission": {"emergency_7d": emergency},
    }


class BuildKpiRowsTest(unittest.TestCase):
    def test_five_rows_in_order(self):
        rows = bwd.build_kpi_rows(_kpi(), _kpi(), _snap(), _snap())
        self.assertEqual([r["label"] for r in rows],
                         ["在院7日平均", "新入院7日累計", "全麻（7平日平均）",
                          "手術室稼働率", "緊急入院"])

    def test_diff_and_rate_computed(self):
        now = _kpi(inp_avg7d=554.7)
        prev = _kpi(inp_avg7d=548.2)
        rows = bwd.build_kpi_rows(now, prev, _snap(), _snap())
        inp_row = rows[0]
        self.assertAlmostEqual(inp_row["diff"], 6.5, places=1)
        self.assertEqual(inp_row["diff_s"], "+6.5")
        # achievement_rate(554.7, 582.8) ≈ 95.2
        self.assertAlmostEqual(inp_row["rate"], 95.2, delta=0.2)
        self.assertIn("―", inp_row["rate_display"])   # 90〜100% は「接近」＝ ―

    def test_status_display_thresholds(self):
        # 達成率100%以上 → ok/▲、90%未満 → dr/▼
        rows_ok = bwd.build_kpi_rows(
            _kpi(inp_avg7d=600, inp_target=500), _kpi(), _snap(), _snap())
        self.assertEqual(rows_ok[0]["status"]["css"], "ok")
        self.assertEqual(rows_ok[0]["status"]["shape"], "▲")

        rows_dr = bwd.build_kpi_rows(
            _kpi(inp_avg7d=100, inp_target=500), _kpi(), _snap(), _snap())
        self.assertEqual(rows_dr[0]["status"]["css"], "dr")
        self.assertEqual(rows_dr[0]["status"]["shape"], "▼")

    def test_no_target_rows_have_no_rate(self):
        rows = bwd.build_kpi_rows(_kpi(), _kpi(), _snap(), _snap())
        or_row, emg_row = rows[3], rows[4]
        self.assertIsNone(or_row["target"])
        self.assertIsNone(or_row["rate"])
        self.assertEqual(or_row["rate_display"], "—")
        self.assertIsNone(emg_row["target"])

    def test_none_values_degrade_to_dash(self):
        now = _kpi()
        now["inpatient_avg_7d"] = None
        rows = bwd.build_kpi_rows(now, _kpi(), _snap(), _snap())
        self.assertEqual(rows[0]["now_s"], "—")
        self.assertIsNone(rows[0]["diff"])
        self.assertEqual(rows[0]["diff_s"], "—")

    def test_missing_snapshot_keys_degrade_gracefully(self):
        rows = bwd.build_kpi_rows(_kpi(), _kpi(), {}, {})
        or_row, emg_row = rows[3], rows[4]
        self.assertIsNone(or_row["now"])
        self.assertIsNone(emg_row["now"])


class RenderTxtTest(unittest.TestCase):
    def _ctx(self, **overrides):
        ctx = {
            "week_start": pd.Timestamp("2026-07-08"),
            "week_end": pd.Timestamp("2026-07-14"),
            "base_date": pd.Timestamp("2026-07-14"),
            "story": None,
            "kpi_rows": bwd.build_kpi_rows(_kpi(), _kpi(inp_avg7d=548.2), _snap(), _snap()),
            "attention": {"dept_count": 5, "ward_count": 3,
                         "worst3": [{"name": "8階病棟", "primary_rate": 78.0}]},
            "improvement": {
                "dept_internal": [{"name": "泌尿器科", "metric_label": "在院",
                                   "delta": 6, "unit": "人", "compare": "前週同曜日比"}],
                "dept_surgery": [], "ward": [],
            },
            "public_base_url": "https://genie-scripts.github.io/Dashboard/",
        }
        ctx.update(overrides)
        return ctx

    def test_header_line_format(self):
        txt = bwd.render_txt(self._ctx())
        self.assertTrue(txt.startswith(
            "【週次ダイジェスト】2026/07/08〜07/14（基準日 07/14）"))

    def test_no_story_falls_back(self):
        txt = bwd.render_txt(self._ctx(story=None))
        self.assertIn("（自動要約なし）", txt)

    def test_story_included_when_present(self):
        txt = bwd.render_txt(self._ctx(story="在院は前週比+6.5人で改善傾向。"))
        self.assertIn("在院は前週比+6.5人で改善傾向。", txt)

    def test_kpi_line_has_now_prev_target_rate(self):
        txt = bwd.render_txt(self._ctx())
        self.assertIn("在院7日平均 554.7人（先週 548.2 / 目標 582.8）", txt)

    def test_attention_line_counts_and_worst(self):
        txt = bwd.render_txt(self._ctx())
        self.assertIn("■ 要注視: 病棟3・診療科5 ─ ワースト: 8階病棟(78%)", txt)

    def test_attention_line_no_worst_omits_dash(self):
        ctx = self._ctx(attention={"dept_count": 0, "ward_count": 0, "worst3": []})
        txt = bwd.render_txt(ctx)
        self.assertIn("■ 要注視: 病棟0・診療科0", txt)
        self.assertNotIn("ワースト:", txt)

    def test_footer_link(self):
        txt = bwd.render_txt(self._ctx())
        self.assertTrue(txt.rstrip().endswith(
            "▶ 詳細（毎日更新）: https://genie-scripts.github.io/Dashboard/portal.html"))


class FmtImprovementTxtTest(unittest.TestCase):
    def test_single_group(self):
        imp = {"dept_internal": [{"name": "泌尿器科", "metric_label": "在院",
                                  "delta": 6, "unit": "人", "compare": "前週同曜日比"}],
               "dept_surgery": [], "ward": []}
        s = bwd._fmt_improvement_txt(imp)
        self.assertEqual(s, "内科系: 泌尿器科 在院+6人（前週同曜日比）")

    def test_multiple_groups_joined(self):
        imp = {
            "dept_internal": [{"name": "腎臓内科", "metric_label": "在院",
                               "delta": 4, "unit": "人", "compare": "前週同曜日比"}],
            "dept_surgery": [{"name": "整形外科", "metric_label": "全麻",
                              "delta": 3, "unit": "件", "compare": "前週比（7日累計）"}],
            "ward": [],
        }
        s = bwd._fmt_improvement_txt(imp)
        self.assertIn("内科系: 腎臓内科 在院+4人（前週同曜日比）", s)
        self.assertIn("外科系: 整形外科 全麻+3件（前週比（7日累計））", s)
        self.assertIn("／", s)

    def test_all_empty_returns_none_label(self):
        imp = {"dept_internal": [], "dept_surgery": [], "ward": []}
        self.assertEqual(bwd._fmt_improvement_txt(imp), "該当なし")


if __name__ == "__main__":
    unittest.main()
