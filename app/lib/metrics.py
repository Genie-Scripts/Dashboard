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
    DEPT_HIDDEN, WARD_HIDDEN, SURGERY_DISPLAY_DEPTS, SURGERY_EVAL_DEPTS,
    OR_MINUTES_PER_ROOM, OR_ROOM_COUNT,
    TARGET_INPATIENT_WEEKDAY, TARGET_INPATIENT_HOLIDAY, TARGET_INPATIENT_ALLDAY,
    TARGET_ADMISSION_WEEKLY, TARGET_GA_DAILY,
    THRESHOLD_DANGER, THRESHOLD_OK,
    status_label, status_display,
    build_headline,
    is_operational_day,
    operational_days_between,
    fmt_jp_range, fmt_jp_range_prevyear,
)

# 前年同期アラインの既定オフセット日数。
# 364 = 52週ちょうど。曜日を揃えるため週次季節性データ（在院/新入院/全麻/週次合計）の
# 年度比較線に適する（365日だと年ごとに曜日が1〜2日ずれて累積する）。
PREVYEAR_OFFSET_DAYS = 364


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
    is_weekday = is_operational_day(date)
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


def discharge_dow_profile(adm: pd.DataFrame, date: pd.Timestamp,
                          group_col: str = None, group_val: str = None,
                          weeks: int = 8) -> dict:
    """退院の曜日プロファイル（曜日平準化指標）

    分子は「退院患者数」のみ（死亡・転出は除外＝平準化＝曜日付け替えの
    対象となる予定退院に近い母数）。直近 weeks 週ぶんの「完全週(月〜日)」を
    集計対象とし、基準日を含む部分週は除外して曜日カウントの偏りをなくす。

    目標プロファイルは「平日均等(月〜金=各20%)・週末最小(土日=0)」。
    谷=月曜シェアを上げ、山=土曜シェアを下げる方針に対応。

    Returns:
        {
          "counts": [月..日],          # 退院患者数の曜日別合計(死亡除く)
          "shares": [月..日],          # 構成比(%)、合計100
          "target": [20,20,20,20,20,0,0],  # 目標シェア(%)
          "mon_share": float,          # 月曜シェア(谷・引き上げ対象)
          "sat_share": float,          # 土曜シェア(山・引き下げ対象)
          "redistribution": float,     # 再配分率(%) = ½Σ|share-target|
          "per_week": float,           # 週あたり退院総数(ガードレール)
          "weeks": float,              # 実集計週数
        }
    """
    # 目標: 平日均等・週末最小（谷=月を上げ／山=土を下げる方針）
    TARGET = [20.0, 20.0, 20.0, 20.0, 20.0, 0.0, 0.0]

    # 直近 weeks 週ぶんの完全週（月〜日）。基準日を含む部分週は除外。
    monday = date - timedelta(days=date.weekday())
    start = monday - timedelta(days=7 * weeks)
    end = monday - timedelta(days=1)

    # 集計週数は暦（全体データの日付）基準。患者ゼロで行が無い日があっても
    # ユニットによって週数がぶれないようにする。
    cal_days = adm[(adm["日付"] >= start) & (adm["日付"] <= end)]["日付"].nunique()
    n_weeks = round(cal_days / 7, 1) if cal_days else 0.0

    df = adm
    if group_col == "病棟コード":
        df = df[df["病棟_表示"]]
    else:
        df = df[df["科_表示"]]
    if group_col and group_val:
        df = df[df[group_col] == group_val]

    win = df[(df["日付"] >= start) & (df["日付"] <= end)]

    counts = [0] * 7
    if len(win) > 0:
        for wd, v in win.groupby("曜日")["退院患者数"].sum().items():
            counts[int(wd)] = int(v)

    total = sum(counts)

    shares = [c / total * 100 for c in counts] if total > 0 else [0.0] * 7
    redistribution = sum(abs(s - t) for s, t in zip(shares, TARGET)) / 2
    per_week = total / n_weeks if n_weeks else 0.0

    return {
        "counts": counts,
        "shares": [round(s, 1) for s in shares],
        "target": TARGET,
        "mon_share": round(shares[0], 1),
        "sat_share": round(shares[5], 1),
        "redistribution": round(redistribution, 1),
        "per_week": round(per_week, 1),
        "weeks": n_weeks,
    }


