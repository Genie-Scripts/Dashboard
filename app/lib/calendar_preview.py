"""calendar_preview.py — P4 暦プレビュー（来週層・来月層・長連休早期警戒層）。

「表示追加のみ・判定不変」の決定論モジュール。adm/surg等の実績データは一切引数に取らず、
base_date と日本の祝日カレンダー（config.day_type 系）だけから機械的にテキストを組み立てる
（LLM不要）。使う既存関数は config.day_type / nonop_run_len / is_long_holiday_eve /
operational_days_between / biz_days_in_month のみ。

3層:
  build_week_preview  : 来週（翌週月〜日）の営業日数・連休情報
  build_month_preview : 来月の営業日数が標準(20日)からどれだけ乖離しているか
  build_early_warning : 2〜4週間後に迫る長連休（run_len>=4）の早期警戒

詳細: spec/暦補正と学習ループ改修プラン.md §5 P4。
"""
from __future__ import annotations

import pandas as pd

from .config import (
    day_type, nonop_run_len, is_long_holiday_eve, operational_days_between,
    biz_days_in_month,
)

# 出典: 司令塔実測 2026-08-29（日次在院の病院合計・2024-01-01〜2026-08-27・営業日mean=555.7 n=644）
# run=4は4〜6日の合算(n=16: 466.8×8/506.2×5/351.0×3)。単調性確保のため小標本セルを統合。
# 年1回（年度替わり）に再計測して更新すること。
RUN_LEN_CENSUS_PROFILE = {1: 535, 2: 505, 3: 495, 4: 457, 9: 436}


def _nearest_profile(run_len: int) -> int:
    """RUN_LEN_CENSUS_PROFILE にキーが無ければ最も近い下位キーへ丸めて実測値を返す
    （5〜8→4、10以上→9）。"""
    keys = sorted(RUN_LEN_CENSUS_PROFILE)
    if run_len in RUN_LEN_CENSUS_PROFILE:
        return RUN_LEN_CENSUS_PROFILE[run_len]
    lower = [k for k in keys if k < run_len]
    return RUN_LEN_CENSUS_PROFILE[lower[-1] if lower else keys[0]]


def _diff_vs_baseline(run_len: int) -> int:
    """「ふだんの土日」(run=2)比の在院人数差を5人単位に丸めて返す。"""
    gap = RUN_LEN_CENSUS_PROFILE[2] - _nearest_profile(run_len)
    return int(round(gap / 5.0) * 5)


def build_week_preview(base_date) -> dict | None:
    """来週層: 翌週(月〜日)の営業日数が5日でない、または連休(run_len>=3)がかかるとき発火。"""
    base_date = pd.Timestamp(base_date)
    next_mon = base_date + pd.Timedelta(days=(7 - base_date.weekday()))
    week_end = next_mon + pd.Timedelta(days=6)
    window = list(pd.date_range(next_mon, week_end, freq="D"))
    biz_days = operational_days_between(next_mon, week_end)
    run_len = max(nonop_run_len(d) for d in window)
    if biz_days == 5 and run_len < 3:
        return None

    # run_len を達成している日から実際のブロック開始日まで遡る（週の外へ跨ることもある）。
    d_start = next(d for d in window if nonop_run_len(d) == run_len)
    while day_type(d_start - pd.Timedelta(days=1)) != "biz":
        d_start -= pd.Timedelta(days=1)
    is_eve = is_long_holiday_eve(d_start - pd.Timedelta(days=1), min_run=3)

    if run_len >= 3:
        gap = _diff_vs_baseline(run_len)
        text = (
            f"来週は{d_start.month}月{d_start.day}日から{run_len}日間の連休があります。"
            f"連休が{run_len}日続くと、在院はふだんの土日よりさらに{gap}人ほど少なくなる"
            f"傾向があります。退院を連休前に固めすぎないこと、連休明けの受け入れ枠を"
            f"あらかじめ空けておくことが、この時期の目安になります。"
        )
    else:
        text = (
            f"来週は営業日が{biz_days}日です（ふだんは5日）。"
            f"手術や新入院の件数は、その分だけ少なくなりやすい週です。"
        )
    return {"biz_days": biz_days, "run_len": run_len, "is_eve": is_eve, "text": text}


def build_month_preview(base_date) -> dict | None:
    """来月層: 翌月の営業日数が標準20日から2日以上乖離しているとき発火。"""
    base_date = pd.Timestamp(base_date)
    next_month_start = base_date.replace(day=1) + pd.DateOffset(months=1)
    biz_days = biz_days_in_month(next_month_start)
    if abs(biz_days - 20) <= 1:
        return None
    text = (
        f"来月は営業日が{biz_days}日です（標準は20日）。"
        f"月間の合計で見るときは、この差を見込んで読んでください。"
    )
    return {"biz_days": biz_days, "text": text}


def build_early_warning(base_date) -> dict | None:
    """早期警戒層: base_date+14〜+28日の範囲に開始日が入る非営業ブロックで run_len>=4
    が存在すれば発火（最も早いものを報告）。

    is_long_holiday_eve は基準日自身が連休初日だと False になるため使わず、
    未来の非営業ブロック開始日を自前で走査する。
    """
    base_date = pd.Timestamp(base_date)
    win_start = base_date + pd.Timedelta(days=14)
    win_end = base_date + pd.Timedelta(days=28)
    for d in pd.date_range(win_start, win_end, freq="D"):
        if day_type(d) == "biz":
            continue
        if day_type(d - pd.Timedelta(days=1)) != "biz":
            continue  # ブロックの開始日でない（走査済みの連休の続き）
        run_len = nonop_run_len(d)
        if run_len < 4:
            continue
        weeks_ahead = (d - base_date).days // 7
        end = d + pd.Timedelta(days=run_len - 1)
        chip_date = f"{d.month}/{d.day}-{end.day}"
        text = (
            f"{d.month}月{d.day}日から{run_len}日間の連休が近づいています"
            f"（あと{weeks_ahead}週）。連休前の週は予定入院と手術が減りやすいため、"
            f"いまのうちに連休前後の受け入れ計画を確認しておくと安心です。"
        )
        return {
            "start": d.strftime("%Y-%m-%d"),
            "run_len": run_len,
            "weeks_ahead": weeks_ahead,
            "text": text,
            "chip": ["連休", chip_date],
        }
    return None


def build_calendar_preview(base_date) -> dict | None:
    """3層をまとめて返す。全てNoneならNone（呼び出し側は if calendar_preview だけで判定可）。"""
    week = build_week_preview(base_date)
    month = build_month_preview(base_date)
    early = build_early_warning(base_date)
    if week is None and month is None and early is None:
        return None
    return {"week": week, "month": month, "early": early}
