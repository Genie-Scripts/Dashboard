"""
triage.py P1 暦是正の回帰テスト（標準ライブラリ unittest・追加依存なし）。

対象:
  - adjusted_weekly_target : 週目標を「直近7暦日窓の営業日数/5」で割り引く（科別達成率の期待値割引）
  - _surgery_trend         : 全麻トレンドを生件数比→件/営業日レート比へ変更（P1暦是正）

実行: リポジトリルートで
    python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

import pandas as pd

# リポジトリルートを import パスに追加（generate_html.py と同方式）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.config import operational_days_between  # noqa: E402
from app.lib.metrics import achievement_rate  # noqa: E402
from app.lib.triage import adjusted_weekly_target, _surgery_trend  # noqa: E402


class TestAdjustedWeeklyTarget(unittest.TestCase):
    def test_none_target_stays_none(self):
        self.assertIsNone(adjusted_weekly_target(None, pd.Timestamp("2026-01-18")))

    def test_ordinary_week_is_unchanged(self):
        # 直近7暦日(2026-02-28〜03-06)は営業日5日・祝日なし → 割引係数1.0
        base = pd.Timestamp("2026-03-06")
        self.assertEqual(operational_days_between(base - pd.Timedelta(days=6), base), 5)
        self.assertEqual(adjusted_weekly_target(25, base), 25.0)

    def test_happy_monday_week_discounts_to_four_fifths(self):
        # 直近7暦日(2026-01-12月祝〜01-18)は営業日4日 → 週目標×4/5
        base = pd.Timestamp("2026-01-18")
        self.assertEqual(operational_days_between(base - pd.Timedelta(days=6), base), 4)
        self.assertAlmostEqual(adjusted_weekly_target(25, base), 25 * 4 / 5)

    def test_discount_changes_achievement_rate(self):
        # 実績20件・週目標25件: 素の週目標なら未達(80%)だが、祝日で窓内営業日が
        # 4日に減った週は割引後目標20.0件に対し達成(100%)へ変わる
        # （＝暦を考慮しない判定は不当な未達を出していたことになる）。
        base = pd.Timestamp("2026-01-18")
        actual = 20
        raw_rate = achievement_rate(actual, 25)
        adj_rate = achievement_rate(actual, adjusted_weekly_target(25, base))
        self.assertAlmostEqual(raw_rate, 80.0)
        self.assertAlmostEqual(adj_rate, 100.0)


class TestSurgeryTrendRateBased(unittest.TestCase):
    def test_below_min_gate_returns_none(self):
        # 生件数ゲート(SURGERY_TREND_MIN_28D=8)は現状維持
        spread, direction = _surgery_trend(7, 20, pd.Timestamp("2026-03-06"))
        self.assertIsNone(spread)
        self.assertIsNone(direction)

    def test_zero_prior_rate_returns_none(self):
        spread, direction = _surgery_trend(20, 0, pd.Timestamp("2026-03-06"))
        self.assertIsNone(spread)
        self.assertIsNone(direction)

    def test_equal_raw_counts_across_holiday_skewed_windows_shows_rate_trend(self):
        # 直近28暦日窓(2025-12-19〜2026-01-15)は年末年始をまたぎ営業日14日。
        # 前28暦日窓(2025-11-21〜2025-12-18)は平常期で営業日19日。
        # 生件数は両窓とも28件で同一（旧ロジックなら「横ばい」＝スプレッド0%）だが、
        # 件/営業日レートで比較すると 28/14=2.0 vs 28/19≒1.47 で実質+36%の改善として
        # 検出される（片窓に祝日を含む場合の暦補正効果）。
        base = pd.Timestamp("2026-01-15")
        biz_now = operational_days_between(base - pd.Timedelta(days=27), base)
        biz_prev = operational_days_between(base - pd.Timedelta(days=55), base - pd.Timedelta(days=28))
        self.assertEqual(biz_now, 14)
        self.assertEqual(biz_prev, 19)

        spread, direction = _surgery_trend(28, 28, base)
        expected = (28 / biz_now - 28 / biz_prev) / (28 / biz_prev) * 100.0
        self.assertAlmostEqual(spread, expected, places=6)
        self.assertGreater(spread, 15.0)
        self.assertEqual(direction, "up")

    def test_ordinary_equal_biz_days_window_is_flat(self):
        # 両窓の営業日数が同じ(祝日構成が対称)なら、生件数が同じ場合は横ばい(flat)のまま
        # （旧ロジックと結果が一致すること＝レート化による過剰検知が無いことの確認）。
        base = pd.Timestamp("2026-07-01")
        prior_base = base - pd.Timedelta(days=28)
        biz_now = operational_days_between(base - pd.Timedelta(days=27), base)
        biz_prev = operational_days_between(prior_base - pd.Timedelta(days=27), prior_base)
        self.assertEqual(biz_now, biz_prev)
        self.assertEqual(biz_now, 20)
        spread, direction = _surgery_trend(20, 20, base)
        self.assertAlmostEqual(spread, 0.0)
        self.assertEqual(direction, "flat")


if __name__ == "__main__":
    unittest.main()
