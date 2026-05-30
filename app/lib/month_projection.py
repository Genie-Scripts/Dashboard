"""
month_projection.py — 当月予測 (4 KPI) ペイロード

各 KPI の MTD実績／月末予測／月次目標／達成率を返す。
detail.html headline 直下の「📅 当月予測」カードに供給される。

予測式:
  - 粗利     : PLレポートと同じ G（MTDブレンド月末見込み × recency補正）。
               病院全体は profit_hybrid_g_override（補正済み, 百万円）を流用し、
               未指定時は meta.latest_mtdblend_total（補正なし）にフォールバック。
  - 在院日平均: (MTD person-days + 残暦日 × 直近30日 在院日平均) / 当月暦日数
  - 新入院   : MTD + 残暦日 × (直近30日 新入院 / 30)
  - 全身麻酔 : MTD + 残営業平日 × (直近28日 全麻 / 直近28日 営業平日数)

目標換算:
  - 粗利     : profit_monthly の月次補正目標合計 (営業日/暦日補正後)
  - 在院日平均: TARGET_INPATIENT_ALLDAY (583/日) のまま
  - 新入院   : (TARGET_ADMISSION_WEEKLY / 7) × 当月暦日数
  - 全身麻酔 : TARGET_GA_DAILY × 当月営業平日数
"""
from __future__ import annotations
from typing import Optional
import pandas as pd

from .config import (
    biz_days_in_month, calendar_days_in_month, is_operational_day,
    TARGET_INPATIENT_ALLDAY, TARGET_ADMISSION_WEEKLY, TARGET_GA_DAILY,
    STD_BIZ_DAYS_PER_MONTH, STD_CAL_DAYS_PER_MONTH,
)


def _status_css(rate: Optional[float]) -> str:
    if rate is None:
        return "mu"
    if rate >= 100:
        return "ok"
    if rate >= 90:
        return "wr"
    return "dr"


def _status_shape(rate: Optional[float]) -> str:
    if rate is None:
        return "—"
    if rate >= 100:
        return "▲"
    if rate >= 90:
        return "―"
    return "▼"


def _status_text(rate: Optional[float]) -> str:
    if rate is None:
        return "—"
    if rate >= 100:
        return "達成"
    if rate >= 90:
        return "接近"
    return "未達"


def _count_biz_days(start: pd.Timestamp, end: pd.Timestamp) -> int:
    """start〜end (両端含む) の営業平日数"""
    if end < start:
        return 0
    return sum(1 for d in pd.date_range(start, end, freq="D") if is_operational_day(d))


