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
                         ["在院7日平均", "新入院7日累計", "全麻（1週・営業日平均）",
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
            "public_base_url": "https://hospital-dashboard-6ow.pages.dev/",
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
            "▶ 詳細（毎日更新）: https://hospital-dashboard-6ow.pages.dev/portal.html"))


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


class RelabelDiffsForCompleteWeekTest(unittest.TestCase):
    """B12: weekly_story.compute_wow_diffs() 由来の「直近7日」表記を「対象週」へ置換
    （値は変えず文言のみ・実走で「直近7日」がHTML/txtに漏れる欠陥の是正）。"""

    def test_replaces_all_occurrences(self):
        diffs = [
            "手術室稼働率（直近7日）: 72.3%→75.1%（+2.8pt）",
            "新入院（直近7日累計）: 370人 → 364人（-6人、-2%）",
            "緊急入院（直近7日累計）: 41件 → 38件（-3件、-7%）",
        ]
        out = bwd.relabel_diffs_for_complete_week(diffs)
        self.assertEqual(out, [
            "手術室稼働率（対象週）: 72.3%→75.1%（+2.8pt）",
            "新入院（対象週累計）: 370人 → 364人（-6人、-2%）",
            "緊急入院（対象週累計）: 41件 → 38件（-3件、-7%）",
        ])
        for d in out:
            self.assertNotIn("直近7日", d)

    def test_leaves_unrelated_text_untouched(self):
        diffs = ["在院患者数（7日平均）: 554.7人 → 548.2人（+6.5人）"]
        self.assertEqual(bwd.relabel_diffs_for_complete_week(diffs), diffs)

    def test_empty_list(self):
        self.assertEqual(bwd.relabel_diffs_for_complete_week([]), [])


class ResolveBaseDateTest(unittest.TestCase):
    """B12: --base-date 未指定時は直近日曜（完全週の終端）へ丸める。明示指定時はそのまま。"""

    def test_unspecified_rounds_to_complete_week_end(self):
        # データ最終日=火曜(2026-09-08) → 直近日曜(2026-09-06)へ丸める
        resolved = bwd.resolve_base_date(None, pd.Timestamp("2026-09-08"))
        self.assertEqual(resolved, pd.Timestamp("2026-09-06"))

    def test_explicit_base_date_is_not_rounded(self):
        # --base-date で火曜を明示指定 → 丸めずそのまま
        resolved = bwd.resolve_base_date("2026-09-08", pd.Timestamp("2026-09-08"))
        self.assertEqual(resolved, pd.Timestamp("2026-09-08"))


class BuildPeriodHeadingTest(unittest.TestCase):
    """B12: 見出し「対象週 8/24(月)〜8/30(日)｜比較: その前の週 8/17〜8/23」。「直近7日」は出ない。"""

    def test_heading_format(self):
        heading = bwd.build_period_heading(pd.Timestamp("2026-08-24"), pd.Timestamp("2026-08-30"))
        self.assertEqual(heading, "対象週 8/24(月)〜8/30(日)｜比較: その前の週 8/17〜8/23")

    def test_no_rolling7_wording(self):
        heading = bwd.build_period_heading(pd.Timestamp("2026-08-24"), pd.Timestamp("2026-08-30"))
        self.assertNotIn("直近7日", heading)


class BuildPosterKpisTest(unittest.TestCase):
    """B13: 掲示用の大きな数字3枚（在院・新入院・全麻）。全麻は§3共通規約どおり単位=件。"""

    def test_three_tiles_in_order(self):
        kpi_now = _kpi()
        kpi_now["operation_7d_total"] = 145
        rows = bwd.build_kpi_rows(kpi_now, _kpi(), _snap(), _snap())
        tiles = bwd.build_poster_kpis(kpi_now, rows, pd.Timestamp("2026-08-30"))
        self.assertEqual([t["label"] for t in tiles], ["在院", "新入院", "全麻"])

    def test_operation_tile_unit_is_count_not_rate(self):
        kpi_now = _kpi()
        kpi_now["operation_7d_total"] = 145
        rows = bwd.build_kpi_rows(kpi_now, _kpi(), _snap(), _snap())
        tiles = bwd.build_poster_kpis(kpi_now, rows, pd.Timestamp("2026-08-30"))
        op_tile = tiles[2]
        self.assertEqual(op_tile["unit"], "件")
        self.assertEqual(op_tile["now_s"], "145")

    def test_inpatient_and_admission_tiles_reuse_kpi_rows(self):
        kpi_now = _kpi(inp_avg7d=554.7, adm_actual7d=370)
        kpi_now["operation_7d_total"] = 145
        rows = bwd.build_kpi_rows(kpi_now, _kpi(), _snap(), _snap())
        tiles = bwd.build_poster_kpis(kpi_now, rows, pd.Timestamp("2026-08-30"))
        self.assertEqual(tiles[0]["now_s"], "554.7")
        self.assertEqual(tiles[0]["unit"], "人")
        self.assertEqual(tiles[1]["now_s"], "370")
        self.assertEqual(tiles[1]["unit"], "人")

    def test_missing_operation_total_degrades_to_dash(self):
        kpi_now = _kpi()  # operation_7d_total 未設定
        rows = bwd.build_kpi_rows(kpi_now, _kpi(), _snap(), _snap())
        tiles = bwd.build_poster_kpis(kpi_now, rows, pd.Timestamp("2026-08-30"))
        self.assertEqual(tiles[2]["now_s"], "—")
        self.assertEqual(tiles[2]["status_shape"], "—")


if __name__ == "__main__":
    unittest.main()
