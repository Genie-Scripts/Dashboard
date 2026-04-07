"""
profit.py — 粗利KPI算出
診療科別月次粗利・年度累計・達成率・トレンド
"""

import pandas as pd
import numpy as np
from typing import Optional


def _fy_start(month: pd.Timestamp) -> pd.Timestamp:
    """対象月の年度開始（4月1日）"""
    y = month.year if month.month >= 4 else month.year - 1
    return pd.Timestamp(f"{y}-04-01")


def build_profit_monthly(profit_data: pd.DataFrame,
                          profit_targets: pd.DataFrame) -> pd.DataFrame:
    """月次粗利に目標・達成率・前月比を付加した全科縦持ちDFを返す

    Returns:
        DataFrame: 診療科名, 月, 粗利, 月次目標, 達成率, 前月比, 前月比率
    """
    tgt_map = profit_targets.set_index("診療科名")["月次目標"].to_dict()
    df = profit_data.copy()
    df["月次目標"] = df["診療科名"].map(tgt_map)
    df["達成率"] = np.where(
        df["月次目標"].notna() & (df["月次目標"] > 0),
        (df["粗利"] / df["月次目標"] * 100).round(1),
        np.nan,
    )
    # 前月比
    df = df.sort_values(["診療科名", "月"])
    df["前月比"] = df.groupby("診療科名")["粗利"].diff().round(1)
    df["前月比率"] = (df["前月比"] / df.groupby("診療科名")["粗利"].shift(1) * 100).round(1)
    return df.reset_index(drop=True)


