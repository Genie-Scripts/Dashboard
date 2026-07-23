"""AIアラートの品質床（B2 見出しガード）・継続台帳（B4）・文脈注入（A1/A3）のユニットテスト。

LLM呼び出し（chat_json）はテストしない。純関数と台帳の入出力のみ:
  - _alert_reject_reason / _headline_echoes_fact : 見出しの機械検査
  - load_prev_alert_streaks / save_alert_snapshot : 継続台帳の往復と時系列選択
  - _build_user_prompt                            : 継続性ブロックの注入
  - eval_rules.build_alert_context                : 経営方針(A3)・打ち手レバー(A1)の注入
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib import ai_narrative as an
from app.lib import eval_rules as er


class TestHeadlineGuard(unittest.TestCase):
    def _obj(self, headline, body="本文", action="打ち手"):
        return {"headline": headline, "body": body, "action": action}

    def test_parse_and_empty(self):
        self.assertEqual(an._alert_reject_reason(None, {"facts": []}), "parse")
        self.assertEqual(an._alert_reject_reason(self._obj("見出し", body=""), {"facts": []}), "empty")

    def test_headline_too_long(self):
        self.assertEqual(an._alert_reject_reason(self._obj("あ" * 30), {"facts": []}), "headline_long")

    def test_headline_verbatim_echo_rejected(self):
        fact = "新入院数が目標を大きく上回っている"
        self.assertEqual(an._alert_reject_reason(self._obj(fact), {"facts": [fact]}), "headline_echo")

    def test_taigen_dome_accepted(self):
        fact = "新入院数が目標を大きく上回っている"
        self.assertIsNone(an._alert_reject_reason(self._obj("新入院、目標を超過"), {"facts": [fact]}))

    def test_echo_ignores_punctuation_and_spaces(self):
        # 句読点・記号の違いだけの丸写しも検知する（正規化して比較）
        self.assertTrue(an._headline_echoes_fact("在院日数の、延長", ["在院日数の延長"]))

    def test_short_headline_not_echo(self):
        self.assertFalse(an._headline_echoes_fact("延長", ["平均在院日数が延長している"]))


class TestContinuityLedger(unittest.TestCase):
    def test_roundtrip_and_time_order(self):
        with tempfile.TemporaryDirectory() as d:
            an.save_alert_snapshot(d, "2026-06-01", {"a": 2, "b": 1})
            an.save_alert_snapshot(d, "2026-06-08", {"a": 3})
            # base より前の最新（06-08）を採用
            self.assertEqual(an.load_prev_alert_streaks(d, "2026-07-01"), {"a": 3})
            # base 以降のスナップショットは拾わない
            self.assertEqual(an.load_prev_alert_streaks(d, "2026-06-01"), {})

    def test_missing_dir_is_empty(self):
        self.assertEqual(an.load_prev_alert_streaks("/nonexistent/alert/state", "2026-07-01"), {})

    def test_continuing_alert_injects_escalation(self):
        alert = {"id": "x", "category": "kpi", "severity": "warn",
                 "facts": ["手術が目標を下回る"], "meta": {},
                 "_continuity": {"streak": 3, "is_new": False}}
        p = an._build_user_prompt(alert)
        self.assertIn("継続しています", p)
        self.assertIn("数値は書かない", p)   # 継続回数を本文に出させない

    def test_new_alert_injects_first_time(self):
        alert = {"id": "x", "category": "kpi", "severity": "warn",
                 "facts": ["手術が目標を下回る"], "meta": {},
                 "_continuity": {"streak": 1, "is_new": True}}
        self.assertIn("初出です", an._build_user_prompt(alert))

    def test_no_continuity_no_block(self):
        alert = {"id": "x", "category": "kpi", "severity": "warn",
                 "facts": ["手術が目標を下回る"], "meta": {}}
        p = an._build_user_prompt(alert)
        self.assertNotIn("【継続性】", p)


class TestContextInjection(unittest.TestCase):
    def test_dept_group_levers_injected(self):
        # A1: 診療科グループの levers が action の起点として注入される
        ctx = er.build_alert_context({"meta": {"dept": "整形外科"}, "facts": []})
        self.assertIn("打ち手（レバー）", ctx)

    def test_policy_injected_and_removed(self):
        # A3: data/management_policy.yaml があれば「今期の経営方針（最優先）」を注入
        pol = er._POLICY_PATH
        if pol.exists():
            self.skipTest("実 data/management_policy.yaml が存在するためスキップ")
        try:
            pol.parent.mkdir(parents=True, exist_ok=True)
            pol.write_text("priorities:\n  - 今期は手術件数の回復を最優先する\n", encoding="utf-8")
            er.reload()
            ctx = er.build_alert_context({"meta": {}, "facts": []})
            self.assertIn("今期の経営方針（最優先）", ctx)
            self.assertIn("手術件数の回復", ctx)
        finally:
            pol.unlink(missing_ok=True)
            er.reload()
        self.assertNotIn("今期の経営方針", er.build_alert_context({"meta": {}, "facts": []}))


if __name__ == "__main__":
    unittest.main()
