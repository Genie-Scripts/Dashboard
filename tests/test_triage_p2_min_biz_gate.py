"""triage.py P2 ステップ4（営業日ベースのスプレッド・極小窓ガード・最小母数ゲート）の回帰テスト。

対象:
  - _ma_spread(..., biz_only=True/False) : 両窓を営業日のみに絞る／暦日のまま（下位互換）
  - _biz_window_avg                      : 窓の営業日数が閾値未満なら None（極小窓ガード）
  - _census_trend_from_series            : 28日窓の営業日在院平均<15 なら trend_dir=None

密閉のため実際の祝日データ（jpholiday）には依存せず、`config.is_operational_day`
（triage.py / config.py 双方の参照）を合成カレンダーへ差し替えて検証する。

実行: リポジトリルートで
    .venv/bin/python -m pytest -q tests/test_triage_p2_min_biz_gate.py
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib import triage as tg  # noqa: E402
from app.lib import config as cfg  # noqa: E402


def _series(base_date: pd.Timestamp, days_back: int, value_of) -> pd.DataFrame:
    """base_date から days_back 日前までの日次系列（値は value_of(date) で決める）。"""
    dates = pd.date_range(base_date - pd.Timedelta(days=days_back), base_date, freq="D")
    return pd.DataFrame({"日付": dates, "値": [value_of(d) for d in dates]})


def _patched_calendar(holidays: set):
    """holidays に含まれる日だけ非営業日とする合成カレンダーで is_operational_day を
    差し替える（triage.py 側の参照と config.operational_days_between 内部の参照の
    両方を一致させる必要があるため2箇所パッチする）。"""
    def fake(d):
        return pd.Timestamp(d) not in holidays
    # side_effect= だと MagicMock が生成され、Series.apply() が MagicMock の自動属性
    # （.keys 等）を見て dict-like と誤判定する。third-positional new= で素の関数に
    # 直接差し替える（MagicMockを介さない）。
    return (
        mock.patch.object(tg, "is_operational_day", fake),
        mock.patch.object(cfg, "is_operational_day", fake),
    )


class MaSpreadBizOnlyTest(unittest.TestCase):
    BASE = pd.Timestamp("2026-03-06")

    def test_calendar_shows_down_but_biz_only_is_flat(self):
        # 直近7日窓のうち2日を「祝日」とし、その2日だけ値が低い合成系列
        # （在院が祝日に急落するデータ異常を想定）。
        holidays = {self.BASE - pd.Timedelta(days=1), self.BASE - pd.Timedelta(days=2)}

        def value_of(d):
            return 40.0 if d in holidays else 100.0

        series = _series(self.BASE, 34, value_of)
        p1, p2 = _patched_calendar(holidays)
        with p1, p2:
            calendar_spread = tg._ma_spread(series, self.BASE, 7, 28, biz_only=False)
            biz_spread = tg._ma_spread(series, self.BASE, 7, 28, biz_only=True)

        self.assertLess(calendar_spread, -3.0)
        self.assertEqual(tg._trend_dir(calendar_spread, 3.0), "down")
        self.assertAlmostEqual(biz_spread, 0.0)
        self.assertEqual(tg._trend_dir(biz_spread, 3.0), "flat")

    def test_biz_only_is_default(self):
        holidays = {self.BASE - pd.Timedelta(days=1), self.BASE - pd.Timedelta(days=2)}

        def value_of(d):
            return 40.0 if d in holidays else 100.0

        series = _series(self.BASE, 34, value_of)
        p1, p2 = _patched_calendar(holidays)
        with p1, p2:
            default_spread = tg._ma_spread(series, self.BASE, 7, 28)
            explicit_spread = tg._ma_spread(series, self.BASE, 7, 28, biz_only=True)
        self.assertEqual(default_spread, explicit_spread)


class MinBizWindowGateTest(unittest.TestCase):
    """極小窓ガード: 7日窓の営業日<3、28日窓の営業日<10 は None（境界2/3・9/10で切替）。"""
    BASE = pd.Timestamp("2026-03-06")

    def _holidays_for(self, window: int, target_biz: int) -> set:
        """window日窓のうち最も古い側から (window - target_biz) 日を祝日にし、
        窓内の営業日数をちょうど target_biz にする。"""
        days = [self.BASE - pd.Timedelta(days=i) for i in range(window - 1, -1, -1)]
        n_holiday = window - target_biz
        return set(days[:n_holiday])

    def _flat_series(self):
        return _series(self.BASE, 34, lambda d: 100.0)

    def test_7day_window_biz_2_is_gated_to_none(self):
        holidays = self._holidays_for(7, 2)
        p1, p2 = _patched_calendar(holidays)
        with p1, p2:
            self.assertIsNone(tg._biz_window_avg(self._flat_series(), self.BASE, 7))

    def test_7day_window_biz_3_is_not_gated(self):
        holidays = self._holidays_for(7, 3)
        p1, p2 = _patched_calendar(holidays)
        with p1, p2:
            self.assertIsNotNone(tg._biz_window_avg(self._flat_series(), self.BASE, 7))

    def test_28day_window_biz_9_is_gated_to_none(self):
        holidays = self._holidays_for(28, 9)
        p1, p2 = _patched_calendar(holidays)
        with p1, p2:
            self.assertIsNone(tg._biz_window_avg(self._flat_series(), self.BASE, 28))

    def test_28day_window_biz_10_is_not_gated(self):
        holidays = self._holidays_for(28, 10)
        p1, p2 = _patched_calendar(holidays)
        with p1, p2:
            self.assertIsNotNone(tg._biz_window_avg(self._flat_series(), self.BASE, 28))

    def test_ma_spread_returns_none_when_short_window_gated(self):
        holidays = self._holidays_for(7, 2)   # 28日窓には影響しない（直近7日だけ祝日）
        p1, p2 = _patched_calendar(holidays)
        with p1, p2:
            self.assertIsNone(tg._ma_spread(self._flat_series(), self.BASE, 7, 28, biz_only=True))


class CensusMinAvgGateTest(unittest.TestCase):
    """最小母数ゲート: 28日窓の営業日在院平均が CENSUS_MIN_AVG_28D(15) 未満なら
    trend_dir=None（primary_trend の値そのものは維持する）。"""
    BASE = pd.Timestamp("2026-03-06")   # 通常週（祝日なし・biz7=5・biz28=20）

    def test_small_avg_dept_has_trend_dir_none_but_spread_kept(self):
        series = _series(self.BASE, 34, lambda d: 10.0)   # 平均10人 < 15人 の小規模科
        with mock.patch.object(tg, "arrow_threshold", return_value=3.0):
            spread, trend_dir = tg._census_trend_from_series(
                series, self.BASE, "dept_census", "テスト科")
        self.assertIsNotNone(spread)
        self.assertIsNone(trend_dir)

    def test_sufficient_avg_dept_has_trend_dir(self):
        series = _series(self.BASE, 34, lambda d: 100.0)   # 平均100人 >= 15人
        with mock.patch.object(tg, "arrow_threshold", return_value=3.0):
            spread, trend_dir = tg._census_trend_from_series(
                series, self.BASE, "dept_census", "テスト科")
        self.assertIsNotNone(trend_dir)

    def test_boundary_avg_just_below_threshold_is_gated(self):
        series = _series(self.BASE, 34, lambda d: 14.9)
        with mock.patch.object(tg, "arrow_threshold", return_value=3.0):
            _, trend_dir = tg._census_trend_from_series(
                series, self.BASE, "dept_census", "テスト科")
        self.assertIsNone(trend_dir)

    def test_boundary_avg_at_threshold_is_not_gated(self):
        series = _series(self.BASE, 34, lambda d: 15.0)
        with mock.patch.object(tg, "arrow_threshold", return_value=3.0):
            _, trend_dir = tg._census_trend_from_series(
                series, self.BASE, "dept_census", "テスト科")
        self.assertIsNotNone(trend_dir)


if __name__ == "__main__":
    unittest.main()
