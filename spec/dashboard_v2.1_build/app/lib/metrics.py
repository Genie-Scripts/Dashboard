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
    by_ward = (day[day["病棟_表示"]].groupby("病棟コード")["新入院患者数"].sum().astype(int).to_dict())
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
    by_ward = (week[week["病棟_表示"]].groupby("病棟コード")["新入院患者数"].sum().astype(int).to_dict())
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


def rolling7_new_admission(adm: pd.DataFrame, date: pd.Timestamp) -> dict:
    """直近7暦日の新入院累計"""
    start = date - timedelta(days=6)
    window = adm[(adm["日付"] >= start) & (adm["日付"] <= date)]
    total = int(window["新入院患者数"].sum())
    by_dept = (window[window["科_表示"]].groupby("診療科名")["新入院患者数"].sum().astype(int).to_dict())
    by_ward = (window[window["病棟_表示"]].groupby("病棟コード")["新入院患者数"].sum().astype(int).to_dict())
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
        kpi = daily_inpatient(adm, date)
        data = kpi["by_dept"]
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
        kpi = daily_inpatient(adm, date)
        data = kpi["by_ward"]
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

    # 年度平均
    fy_year = date.year if date.month >= 4 else date.year - 1
    fy_start = pd.Timestamp(f"{fy_year}-04-01")
    fy_series = series_inp[(series_inp["日付"] >= fy_start) & (series_inp["日付"] <= date)]
    fy_avg_inp = round(fy_series["値"].mean(), 1) if len(fy_series) > 0 else None

    # 前年度平均
    prev_fy_start = pd.Timestamp(f"{fy_year - 1}-04-01")
    prev_fy_end = pd.Timestamp(f"{fy_year}-03-31")
    prev_series = series_inp[(series_inp["日付"] >= prev_fy_start) & (series_inp["日付"] <= prev_fy_end)]
    prev_avg_inp = round(prev_series["値"].mean(), 1) if len(prev_series) > 0 else None

    inpatient_rate = achievement_rate(inp["total"], TARGET_INPATIENT_ALLDAY)

    # ── 新入院 ──
    series_nadm = build_daily_series(adm, "新入院患者数")
    rolling7_start = date - timedelta(days=6)
    rolling7 = series_nadm[(series_nadm["日付"] >= rolling7_start) & (series_nadm["日付"] <= date)]
    nadm_7d = int(rolling7["値"].sum())
    nadm_7d_rate = achievement_rate(nadm_7d, TARGET_ADMISSION_WEEKLY)

    fy_nadm = series_nadm[(series_nadm["日付"] >= fy_start) & (series_nadm["日付"] <= date)]
    fy_weeks = max(((date - fy_start).days + 1) / 7, 1)
    fy_avg_nadm = round(fy_nadm["値"].sum() / fy_weeks, 1) if len(fy_nadm) > 0 else None
    fy_rate_nadm = achievement_rate(fy_avg_nadm, TARGET_ADMISSION_WEEKLY)

    cutoff_365 = date - timedelta(days=364)
    series_365 = series_nadm[(series_nadm["日付"] >= cutoff_365) & (series_nadm["日付"] <= date)]
    prev_avg_nadm = round(series_365["値"].sum() / max(len(series_365) / 7, 1), 1) if len(series_365) > 0 else None

    # ── 手術（病院全体=営業平日基準）──
    ga_biz = ga_rolling_biz_avg(surg, date, window=7)
    surg_daily = daily_surgery(surg, date)
    wk_surg = weekly_surgery(surg, date)
    operation_rate = achievement_rate(ga_biz["avg"], TARGET_GA_DAILY)

    # OR稼働率
    or_util = daily_or_utilization(surg, date)

    # ── ヘッドライン ──
    hl_input = {
        "inpatient_rate": inpatient_rate,
        "admission_rate": nadm_7d_rate,
        "operation_rate": operation_rate,
        "inpatient_actual": inp["total"],
        "admission_actual_7d": nadm_7d,
        "operation_daily_avg": ga_biz["avg"],
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
        "inpatient_gap": round(inp["total"] - TARGET_INPATIENT_ALLDAY, 1),
        "inpatient_wow": wow_inp,
        "inpatient_status": status_display(inpatient_rate),

        # 新入院
        "admission_actual_7d": nadm_7d,
        "admission_target_weekly": TARGET_ADMISSION_WEEKLY,
        "admission_rate_7d": nadm_7d_rate,
        "admission_fy_avg": fy_avg_nadm,
        "admission_fy_rate": fy_rate_nadm,
        "admission_prev_avg": prev_avg_nadm,
        "admission_gap": round(nadm_7d - TARGET_ADMISSION_WEEKLY, 1),
        "admission_daily_actual": nadm["total_new"],
        "admission_status": status_display(nadm_7d_rate),

        # 手術（病院全体=営業平日基準）
        "operation_daily_avg": ga_biz["avg"],
        "operation_target": TARGET_GA_DAILY,
        "operation_rate": operation_rate,
        "operation_week_total": wk_surg["total"],
        "operation_fy_avg": ga_biz["fy_biz_avg"],
        "operation_gap": round((ga_biz["avg"] or 0) - TARGET_GA_DAILY, 1),
        "operation_in_hours_rate": or_util,
        "operation_status": status_display(operation_rate),

        # 退院・負荷
        "discharge_total": nadm["total_discharge"],
        "transfer_in": nadm["total_transfer_in"],
        "transfer_out": nadm["total_transfer_out"],
    }
