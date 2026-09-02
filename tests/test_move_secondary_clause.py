"""dept_report._secondary_clause（副トピック本文併記）のユニットテスト。

2026-09-02是正: 旧版は topic=="leveling" のとき実測を見ずに固定文「なお、週末在院の
維持には改善余地があります。」を返していた（週末が主トピックでさえあれば毎回付く＝
事実と無関係。09-01レビューHTMLで週末21回/新入院6回/全麻0回という非対称の一因）。
新シグネチャは主トピック(primary)以外の3トピックすべてを実測(tier)から都度判定し、
未達（close/mild/poor）のものだけを候補にする。週末だけは mild/poor のみ候補にする
（close は併記しない＝発信方針で週末の扱いを一段控えめにする）。同tierは
_SECONDARY_PRIORITY（admission > surgery > leveling）で決定論に選ぶ。
LLM呼び出しはテストしない。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.dept_report import _secondary_clause


class SecondaryClauseNeutralConnectiveTest(unittest.TestCase):
    """副トピック併記は極性中立の「なお、〜は」。主文(LLM自由文)がポジティブに終わっても
    「あわせて、〜も」のようなねじれた順接にならないこと（回帰）。"""

    def test_admission_clause(self):
        # primary!=admission・新入院 poor で候補 → 「なお、新入院は〜」
        c = _secondary_clause("surgery", "目標を下回っている", None, None,
                              na_gap=8, na_tgt=10)
        self.assertEqual(c, "なお、新入院は目標を下回っている状況です。")

    def test_leveling_clause(self):
        # primary!=leveling・週末 poor で候補 → 「なお、週末在院の維持率は〜」
        # （旧固定文「なお、週末在院の維持には改善余地があります。」は廃止）
        c = _secondary_clause("admission", None, None, 0.70)
        self.assertTrue(c.startswith("なお、週末在院の維持率は"))
        self.assertIn("状況です。", c)

    def test_no_additive_connective(self):
        cases = [
            _secondary_clause("surgery", "目標を下回っている", None, None, na_gap=8, na_tgt=10),
            _secondary_clause("admission", None, "目標を下回っている", None, sv_gap=8, surg_tgt=10),
            _secondary_clause("admission", None, None, 0.70),
        ]
        for c in cases:
            self.assertIsNotNone(c)
            self.assertNotIn("あわせて", c)
            # 「〜も…」の同調前提を含意しない（主語直後は「は/には」）
            self.assertNotRegex(c, r"(新入院|全身麻酔手術)も")


class SecondaryClauseTierSelectionTest(unittest.TestCase):
    """実測(tier)からの候補選定・同tier優先順位・週末close除外の仕様検証。"""

    def test1_surgery_primary_close_weekend_met_admission_no_candidate(self):
        # ①surgery 主・ret=0.92(→ close, 週末は mild/poor のみ候補なので除外)・新入院達成 → None
        c = _secondary_clause("surgery", "目標を達成している", None, 0.92,
                              na_gap=10, na_tgt=10)
        self.assertIsNone(c)

    def test2_surgery_primary_weekend_mild_becomes_candidate(self):
        # ②surgery 主・ret=0.86（目標比0.925=mild）・新入院達成 → 週末文
        c = _secondary_clause("surgery", "目標を達成している", None, 0.86,
                              na_gap=10, na_tgt=10)
        self.assertEqual(c, "なお、週末在院の維持率は目標をやや下回っている状況です。")

    def test3_surgery_primary_weekend_close_excluded(self):
        # ③surgery 主・ret=0.90（目標比0.968=close）・新入院達成 → None（週末は close で併記しない）
        c = _secondary_clause("surgery", "目標を達成している", None, 0.90,
                              na_gap=10, na_tgt=10)
        self.assertIsNone(c)

    def test4_leveling_primary_admission_close_becomes_candidate(self):
        # ④leveling 主・新入院 ratio 0.97（close） → 新入院文（leveling自身は候補対象外なので
        # close でも新入院側は候補になる＝週末限定の抑制は他トピックには適用しない）
        c = _secondary_clause("leveling", "目標をわずかに下回っている", None, None,
                              na_gap=9.7, na_tgt=10)
        self.assertEqual(c, "なお、新入院は目標をわずかに下回っている状況です。")

    def test5_tie_prefers_admission_over_leveling(self):
        # ⑤surgery 主・新入院 poor(比0.80)・ret=0.70(比0.75=poor) → 新入院文（同順位は admission 優先）
        c = _secondary_clause("surgery", "目標を明確に下回っている", None, 0.70,
                              na_gap=8, na_tgt=10)
        self.assertEqual(c, "なお、新入院は目標を明確に下回っている状況です。")

    def test6_leveling_rank_wins_when_higher_tier(self):
        # ⑥surgery 主・新入院 close(0.97)・ret=0.70(poor) → 週末文（rank最大が優先）
        c = _secondary_clause("surgery", "目標をわずかに下回っている", None, 0.70,
                              na_gap=9.7, na_tgt=10)
        self.assertEqual(c, "なお、週末在院の維持率は目標を明確に下回っている状況です。")

    def test7_admission_primary_surgery_mild_uses_label(self):
        # ⑦admission 主・外科系で全麻 mild・ret good(met) → 全麻文
        # （label引数「全手術」を渡すと「なお、全手術は」になる＝眼科向けラベル差し替え）
        c = _secondary_clause("admission", None, "目標をやや下回っている", 1.0,
                              sv_gap=8.8, surg_tgt=10, surgery_label="全手術")
        self.assertEqual(c, "なお、全手術は目標をやや下回っている状況です。")

    def test8_all_missing_data_is_none(self):
        # ⑧ret None かつ新入院 None（かつ手術データも無し） → None
        c = _secondary_clause("surgery", None, None, None)
        self.assertIsNone(c)


if __name__ == "__main__":
    unittest.main()
