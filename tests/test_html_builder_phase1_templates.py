"""訴求力強化Phase1(A1〜A9)のテンプレ断面レンダーテスト。

`tests/test_ai_alerts_display.py` と同じ手法（jinja2テンプレートの部分レンダー・
ChainableUndefinedで無関係コンテキストを未定義許容にする）で、以下の文言が
生成HTMLに出ることを確認する:
  「先週の確定」「今週ここまで」「週目標」「今週の暦なら」「通常変動帯」「年度」
LLM(oMLX)・常駐サーバは一切呼ばない。detail.html/dept.htmlは data_json="{}" の
JS文字列のみで検証する箇所（通常変動帯のトグルは静的マークアップ）を含む。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jinja2 import Environment, ChainableUndefined

from generate_html import _build_jinja_env


def _template_env() -> Environment:
    base_env = _build_jinja_env()
    env = Environment(loader=base_env.loader, undefined=ChainableUndefined,
                      autoescape=base_env.autoescape)
    env.filters.update(base_env.filters)
    return env


def _render_portal(**extra) -> str:
    tmpl = _template_env().get_template("portal.html")
    ctx = {
        "base_date": "2026-08-01", "generated_at": "2026/08/01 09:00",
        "headline": {"level": "ok", "icon": "🏥", "text": "テスト見出し"},
        "kpi_cards": [],
        "freshness": {"text": "データは 8/1(土) まで｜8/1 09:00 作成｜平日の朝に更新",
                     "stale": False, "prev_label": None},
    }
    ctx.update(extra)
    return tmpl.render(**ctx)


class TestA2LastWeekPrefix(unittest.TestCase):
    """A2(§7裁定): 月曜ビューのみ週次ストーリー見出しに前置。"""

    def test_prefix_renders_before_headline(self):
        html = _render_portal(last_week_prefix="先週の確定 8/24〜8/30")
        self.assertIn("先週の確定 8/24〜8/30", html)

    def test_no_prefix_when_none(self):
        html = _render_portal(last_week_prefix=None)
        self.assertNotIn("先週の確定", html)


class TestA1WeekNoteOnCard(unittest.TestCase):
    """A1: portal KPIカードの「今週ここまで」行。"""

    def _card(self, **overrides):
        card = {
            "id": "admission", "icon": "🚪", "label": "新入院患者数", "period": "直近7日累計",
            "value": 374, "unit": "人", "gap": -5, "gap_unit": "人",
            "status": {"css": "wr", "shape": "―", "text": "接近"},
            "href": "detail.html#admission",
        }
        card.update(overrides)
        return card

    def test_week_note_renders(self):
        card = self._card(week_note="今週ここまで 293人／按分目標303人（97%）")
        html = _render_portal(kpi_cards=[card])
        self.assertIn("今週ここまで", html)
        self.assertIn("按分目標", html)

    def test_no_week_note_when_none(self):
        # ★CSSコメントに定型句「今週ここまで」が出るため、機能マーカー(kc-week要素)の
        #   有無で判定する（生テキスト検索はCSSコメントで偽陽性になる）。
        card = self._card(week_note=None)
        html = _render_portal(kpi_cards=[card])
        self.assertNotIn('class="kc-week"', html)

    def test_dual_target_note_renders_both_labels(self):
        """Phase0持ち越し: 「週目標」「今週の暦なら」の両語彙が出る（祝日週のみ発火）。"""
        card = self._card(dual_target_note="週目標 379人／今週の暦なら 303人（営業日4/5）")
        html = _render_portal(kpi_cards=[card])
        self.assertIn("週目標", html)
        self.assertIn("今週の暦なら", html)


class TestA3WindowNotes(unittest.TestCase):
    def test_window_note_text_renders(self):
        html = _render_portal(window_notes={
            7: {"window": 7, "biz_days": 4, "std_days": 5,
                "text": "この7日間は営業日4日（9/23 秋分の日）。新入院と手術はその分少なく出ます。"}
        })
        self.assertIn("この7日間は営業日4日", html)

    def test_no_section_when_empty(self):
        html = _render_portal(window_notes={})
        self.assertNotIn("暦注記", html)


class TestA7FyProgress(unittest.TestCase):
    def test_fy_progress_text_contains_nendo(self):
        html = _render_portal(fy_progress={
            "text": "年度 4/1〜9/3: 新入院 累計379人／按分目標303人（79%）・"
                    "全麻 21.0件/日／目標21件/日（100%）・在院 年度平均580人／目標583人（99%）",
        })
        self.assertIn("年度", html)
        self.assertIn("按分目標", html)


def _render_bare_detail_json(template_name: str) -> str:
    tmpl = _template_env().get_template(template_name)
    return tmpl.render(base_date="2026-09-03", generated_at="2026/09/04 07:39",
                       data_json="{}")


class TestA6BandToggleStaticMarkup(unittest.TestCase):
    """A6: 「通常変動帯」チェックボックスはJS内の静的マークアップのため
    data_json の中身に関わらず detail.html/dept.html に常に出る。"""

    def test_detail_has_band_toggle_label(self):
        html = _render_bare_detail_json("detail.html")
        self.assertIn("通常変動帯", html)

    def test_dept_has_band_toggle_label(self):
        html = _render_bare_detail_json("dept.html")
        self.assertIn("通常変動帯", html)


class TestFreshnessTextPattern(unittest.TestCase):
    """共通規約§3: 「データは … まで」。"""

    def test_portal_contains_pattern(self):
        html = _render_portal()
        self.assertIn("データは", html)
        self.assertIn("まで", html)

    def test_detail_dept_have_freshness_id(self):
        for name in ("detail.html", "dept.html"):
            html = _render_bare_detail_json(name)
            self.assertIn('id="freshness"', html)


if __name__ == "__main__":
    unittest.main()
