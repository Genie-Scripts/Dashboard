"""部門レポート「この期間の一手」のトピック選定と直接文言のユニットテスト。

全麻(surgery)のトピック別足切り（外科系98%=0.02 / 病院全体95%=0.05）と、
未達 action の直接文言化（件数増に専念／患者数増に取り組む）を純関数で検証する。
LLM呼び出しはテストしない。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.dept_report import (_select_action_topic, _select_hospital_topic,
                                 _fallback_move_surgery, _fallback_move_admission,
                                 SURGERY_TOPIC_MIN_SCORE,
                                 SURGERY_TOPIC_MIN_SCORE_HOSPITAL)


class SelectActionTopicTest(unittest.TestCase):
    def test_surgical_97pct_becomes_primary(self):
        # 外科系: 全麻97%(スコア0.03≥0.02)のみ eligible → 主トピック=surgery
        # leveling=0.05(<0.12足切り)・admission=目標達成(0)
        p, s, sc = _select_action_topic("surgical", 0.05, 1.0, 10, 10, 9.7, 10)
        self.assertEqual(p, "surgery")
        self.assertAlmostEqual(sc["surgery"], 0.03, places=4)

    def test_surgical_99pct_falls_back_to_leveling(self):
        # 外科系: 全麻99%(0.01<0.02) は足切り → eligible無し → leveling 既定
        p, s, _ = _select_action_topic("surgical", 0.05, 1.0, 10, 10, 9.9, 10)
        self.assertEqual(p, "leveling")

    def test_surgical_96pct_as_secondary(self):
        # 外科系: leveling 0.5 が主でも 全麻96%(0.04≥0.02) は副トピックで言及される
        p, s, _ = _select_action_topic("surgical", 0.5, 1.0, 10, 10, 9.6, 10)
        self.assertEqual((p, s), ("leveling", "surgery"))

    def test_internal_never_has_surgery(self):
        # 内科系: 全麻データがあっても候補にならない（回帰）
        p, s, sc = _select_action_topic("internal", 0.05, 1.0, 10, 10, 5, 10)
        self.assertEqual(p, "leveling")
        self.assertNotIn("surgery", sc)

    def test_surgery_beats_slightly_larger_leveling(self):
        # 全麻優先の意図的非対称: leveling(0.03<0.12足切りで落選) と 全麻(0.025≥0.02)
        # → 生スコアは leveling の方が大きいが eligible は surgery のみ → 主=surgery
        p, s, sc = _select_action_topic("surgical", 0.03, 1.0, 10, 10, 9.75, 10)
        self.assertEqual(p, "surgery")

    def test_internal_call_matches_default(self):
        # 既定 surgery_min を渡さない呼び出しでも内科系挙動は不変
        self.assertEqual(SURGERY_TOPIC_MIN_SCORE, 0.02)


class SelectHospitalTopicTest(unittest.TestCase):
    def test_reproduces_20260705(self):
        # 病院全体: 2026-07-05 実測値の再現
        # leveling 0.088(<0.12) admission 0.055(<0.12) surgery 0.081(≥0.05)
        # → surgery だけ eligible → "surgery"
        scores = {"leveling": 0.088, "admission": 0.055, "surgery": 0.081}
        self.assertEqual(_select_hospital_topic(scores), "surgery")

    def test_surgery_94pct_eligible(self):
        # 全麻達成率94%(0.06≥0.05) → surgery が主
        self.assertEqual(SURGERY_TOPIC_MIN_SCORE_HOSPITAL, 0.05)
        scores = {"leveling": 0.03, "admission": 0.03, "surgery": 0.06}
        self.assertEqual(_select_hospital_topic(scores), "surgery")

    def test_surgery_96pct_below_threshold(self):
        # 全麻達成率96%(0.04<0.05) かつ他も足切り未満 → leveling 既定
        scores = {"leveling": 0.03, "admission": 0.02, "surgery": 0.04}
        self.assertEqual(_select_hospital_topic(scores), "leveling")

    def test_admission_still_needs_012(self):
        # admission は従来どおり 0.12 足切り（0.06では選ばれない）
        scores = {"leveling": 0.02, "admission": 0.06, "surgery": 0.0}
        self.assertEqual(_select_hospital_topic(scores), "leveling")


class DirectWordingTest(unittest.TestCase):
    def test_surgery_fallback_direct(self):
        move = _fallback_move_surgery("目標をやや下回っている")
        self.assertIn("件数増に専念", move["action"])

    def test_admission_fallback_direct(self):
        move = _fallback_move_admission("目標をやや下回っている")
        self.assertIn("患者数増に取り組", move["action"])

    def test_met_state_unchanged(self):
        # 達成時（維持系）の文言は直接化しない（回帰）
        move = _fallback_move_surgery("目標を達成している")
        self.assertNotIn("件数増に専念", move["action"])
        self.assertIn("維持", move["action"])


if __name__ == "__main__":
    unittest.main()