def build_month_projection_payload(
    adm: pd.DataFrame,
    surg: pd.DataFrame,
    profit_monthly: Optional[pd.DataFrame],
    profit_hybrid_meta: Optional[dict],
    profit_hybrid_hospital_series: Optional[dict],
    base_date: pd.Timestamp,
    *,
    dept: Optional[str] = None,
    dept_inpatient_target: Optional[float] = None,
    dept_admission_weekly: Optional[float] = None,
    dept_operation_weekly: Optional[float] = None,
    dept_profit_projection_total: Optional[float] = None,
    profit_hybrid_g_override: Optional[float] = None,
) -> dict:
    """当月予測ペイロードを生成。

    dept を指定すると当該診療科のみで集計し、目標も per-dept 値を使う。
    None の場合は病院全体。
    """
    base_date = pd.Timestamp(base_date).normalize()
    month_start = base_date.replace(day=1)
    month_end = month_start + pd.offsets.MonthEnd(0)

    cal_days_total = calendar_days_in_month(month_start)
    biz_days_total = biz_days_in_month(month_start)
    cal_days_elapsed = (base_date - month_start).days + 1
    cal_days_remaining = max(0, cal_days_total - cal_days_elapsed)
    biz_days_elapsed = _count_biz_days(month_start, base_date)
    biz_days_remaining = max(0, biz_days_total - biz_days_elapsed)

    win30_start = base_date - pd.Timedelta(days=29)
    win28_start = base_date - pd.Timedelta(days=27)

    if dept is not None:
        adm = adm[adm["診療科名"] == dept]
        surg = surg[surg["実施診療科"] == dept]

    # ── 在院 日平均 ──
    inp_mtd_daily = (adm[(adm["日付"] >= month_start) & (adm["日付"] <= base_date)]
                     .groupby("日付")["在院患者数"].sum())
    inp_mtd_sum = float(inp_mtd_daily.sum())
    inp_mtd_avg = float(inp_mtd_daily.mean()) if len(inp_mtd_daily) > 0 else 0.0

    inp_win30_daily = (adm[(adm["日付"] >= win30_start) & (adm["日付"] <= base_date)]
                       .groupby("日付")["在院患者数"].sum())
    inp_pace = float(inp_win30_daily.mean()) if len(inp_win30_daily) > 0 else 0.0
    inp_remaining_sum = inp_pace * cal_days_remaining
    inp_proj_avg = ((inp_mtd_sum + inp_remaining_sum) / cal_days_total
                    if cal_days_total > 0 else 0.0)
    inp_target = (float(dept_inpatient_target) if dept is not None
                  else float(TARGET_INPATIENT_ALLDAY))
    inp_rate = (round(inp_proj_avg / inp_target * 100, 1)
                if inp_target and inp_target > 0 else None)

    # ── 新入院 ──
    adm_mtd = float(adm[(adm["日付"] >= month_start)
                        & (adm["日付"] <= base_date)]["新入院患者数"].sum())
    adm_win30 = float(adm[(adm["日付"] >= win30_start)
                          & (adm["日付"] <= base_date)]["新入院患者数"].sum())
    adm_pace = adm_win30 / 30.0
    adm_proj = adm_mtd + adm_pace * cal_days_remaining
    adm_weekly = (float(dept_admission_weekly) if dept is not None
                  else float(TARGET_ADMISSION_WEEKLY))
    adm_target = (adm_weekly / 7.0 * cal_days_total
                  if adm_weekly and adm_weekly > 0 else None)
    adm_rate = (round(adm_proj / adm_target * 100, 1)
                if adm_target and adm_target > 0 else None)

    # ── 全身麻酔 (営業平日ペース) ──
    ga_mtd_df = surg[(surg["手術実施日"] >= month_start)
                     & (surg["手術実施日"] <= base_date)
                     & surg["全麻"]]
    ga_mtd = int(len(ga_mtd_df))
    ga_win28_df = surg[(surg["手術実施日"] >= win28_start)
                       & (surg["手術実施日"] <= base_date)
                       & surg["全麻"]]
    win28_biz = _count_biz_days(win28_start, base_date)
    ga_pace = (len(ga_win28_df) / win28_biz) if win28_biz > 0 else 0.0
    ga_proj = ga_mtd + ga_pace * biz_days_remaining
    if dept is not None:
        # 週目標 ÷ 5平日 × 月営業平日数
        ga_target = (float(dept_operation_weekly) / 5.0 * biz_days_total
                     if dept_operation_weekly else None)
    else:
        ga_target = TARGET_GA_DAILY * biz_days_total
    ga_rate = (round(ga_proj / ga_target * 100, 1)
               if ga_target and ga_target > 0 else None)

    # ── 粗利 ──
    # 病院全体の月末予測は PLレポートと同じ G（MTDブレンド × recency補正）を使う。
    #   profit_hybrid_g_override が来ればそれ（補正済み）、無ければ
    #   meta.latest_mtdblend_total（補正なし）→ latest_projection_total の順でフォールバック。
    # 目標は profit_monthly 最新月の per-dept 目標から当月の biz/cal で補正して再計算。
    # MTD実績は profit_data が月単位確定値のみなので、当月の途中段階では取得不能 → None で表示せず。
    profit_mtd = None
    profit_target = None
    profit_proj = None
    profit_rate = None
    if dept is not None:
        if dept_profit_projection_total is not None:
            profit_proj = round(float(dept_profit_projection_total), 1)
    elif profit_hybrid_g_override is not None:
        profit_proj = round(float(profit_hybrid_g_override), 1)
    elif profit_hybrid_meta:
        proj = (profit_hybrid_meta.get("latest_mtdblend_total")
                or profit_hybrid_meta.get("latest_projection_total"))
        if proj is not None:
            profit_proj = round(float(proj), 1)
    _ = profit_hybrid_hospital_series  # 将来 daily run-rate 推定が必要になった場合の参照点
    if profit_monthly is not None and len(profit_monthly) > 0:
        pm = (profit_monthly[profit_monthly["診療科名"] == dept]
              if dept is not None else profit_monthly)
        if len(pm) > 0:
            latest_month = pm["月"].max()
            latest_rows = pm[pm["月"] == latest_month]
            if len(latest_rows) > 0:
                has_bd = ("外来目標" in latest_rows.columns
                          and "入院目標" in latest_rows.columns)
                if has_bd:
                    g = float(latest_rows["外来目標"].fillna(0).sum())
                    n = float(latest_rows["入院目標"].fillna(0).sum())
                    tgt_sennen = (g * biz_days_total / STD_BIZ_DAYS_PER_MONTH
                                  + n * cal_days_total / STD_CAL_DAYS_PER_MONTH)
                else:
                    total_tgt = float(latest_rows["月次目標"].fillna(0).sum())
                    tgt_sennen = total_tgt * biz_days_total / STD_BIZ_DAYS_PER_MONTH
                if tgt_sennen > 0:
                    profit_target = round(tgt_sennen / 1000.0, 1)
    if profit_proj is not None and profit_target and profit_target > 0:
        profit_rate = round(profit_proj / profit_target * 100, 1)

    def _tile(icon, label, actual, projection, target, unit, rate, actual_unit=None):
        return {
            "icon": icon,
            "label": label,
            "actual": actual,
            "actual_unit": actual_unit or unit,
            "projection": projection,
            "target": target,
            "unit": unit,
            "rate": rate,
            "status_css": _status_css(rate),
            "status_shape": _status_shape(rate),
            "status_text": _status_text(rate),
        }

    def _i(v):
        return int(round(v)) if v is not None else None

    # per-dept で対象データがない KPI は None で返し、JS 側で非表示
    profit_tile = (None if (dept is not None
                            and profit_proj is None and profit_target is None)
                   else _tile("", "粗利",
                              profit_mtd, profit_proj, profit_target,
                              "百万円", profit_rate))
    operation_tile = (None if (dept is not None
                               and dept_operation_weekly is None)
                      else _tile("", "全身麻酔",
                                 ga_mtd, _i(ga_proj), _i(ga_target),
                                 "件", ga_rate))

    return {
        "meta": {
            "month": base_date.strftime("%Y-%m"),
            "base_date": base_date.strftime("%Y-%m-%d"),
            "cal_days_total": cal_days_total,
            "cal_days_elapsed": cal_days_elapsed,
            "cal_days_remaining": cal_days_remaining,
            "biz_days_total": biz_days_total,
            "biz_days_elapsed": biz_days_elapsed,
            "biz_days_remaining": biz_days_remaining,
        },
        "profit": profit_tile,
        "inpatient": _tile(
            "", "在院 日平均",
            round(inp_mtd_avg, 1), round(inp_proj_avg, 1),
            round(inp_target, 0) if inp_target else None,
            "人/日", inp_rate,
        ),
        "admission": _tile(
            "", "新入院",
            int(round(adm_mtd)), int(round(adm_proj)),
            _i(adm_target),
            "人", adm_rate,
        ),
        "operation": operation_tile,
    }
