"""
metrics.py — KPI算出エンジン
日次・週次KPI、移動平均、達成率、ランキング構築
"""

import pandas as pd
import numpy as np
from datetime import timedelta
from .config import (
    DEPT_HIDDEN, WARD_HIDDEN, SURGERY_DISPLAY_DEPTS,
    OR_MINUTES_PER_ROOM, OR_ROOM_COUNT,
)


# ────────────────────────────────
# 日次集計
# ────────────────────────────────

def daily_inpatient(adm: pd.DataFrame, date: pd.Timestamp) -> dict:
    """日次在院患者数（全体・診療科別・病棟別）"""
    day = adm[adm["日付"] == date]
    
    total = int(day["在院患者数"].sum())
    
    # 診療科別（表示対象のみ）
    by_dept = (day[day["科_表示"]]
               .groupby("診療科名")["在院患者数"].sum()
               .astype(int).to_dict())
    
    # 病棟別（表示対象のみ）
    by_ward = (day[day["病棟_表示"]]
               .groupby("病棟コード")["在院患者数"].sum()
               .astype(int).to_dict())
    
    is_weekday = date.weekday() < 5
    
    return {
        "date": date,
        "total": total,
        "is_weekday": is_weekday,
        "by_dept": by_dept,
        "by_ward": by_ward,
    }


def daily_new_admission(adm: pd.DataFrame, date: pd.Timestamp) -> dict:
    """日次新入院・緊急入院・退院"""
    day = adm[adm["日付"] == date]
    
    total_new = int(day["新入院患者数"].sum())
    total_emg = int(day["緊急入院患者数"].sum())
    total_discharge = int(day["退院合計"].sum())
    total_transfer_in = int(day["転入患者数"].sum())
    total_transfer_out = int(day["転出患者数"].sum())
    
    by_dept = (day[day["科_表示"]]
               .groupby("診療科名")["新入院患者数"].sum()
               .astype(int).to_dict())
    
    by_ward = (day[day["病棟_表示"]]
               .groupby("病棟コード")["新入院患者数"].sum()
               .astype(int).to_dict())
    
    by_ward_discharge = (day[day["病棟_表示"]]
                         .groupby("病棟コード")["退院合計"].sum()
                         .astype(int).to_dict())
    
    by_ward_load = (day[day["病棟_表示"]]
                    .groupby("病棟コード")["出入り負荷"].sum()
                    .astype(int).to_dict())
    
    return {
        "date": date,
        "total_new": total_new,
        "total_emg": total_emg,
        "total_discharge": total_discharge,
        "total_transfer_in": total_transfer_in,
        "total_transfer_out": total_transfer_out,
        "by_dept": by_dept,
        "by_ward": by_ward,
        "by_ward_discharge": by_ward_discharge,
        "by_ward_load": by_ward_load,
    }


def daily_surgery(surg: pd.DataFrame, date: pd.Timestamp) -> dict:
    """日次手術件数"""
    day = surg[surg["手術実施日"] == date]
    
    total = len(day)
    ga_total = int(day["全麻"].sum())
    
    by_dept = (day[day["科_表示"] & day["全麻"]]
               .groupby("実施診療科").size().to_dict())
    
    return {
        "date": date,
        "total_ops": total,
        "total_ga": ga_total,
        "by_dept": by_dept,
    }


def daily_or_utilization(surg: pd.DataFrame, date: pd.Timestamp) -> float:
    """日次手術室稼働率(%)"""
    day = surg[(surg["手術実施日"] == date) & surg["稼働対象室"] & surg["平日"]]
    if len(day) == 0:
        return 0.0
    total_minutes = day["稼働分"].sum()
    denominator = OR_MINUTES_PER_ROOM * OR_ROOM_COUNT
    return round(total_minutes / denominator * 100, 1)


# ────────────────────────────────
# 週次集計
# ────────────────────────────────

