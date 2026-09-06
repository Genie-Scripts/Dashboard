"""B4（マイ部門 localStorage['myUnit']）の回帰テスト。

dept.html側のJS（保存/解除/QR自動保存）はDashboardにJSテストハーネスが無いため、ソース検査
（既存`test_mobile_chart_lock.py`と同様の型）で構造の存在を担保する。portal.html側の導線は
`_build_jinja_env()`のテンプレ断面レンダーで「保存済みのときのみ表示」の初期状態
（display:none既定・JS側で切替）を確認する。常駐サーバ・LLMは一切呼ばない。

実行: リポジトリルートで
    python -m pytest tests/test_my_unit.py -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jinja2 import Environment, ChainableUndefined

from generate_html import _build_jinja_env

ROOT = Path(__file__).resolve().parent.parent
DEPT_HTML = ROOT / "app" / "templates" / "dept.html"
PORTAL_HTML = ROOT / "app" / "templates" / "portal.html"


class DeptMyUnitJsTest(unittest.TestCase):
    def setUp(self):
        self.src = DEPT_HTML.read_text(encoding="utf-8")

    def test_get_set_clear_toggle_functions_exist(self):
        for fn in ("function getMyUnit()", "function setMyUnit(name)",
                   "function clearMyUnit()", "function toggleMyUnit(name, ev)"):
            with self.subTest(fn=fn):
                self.assertIn(fn, self.src)

    def test_localstorage_access_wrapped_in_try_catch(self):
        self.assertIn("localStorage.getItem('myUnit')", self.src)
        self.assertIn("localStorage.setItem('myUnit'", self.src)
        self.assertIn("localStorage.removeItem('myUnit')", self.src)
        # try/catchで包まれている（設計指示）
        self.assertIn("try { return localStorage.getItem('myUnit')", self.src)

    def test_qr_landing_auto_saves_my_unit(self):
        """dept.html#<unit> への直接着地（DOMContentLoaded分岐）でsetMyUnitが呼ばれること。"""
        idx_hash_check = self.src.find("if (hash && DATA.drill[hash]) {\n    // B4")
        self.assertGreater(idx_hash_check, -1)
        snippet = self.src[idx_hash_check:idx_hash_check + 200]
        self.assertIn("setMyUnit(hash)", snippet)

    def test_star_button_present_in_card_html(self):
        self.assertIn("pin-btn", self.src)
        self.assertIn("toggleMyUnit('${escName}', event)", self.src)

    def test_my_unit_landing_section_label(self):
        self.assertIn("⭐ あなたの部門", self.src)


class PortalMyUnitLinkTest(unittest.TestCase):
    def _template_env(self) -> Environment:
        base_env = _build_jinja_env()
        env = Environment(loader=base_env.loader, undefined=ChainableUndefined,
                          autoescape=base_env.autoescape)
        env.filters.update(base_env.filters)
        return env

    def test_link_hidden_by_default_and_js_reads_myunit(self):
        tmpl = self._template_env().get_template("portal.html")
        html = tmpl.render(
            base_date="2026-08-01", generated_at="2026/08/01 09:00",
            headline={"level": "ok", "icon": "🏥", "text": "t"}, kpi_cards=[], ai_alerts=[],
        )
        self.assertIn('id="myUnitLink"', html)
        self.assertIn('style="display:none"', html)
        self.assertIn("自科/自病棟を見る", html)
        # JS側: localStorageのmyUnitを読んで表示を切り替える（try/catchで包む）
        self.assertIn("localStorage.getItem('myUnit')", html)
        self.assertIn("try{", html)


if __name__ == "__main__":
    unittest.main()
