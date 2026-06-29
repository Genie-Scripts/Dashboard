"""週次ストーリーの暦の事実注入テスト（連休・祝日の幻覚抑止）。

対象:
  - _holiday_fact          : 今週/前回保存時の各7日窓の祝日有無を確定事実化
  - _build_user_prompt     : 暦の事実がプロンプトに注入される

実行: リポジトリルートで
    python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib import weekly_story


class HolidayFactTest(unittest.TestCase):
    def test_no_holiday_week_suppresses(self):
        # 2026-06 は祝日なし（今週6/22-28・前回6/15-21）→「言及しない」を明示
        note = weekly_story._holiday_fact("2026-06-28", "2026-06-21")
        self.assertIn("祝日はありません", note)
        self.assertIn("一切言及しないこと", note)

    def test_holiday_week_lists_names(self):
        # GW: 今週 4/30-5/6 に祝日名が確定事実として入る
        note = weekly_story._holiday_fact("2026-05-06", "2026-04-29")
        self.assertIn("こどもの日", note)
        self.assertIn("今週", note)

    def test_partial_holiday_only_one_window(self):
        # 今週=祝日なし(6/28)・前回=元日週(2026-01-04) → 一覧形式で両窓を提示
        note = weekly_story._holiday_fact("2026-06-28", "2026-01-04")
        self.assertIn("元日", note)
        self.assertIn("今週", note)
        self.assertIn("前回保存時", note)

    def test_bad_date_degrades_to_empty(self):
        self.assertEqual(weekly_story._holiday_fact("", ""), "")

    def test_prompt_injects_holiday_fact(self):
        prompt = weekly_story._build_user_prompt(
            ["新入院 7日合計 370→391（+21）"], "2026-06-28", "2026-06-21")
        self.assertIn("【暦の事実】", prompt)
        self.assertIn("祝日はありません", prompt)


if __name__ == "__main__":
    unittest.main()
