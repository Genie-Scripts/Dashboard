"""build_hospital_report.py（B12: PDF・掲示の完全週固定）のユニットテスト。

対象: resolve_base_date（--base-date 未指定時の直近日曜丸め）・
      build_period_heading（見出し「対象週…｜比較: その前の週…」）。
実データ・ファイルI/O・Chrome起動には依存しない（純関数のみ）。

実行: リポジトリルートで
    python -m pytest tests/ -q
"""
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.build_hospital_report import resolve_base_date, build_period_heading


class ResolveBaseDateTest(unittest.TestCase):
    """B12: --base-date 未指定時は直近日曜（完全週の終端）へ丸める。明示指定時はそのまま。"""

    def test_unspecified_rounds_to_complete_week_end(self):
        resolved = resolve_base_date(None, pd.Timestamp("2026-09-08"))  # 火曜
        self.assertEqual(resolved, pd.Timestamp("2026-09-06"))  # 直近日曜

    def test_explicit_base_date_is_not_rounded(self):
        resolved = resolve_base_date("2026-09-08", pd.Timestamp("2026-09-08"))
        self.assertEqual(resolved, pd.Timestamp("2026-09-08"))


class BuildPeriodHeadingTest(unittest.TestCase):
    """B12: 見出し「対象週 8/24(月)〜8/30(日)｜比較: その前の週 8/17〜8/23」。「直近7日」は出ない。"""

    def test_heading_format(self):
        heading = build_period_heading(pd.Timestamp("2026-08-24"), pd.Timestamp("2026-08-30"))
        self.assertEqual(heading, "対象週 8/24(月)〜8/30(日)｜比較: その前の週 8/17〜8/23")

    def test_no_rolling7_wording(self):
        heading = build_period_heading(pd.Timestamp("2026-08-24"), pd.Timestamp("2026-08-30"))
        self.assertNotIn("直近7日", heading)


if __name__ == "__main__":
    unittest.main()
