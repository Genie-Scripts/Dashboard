"""B5（称賛先行: 既定タブ切替条件・順位数字なし）のテスト。

`_build_ward_praise_first`/`dept_praise_first`はbuild_portal_context内の派生値のため、
build_portal_context を直接呼ばず（実データ・外部依存を避けるため）テンプレート断面
レンダーで検証する（`tests/test_ai_alerts_display.py`と同型）。加えて html_builder の
top_improvements 集約ロジックのみ、合成 dict を使った純粋な単体テストで担保する。
常駐サーバ・LLMは一切呼ばない。

実行: リポジトリルートで
    python -m pytest tests/test_portal_praise_first_tab.py -v
"""
import sys
import re
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


def _render(**overrides) -> str:
    ctx = {
        "base_date": "2026-08-01", "generated_at": "2026/08/01 09:00",
        "headline": {"level": "ok", "icon": "🏥", "text": "t"},
        "kpi_cards": [], "ai_alerts": [],
        "triage": {"ward": [], "dept_internal": [], "dept_surgery": [],
                   "ward_leveling": [], "dept_leveling": []},
        "improvement": {"ward": [], "dept_internal": [], "dept_surgery": []},
    }
    ctx.update(overrides)
    tmpl = _template_env().get_template("portal.html")
    return tmpl.render(**ctx)


_IMP_ITEM = {"name": "架空病棟X", "href": "dept.html#架空病棟X", "metric_label": "在院",
             "unit": "人", "delta": 5, "compare": "前週同曜日比"}


class WardPraiseFirstDefaultTabTest(unittest.TestCase):
    def test_improvement_tab_active_and_unhidden_when_attention_empty(self):
        html = _render(
            triage={"ward": [], "dept_internal": [], "dept_surgery": [],
                    "ward_leveling": [], "dept_leveling": []},
            improvement={"ward": [_IMP_ITEM], "dept_internal": [], "dept_surgery": []},
            ward_praise_first=True, dept_praise_first=False,
        )
        self.assertRegex(html, r'id="triage-ward-improvement" class="triage-panel" role="tabpanel">')
        self.assertRegex(html, r'id="triage-ward-attention" class="triage-panel" role="tabpanel" hidden>')

    def test_attention_tab_stays_default_when_any_attention_item_exists(self):
        """要注視が1件でもあれば改善が0件でも「要注視」が既定のまま。"""
        html = _render(
            triage={"ward": [{"name": "架空病棟Y", "priority": "mid", "href": "#", "facts": ["f"]}],
                    "dept_internal": [], "dept_surgery": [], "ward_leveling": [], "dept_leveling": []},
            improvement={"ward": [], "dept_internal": [], "dept_surgery": []},
            ward_praise_first=False, dept_praise_first=False,
        )
        self.assertRegex(html, r'id="triage-ward-attention" class="triage-panel" role="tabpanel">')
        self.assertRegex(html, r'id="triage-ward-improvement" class="triage-panel" role="tabpanel" hidden>')

    def test_dept_improvement_tab_active_when_dept_attention_empty(self):
        html = _render(
            triage={"ward": [], "dept_internal": [], "dept_surgery": [],
                    "ward_leveling": [], "dept_leveling": []},
            improvement={"ward": [], "dept_internal": [_IMP_ITEM], "dept_surgery": []},
            ward_praise_first=False, dept_praise_first=True,
        )
        self.assertRegex(html, r'id="triage-dept-improvement" class="triage-panel" role="tabpanel">')
        self.assertRegex(html, r'id="triage-dept-attention" class="triage-panel" role="tabpanel" hidden>')


class NoRankNumberTest(unittest.TestCase):
    def test_imp_rank_never_contains_digit(self):
        html = _render(
            improvement={"ward": [_IMP_ITEM, dict(_IMP_ITEM, name="架空病棟Z")],
                         "dept_internal": [_IMP_ITEM], "dept_surgery": [_IMP_ITEM]},
            ward_praise_first=True, dept_praise_first=True,
            top_improvements=[_IMP_ITEM],
        )
        self.assertIsNone(re.search(r'imp-rank">\d', html),
                          "改善カードの丸バッジに数字が出てはいけない（順位付けの禁止）")
        self.assertIn('imp-rank">🎉', html)


class TopImprovementsSectionTest(unittest.TestCase):
    def test_top_improvements_rendered_before_triage_sec_max_3(self):
        items = [dict(_IMP_ITEM, name=f"架空部門{i}") for i in range(3)]
        html = _render(top_improvements=items)
        self.assertIn("今週よくなった部門", html)
        idx_top = html.find("今週よくなった部門")
        idx_triage = html.find('class="triage-sec"')
        self.assertGreater(idx_top, 0)
        self.assertGreater(idx_triage, 0)
        self.assertLess(idx_top, idx_triage)
        self.assertEqual(html.count("架空部門0") + html.count("架空部門1") + html.count("架空部門2"), 3)

    def test_section_absent_when_no_top_improvements(self):
        html = _render(top_improvements=[])
        self.assertNotIn("今週よくなった部門", html)


if __name__ == "__main__":
    unittest.main()
