"""
profit.py — 粗利KPI算出
診療科別月次粗利・年度累計・達成率・トレンド

達成率は「補正後月次目標」との比で評価する:
  - 内訳モード（外来/入院の内訳データ＆目標が両方ある）:
      月次補正目標 = 外来目標 × biz_days(m)/STD_BIZ + 入院目標 × cal_days(m)/STD_CAL
      達成率 = 粗利合計 / 月次補正目標 × 100
  - 旧式モード（合算データ＆合算目標のみ）:
      月次補正目標 = 月次目標 × biz_days(m)/STD_BIZ
      達成率 = 日次粗利 / 日次目標 × 100  （=粗利/月次補正目標×100 と等価）

営業日数は config.is_operational_day、暦日数は config.calendar_days_in_month で判定。
"""

import pandas as pd
import numpy as np
from typing import Optional

from .config import (
    STD_BIZ_DAYS_PER_MONTH, STD_CAL_DAYS_PER_MONTH,
    biz_days_in_month, calendar_days_in_month,
)


def _fy_start(month: pd.Timestamp) -> pd.Timestamp:
    """対象月の年度開始（4月1日）"""
    y = month.year if month.month >= 4 else month.year - 1
    return pd.Timestamp(f"{y}-04-01")


def _fy_cum_biz_days(fy_start: pd.Timestamp, base_month: pd.Timestamp) -> int:
    """年度開始月から base_month までの累計営業日数"""
    months = pd.date_range(fy_start, base_month, freq="MS")
    return sum(biz_days_in_month(m) for m in months)


def _fy_cum_cal_days(fy_start: pd.Timestamp, base_month: pd.Timestamp) -> int:
    """年度開始月から base_month までの累計暦日数"""
    months = pd.date_range(fy_start, base_month, freq="MS")
    return sum(calendar_days_in_month(m) for m in months)


def _has_breakdown(profit_breakdown, profit_targets_breakdown) -> bool:
    """内訳モード判定: 内訳データ＆内訳目標の両方が提供されているか"""
    return (profit_breakdown is not None and len(profit_breakdown) > 0
            and profit_targets_breakdown is not None and len(profit_targets_breakdown) > 0)


