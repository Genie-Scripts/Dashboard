"""
metrics.py — KPI算出エンジン（v2.1）
日次・週次KPI、移動平均、達成率、ランキング構築

v2.1 変更点:
  - 手術KPIの二重集計基準（病院全体=営業平日 / 診療科別=全日）
  - ステータス閾値を config.THRESHOLD_DANGER/THRESHOLD_OK に委譲
  - ga_rolling_calendar_dept() 新設（診療科別の暦日7日集計）
  - build_kpi_summary() にv2.1用フィールド追加
  - build_surgery_ranking() を全日基準に修正
"""

import pandas as pd
import numpy as np
from datetime import timedelta
from .config import (
    DEPT_HIDDEN, WARD_HIDDEN, SURGERY_DISPLAY_DEPTS,
    OR_MINUTES_PER_ROOM, OR_ROOM_COUNT,
    TARGET_INPATIENT_WEEKDAY, TARGET_INPATIENT_HOLIDAY, TARGET_INPATIENT_ALLDAY,
    TARGET_ADMISSION_WEEKLY, TARGET_GA_DAILY,
    THRESHOLD_DANGER, THRESHOLD_OK,
    status_label, status_display,
    build_headline,
)


# ════════════════════════════════════════
# 日次集計（変更なし）
# ════════════════════════════════════════

def daily_inpatient(adm: pd.DataFrame, date: pd.Timestamp) -> dict:
    """日次在院患者数（全体・診療科別・病棟別）"""
    day = adm[adm["日付"] == date]
    total = int(day["在院患者数"].sum())
    by_dept = (day[day["科_表示"]]
               .groupby("診療科名")["在院患者数"].sum()
               .astype(int).to_dict())
    by_ward = (day[day["病棟_表示"]]
               .groupby("病棟コード")["在院患者数"].sum()
               .astype(int).to_dict())
    is_weekday = date.weekday() < 5
    return {
        "date": date, "total": total, "is_weekday": is_weekday,
        "by_dept": by_dept, "by_ward": by_ward,
    }


def daily_new_admission(adm: pd.DataFrame, date: pd.Timestamp) -> dict:
    """日次新入院・退院・負荷"""
    day = adm[adm["日付"] == date]
    total_new = int(day["新入院患者数"].sum())
    total_emg = int(day["緊急入院患者数"].sum())
    total_discharge = int(day["退院合計"].sum())
    total_transfer_in = int(day["転入患者数"].sum())
    total_transfer_out = int(day["転出患者数"].sum())
    by_dept = (day[day["科_表示"]].groupby("診療科名")["新入院患者数"].sum().astype(int).to_dict())
    # 病棟は転入を含む（病棟単位の新入院実態）
    by_ward = (day[day["病棟_表示"]].groupby("病棟コード")["新入院患者数_病棟"].sum().astype(int).to_dict())
    by_ward_discharge = (day[day["病棟_表示"]].groupby("病棟コード")["退院合計"].sum().astype(int).to_dict())
    by_ward_load = (day[day["病棟_表示"]].groupby("病棟コード")["出入り負荷"].sum().astype(int).to_dict())
    return {
        "date": date, "total_new": total_new, "total_emg": total_emg,
        "total_discharge": total_discharge,
        "total_transfer_in": total_transfer_in, "total_transfer_out": total_transfer_out,
        "by_dept": by_dept, "by_ward": by_ward,
        "by_ward_discharge": by_ward_discharge, "by_ward_load": by_ward_load,
    }


def daily_surgery(surg: pd.DataFrame, date: pd.Timestamp) -> dict:
    """日次手術件数"""
    day = surg[surg["手術実施日"] == date]
    total = len(day)
    ga_total = int(day["全麻"].sum())
    by_dept = (day[day["科_表示"] & day["全麻"]].groupby("実施診療科").size().to_dict())
    return {"date": date, "total_ops": total, "total_ga": ga_total, "by_dept": by_dept}


def daily_or_utilization(surg: pd.DataFrame, date: pd.Timestamp) -> float:
    """日次手術室稼働率(%)"""
    day = surg[(surg["手術実施日"] == date) & surg["稼働対象室"] & surg["平日"]]
    if len(day) == 0:
        return 0.0
    total_minutes = day["稼働分"].sum()
    denominator = OR_MINUTES_PER_ROOM * OR_ROOM_COUNT
    return round(total_minutes / denominator * 100, 1)


# ════════════════════════════════════════
# 週次・ローリング集計
# ════════════════════════════════════════

def weekly_new_admission(adm: pd.DataFrame, date: pd.Timestamp) -> dict:
    """基準日を含む週(月〜日)の新入院累計"""
    weekday = date.weekday()
    monday = date - timedelta(days=weekday)
    week = adm[(adm["日付"] >= monday) & (adm["日付"] <= date)]
    total = int(week["新入院患者数"].sum())
    by_dept = (week[week["科_表示"]].groupby("診療科名")["新入院患者数"].sum().astype(int).to_dict())
    by_ward = (week[week["病棟_表示"]].groupby("病棟コード")["新入院患者数_病棟"].sum().astype(int).to_dict())
    return {"monday": monday, "date": date, "days_elapsed": (date - monday).days + 1,
            "total": total, "by_dept": by_dept, "by_ward": by_ward}


