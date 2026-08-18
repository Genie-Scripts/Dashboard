"""汎用AIアラート(ai_alerts)のportal.html表示復活のユニットテスト。

⚠️ PUBLIC リポにつき、実在の診療科名・実際のレポート文は書かない。架空科A/架空病棟X等の
合成データのみ使う。LLM(oMLX)・常駐サーバは一切呼ばない
（1. は alerts.detect_alerts / ai_narrative.narrate_alerts を mock.patch で密閉、
  2./3. は jinja2 のテンプレートレンダーのみ・generate_html.py の main() は呼ばない
  ＝ `_build_jinja_env()` というEnvironment構築ヘルパをimportして再利用するだけ）。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jinja2 import Environment, ChainableUndefined

from app.lib import html_builder
from generate_html import _build_jinja_env


def _mixed_category_alerts():
    """kpi/dept/ward/momentumが混在する合成alertリスト（alerts.detect_alerts相当）。"""
    return [
        {"id": "kpi_inpatient_underperform", "severity": "danger", "category": "kpi",
         "icon": "🛏️", "title_fallback": "在院患者数が目標未達",
         "facts": ["在院患者数の目標達成率が警戒水準を下回っている"],
         "meta": {"kpi": "inpatient", "href": "detail.html#inpatient"}},
        {"id": "kpi_admission_underperform", "severity": "warn", "category": "kpi",
         "icon": "🚪", "title_fallback": "新入院患者数が目標未達",
         "facts": ["新入院患者数の目標達成率が警戒水準を下回っている"],
         "meta": {"kpi": "admission", "href": "detail.html#admission"}},
        {"id": "dept_admission_架空科A", "severity": "warn", "category": "dept",
         "icon": "🏥", "title_fallback": "架空科Aの新入院が低調",
         "facts": ["f"], "meta": {"href": "dept.html#架空科A"}},
        {"id": "ward_inpatient_架空病棟X", "severity": "warn", "category": "ward",
         "icon": "🛏️", "title_fallback": "架空病棟Xの在院低下",
         "facts": ["f"], "meta": {"href": "dept.html#架空病棟X"}},
        {"id": "momentum_架空科B", "severity": "info", "category": "momentum",
         "icon": "📈", "title_fallback": "架空科Bの新入院が回復",
         "facts": ["f"], "meta": {"href": "dept.html#架空科B"}},
    ]


# ════════════════════════════════════════════════════════════
# 1. _build_ai_alerts のフィルタ検証
# ════════════════════════════════════════════════════════════
class TestBuildAiAlertsFiltersToKpi(unittest.TestCase):
    def test_only_kpi_category_passed_to_narrate_alerts(self):
        mixed = _mixed_category_alerts()
        with mock.patch("app.lib.alerts.detect_alerts", return_value=mixed) as m_detect, \
             mock.patch("app.lib.ai_narrative.narrate_alerts", return_value=[]) as m_narrate:
            html_builder._build_ai_alerts(None, None, None, None, None)

        m_detect.assert_called_once()
        m_narrate.assert_called_once()
        passed_raw = m_narrate.call_args[0][0]   # narrate_alerts(raw, state_dir=..., base_date=...)
        self.assertEqual([a["id"] for a in passed_raw],
                         ["kpi_inpatient_underperform", "kpi_admission_underperform"])
        self.assertTrue(all(a["category"] == "kpi" for a in passed_raw))

    def test_narrate_alerts_not_called_when_no_kpi_alerts(self):
        """kpi以外だけの検知結果はナレーション自体を呼ばない（非表示カードへのLLMコール停止）。"""
        non_kpi = [a for a in _mixed_category_alerts() if a["category"] != "kpi"]
        with mock.patch("app.lib.alerts.detect_alerts", return_value=non_kpi), \
             mock.patch("app.lib.ai_narrative.narrate_alerts") as m_narrate:
            result = html_builder._build_ai_alerts(None, None, None, None, None)
        m_narrate.assert_not_called()
        self.assertEqual(result, [])


# ════════════════════════════════════════════════════════════
# 2./3. テンプレート断面レンダー
# ════════════════════════════════════════════════════════════
def _template_env() -> Environment:
    """html_builder(generate_html.py)が実際に使うEnvironment構築を再利用しつつ、
    無関係なコンテキスト（headline以外の大半）を未定義許容にするため ChainableUndefined を
    足した Environment を作る（loader/filtersは _build_jinja_env() のものをそのまま使う）。
    """
    base_env = _build_jinja_env()
    env = Environment(loader=base_env.loader, undefined=ChainableUndefined,
                      autoescape=base_env.autoescape)
    env.filters.update(base_env.filters)
    return env


def _render_portal(ai_alerts=None, include_ai_alerts_key=True) -> str:
    """portal.html全体をレンダーする。ai_alerts以外は未定義のまま
    （headline/kpi_cardsのみ、テンプレートの他セクションが例外を出さないための最小限）。"""
    tmpl = _template_env().get_template("portal.html")
    ctx = {
        "base_date": "2026-08-01", "generated_at": "2026/08/01 09:00",
        "headline": {"level": "ok", "icon": "🏥", "text": "テスト見出し"},
        "kpi_cards": [],
    }
    if include_ai_alerts_key:
        ctx["ai_alerts"] = ai_alerts if ai_alerts is not None else []
    return tmpl.render(**ctx)


_AI_SEC_MARKER = 'class="ai-sec"'


class TestPortalTemplateAiAlertsSection(unittest.TestCase):
    def test_headline_body_action_and_continuity_badge_render(self):
        alerts = [{
            "id": "kpi_inpatient_underperform", "severity": "danger", "category": "kpi",
            "icon": "🛏️", "title_fallback": "在院患者数が目標未達",
            "facts": ["在院患者数の目標達成率が警戒水準を下回っている"],
            "meta": {"href": "detail.html#inpatient"},
            "narrative": {"headline": "在院、目標未達",
                         "body": "在院患者数が目標を下回って推移しています。",
                         "action": "新入院・紹介患者の確保に取り組みましょう。"},
            "continuity": {"streak": 3, "is_new": False},
        }]
        html = _render_portal(alerts)
        self.assertIn(_AI_SEC_MARKER, html)
        self.assertIn("在院、目標未達", html)
        self.assertIn("在院患者数が目標を下回って推移しています。", html)
        self.assertIn("新入院・紹介患者の確保に取り組みましょう。", html)
        self.assertIn("継続 3回目", html)

    def test_no_narrative_falls_back_to_title_and_rule_generated_tag(self):
        alerts = [{
            "id": "kpi_admission_underperform", "severity": "warn", "category": "kpi",
            "icon": "🚪", "title_fallback": "新入院患者数が目標未達",
            "facts": ["新入院患者数の目標達成率が警戒水準を下回っている", "前週同期より悪化傾向"],
            "meta": {"href": "detail.html#admission"},
            "narrative": None,
        }]
        html = _render_portal(alerts)
        self.assertIn(_AI_SEC_MARKER, html)
        self.assertIn("新入院患者数が目標未達", html)
        self.assertIn("（ルール生成）", html)
        # narrative無し時は facts が「／」連結で本文になる
        self.assertIn("新入院患者数の目標達成率が警戒水準を下回っている／前週同期より悪化傾向", html)
        # continuityバッジは継続2回以上のときだけ（このケースは無し）
        self.assertNotIn("継続", html)

    def test_new_continuity_or_streak_one_shows_no_badge(self):
        """is_new=True、またはstreak<2のときは継続バッジを出さない。"""
        alerts = [{
            "id": "kpi_operation_underperform", "severity": "warn", "category": "kpi",
            "icon": "💉", "title_fallback": "全身麻酔手術が目標未達",
            "facts": ["f"], "meta": {"href": "detail.html#operation"},
            "narrative": {"headline": "全麻、目標未達", "body": "b", "action": ""},
            "continuity": {"streak": 1, "is_new": True},
        }]
        html = _render_portal(alerts)
        self.assertIn(_AI_SEC_MARKER, html)
        self.assertNotIn("継続", html)

    def test_section_absent_when_no_ai_alerts(self):
        self.assertNotIn(_AI_SEC_MARKER, _render_portal([]))
        # ai_alerts キー自体を渡さない（テンプレ未定義）場合も出ない
        self.assertNotIn(_AI_SEC_MARKER, _render_portal(include_ai_alerts_key=False))

    def test_missing_continuity_key_does_not_raise(self):
        """continuityキー自体が無いalertでもレンダーが例外にならない。"""
        alerts = [{
            "id": "kpi_x", "severity": "warn", "category": "kpi", "icon": "💉",
            "title_fallback": "全身麻酔手術が目標未達", "facts": ["f"],
            "meta": {"href": "detail.html#operation"},
            # narrative/continuity どちらも無し
        }]
        try:
            html = _render_portal(alerts)
        except Exception as e:  # noqa: BLE001
            self.fail(f"continuityキー欠落でレンダーが例外を送出した: {e}")
        self.assertIn(_AI_SEC_MARKER, html)
        self.assertIn("全身麻酔手術が目標未達", html)
        self.assertIn("（ルール生成）", html)


if __name__ == "__main__":
    unittest.main()