def build_profit_monthly(profit_data: pd.DataFrame,
                          profit_targets: pd.DataFrame,
                          profit_breakdown: Optional[pd.DataFrame] = None,
                          profit_targets_breakdown: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """月次粗利に目標・達成率・前月比を付加した全科縦持ちDFを返す

    内訳（profit_breakdown と profit_targets_breakdown）が両方提供されている場合:
      月次補正目標 = 外来目標×biz/STD_BIZ + 入院目標×cal/STD_CAL
      達成率 = 粗利 / 月次補正目標 × 100
    それ以外は旧式（営業日補正のみ）:
      月次補正目標 = 月次目標 × biz/STD_BIZ
      達成率 = 日次粗利 / 日次目標 × 100

    Returns:
        DataFrame (内訳モード時は追加列あり):
          [common]
          診療科名, 月, 粗利, 月次目標, 当月営業日数, 日次粗利, 日次目標,
          月次補正目標, 達成率, 前月比, 前月比率
          [内訳モードのみ追加]
          外来粗利, 入院粗利, 外来目標, 入院目標, 当月暦日数,
          外来補正目標, 入院補正目標
    """
    tgt_map = profit_targets.set_index("診療科名")["月次目標"].to_dict()
    df = profit_data.copy()
    df["月次目標"] = df["診療科名"].map(tgt_map)

    df["当月営業日数"] = df["月"].map(biz_days_in_month).astype("Int64")
    biz = df["当月営業日数"].astype("float")

    # 日次ペース（営業日基準）: 旧式表示の互換維持
    df["日次粗利"] = np.where(biz > 0, df["粗利"] / biz, np.nan)
    df["日次目標"] = np.where(
        df["月次目標"].notna() & (df["月次目標"] > 0),
        df["月次目標"] / STD_BIZ_DAYS_PER_MONTH,
        np.nan,
    )

    if _has_breakdown(profit_breakdown, profit_targets_breakdown):
        # 内訳モード: 外来/入院別の補正後目標を作って合算
        bd_pivot = (profit_breakdown.pivot_table(
                        index=["診療科名", "月"], columns="区分",
                        values="粗利", aggfunc="sum")
                    .reset_index())
        bd_pivot.columns.name = None
        rename_data = {}
        if "外来" in bd_pivot.columns: rename_data["外来"] = "外来粗利"
        if "入院" in bd_pivot.columns: rename_data["入院"] = "入院粗利"
        bd_pivot = bd_pivot.rename(columns=rename_data)
        for c in ("外来粗利", "入院粗利"):
            if c not in bd_pivot.columns:
                bd_pivot[c] = np.nan
        df = df.merge(bd_pivot[["診療科名", "月", "外来粗利", "入院粗利"]],
                      on=["診療科名", "月"], how="left")

        tb_pivot = (profit_targets_breakdown.pivot_table(
                        index="診療科名", columns="区分",
                        values="月次目標", aggfunc="sum")
                    .reset_index())
        tb_pivot.columns.name = None
        rename_tgt = {}
        if "外来" in tb_pivot.columns: rename_tgt["外来"] = "外来目標"
        if "入院" in tb_pivot.columns: rename_tgt["入院"] = "入院目標"
        tb_pivot = tb_pivot.rename(columns=rename_tgt)
        for c in ("外来目標", "入院目標"):
            if c not in tb_pivot.columns:
                tb_pivot[c] = np.nan
        df = df.merge(tb_pivot[["診療科名", "外来目標", "入院目標"]],
                      on="診療科名", how="left")

        df["当月暦日数"] = df["月"].map(calendar_days_in_month).astype("Int64")
        cal = df["当月暦日数"].astype("float")

        gairai_adj = np.where(
            df["外来目標"].notna() & (df["外来目標"] > 0) & (biz > 0),
            df["外来目標"] * biz / STD_BIZ_DAYS_PER_MONTH,
            0.0,
        )
        nyuin_adj = np.where(
            df["入院目標"].notna() & (df["入院目標"] > 0) & (cal > 0),
            df["入院目標"] * cal / STD_CAL_DAYS_PER_MONTH,
            0.0,
        )
        has_any_tgt = df["外来目標"].notna() | df["入院目標"].notna()
        df["月次補正目標"] = np.where(has_any_tgt, gairai_adj + nyuin_adj, np.nan)
        # チャート用の区分別補正目標（外来は営業日、入院は暦日で補正）
        df["外来補正目標"] = np.where(
            df["外来目標"].notna() & (df["外来目標"] > 0) & (biz > 0),
            df["外来目標"] * biz / STD_BIZ_DAYS_PER_MONTH,
            np.nan,
        )
        df["入院補正目標"] = np.where(
            df["入院目標"].notna() & (df["入院目標"] > 0) & (cal > 0),
            df["入院目標"] * cal / STD_CAL_DAYS_PER_MONTH,
            np.nan,
        )

        df["達成率"] = np.where(
            df["月次補正目標"].notna() & (df["月次補正目標"] > 0),
            (df["粗利"] / df["月次補正目標"] * 100).round(1),
            np.nan,
        )
    else:
        # 旧式モード（営業日補正のみ）
        df["月次補正目標"] = np.where(
            df["月次目標"].notna() & (df["月次目標"] > 0) & (biz > 0),
            df["月次目標"] * biz / STD_BIZ_DAYS_PER_MONTH,
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
    """最新月（または指定月）の診療科別サマリー（達成率降順）"""
    if base_month is None:
        base_month = profit_monthly["月"].max()
    latest = profit_monthly[profit_monthly["月"] == base_month].copy()
    return latest.sort_values("達成率", ascending=False, na_position="last").reset_index(drop=True)


def get_ytd_summary(profit_monthly: pd.DataFrame,
                     base_month: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """今年度の診療科別累計粗利サマリー

    達成率は 年度累計 / 年度累計補正目標（=月別補正目標の和） × 100。

    Returns:
        DataFrame: 診療科名, 年度累計, 月次目標, 年度目標, 年度累計補正目標,
                   累計営業日, 日次目標, 達成率, 月数
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

    # 年度累計補正目標 = 年度内の月別補正目標の和
    cum_adj = df.groupby("診療科名")["月次補正目標"].sum()
    agg["年度累計補正目標"] = agg["診療科名"].map(cum_adj)
    agg["累計営業日"] = cum_biz_days
    agg["日次目標"] = np.where(
        agg["月次目標"].notna() & (agg["月次目標"] > 0),
        agg["月次目標"] / STD_BIZ_DAYS_PER_MONTH,
        np.nan,
    )

    agg["達成率"] = np.where(
        agg["年度累計補正目標"].notna() & (agg["年度累計補正目標"] > 0),
        (agg["年度累計"] / agg["年度累計補正目標"] * 100).round(1),
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

    達成率は補正後月次目標ベース。絶対額（百万円）と日次ペース（万円/営業日）を併存。
    内訳モード時は外来/入院別の日次ペースも併記する。

    Returns:
        {
          "base_month": Timestamp,
          "hospital_total": float,             # 全科合計（最新月）百万円
          "hospital_target": float,            # 全科合計目標 百万円
          "hospital_adj_target": float,        # 全科補正後目標 百万円
          "hospital_achievement": float,       # 全科達成率(補正後ベース)
          "hospital_daily_pace": float,        # 全科 日次粗利 万円/営業日
          "hospital_daily_target": float,      # 全科 日次目標 万円/営業日
          "current_biz_days": int,             # 当月営業日数
          "current_cal_days": int,             # 当月暦日数
          "std_biz_days": int,                 # 標準営業日数(=20)
          "std_cal_days": float,               # 標準暦日数(≒30.4)
          "has_breakdown": bool,               # 内訳モード稼働中か
          # 内訳モードのみ:
          "hospital_gairai_total": float,      # 外来粗利合計 百万円
          "hospital_nyuin_total": float,       # 入院粗利合計 百万円
          "hospital_gairai_daily_pace": float, # 外来 万円/営業日
          "hospital_nyuin_daily_pace": float,  # 入院 万円/暦日
          "hospital_gairai_daily_target": float,
          "hospital_nyuin_daily_target": float,
          # 年度:
          "hospital_ytd": float,               # 年度累計 億円
          "hospital_ytd_target": float,        # 年度目標 億円
          "hospital_ytd_adj_target": float,    # 年度累計補正目標 億円
          "hospital_ytd_achievement": float,   # 年度累計達成率
          "hospital_3m_avg": float,
          "hospital_ytd_monthly_avg": float,
          "prev_month_total": float,
          "prev_3m_avg": float,
          "prev_ytd_monthly_avg": float,
          "top3": [...], "bottom3": [...],
        }
    """
    if base_month is None:
        base_month = profit_monthly["月"].max()

    latest = get_latest_month_summary(profit_monthly, base_month)
    ytd    = get_ytd_summary(profit_monthly, base_month)

    current_biz_days = biz_days_in_month(base_month)
    current_cal_days = calendar_days_in_month(base_month)
    has_breakdown = "外来粗利" in latest.columns

    total     = latest["粗利"].sum()
    tgt_total = latest["月次目標"].sum()
    adj_tgt_total = latest["月次補正目標"].sum() if "月次補正目標" in latest.columns else None

    # 全科 日次ペース・日次目標 (万円/営業日 = 千円÷10)
    daily_pace_total = round(total / current_biz_days / 10, 1) if current_biz_days > 0 else None
    daily_target_total = round(tgt_total / STD_BIZ_DAYS_PER_MONTH / 10, 1) if tgt_total > 0 else None

    # 全科達成率（補正後ベース）
    ach_total = (
        round(total / adj_tgt_total * 100, 1)
        if adj_tgt_total and adj_tgt_total > 0 else None
    )

    # 内訳ペース（内訳モードのみ）
    gairai_total = nyuin_total = None
    gairai_daily_pace = nyuin_daily_pace = None
    gairai_daily_target = nyuin_daily_target = None
    if has_breakdown:
        gairai_sum = float(latest["外来粗利"].sum()) if "外来粗利" in latest.columns else 0.0
        nyuin_sum  = float(latest["入院粗利"].sum()) if "入院粗利" in latest.columns else 0.0
        gairai_total = round(gairai_sum / 1000, 1)
        nyuin_total  = round(nyuin_sum / 1000, 1)
        gairai_daily_pace = round(gairai_sum / current_biz_days / 10, 1) if current_biz_days > 0 else None
        nyuin_daily_pace  = round(nyuin_sum / current_cal_days / 10, 1)  if current_cal_days > 0 else None
        gairai_tgt_sum = float(latest["外来目標"].sum()) if "外来目標" in latest.columns else 0.0
        nyuin_tgt_sum  = float(latest["入院目標"].sum()) if "入院目標" in latest.columns else 0.0
        gairai_daily_target = round(gairai_tgt_sum / STD_BIZ_DAYS_PER_MONTH / 10, 1) if gairai_tgt_sum > 0 else None
        nyuin_daily_target  = round(nyuin_tgt_sum  / STD_CAL_DAYS_PER_MONTH / 10, 1) if nyuin_tgt_sum  > 0 else None

    ytd_total     = ytd["年度累計"].sum()
    ytd_tgt_total = ytd["年度目標"].sum()
    ytd_adj_tgt_total = (ytd["年度累計補正目標"].sum()
                         if "年度累計補正目標" in ytd.columns else None)

    fy_start = _fy_start(base_month)
    months_elapsed = (base_month.year - fy_start.year) * 12 + (base_month.month - fy_start.month) + 1

    # 年度累計達成率（補正後ベース）
    ytd_achievement = (
        round(ytd_total / ytd_adj_tgt_total * 100, 1)
        if ytd_adj_tgt_total and ytd_adj_tgt_total > 0 else None
    )

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
        cal = row.get("当月暦日数") if has_breakdown else None
        daily_pace = None
        if pd.notna(biz) and biz and float(biz) > 0:
            daily_pace = round(float(row["粗利"]) / float(biz) / 10, 1)
        daily_target = None
        if pd.notna(row["月次目標"]) and float(row["月次目標"]) > 0:
            daily_target = round(float(row["月次目標"]) / STD_BIZ_DAYS_PER_MONTH / 10, 1)
        d = {
            "name":         row["診療科名"],
            "actual":       round(float(row["粗利"]) / 1000, 1),
            "target":       round(float(row["月次目標"]) / 1000, 1) if pd.notna(row["月次目標"]) else None,
            "achievement":  float(row["達成率"]) if pd.notna(row["達成率"]) else None,
            "daily_pace":   daily_pace,
            "daily_target": daily_target,
            "biz_days":     int(biz) if pd.notna(biz) else None,
            "mom":          round(float(row["前月比"]) / 1000, 1) if pd.notna(row.get("前月比")) else None,
        }
        if has_breakdown:
            d["cal_days"] = int(cal) if pd.notna(cal) else None
            gairai_val = row.get("外来粗利")
            nyuin_val  = row.get("入院粗利")
            d["gairai_daily_pace"] = (round(float(gairai_val) / float(biz) / 10, 1)
                                       if pd.notna(gairai_val) and pd.notna(biz) and float(biz) > 0 else None)
            d["nyuin_daily_pace"]  = (round(float(nyuin_val) / float(cal) / 10, 1)
                                       if pd.notna(nyuin_val) and pd.notna(cal) and float(cal) > 0 else None)
        return d

    top3    = [_row_to_dict(r) for _, r in latest.head(3).iterrows()]
    bottom3 = [_row_to_dict(r) for _, r in
                latest[latest["達成率"].notna()].tail(3).iterrows()]

    return {
        "base_month":                base_month,
        "hospital_total":            round(total / 1000, 1),
        "hospital_target":           round(tgt_total / 1000, 1),
        "hospital_adj_target":       round(adj_tgt_total / 1000, 1) if adj_tgt_total else None,
        "hospital_achievement":      ach_total,
        "hospital_daily_pace":       daily_pace_total,
        "hospital_daily_target":     daily_target_total,
        "current_biz_days":          int(current_biz_days),
        "current_cal_days":          int(current_cal_days),
        "std_biz_days":              STD_BIZ_DAYS_PER_MONTH,
        "std_cal_days":              round(STD_CAL_DAYS_PER_MONTH, 2),
        "has_breakdown":             has_breakdown,
        "hospital_gairai_total":     gairai_total,
        "hospital_nyuin_total":      nyuin_total,
        "hospital_gairai_daily_pace":   gairai_daily_pace,
        "hospital_nyuin_daily_pace":    nyuin_daily_pace,
        "hospital_gairai_daily_target": gairai_daily_target,
        "hospital_nyuin_daily_target":  nyuin_daily_target,
        "hospital_ytd":              round(ytd_total / 1000000, 2),
        "hospital_ytd_target":       round(ytd_tgt_total / 1000000, 2),
        "hospital_ytd_adj_target":   round(ytd_adj_tgt_total / 1000000, 2) if ytd_adj_tgt_total else None,
        "hospital_ytd_achievement":  ytd_achievement,
        "hospital_3m_avg":           avg_3m,
        "hospital_ytd_monthly_avg":  ytd_monthly_avg,
        "prev_month_total":          prev_total,
        "prev_3m_avg":               prev_avg_3m,
        "prev_ytd_monthly_avg":      prev_fy_monthly_avg,
        "top3":    top3,
        "bottom3": bottom3,
    }


def build_profit_chart_data(profit_monthly: pd.DataFrame) -> dict:
    """JS埋め込み用粗利グラフデータ

    targets[m] は補正後月次目標（内訳モードは外来×biz/STD_BIZ＋入院×cal/STD_CAL、
    旧式は月次目標×biz/STD_BIZ）。targets_nominal は補正前の名目目標。

    Returns:
        {
          "global": {months,values,targets,targets_nominal,achievements,biz_days[,cal_days]},
          "by_dept": {dept: {months,values,target,targets,targets_nominal,achievements,biz_days
                            [,cal_days,gairai_values,nyuin_values,gairai_targets,nyuin_targets]}}
        }
        内訳モードでは by_dept[*] に外来/入院の月次値と日数補正済み目標配列が追加される。
    """
    def _fmt_month(m) -> str:
        return m.strftime("%Y-%m") if hasattr(m, "strftime") else str(m)[:7]

    df = profit_monthly.copy()
    has_breakdown = "外来粗利" in df.columns

    # 全科合計月次
    base_agg = {"粗利":         ("粗利", "sum"),
                "月次目標":     ("月次目標", "sum"),
                "月次補正目標": ("月次補正目標", "sum"),
                "当月営業日数": ("当月営業日数", "max")}
    if has_breakdown:
        base_agg["当月暦日数"] = ("当月暦日数", "max")
    global_agg = (df.groupby("月").agg(**base_agg)
                    .reset_index()
                    .sort_values("月"))

    g_values  = [round(v / 1000, 1) for v in global_agg["粗利"]]
    g_targets = [round(v / 1000, 1) if pd.notna(v) else None
                 for v in global_agg["月次補正目標"]]
    g_targets_nominal = [round(v / 1000, 1) if pd.notna(v) else None
                         for v in global_agg["月次目標"]]
    g_biz_days = [int(b) if pd.notna(b) else None for b in global_agg["当月営業日数"]]
    g_achievements = []
    for adj_t, total in zip(global_agg["月次補正目標"], global_agg["粗利"]):
        if pd.notna(adj_t) and adj_t > 0:
            g_achievements.append(round(total / adj_t * 100, 1))
        else:
            g_achievements.append(None)

    global_data = {
        "months":          [_fmt_month(m) for m in global_agg["月"]],
        "values":          g_values,
        "targets":         g_targets,
        "targets_nominal": g_targets_nominal,
        "achievements":    g_achievements,
        "biz_days":        g_biz_days,
    }
    if has_breakdown:
        global_data["cal_days"] = [int(b) if pd.notna(b) else None
                                    for b in global_agg["当月暦日数"]]

    # 診療科別
    by_dept = {}
    for dept, grp in profit_monthly.groupby("診療科名"):
        grp = grp.sort_values("月")
        tgt = grp["月次目標"].iloc[-1] if len(grp) > 0 else None
        d = {
            "months":          [_fmt_month(m) for m in grp["月"]],
            "values":          [round(v / 1000, 1) for v in grp["粗利"]],
            "target":          round(float(tgt) / 1000, 1) if pd.notna(tgt) else None,
            "targets":         [round(v / 1000, 1) if pd.notna(v) else None
                                for v in grp["月次補正目標"]],
            "targets_nominal": [round(float(v) / 1000, 1) if pd.notna(v) else None
                                for v in grp["月次目標"]],
            "achievements":    [round(float(a), 1) if pd.notna(a) else None
                                for a in grp["達成率"]],
            "biz_days":        [int(b) if pd.notna(b) else None
                                for b in grp["当月営業日数"]],
        }
        if has_breakdown:
            d["cal_days"] = [int(b) if pd.notna(b) else None
                              for b in grp["当月暦日数"]]
            d["gairai_values"]  = [round(float(v) / 1000, 1) if pd.notna(v) else None
                                    for v in grp["外来粗利"]]
            d["nyuin_values"]   = [round(float(v) / 1000, 1) if pd.notna(v) else None
                                    for v in grp["入院粗利"]]
            d["gairai_targets"] = [round(float(v) / 1000, 1) if pd.notna(v) else None
                                    for v in grp["外来補正目標"]]
            d["nyuin_targets"]  = [round(float(v) / 1000, 1) if pd.notna(v) else None
                                    for v in grp["入院補正目標"]]
        by_dept[dept] = d

    return {"global": global_data, "by_dept": by_dept}
