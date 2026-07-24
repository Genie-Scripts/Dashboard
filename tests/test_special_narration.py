"""特例ユニット（救急病棟/重症ケア病棟/救急科）のナレーション分岐の回帰テスト。

これらは「予定入院・紹介」という業務前提が無く、専用プロンプトを使う。
- 4A/4C  = 救命救急病棟（稼働率が最終目標・救急受け入れ／ICU・HCU連携）
- ICU/HCU = 重症ケア病棟（在院・稼働率が最終目標・院内急変/緊急術後受け入れ）
- 救急科  = ER（応需台数・ER滞在時間短縮が北極星）
- 眼科    = 全手術KPIの特例（群未登録だが peer 表示は「外科系」）
"""
import unittest

from app.lib.dept_report import _special_narration_kind
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


if __name__ == "__main__":
    unittest.main()
