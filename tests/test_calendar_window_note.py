"""calendar_preview.py の第4層（A3: いまの窓の暦注記）の回帰テスト。

対象:
  - build_window_note  : window=7/28 の窓内営業日数が標準と異なるときだけ発火
  - build_window_notes : 7日・28日をまとめて返す（Noneは省く）

祝日判定はjpholiday実カレンダー依存のため、テスト側で日付選定して調整している
（config/jpholiday側は不可侵。既存 tests/test_calendar_preview.py の慣習を踏襲）。

実行: リポジトリルートで
    OMLX_BASE_URL=http://127.0.0.1:9 .venv/bin/python -m pytest -q tests/test_calendar_window_note.py
"""
import sys
import unittest
from pathlib import Path

import pandas as pd

# リポジトリルートを import パスに追加（generate_html.py と同方式）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.calendar_preview import build_window_note, build_window_notes  # noqa: E402


class OrdinaryWeekBothWindowsNoneTest(unittest.TestCase):
    """① 通常週（2026-07-03）: 7日窓=営業日5・28日窓=営業日20 → どちらも発火しない"""

    def test_both_windows_none(self):
        base = pd.Timestamp("2026-07-03")
        self.assertIsNone(build_window_note(base, 7))
        self.assertIsNone(build_window_note(base, 28))
        self.assertEqual(build_window_notes(base), {})


class SevenDayWindowSingleHolidayTest(unittest.TestCase):
    """② 7日窓のみ発火（2026-02-13金曜・7日窓2/7-2/13に建国記念の日2/11が1日）

    28日窓(1/17-2/13)にも同じ祝日1件が含まれるため実際は両方発火する
    （③で28日窓側の内容も検証）。
    """

    def test_seven_day_window_fires_with_reason(self):
        base = pd.Timestamp("2026-02-13")
        note = build_window_note(base, 7)
        self.assertIsNotNone(note)
        self.assertEqual(note["window"], 7)
        self.assertEqual(note["biz_days"], 4)
        self.assertEqual(note["std_days"], 5)
        self.assertIn("この7日間は営業日4日", note["text"])
        self.assertIn("2/11 建国記念の日", note["text"])
        self.assertIn("新入院と手術はその分少なく出ます。", note["text"])

    def test_both_windows_present_in_notes(self):
        base = pd.Timestamp("2026-02-13")
        notes = build_window_notes(base)
        self.assertEqual(set(notes.keys()), {7, 28})
        self.assertEqual(notes[28]["biz_days"], 19)
        self.assertIn("この28日間は営業日19日", notes[28]["text"])
        self.assertIn("2/11 建国記念の日", notes[28]["text"])


class TwentyEightDayWindowOnlyFiresTest(unittest.TestCase):
    """③ 28日窓のみ発火（2026-01-30金曜・7日窓は営業日5=標準／28日窓は成人の日1/12で19日）"""

    def test_seven_day_window_none(self):
        base = pd.Timestamp("2026-01-30")
        self.assertIsNone(build_window_note(base, 7))

    def test_twenty_eight_day_window_fires_with_reason(self):
        base = pd.Timestamp("2026-01-30")
        note = build_window_note(base, 28)
        self.assertIsNotNone(note)
        self.assertEqual(note["window"], 28)
        self.assertEqual(note["biz_days"], 19)
        self.assertEqual(note["std_days"], 20)
        self.assertIn("この28日間は営業日19日", note["text"])
        self.assertIn("1/12 成人の日", note["text"])

    def test_build_window_notes_omits_seven_day_key(self):
        notes = build_window_notes(pd.Timestamp("2026-01-30"))
        self.assertEqual(set(notes.keys()), {28})


class MultiReasonWindowTest(unittest.TestCase):
    """④ シルバーウィーク（2026-09-23秋分の日）: 7日窓(9/17-9/23)に祝日3日連続で営業日2日"""

    def test_seven_day_window_multiple_reasons(self):
        base = pd.Timestamp("2026-09-23")
        note = build_window_note(base, 7)
        self.assertIsNotNone(note)
        self.assertEqual(note["biz_days"], 2)
        self.assertIn("9/23 秋分の日", note["text"])
        self.assertIn("9/21", note["text"])
        self.assertIn("9/22", note["text"])


class JsonSerializableTest(unittest.TestCase):
    """⑤ build_window_notes の返り値は json.dumps 可能"""

    def test_json_dumps_succeeds(self):
        import json
        notes = build_window_notes(pd.Timestamp("2026-02-13"))
        json.dumps(notes, ensure_ascii=False)  # 例外が出ないことのみ確認


if __name__ == "__main__":
    unittest.main()