def get_latest_month_summary(profit_monthly: pd.DataFrame,
                              base_month: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """最新月（または指定月）の診療科別サマリー

    Returns:
        DataFrame: 診療科名, 粗利, 月次目標, 達成率, 前月比, 前月比率
                   ※達成率降順ソート済み
    """
    if base_month is None:
        base_month = profit_monthly["月"].max()
    latest = profit_monthly[profit_monthly["月"] == base_month].copy()
    return latest.sort_values("達成率", ascending=False, na_position="last").reset_index(drop=True)


def get_ytd_summary(profit_monthly: pd.DataFrame,
                     base_month: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """今年度の診療科別累計粗利サマリー

    Returns:
        DataFrame: 診療科名, 年度累計, 月次目標, 年度目標, 達成率(年度), 月数
    """
    if base_month is None:
        base_month = profit_monthly["月"].max()
    fy_start = _fy_start(base_month)
    months_elapsed = (base_month.year - fy_start.year) * 12 + (base_month.month - fy_start.month) + 1

    df = profit_monthly[(profit_monthly["月"] >= fy_start)
                         & (profit_monthly["月"] <= base_month)]

    agg = (df.groupby("診療科名")["粗利"].sum()
             .reset_index()
             .rename(columns={"粗利": "年度累計"}))

    # 月次目標（最新月から引用）
    latest_tgt = (profit_monthly[profit_monthly["月"] == base_month]
                  .set_index("診療科名")["月次目標"])
    agg["月次目標"]  = agg["診療科名"].map(latest_tgt)
    agg["年度目標"]  = agg["月次目標"] * 12
    agg["達成率"]    = np.where(
        agg["年度目標"].notna() & (agg["年度目標"] > 0),
        (agg["年度累計"] / (agg["月次目標"] * months_elapsed) * 100).round(1),
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

    Returns:
        {
          "base_month": Timestamp,
          "hospital_total": float,         # 全科合計（最新月）百万円
          "hospital_target": float,        # 全科合計目標 百万円
          "hospital_achievement": float,   # 全科達成率
          "hospital_ytd": float,           # 年度累計 億円
          "hospital_ytd_target": float,    # 年度目標 億円
          "hospital_3m_avg": float,        # 直近3ヶ月平均 百万円
          "hospital_ytd_monthly_avg": float, # 年度累計月平均 百万円
          "prev_month_total": float,       # 前年同月合計 百万円
          "prev_3m_avg": float,            # 前年同期3ヶ月平均 百万円
          "prev_ytd_monthly_avg": float,   # 前年度月平均 百万円
          "top3": [...],
          "bottom3": [...],
        }
    """
    if base_month is None:
        base_month = profit_monthly["月"].max()

    latest = get_latest_month_summary(profit_monthly, base_month)
    ytd    = get_ytd_summary(profit_monthly, base_month)

    total     = latest["粗利"].sum()
    tgt_total = latest["月次目標"].sum()
    ach_total = round(total / tgt_total * 100, 1) if tgt_total > 0 else None

    ytd_total     = ytd["年度累計"].sum()
    ytd_tgt_total = ytd["年度目標"].sum()

    # 年度経過月数
    fy_start = _fy_start(base_month)
    months_elapsed = (base_month.year - fy_start.year) * 12 + (base_month.month - fy_start.month) + 1

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
        return {
            "name":        row["診療科名"],
            "actual":      round(float(row["粗利"]) / 1000, 1),   # 百万円
            "target":      round(float(row["月次目標"]) / 1000, 1) if pd.notna(row["月次目標"]) else None,
            "achievement": float(row["達成率"]) if pd.notna(row["達成率"]) else None,
            "mom":         round(float(row["前月比"]) / 1000, 1) if pd.notna(row.get("前月比")) else None,
        }

    top3    = [_row_to_dict(r) for _, r in latest.head(3).iterrows()]
    bottom3 = [_row_to_dict(r) for _, r in
                latest[latest["達成率"].notna()].tail(3).iterrows()]

    return {
        "base_month":              base_month,
        "hospital_total":          round(total / 1000, 1),           # 百万円
        "hospital_target":         round(tgt_total / 1000, 1),
        "hospital_achievement":    ach_total,
        "hospital_ytd":            round(ytd_total / 1000000, 2),    # 億円
        "hospital_ytd_target":     round(ytd_tgt_total / 1000000, 2),
        "hospital_3m_avg":         avg_3m,
        "hospital_ytd_monthly_avg": ytd_monthly_avg,
        "prev_month_total":        prev_total,
        "prev_3m_avg":             prev_avg_3m,
        "prev_ytd_monthly_avg":    prev_fy_monthly_avg,
        "top3":    top3,
        "bottom3": bottom3,
    }


def build_profit_chart_data(profit_monthly: pd.DataFrame) -> dict:
    """JS埋め込み用粗利グラフデータ

    Returns:
        {
          "global": {"months": [...], "values": [...], "targets": [...]},
          "by_dept": {"総合内科": {"months":[], "values":[], "target": float}, ...}
        }
    """
    def _fmt_month(m) -> str:
        return m.strftime("%Y-%m") if hasattr(m, "strftime") else str(m)[:7]

    # 全科合計月次
    global_agg = (profit_monthly.groupby("月")
                  .agg(粗利=("粗利","sum"), 月次目標=("月次目標","sum"))
                  .reset_index()
                  .sort_values("月"))
    global_data = {
        "months":  [_fmt_month(m) for m in global_agg["月"]],
        "values":  [round(v/1000, 1) for v in global_agg["粗利"]],      # 百万円
        "targets": [round(v/1000, 1) if pd.notna(v) else None
                    for v in global_agg["月次目標"]],
    }

    # 診療科別
    by_dept = {}
    for dept, grp in profit_monthly.groupby("診療科名"):
        grp = grp.sort_values("月")
        tgt = grp["月次目標"].iloc[-1] if len(grp) > 0 else None
        by_dept[dept] = {
            "months":  [_fmt_month(m) for m in grp["月"]],
            "values":  [round(v/1000, 1) for v in grp["粗利"]],
            "target":  round(float(tgt)/1000, 1) if pd.notna(tgt) else None,
            "achievements": [round(float(a), 1) if pd.notna(a) else None
                             for a in grp["達成率"]],
        }

    return {"global": global_data, "by_dept": by_dept}
