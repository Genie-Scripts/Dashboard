"""build_selfcontained.py（Comedix配布用 自己完結HTML・§8 S1〜S4）のユニットテスト。

純関数のみ・ブラウザ不要。対象:
  - moves_patch: </body>直前に1回だけ注入／</エスケープ／dept:・ward:キー→名前変換／
                 空move除外／moves無し→無変更
  - load_latest_moves（moves_store）: 最新選択／45日超は無視／壊れたJSONスキップ／
                 ディレクトリ無し→None
  - extract_generated_date: "generated"マーカー優先・portalフッター基準日フォールバック
  - neutralize_links: style/script断片の注入

実行: リポジトリルートで
    python -m unittest discover -s tests -v
"""
import json
import re
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_selfcontained
from app.lib.moves_store import load_latest_moves


def _html():
    return "<html><body><script>var DATA={drill:{}};</script></body></html>"


class MovesPatchTest(unittest.TestCase):
    def setUp(self):
        self._orig = build_selfcontained.load_latest_moves
        self.addCleanup(lambda: setattr(build_selfcontained, "load_latest_moves", self._orig))

    def test_injects_once_before_body_close(self):
        moves = {"base_date": "2026-07-12",
                 "units": {"dept:消化器内科": {"body": "件数増を継続", "action": "対応する"}}}
        build_selfcontained.load_latest_moves = lambda base: moves
        html = _html()
        out, n = build_selfcontained.moves_patch(html)
        self.assertEqual(n, 1)
        self.assertEqual(out.count('id="selfcontained-moves-patch"'), 1)
        self.assertLess(out.index('id="selfcontained-moves-patch"'), out.rindex("</body>"))

    def test_slash_escape_prevents_early_script_close(self):
        moves = {"base_date": "2026-07-12",
                 "units": {"dept:内科A": {"body": "件数を</script>増やす"}}}
        build_selfcontained.load_latest_moves = lambda base: moves
        out, _ = build_selfcontained.moves_patch(_html())
        self.assertNotIn("</script>増やす", out)
        self.assertIn("<\\/script>増やす", out)

    def test_dept_ward_key_stripped_to_name(self):
        moves = {"base_date": "2026-07-12",
                 "units": {"dept:消化器内科": {"body": "b1"}, "ward:3階病棟": {"body": "b2"}}}
        build_selfcontained.load_latest_moves = lambda base: moves
        out, n = build_selfcontained.moves_patch(_html())
        self.assertEqual(n, 2)
        self.assertIn('"消化器内科"', out)
        self.assertIn('"3階病棟"', out)
        self.assertNotIn("dept:消化器内科", out)
        self.assertNotIn("ward:3階病棟", out)

    def test_empty_move_excluded(self):
        moves = {"base_date": "2026-07-12",
                 "units": {"dept:内科A": {}, "dept:内科B": {"body": "b"}}}
        build_selfcontained.load_latest_moves = lambda base: moves
        out, n = build_selfcontained.moves_patch(_html())
        self.assertEqual(n, 1)
        self.assertNotIn("内科A", out)
        self.assertIn("内科B", out)

    def test_no_moves_returns_unchanged(self):
        build_selfcontained.load_latest_moves = lambda base: None
        html = _html()
        out, n = build_selfcontained.moves_patch(html)
        self.assertEqual(n, 0)
        self.assertEqual(out, html)

    def test_double_call_does_not_duplicate_injection(self):
        moves = {"base_date": "2026-07-12", "units": {"dept:内科A": {"body": "b"}}}
        build_selfcontained.load_latest_moves = lambda base: moves
        once, _ = build_selfcontained.moves_patch(_html())
        twice, n2 = build_selfcontained.moves_patch(once)
        self.assertEqual(twice.count('id="selfcontained-moves-patch"'), 1)
        self.assertEqual(n2, 1)


class LoadLatestMovesTest(unittest.TestCase):
    def _write(self, d, date_str, payload=None):
        p = Path(d) / f"moves_{date_str}.json"
        p.write_text(json.dumps(payload if payload is not None else {"base_date": date_str, "units": {}}),
                     encoding="utf-8")
        return p

    def test_picks_latest_at_or_before_base_date(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "2026-07-10")
            self._write(d, "2026-07-12")
            result = load_latest_moves("2026-07-13", state_dir=d)
            self.assertEqual(result["base_date"], "2026-07-12")

    def test_ignores_files_after_base_date(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "2026-07-10")
            self._write(d, "2026-07-15")  # base_date より未来 → 対象外
            result = load_latest_moves("2026-07-13", state_dir=d)
            self.assertEqual(result["base_date"], "2026-07-10")

    def test_ignores_files_older_than_max_age_days(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "2026-05-01")  # 2026-07-13 から45日超前
            result = load_latest_moves("2026-07-13", state_dir=d)
            self.assertIsNone(result)

    def test_broken_json_is_skipped_not_raised(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "moves_2026-07-12.json").write_text("{not valid json", encoding="utf-8")
            result = load_latest_moves("2026-07-13", state_dir=d)
            self.assertIsNone(result)

    def test_missing_directory_returns_none(self):
        result = load_latest_moves("2026-07-13", state_dir="/nonexistent/dir/for/moves_store_test")
        self.assertIsNone(result)


