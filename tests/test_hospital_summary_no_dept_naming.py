"""病院全体サマリの「この期間の一手」で個別病棟/診療科を名指ししない回帰テスト（2026-08）。

背景: 旧実装は「牽引役（leader）」を選定し、_fallback_move_hospital が
「こうした中、{leader}は{leader_label}状況で、手本になっています。」を本文へ追記していた。
病院全体サマリは複数部門の総称であり、個別部門を名指しする一手は誤読を招くため、
build_hospital_overview_context 側の呼び出しを leader=None 固定へ変更した
（app/lib/dept_report.py 内 `_fallback_move_hospital(h_topic, h_primary_state, ret)`）。

本テストは _fallback_move_hospital を本番と同じ「leader引数なし」の形で直接呼び、
「手本になっています」文言と「病棟」名が body に混入しないことを確認する
（narrate_hospital_summary 経由のAI文生成はLLM依存のため対象外）。

実行: リポジトリルートで
    python -m pytest tests/ -q
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib import dept_report as dr  # noqa: E402


class TestFallbackMoveHospitalNoLeaderNaming(unittest.TestCase):
    """_fallback_move_hospital(topic, state, ret) の本番呼び出し形（leader渡しなし）。"""

    def test_leveling_achieved_no_leader_naming(self):
        # ret=0.95 (>=TARGET_WEEKEND_RETENTION=93%) → 「達成」分岐
        move = dr._fallback_move_hospital("leveling", "目標を上回っている", 0.95)
        self.assertNotIn("手本になっています", move["body"])
        self.assertNotIn("病棟", move["body"])

    def test_leveling_unmet_no_leader_naming(self):
        # ret=0.5 (<TARGET_WEEKEND_RETENTION=93%) → 「未達」分岐
        move = dr._fallback_move_hospital("leveling", "目標を下回っている", 0.5)
        self.assertNotIn("手本になっています", move["body"])
        self.assertNotIn("病棟", move["body"])


if __name__ == "__main__":
    unittest.main()
