"""stats_band.py — A6: 通常変動帯 ±1.5σ（表示専用・判定不変）。

`暦補正と学習ループ改修プラン.md` §2 P2 と同一定義のσ許容帯を、P2 が正式に
triage.py へ実装されるより先に表示専用として提供する。triage.py の判定ロジック
（`_ma_spread` / `_surgery_trend`）は意図的に import・再利用せず、同一の式を
こちらに複製する（判定コードに一切触れず「表示のみ・判定不変」を型で保証する
ため）。P2 が正式実装されたら、この複製は解消してよい
（P2 着手時に triage 側を stats_band へ寄せる）。

detail.html / dept.html のチャートに帯トレースとして描画する配線は次バッチで
html_builder.py 側に追加する（本ファイルは純関数のみ）。
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from .config import operational_days_between

CENSUS_FLOOR_PCT = 3.0
SURGERY_FLOOR_PCT = 15.0
MIN_SAMPLES = 8
DEFAULT_WEEKS = 52


def census_spread_samples(series: pd.DataFrame, base_date, weeks: int = DEFAULT_WEEKS,
                           short: int = 7, long_: int = 28, value_col: str = "値") -> list[float]:
    """直近 weeks 週、週次サンプル点(各週の月曜)での
    (直近short日平均 - 直近long_日平均)/直近long_日平均 のスプレッド(%)を集める。

    triage._ma_spread と同一式（判定コードは共有しない。本モジュール冒頭の注記参照）。
    """
    base_date = pd.Timestamp(base_date)
    monday = base_date - pd.Timedelta(days=base_date.weekday())
    out: list[float] = []
    for i in range(weeks):
        d = monday - pd.Timedelta(weeks=i)
        s_win = series[(series["日付"] > d - pd.Timedelta(days=short)) & (series["日付"] <= d)]
        l_win = series[(series["日付"] > d - pd.Timedelta(days=long_)) & (series["日付"] <= d)]
        if len(s_win) < max(3, short // 2) or len(l_win) < max(7, long_ // 2):
            continue
        ma_s, ma_l = s_win[value_col].mean(), l_win[value_col].mean()
        if ma_l:
            out.append((ma_s - ma_l) / ma_l * 100.0)
    return out


def surgery_rate_spread_samples(surg: pd.DataFrame, base_date, weeks: int = DEFAULT_WEEKS,
                                 window: int = 28) -> list[float]:
    """直近 weeks 週、週次サンプル点での 28日窓 件/営業日レート の
    直近28日 vs 前28日 スプレッド(%)。

    triage._surgery_trend と同一式（判定コードは共有しない。本モジュール冒頭の注記参照）。
    """
    base_date = pd.Timestamp(base_date)
    monday = base_date - pd.Timedelta(days=base_date.weekday())
    ga = surg[surg["全麻"]]
    daily = ga.groupby("手術実施日").size()
    out: list[float] = []
    for i in range(weeks):
        d = monday - pd.Timedelta(weeks=i)
        now_start, now_end = d - pd.Timedelta(days=window - 1), d
        prev_start, prev_end = d - pd.Timedelta(days=2 * window - 1), d - pd.Timedelta(days=window)
        now_cnt = daily[(daily.index >= now_start) & (daily.index <= now_end)].sum()
        prev_cnt = daily[(daily.index >= prev_start) & (daily.index <= prev_end)].sum()
        biz_now = operational_days_between(now_start, now_end)
        biz_prev = operational_days_between(prev_start, prev_end)
        if biz_now == 0 or biz_prev == 0:
            continue
        rate_now, rate_prev = now_cnt / biz_now, prev_cnt / biz_prev
        if rate_prev:
            out.append((rate_now - rate_prev) / rate_prev * 100.0)
    return out


def unit_sigma(samples: list[float]) -> Optional[float]:
    """サンプル数が MIN_SAMPLES 未満なら None（縮退）。"""
    if len(samples) < MIN_SAMPLES:
        return None
    s = pd.Series(samples)
    return round(float(s.std(ddof=1)), 2)


def band_width_pct(sigma_pct: Optional[float], floor_pct: float) -> float:
    """帯の半幅(%)。sigma不明時はfloorのまま、既知なら floor と 1.5σ の大きい方。"""
    if sigma_pct is None:
        return floor_pct
    return max(floor_pct, 1.5 * sigma_pct)


def build_band(kind: str, sigma_pct: Optional[float]) -> dict:
    """kind: 'census' | 'surgery_rate'。表示側(JS)へ渡す最小情報のみ返す
    （帯の上下限系列そのものは送らない。中心線×(1±width/100)をJS側で描く）。"""
    floor = CENSUS_FLOOR_PCT if kind == "census" else SURGERY_FLOOR_PCT
    width = band_width_pct(sigma_pct, floor)
    return {"sigma_pct": sigma_pct, "width_pct": round(width, 1), "floor_pct": floor}