def weekly_surgery(surg: pd.DataFrame, date: pd.Timestamp) -> dict:
    """基準日を含む週の全麻件数累計"""
    weekday = date.weekday()
    monday = date - timedelta(days=weekday)
    week = surg[(surg["手術実施日"] >= monday) & (surg["手術実施日"] <= date)]
    ga_week = week[week["全麻"]]
    total = len(ga_week)
    by_dept = (ga_week[ga_week["科_表示"]].groupby("実施診療科").size().to_dict())
    return {"monday": monday, "date": date, "total": total, "by_dept": by_dept}


def rolling7_inpatient_avg(adm: pd.DataFrame, date: pd.Timestamp) -> dict:
    """直近7暦日の在院患者数日平均（診療科別・病棟別）

    日付ごとに合計してから7日平均を取る（行単位の平均ではない）
    """
    start = date - timedelta(days=6)
    window = adm[(adm["日付"] >= start) & (adm["日付"] <= date)]

    # 診療科: 日付×診療科で合計 → 診療科ごとに7日平均
    dept_daily = (window[window["科_表示"]]
                  .groupby(["日付", "診療科名"])["在院患者数"].sum())
    by_dept = dept_daily.groupby("診療科名").mean().round(1).to_dict()

    # 病棟: 日付×病棟コードで合計 → 病棟ごとに7日平均
    ward_daily = (window[window["病棟_表示"]]
                  .groupby(["日付", "病棟コード"])["在院患者数"].sum())
    by_ward = ward_daily.groupby("病棟コード").mean().round(1).to_dict()

    return {"start": start, "date": date, "by_dept": by_dept, "by_ward": by_ward}


def rolling7_new_admission(adm: pd.DataFrame, date: pd.Timestamp) -> dict:
    """直近7暦日の新入院累計"""
    start = date - timedelta(days=6)
    window = adm[(adm["日付"] >= start) & (adm["日付"] <= date)]
    total = int(window["新入院患者数"].sum())
    by_dept = (window[window["科_表示"]].groupby("診療科名")["新入院患者数"].sum().astype(int).to_dict())
    by_ward = (window[window["病棟_表示"]].groupby("病棟コード")["新入院患者数_病棟"].sum().astype(int).to_dict())
    return {"start": start, "date": date, "total": total, "by_dept": by_dept, "by_ward": by_ward}


def rolling7_surgery(surg: pd.DataFrame, date: pd.Timestamp) -> dict:
    """直近7暦日の全麻件数（診療科別=全日基準）★v2.1"""
    start = date - timedelta(days=6)
    window = surg[(surg["手術実施日"] >= start) & (surg["手術実施日"] <= date)]
    ga_window = window[window["全麻"]]
    total = len(ga_window)
    by_dept = (ga_window[ga_window["科_表示"]].groupby("実施診療科").size().to_dict())
    return {"start": start, "date": date, "total": total, "by_dept": by_dept}


def rolling28_surgery_dept(surg: pd.DataFrame, date: pd.Timestamp) -> dict:
    """直近28暦日の全麻件数（診療科別=全日基準、過去4週平均）★v2.1 新設"""
    start = date - timedelta(days=27)
    window = surg[(surg["手術実施日"] >= start) & (surg["手術実施日"] <= date)]
    ga_window = window[window["全麻"]]
    total = len(ga_window)
    by_dept = (ga_window[ga_window["科_表示"]].groupby("実施診療科").size().to_dict())
    # 4週平均
    avg_by_dept = {k: round(v / 4, 1) for k, v in by_dept.items()}
    return {"start": start, "date": date, "total": total,
            "by_dept": by_dept, "avg_by_dept": avg_by_dept}


# ════════════════════════════════════════
# 移動平均・トレンドデータ
# ════════════════════════════════════════

def build_daily_series(adm: pd.DataFrame, col: str = "在院患者数",
                       group_col: str = None, group_val: str = None,
                       display_filter: bool = True) -> pd.DataFrame:
    """日次集計の時系列"""
    df = adm.copy()
    if display_filter:
        if group_col == "病棟コード":
            df = df[df["病棟_表示"]]
        else:
            df = df[df["科_表示"]]
    if group_col and group_val:
        df = df[df[group_col] == group_val]
    series = df.groupby("日付")[col].sum().reset_index()
    series.columns = ["日付", "値"]
    return series.sort_values("日付")


def build_surgery_daily_series(surg: pd.DataFrame,
                               ga_only: bool = True,
                               dept: str = None) -> pd.DataFrame:
    """手術日次時系列"""
    df = surg.copy()
    if ga_only:
        df = df[df["全麻"]]
    if dept:
        df = df[df["実施診療科"] == dept]
    series = df.groupby("手術実施日").size().reset_index(name="値")
    series.columns = ["日付", "値"]
    return series.sort_values("日付")


def add_moving_average(series: pd.DataFrame, window: int = 7,
                       col: str = "値") -> pd.DataFrame:
    """移動平均を追加"""
    series = series.copy()
    series[f"MA{window}"] = series[col].rolling(window=window, min_periods=1).mean()
    return series


