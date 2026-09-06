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

import pandas as pd

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


# ════════════════════════════════════════
# ★A5: build_kpi_snapshot への generated_at 追加（訴求力強化 Phase1 バッチ1a）
# ════════════════════════════════════════

_EMPTY_ADM = pd.DataFrame({"日付": pd.to_datetime([])})
_EMPTY_SURG = pd.DataFrame({"手術実施日": pd.to_datetime([])})


class BuildKpiSnapshotGeneratedAtTest(unittest.TestCase):
    def test_generated_at_is_included_when_passed(self):
        ts = pd.Timestamp("2026-09-04T07:39:00")
        snap = weekly_story.build_kpi_snapshot(
            _EMPTY_ADM, _EMPTY_SURG, {}, None, pd.Timestamp("2026-09-03"),
            generated_at=ts)
        self.assertEqual(snap["generated_at"], ts.isoformat())

    def test_generated_at_defaults_to_none_for_backward_compat(self):
        # 呼び出し元(scripts/build_weekly_digest.py 等)が未対応でも壊れないこと。
        snap = weekly_story.build_kpi_snapshot(
            _EMPTY_ADM, _EMPTY_SURG, {}, None, pd.Timestamp("2026-09-03"))
        self.assertIsNone(snap["generated_at"])

    def test_load_history_tolerates_snapshots_without_generated_at(self):
        # 旧フォーマット（generated_at列が無い）の履歴でも例外を出さないこと。
        import json
        import tempfile
        old_snap = {"base_date": "2026-08-27", "inpatient": {}, "admission": {},
                   "operation": {}, "profit_top": []}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "last_kpi.json"
            path.write_text(json.dumps({"snapshots": [old_snap]}), encoding="utf-8")
            history = weekly_story.load_history(path)
        self.assertEqual(len(history), 1)
        self.assertIsNone(history[0].get("generated_at"))


class BuildWeeklyStoryForwardsGeneratedAtTest(unittest.TestCase):
    def test_generated_at_forwarded_to_snapshot(self):
        import tempfile
        ts = pd.Timestamp("2026-09-08T07:40:00")
        with tempfile.TemporaryDirectory() as d:
            snapshot_path = Path(d) / "last_kpi.json"
            result = weekly_story.build_weekly_story(
                _EMPTY_ADM, _EMPTY_SURG, {}, None, pd.Timestamp("2026-09-08"),
                snapshot_path, quiet=True, generated_at=ts)
            self.assertEqual(result["base_date"], "2026-09-08")
            history = weekly_story.load_history(snapshot_path)
            self.assertEqual(history[0]["generated_at"], ts.isoformat())


if __name__ == "__main__":
    unittest.main()
