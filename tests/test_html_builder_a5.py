"""A5(鮮度1行)のユニットテスト。

`html_builder._build_freshness()` の3ケース（通常/2日空き/初回）と、
`tests/test_ai_alerts_display.py` と同じ手法（jinja2テンプレートの部分レンダー）で
portal.html の `#freshness` に「データは…まで」が出ることを確認する。
LLM(oMLX)・常駐サーバは一切呼ばない。
"""
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jinja2 import Environment, ChainableUndefined

from app.lib.html_builder import _build_freshness
from generate_html import _build_jinja_env


class TestBuildFreshness(unittest.TestCase):
    def test_normal_no_gap_note(self):
        """初回でなくても1日以内のギャップなら prev_label は出ない（=通常運用）。"""
        base_date = pd.Timestamp("2026-09-03")
        generated_at = pd.Timestamp("2026-09-04 07:39")
        prior = pd.Timestamp("2026-09-03 07:12")   # 1日前
        r = _build_freshness(base_date, generated_at, prior)
        self.assertFalse(r["stale"])
        self.assertIsNone(r["prev_label"])
        self.assertIn("データは 9/3(木) まで", r["text"])
        self.assertIn("9/4 07:39 作成", r["text"])
        self.assertIn("平日の朝に更新", r["text"])

    def test_gap_2days_or_more_sets_prev_label(self):
        base_date = pd.Timestamp("2026-09-03")
        generated_at = pd.Timestamp("2026-09-04 07:39")
        prior = pd.Timestamp("2026-09-01 07:10")   # 3日前
        r = _build_freshness(base_date, generated_at, prior)
        self.assertTrue(r["stale"])
        self.assertEqual(r["prev_label"], "前回更新 9/1")

    def test_first_build_no_prior_snapshot(self):
        """初回（前回スナップショット無し）は stale=False・prev_label=None（無害縮退）。"""
        base_date = pd.Timestamp("2026-09-03")
        generated_at = pd.Timestamp("2026-09-04 07:39")
        r = _build_freshness(base_date, generated_at, None)
        self.assertFalse(r["stale"])
        self.assertIsNone(r["prev_label"])
        self.assertIn("データは 9/3(木) まで", r["text"])


def _template_env() -> Environment:
    base_env = _build_jinja_env()
    env = Environment(loader=base_env.loader, undefined=ChainableUndefined,
                      autoescape=base_env.autoescape)
    env.filters.update(base_env.filters)
    return env


class TestFreshnessTemplatePlacement(unittest.TestCase):
    """B8: 3テンプレとも id="freshness" にA5の文言が出る（配置はB8裁定どおり簡素版）。"""

    def test_portal_freshness_text(self):
        tmpl = _template_env().get_template("portal.html")
        html = tmpl.render(
            base_date="2026-09-03", generated_at="2026/09/04 07:39",
            headline={"level": "ok", "icon": "🏥", "text": "テスト見出し"},
            kpi_cards=[],
            freshness={"text": "データは 9/3(木) まで｜9/4 07:39 作成｜平日の朝に更新",
                      "stale": False, "prev_label": None},
        )
        self.assertIn('id="freshness"', html)
        self.assertIn("データは 9/3(木) まで", html)
        self.assertIn("平日の朝に更新", html)

    def test_portal_freshness_prev_label_appended(self):
        tmpl = _template_env().get_template("portal.html")
        html = tmpl.render(
            base_date="2026-09-03", generated_at="2026/09/04 07:39",
            headline={"level": "ok", "icon": "🏥", "text": "テスト見出し"},
            kpi_cards=[],
            freshness={"text": "データは 9/3(木) まで｜9/4 07:39 作成｜平日の朝に更新",
                      "stale": True, "prev_label": "前回更新 9/1"},
        )
        self.assertIn("前回更新 9/1", html)


if __name__ == "__main__":
    unittest.main()