def ga_rolling_biz_avg(surg: pd.DataFrame, date: pd.Timestamp,
                       window: int = 7) -> dict:
    """
    全麻の直近N営業平日移動平均（病院全体KPI用）★営業平日基準
    """
    import jpholiday

    def _is_biz(d: pd.Timestamp) -> bool:
        if d.weekday() >= 5:
            return False
        if jpholiday.is_holiday(d.date()):
            return False
        if (d.month == 12 and d.day >= 29) or (d.month == 1 and d.day <= 3):
            return False
        return True

    fy_start_year = date.year if date.month >= 4 else date.year - 1
    fy_start = pd.Timestamp(f"{fy_start_year}-04-01")

    past = surg[surg["手術実施日"] <= date]
    daily_ga = (past[past["全麻"]].groupby("手術実施日").size().reset_index(name="件数"))
    daily_ga = daily_ga.sort_values("手術実施日")

    biz_rows = daily_ga[daily_ga["手術実施日"].apply(_is_biz)].copy()

    recent = biz_rows.tail(window)
    biz_days = len(recent)
    total = int(recent["件数"].sum()) if biz_days > 0 else 0
    avg = round(total / biz_days, 1) if biz_days > 0 else None

    if biz_days > 0:
        last_row = recent.iloc[-1]
        last_biz_date = last_row["手術実施日"]
        last_biz_count = int(last_row["件数"])
    else:
        last_biz_date = None
        last_biz_count = None

    fy_rows = biz_rows[biz_rows["手術実施日"] >= fy_start]
    fy_days = len(fy_rows)
    fy_biz_avg = round(fy_rows["件数"].sum() / fy_days, 1) if fy_days > 0 else None

    return {
        "avg": avg, "total": total, "biz_days": biz_days,
        "last_biz_date": last_biz_date, "last_biz_count": last_biz_count,
        "fy_biz_avg": fy_biz_avg,
    }


def ga_rolling_calendar_dept(surg: pd.DataFrame, date: pd.Timestamp,
                             window: int = 7) -> dict:
    """
    全麻の直近N暦日移動平均（診療科別KPI用）★全日基準 ★v2.1 新設

    Returns dict:
        total_by_dept : {科名: 直近N暦日の件数}
        avg_by_dept   : {科名: 直近N暦日の日平均}
    """
    start = date - timedelta(days=window - 1)
    window_data = surg[(surg["手術実施日"] >= start) & (surg["手術実施日"] <= date)]
    ga_data = window_data[window_data["全麻"] & window_data["科_表示"]]

    by_dept = ga_data.groupby("実施診療科").size().to_dict()
    avg_by_dept = {k: round(v / window, 2) for k, v in by_dept.items()}

    return {
        "total_by_dept": by_dept,
        "avg_by_dept": avg_by_dept,
    }


def build_biz_ma30_series(surg: pd.DataFrame, base_date: pd.Timestamp,
                          prev_year: bool = False) -> dict:
    """
    全麻の30平日移動平均を日次時系列で返す（病院全体KPI用）。

    各営業平日について、その日以前の直近30営業平日のGA件数平均を算出。
    prev_year=True の場合、1年前のデータで同じ計算を行い、
    日付は当年にアラインして返す。

    Returns:
        {"dates": [str, ...], "values": [float, ...]}
    """
    from .config import is_operational_day

    offset = timedelta(days=365) if prev_year else timedelta(0)
    shifted_base = base_date - offset

    # 全麻の日次件数
    ga = surg[surg["全麻"]].copy()
    daily_ga = ga.groupby("手術実施日").size().reset_index(name="件数")
    daily_ga = daily_ga.sort_values("手術実施日")
    ga_map = dict(zip(daily_ga["手術実施日"], daily_ga["件数"]))

    # 全日付リスト（データ範囲）
    if len(daily_ga) == 0:
        return {"dates": [], "values": []}
    min_date = daily_ga["手術実施日"].min()
    all_dates = pd.date_range(min_date, shifted_base, freq="D")

    # 営業平日の日付と件数を収集
    biz_dates = []
    biz_counts = []
    for d in all_dates:
        if is_operational_day(d):
            biz_dates.append(d)
            biz_counts.append(ga_map.get(d, 0))

    # 各営業平日について直近30平日の移動平均を算出
    result_dates = []
    result_values = []
    for i, d in enumerate(biz_dates):
        window_start = max(0, i - 29)
        window = biz_counts[window_start:i + 1]
        avg = round(sum(window) / len(window), 1)
        out_date = d + offset if prev_year else d
        result_dates.append(out_date.strftime("%Y-%m-%d"))
        result_values.append(avg)

    return {"dates": result_dates, "values": result_values}


def build_weekly_agg(series: pd.DataFrame) -> pd.DataFrame:
    """日次→週次集約（月曜始まり）"""
    df = series.copy()
    df["週開始"] = df["日付"] - pd.to_timedelta(df["日付"].dt.weekday, unit="D")
    weekly = df.groupby("週開始")["値"].agg(["sum", "mean", "count"]).reset_index()
    weekly.columns = ["週開始", "合計", "平均", "日数"]
    return weekly


# ════════════════════════════════════════
# 達成率・比較
# ════════════════════════════════════════

def _ga_biz_avg_in_range(surg: pd.DataFrame,
                         start: pd.Timestamp, end: pd.Timestamp) -> float:
    """指定期間内の平日全麻件数の日平均（平日のみカウント）"""
    import jpholiday

    def _is_biz(d: pd.Timestamp) -> bool:
        if d.weekday() >= 5:
            return False
        if jpholiday.is_holiday(d.date()):
            return False
        if (d.month == 12 and d.day >= 29) or (d.month == 1 and d.day <= 3):
            return False
        return True

    window = surg[(surg["手術実施日"] >= start) & (surg["手術実施日"] <= end) & surg["全麻"]]
    if len(window) == 0:
        return None
    daily_ga = window.groupby("手術実施日").size().reset_index(name="件数")
    biz_rows = daily_ga[daily_ga["手術実施日"].apply(_is_biz)]
    biz_days = len(biz_rows)
    return round(int(biz_rows["件数"].sum()) / biz_days, 1) if biz_days > 0 else None


