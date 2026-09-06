"""B9（線色統一・Web側）: 「今年（当年度）の推移線・その帯・凡例」に限定した回帰テスト。

★2026-09-06 是正: 当初は detail.html/dept.html に文字列`#E69F00`が一切無いことを検査して
いたが、これは機械的すぎて「今年線」以外の用途（入退院バランスの退院バー、推計粗利
チャートの入院/外来スタック層、件数vs粗利の指数比較線など＝カテゴリ区別の配色）まで
巻き込んでしまった（司令塔是正指示）。塗り用途・カテゴリ区別の橙 `#E69F00` は許容し、
「今年度」と名付けられた推移線トレース（`mode:'lines'`）とその終点ラベル（`text:['今年度']`
に紐づく marker/textfont）のみを `#2b6cb0` に限定して検査する。

改修プラン_訴求力強化.md F5／B9裁定（`#2b6cb0`に統一。PDF側は
`tests/test_pdf_line_color_consistency.py`が別途担保）。

実行: リポジトリルートで
    python -m pytest tests/test_line_color_consistency.py -v
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
DETAIL_HTML = ROOT / "app" / "templates" / "detail.html"
DEPT_HTML = ROOT / "app" / "templates" / "dept.html"
UNIFIED = "#2b6cb0"

# 「今年度」線トレース本体: name:'今年度(28日平均)'/'今年度(週次合計)'（B9で#E69F00→#2b6cb0へ
# 移行した在院/新入院/手術(週次)の当年トレース）の直後、mode:'lines' の scatter で
# line:{color:'#xxxxxx' が出るまでの区間を1トレース分として抽出する。
# ★注: detail.html の全身麻酔手術チャート（renderOperationChart）の
# name:'今年度(30平日)' は元々 #0072B2（B9のオレンジ統一対象16箇所に含まれておらず、
# 設計書のB9対象行リストにも無い＝別の既存配色）のためスコープ外（意図的に除外）。
TODAY_LINE_RE = re.compile(
    r"name:\s*['\"]今年度\((?:28日平均|週次合計)\)['\"][^{}]*?mode:\s*['\"]lines['\"][^{}]*?"
    r"line:\{color:\s*['\"](#[0-9A-Fa-f]{6})['\"]",
    re.S,
)
# 「今年度」終点ラベル（マーカー+テキスト）: text:['今年度'] に紐づく textfont/marker の色。
TODAY_LABEL_RE = re.compile(
    r"text:\s*\['今年度'\][^{}]*?textfont:\{[^}]*?color:\s*['\"](#[0-9A-Fa-f]{6})['\"][^}]*?\}"
    r"[^{}]*?marker:\{[^}]*?color:\s*['\"](#[0-9A-Fa-f]{6})['\"]",
    re.S,
)
LABEL_SEARCH_WINDOW = 400  # 当年ラインpush直後、終点ラベルpushが現れるまでの想定距離


class TodayYearLineTraceColorTest(unittest.TestCase):
    """当年(今年度)の推移線トレースは #2b6cb0 に統一されていること。"""

    def _check_file(self, path: Path):
        src = path.read_text(encoding="utf-8")
        matches = TODAY_LINE_RE.findall(src)
        self.assertGreater(len(matches), 0, f"{path.name}: 今年度ラインが1本も見つからない（検査パターンの陳腐化要確認）")
        for color in matches:
            self.assertEqual(color, UNIFIED, f"{path.name}: 今年度の推移線が{color}のまま（{UNIFIED}であるべき）")

    def test_detail_html_today_line_is_unified_color(self):
        self._check_file(DETAIL_HTML)

    def test_dept_html_today_line_is_unified_color(self):
        self._check_file(DEPT_HTML)


class TodayYearEndpointLabelColorTest(unittest.TestCase):
    """今年度の推移線の終点ラベル（凡例的な役割の文字色・マーカー）も同じ青であること。

    ラベルpush自体には`name:`が無いため、B9対象の当年ラインpush（TODAY_LINE_RE）の
    直後の一定範囲内に現れるラベルだけを検査する（detail.htmlのrenderOperationChartに
    ある#0072B2の「今年度(30平日)」用ラベルは、そのライン自体がB9対象外＝TODAY_LINE_REに
    マッチしないため、この範囲探索にも含まれずスコープ外のまま保たれる）。"""

    def _check_file(self, path: Path):
        src = path.read_text(encoding="utf-8")
        line_matches = list(TODAY_LINE_RE.finditer(src))
        self.assertGreater(len(line_matches), 0, f"{path.name}: 今年度ラインが1本も見つからない")
        checked = 0
        for m in line_matches:
            window = src[m.end():m.end() + LABEL_SEARCH_WINDOW]
            label_m = TODAY_LABEL_RE.search(window)
            if not label_m:
                continue  # このB9対象ラインには終点ラベルpushが無い（許容）
            text_color, marker_color = label_m.groups()
            checked += 1
            self.assertEqual(text_color, UNIFIED, f"{path.name}: 今年度ラベル文字色が{text_color}")
            self.assertEqual(marker_color, UNIFIED, f"{path.name}: 今年度ラベルマーカー色が{marker_color}")
        self.assertGreater(checked, 0, f"{path.name}: 今年度終点ラベルが1件も検証できなかった")

    def test_detail_html_label_is_unified_color(self):
        self._check_file(DETAIL_HTML)

    def test_dept_html_label_is_unified_color(self):
        self._check_file(DEPT_HTML)


class CategoricalOrangeStillAllowedTest(unittest.TestCase):
    """今年線以外（カテゴリ区別の塗り・棒）は橙のままでよい（B9のスコープ外・退行防止）。
    ここでは「元々カテゴリ用途だった箇所」がオレンジのままであることのみ確認する
    （オレンジを強制はしない。將来別配色に変える余地は残す）。"""

    def test_detail_html_categorical_fills_kept_orange(self):
        src = DETAIL_HTML.read_text(encoding="utf-8")
        self.assertIn("入院見込み", src)
        self.assertIn(
            "line:{color:'#E69F00', width:0.5}, fillcolor:'rgba(230,159,0,0.40)', "
            "hovertemplate:'%{y:.1f}百万円<extra>入院見込み</extra>'}",
            src,
        )

    def test_dept_html_categorical_fills_kept_orange(self):
        src = DEPT_HTML.read_text(encoding="utf-8")
        self.assertIn(
            "line:{color:'#E69F00', width:0.5}, fillcolor:'rgba(230,159,0,0.40)',",
            src,
        )


if __name__ == "__main__":
    unittest.main()
