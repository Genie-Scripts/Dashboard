"""B7（KPIカードのスパークライン・サーバSVG）のテスト。

`app/lib/sparkline.py`（新規・純関数）の単体テストと、`build_portal_context`が全カードに
`sparkline`キーを持つことの結合テスト（`tests/test_portal_history.py`と同型の合成データ）。
常駐サーバ・LLMは一切呼ばない。

実行: リポジトリルートで
    python -m pytest tests/test_sparkline.py -v
"""
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.sparkline import render_sparkline_svg
from app.lib import html_builder


class RenderSparklineSvgTest(unittest.TestCase):
    def test_empty_values_returns_empty_string(self):
        self.assertEqual(render_sparkline_svg([]), "")
        self.assertEqual(render_sparkline_svg([None, None, None]), "")

    def test_valid_svg_with_path_and_height_40(self):
        svg = render_sparkline_svg([580, 590, 600, 595, 605], ref=600)
        self.assertTrue(svg.startswith("<svg"))
        self.assertTrue(svg.endswith("</svg>"))
        self.assertIn("<path", svg)
        self.assertIn('height="40"', svg)

    def test_ref_line_uses_dasharray(self):
        svg = render_sparkline_svg([1, 2, 3], ref=2)
        self.assertIn("stroke-dasharray", svg)

    def test_no_ref_no_dasharray(self):
        svg = render_sparkline_svg([1, 2, 3], ref=None)
        self.assertNotIn("stroke-dasharray", svg)

    def test_default_color_is_b9_unified_blue(self):
        svg = render_sparkline_svg([1, 2, 3])
        self.assertIn("#2b6cb0", svg)

    def test_single_value_does_not_crash(self):
        svg = render_sparkline_svg([42])
        self.assertIn("<svg", svg)

    def test_none_gaps_in_series_are_skipped_gracefully(self):
        svg = render_sparkline_svg([10, None, 12, None, 14])
        self.assertIn("<path", svg)


class BuildKpiSparklinesIntegrationTest(unittest.TestCase):
    """build_portal_context が3枚のkpi_cardsすべてに sparkline キーを持つことの結合確認。
    実データ依存を避けるため、tests/test_portal_history.py 同様の最小合成データを使う。"""

    def _make_adm(self):
        dates = pd.date_range("2026-01-01", "2026-08-01", freq="D")
        rows = []
        for d in dates:
            rows.append({
                "日付": d, "在院患者数": 590, "新入院患者数": 20, "退院合計": 19,
                "入院患者数": 15, "緊急入院患者数": 5, "科_表示": True, "病棟_表示": True,
                "診療科名": "架空内科", "病棟コード": "1A",
            })
        return pd.DataFrame(rows)

    def _make_surg(self):
        dates = pd.date_range("2026-01-01", "2026-08-01", freq="D")
        rows = []
        for d in dates:
            rows.append({"手術実施日": d, "全麻": True, "実施診療科": "架空外科",
                        "術数対象": True})
        return pd.DataFrame(rows)

    def test_sparklines_present_on_all_three_cards(self):
        adm = self._make_adm()
        surg = self._make_surg()
        base_date = pd.Timestamp("2026-08-01")
        sparklines = html_builder._build_kpi_sparklines(adm, surg, base_date)
        self.assertIn("inpatient", sparklines)
        self.assertIn("admission", sparklines)
        self.assertIn("operation", sparklines)
        for key, svg in sparklines.items():
            with self.subTest(card=key):
                self.assertTrue(svg == "" or svg.startswith("<svg"))


class PortalTemplateWiringTest(unittest.TestCase):
    """portal.html はPlotly非依存のまま(<script src=...plotly...>が無い)、
    card.sparkline がある場合のみ kc-spark を描画することを確認する。"""

    def test_portal_has_kc_spark_block_and_no_plotly_script(self):
        src = Path(__file__).resolve().parent.parent / "app" / "templates" / "portal.html"
        html = src.read_text(encoding="utf-8")
        self.assertIn('{% if card.sparkline %}<div class="kc-spark">', html)
        self.assertNotIn("plot.ly", html.lower())
        self.assertNotIn("plotly.min.js", html.lower())


if __name__ == "__main__":
    unittest.main()