def weekly_new_admission(adm: pd.DataFrame, date: pd.Timestamp) -> dict:
    """基準日を含む週(月〜日)の新入院累計"""
    weekday = date.weekday()
    monday = date - timedelta(days=weekday)
    sunday = monday + timedelta(days=6)
    
    week = adm[(adm["日付"] >= monday) & (adm["日付"] <= min(date, sunday))]
    
    total = int(week["新入院患者数"].sum())
    days_elapsed = (date - monday).days + 1
    
    by_dept = (week[week["科_表示"]]
               .groupby("診療科名")["新入院患者数"].sum()
               .astype(int).to_dict())
    
    by_ward = (week[week["病棟_表示"]]
               .groupby("病棟コード")["新入院患者数"].sum()
               .astype(int).to_dict())
    
    return {
        "monday": monday,
        "date": date,
        "days_elapsed": days_elapsed,
        "total": total,
        "by_dept": by_dept,
        "by_ward": by_ward,
    }


def weekly_surgery(surg: pd.DataFrame, date: pd.Timestamp) -> dict:
    """基準日を含む週の全麻件数累計"""
    weekday = date.weekday()
    monday = date - timedelta(days=weekday)
    
    week = surg[(surg["手術実施日"] >= monday) & (surg["手術実施日"] <= date)]
    ga_week = week[week["全麻"]]
    
    total = len(ga_week)
    weekdays_elapsed = sum(1 for i in range(weekday + 1) if i < 5)
    
    by_dept = (ga_week[ga_week["科_表示"]]
               .groupby("実施診療科").size().to_dict())
    
    return {
        "monday": monday,
        "date": date,
        "weekdays_elapsed": weekdays_elapsed,
        "total": total,
        "by_dept": by_dept,
    }


def rolling7_new_admission(adm: pd.DataFrame, date: pd.Timestamp) -> dict:
    """直近7日（ローリング）の新入院累計・科別集計"""
    start = date - timedelta(days=6)
    window = adm[(adm["日付"] >= start) & (adm["日付"] <= date)]
    total = int(window["新入院患者数"].sum())
    by_dept = (window[window["科_表示"]]
               .groupby("診療科名")["新入院患者数"].sum()
               .astype(int).to_dict())
    by_ward = (window[window["病棟_表示"]]
               .groupby("病棟コード")["新入院患者数"].sum()
               .astype(int).to_dict())
    return {
        "start": start,
        "date": date,
        "total": total,
        "by_dept": by_dept,
        "by_ward": by_ward,
    }


def rolling7_surgery(surg: pd.DataFrame, date: pd.Timestamp) -> dict:
    """直近7日（ローリング）の全麻件数・科別集計"""
    start = date - timedelta(days=6)
    window = surg[(surg["手術実施日"] >= start) & (surg["手術実施日"] <= date)]
    ga_window = window[window["全麻"]]
    total = len(ga_window)
    by_dept = (ga_window[ga_window["科_表示"]]
               .groupby("実施診療科").size().to_dict())
    return {
        "start": start,
        "date": date,
        "total": total,
        "by_dept": by_dept,
    }


# ────────────────────────────────
# 移動平均・トレンドデータ
# ────────────────────────────────

def build_daily_series(adm: pd.DataFrame, col: str = "在院患者数",
                        group_col: str = None, group_val: str = None,
                        display_filter: bool = True) -> pd.DataFrame:
    """日次集計の時系列を構築
    
    Args:
        col: 集計対象列
        group_col: グループ化する列（"診療科名" or "病棟コード" or None=全体）
        group_val: グループ値（特定の科/病棟 or None=全体）
        display_filter: 表示対象のみ
    
    Returns:
        DataFrame with 日付, 値 columns
    """
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
    series = series.sort_values("日付")
    return series


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
    全麻の直近N平日移動平均を算出する。

    Returns dict:
        avg           : 直近N平日の1日あたり平均件数 (float | None)
        total         : 直近N平日の合計件数 (int)
        biz_days      : 実際に集計できた平日数 (int)
        last_biz_date : 最直近の平日 (Timestamp | None)
        last_biz_count: 最直近平日の実績件数 (int | None)
        fy_biz_avg    : 今年度の平日1日あたり平均件数 (float | None)
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

    # 今年度開始日（4月1日）
    fy_start_year = date.year if date.month >= 4 else date.year - 1
    fy_start = pd.Timestamp(f"{fy_start_year}-04-01")

    past = surg[surg["手術実施日"] <= date]
    daily_ga = (past[past["全麻"]]
                .groupby("手術実施日").size()
                .reset_index(name="件数"))
    daily_ga = daily_ga.sort_values("手術実施日")

    # 平日のみ
    biz_rows = daily_ga[daily_ga["手術実施日"].apply(_is_biz)].copy()

    # 直近N平日
    recent = biz_rows.tail(window)
    biz_days = len(recent)
    total = int(recent["件数"].sum()) if biz_days > 0 else 0
    avg = round(total / biz_days, 1) if biz_days > 0 else None

    # 最直近平日
    if biz_days > 0:
        last_row = recent.iloc[-1]
        last_biz_date = last_row["手術実施日"]
        last_biz_count = int(last_row["件数"])
    else:
        last_biz_date = None
        last_biz_count = None

    # 今年度平日平均
    fy_rows = biz_rows[biz_rows["手術実施日"] >= fy_start]
    fy_days = len(fy_rows)
    fy_biz_avg = round(fy_rows["件数"].sum() / fy_days, 1) if fy_days > 0 else None

    return {
        "avg":            avg,
        "total":          total,
        "biz_days":       biz_days,
        "last_biz_date":  last_biz_date,
        "last_biz_count": last_biz_count,
        "fy_biz_avg":     fy_biz_avg,
    }


