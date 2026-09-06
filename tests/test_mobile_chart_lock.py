"""B3（スマホ Plotly 固定軸＋役割プリセット復活）の回帰テスト。

JS挙動そのものはDashboardにJSテストハーネスが無いため pytest 対象外（既存 stCss/stShape/stText
と同様に無テスト）。ここでは (1) 固定軸ヘルパ関数のソース検査、(2) テンプレ内の全
`Plotly.newPlot(` 呼び出しが共通関数 `newPlotMobileSafe` を通ること（grepベースの網羅性担保）、
(3) 役割プリセット（.role）が設定シートへ移設されている（id/セクション/movesエントリ）ことを
文字列検査で確認する。常駐サーバ・LLMは一切呼ばない。

実行: リポジトリルートで
    python -m pytest tests/test_mobile_chart_lock.py -v
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
DETAIL_HTML = ROOT / "app" / "templates" / "detail.html"
DEPT_HTML = ROOT / "app" / "templates" / "dept.html"


def _all_newplot_call_lines(src: str):
    """`Plotly.newPlot(` を含む行を、ラッパー自身の定義行を除いて返す。"""
    lines = src.split("\n")
    return [ln for ln in lines
            if "Plotly.newPlot(" in ln and "return Plotly.newPlot(" not in ln]


class LockMobileAxesHelperTest(unittest.TestCase):
    def setUp(self):
        self.detail_src = DETAIL_HTML.read_text(encoding="utf-8")
        self.dept_src = DEPT_HTML.read_text(encoding="utf-8")

    def test_lock_mobile_axes_defined_in_both_templates(self):
        for name, src in (("detail.html", self.detail_src), ("dept.html", self.dept_src)):
            with self.subTest(template=name):
                self.assertIn("function lockMobileAxes(layout, config)", src)
                self.assertIn("fixedrange: true", src)
                self.assertIn("dragmode: false", src)
                self.assertIn("scrollZoom: false", src)
                self.assertIn("window.innerWidth", src)

    def test_wrapper_function_defined_in_both_templates(self):
        for name, src in (("detail.html", self.detail_src), ("dept.html", self.dept_src)):
            with self.subTest(template=name):
                self.assertIn("function newPlotMobileSafe(divId, traces, layout, config)", src)


class AllNewPlotCallsRouteThroughWrapperTest(unittest.TestCase):
    """テンプレ内の生の`Plotly.newPlot(`は、ラッパー自身の内部呼び出し1件のみであること
    （＝他の全呼び出し箇所は`newPlotMobileSafe(`経由）を grep で網羅的に確認する。"""

    def test_detail_html_no_bare_newplot_outside_wrapper(self):
        src = DETAIL_HTML.read_text(encoding="utf-8")
        bare = _all_newplot_call_lines(src)
        self.assertEqual(bare, [], f"ラッパーを経由しない Plotly.newPlot 呼び出しが残っている: {bare}")
        self.assertGreaterEqual(src.count("newPlotMobileSafe("), 10)

    def test_dept_html_no_bare_newplot_outside_wrapper(self):
        src = DEPT_HTML.read_text(encoding="utf-8")
        bare = _all_newplot_call_lines(src)
        self.assertEqual(bare, [], f"ラッパーを経由しない Plotly.newPlot 呼び出しが残っている: {bare}")
        self.assertGreaterEqual(src.count("newPlotMobileSafe("), 8)


class RolePresetMovedToSettingsSheetTest(unittest.TestCase):
    def setUp(self):
        self.src = DETAIL_HTML.read_text(encoding="utf-8")

    def test_role_element_has_id_for_dom_move(self):
        self.assertIn('class="role" id="roleControls"', self.src)

    def test_settings_sheet_has_role_section(self):
        self.assertIn('id="sheetSecRole"', self.src)
        self.assertIn("表示プリセット", self.src)

    def test_moves_array_includes_role_controls(self):
        m = re.search(r"var moves=\[(.*?)\];", self.src, re.S)
        self.assertIsNotNone(m)
        self.assertIn("roleControls", m.group(1))
        self.assertIn("sheetSecRole", m.group(1))

    def test_role_hide_rule_scoped_to_header_not_bare(self):
        """`.role{display:none}`単独（.hdr等でスコープされていない）が残っていないこと
        （シートへ移動後も非表示のままになる罠の回帰）。"""
        self.assertNotIn(".drill-graphs{grid-template-columns:1fr}.role{display:none}", self.src)
        self.assertIn(".hdr .role{display:none}", self.src)


if __name__ == "__main__":
    unittest.main()