def achievement_rate(actual, target) -> float:
    """達成率(%)"""
    if target is None or target == 0 or pd.isna(target):
        return None
    return round(actual / target * 100, 1)


def week_over_week(series: pd.DataFrame, date: pd.Timestamp,
                   col: str = "値") -> float:
    """前週同曜日比"""
    prev = date - timedelta(days=7)
    curr_val = series.loc[series["日付"] == date, col]
    prev_val = series.loc[series["日付"] == prev, col]
    if len(curr_val) == 0 or len(prev_val) == 0:
        return None
    return int(curr_val.iloc[0]) - int(prev_val.iloc[0])


# ════════════════════════════════════════
# ランキング構築
# ════════════════════════════════════════

def build_dept_ranking(adm: pd.DataFrame, date: pd.Timestamp,
                       targets: dict, metric: str = "inpatient",
                       sort_by: str = "achievement") -> pd.DataFrame:
    """診療科別ランキング（在院/新入院）"""
    if metric == "inpatient":
        r7 = rolling7_inpatient_avg(adm, date)
        data = r7["by_dept"]
        target_map = targets.get("inpatient", {}).get("dept", {})
    else:
        r7 = rolling7_new_admission(adm, date)
        data = r7["by_dept"]
        target_map = targets.get("new_admission", {}).get("dept", {})

    rows = []
    for dept, actual in data.items():
        target = target_map.get(dept)
        rate = achievement_rate(actual, target)
        st = status_label(rate)
        rows.append({"診療科": dept, "実績": actual, "目標": target, "達成率": rate, "status": st})

    df = pd.DataFrame(rows)
    if len(df) == 0:
        return df
    if sort_by == "achievement":
        df = df.sort_values("達成率", ascending=False, na_position="last")
    elif sort_by == "actual":
        df = df.sort_values("実績", ascending=False)
    df["順位"] = range(1, len(df) + 1)
    return df.reset_index(drop=True)


def build_ward_ranking(adm: pd.DataFrame, date: pd.Timestamp,
                       targets: dict, metric: str = "inpatient",
                       sort_by: str = "achievement") -> pd.DataFrame:
    """病棟別ランキング"""
    from .config import WARD_NAMES

    if metric == "inpatient":
        r7 = rolling7_inpatient_avg(adm, date)
        data = r7["by_ward"]
        target_map = targets.get("inpatient", {}).get("ward", {})
        beds_map = targets.get("inpatient", {}).get("ward_beds", {})
    else:
        r7 = rolling7_new_admission(adm, date)
        data = r7["by_ward"]
        target_map = targets.get("new_admission", {}).get("ward", {})
        beds_map = targets.get("inpatient", {}).get("ward_beds", {})

    rows = []
    for ward_code, actual in data.items():
        if ward_code in WARD_HIDDEN:
            continue
        target = target_map.get(ward_code)
        rate = achievement_rate(actual, target)
        beds = beds_map.get(ward_code)
        utilization = round(actual / beds * 100, 1) if beds else None
        st = status_label(rate)
        rows.append({
            "病棟コード": ward_code, "病棟名": WARD_NAMES.get(ward_code, ward_code),
            "実績": actual, "目標": target, "病床数": beds,
            "達成率": rate, "利用率": utilization, "status": st,
        })

    df = pd.DataFrame(rows)
    if len(df) == 0:
        return df
    if sort_by == "achievement":
        df = df.sort_values("達成率", ascending=False, na_position="last")
    elif sort_by == "actual":
        df = df.sort_values("実績", ascending=False)
    df["順位"] = range(1, len(df) + 1)
    return df.reset_index(drop=True)


def build_surgery_ranking(surg: pd.DataFrame, date: pd.Timestamp,
                          surgery_targets: dict,
                          sort_by: str = "achievement",
                          period: str = "7") -> pd.DataFrame:
    """
    診療科別 全麻ランキング ★v2.1: 全日(暦日)基準

    Args:
        period: "7" → 直近7暦日, "28" → 直近28暦日(4週平均), "fy" → 今年度
    """
    if period == "7":
        r = rolling7_surgery(surg, date)
        data = r["by_dept"]
    elif period == "28":
        r = rolling28_surgery_dept(surg, date)
        data = r["avg_by_dept"]  # 4週平均
    else:
        # 年度
        fy_year = date.year if date.month >= 4 else date.year - 1
        fy_start = pd.Timestamp(f"{fy_year}-04-01")
        fy_data = surg[(surg["手術実施日"] >= fy_start) & (surg["手術実施日"] <= date)]
        ga_fy = fy_data[fy_data["全麻"] & fy_data["科_表示"]]
        weeks = max(((date - fy_start).days + 1) / 7, 1)
        total_by_dept = ga_fy.groupby("実施診療科").size().to_dict()
        data = {k: round(v / weeks, 1) for k, v in total_by_dept.items()}

    rows = []
    for dept in SURGERY_DISPLAY_DEPTS:
        actual = data.get(dept, 0)
        target = surgery_targets.get(dept)
        rate = achievement_rate(actual, target)
        st = status_label(rate)
        rows.append({
            "診療科": dept, "実績": actual, "週目標": target,
            "達成率": rate, "status": st,
        })

    df = pd.DataFrame(rows)
    if sort_by == "achievement":
        df = df.sort_values("達成率", ascending=False, na_position="last")
    elif sort_by == "actual":
        df = df.sort_values("実績", ascending=False)
    df["順位"] = range(1, len(df) + 1)
    return df.reset_index(drop=True)


