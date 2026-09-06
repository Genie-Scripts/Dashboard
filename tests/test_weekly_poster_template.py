"""weekly_poster.html（B13: 掲示A4とメール本文の分離）のテンプレート断面レンダーテスト。

tests/test_ai_alerts_display.py と同じ手法（generate_html._build_jinja_env() を再利用し
テンプレート単体をレンダー）。実データ・ファイルI/O・LLMには依存しない。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from generate_html import _build_jinja_env

_FAKE_QR_SVG = '<svg viewBox="0 0 10 10"><rect width="10" height="10"/></svg>'


def _poster_kpis():
    return [
        {"label": "在院", "unit": "人", "now_s": "554.7", "target_s": "582.8",
         "status_css": "wr", "status_shape": "―"},
        {"label": "新入院", "unit": "人", "now_s": "370", "target_s": "379",
         "status_css": "ok", "status_shape": "▲"},
        {"label": "全麻", "unit": "件", "now_s": "138", "target_s": "150",
         "status_css": "dr", "status_shape": "▼"},
    ]


def _render_poster(**overrides) -> str:
    ctx = {
        "hospital_name": "",
        "base_date": "2026-08-30",
        "period_heading": "対象週 8/24(月)〜8/30(日)｜比較: その前の週 8/17〜8/23",
        "generated_at": "2026/08/31 07:39",
        "story": "在院は前週比+6.5人で改善傾向。",
        "poster_kpis": _poster_kpis(),
        "qr_svg": _FAKE_QR_SVG,
        "public_base_url": "https://hospital-dashboard-6ow.pages.dev/",
    }
    ctx.update(overrides)
    tmpl = _build_jinja_env().get_template("weekly_poster.html")
    return tmpl.render(**ctx)


class WeeklyPosterTemplateTest(unittest.TestCase):
    def test_three_big_numbers_render(self):
        html = _render_poster()
        for now_s in ("554.7", "370", "138"):
            self.assertIn(now_s, html)

    def test_one_sentence_story_renders(self):
        html = _render_poster()
        self.assertIn("在院は前週比+6.5人で改善傾向。", html)

    def test_fallback_sentence_when_story_missing(self):
        html = _render_poster(story=None)
        self.assertIn("今週も引き続き、日々の目標達成をよろしくお願いします。", html)

    def test_qr_svg_renders(self):
        html = _render_poster()
        self.assertIn("<svg", html)

    def test_period_heading_renders(self):
        html = _render_poster()
        self.assertIn("対象週 8/24(月)〜8/30(日)｜比較: その前の週 8/17〜8/23", html)

    def test_status_shapes_render(self):
        html = _render_poster()
        for shape in ("▲", "―", "▼"):
            self.assertIn(shape, html)

    def test_font_size_60px_present(self):
        html = _render_poster()
        self.assertIn("60px", html)

    def test_no_detail_table_or_attention_list(self):
        """掲示版はKPI表・要注視・改善を含まない（詳細は.txt/digest側）。"""
        html = _render_poster()
        self.assertNotIn("kpi-table", html)
        self.assertNotIn("要注視", html)


if __name__ == "__main__":
    unittest.main()
