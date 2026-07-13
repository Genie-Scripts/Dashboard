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