# ════════════════════════════════════════
# 医師版ウォッチランキング（変更なし）
# ════════════════════════════════════════

def build_doctor_watch_ranking(adm, surg, date, targets, surg_targets, top_n=10):
    """要注視診療科ランキング（スコア = 新入院未達×0.5 + MA乖離×0.3 + 手術未達×0.2）"""
    r7_nadm = rolling7_new_admission(adm, date)
    nadm_by_dept = r7_nadm["by_dept"]
    nadm_tgt_map = targets.get("new_admission", {}).get("dept", {})
    r7_surg = rolling7_surgery(surg, date)
    surg_by_dept = r7_surg["by_dept"]

    ma7_map = {}
    for dept in nadm_tgt_map:
        s = build_daily_series(adm, "新入院患者数", group_col="診療科名", group_val=dept)
        if len(s) == 0:
            ma7_map[dept] = None
            continue
        s = add_moving_average(s, 7)
        row = s.loc[s["日付"] == date, "MA7"]
        ma7_map[dept] = round(float(row.iloc[0]), 1) if len(row) > 0 else None

    all_depts = set(nadm_tgt_map.keys()) | set(surg_targets.keys())
    rows = []
    for dept in all_depts:
        nadm_actual = nadm_by_dept.get(dept, 0)
        nadm_tgt = nadm_tgt_map.get(dept)
        nadm_under = max(-(nadm_actual - nadm_tgt) / nadm_tgt * 100, 0) if nadm_tgt and nadm_tgt > 0 else 0
        nadm_gap = (nadm_actual - nadm_tgt) if nadm_tgt else None

        ma7 = ma7_map.get(dept)
        if ma7 is not None and nadm_tgt:
            daily_tgt = nadm_tgt / 7
            ma_dev = max((daily_tgt - ma7) / daily_tgt * 100, 0)
        else:
            ma_dev = 0

        surg_actual = surg_by_dept.get(dept, 0)
        surg_tgt = surg_targets.get(dept)
        surg_under = max(-(surg_actual - surg_tgt) / surg_tgt * 100, 0) if surg_tgt and surg_tgt > 0 else 0

        score = round(nadm_under * 0.5 + ma_dev * 0.3 + surg_under * 0.2, 1)
        rows.append({
            "name": dept, "score": score,
            "kpi": "admission",
            "icon": "🚪",
            "gap": round(float(nadm_gap), 0) if nadm_gap is not None else None,
            "actual": nadm_actual,
            "target": round(float(nadm_tgt), 1) if nadm_tgt else None,
        })

    rows.sort(key=lambda r: -r["score"])
    return rows[:top_n]


# ════════════════════════════════════════
# 看護師版ウォッチランキング ★v2.1 用語変更: 稼働率→利用率
# ════════════════════════════════════════

def build_nurse_watch_ranking(adm, date, targets, top_n=10):
    """要対応病棟ランキング（利用率超過×1.5 + 入退院負荷スコア）"""
    from .config import WARD_NAMES

    inp_by_ward = daily_inpatient(adm, date)["by_ward"]
    nadm_by_ward = daily_new_admission(adm, date)
    beds_map = targets.get("inpatient", {}).get("ward_beds", {})
    inp_tgt_map = targets.get("inpatient", {}).get("ward", {})

    rows = []
    for wcode, inp_val in inp_by_ward.items():
        if wcode in WARD_HIDDEN:
            continue
        beds = beds_map.get(wcode)
        tgt = inp_tgt_map.get(wcode)
        util_rate = round(inp_val / beds * 100, 1) if beds else None
        occ_over = max((util_rate - 95), 0) if util_rate is not None else 0
        ach = achievement_rate(inp_val, tgt)
        load_val = nadm_by_ward["by_ward_load"].get(wcode, 0)
        load_score = min(load_val * 2, 30)
        score = round(occ_over * 1.5 + load_score, 1)

        rows.append({
            "name": WARD_NAMES.get(wcode, wcode),
            "ward_code": wcode,
            "score": score,
            "kpi": "inpatient",
            "icon": "🛏️",
            "gap": round(float(inp_val - tgt), 0) if tgt else None,
            "actual": inp_val,
            "target": round(float(tgt), 1) if tgt else None,
            "util_rate": util_rate,
        })

    rows.sort(key=lambda r: -r["score"])
    return rows[:top_n]


def build_nurse_load_ranking(adm, date, top_n=15):
    """入退院負荷ランキング"""
    from .config import WARD_NAMES
    inp_data = daily_inpatient(adm, date)
    day_data = daily_new_admission(adm, date)
    rows = []
    for wcode in inp_data["by_ward"]:
        if wcode in WARD_HIDDEN:
            continue
        nadm = day_data["by_ward"].get(wcode, 0)
        dis = day_data["by_ward_discharge"].get(wcode, 0)
        load = day_data["by_ward_load"].get(wcode, 0)
        rows.append({
            "ward_code": wcode, "ward_name": WARD_NAMES.get(wcode, wcode),
            "load": load, "nadm": nadm, "discharge": dis,
        })
    rows.sort(key=lambda r: -r["load"])
    return rows[:top_n]


