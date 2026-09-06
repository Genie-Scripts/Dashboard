"""B2（スマホKPI1カラム）: portal.html のCSSソース検査（回帰テスト）。

CSSのみの変更でJS/Pythonロジックへの影響がないため、既存様式（ファイル内容の文字列検査）
に倣い最小コストで担保する（`tests/test_line_color_consistency.py` と同様の型）。
常駐サーバ・LLMは一切呼ばない。

実行: リポジトリルートで
    python -m pytest tests/test_portal_kpi_mobile_layout.py -v
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PORTAL_HTML = Path(__file__).resolve().parent.parent / "app" / "templates" / "portal.html"


class KpiMobileOneColumnTest(unittest.TestCase):
    def setUp(self):
        self.src = PORTAL_HTML.read_text(encoding="utf-8")

    def test_max_width_480_block_exists(self):
        self.assertIn("max-width:480px", self.src)

    def test_kpi_grid_is_single_column_within_480_block(self):
        m = re.search(r"@media\(max-width:480px\)\{(.*?)\n\}", self.src, re.S)
        self.assertIsNotNone(m, "480px ブロックが見つからない")
        block = m.group(1)
        self.assertIn(".kpi{grid-template-columns:1fr}", block)

    def test_dual_row_status_chip_reordered_within_480_block(self):
        m = re.search(r"@media\(max-width:480px\)\{(.*?)\n\}", self.src, re.S)
        block = m.group(1)
        # ステータス行末チップ化: dual-st を行末(margin-left:auto)へ寄せる
        self.assertIn(".dual-st{", block)
        self.assertIn("margin-left:auto", block)

    def test_768_block_unaffected_still_two_columns(self):
        m = re.search(r"@media\(max-width:768px\)\{(.*?)\n\}", self.src, re.S)
        self.assertIsNotNone(m)
        block = m.group(1)
        self.assertIn(".kpi{grid-template-columns:1fr 1fr}", block)


if __name__ == "__main__":
    unittest.main()
