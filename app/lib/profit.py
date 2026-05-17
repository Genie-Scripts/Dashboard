"""
profit.py — 粗利KPI算出
診療科別月次粗利・年度累計・達成率・トレンド

達成率は「営業日換算」(日次粗利 / 日次目標) で評価する。
営業日数は config.is_operational_day で判定。標準営業日数は config.STD_BIZ_DAYS_PER_MONTH。
"""

import pandas as pd
import numpy as np
from typing import Optional

from .config import STD_BIZ_DAYS_PER_MONTH, biz_days_in_month


def _fy_start(month: pd.Timestamp) -> pd.Timestamp:
    """対象月の年度開始（4月1日）"""
    y = month.year if month.month >= 4 else month.year - 1
    return pd.Timestamp(f"{y}-04-01")


def _fy_cum_biz_days(fy_start: pd.Timestamp, base_month: pd.Timestamp) -> int:
    """年度開始月から base_month までの累計営業日数"""
    months = pd.date_range(fy_start, base_month, freq="MS")
    return sum(biz_days_in_month(m) for m in months)


def build_profit_monthly(profit_data: pd.DataFrame,
                          profit_targets: pd.DataFrame) -> pd.DataFrame:
    """月次粗利に目標・達成率・前月比を付加した全科縦持ちDFを返す

    達成率は (日次粗利 / 日次目標) × 100 として営業日数で正規化する。

    Returns:
        DataFrame: 診療科名, 月, 粗利, 月次目標, 当月営業日数,
                   日次粗利, 日次目標, 達成率, 前月比, 前月比率
    """
    tgt_map = profit_targets.set_index("診療科名")["月次目標"].to_dict()
    df = profit_data.copy()
    df["月次目標"] = df["診療科名"].map(tgt_map)

    df["当月営業日数"] = df["月"].map(biz_days_in_month).astype("Int64")
    biz = df["当月営業日数"].astype("float")
    df["日次粗利"] = np.where(biz > 0, df["粗利"] / biz, np.nan)
    df["日次目標"] = np.where(
        df["月次目標"].notna() & (df["月次目標"] > 0),
        df["月次目標"] / STD_BIZ_DAYS_PER_MONTH,
        np.nan,
    )
    df["達成率"] = np.where(
        df["月次目標"].notna() & (df["月次目標"] > 0) & (biz > 0),
        (df["日次粗利"] / df["日次目標"] * 100).round(1),
        np.nan,
    )

    df = df.sort_values(["診療科名", "月"])
    df["前月比"] = df.groupby("診療科名")["粗利"].diff().round(1)
    df["前月比率"] = (df["前月比"] / df.groupby("診療科名")["粗利"].shift(1) * 100).round(1)
    return df.reset_index(drop=True)