class ExtractGeneratedDateTest(unittest.TestCase):
    def test_generated_marker_takes_priority(self):
        html = ('<script>const DATA = {"generated": "2026-07-01T00:00:00"};</script>'
                '<footer>基準日 2026-07-13</footer>')
        self.assertEqual(build_selfcontained.extract_generated_date(html), "2026-07-01")

    def test_portal_footer_fallback_when_no_generated_marker(self):
        html = ('<footer>🏥 診療ダッシュボード ポータル ｜ 生成 2026/07/13 07:09 ｜ '
                '基準日 2026-07-13</footer>')
        self.assertEqual(build_selfcontained.extract_generated_date(html), "2026-07-13")

    def test_falls_back_to_today_when_neither_present(self):
        result = build_selfcontained.extract_generated_date("<html></html>")
        self.assertEqual(result, date.today().isoformat())


class HideCrossPageNavTest(unittest.TestCase):
    """他ページナビの非表示（単一ページ配信でのデッドリンク＝404 の防止）。

    退行事例: 非表示CSSが #backLink,#pageNav の id 指定だけだった頃、
    portal.html のヘッダーナビ <nav class="nav-bar">（id無し）が消えず、
    配布HTMLの「ポータル/統合詳細/部門別」を押すと 404 になっていた。
    """

    def test_injects_style_into_head(self):
        html = "<html><head><title>t</title></head><body></body></html>"
        out, n = build_selfcontained.hide_cross_page_nav(html)
        self.assertEqual(n, 1)
        self.assertIn('id="selfcontained-nav-hide"', out)
        self.assertLess(out.index("selfcontained-nav-hide"), out.index("</head>"))

    def test_no_head_tag_returns_unchanged(self):
        html = "<html><body>no head</body></html>"
        out, n = build_selfcontained.hide_cross_page_nav(html)
        self.assertEqual(n, 0)
        self.assertEqual(out, html)

    def test_selectors_cover_both_id_and_class_hooks(self):
        style = build_selfcontained.NAV_HIDE_STYLE
        for sel in ("#backLink", "#pageNav", ".hdr .nav-bar", "nav.nav"):
            self.assertIn(sel, style, f"{sel} が非表示CSSに無い")

    def test_real_portal_nav_hooks_still_match_selectors(self):
        """実 portal.html のナビ実装が変わったら気付けるようにする（CSSの陳腐化検知）。"""
        portal = REPO_ROOT / "portal.html"
        if not portal.exists():
            self.skipTest("portal.html 未ビルド")
        html = portal.read_text(encoding="utf-8")
        header = re.search(r'<header class="hdr">.*?</header>', html, re.DOTALL)
        self.assertIsNotNone(header, "ヘッダー <header class=\"hdr\"> が見つからない")
        # ヘッダー内ナビは class="nav-bar"（id は無いことがある）→ .hdr .nav-bar で拾う
        self.assertIn('<nav class="nav-bar"', header.group(0))
        # 狭幅時の下部固定ナビは <nav class="nav">
        self.assertIn('<nav class="nav">', html)


class PortalStandaloneDeadLinkTest(unittest.TestCase):
    """portal 単体配布HTMLに「押すと404」になるリンクが残っていないこと。"""

    def _built(self):
        portal = REPO_ROOT / "portal.html"
        if not portal.exists():
            self.skipTest("portal.html 未ビルド")
        html = portal.read_text(encoding="utf-8")
        html, _ = build_selfcontained.hide_cross_page_nav(html)
        html, _ = build_selfcontained.neutralize_links(html)
        return html

    def test_header_nav_hidden_by_class_selector(self):
        html = self._built()
        self.assertIn(".hdr .nav-bar", html)
        self.assertIn("display:none!important", html)

    def test_every_cross_page_href_is_neutralized_or_hidden(self):
        html = self._built()
        targets = set(re.findall(r'href="((?:portal|detail|dept)\.html[^"]*)"', html))
        self.assertTrue(targets, "他ページリンクが1つも無い（前提が変わった）")
        # クリック遮断スクリプトが3ページ全ての接頭辞を対象にしている
        for page in ("portal.html", "detail.html", "dept.html"):
            self.assertIn('a[href^="%s"]' % page, html)


class NeutralizeLinksTest(unittest.TestCase):
    def test_injects_style_and_script_before_body_close(self):
        html = "<html><body>content</body></html>"
        out, n = build_selfcontained.neutralize_links(html)
        self.assertEqual(n, 1)
        self.assertIn('id="selfcontained-links-off"', out)
        self.assertIn('id="selfcontained-links-off-js"', out)
        self.assertIn(".kc .cta{display:none!important}", out)
        self.assertLess(out.index('id="selfcontained-links-off"'), out.index("</body>"))

    def test_no_body_tag_returns_unchanged(self):
        html = "<html><div>no body tag here</div></html>"
        out, n = build_selfcontained.neutralize_links(html)
        self.assertEqual(n, 0)
        self.assertEqual(out, html)


if __name__ == "__main__":
    unittest.main()