def dow_event_profile(adm: pd.DataFrame, date: pd.Timestamp, value_col: str,
                      group_col: str = None, group_val: str = None,
                      weeks: int = 8) -> dict:
    """任意イベント列の曜日プロファイル（退院/入院/予定/緊急/転入/転出 共通）。

    直近 weeks 週の完全週(月〜日)を母数に、曜日別合計・構成比(%)を返す。
    さらに後半 weeks/2 週（=直近）と前半 weeks/2 週に分割した構成比と
    その差分(Δpt = 直近 − 前)も返す（4週Δ＝改善/悪化モード用）。
    discharge_dow_profile と同じ表示フィルタ（科_表示/病棟_表示）に従う。
    """
    monday = date - timedelta(days=date.weekday())
    start = monday - timedelta(days=7 * weeks)
    end = monday - timedelta(days=1)
    half = max(1, weeks // 2)
    mid = monday - timedelta(days=7 * half)   # 直近半 = [mid, end], 前半 = [start, mid)

    df = adm
    if group_col == "病棟コード":
        df = df[df["病棟_表示"]]
    else:
        df = df[df["科_表示"]]
    if group_col and group_val:
        df = df[df[group_col] == group_val]

    win = df[(df["日付"] >= start) & (df["日付"] <= end)]

    def _counts(sub):
        c = [0.0] * 7
        if len(sub) > 0:
            for wd, v in sub.groupby("曜日")[value_col].sum().items():
                c[int(wd)] = float(v)
        return c

    def _shares(c):
        tot = sum(c)
        return [v / tot * 100 for v in c] if tot > 0 else [0.0] * 7

    counts = _counts(win)
    shares_recent = _shares(_counts(win[win["日付"] >= mid]))
    shares_prev = _shares(_counts(win[win["日付"] < mid]))
    shares = _shares(counts)
    delta = [r - p for r, p in zip(shares_recent, shares_prev)]

    cal_days = adm[(adm["日付"] >= start) & (adm["日付"] <= end)]["日付"].nunique()
    n_weeks = round(cal_days / 7, 1) if cal_days else 0.0
    per_week = sum(counts) / n_weeks if n_weeks else 0.0

    return {
        "counts": [round(c, 1) for c in counts],
        "shares": [round(s, 1) for s in shares],
        "shares_recent": [round(s, 1) for s in shares_recent],
        "shares_prev": [round(s, 1) for s in shares_prev],
        "delta": [round(d, 1) for d in delta],
        "per_week": round(per_week, 1),
        "weeks": n_weeks,
    }


def weekend_census_retention(adm: pd.DataFrame, date: pd.Timestamp,
                             entity: str = "ward", weeks: int = 8,
                             min_weekday_avg: float = 5.0) -> dict:
    """週末(土日)の在院ディップをユニット別に算出（平準化アクション層の主指標）。

    平日=月〜金, 週末=土日。直近 weeks 完全週(月〜日)の在院患者数を母数に、
    ユニットごとに 平日平均在院 / 土日平均在院 / 維持率 / のびしろ(人日/週) を返す。
      維持率   = 土日平均在院 ÷ 平日平均在院         （高い=褒める方向。100%=ディップ無）
      のびしろ = (平日平均在院 − 土日平均在院) × 2日  （週末に取り戻せる在院人日/週・0クリップ）
      room_delta_4w = 直近4週 − 前4週 の のびしろ      （正=拡大=悪化 / 負=改善）
    ※在院ディップは実データ上 土日（金曜の在院は平日水準）。金曜の退院ラッシュは
      原因として dow_event_profile（曜日プロファイル）で別途捕捉する＝二窓設計。

    entity: "ward"=病棟コード / "dept"=診療科名。
    min_weekday_avg: 平日平均在院がこの値未満のユニットは除外（小規模ノイズ）。
    返値 units はのびしろ降順（インパクト順）。
    """
    group_col = "病棟コード" if entity == "ward" else "診療科名"
    disp_col = "病棟_表示" if entity == "ward" else "科_表示"

    if len(adm) == 0:
        return {"entity": entity, "weeks": 0.0, "base_date": date, "units": [], "total": {}}

    monday = date - timedelta(days=date.weekday())
    start = monday - timedelta(days=7 * weeks)
    end = monday - timedelta(days=1)
    half = max(1, weeks // 2)
    mid = monday - timedelta(days=7 * half)   # 直近半 = [mid, end], 前半 = [start, mid)

    df = adm[adm[disp_col]]
    win = df[(df["日付"] >= start) & (df["日付"] <= end)]
    cal_days = win["日付"].nunique()
    n_weeks = round(cal_days / 7, 1) if cal_days else 0.0
    if len(win) == 0:
        return {"entity": entity, "weeks": 0.0, "base_date": date, "units": [], "total": {}}

    # 日付×ユニットで在院合計（行重複を排除して各日の在院数にする）
    daily = (win.groupby(["日付", group_col])["在院患者数"].sum().reset_index())
    daily["wd"] = daily["日付"].dt.weekday

    def _stats(sub):
        """平日平均・土日平均・維持率・のびしろ(人日/週)"""
        wk = sub[sub["wd"] <= 4]["在院患者数"]
        we = sub[sub["wd"] >= 5]["在院患者数"]
        wk_avg = float(wk.mean()) if len(wk) else 0.0
        we_avg = float(we.mean()) if len(we) else 0.0
        retention = (we_avg / wk_avg) if wk_avg > 0 else None
        room = max(0.0, wk_avg - we_avg) * 2.0
        return wk_avg, we_avg, retention, room

    recent = daily[daily["日付"] >= mid]
    prev = daily[daily["日付"] < mid]

    # 病棟は表示名に統一（dow_unit_detail/ヒートマップ行クリックが表示名キーのため）
    name_map = {}
    if entity == "ward":
        from .config import WARD_NAMES
        name_map = WARD_NAMES

    units = []
    for name, sub in daily.groupby(group_col):
        wk_avg, we_avg, retention, room = _stats(sub)
        if wk_avg < min_weekday_avg:
            continue
        wk_r, _, _, room_r = _stats(recent[recent[group_col] == name])
        wk_p, _, _, room_p = _stats(prev[prev[group_col] == name])
        units.append({
            "name": name_map.get(name, name),
            "weekday_avg": round(wk_avg, 1),
            "weekend_avg": round(we_avg, 1),
            "retention": round(retention, 3) if retention is not None else None,
            "room_per_week": round(room, 1),
            "room_delta_4w": round(room_r - room_p, 1),
            # 在院サマリ バッジ用: 平日平均在院の 直近4週 − 前4週（増減アイコン・評価色なし）
            "census_delta_4w": round(wk_r - wk_p, 1),
        })

    units.sort(key=lambda u: u["room_per_week"], reverse=True)

    # 全体（エンティティ合計の日次在院から）
    g = daily.groupby("日付")["在院患者数"].sum().reset_index()
    g["wd"] = g["日付"].dt.weekday
    wk_all = float(g[g["wd"] <= 4]["在院患者数"].mean()) if len(g) else 0.0
    we_all = float(g[g["wd"] >= 5]["在院患者数"].mean()) if len(g) else 0.0
    ret_all = round(we_all / wk_all, 3) if wk_all > 0 else None
    # 全体の room_delta_4w も units と同じ定義（直近4週−前4週のびしろ）で算出
    # （病院全体サマリのAI一手で、水準×傾向を語るのに使う）
    _, _, _, room_r_all = _stats(g[g["日付"] >= mid])
    _, _, _, room_p_all = _stats(g[g["日付"] < mid])

    return {
        "entity": entity,
        "weeks": n_weeks,
        "base_date": date,
        "units": units,
        "total": {
            "weekday_avg": round(wk_all, 1),
            "weekend_avg": round(we_all, 1),
            "retention": ret_all,
            "room_per_week": round(sum(u["room_per_week"] for u in units), 1),
            "room_delta_4w": round(room_r_all - room_p_all, 1),
        },
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


def weekly_inpatient_avg(adm: pd.DataFrame, date: pd.Timestamp) -> dict:
    """基準日を含む週(月〜基準日)の在院患者数日平均。A1「今週ここまで」/「先週の確定」用
    （在院はストック指標のため合計でなく平均。date に日曜を渡せば完全週(月〜日)の平均になる）。
    """
    weekday = date.weekday()
    monday = date - timedelta(days=weekday)
    week = adm[(adm["日付"] >= monday) & (adm["日付"] <= date)]
    daily = week.groupby("日付")["在院患者数"].sum()
    return {"monday": monday, "date": date,
            "avg": round(float(daily.mean()), 1) if len(daily) else None,
            "days": len(daily)}


# ── A1: 先週の確定＋今週ここまで（週窓ヘルパー。★P1 訴求力強化） ──

def week_windows(base_date: pd.Timestamp) -> dict:
    """A1: 「先週の確定」と「今週ここまで」の日付境界を返す。

    運用上 base_date の曜日は {日(月曜ビルド), 月, 火, 水, 木} のいずれかしか
    起こらない（月曜ビルド=基準日は日曜、火〜金は前日）。
    base_date が日曜(weekday()==6)のときは、その週(月〜日)が「見る側の月曜朝」
    には既に終わっているため、rolling7 と同一の窓がそのまま「先週の確定」になり、
    「今週ここまで」は存在しない（月曜の朝はまだ今週のデータが無い）。
    それ以外（月〜木）は、直前の完全週が「先週の確定」、当週の月曜〜base_date が
    「今週ここまで」になる。
    """
    base_date = pd.Timestamp(base_date)
    curr_week_monday = base_date - timedelta(days=base_date.weekday())
    if base_date.weekday() == 6:
        last_week_start, last_week_end = curr_week_monday, base_date
        this_week = None
    else:
        last_week_start = curr_week_monday - timedelta(days=7)
        last_week_end = curr_week_monday - timedelta(days=1)
        this_week = {"start": curr_week_monday, "end": base_date}
    return {"last_week": {"start": last_week_start, "end": last_week_end},
            "this_week": this_week}


def partial_week_target(weekly_target, biz_days_elapsed: int):
    """週目標を部分週の営業日数だけの期待値に換算（フロー系＝営業日期待値の規約）。
    A1「今週ここまで」の按分目標（新入院・全麻＝週相当目標で共通利用）。
    """
    return None if weekly_target is None else round(weekly_target / 5 * biz_days_elapsed, 1)


def partial_week_inpatient_target(base_date, monday) -> float:
    """在院（ストック）の部分週按分目標。営業日/非営業日の混在を暦日加重平均する
    （規約どおりストック=暦日）。A1「今週ここまで」の在院按分目標用。
    """
    n_biz = operational_days_between(monday, base_date)
    n_total = (pd.Timestamp(base_date) - pd.Timestamp(monday)).days + 1
    n_non = n_total - n_biz
    return round((n_biz * TARGET_INPATIENT_WEEKDAY + n_non * TARGET_INPATIENT_HOLIDAY) / n_total, 1)


# ── F1: 日付ラベル・完全週ヘルパー（★P0 訴求力強化）
# ★P1統合: 実体は config.fmt_jp_range/fmt_jp_range_prevyear・本関数 week_windows() に
# 寄せ、以下は後方互換のための薄いラッパ（重複定義を残さないため）。 ──

def _fmt_range(start: pd.Timestamp, end: pd.Timestamp) -> str:
    """「8/28〜9/3」形式（同一年の期間ラベル）。★薄いラッパ→config.fmt_jp_range。"""
    return fmt_jp_range(start, end)


def _fmt_range_prevyear(start: pd.Timestamp, end: pd.Timestamp) -> str:
    """「2025/8/29〜9/4」形式（前年同期ラベル・開始日のみ年を付す）。
    ★薄いラッパ→config.fmt_jp_range_prevyear。"""
    return fmt_jp_range_prevyear(start, end)


def _last_complete_week(date: pd.Timestamp) -> tuple:
    """基準日以前で最新の完全週（月〜日）の(月曜, 日曜)を返す。基準日が日曜ならその週。
    ★薄いラッパ→week_windows()の"last_week"（同一ロジックの重複を避ける）。"""
    lw = week_windows(date)["last_week"]
    return lw["start"], lw["end"]


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
    """直近7暦日の手術件数（診療科別=全日基準）★v2.1
    診療科別(by_dept)は術数対象（眼科=全手術、他科=全麻）基準。病院合計(total)は全麻を維持。"""
    start = date - timedelta(days=6)
    window = surg[(surg["手術実施日"] >= start) & (surg["手術実施日"] <= date)]
    total = int(window["全麻"].sum())
    by_dept = (window[window["術数対象"]].groupby("実施診療科").size().to_dict())
    return {"start": start, "date": date, "total": total, "by_dept": by_dept}


def rolling28_surgery_dept(surg: pd.DataFrame, date: pd.Timestamp) -> dict:
    """直近28暦日の手術件数（診療科別=全日基準、過去4週平均）★v2.1 新設
    診療科別(by_dept/avg_by_dept)は術数対象（眼科=全手術、他科=全麻）基準。病院合計(total)は全麻を維持。"""
    start = date - timedelta(days=27)
    window = surg[(surg["手術実施日"] >= start) & (surg["手術実施日"] <= date)]
    total = int(window["全麻"].sum())
    by_dept = (window[window["術数対象"]].groupby("実施診療科").size().to_dict())
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
    """手術日次時系列。
    dept指定時は術数対象（眼科=全手術、他科=全麻）基準を優先（ga_onlyより優先）。
    dept=None（病院全体）は従来通り ga_only（全麻）基準。"""
    df = surg.copy()
    if dept:
        df = df[df["実施診療科"] == dept]
        df = df[df["術数対象"]]
    elif ga_only:
        df = df[df["全麻"]]
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
    全麻の直近window暦日窓・営業日平均（病院全体KPI用）★P1 暦是正版

    ★v2.1〜P1変更点（詳細: spec/暦補正と学習ループ改修プラン.md P1）:
      window の意味を「直近window『営業平日』（tail方式）」から
      「直近window『暦日』の窓 [date-(window-1), date]」へ変更（既定7のまま。
      窓は暦日に固定し曜日構成を均等化＝旧tail方式は7=1.4週で常時2-3曜日が
      2回入る回転不均衡があった）。
        分子 total   = 窓内の営業日に発生した全麻件数の合計。手術行が無い
                       （＝0件の）営業日も0として正しく合算する
                       （旧方式は「データがある営業日」だけをtailしていたため
                       ゼロ件営業日が暗黙に欠落し得た）。
        分母 biz_days = 窓の暦日区間から算出した営業日数（operational_days_between。
                       ゼロ件営業日も正しく分母に入る）。
    fy_biz_avg（年度・営業日tail平均）と build_biz_ma30_series（30営業日移動平均）は
    本改修の対象外で、従来どおり営業日tail方式のまま維持する。
    """
    fy_start_year = date.year if date.month >= 4 else date.year - 1
    fy_start = pd.Timestamp(f"{fy_start_year}-04-01")

    past = surg[surg["手術実施日"] <= date]
    if len(past) == 0:
        return {
            "avg": None, "total": 0, "biz_days": 0,
            "last_biz_date": None, "last_biz_count": None,
            "fy_biz_avg": None,
        }

    daily_ga = (past[past["全麻"]].groupby("手術実施日").size().reset_index(name="件数"))
    daily_ga = daily_ga.sort_values("手術実施日")

    # ── P1: 直近window暦日窓・営業日集計（ゼロ件営業日も分子・分母へ正しく算入）──
    win_start = date - timedelta(days=window - 1)
    full_idx = pd.date_range(win_start, date, freq="D")
    counts_in_win = daily_ga.set_index("手術実施日")["件数"].reindex(full_idx, fill_value=0)
    biz_mask = [is_operational_day(d) for d in full_idx]
    biz_series = counts_in_win[biz_mask]

    total = int(biz_series.sum())
    biz_days = operational_days_between(win_start, date)
    avg = round(total / biz_days, 1) if biz_days > 0 else None

    if len(biz_series) > 0:
        last_biz_date = biz_series.index[-1]
        last_biz_count = int(biz_series.iloc[-1])
    else:
        last_biz_date = None
        last_biz_count = None

    # ── fy_biz_avg（年度・営業日tail平均）は変更しない ──
    biz_rows_all = daily_ga[daily_ga["手術実施日"].apply(is_operational_day)]
    fy_rows = biz_rows_all[biz_rows_all["手術実施日"] >= fy_start]
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

    if surg is None or len(surg) == 0 or "全麻" not in surg.columns:
        return {"dates": [], "values": []}

    offset = timedelta(days=PREVYEAR_OFFSET_DAYS) if prev_year else timedelta(0)
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


def build_prevyear_ma_series(series: pd.DataFrame, base_date: pd.Timestamp,
                             window: int = 28, offset_days: int = PREVYEAR_OFFSET_DAYS,
                             value_col: str = "値") -> dict:
    """
    前年同期の window 日暦日移動平均を当年度日付にアラインして返す。

    在院/新入院/部門別の「昨年度同期」比較線オーバーレイ用。全麻の
    build_biz_ma30_series(prev_year=True)（営業平日基準）の暦日版に相当する。

    base_date - offset_days 以前の前年データで window 日暦日trailing MA を算出し、
    各日付を +offset_days して当年度にアラインして返す。offset_days の既定は
    PREVYEAR_OFFSET_DAYS（=364, 52週）で曜日を揃える。window=28（4週）は
    曜日・週末パターンを平準化する。デフォルト表示時のフロント calcMA(values,28)
    （連続日 trailing 28日平均）と定義が一致する。

    Returns:
        {"dates": [str, ...], "values": [float, ...]}
    """
    if series is None or len(series) == 0:
        return {"dates": [], "values": []}
    s = series[["日付", value_col]].dropna(subset=["日付"]).sort_values("日付")
    if len(s) == 0:
        return {"dates": [], "values": []}

    # 暦日に reindex（歯抜け日は0埋め）して暦日 trailing 移動平均
    full_idx = pd.date_range(s["日付"].min(), s["日付"].max(), freq="D")
    daily = s.set_index("日付")[value_col].reindex(full_idx, fill_value=0)
    ma = daily.rolling(window=window, min_periods=1).mean()

    # 前年同期窓のみ採用 → 日付を +offset_days して当年度へアライン
    offset = timedelta(days=offset_days)
    shifted_base = base_date - offset
    ma = ma[ma.index <= shifted_base]
    if len(ma) == 0:
        return {"dates": [], "values": []}

    dates = [(d + offset).strftime("%Y-%m-%d") for d in ma.index]
    values = [round(float(v), 1) for v in ma.values]
    return {"dates": dates, "values": values}


def build_prevyear_daily_series(series: pd.DataFrame, base_date: pd.Timestamp,
                                offset_days: int = PREVYEAR_OFFSET_DAYS,
                                value_col: str = "値") -> dict:
    """前年同期の日次生データを当年度日付にアラインして返す（昨年度同期線を
    フロント側で当年線と同一の filterByDayType→calcMA で算出するための素データ）。

    build_prevyear_ma_series の事前MA版に対し、こちらは MA を取らない生の日次値を
    返す。フロントが当年線と全く同じ計算経路（日種フィルタ→移動平均）を通すことで、
    昨年度同期線も平日/休日フィルタ・入院種別フィルタに同条件で追随できる。

    暦日に reindex（歯抜け日0埋め）して当年線と同じ連続日前提を満たす。各日付を
    +offset_days して当年度へアライン。is_weekday は「前年実日付」の営業日判定
    （offset=364日=52週で曜日は一致するため、差が出るのは年により移動する休日のみ。
    前年の平日水準 vs 当年の平日水準という同条件比較になる）。

    Returns:
        {"dates": [str, ...], "values": [int, ...], "is_weekday": [bool, ...]}
    """
    from .config import is_operational_day

    empty = {"dates": [], "values": [], "is_weekday": []}
    if series is None or len(series) == 0:
        return empty
    s = series[["日付", value_col]].dropna(subset=["日付"]).sort_values("日付")
    if len(s) == 0:
        return empty

    full_idx = pd.date_range(s["日付"].min(), s["日付"].max(), freq="D")
    daily = s.set_index("日付")[value_col].reindex(full_idx, fill_value=0)

    offset = timedelta(days=offset_days)
    shifted_base = base_date - offset
    daily = daily[daily.index <= shifted_base]
    if len(daily) == 0:
        return empty

    dates = [(d + offset).strftime("%Y-%m-%d") for d in daily.index]
    values = [int(round(float(v))) for v in daily.values]
    is_weekday = [bool(is_operational_day(d)) for d in daily.index]  # 前年実日付基準
    return {"dates": dates, "values": values, "is_weekday": is_weekday}


def build_prevyear_weekly_series(series: pd.DataFrame, base_date: pd.Timestamp,
                                 sum_window: int = 7, smooth_window: int = 28,
                                 offset_days: int = PREVYEAR_OFFSET_DAYS,
                                 value_col: str = "値") -> dict:
    """
    前年同期の sum_window 日ローリング合計（件/週）を smooth_window 日MAで平滑化し、
    当年度日付にアラインして返す。部門別の全麻チャート（週次合計表示）の
    「昨年度同期」比較線用。

    暦日 reindex（歯抜け日0埋め）→ 7日ローリング合計 → 28日MAで平滑化 →
    base_date - offset_days 以前のみ採用 → 各日付を +offset_days して当年度へ。
    フロント renderSurgeryChart の calcRollingSumByDate(.,7) + calcMA(.,28) と
    定義が一致する（min_periods=1）。

    Returns:
        {"dates": [str, ...], "values": [float, ...]}
    """
    if series is None or len(series) == 0:
        return {"dates": [], "values": []}
    s = series[["日付", value_col]].dropna(subset=["日付"]).sort_values("日付")
    if len(s) == 0:
        return {"dates": [], "values": []}

    full_idx = pd.date_range(s["日付"].min(), s["日付"].max(), freq="D")
    daily = s.set_index("日付")[value_col].reindex(full_idx, fill_value=0)
    weekly = daily.rolling(window=sum_window, min_periods=1).sum()
    smooth = weekly.rolling(window=smooth_window, min_periods=1).mean()

    offset = timedelta(days=offset_days)
    shifted_base = base_date - offset
    smooth = smooth[smooth.index <= shifted_base]
    if len(smooth) == 0:
        return {"dates": [], "values": []}

    dates = [(d + offset).strftime("%Y-%m-%d") for d in smooth.index]
    values = [round(float(v), 1) for v in smooth.values]
    return {"dates": dates, "values": values}


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

    # ★F3是正: 新入院ランキングの達成率/statusは営業日期待値で割り引いた目標を使う
    # （全麻ランキングとの非対称=祝日週に新入院だけ不当に未達扱いになる、を解消）。
    # 「目標」列は生の週目標のまま維持（診療科内の相対比較=peer比に使われるため）し、
    # 「目標_adj」「biz_days」「biz_days_full」を新設して並置する。
    from .triage import adjusted_weekly_target  # 遅延import（triage⇄metrics の循環回避）
    biz_days = (operational_days_between(date - timedelta(days=6), date)
               if metric != "inpatient" else None)
    rows = []
    for dept, actual in data.items():
        target = target_map.get(dept)
        if metric == "inpatient":
            rate = achievement_rate(actual, target)
            row = {"診療科": dept, "実績": actual, "目標": target, "達成率": rate}
        else:
            target_adj = adjusted_weekly_target(target, date)
            rate = achievement_rate(actual, target_adj)
            row = {"診療科": dept, "実績": actual, "目標": target, "達成率": rate,
                  "目標_adj": target_adj, "biz_days": biz_days, "biz_days_full": 5}
        st = status_label(rate)
        row["status"] = st
        rows.append(row)

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

    # ★F3是正: 新入院ランキング（病棟軸）も dept 版と同じ営業日期待値の割引を適用する
    # （detail.html の「達成状況」テーブルは軸トグルで同じ表に出るため、dept/ward で
    # 判定基準が食い違わないようにする）。
    from .triage import adjusted_weekly_target  # 遅延import（triage⇄metrics の循環回避）
    biz_days = (operational_days_between(date - timedelta(days=6), date)
               if metric != "inpatient" else None)
    rows = []
    for ward_code, actual in data.items():
        if ward_code in WARD_HIDDEN:
            continue
        target = target_map.get(ward_code)
        beds = beds_map.get(ward_code)
        utilization = round(actual / beds * 100, 1) if beds else None
        row = {
            "病棟コード": ward_code, "病棟名": WARD_NAMES.get(ward_code, ward_code),
            "実績": actual, "目標": target, "病床数": beds, "利用率": utilization,
        }
        if metric == "inpatient":
            rate = achievement_rate(actual, target)
        else:
            target_adj = adjusted_weekly_target(target, date)
            rate = achievement_rate(actual, target_adj)
            row["目標_adj"] = target_adj
            row["biz_days"] = biz_days
            row["biz_days_full"] = 5
        row["達成率"] = rate
        row["status"] = status_label(rate)
        rows.append(row)

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
        eval_fy = fy_data[fy_data["術数対象"]]
        weeks = max(((date - fy_start).days + 1) / 7, 1)
        total_by_dept = eval_fy.groupby("実施診療科").size().to_dict()
        data = {k: round(v / weeks, 1) for k, v in total_by_dept.items()}

    rows = []
    for dept in SURGERY_EVAL_DEPTS:
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
    # ★A7: 年度進捗の按分（新入院/全麻と共有）に使う年度経過営業日数
    fy_biz_days_elapsed = operational_days_between(fy_start, date)
    fy_series = series_inp[(series_inp["日付"] >= fy_start) & (series_inp["日付"] <= date)]
    fy_avg_inp = round(fy_series["値"].mean(), 1) if len(fy_series) > 0 else None
    fy_avg_inp_wd, fy_avg_inp_hd = _wd_hd_avg(fy_series)
    # ★A7: 在院はストック規約どおり暦日平均のまま目標(全日目標)と比較すればよい
    inpatient_fy_rate = achievement_rate(fy_avg_inp, TARGET_INPATIENT_ALLDAY)

    # 前年度（在院）
    prev_fy_start = pd.Timestamp(f"{fy_year - 1}-04-01")
    prev_fy_end = pd.Timestamp(f"{fy_year}-03-31")
    prev_series = series_inp[(series_inp["日付"] >= prev_fy_start) & (series_inp["日付"] <= prev_fy_end)]
    prev_avg_inp = round(prev_series["値"].mean(), 1) if len(prev_series) > 0 else None

    # 前年同期 7日平均・28日平均（在院）
    prev_7d_end_inp = date - timedelta(days=PREVYEAR_OFFSET_DAYS)
    prev_7d_start_inp = prev_7d_end_inp - timedelta(days=6)
    prev_7d_inp = series_inp[(series_inp["日付"] >= prev_7d_start_inp) & (series_inp["日付"] <= prev_7d_end_inp)]
    prev_avg_7d_inp = round(prev_7d_inp["値"].mean(), 1) if len(prev_7d_inp) > 0 else None
    # ★A9是正: 前年比チップの窓をカード本体（平日/休日別）に揃える
    prev_avg_7d_inp_wd, prev_avg_7d_inp_hd = _wd_hd_avg(prev_7d_inp)

    prev_28d_end_inp = date - timedelta(days=PREVYEAR_OFFSET_DAYS)
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
    # ★F3是正: 新入院の週目標にも全麻と同じ営業日期待値の割引（adjusted_weekly_target）
    # を適用する（連休週に「全麻は達成・新入院は未達」の非対称が出ないようにする）。
    # 通常週(biz=5)は恒等短絡のため判定・数値とも変化しない。生の週目標キーは維持し、
    # 隣に _adj と biz_days/biz_days_full（=5）を追加する。
    from .triage import adjusted_weekly_target  # 遅延import（triage⇄metrics の循環回避）
    admission_biz_days = operational_days_between(date - timedelta(days=6), date)
    admission_target_weekly_adj = adjusted_weekly_target(TARGET_ADMISSION_WEEKLY, date)
    nadm_7d_rate = achievement_rate(nadm_7d, admission_target_weekly_adj)

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
    # ★A7是正: 達成率(fy_rate_nadm)は暦日按分(fy_weeks)でなく営業日按分に統一する
    # （規約「フロー=営業日期待値」。fy_avg_nadmそのもの＝週平均実績は表示用にそのまま維持）。
    fy_nadm_sum = int(fy_nadm["値"].sum()) if len(fy_nadm) > 0 else None
    admission_fy_biz_target = round(TARGET_ADMISSION_WEEKLY / 5 * fy_biz_days_elapsed, 1)
    fy_rate_nadm = achievement_rate(fy_nadm_sum, admission_fy_biz_target)

    # 前年同期 7日/28日合計（新入院）
    prev_nadm_7d_end = date - timedelta(days=PREVYEAR_OFFSET_DAYS)
    prev_nadm_7d_start = prev_nadm_7d_end - timedelta(days=6)
    prev_nadm_7d_s = series_nadm[(series_nadm["日付"] >= prev_nadm_7d_start) & (series_nadm["日付"] <= prev_nadm_7d_end)]
    prev_nadm_7d_total = int(prev_nadm_7d_s["値"].sum()) if len(prev_nadm_7d_s) > 0 else None

    prev_nadm_28d_end = date - timedelta(days=PREVYEAR_OFFSET_DAYS)
    prev_nadm_28d_start = prev_nadm_28d_end - timedelta(days=27)
    prev_nadm_28d_s = series_nadm[(series_nadm["日付"] >= prev_nadm_28d_start) & (series_nadm["日付"] <= prev_nadm_28d_end)]
    prev_nadm_28d_total = int(prev_nadm_28d_s["値"].sum()) if len(prev_nadm_28d_s) > 0 else None

    # 前年度週平均（新入院）
    prev_fy_nadm_s = series_nadm[(series_nadm["日付"] >= prev_fy_start) & (series_nadm["日付"] <= prev_fy_end)]
    prev_fy_weeks_nadm = max(((prev_fy_end - prev_fy_start).days + 1) / 7, 1)
    prev_fy_avg_nadm = round(prev_fy_nadm_s["値"].sum() / prev_fy_weeks_nadm, 1) if len(prev_fy_nadm_s) > 0 else None

    cutoff_364 = date - timedelta(days=364)
    series_364 = series_nadm[(series_nadm["日付"] >= cutoff_364) & (series_nadm["日付"] <= date)]
    prev_avg_nadm = round(series_364["値"].sum() / max(len(series_364) / 7, 1), 1) if len(series_364) > 0 else None

    # ── 手術（病院全体=営業平日基準）──
    ga_biz = ga_rolling_biz_avg(surg, date, window=7)
    surg_daily = daily_surgery(surg, date)
    wk_surg = weekly_surgery(surg, date)
    operation_rate = achievement_rate(ga_biz["avg"], TARGET_GA_DAILY)
    # ★A7: fy_biz_avgは元々営業日tail平均どうしの比なので追加按分は不要
    operation_fy_rate = achievement_rate(ga_biz["fy_biz_avg"], TARGET_GA_DAILY)

    # 4週平日平均: 直近5週(35日)から直近7日を除いた期間の平日全麻平均
    op_4w_biz_avg = _ga_biz_avg_in_range(surg, date - timedelta(days=34), date - timedelta(days=7))
    op_4w_prev_avg = _ga_biz_avg_in_range(surg,
                                           date - timedelta(days=34 + PREVYEAR_OFFSET_DAYS),
                                           date - timedelta(days=7 + PREVYEAR_OFFSET_DAYS))

    # 前年同期 週間合計
    prev_yr_date = date - timedelta(days=PREVYEAR_OFFSET_DAYS)
    prev_yr_monday = prev_yr_date - timedelta(days=prev_yr_date.weekday())
    prev_wk_ga = surg[(surg["手術実施日"] >= prev_yr_monday) & (surg["手術実施日"] <= prev_yr_date) & surg["全麻"]]
    op_prev_week_total = len(prev_wk_ga)

    # 前年度 FY平日平均（手術）
    op_fy_prev_avg = _ga_biz_avg_in_range(surg, prev_fy_start, prev_fy_end)

    # ── F1是正: 全麻「直近7日合計」の真の直近7暦日（date-6..date）と、
    # その364日前の同区間（曜日を揃えた前年同期）を別途算出する（★P0 訴求力強化）。
    # 旧 operation_week_total は月〜基準日の部分週のまま「今週ここまで」用に残す。
    r7_surg = rolling7_surgery(surg, date)
    op_7d_total = r7_surg["total"]
    op_7d_range = _fmt_range(r7_surg["start"], date)
    op_7d_prev_date = date - timedelta(days=PREVYEAR_OFFSET_DAYS)
    r7_surg_prev = rolling7_surgery(surg, op_7d_prev_date)
    op_7d_prev_total = r7_surg_prev["total"]
    op_7d_prev_range = _fmt_range_prevyear(r7_surg_prev["start"], op_7d_prev_date)

    # ★A9是正: 全麻YoYチップの窓をカード本体(operation_daily_avg=直近1週営業日平均)に
    # 揃える（従来は4週平日平均とのYoYで窓が違った）。364日前を終端とする7暦日窓の営業日平均。
    operation_prev_7d_biz_avg = _ga_biz_avg_in_range(
        surg, op_7d_prev_date - timedelta(days=6), op_7d_prev_date)

    # 先週の確定（月〜日の直近完全週）。基準日が日曜ならその週。
    last_week_monday, last_week_sunday = _last_complete_week(date)
    last_week_ga = surg[(surg["手術実施日"] >= last_week_monday) &
                        (surg["手術実施日"] <= last_week_sunday) & surg["全麻"]]
    op_last_week_total = len(last_week_ga)
    op_last_week_range = _fmt_range(last_week_monday, last_week_sunday)

    # ── A1: 先週の確定（月〜日）＋今週ここまで（月〜基準日）。
    # 在院・新入院・全麻の3KPI分そろえて算出する（★P1 訴求力強化）。
    # 全麻の先週の確定(op_last_week_total/_range)は上のF1算出をそのまま使う（重複計算しない）。
    # last_week_sunday は常に日曜のため weekly_inpatient_avg/weekly_new_admission に
    # 渡すと「月〜last_week_sunday」がそのまま完全7日週になる。
    last_week_range = _fmt_range(last_week_monday, last_week_sunday)
    inp_last_week = weekly_inpatient_avg(adm, last_week_sunday)
    nadm_last_week = weekly_new_admission(adm, last_week_sunday)
    inpatient_last_week_avg = inp_last_week["avg"]
    admission_last_week_total = nadm_last_week["total"]

    # 今週ここまで（月〜基準日）。裁定3: 営業日1日（例: 火曜）でも按分目標比を出す。
    # 基準日が日曜(=月曜ビュー)のときは「今週」がまだ存在しないため None（裁定どおり伏せる）。
    ww_a1 = week_windows(date)
    this_week = ww_a1["this_week"]
    if this_week is not None:
        tw_start, tw_end = this_week["start"], this_week["end"]
        this_week_range = _fmt_range(tw_start, tw_end)
        this_week_biz_days = operational_days_between(tw_start, tw_end)

        inp_this_week = weekly_inpatient_avg(adm, date)
        inpatient_this_week_avg = inp_this_week["avg"]
        inpatient_this_week_target = partial_week_inpatient_target(date, tw_start)
        inpatient_this_week_rate = achievement_rate(inpatient_this_week_avg, inpatient_this_week_target)

        nadm_this_week = weekly_new_admission(adm, date)
        admission_this_week_total = nadm_this_week["total"]
        admission_this_week_target = partial_week_target(TARGET_ADMISSION_WEEKLY, this_week_biz_days)
        admission_this_week_rate = achievement_rate(admission_this_week_total, admission_this_week_target)

        # 全麻は日次目標(TARGET_GA_DAILY)なので「週相当目標」(日次×5)を渡し、
        # 新入院と同型の按分式(週目標÷5×営業日数=結局 日次目標×営業日数)に揃える。
        operation_this_week_total = wk_surg["total"]  # 月〜基準日累計（=operation_week_totalと同値）
        operation_this_week_target = partial_week_target(TARGET_GA_DAILY * 5, this_week_biz_days)
        operation_this_week_rate = achievement_rate(operation_this_week_total, operation_this_week_target)
    else:
        this_week_range = None
        this_week_biz_days = None
        inpatient_this_week_avg = None
        inpatient_this_week_target = None
        inpatient_this_week_rate = None
        admission_this_week_total = None
        admission_this_week_target = None
        admission_this_week_rate = None
        operation_this_week_total = None
        operation_this_week_target = None
        operation_this_week_rate = None

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
        "fy_biz_days_elapsed": fy_biz_days_elapsed,   # ★A7: 年度経過営業日数（3KPI共有）

        # 在院
        "inpatient_actual": inp["total"],
        "inpatient_target": inp_target,
        "inpatient_target_allday": TARGET_INPATIENT_ALLDAY,
        "inpatient_rate": inpatient_rate,
        "inpatient_avg_7d": ma7_inp,
        "inpatient_avg_28d": ma28_inp,
        "inpatient_fy_avg": fy_avg_inp,
        "inpatient_fy_rate": inpatient_fy_rate,   # ★A7
        "inpatient_prev_avg": prev_avg_inp,
        "inpatient_prev_7d_avg": prev_avg_7d_inp,
        "inpatient_prev_7d_avg_wd": prev_avg_7d_inp_wd,   # ★A9: YoYチップを本体(平日/休日別)に揃える
        "inpatient_prev_7d_avg_hd": prev_avg_7d_inp_hd,
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
        # ★A1: 先週の確定（月〜日・週平均）／今週ここまで（月〜基準日・週平均）
        "inpatient_last_week_avg": inpatient_last_week_avg,
        "inpatient_last_week_range": last_week_range,
        "inpatient_this_week_avg": inpatient_this_week_avg,
        "inpatient_this_week_range": this_week_range,
        "inpatient_this_week_biz_days": this_week_biz_days,
        "inpatient_this_week_target": inpatient_this_week_target,
        "inpatient_this_week_rate": inpatient_this_week_rate,

        # 新入院
        "admission_actual_7d": nadm_7d,
        "admission_actual_14d_weekly": nadm_14d_weekly,  # 14日÷2 (7日換算)
        "admission_prior_range_weekly": nadm_prior_range_weekly,  # days 15-42の7日換算÷4
        "admission_actual_28d": nadm_28d,
        "admission_target_weekly": TARGET_ADMISSION_WEEKLY,   # 生の週目標（既存キー・維持）
        "admission_target_weekly_adj": admission_target_weekly_adj,   # ★F3: 営業日期待値
        "admission_biz_days": admission_biz_days,
        "admission_biz_days_full": 5,
        "admission_rate_7d": nadm_7d_rate,
        "admission_fy_avg": fy_avg_nadm,
        "admission_fy_rate": fy_rate_nadm,   # ★A7: 営業日按分に統一（暦日按分から変更）
        "admission_fy_actual_total": fy_nadm_sum,   # ★A7: 年度累計実績（rateの分子・新規）
        "admission_fy_biz_target": admission_fy_biz_target,   # ★A7: 年度目標(営業日按分・新規)
        "admission_prev_avg": prev_avg_nadm,
        "admission_prev_7d_total": prev_nadm_7d_total,
        "admission_prev_28d_total": prev_nadm_28d_total,
        "admission_prev_fy_avg": prev_fy_avg_nadm,
        "admission_gap": round(nadm_7d - admission_target_weekly_adj, 1),
        "admission_daily_actual": nadm["total_new"],
        "admission_trend": trend_adm,
        "admission_status": status_display(nadm_7d_rate),
        # ★A1: 先週の確定（月〜日・週累計）／今週ここまで（月〜基準日・累計・按分目標比）
        "admission_last_week_total": admission_last_week_total,
        "admission_last_week_range": last_week_range,
        "admission_this_week_total": admission_this_week_total,
        "admission_this_week_range": this_week_range,
        "admission_this_week_biz_days": this_week_biz_days,
        "admission_this_week_target": admission_this_week_target,
        "admission_this_week_rate": admission_this_week_rate,

        # 手術（病院全体=営業平日基準）
        "operation_daily_avg": ga_biz["avg"],
        "operation_target": TARGET_GA_DAILY,
        "operation_rate": operation_rate,
        "operation_fy_rate": operation_fy_rate,   # ★A7
        "operation_week_total": wk_surg["total"],  # 月〜基準日の部分週（「今週ここまで」用に維持）
        "operation_7d_total": op_7d_total,          # ★F1: 真の直近7暦日(date-6..date)合計
        "operation_7d_prev_total": op_7d_prev_total,
        "operation_7d_range": op_7d_range,
        "operation_7d_prev_range": op_7d_prev_range,
        "operation_prev_7d_biz_avg": operation_prev_7d_biz_avg,   # ★A9: YoYチップを本体の窓に揃える
        "operation_last_week_total": op_last_week_total,   # 先週の確定（月〜日）
        "operation_last_week_range": op_last_week_range,
        "operation_fy_avg": ga_biz["fy_biz_avg"],
        "operation_4w_biz_avg": op_4w_biz_avg,
        "operation_gap": round((ga_biz["avg"] or 0) - TARGET_GA_DAILY, 1),
        "operation_prev_4w_avg": op_4w_prev_avg,
        "operation_prev_week_total": op_prev_week_total,
        "operation_fy_prev_avg": op_fy_prev_avg,
        "operation_trend": trend_op,
        "operation_status": status_display(operation_rate),
        # ★A1: 今週ここまで（月〜基準日・累計・按分目標比）。先週の確定はF1のキーを流用。
        "operation_this_week_total": operation_this_week_total,
        "operation_this_week_range": this_week_range,
        "operation_this_week_biz_days": this_week_biz_days,
        "operation_this_week_target": operation_this_week_target,
        "operation_this_week_rate": operation_this_week_rate,

        # 退院・負荷
        "discharge_total": nadm["total_discharge"],
        "transfer_in": nadm["total_transfer_in"],
        "transfer_out": nadm["total_transfer_out"],
    }
