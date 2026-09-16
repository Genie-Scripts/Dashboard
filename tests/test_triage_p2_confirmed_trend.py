"""triage.confirmed_trend_dir（P2 ステップ5: 2週連続確定＋非対称ヒステリシス）の回帰テスト。

対象の6件:
  ① 1週だけの逸脱は点灯しない
  ② 2週連続で点灯
  ③ thr/2 未満で即消灯
  ④ 逆方向は2週確定が要る
  ⑤ lookback 境界は消灯始まり（保守側）
  ⑥ spread=None を透過

状態ファイルは使わない（毎回 lookback_weeks 分だけの決定論的畳み込み）ため、
週次スプレッドの合成辞書を用意して spread_at に渡すだけで密閉に検証できる。

実行: リポジトリルートで
    .venv/bin/python -m pytest -q tests/test_triage_p2_confirmed_trend.py
"""
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.triage import confirmed_trend_dir  # noqa: E402

BASE = pd.Timestamp("2026-06-01")   # 月曜。週次サンプル点は BASE - 7*k
THR = 10.0


def _weeks(lookback_weeks=8, base=BASE):
    """[最古 ... 最新(base)] の順で週次日付を返す。"""
    return [base - pd.Timedelta(weeks=k) for k in range(lookback_weeks, -1, -1)]


def _spread_at(mapping, default=0.0):
    """{Timestamp: spread(%)} の合成辞書から spread_at コールバックを作る。"""
    def f(d):
        return mapping.get(d, default)
    return f


class SingleWeekDeviationDoesNotConfirm(unittest.TestCase):
    """① 1週だけの逸脱（他は横ばい）では点灯しない。"""

    def test_single_up_week_among_flat_weeks_stays_flat(self):
        weeks = _weeks()
        mapping = {w: 0.0 for w in weeks}
        mapping[BASE] = 15.0   # 直近1週だけ +15%（thr=10超）
        result = confirmed_trend_dir(_spread_at(mapping), BASE, THR)
        self.assertEqual(result, "flat")


class TwoConsecutiveWeeksConfirm(unittest.TestCase):
    """② 2週連続で同方向なら点灯する。"""

    def test_two_consecutive_up_weeks_light_up(self):
        weeks = _weeks()
        mapping = {w: 0.0 for w in weeks}
        mapping[BASE] = 15.0
        mapping[BASE - pd.Timedelta(weeks=1)] = 15.0
        result = confirmed_trend_dir(_spread_at(mapping), BASE, THR)
        self.assertEqual(result, "up")

    def test_two_consecutive_down_weeks_light_up(self):
        weeks = _weeks()
        mapping = {w: 0.0 for w in weeks}
        mapping[BASE] = -15.0
        mapping[BASE - pd.Timedelta(weeks=1)] = -15.0
        result = confirmed_trend_dir(_spread_at(mapping), BASE, THR)
        self.assertEqual(result, "down")


class ImmediateOffBelowHalfThreshold(unittest.TestCase):
    """③ 点灯中に |spread| が thr/2 を割ったら即時消灯（保守側）。"""

    def test_drop_below_half_threshold_turns_off_immediately(self):
        weeks = _weeks()
        mapping = {w: 0.0 for w in weeks}
        w7, w8 = weeks[-2], weeks[-3]   # BASE-7週, BASE-14週（点灯させる2週）
        mapping[w8] = 15.0
        mapping[w7] = 15.0
        mapping[BASE] = 2.0   # |2.0| < thr/2(5.0) → 直近週で即消灯
        result = confirmed_trend_dir(_spread_at(mapping), BASE, THR)
        self.assertEqual(result, "flat")

    def test_stays_lit_while_at_or_above_half_threshold(self):
        weeks = _weeks()
        mapping = {w: 0.0 for w in weeks}
        w7, w8 = weeks[-2], weeks[-3]
        mapping[w8] = 15.0
        mapping[w7] = 15.0
        mapping[BASE] = 6.0   # |6.0| >= thr/2(5.0) → 維持
        result = confirmed_trend_dir(_spread_at(mapping), BASE, THR)
        self.assertEqual(result, "up")


class ReversalNeedsTwoWeekConfirm(unittest.TestCase):
    """④ 逆方向へ切り替わるには、消灯を経ずとも改めて2週連続確定が必要。"""

    def test_single_opposite_week_does_not_flip(self):
        weeks = _weeks()
        mapping = {w: 0.0 for w in weeks}
        w7, w8 = weeks[-2], weeks[-3]
        mapping[w8] = 15.0
        mapping[w7] = 15.0        # up に点灯
        mapping[BASE] = -15.0     # 直近1週だけ down（|spread|>=thr/2 なのでヒステリシスで up 維持）
        result = confirmed_trend_dir(_spread_at(mapping), BASE, THR)
        self.assertEqual(result, "up")

    def test_two_consecutive_opposite_weeks_flip(self):
        weeks = _weeks()
        mapping = {w: 0.0 for w in weeks}
        w7, w8 = weeks[-2], weeks[-3]
        mapping[w8] = 15.0
        mapping[w7] = 15.0                          # up に点灯
        mapping[BASE - pd.Timedelta(weeks=1)] = -15.0
        mapping[BASE] = -15.0                        # down が2週連続 → down へ確定
        result = confirmed_trend_dir(_spread_at(mapping), BASE, THR)
        self.assertEqual(result, "down")


class LookbackBoundaryStartsOff(unittest.TestCase):
    """⑤ lookback 境界（最古の週）は必ず消灯（flat）から始まる＝保守側。
    最古週が単独で "up" 相当でも、直後が逆方向なら点灯を経由せず flat のままになる。"""

    def test_oldest_week_alone_does_not_seed_a_lit_state(self):
        weeks = _weeks()
        mapping = {w: 0.0 for w in weeks}
        mapping[weeks[0]] = 15.0    # 最古週（lookback境界）が単独で up 相当
        mapping[weeks[1]] = -15.0   # その直後は down 相当（1つ前と不一致 → 点灯しない）
        # 以降はすべて横ばいのまま base_date まで進める
        result = confirmed_trend_dir(_spread_at(mapping), BASE, THR)
        self.assertEqual(result, "flat")


class NoneSpreadPassesThrough(unittest.TestCase):
    """⑥ 直近時点の spread が None（判定保留）なら None を透過する。"""

    def test_current_point_none_returns_none(self):
        result = confirmed_trend_dir(lambda d: None, BASE, THR)
        self.assertIsNone(result)

    def test_none_only_at_current_point_overrides_prior_lit_state(self):
        weeks = _weeks()
        mapping = {w: 0.0 for w in weeks}
        w7, w8 = weeks[-2], weeks[-3]
        mapping[w8] = 15.0
        mapping[w7] = 15.0   # up に点灯させた状態でも
        result = confirmed_trend_dir(
            lambda d: None if d == BASE else mapping.get(d, 0.0), BASE, THR)
        self.assertIsNone(result)   # 直近点が測れない → 過去の状態を引きずらず None


if __name__ == "__main__":
    unittest.main()
