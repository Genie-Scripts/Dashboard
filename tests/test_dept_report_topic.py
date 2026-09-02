"""部門レポート「この期間の一手」のトピック選定と直接文言のユニットテスト。

外科系診療科の surgery 主トピック常時固定（2026-07-22・達成状況によらず手術コメントを
必ず先頭へ）、病院全体の足切り（全麻95%=0.05／leveling・admission=3%＝2026-09-02是正）、
未達 action の直接文言化（件数増に専念／患者数増に取り組む）を純関数で検証する。
副トピック併記（_secondary_clause）のテストは tests/test_move_secondary_clause.py へ分離。
LLM呼び出しはテストしない。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.dept_report import (_select_action_topic, _select_hospital_topic,
                                 _fallback_move_surgery, _fallback_move_admission,
                                 _leveling_gap_score, _hospital_other_topic_facts,
                                 SURGERY_TOPIC_MIN_SCORE,
                                 SURGERY_TOPIC_MIN_SCORE_HOSPITAL,
                                 ACTION_TOPIC_MIN_SCORE_HOSPITAL)


class SelectActionTopicTest(unittest.TestCase):
    def test_surgical_97pct_becomes_primary(self):
        # 外科系: 全麻97% → 主トピック=surgery（常時固定）
        p, s, sc = _select_action_topic("surgical", 0.05, 1.0, 10, 10, 9.7, 10)
        self.assertEqual(p, "surgery")
        self.assertAlmostEqual(sc["surgery"], 0.03, places=4)

    def test_surgical_99pct_still_surgery(self):
        # 外科系: 全麻99%（旧仕様では足切りで leveling 既定）でも surgery 固定
        p, s, _ = _select_action_topic("surgical", 0.05, 1.0, 10, 10, 9.9, 10)
        self.assertEqual(p, "surgery")

    def test_surgical_met_target_still_surgery(self):
        # 外科系: 全麻105%達成でも surgery 固定（達成時は維持系の文言になる）
        p, s, _ = _select_action_topic("surgical", 0.05, 1.0, 10, 10, 10.5, 10)
        self.assertEqual(p, "surgery")
        self.assertIsNone(s)

    def test_surgical_leveling_demoted_to_secondary(self):
        # 外科系: leveling 0.5(≥0.12) は副トピックへ降格し「なお…」併記で言及される
        p, s, _ = _select_action_topic("surgical", 0.5, 1.0, 10, 10, 9.6, 10)
        self.assertEqual((p, s), ("surgery", "leveling"))

    def test_surgical_no_target_falls_back_to_selection(self):
        # 外科系でも手術目標未設定なら forced 分岐に入らず従来選定（leveling 既定）
        p, s, sc = _select_action_topic("surgical", 0.05, 1.0, 10, 10, None, None)
        self.assertEqual(p, "leveling")
        self.assertEqual(sc["surgery"], 0.0)

    def test_internal_never_has_surgery(self):
        # 内科系: 全麻データがあっても候補にならない（回帰）
        p, s, sc = _select_action_topic("internal", 0.05, 1.0, 10, 10, 5, 10)
        self.assertEqual(p, "leveling")
        self.assertNotIn("surgery", sc)

    def test_internal_call_matches_default(self):
        # 既定 surgery_min を渡さない呼び出しでも内科系挙動は不変
        self.assertEqual(SURGERY_TOPIC_MIN_SCORE, 0.02)


class SelectHospitalTopicTest(unittest.TestCase):
    def test_reproduces_20260705(self):
        # 病院全体: 2026-07-05 実測値の再現（新式で再計算）。
        # leveling=_leveling_gap_score(91.2)≈0.019(<0.03) admission 0.055(≥0.03)
        # surgery 0.081(≥0.05) → surgery のまま
        scores = {"leveling": _leveling_gap_score(91.2), "admission": 0.055, "surgery": 0.081}
        self.assertEqual(_select_hospital_topic(scores), "surgery")

    def test_surgery_94pct_eligible(self):
        # 全麻達成率94%(0.06≥0.05) → surgery が主（新ルールでも期待値は不変）
        self.assertEqual(SURGERY_TOPIC_MIN_SCORE_HOSPITAL, 0.05)
        scores = {"leveling": 0.03, "admission": 0.03, "surgery": 0.06}
        self.assertEqual(_select_hospital_topic(scores), "surgery")

    def test_surgery_96pct_below_threshold(self):
        # 全麻達成率96%(0.04<0.05)・leveling 0.03(≥0.03) → leveling が eligible 内最大
        # （新ルールでも期待値は不変）
        scores = {"leveling": 0.03, "admission": 0.02, "surgery": 0.04}
        self.assertEqual(_select_hospital_topic(scores), "leveling")

    def test_admission_hospital_threshold_003(self):
        # 2026-09-02是正: admission の足切りは病院全体では 0.03（旧 0.12 から緩和）。
        # leveling 0.02(<0.03)・admission 0.06(≥0.03)・surgery 0.0 → admission が主
        scores = {"leveling": 0.02, "admission": 0.06, "surgery": 0.0}
        self.assertEqual(ACTION_TOPIC_MIN_SCORE_HOSPITAL, 0.03)
        self.assertEqual(_select_hospital_topic(scores), "admission")

    def test_no_eligible_falls_back_to_max(self):
        # eligible が全て足切り未満でも 0 でなければ leveling 既定にせず全体最大を採る
        # （旧仕様は3トピックとも好調なとき admission/surgery が選ばれる経路が無かった）
        scores = {"leveling": 0.012, "admission": 0.02, "surgery": 0.01}
        self.assertEqual(_select_hospital_topic(scores), "admission")

    def test_all_zero_defaults_to_leveling(self):
        scores = {"leveling": 0.0, "admission": 0.0, "surgery": 0.0}
        self.assertEqual(_select_hospital_topic(scores), "leveling")

    def test_tie_prefers_admission_over_surgery(self):
        # 同点は _SECONDARY_PRIORITY（admission > surgery > leveling）で決定論に選ぶ
        scores = {"leveling": 0.0, "admission": 0.06, "surgery": 0.06}
        self.assertEqual(_select_hospital_topic(scores), "admission")


class LevelingGapScoreTest(unittest.TestCase):
    def test_matches_target_gap_ratio(self):
        # 91.9% → 1 - 91.9/93 ≈ 0.0118（新入院/全麻と同じ「目標比の絶対不足率」の物差し）
        self.assertAlmostEqual(_leveling_gap_score(91.9), 0.0118, places=3)

    def test_at_or_above_target_is_zero(self):
        self.assertEqual(_leveling_gap_score(95.0), 0.0)

    def test_none_is_zero(self):
        self.assertEqual(_leveling_gap_score(None), 0.0)


class HospitalOtherTopicFactsTest(unittest.TestCase):
    def test_only_below_target_tiers_included(self):
        # met は候補外・admission(=h_topic)は自身なので対象外・surgery(mild)だけ返る
        states = {"leveling": "目標を達成している", "admission": "目標を明確に下回っている",
                  "surgery": "目標をやや下回っている"}
        tiers = {"leveling": "met", "surgery": "mild", "admission": "poor"}
        facts = _hospital_other_topic_facts("admission", states, tiers)
        self.assertEqual(facts, ["全身麻酔手術: 目標をやや下回っている"])


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