def build_weekly_agg(series: pd.DataFrame) -> pd.DataFrame:
    """日次→週次集約（月曜始まり）"""
    df = series.copy()
    df["週開始"] = df["日付"] - pd.to_timedelta(
        df["日付"].dt.weekday, unit="D")
    weekly = df.groupby("週開始")["値"].agg(["sum", "mean", "count"]).reset_index()
    weekly.columns = ["週開始", "合計", "平均", "日数"]
    return weekly


# ────────────────────────────────
# 達成率・比較
# ────────────────────────────────

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


# ────────────────────────────────
# ランキング構築
# ────────────────────────────────

def build_dept_ranking(adm: pd.DataFrame, date: pd.Timestamp,
                        targets: dict, metric: str = "inpatient",
                        sort_by: str = "achievement") -> pd.DataFrame:
    """診療科別ランキング
    
    Args:
        metric: "inpatient" or "new_admission"
        sort_by: "achievement" or "actual" or "wow"
    """
    if metric == "inpatient":
        kpi = daily_inpatient(adm, date)
        data = kpi["by_dept"]
        target_map = targets.get("inpatient", {}).get("dept", {})
    else:
        wk = weekly_new_admission(adm, date)
        data = wk["by_dept"]
        target_map = targets.get("new_admission", {}).get("dept", {})
    
    rows = []
    for dept, actual in data.items():
        target = target_map.get(dept)
        rate = achievement_rate(actual, target)
        rows.append({
            "診療科": dept,
            "実績": actual,
            "目標": target,
            "達成率": rate,
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
        wk = weekly_new_admission(adm, date)
        data = wk["by_ward"]
        target_map = targets.get("new_admission", {}).get("ward", {})
        beds_map = targets.get("inpatient", {}).get("ward_beds", {})
    
    rows = []
    for ward_code, actual in data.items():
        if ward_code in WARD_HIDDEN:
            continue
        target = target_map.get(ward_code)
        rate = achievement_rate(actual, target)
        rows.append({
            "病棟コード": ward_code,
            "病棟名": WARD_NAMES.get(ward_code, ward_code),
            "実績": actual,
            "目標": target,
            "病床数": beds_map.get(ward_code),
            "達成率": rate,
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
                           sort_by: str = "achievement") -> pd.DataFrame:
    """診療科別 週全麻ランキング"""
    wk = weekly_surgery(surg, date)
    data = wk["by_dept"]
    
    rows = []
    for dept in SURGERY_DISPLAY_DEPTS:
        actual = data.get(dept, 0)
        target = surgery_targets.get(dept)
        rate = achievement_rate(actual, target)
        rows.append({
            "診療科": dept,
            "実績": actual,
            "週目標": target,
            "達成率": rate,
        })
    
    df = pd.DataFrame(rows)
    if sort_by == "achievement":
        df = df.sort_values("達成率", ascending=False, na_position="last")
    elif sort_by == "actual":
        df = df.sort_values("実績", ascending=False)
    
    df["順位"] = range(1, len(df) + 1)
    return df.reset_index(drop=True)


# ────────────────────────────────
# 医師版 新ランキング（Phase 1）
# ────────────────────────────────

def build_doctor_watch_ranking(adm: pd.DataFrame, surg: pd.DataFrame,
                                date: pd.Timestamp,
                                targets: dict, surg_targets: dict,
                                top_n: int = 10) -> list:
    """
    要注視診療科ランキング。
    スコア = 目標未達度 + 7日移動平均乖離 + 全麻未達度
    
    Returns:
        list of dict: [{department_name, watch_score, status, note}, ...]
    """
    # 新入院・全麻 直近7日ローリング
    r7_nadm = rolling7_new_admission(adm, date)
    nadm_by_dept = r7_nadm["by_dept"]
    nadm_tgt_map = targets.get("new_admission", {}).get("dept", {})

    r7_surg = rolling7_surgery(surg, date)
    surg_by_dept = r7_surg["by_dept"]

    # 7日MA（スコア計算の乖離度用・引き続き使用）
    ma7_map: dict = {}
    for dept in nadm_tgt_map:
        s = build_daily_series(adm, "新入院患者数", group_col="診療科名", group_val=dept)
        if len(s) == 0:
            ma7_map[dept] = None
            continue
        s = add_moving_average(s, 7)
        row = s.loc[s["日付"] == date, "MA7"]
        ma7_map[dept] = round(float(row.iloc[0]), 1) if len(row) > 0 else None

    # 表示除外科（内科系は集計に含めるが要注視ランキングには表示しない）
    RANKING_HIDDEN_DEPTS = {"内科"}
    all_depts = set(nadm_tgt_map.keys()) | set(surg_targets.keys())
    rows = []

    for dept in all_depts:
        if dept in RANKING_HIDDEN_DEPTS:
            continue
        # 新入院 目標未達度
        nadm_actual = nadm_by_dept.get(dept, 0)
        nadm_tgt    = nadm_tgt_map.get(dept)
        if nadm_tgt and nadm_tgt > 0:
            nadm_gap    = nadm_actual - nadm_tgt
            nadm_under  = max(-nadm_gap / nadm_tgt * 100, 0)
        else:
            nadm_gap   = None
            nadm_under = 0

        # 7日MA乖離
        ma7 = ma7_map.get(dept)
        if ma7 is not None and nadm_tgt:
            daily_tgt  = nadm_tgt / 7
            ma_dev     = max((daily_tgt - ma7) / daily_tgt * 100, 0)
        else:
            ma_dev = 0

        # 全麻未達度
        surg_actual = surg_by_dept.get(dept, 0)
        surg_tgt    = surg_targets.get(dept)
        if surg_tgt and surg_tgt > 0:
            surg_gap   = surg_actual - surg_tgt
            surg_under = max(-surg_gap / surg_tgt * 100, 0)
        else:
            surg_gap   = None
            surg_under = 0

        score = round(nadm_under * 0.5 + ma_dev * 0.3 + surg_under * 0.2, 1)

        # ノート
        note_parts = []
        if nadm_gap is not None:
            note_parts.append(f"新入院 {nadm_gap:+.0f}件")
        if surg_gap is not None:
            note_parts.append(f"全麻 {surg_gap:+.0f}件")

        # ステータス
        if score >= 20:
            status = "ng"
        elif score >= 8:
            status = "warn"
        else:
            status = "ok"

        nadm_ach = round(nadm_actual / nadm_tgt * 100, 1) if nadm_tgt else None
        surg_ach  = round(surg_actual / surg_tgt  * 100, 1) if surg_tgt  else None
        rows.append({
            "department_name": dept,
            "watch_score":     score,
            "status":          status,
            "note":            " / ".join(note_parts) if note_parts else "—",
            "nadm_actual":     nadm_actual,
            "nadm_target":     round(float(nadm_tgt), 1) if nadm_tgt else None,
            "nadm_gap":        round(float(nadm_gap), 0) if nadm_gap is not None else None,
            "nadm_achievement":nadm_ach,
            "surg_actual":     surg_actual,
            "surg_target":     round(float(surg_tgt), 1) if surg_tgt else None,
            "surg_gap":        round(float(surg_gap), 0) if surg_gap is not None else None,
            "surg_achievement":surg_ach,
        })

    rows.sort(key=lambda r: -r["watch_score"])
    return rows[:top_n]


def build_doctor_gap_ranking(adm: pd.DataFrame, surg: pd.DataFrame,
                              date: pd.Timestamp,
                              targets: dict, surg_targets: dict) -> dict:
    """
    目標差分ランキング（全麻・新入院）。
    
    Returns:
        {"surgery_gap": [...], "admission_gap": [...]}
    """
    from .config import SURGERY_DISPLAY_DEPTS

    # ─ 全麻 目標差分ランキング（直近7日ローリング）─
    r7_surg      = rolling7_surgery(surg, date)
    surg_by_dept = r7_surg["by_dept"]
    surg_rows    = []
    for dept in SURGERY_DISPLAY_DEPTS:
        actual = surg_by_dept.get(dept, 0)
        tgt    = surg_targets.get(dept)
        gap    = (actual - tgt) if tgt is not None else None
        ach    = round(actual / tgt * 100, 1) if tgt else None
        if tgt is None:
            status = "neutral"
        elif gap >= 0:
            status = "ok"
        elif gap >= -2:
            status = "warn"
        else:
            status = "ng"
        surg_rows.append({
            "department_name": dept,
            "actual":      actual,
            "target":      round(float(tgt), 1) if tgt is not None else None,
            "gap":         round(float(gap), 0) if gap is not None else None,
            "gap_str":     (f"+{gap:.0f}件" if gap >= 0 else f"{gap:.0f}件") if gap is not None else "目標未設定",
            "achievement": ach,
            "status":      status,
        })
    surg_rows.sort(key=lambda r: (r["gap"] is None, r["gap"] or 0))

    # ─ 新入院 目標差分ランキング（直近7日ローリング）─
    r7_nadm      = rolling7_new_admission(adm, date)
    nadm_by_dept = r7_nadm["by_dept"]
    nadm_tgt_map = targets.get("new_admission", {}).get("dept", {})
    NADM_RANK_HIDDEN = {"内科"}
    nadm_rows    = []
    for dept, tgt in nadm_tgt_map.items():
        if dept in NADM_RANK_HIDDEN:
            continue
        actual = nadm_by_dept.get(dept, 0)
        gap    = actual - tgt if tgt is not None else None
        ach    = round(actual / tgt * 100, 1) if tgt else None
        if tgt is None:
            status = "neutral"
        elif gap >= 0:
            status = "ok"
        elif gap >= -2:
            status = "warn"
        else:
            status = "ng"
        nadm_rows.append({
            "department_name": dept,
            "actual":      actual,
            "target":      round(float(tgt), 1) if tgt is not None else None,
            "gap":         round(float(gap), 0) if gap is not None else None,
            "gap_str":     (f"+{gap:.0f}人" if gap >= 0 else f"{gap:.0f}人") if gap is not None else "目標未設定",
            "achievement": ach,
            "status":      status,
        })
    nadm_rows.sort(key=lambda r: (r["gap"] is None, r["gap"] or 0))

    return {"surgery_gap": surg_rows, "admission_gap": nadm_rows}


# ────────────────────────────────
# 看護師版 新ランキング（Phase 1）
# ────────────────────────────────

def build_nurse_watch_ranking(adm: pd.DataFrame, date: pd.Timestamp,
                               targets: dict, top_n: int = 10) -> list:
    """
    要対応病棟ランキング。
    スコア = 稼働率超過度 + 入退院負荷比重
    
    Returns:
        list of dict
    """
    from .config import WARD_NAMES, WARD_HIDDEN

    inp_by_ward   = daily_inpatient(adm, date)["by_ward"]
    nadm_by_ward  = daily_new_admission(adm, date)

    beds_map   = targets.get("inpatient", {}).get("ward_beds", {})
    inp_tgt_map = targets.get("inpatient", {}).get("ward", {})

    rows = []
    for wcode, inp_val in inp_by_ward.items():
        if wcode in WARD_HIDDEN:
            continue
        beds  = beds_map.get(wcode)
        tgt   = inp_tgt_map.get(wcode)

        # 稼働率
        occ_rate = round(inp_val / beds * 100, 1) if beds else None
        occ_over = max((occ_rate - 95), 0) if occ_rate is not None else 0

        # 達成率（在院）
        ach = achievement_rate(inp_val, tgt)

        # 入退院負荷
        load_val = nadm_by_ward["by_ward_load"].get(wcode, 0)
        load_score = min(load_val * 2, 30)

        score = round(occ_over * 1.5 + load_score, 1)

        if score >= 25:
            status = "ng"
        elif score >= 10:
            status = "warn"
        else:
            status = "ok"

        note_parts = []
        if occ_rate is not None:
            note_parts.append(f"稼働率 {occ_rate:.0f}%")
        note_parts.append(f"負荷 {load_val}件")

        rows.append({
            "ward_code":   wcode,
            "ward_name":   WARD_NAMES.get(wcode, wcode),
            "watch_score": score,
            "status":      status,
            "note":        " / ".join(note_parts),
            "occ_rate":    occ_rate,
            "inp_actual":  inp_val,
            "inp_target":  round(float(tgt), 1) if tgt else None,
            "load_val":    load_val,
            "achievement": ach,
        })

    rows.sort(key=lambda r: -r["watch_score"])
    return rows[:top_n]


def build_nurse_load_ranking(adm: pd.DataFrame, date: pd.Timestamp,
                              top_n: int = 15) -> list:
    """
    入退院負荷ランキング。
    負荷 = 新入院 + 転入 + 退院 + 転出
    
    Returns:
        list of dict
    """
    from .config import WARD_NAMES, WARD_HIDDEN

    day_data = daily_new_admission(adm, date)
    inp_data = daily_inpatient(adm, date)

    rows = []
    all_wards = set(inp_data["by_ward"].keys())
    for wcode in all_wards:
        if wcode in WARD_HIDDEN:
            continue
        nadm  = day_data["by_ward"].get(wcode, 0)
        dis   = day_data["by_ward_discharge"].get(wcode, 0)
        load  = day_data["by_ward_load"].get(wcode, 0)

        rows.append({
            "ward_code": wcode,
            "ward_name": WARD_NAMES.get(wcode, wcode),
            "load":      load,
            "nadm":      nadm,
            "discharge": dis,
            "status":    "ng" if load >= 15 else ("warn" if load >= 8 else "ok"),
        })

    rows.sort(key=lambda r: -r["load"])
    return rows[:top_n]


# ────────────────────────────────
# KPIサマリー構築
# ────────────────────────────────

def build_kpi_summary(adm: pd.DataFrame, surg: pd.DataFrame,
                       date: pd.Timestamp, targets: dict,
                       surgery_targets: dict) -> dict:
    """トップ画面用KPIサマリー（v1.4）"""

    inp  = daily_inpatient(adm, date)
    nadm = daily_new_admission(adm, date)
    wk_nadm = weekly_new_admission(adm, date)

    # ── 在院目標（平日/休日切替）──
    if inp["is_weekday"]:
        inp_target = targets.get("inpatient", {}).get("hospital", {}).get("平日", 580)
    else:
        inp_target = targets.get("inpatient", {}).get("hospital", {}).get("休日", 540)

    # 在院 7日MA・前週比
    series_inp = build_daily_series(adm, "在院患者数")
    series_inp = add_moving_average(series_inp, 7)
    ma7_inp = series_inp.loc[series_inp["日付"] == date, "MA7"]
    ma7_inp = round(ma7_inp.iloc[0], 1) if len(ma7_inp) > 0 else None
    wow_inp = week_over_week(series_inp, date)

    # ── 手術 ──
    surg_daily  = daily_surgery(surg, date)
    wk_surg     = weekly_surgery(surg, date)
    ga_biz      = ga_rolling_biz_avg(surg, date, window=7)
    GA_DAILY_TARGET = 21

    # ── 新入院 365日平均（平日・休日別）──
    series_nadm = build_daily_series(adm, "新入院患者数")
    cutoff_365  = date - timedelta(days=364)
    series_365  = series_nadm[(series_nadm["日付"] >= cutoff_365) &
                               (series_nadm["日付"] <= date)].copy()
    series_365["is_weekday"] = series_365["日付"].dt.weekday < 5

    nadm_365_wkday_avg   = None
    nadm_365_holiday_avg = None
    if len(series_365) > 0:
        wkday_vals = series_365[series_365["is_weekday"]]["値"]
        hday_vals  = series_365[~series_365["is_weekday"]]["値"]
        nadm_365_wkday_avg   = round(wkday_vals.mean(), 1) if len(wkday_vals) > 0 else None
        nadm_365_holiday_avg = round(hday_vals.mean(),  1) if len(hday_vals)  > 0 else None

    # ── 直近7日累計（rolling）──
    rolling7_start       = date - timedelta(days=6)
    rolling7             = series_nadm[(series_nadm["日付"] >= rolling7_start) &
                                        (series_nadm["日付"] <= date)]
    nadm_rolling7_total  = int(rolling7["値"].sum())
    nadm_365_weekly_avg  = (round(series_365["値"].sum() / (len(series_365) / 7), 1)
                            if len(series_365) > 0 else None)
    NADM_ROLLING7_TARGET = targets.get("new_admission", {}).get("hospital", {}).get("全日", 385)
    nadm_rolling7_progress = achievement_rate(nadm_rolling7_total, NADM_ROLLING7_TARGET)
    nadm_rolling7_vs_365w  = (round(nadm_rolling7_total - nadm_365_weekly_avg, 1)
                               if nadm_365_weekly_avg is not None else None)

    # ── 昨日1日目標（平日80・休日40）──
    NADM_DAILY_TARGET_WKDAY   = 80
    NADM_DAILY_TARGET_HOLIDAY = 40
    nadm_daily_target   = NADM_DAILY_TARGET_WKDAY if inp["is_weekday"] else NADM_DAILY_TARGET_HOLIDAY
    nadm_daily_value    = nadm["total_new"]
    nadm_daily_progress = achievement_rate(nadm_daily_value, nadm_daily_target)
    nadm_daily_365_avg  = nadm_365_wkday_avg if inp["is_weekday"] else nadm_365_holiday_avg
    nadm_daily_vs_365   = (round(nadm_daily_value - nadm_daily_365_avg, 1)
                            if nadm_daily_365_avg is not None else None)

    # 後方互換
    nadm_wk_target   = NADM_ROLLING7_TARGET
    nadm_wk_progress = nadm_rolling7_progress

    return {
        "base_date": date,
        "inpatient": {
            "value":       inp["total"],
            "target":      inp_target,
            "achievement": achievement_rate(inp["total"], inp_target),
            "ma7":         ma7_inp,
            "wow":         wow_inp,
        },
        "new_admission": {
            # 後方互換
            "value":           nadm_daily_value,
            "emergency":       nadm["total_emg"],
            "weekly_total":    nadm_rolling7_total,
            "weekly_target":   nadm_wk_target,
            "weekly_progress": nadm_wk_progress,
            # 直近7日累計カード
            "rolling7_total":       nadm_rolling7_total,
            "rolling7_target":      NADM_ROLLING7_TARGET,
            "rolling7_progress":    nadm_rolling7_progress,
            "rolling7_vs_365w":     nadm_rolling7_vs_365w,
            "nadm_365_weekly_avg":  nadm_365_weekly_avg,
            # 昨日1日カード
            "daily_value":        nadm_daily_value,
            "daily_target":       nadm_daily_target,
            "daily_progress":     nadm_daily_progress,
            "daily_vs_365":       nadm_daily_vs_365,
            "daily_365_avg":      nadm_daily_365_avg,
            "daily_is_weekday":   inp["is_weekday"],
        },
        "surgery": {
            "ga_rolling_avg":    ga_biz["avg"],
            "ga_rolling_total":  ga_biz["total"],
            "ga_rolling_days":   ga_biz["biz_days"],
            "ga_last_biz_count": ga_biz["last_biz_count"],
            "ga_last_biz_date":  ga_biz["last_biz_date"],
            "ga_fy_biz_avg":     ga_biz["fy_biz_avg"],
            "ga_count":          surg_daily["total_ga"],
            "ga_target":         GA_DAILY_TARGET,
            "ga_achievement":    achievement_rate(ga_biz["avg"], GA_DAILY_TARGET),
            "weekly_ga":         wk_surg["total"],
            "total_ops":         surg_daily["total_ops"],
        },
        "discharge": {
            "value":        nadm["total_discharge"],
            "transfer_in":  nadm["total_transfer_in"],
            "transfer_out": nadm["total_transfer_out"],
        },
        "or_utilization": daily_or_utilization(surg, date),
    }
