"""B6（決定論1行読み取り横展開）のテスト。

中身の文言計算はJS側で行われるため（Dashboard既存流儀＝stCss/stShape/stText同様に無テスト）、
本テストは (1) キャプション要素自体の存在（Jinjaレンダー）、(2) キャプション生成関数のソース検査
（記号・単位規約=あと/超過の言い回しが既存`_gap_s`型と同型であること）のみをアサーションする。
常駐サーバ・LLMは一切呼ばない。

実行: リポジトリルートで
    python -m pytest tests/test_chart_caption_placeholders.py -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from generate_html import _build_jinja_env

ROOT = Path(__file__).resolve().parent.parent
DETAIL_HTML = ROOT / "app" / "templates" / "detail.html"
DEPT_HTML = ROOT / "app" / "templates" / "dept.html"


class CaptionElementsExistTest(unittest.TestCase):
    def test_main_chart_cap_element_in_detail_html(self):
        env = _build_jinja_env()
        html = env.get_template("detail.html").render()
        self.assertIn('id="mainChartCap"', html)
        self.assertIn('class="dhm-cap"', html)

    def test_chart1_to_4_cap_elements_in_dept_html(self):
        env = _build_jinja_env()
        html = env.get_template("dept.html").render()
        for n in (1, 2, 3, 4):
            with self.subTest(chart=n):
                self.assertIn(f'id="chart{n}Cap"', html)


class CaptionFunctionSourceTest(unittest.TestCase):
    def test_detail_caption_function_uses_shared_symbol_vocabulary(self):
        src = DETAIL_HTML.read_text(encoding="utf-8")
        self.assertIn("function renderMainChartCaption(seriesVals, tgt, unit)", src)
        self.assertIn("'あと'", src)
        self.assertIn("'超過'", src)
        self.assertIn("stShape(rate)", src)

    def test_dept_caption_function_uses_shared_symbol_vocabulary(self):
        src = DEPT_HTML.read_text(encoding="utf-8")
        self.assertIn("function setChartCaption(elId, last, tgt, unit)", src)
        self.assertIn("'あと'", src)
        self.assertIn("'超過'", src)
        self.assertIn("stShape(rate)", src)

    def test_detail_caption_called_for_all_four_mainchart_renderers(self):
        src = DETAIL_HTML.read_text(encoding="utf-8")
        self.assertEqual(src.count("renderMainChartCaption("), 5,
                         "定義1件+呼び出し4件(汎用/バランス/全麻/粗利)が無いと横展開漏れ")

    def test_dept_caption_called_for_chart_renderers(self):
        src = DEPT_HTML.read_text(encoding="utf-8")
        # renderOneChart(chart1/chart2)/renderSurgeryChart(chart3)/renderProfitDeptChart(chart3/4)/
        # renderFlowChart(chart3/chartBal)。定義1件+複数呼び出し。
        self.assertGreaterEqual(src.count("setChartCaption("), 6)


if __name__ == "__main__":
    unittest.main()
