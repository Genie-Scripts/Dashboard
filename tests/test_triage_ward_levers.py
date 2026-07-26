"""ポータル部門トリアージの「ユニットの役割」対応の回帰テスト。

病棟は外来も地域連携の窓口も持たないため、診療科向けの打ち手（紹介患者の確保・
地域医療連携の強化）は病棟・特例ユニット（ICU/HCU・4A/4C・救急科）の suggestion に
出てはいけない。LLM は呼ばず、プロンプト文字列と Python 定型文のみを検査する。
"""
import unittest

from app.lib import triage
from app.lib import eval_rules
from app.lib.config import WARD_BANNED_LEVER_TERMS


def _dept_item(name="消化器内科", primary_kpi="inp", **overrides) -> dict:
    """_build_triage_prompt / _make_fallback_narrative が参照するキーのみを持つ診療科 item。"""
    item = {
        "entity_type": "dept",
        "ward_code": None,
        "name": name,
        "entity_label": "科",
        "facts": ["在院患者: 実績85人 / 目標100人（達成率85%・目標まであと15.0人）"],
        "rank_from_bottom": 2,
        "total_items": 5,
        "priority": "high",
        "primary_kpi": primary_kpi,
        "status_kind": "below",
        "improving": False,
        "worsening": False,
        "surgery_strong": False,
        "primary_is_fallback": False,
    }
    item.update(overrides)
    return item


def _ward_item(name="5階A病棟", ward_code="05A", **overrides) -> dict:
    item = {
        "entity_type": "ward",
        "ward_code": ward_code,
        "name": name,
        "entity_label": "病棟",
        "facts": ["在院患者: 実績80人 / 目標90人（達成率89%・目標まであと10.0人）"],
        "rank_from_bottom": 1,
        "total_items": 3,
        "priority": "mid",
        "primary_kpi": "inp",
        "status_kind": "below",
        "improving": False,
        "worsening": False,
        "surgery_strong": False,
        "primary_is_fallback": False,
    }
    item.update(overrides)
    return item


class TestUnitKind(unittest.TestCase):
    """_unit_kind がユニットの役割種別を正しく判定すること。"""

    def test_general_ward_is_ward(self):
        self.assertEqual(triage._unit_kind(_ward_item()), "ward")

    def test_icu_hcu_are_critical_care(self):
        self.assertEqual(
            triage._unit_kind(_ward_item(name="ICU", ward_code="04B")), "critical_care")
        self.assertEqual(
            triage._unit_kind(_ward_item(name="HCU", ward_code="04D")), "critical_care")

    def test_4a_4c_are_emergency(self):
        self.assertEqual(
            triage._unit_kind(_ward_item(name="4階A病棟", ward_code="04A")), "emergency")
        self.assertEqual(
            triage._unit_kind(_ward_item(name="4階C病棟", ward_code="04C")), "emergency")

    def test_er_dept_is_er_dept(self):
        item = _dept_item(name="救急科")
        self.assertEqual(triage._unit_kind(item), "er_dept")

    def test_internal_dept_is_none(self):
        self.assertIsNone(triage._unit_kind(_dept_item(name="消化器内科")))


class TestBuildTriagePromptWard(unittest.TestCase):
    """病棟 item のプロンプトに【打ち手（レバー）】と【禁止】が入り、goal 行に紹介が無いこと。"""

    def test_ward_prompt_has_levers_and_prohibition(self):
        prompt = triage._build_triage_prompt(_ward_item())
        self.assertIn("【このユニットで使える打ち手（レバー）】", prompt)
        self.assertIn("【禁止】", prompt)

        goal_line = next(
            line for line in prompt.splitlines() if line.startswith("【この病棟の目標KPI】"))
        self.assertNotIn("紹介", goal_line)

    def test_critical_care_prompt_goal_line(self):
        item = _ward_item(name="ICU", ward_code="04B")
        prompt = triage._build_triage_prompt(item)
        self.assertIn("【このユニットで使える打ち手（レバー）】", prompt)
        self.assertIn("【禁止】", prompt)
        goal_line = next(
            line for line in prompt.splitlines() if line.startswith("【この病棟の目標KPI】"))
        self.assertNotIn("紹介", goal_line)

    def test_er_dept_prompt_goal_line(self):
        item = _dept_item(name="救急科", primary_kpi="inp")
        prompt = triage._build_triage_prompt(item)
        self.assertIn("【このユニットで使える打ち手（レバー）】", prompt)
        self.assertIn("【禁止】", prompt)
        goal_line = next(
            line for line in prompt.splitlines() if line.startswith("【この科の目標KPI】"))
        self.assertNotIn("紹介", goal_line)