def get_latest_month_summary(profit_monthly: pd.DataFrame,
                              base_month: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """最新月（または指定月）の診療科別サマリー

    Returns:
        DataFrame: 診療科名, 粗利, 月次目標, 当月営業日数, 日次粗利, 日次目標,
                   達成率, 前月比, 前月比率
                   ※達成率降順ソート済み
    """
    if base_month is None:
        base_month = profit_monthly["月"].max()
    latest = profit_monthly[profit_monthly["月"] == base_month].copy()
    return latest.sort_values("達成率", ascending=False, na_position="last").reset_index(drop=True)


def get_ytd_summary(profit_monthly: pd.DataFrame,
                     base_month: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """今年度の診療科別累計粗利サマリー

    達成率は (年度累計 / 累計営業日) を 日次目標(=月次目標/標準営業日数) と比較する
    日次ペース換算で算出する。

    Returns:
        DataFrame: 診療科名, 年度累計, 月次目標, 年度目標, 累計営業日, 日次目標,
                   達成率, 月数
    """
    if base_month is None:
        base_month = profit_monthly["月"].max()
    fy_start = _fy_start(base_month)
    months_elapsed = (base_month.year - fy_start.year) * 12 + (base_month.month - fy_start.month) + 1
    cum_biz_days = _fy_cum_biz_days(fy_start, base_month)

    df = profit_monthly[(profit_monthly["月"] >= fy_start)
                         & (profit_monthly["月"] <= base_month)]

    agg = (df.groupby("診療科名")["粗利"].sum()
             .reset_index()
             .rename(columns={"粗利": "年度累計"}))

    # 月次目標（最新月から引用）
    latest_tgt = (profit_monthly[profit_monthly["月"] == base_month]
                  .set_index("診療科名")["月次目標"])
    agg["月次目標"] = agg["診療科名"].map(latest_tgt)
    agg["年度目標"] = agg["月次目標"] * 12
    agg["累計営業日"] = cum_biz_days
    agg["日次目標"] = np.where(
        agg["月次目標"].notna() & (agg["月次目標"] > 0),
        agg["月次目標"] / STD_BIZ_DAYS_PER_MONTH,
        np.nan,
    )
    daily_actual = np.where(cum_biz_days > 0, agg["年度累計"] / cum_biz_days, np.nan)
    agg["達成率"] = np.where(
        agg["日次目標"].notna() & (agg["日次目標"] > 0) & (cum_biz_days > 0),
        (daily_actual / agg["日次目標"] * 100).round(1),
        np.nan,
    )
    agg["月数"] = months_elapsed
    return agg.sort_values("達成率", ascending=False, na_position="last").reset_index(drop=True)


def get_dept_profit_series(profit_monthly: pd.DataFrame,
                            dept: str) -> pd.DataFrame:
    """特定診療科の月次粗利時系列（全期間）"""
    return (profit_monthly[profit_monthly["診療科名"] == dept]
            .sort_values("月")
            .reset_index(drop=True))


def build_profit_kpi(profit_monthly: pd.DataFrame,
                      base_month: Optional[pd.Timestamp] = None) -> dict:
    """粗利タブ用トップKPI

    達成率は営業日換算ベース。絶対額（百万円）と日次ペース（万円/営業日）を併存させる。

    Returns:
        {
          "base_month": Timestamp,
          "hospital_total": float,            # 全科合計（最新月）百万円
          "hospital_target": float,           # 全科合計目標 百万円
          "hospital_achievement": float,      # 全科達成率(営業日換算)
          "hospital_daily_pace": float,       # 全科 日次粗利 万円/営業日
          "hospital_daily_target": float,     # 全科 日次目標 万円/営業日
          "current_biz_days": int,            # 当月営業日数
          "std_biz_days": int,                # 標準営業日数(=20)
          "hospital_ytd": float,              # 年度累計 億円
          "hospital_ytd_target": float,       # 年度目標 億円
          "hospital_ytd_achievement": float,  # 年度累計達成率(営業日換算)
          "hospital_3m_avg": float,           # 直近3ヶ月平均 百万円
          "hospital_ytd_monthly_avg": float,  # 年度累計月平均 百万円
          "prev_month_total": float,          # 前年同月合計 百万円
          "prev_3m_avg": float,               # 前年同期3ヶ月平均 百万円
          "prev_ytd_monthly_avg": float,      # 前年度月平均 百万円
          "top3": [...],
          "bottom3": [...],
        }
    """
    if base_month is None:
        base_month = profit_monthly["月"].max()

    latest = get_latest_month_summary(profit_monthly, base_month)
    ytd    = get_ytd_summary(profit_monthly, base_month)

    current_biz_days = biz_days_in_month(base_month)
    total     = latest["粗利"].sum()
    tgt_total = latest["月次目標"].sum()

    # 全科 日次ペース・日次目標 (万円/営業日 = 千円÷10)
    daily_pace_total = round(total / current_biz_days / 10, 1) if current_biz_days > 0 else None
    daily_target_total = round(tgt_total / STD_BIZ_DAYS_PER_MONTH / 10, 1) if tgt_total > 0 else None
    ach_total = (
        round(daily_pace_total / daily_target_total * 100, 1)
        if daily_pace_total is not None and daily_target_total and daily_target_total > 0
        else None
    )

    ytd_total     = ytd["年度累計"].sum()
    ytd_tgt_total = ytd["年度目標"].sum()

    # 年度経過月数 / 累計営業日
    fy_start = _fy_start(base_month)
    months_elapsed = (base_month.year - fy_start.year) * 12 + (base_month.month - fy_start.month) + 1
    cum_biz_days = _fy_cum_biz_days(fy_start, base_month)

    # 年度累計達成率（営業日換算）: (年度累計 / 累計営業日) / (月次目標合計 / 標準営業日数)
    ytd_achievement = None
    if cum_biz_days > 0 and tgt_total > 0:
        ytd_daily_actual = ytd_total / cum_biz_days
        ytd_daily_target = tgt_total / STD_BIZ_DAYS_PER_MONTH
        if ytd_daily_target > 0:
            ytd_achievement = round(ytd_daily_actual / ytd_daily_target * 100, 1)

    # 年度累計月平均
    ytd_monthly_avg = round(ytd_total / months_elapsed / 1000, 1) if months_elapsed > 0 else None

    # 直近3ヶ月平均
    m3_start = base_month - pd.DateOffset(months=2)
    recent_3m = profit_monthly[(profit_monthly["月"] >= m3_start) & (profit_monthly["月"] <= base_month)]
    monthly_3m = recent_3m.groupby("月")["粗利"].sum()
    avg_3m = round(float(monthly_3m.mean()) / 1000, 1) if len(monthly_3m) > 0 else None

    # 前年同月
    prev_month = base_month - pd.DateOffset(years=1)
    prev_latest = profit_monthly[profit_monthly["月"] == prev_month]
    prev_total = round(float(prev_latest["粗利"].sum()) / 1000, 1) if len(prev_latest) > 0 else None

    # 前年同期3ヶ月平均
    prev_3m_end = base_month - pd.DateOffset(years=1)
    prev_3m_start = prev_3m_end - pd.DateOffset(months=2)
    prev_3m = profit_monthly[(profit_monthly["月"] >= prev_3m_start) & (profit_monthly["月"] <= prev_3m_end)]
    prev_monthly_3m = prev_3m.groupby("月")["粗利"].sum()
    prev_avg_3m = round(float(prev_monthly_3m.mean()) / 1000, 1) if len(prev_monthly_3m) > 0 else None

    # 前年度月平均
    prev_fy_start = pd.Timestamp(f"{fy_start.year - 1}-04-01")
    prev_fy_end = pd.Timestamp(f"{fy_start.year}-03-31")
    prev_fy_data = profit_monthly[(profit_monthly["月"] >= prev_fy_start) & (profit_monthly["月"] <= prev_fy_end)]
    prev_fy_monthly = prev_fy_data.groupby("月")["粗利"].sum()
    prev_fy_monthly_avg = round(float(prev_fy_monthly.mean()) / 1000, 1) if len(prev_fy_monthly) > 0 else None

    def _row_to_dict(row):
        biz = row.get("当月営業日数")
        daily_pace = None
        if pd.notna(biz) and biz and float(biz) > 0:
            daily_pace = round(float(row["粗利"]) / float(biz) / 10, 1)  # 万円/営業日
        daily_target = None
        if pd.notna(row["月次目標"]) and float(row["月次目標"]) > 0:
            daily_target = round(float(row["月次目標"]) / STD_BIZ_DAYS_PER_MONTH / 10, 1)
        return {
            "name":         row["診療科名"],
            "actual":       round(float(row["粗利"]) / 1000, 1),  # 百万円
            "target":       round(float(row["月次目標"]) / 1000, 1) if pd.notna(row["月次目標"]) else None,
            "achievement":  float(row["達成率"]) if pd.notna(row["達成率"]) else None,
            "daily_pace":   daily_pace,    # 万円/営業日
            "daily_target": daily_target,  # 万円/営業日
            "biz_days":     int(biz) if pd.notna(biz) else None,
            "mom":          round(float(row["前月比"]) / 1000, 1) if pd.notna(row.get("前月比")) else None,
        }

    top3    = [_row_to_dict(r) for _, r in latest.head(3).iterrows()]
    bottom3 = [_row_to_dict(r) for _, r in
                latest[latest["達成率"].notna()].tail(3).iterrows()]

    return {
        "base_month":               base_month,
        "hospital_total":           round(total / 1000, 1),           # 百万円
        "hospital_target":          round(tgt_total / 1000, 1),
        "hospital_achievement":     ach_total,
        "hospital_daily_pace":      daily_pace_total,                 # 万円/営業日
        "hospital_daily_target":    daily_target_total,               # 万円/営業日
        "current_biz_days":         int(current_biz_days),
        "std_biz_days":             STD_BIZ_DAYS_PER_MONTH,
        "hospital_ytd":             round(ytd_total / 1000000, 2),    # 億円
        "hospital_ytd_target":      round(ytd_tgt_total / 1000000, 2),
        "hospital_ytd_achievement": ytd_achievement,
        "hospital_3m_avg":          avg_3m,
        "hospital_ytd_monthly_avg": ytd_monthly_avg,
        "prev_month_total":         prev_total,
        "prev_3m_avg":              prev_avg_3m,
        "prev_ytd_monthly_avg":     prev_fy_monthly_avg,
        "top3":    top3,
        "bottom3": bottom3,
    }


def build_profit_chart_data(profit_monthly: pd.DataFrame) -> dict:
    """JS埋め込み用粗利グラフデータ

    targets は当月営業日数で補正済み（月次目標 × 当月営業日数 / 標準営業日数）。
    バーの色分けに使う達成率を achievements に同梱する。

    Returns:
        {
          "global": {"months":[], "values":[], "targets":[], "targets_nominal":[], "achievements":[], "biz_days":[]},
          "by_dept": {"総合内科": {"months":[], "values":[], "target": float, "achievements":[], "biz_days":[]}, ...}
        }
    """
    def _fmt_month(m) -> str:
        return m.strftime("%Y-%m") if hasattr(m, "strftime") else str(m)[:7]

    # 全科合計月次（営業日補正後の目標で集計）
    df = profit_monthly.copy()
    biz = df["当月営業日数"].astype("float")
    df["月次目標_補正"] = np.where(
        df["月次目標"].notna() & (biz > 0),
        df["月次目標"] * biz / STD_BIZ_DAYS_PER_MONTH,
        np.nan,
    )
    global_agg = (df.groupby("月")
                  .agg(粗利=("粗利", "sum"),
                       月次目標=("月次目標", "sum"),
                       月次目標_補正=("月次目標_補正", "sum"),
                       当月営業日数=("当月営業日数", "max"))
                  .reset_index()
                  .sort_values("月"))

    g_values  = [round(v / 1000, 1) for v in global_agg["粗利"]]
    g_targets = [round(v / 1000, 1) if pd.notna(v) else None
                 for v in global_agg["月次目標_補正"]]
    g_targets_nominal = [round(v / 1000, 1) if pd.notna(v) else None
                         for v in global_agg["月次目標"]]
    g_biz_days = [int(b) if pd.notna(b) else None for b in global_agg["当月営業日数"]]
    g_achievements = []
    for v_t, b, total in zip(global_agg["月次目標"], global_agg["当月営業日数"], global_agg["粗利"]):
        if pd.notna(v_t) and v_t > 0 and pd.notna(b) and b > 0:
            daily_actual = total / b
            daily_target = v_t / STD_BIZ_DAYS_PER_MONTH
            g_achievements.append(round(daily_actual / daily_target * 100, 1) if daily_target > 0 else None)
        else:
            g_achievements.append(None)

    global_data = {
        "months":          [_fmt_month(m) for m in global_agg["月"]],
        "values":          g_values,        # 百万円
        "targets":         g_targets,       # 百万円（営業日補正後）
        "targets_nominal": g_targets_nominal,  # 百万円（補正前の月次目標合計）
        "achievements":    g_achievements,  # %（日次換算）
        "biz_days":        g_biz_days,
    }

    # 診療科別
    by_dept = {}
    for dept, grp in profit_monthly.groupby("診療科名"):
        grp = grp.sort_values("月")
        tgt = grp["月次目標"].iloc[-1] if len(grp) > 0 else None
        by_dept[dept] = {
            "months":       [_fmt_month(m) for m in grp["月"]],
            "values":       [round(v / 1000, 1) for v in grp["粗利"]],
            "target":       round(float(tgt) / 1000, 1) if pd.notna(tgt) else None,
            "achievements": [round(float(a), 1) if pd.notna(a) else None
                             for a in grp["達成率"]],
            "biz_days":     [int(b) if pd.notna(b) else None
                             for b in grp["当月営業日数"]],
        }

    return {"global": global_data, "by_dept": by_dept}