# ════════════════════════════════════════
# KPIサマリー構築 ★v2.1 対応
# ════════════════════════════════════════

def build_kpi_summary(adm: pd.DataFrame, surg: pd.DataFrame,
                      date: pd.Timestamp, targets: dict,
                      surgery_targets: dict) -> dict:
    """
    トップ画面用KPIサマリー（v2.1）

    Returns:
        - portal/detail 両方で使えるフラット辞書
        - headline は config.build_headline() で生成
    """
    inp = daily_inpatient(adm, date)
    nadm = daily_new_admission(adm, date)

    # ── 在院 ──
    inp_target = TARGET_INPATIENT_WEEKDAY if inp["is_weekday"] else TARGET_INPATIENT_HOLIDAY
    series_inp = build_daily_series(adm, "在院患者数")
    series_inp = add_moving_average(series_inp, 7)
    series_inp = add_moving_average(series_inp, 28)
    ma7_inp = series_inp.loc[series_inp["日付"] == date, "MA7"]
    ma7_inp = round(ma7_inp.iloc[0], 1) if len(ma7_inp) > 0 else None
    ma28_inp = series_inp.loc[series_inp["日付"] == date, "MA28"]
    ma28_inp = round(ma28_inp.iloc[0], 1) if len(ma28_inp) > 0 else None
    wow_inp = week_over_week(series_inp, date)

    # 平日/休日フラグを series_inp に結合（日付別に1レコードなので first で取得）
    _daytype_map = adm.groupby("日付")["平日"].first()
    series_inp = series_inp.merge(
        _daytype_map.rename("is_wd").reset_index(),
        on="日付", how="left",
    )

    # 平日/休日別平均を算出するヘルパー
    def _wd_hd_avg(s: pd.DataFrame):
        wd = s[s["is_wd"] == True]
        hd = s[s["is_wd"] == False]
        avg_wd = round(wd["値"].mean(), 1) if len(wd) > 0 else None
        avg_hd = round(hd["値"].mean(), 1) if len(hd) > 0 else None
        return avg_wd, avg_hd

    # 年度平均
    fy_year = date.year if date.month >= 4 else date.year - 1
    fy_start = pd.Timestamp(f"{fy_year}-04-01")
    fy_series = series_inp[(series_inp["日付"] >= fy_start) & (series_inp["日付"] <= date)]
    fy_avg_inp = round(fy_series["値"].mean(), 1) if len(fy_series) > 0 else None
    fy_avg_inp_wd, fy_avg_inp_hd = _wd_hd_avg(fy_series)

    # 前年度（在院）
    prev_fy_start = pd.Timestamp(f"{fy_year - 1}-04-01")
    prev_fy_end = pd.Timestamp(f"{fy_year}-03-31")
    prev_series = series_inp[(series_inp["日付"] >= prev_fy_start) & (series_inp["日付"] <= prev_fy_end)]
    prev_avg_inp = round(prev_series["値"].mean(), 1) if len(prev_series) > 0 else None

    # 前年同期 7日平均・28日平均（在院）
    prev_7d_end_inp = date - timedelta(days=365)
    prev_7d_start_inp = prev_7d_end_inp - timedelta(days=6)
    prev_7d_inp = series_inp[(series_inp["日付"] >= prev_7d_start_inp) & (series_inp["日付"] <= prev_7d_end_inp)]
    prev_avg_7d_inp = round(prev_7d_inp["値"].mean(), 1) if len(prev_7d_inp) > 0 else None

    prev_28d_end_inp = date - timedelta(days=365)
    prev_28d_start_inp = prev_28d_end_inp - timedelta(days=27)
    prev_28d_inp = series_inp[(series_inp["日付"] >= prev_28d_start_inp) & (series_inp["日付"] <= prev_28d_end_inp)]
    prev_avg_28d_inp = round(prev_28d_inp["値"].mean(), 1) if len(prev_28d_inp) > 0 else None

    inpatient_rate = achievement_rate(inp["total"], inp_target)

    # 在院: 直近5週から直近7日を除いた実績値の平均（days 8-35）
    inp_prior_range = series_inp[
        (series_inp["日付"] >= date - timedelta(days=34)) &
        (series_inp["日付"] <= date - timedelta(days=7))
    ]
    inp_prior_range_avg = round(inp_prior_range["値"].mean(), 1) if len(inp_prior_range) > 0 else None

    # 在院: 直近7日・直近4週の平日/休日別平均
    d7_series = series_inp[(series_inp["日付"] >= date - timedelta(days=6)) & (series_inp["日付"] <= date)]
    avg_7d_inp_wd, avg_7d_inp_hd = _wd_hd_avg(d7_series)
    d28_series = series_inp[(series_inp["日付"] >= date - timedelta(days=27)) & (series_inp["日付"] <= date)]
    avg_28d_inp_wd, avg_28d_inp_hd = _wd_hd_avg(d28_series)

    # ── 新入院 ──
    series_nadm = build_daily_series(adm, "新入院患者数")
    rolling7_start = date - timedelta(days=6)
    rolling7 = series_nadm[(series_nadm["日付"] >= rolling7_start) & (series_nadm["日付"] <= date)]
    nadm_7d = int(rolling7["値"].sum())
    nadm_7d_rate = achievement_rate(nadm_7d, TARGET_ADMISSION_WEEKLY)

    # 直近14日累計 → 7日換算（÷2）
    rolling14_nadm = series_nadm[
        (series_nadm["日付"] >= date - timedelta(days=13)) &
        (series_nadm["日付"] <= date)
    ]
    nadm_14d = int(rolling14_nadm["値"].sum())
    nadm_14d_weekly = round(nadm_14d / 2, 1)

    # 直近6週から直近14日を除いた期間の7日換算（days 15-42、÷4）
    nadm_prior_range = series_nadm[
        (series_nadm["日付"] >= date - timedelta(days=41)) &
        (series_nadm["日付"] <= date - timedelta(days=14))
    ]
    nadm_prior_range_weekly = round(int(nadm_prior_range["値"].sum()) / 4, 1) if len(nadm_prior_range) > 0 else None

    # 直近28日累計（新入院）
    rolling28_nadm_start = date - timedelta(days=27)
    rolling28_nadm = series_nadm[(series_nadm["日付"] >= rolling28_nadm_start) & (series_nadm["日付"] <= date)]
    nadm_28d = int(rolling28_nadm["値"].sum())

    fy_nadm = series_nadm[(series_nadm["日付"] >= fy_start) & (series_nadm["日付"] <= date)]
    fy_weeks = max(((date - fy_start).days + 1) / 7, 1)
    fy_avg_nadm = round(fy_nadm["値"].sum() / fy_weeks, 1) if len(fy_nadm) > 0 else None
    fy_rate_nadm = achievement_rate(fy_avg_nadm, TARGET_ADMISSION_WEEKLY)

    # 前年同期 7日/28日合計（新入院）
    prev_nadm_7d_end = date - timedelta(days=365)
    prev_nadm_7d_start = prev_nadm_7d_end - timedelta(days=6)
    prev_nadm_7d_s = series_nadm[(series_nadm["日付"] >= prev_nadm_7d_start) & (series_nadm["日付"] <= prev_nadm_7d_end)]
    prev_nadm_7d_total = int(prev_nadm_7d_s["値"].sum()) if len(prev_nadm_7d_s) > 0 else None

    prev_nadm_28d_end = date - timedelta(days=365)
    prev_nadm_28d_start = prev_nadm_28d_end - timedelta(days=27)
    prev_nadm_28d_s = series_nadm[(series_nadm["日付"] >= prev_nadm_28d_start) & (series_nadm["日付"] <= prev_nadm_28d_end)]
    prev_nadm_28d_total = int(prev_nadm_28d_s["値"].sum()) if len(prev_nadm_28d_s) > 0 else None

    # 前年度週平均（新入院）
    prev_fy_nadm_s = series_nadm[(series_nadm["日付"] >= prev_fy_start) & (series_nadm["日付"] <= prev_fy_end)]
    prev_fy_weeks_nadm = max(((prev_fy_end - prev_fy_start).days + 1) / 7, 1)
    prev_fy_avg_nadm = round(prev_fy_nadm_s["値"].sum() / prev_fy_weeks_nadm, 1) if len(prev_fy_nadm_s) > 0 else None

    cutoff_365 = date - timedelta(days=364)
    series_365 = series_nadm[(series_nadm["日付"] >= cutoff_365) & (series_nadm["日付"] <= date)]
    prev_avg_nadm = round(series_365["値"].sum() / max(len(series_365) / 7, 1), 1) if len(series_365) > 0 else None

    # ── 手術（病院全体=営業平日基準）──
    ga_biz = ga_rolling_biz_avg(surg, date, window=7)
    surg_daily = daily_surgery(surg, date)
    wk_surg = weekly_surgery(surg, date)
    operation_rate = achievement_rate(ga_biz["avg"], TARGET_GA_DAILY)

    # 4週平日平均: 直近5週(35日)から直近7日を除いた期間の平日全麻平均
    op_4w_biz_avg = _ga_biz_avg_in_range(surg, date - timedelta(days=34), date - timedelta(days=7))
    op_4w_prev_avg = _ga_biz_avg_in_range(surg,
                                           date - timedelta(days=34 + 365),
                                           date - timedelta(days=7 + 365))

    # 前年同期 週間合計
    prev_yr_date = date - timedelta(days=365)
    prev_yr_monday = prev_yr_date - timedelta(days=prev_yr_date.weekday())
    prev_wk_ga = surg[(surg["手術実施日"] >= prev_yr_monday) & (surg["手術実施日"] <= prev_yr_date) & surg["全麻"]]
    op_prev_week_total = len(prev_wk_ga)

    # 前年度 FY平日平均（手術）
    op_fy_prev_avg = _ga_biz_avg_in_range(surg, prev_fy_start, prev_fy_end)

    # ── トレンド方向（先週比±5%で判定）──
    def _trend(curr, prev):
        if curr is None or prev is None or prev == 0:
            return {"dir": "→", "label": "→ 横ばい", "css": "mu"}
        pct = (curr - prev) / abs(prev) * 100
        if pct > 5:
            return {"dir": "↑", "label": f"↑先週比+{pct:.0f}%", "css": "ok"}
        if pct < -5:
            return {"dir": "↓", "label": f"↓先週比{pct:.0f}%", "css": "dr"}
        return {"dir": "→", "label": "→ 横ばい", "css": "mu"}

    # 先週7日平均（在院）= days 8-14前
    prev_wk_inp = series_inp[
        (series_inp["日付"] >= date - timedelta(days=13)) &
        (series_inp["日付"] <= date - timedelta(days=7))
    ]
    prev_wk_inp_avg = round(prev_wk_inp["値"].mean(), 1) if len(prev_wk_inp) > 0 else None
    trend_inp = _trend(ma7_inp, prev_wk_inp_avg)

    # 先週7日累計（新入院）
    prev_wk_nadm = series_nadm[
        (series_nadm["日付"] >= date - timedelta(days=13)) &
        (series_nadm["日付"] <= date - timedelta(days=7))
    ]
    prev_wk_nadm_total = int(prev_wk_nadm["値"].sum()) if len(prev_wk_nadm) > 0 else None
    trend_adm = _trend(nadm_7d, prev_wk_nadm_total)

    # 先週7平日平均（手術）
    prev_wk_ga_avg = _ga_biz_avg_in_range(surg, date - timedelta(days=13), date - timedelta(days=7))
    trend_op = _trend(ga_biz["avg"], prev_wk_ga_avg)

    # ── ヘッドライン ──
    hl_input = {
        "inpatient_rate": inpatient_rate,
        "admission_rate": nadm_7d_rate,
        "operation_rate": operation_rate,
        "inpatient_actual": inp["total"],
        "inpatient_target": inp_target,
        "admission_actual_7d": nadm_7d,
        "operation_daily_avg": ga_biz["avg"],
        "trend_inp": trend_inp,
        "trend_adm": trend_adm,
        "trend_op": trend_op,
    }
    headline = build_headline(hl_input)

    return {
        "base_date": date,
        "headline": headline,

        # 在院
        "inpatient_actual": inp["total"],
        "inpatient_target": inp_target,
        "inpatient_target_allday": TARGET_INPATIENT_ALLDAY,
        "inpatient_rate": inpatient_rate,
        "inpatient_avg_7d": ma7_inp,
        "inpatient_avg_28d": ma28_inp,
        "inpatient_fy_avg": fy_avg_inp,
        "inpatient_prev_avg": prev_avg_inp,
        "inpatient_prev_7d_avg": prev_avg_7d_inp,
        "inpatient_prev_28d_avg": prev_avg_28d_inp,
        "inpatient_prior_range_avg": inp_prior_range_avg,  # days 8-35 avg (対照: 直近5週-7日)
        "inpatient_avg_7d_wd": avg_7d_inp_wd,
        "inpatient_avg_7d_hd": avg_7d_inp_hd,
        "inpatient_avg_28d_wd": avg_28d_inp_wd,
        "inpatient_avg_28d_hd": avg_28d_inp_hd,
        "inpatient_fy_avg_wd": fy_avg_inp_wd,
        "inpatient_fy_avg_hd": fy_avg_inp_hd,
        "inpatient_is_weekday": inp["is_weekday"],
        "inpatient_gap": round(inp["total"] - inp_target, 1),
        "inpatient_wow": wow_inp,
        "inpatient_trend": trend_inp,
        "inpatient_status": status_display(inpatient_rate),

        # 新入院
        "admission_actual_7d": nadm_7d,
        "admission_actual_14d_weekly": nadm_14d_weekly,  # 14日÷2 (7日換算)
        "admission_prior_range_weekly": nadm_prior_range_weekly,  # days 15-42の7日換算÷4
        "admission_actual_28d": nadm_28d,
        "admission_target_weekly": TARGET_ADMISSION_WEEKLY,
        "admission_rate_7d": nadm_7d_rate,
        "admission_fy_avg": fy_avg_nadm,
        "admission_fy_rate": fy_rate_nadm,
        "admission_prev_avg": prev_avg_nadm,
        "admission_prev_7d_total": prev_nadm_7d_total,
        "admission_prev_28d_total": prev_nadm_28d_total,
        "admission_prev_fy_avg": prev_fy_avg_nadm,
        "admission_gap": round(nadm_7d - TARGET_ADMISSION_WEEKLY, 1),
        "admission_daily_actual": nadm["total_new"],
        "admission_trend": trend_adm,
        "admission_status": status_display(nadm_7d_rate),

        # 手術（病院全体=営業平日基準）
        "operation_daily_avg": ga_biz["avg"],
        "operation_target": TARGET_GA_DAILY,
        "operation_rate": operation_rate,
        "operation_week_total": wk_surg["total"],
        "operation_fy_avg": ga_biz["fy_biz_avg"],
        "operation_4w_biz_avg": op_4w_biz_avg,
        "operation_gap": round((ga_biz["avg"] or 0) - TARGET_GA_DAILY, 1),
        "operation_prev_4w_avg": op_4w_prev_avg,
        "operation_prev_week_total": op_prev_week_total,
        "operation_fy_prev_avg": op_fy_prev_avg,
        "operation_trend": trend_op,
        "operation_status": status_display(operation_rate),

        # 退院・負荷
        "discharge_total": nadm["total_discharge"],
        "transfer_in": nadm["total_transfer_in"],
        "transfer_out": nadm["total_transfer_out"],
    }