class TestBuildTriagePromptDeptUnchanged(unittest.TestCase):
    """内科系診療科の item は本変更の前後で出力が同一であること（回帰）。"""

    def test_internal_dept_prompt_unchanged(self):
        item = _dept_item(name="消化器内科", primary_kpi="inp")
        prompt = triage._build_triage_prompt(item)

        facts_block = "\n".join(f"- {f}" for f in item["facts"])
        expected = f"""以下の確定事実を要約し、JSON を1つだけ出力してください。

【科】消化器内科（下位2位 / 全5科）
【優先度】high
【この科の目標KPI】在院患者数の増加（レバー: 新入院・紹介患者の確保）

【確定事実】
{facts_block}

【注意】
- priority は必ず "high" を出力すること（Python で再検証する）
- headline / observation / suggestion / priority の4キーを持つ JSON を出力すること
- 「合成達成率」という語句・その数値は出力しないこと
- 事実にない数値・原因・人物を補わないこと
- JSON 以外の文字（```、前置き、末尾コメント）を出力しないこと"""
        self.assertEqual(prompt, expected)
        self.assertNotIn("【このユニットで使える打ち手", prompt)
        self.assertNotIn("【禁止】", prompt)


class TestFallbackNarrativeWard(unittest.TestCase):
    """病棟・ICU/HCU・救急科の fallback suggestion に紹介・地域医療連携が含まれないこと。"""

    def _assert_clean(self, item: dict):
        for state_overrides in (
            {"status_kind": "watch", "improving": False},          # 達成中だが悪化傾向
            {"status_kind": "below", "improving": True},           # 未達だが改善傾向
            {"status_kind": "below", "improving": False},          # 通常（未達）
        ):
            it = dict(item, **state_overrides)
            narrative = triage._make_fallback_narrative(it)
            suggestion = narrative["suggestion"]
            for term in WARD_BANNED_LEVER_TERMS:
                self.assertNotIn(term, suggestion,
                                  f"{it['name']} ({state_overrides}) の suggestion に禁止語: {suggestion}")

    def test_general_ward(self):
        self._assert_clean(_ward_item())

    def test_icu(self):
        self._assert_clean(_ward_item(name="ICU", ward_code="04B"))

    def test_hcu(self):
        self._assert_clean(_ward_item(name="HCU", ward_code="04D"))

    def test_emergency_ward(self):
        self._assert_clean(_ward_item(name="4階A病棟", ward_code="04A"))

    def test_er_dept(self):
        self._assert_clean(_dept_item(name="救急科", primary_kpi="inp"))


class TestBuildAlertContextWard(unittest.TestCase):
    """eval_rules.build_alert_context が病棟アラートにレバーを注入すること。"""

    def test_general_ward_has_levers_no_referral(self):
        ctx = eval_rules.build_alert_context(
            {"meta": {"ward": "5階A病棟", "ward_code": "05A"}})
        self.assertIn("で使える打ち手（レバー）", ctx)
        self.assertNotIn("紹介", ctx)

    def test_icu_has_critical_care_levers(self):
        ctx = eval_rules.build_alert_context(
            {"meta": {"ward": "ICU", "ward_code": "04B"}})
        self.assertIn("院内急変", ctx)
        self.assertNotIn("紹介", ctx)


if __name__ == "__main__":
    unittest.main()
