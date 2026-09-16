"""calendar_preview.build_window_note の P2 ステップ7（極小窓ガードの表示注記）回帰テスト。

対象:
  - build_window_note: 窓の営業日(biz) < 3 のとき、既存の暦注記の文末へ
    「営業日が少ないため、傾向の矢印は保留しています。」を追記する（新規UIは作らない）。

実行: リポジトリルートで
    .venv/bin/python -m pytest -q tests/test_calendar_preview_min_biz_note.py
"""
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.calendar_preview import build_window_note  # noqa: E402

APPEND_TEXT = "営業日が少ないため、傾向の矢印は保留しています。"


class WindowNoteMinBizAppendTest(unittest.TestCase):
    def test_biz_5_matches_standard_returns_none(self):
        # 直近7暦日(2026-01-03〜01-09)は営業日5日（標準どおり）→ 注記そのものが発火しない
        note = build_window_note(pd.Timestamp("2026-01-09"), 7)
        self.assertIsNone(note)

    def test_biz_4_below_standard_but_not_extreme_has_no_append(self):
        # 直近7暦日(2025-12-23〜12-29)は営業日4日（標準5からの乖離はあるが極小ではない）
        note = build_window_note(pd.Timestamp("2025-12-29"), 7)
        self.assertIsNotNone(note)
        self.assertEqual(note["biz_days"], 4)
        self.assertNotIn(APPEND_TEXT, note["text"])

    def test_biz_2_extreme_has_append(self):
        # 直近7暦日(2025-12-31〜2026-01-06)は年末年始で営業日2日 → 極小窓ガード注記を追記
        note = build_window_note(pd.Timestamp("2026-01-06"), 7)
        self.assertIsNotNone(note)
        self.assertEqual(note["biz_days"], 2)
        self.assertIn(APPEND_TEXT, note["text"])
        # 既存の暦注記（値そのもの）は置換せず維持されていること
        self.assertIn("営業日2日", note["text"])

    def test_append_text_is_at_the_end(self):
        note = build_window_note(pd.Timestamp("2026-01-06"), 7)
        self.assertTrue(note["text"].endswith(APPEND_TEXT))


if __name__ == "__main__":
    unittest.main()
