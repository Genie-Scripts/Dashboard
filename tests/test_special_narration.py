"""特例ユニット（救急病棟/重症ケア病棟/救急科）のナレーション分岐の回帰テスト。

これらは「予定入院・紹介」という業務前提が無く、専用プロンプトを使う。
- 4A/4C  = 救命救急病棟（稼働率が最終目標・救急受け入れ／ICU・HCU連携）
- ICU/HCU = 重症ケア病棟（在院・稼働率が最終目標・院内急変/緊急術後受け入れ）
- 救急科  = ER（応需台数・ER滞在時間短縮が北極星）
- 眼科    = 全手術KPIの特例（群未登録だが peer 表示は「外科系」）
"""
import unittest

from app.lib.config import unit_narration_kind
from app.lib.dept_report import _special_narration_kind, _fallback_move_ward_admission
from app.lib.eval_rules import dept_group_label
from app.lib import ai_narrative as an


class TestSpecialNarrationKind(unittest.TestCase):
    def test_emergency_wards(self):
        self.assertEqual(_special_narration_kind("ward", "04A", "4階A病棟"), "emergency")
        self.assertEqual(_special_narration_kind("ward", "04C", "4階C病棟"), "emergency")

    def test_critical_care_wards(self):
        self.assertEqual(_special_narration_kind("ward", "04B", "ICU"), "critical_care")
        self.assertEqual(_special_narration_kind("ward", "04D", "HCU"), "critical_care")

    def test_er_dept(self):
        self.assertEqual(_special_narration_kind("dept", "", "救急科"), "er_dept")

    def test_normal_units_are_none(self):
        self.assertIsNone(_special_narration_kind("ward", "05A", "5階A病棟"))
        self.assertIsNone(_special_narration_kind("dept", "", "消化器内科"))
        self.assertIsNone(_special_narration_kind("dept", "", "眼科"))  # 眼科は特例だが病棟系ではない


class TestUnitNarrationKindByNameOnly(unittest.TestCase):
    """unit_narration_kind は病棟コードが解決できず名称のみでも特例を判定できること
    （呼び出し側のコード解決が失敗して ICU が一般病棟扱いされる回帰の防止）。"""

    def test_icu_hcu_by_name_only(self):
        self.assertEqual(unit_narration_kind("ward", code=None, name="ICU"), "critical_care")
        self.assertEqual(unit_narration_kind("ward", code=None, name="HCU"), "critical_care")

    def test_emergency_ward_by_name_only(self):
        self.assertEqual(unit_narration_kind("ward", code=None, name="4階A病棟"), "emergency")

    def test_normal_ward_by_name_only(self):
        self.assertIsNone(unit_narration_kind("ward", code=None, name="5階A病棟"))


class TestOphthalmologyPeerLabel(unittest.TestCase):
    def test_gan_ka_is_gaikakei(self):
        # 群には入れない（GA前提の rules/levers を避ける）が peer 表示は外科系
        self.assertEqual(dept_group_label("眼科"), "外科系")


class TestSpecialPromptsExcludePlannedAdmission(unittest.TestCase):
    """全特例の banned に「予定入院」「紹介」が含まれること（一次防御）。"""

    def test_banned_tuples(self):
        for banned in (
            an._EMERGENCY_LEVELING_BANNED, an._EMERGENCY_ADMISSION_BANNED,
            an._CRITICAL_CARE_LEVELING_BANNED, an._CRITICAL_CARE_ADMISSION_BANNED,
            an._ER_LEVELING_BANNED, an._ER_ADMISSION_BANNED,
        ):
            self.assertIn("予定入院", banned)
            self.assertIn("紹介", banned)

    def test_prompts_do_not_invite_digit_echo(self):
        # 状態ヘッダに数字を書かない（本文への数値エコー→digitガード棄却→定型文化を避ける）。
        for p in (an._build_er_admission_prompt("救急科", "目標を下回っている"),
                  an._build_er_leveling_prompt("救急科", "落ち込んでいる")):
            self.assertNotIn("7日", p)


class TestWardAdmissionBannedTerms(unittest.TestCase):
    """一般病棟（特例でない病棟）向け新入院プロンプトの禁止語に、診療科専用レバー
    （紹介・地域医療連携）が含まれること（機械ガードによる多重防衛）。"""

    def test_banned_includes_referral_terms(self):
        self.assertIn("紹介", an._WARD_ADMISSION_BANNED)
        self.assertIn("地域医療連携", an._WARD_ADMISSION_BANNED)


class TestWardAdmissionPromptExcludesReferral(unittest.TestCase):
    """一般病棟向けプロンプトに「紹介元への働きかけ」等の推奨形が出ないこと。

    プロンプト文には「〜は提案しない」という禁止文として「紹介」の語が意図的に現れる
    （厳守事項の中で明示的に禁止するため）ので、単純な assertNotIn("紹介", ...) は
    使えない。ここでは推奨形（診療科向け実例文に出る言い回し）が無いことと、
    禁止文自体が存在することの双方を確認する。
    """

    def test_system_prompt_bans_not_recommends_referral(self):
        text = an.WARD_ADMISSION_ACTION_SYSTEM_PROMPT
        self.assertNotIn("紹介元への働きかけを強化", text)
        self.assertNotIn("紹介受け入れの重点化", text)
        # 禁止文として「紹介元への働きかけ」が明示されていること
        self.assertIn("「紹介元への働きかけ」", text)
        self.assertIn("は提案しない", text)

    def test_user_prompt_bans_not_recommends_referral(self):
        text = an._build_ward_admission_prompt("5階A病棟", "目標を下回っている")
        self.assertNotIn("紹介元への働きかけを強化", text)
        self.assertNotIn("紹介受け入れの重点化", text)
        self.assertIn("紹介元への働きかけ・地域医療連携・予定入院枠の前倒しは書かない", text)


class TestFallbackMoveWardAdmission(unittest.TestCase):
    """dept_report._fallback_move_ward_admission の body/action に「紹介」
    「地域医療連携」が含まれないこと（未達・達成・達成かつ鈍化の3状態すべて）。"""

    def _assert_no_referral(self, move):
        for key in ("body", "action"):
            self.assertNotIn("紹介", move[key])
            self.assertNotIn("地域医療連携", move[key])

    def test_not_met(self):
        move = _fallback_move_ward_admission("目標を明確に下回っている")
        self._assert_no_referral(move)

    def test_met(self):
        move = _fallback_move_ward_admission("目標を達成している")
        self._assert_no_referral(move)

    def test_met_but_slowing(self):
        move = _fallback_move_ward_admission("目標を達成しているが、直近は伸びが鈍ってきている")
        self._assert_no_referral(move)


if __name__ == "__main__":
    unittest.main()
