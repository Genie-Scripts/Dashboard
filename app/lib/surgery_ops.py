"""
surgery_ops.py — 手術分析（Track S: S1〜S7）

detail.html「手術分析」タブ用の集計＋Plotly trace生成を1モジュールに同居させる
（profit_surgery.py / weekly_story.py / month_projection.py と同形式）。

対象:
    S1 時間外手術比率   … 月次線（24ヶ月） + 科別横棒（24週）
    S2 入替時間         … 室別 中央値横棒 + p75点（24週）
    S3 科別キャパ占有シェア … 積上げ横棒1本（12週）
    S4 緊急+臨時の時間帯×曜日 … heatmap（52週・全室・全曜日）
    S5 週間ORタイムライン … 日別ガント（直前の完全な1週）
    S6 科別予実比        … 科別 中央値横棒 + p75点（24週）
    S7 割り込み率+構成   … 割り込み率線 + 申込区分/入外区分 構成（24ヶ月）

共通の母集団は「平日×稼働対象室」（_CORE）。S4/S5/S7構成のみ全室・全曜日を使う
（個々の関数のdocstringに明記）。所要分は preprocess.py の「稼働分」（8:45-17:15
クリップ済み）を再利用せず、本モジュール内で入室〜退室の生の差分から算出する
（クリップ値では時間外が構造的に0になってしまうため）。

科別カットの最小n=30件。閾値未満の科は excluded に落とし、chart には出さない
（眼科が稼働対象室をほとんど使わない＝外来手術センター中心、という実態が
 自然にこの閾値で表現される。config側の科集合による絞り込みは行わない）。
"""
from __future__ import annotations

import pandas as pd

from .charts import _base_layout, _DHM_LOAD
from .config import (
    OR_ROOMS_ACTIVE, OR_MINUTES_PER_ROOM, OR_ROOM_COUNT,
    OR_START_HOUR, OR_START_MIN, OR_END_HOUR, OR_END_MIN,
    operational_days_between,
)

# 母集団ヘルパー（平日×稼働対象室）。preprocess.py の「稼働分」はクリップ済みで
# 別物のため使わず、時刻は本モジュール内のヘルパーで生のまま扱う。
_CORE = lambda s: s[s["稼働対象室"] & s["平日"]]  # noqa: E731

# 稼働対象室（OP-1〜10, 12 の11室）を番号順に並べたリスト（S2/S5の行順）。
_OR_ROOMS_SORTED = sorted(OR_ROOMS_ACTIVE, key=lambda r: int(r.split("-")[1]))

# 緊急・臨時の申込区分（dept_report.py:791 の isin(["緊急","臨時"]) と一致させる）。
_URGENT_KINDS = ["緊急", "臨時"]

# 時間外判定の閾値（分）。17:15 ちょうどは含まない（> のみ）。
_OVERTIME_THRESHOLD_MIN = OR_END_HOUR * 60 + OR_END_MIN

# 科別カットの最小サンプル数。
_MIN_DEPT_N = 30

# 診療科 → 色（固定）。手術ダッシュボード表示対象12科 + その他（フォールバック）。
DEPT_COLORS = {
    "整形外科": "#0072B2",
    "泌尿器科": "#E69F00",
    "産婦人科": "#009E73",
    "一般消化器外科": "#D55E00",
    "耳鼻咽喉科": "#56B4E9",
    "皮膚科": "#CC79A7",
    "形成外科": "#8C7AA9",
    "乳腺外科": "#B07AA1",
    "歯科口腔外科": "#7F9EB2",
    "脳神経外科": "#A0785A",
    "呼吸器外科": "#6B8E23",
    "心臓血管外科": "#9E2A2B",
    "その他": "#a6b3c4",
}


def _dept_color(dept: str) -> str:
    return DEPT_COLORS.get(dept, DEPT_COLORS["その他"])


def _min_of_day(series: pd.Series) -> pd.Series:
    """"HH:MM" 形式の時刻文字列を 0:00 起点の分（float）に変換する。パース不能はNaN。"""
    def _parse(v):
        if pd.isna(v):
            return float("nan")
        s = str(v).strip()
        parts = s.split(":")
        if len(parts) < 2:
            return float("nan")
        try:
            h, m = int(parts[0]), int(parts[1])
        except ValueError:
            return float("nan")
        return float(h * 60 + m)
    return series.apply(_parse)


def _duration_min(df: pd.DataFrame) -> pd.Series:
    """退室時刻 − 入室時刻（分）。日をまたぐ場合（負値）は +1440 で補正する。"""
    enter = _min_of_day(df["入室時刻"])
    leave = _min_of_day(df["退室時刻"])
    dur = leave - enter
    return dur.mask(dur < 0, dur + 1440)


def _window(df: pd.DataFrame, base_date, weeks: int = None, months: int = None) -> pd.DataFrame:
    """base_date を右端（含む）とする期間で 手術実施日 を絞り込む。

    weeks 指定時は (base_date - 7*weeks日, base_date]、
    months 指定時は (base_date - Nヶ月, base_date] で切り出す。
    """
    base_date = pd.Timestamp(base_date).normalize()
    if weeks is not None:
        start = base_date - pd.Timedelta(weeks=weeks)
    elif months is not None:
        start = base_date - pd.DateOffset(months=months)
    else:
        raise ValueError("_window: weeks か months のいずれかを指定してください")
    return df[(df["手術実施日"] > start) & (df["手術実施日"] <= base_date)]


def _dept_groups(df: pd.DataFrame, value_col: str):
    """実施診療科でグルーピングし、n<30 の科を excluded に落とす共通処理。
    実際の集約（mean/median/sum等）は呼び出し側が series に対して行う。

    Returns: (kept: list[(dept, n, series)], excluded: list[str])
    """
    kept, excluded = [], []
    if len(df) == 0:
        return kept, excluded
    for dept, s in df.groupby("実施診療科")[value_col]:
        n = int(s.shape[0])
        if n < _MIN_DEPT_N:
            excluded.append(str(dept))
            continue
        kept.append((str(dept), n, s))
    return kept, sorted(excluded)


# ════════════════════════════════════════
# S1: 時間外手術比率
# ════════════════════════════════════════

def overtime_ratio(surg: pd.DataFrame, base_date: pd.Timestamp) -> dict:
    """時間外手術比率。母集団は平日×稼働対象室（_CORE）。

    退室時刻は日跨ぎ（退室<入室）を +1440 補正した「実効退室分」で 17:15 と比較する
    （17:15 ちょうどは時間外に含めない＝ > のみ）。

    Returns: {"s1": {月次線チャート, latest_rate}, "s1b": {科別横棒チャート（24週）}}
    """
    core = _CORE(surg)

    # ── s1: 月次線（直近24ヶ月） ──
    win_m = _window(core, base_date, months=24)
    n_m = 0
    months, rates = [], []
    if len(win_m) > 0:
        w = win_m.copy()
        enter = _min_of_day(w["入室時刻"])
        dur = _duration_min(w)
        valid = enter.notna() & dur.notna()
        n_m = int(valid.sum())
        if n_m > 0:
            w = w.loc[valid]
            eff_leave = enter[valid] + dur[valid]
            tmp = pd.DataFrame({
                "month": w["手術実施日"].dt.to_period("M").astype(str).to_numpy(),
                "overtime": (eff_leave > _OVERTIME_THRESHOLD_MIN).to_numpy(),
            })
            monthly = tmp.groupby("month")["overtime"].mean().sort_index()
            months = list(monthly.index)
            rates = [round(float(v) * 100, 1) for v in monthly]

    trace_line = {
        "name": "時間外手術比率", "x": months, "y": rates,
        "type": "scatter", "mode": "lines",
        "line": {"color": "#0072B2", "width": 3},
    }
    layout_s1 = _base_layout("")
    layout_s1["xaxis"] = {"type": "category", "gridcolor": "#DCE1E9"}
    layout_s1["yaxis"]["ticksuffix"] = "%"

    # ── s1b: 直近24週・科別横棒（降順） + latest_rate ──
    win_w = _window(core, base_date, weeks=24)
    n_w = 0
    latest_rate = None
    dept_rows, excluded = [], []
    if len(win_w) > 0:
        w = win_w.copy()
        enter = _min_of_day(w["入室時刻"])
        dur = _duration_min(w)
        valid = enter.notna() & dur.notna()
        n_w = int(valid.sum())
        if n_w > 0:
            w = w.loc[valid].copy()
            w["_overtime"] = (enter[valid] + dur[valid] > _OVERTIME_THRESHOLD_MIN)
            latest_rate = round(float(w["_overtime"].mean()) * 100, 1)
            kept, excluded = _dept_groups(w, "_overtime")
            dept_rows = [{"dept": d, "rate": round(float(s.mean()) * 100, 1), "n": n} for d, n, s in kept]
            dept_rows.sort(key=lambda r: -r["rate"])

    bar_depts = [d["dept"] for d in dept_rows]
    bar_vals = [d["rate"] for d in dept_rows]
    trace_bar = {
        "x": bar_vals, "y": bar_depts, "type": "bar", "orientation": "h",
        "marker": {"color": [_dept_color(d) for d in bar_depts]},
        "hovertemplate": "%{y}: %{x}%<extra></extra>",
    }
    layout_s1b = _base_layout("", height=max(220, 26 * len(bar_depts) + 92))
    layout_s1b["yaxis"]["autorange"] = "reversed"
    layout_s1b["xaxis"] = {"gridcolor": "#DCE1E9", "ticksuffix": "%"}

    return {
        "s1": {
            "chart": {"traces": [trace_line], "layout": layout_s1, "config": {"responsive": True}},
            "caption": (
                "17時15分より後に退室した手術の割合です。直近24週は"
                f"{latest_rate if latest_rate is not None else '—'}%でした。"
            ),
            "excluded": [],
            "n": n_m,
            "latest_rate": latest_rate,
        },
        "s1b": {
            "chart": {"traces": [trace_bar], "layout": layout_s1b, "config": {"responsive": True}},
            "caption": "診療科別の時間外手術比率です（直近24週）。",
            "excluded": excluded,
            "n": n_w,
            "depts": dept_rows,
        },
    }


# ════════════════════════════════════════
# S2: 入替時間
# ════════════════════════════════════════

def turnover_minutes(surg: pd.DataFrame, base_date: pd.Timestamp) -> dict:
    """入替時間（室別）。母集団は平日×稼働対象室（_CORE）・直近24週。

    同一(手術実施日,手術室)内を入室分順に並べ「次入室−前退室」を計算し、
    0〜180分のみ採用する（負値＝データ不整合、181分以上＝空き時間とみなし除外）。
    """
    win = _window(_CORE(surg), base_date, weeks=24)
    gaps_by_room: dict = {r: [] for r in _OR_ROOMS_SORTED}
    if len(win) > 0:
        w = win.copy()
        w["_enter"] = _min_of_day(w["入室時刻"])
        w["_leave"] = _min_of_day(w["退室時刻"])
        w = w[w["_enter"].notna() & w["_leave"].notna()]
        for (_day, room), g in w.groupby(["手術実施日", "手術室"]):
            if room not in gaps_by_room:
                continue
            g = g.sort_values("_enter")
            enters = g["_enter"].tolist()
            leaves = g["_leave"].tolist()
            for i in range(1, len(g)):
                gap = enters[i] - leaves[i - 1]
                if 0 <= gap <= 180:
                    gaps_by_room[room].append(float(gap))

    rooms_info = []
    all_gaps: list = []
    for room in _OR_ROOMS_SORTED:
        vals = gaps_by_room[room]
        all_gaps.extend(vals)
        if vals:
            s = pd.Series(vals)
            rooms_info.append({
                "room": room, "median": round(float(s.median()), 1),
                "p75": round(float(s.quantile(0.75)), 1), "n": len(vals),
            })
        else:
            rooms_info.append({"room": room, "median": None, "p75": None, "n": 0})

    n_total = len(all_gaps)
    overall_median = round(float(pd.Series(all_gaps).median()), 1) if all_gaps else None

    rooms = [r["room"] for r in rooms_info]
    trace_bar = {
        "name": "中央値", "x": [r["median"] for r in rooms_info], "y": rooms,
        "type": "bar", "orientation": "h", "marker": {"color": "#0072B2"},
        "hovertemplate": "%{y}: 中央値%{x}分<extra></extra>",
    }
    trace_p75 = {
        "name": "p75", "x": [r["p75"] for r in rooms_info], "y": rooms,
        "type": "scatter", "mode": "markers",
        "marker": {"color": "#D55E00", "symbol": "diamond", "size": 9},
        "hovertemplate": "%{y}: p75 %{x}分<extra></extra>",
    }
    layout = _base_layout("", height=max(220, 26 * len(rooms) + 92))
    layout["yaxis"]["autorange"] = "reversed"
    layout["xaxis"] = {"gridcolor": "#DCE1E9", "title": {"text": "分", "font": {"size": 10}}}
    if overall_median is not None:
        layout["shapes"] = [{
            "type": "line", "xref": "x", "yref": "paper",
            "x0": overall_median, "x1": overall_median, "y0": 0, "y1": 1,
            "line": {"color": "#9daab8", "width": 1.5, "dash": "dot"},
        }]

    med_str = overall_median if overall_median is not None else "—"
    return {
        "chart": {"traces": [trace_bar, trace_p75], "layout": layout, "config": {"responsive": True}},
        "caption": (
            "同じ部屋で前の手術が退室してから次が入室するまでの時間です"
            f"（0〜180分の間に限って集計）。全体の中央値は{med_str}分です。"
        ),
        "excluded": [],
        "n": n_total,
        "rooms": rooms_info,
    }


# ════════════════════════════════════════
# S3: 科別キャパ占有シェア
# ════════════════════════════════════════

def capacity_share(surg: pd.DataFrame, base_date: pd.Timestamp) -> dict:
    """科別キャパ占有シェア。母集団は平日×稼働対象室（_CORE）・直近12週。

    分子=科別所要分合計、分母=主要手術室数×510分×窓内営業日数
    （operational_days_between。祝日を含む窓では分母が減る＝データ実在に依らず
    暦から機械的に決まる）。
    """
    base_date = pd.Timestamp(base_date).normalize()
    win_start = (base_date - pd.Timedelta(weeks=12) + pd.Timedelta(days=1)).normalize()
    biz_days = operational_days_between(win_start, base_date)
    denom = OR_MINUTES_PER_ROOM * OR_ROOM_COUNT * biz_days

    win = _window(_CORE(surg), base_date, weeks=12)
    n_valid = 0
    dept_rows, excluded = [], []
    if len(win) > 0:
        w = win.copy()
        w["_dur"] = _duration_min(w)
        w = w[w["_dur"].notna()]
        n_valid = int(len(w))
        kept, excluded = _dept_groups(w, "_dur")
        for dept, n, s in kept:
            minutes = float(s.sum())
            pct = round(minutes / denom * 100, 1) if denom else 0.0
            dept_rows.append({"dept": dept, "minutes": round(minutes, 1), "pct": pct, "n": n})
        dept_rows.sort(key=lambda r: -r["minutes"])

    traces = [{
        "name": d["dept"], "x": [d["pct"]], "y": [""], "type": "bar", "orientation": "h",
        "marker": {"color": _dept_color(d["dept"])},
        "hovertemplate": f"{d['dept']}: %{{x}}%<extra></extra>",
    } for d in dept_rows]
    used_pct = sum(d["pct"] for d in dept_rows)
    vacant_pct = round(max(0.0, 100.0 - used_pct), 1) if denom else None
    traces.append({
        "name": "空き", "x": [vacant_pct if vacant_pct is not None else 0], "y": [""],
        "type": "bar", "orientation": "h", "marker": {"color": "#e5e7eb"},
        "hovertemplate": "空き: %{x}%<extra></extra>",
    })

    layout = _base_layout("", height=180)
    layout["barmode"] = "stack"
    layout["xaxis"] = {"gridcolor": "#DCE1E9", "range": [0, 100], "ticksuffix": "%"}
    layout["yaxis"] = {"gridcolor": "transparent"}

    return {
        "chart": {"traces": traces, "layout": layout, "config": {"responsive": True}},
        "caption": (
            f"主要手術室{OR_ROOM_COUNT}室×{OR_MINUTES_PER_ROOM}分×営業日を100%として、"
            "どの科がどれだけ使ったかの内訳です。枠の割り当て表ではなく、"
            "実際の入室〜退室の合計です。"
        ),
        "excluded": excluded,
        "n": n_valid,
        "depts": dept_rows,
        "biz_days": biz_days,
        "denom_minutes": denom,
    }


# ════════════════════════════════════════
# S4: 緊急+臨時の時間帯×曜日
# ════════════════════════════════════════

def urgent_hour_dow(surg: pd.DataFrame, base_date: pd.Timestamp) -> dict:
    """緊急・臨時手術の曜日×時間帯heatmap。母集団は全室・全曜日・直近52週
    （OP-11A/外来手術センター/心カテなど稼働対象外の室も含む）。
    """
    win = _window(surg, base_date, weeks=52)
    labels_dow = ["月", "火", "水", "木", "金", "土", "日"]
    bin_labels = [f"{h:02d}-{h + 2:02d}" for h in range(0, 24, 2)]
    z = [[0] * 12 for _ in range(7)]
    n_valid = 0
    if len(win) > 0 and "申込区分" in win.columns:
        w = win[win["申込区分"].isin(_URGENT_KINDS)].copy()
        enter = _min_of_day(w["入室時刻"])
        valid = enter.notna()
        n_valid = int(valid.sum())
        w = w.loc[valid]
        enter = enter[valid]
        dow = w["手術実施日"].dt.weekday.to_numpy()
        hour_bin = (enter.to_numpy() // 120).astype(int).clip(0, 11)
        for d, hb in zip(dow, hour_bin):
            z[int(d)][int(hb)] += 1

    trace = {
        "type": "heatmap", "z": z, "x": bin_labels, "y": labels_dow,
        "colorscale": _DHM_LOAD, "zmin": 0,
        "colorbar": {"title": {"text": "件/年", "side": "right"}, "thickness": 12, "len": 0.9},
        "hovertemplate": "%{y}曜 %{x}: %{z}件/年<extra></extra>",
    }
    layout = _base_layout("", height=280)
    layout["xaxis"] = {"type": "category", "side": "top", "gridcolor": "transparent"}
    layout["yaxis"] = {"autorange": "reversed", "gridcolor": "transparent"}

    return {
        "chart": {"traces": [trace], "layout": layout, "config": {"responsive": True}},
        "caption": (
            "予定外（緊急・臨時）の手術が、どの曜日・どの時間帯に入っているかです。"
            "色が濃いほど件数が多い時間帯です。"
        ),
        "excluded": [],
        "n": n_valid,
    }


# ════════════════════════════════════════
# S5: 週間ORタイムライン
# ════════════════════════════════════════

def or_timeline(surg: pd.DataFrame, base_date: pd.Timestamp) -> dict:
    """基準日直前の完全な1週（月〜日）の日別ORタイムライン（ガント）。
    母集団は稼働対象室・全曜日（土日も表示し、稼働の有無自体を見せる）。

    hoverは科名＋入室〜退室時刻＋所要分のみ（術者・術式は含めない）。
    """
    base_date = pd.Timestamp(base_date).normalize()
    dow0 = base_date.weekday()
    this_monday = base_date - pd.Timedelta(days=dow0)
    this_sunday = this_monday + pd.Timedelta(days=6)
    week_monday = this_monday if this_sunday <= base_date else this_monday - pd.Timedelta(days=7)
    week_dates = [week_monday + pd.Timedelta(days=i) for i in range(7)]
    dow_labels = ["月", "火", "水", "木", "金", "土", "日"]

    core = surg[surg["稼働対象室"]]
    win = core[(core["手術実施日"] >= week_monday) & (core["手術実施日"] <= week_monday + pd.Timedelta(days=6))]

    start_line = OR_START_HOUR * 60 + OR_START_MIN
    end_line = OR_END_HOUR * 60 + OR_END_MIN
    xaxis = {
        "type": "linear", "range": [420, 1320],
        "tickvals": list(range(480, 1321, 60)),
        "ticktext": [f"{h}:00" for h in range(8, 23)],
        "gridcolor": "#DCE1E9",
    }
    shapes = [
        {"type": "line", "xref": "x", "yref": "paper", "x0": start_line, "x1": start_line,
         "y0": 0, "y1": 1, "line": {"color": "#9daab8", "width": 1, "dash": "dot"}},
        {"type": "line", "xref": "x", "yref": "paper", "x0": end_line, "x1": end_line,
         "y0": 0, "y1": 1, "line": {"color": "#9daab8", "width": 1, "dash": "dot"}},
    ]

    n_total = 0
    days_payload = {}
    day_labels = []
    for d in week_dates:
        d_key = d.strftime("%Y-%m-%d")
        day_labels.append([d_key, dow_labels[d.weekday()]])
        day_rows = win[win["手術実施日"] == d].copy()
        if len(day_rows) > 0:
            day_rows["_enter"] = _min_of_day(day_rows["入室時刻"])
            day_rows["_dur"] = _duration_min(day_rows)
            day_rows = day_rows[day_rows["_enter"].notna() & day_rows["_dur"].notna()]
        n_total += len(day_rows)

        traces = []
        if len(day_rows) > 0:
            for dept in sorted(day_rows["実施診療科"].unique()):
                dd = day_rows[day_rows["実施診療科"] == dept]
                durations = [int(round(v)) for v in dd["_dur"]]
                customdata = list(zip(dd["入室時刻"], dd["退室時刻"], durations))
                traces.append({
                    "name": str(dept),
                    "y": dd["手術室"].tolist(),
                    "x": durations,
                    "base": dd["_enter"].tolist(),
                    "type": "bar", "orientation": "h",
                    "marker": {"color": _dept_color(dept)},
                    "customdata": customdata,
                    "hovertemplate": f"{dept}<br>" + "%{customdata[0]}〜%{customdata[1]}（%{customdata[2]}分）<extra></extra>",
                })

        layout = _base_layout("", height=max(220, 30 * len(_OR_ROOMS_SORTED) + 100))
        layout["barmode"] = "overlay"
        layout["xaxis"] = dict(xaxis)
        layout["yaxis"] = {"type": "category", "categoryorder": "array",
                            "categoryarray": _OR_ROOMS_SORTED, "gridcolor": "#DCE1E9"}
        layout["shapes"] = [dict(s) for s in shapes]
        days_payload[d_key] = {"traces": traces, "layout": layout, "config": {"responsive": True}}

    return {
        "caption": (
            "先週の手術室の使われ方です。横軸が時刻、1本が1件で、色は診療科です。"
            "点線は8時45分と17時15分です。"
        ),
        "excluded": [],
        "n": n_total,
        "days": days_payload,
        "day_labels": day_labels,
    }


# ════════════════════════════════════════
# S6: 科別予実比
# ════════════════════════════════════════

def planned_actual_ratio(surg: pd.DataFrame, base_date: pd.Timestamp) -> dict:
    """科別予実比（実際の所要時間 ÷ 予定手術時間）。母集団は平日×稼働対象室
    （_CORE）・直近24週・予定手術時間>0 かつ 所要>0 のみ（NaN/0は除外）。
    """
    win = _window(_CORE(surg), base_date, weeks=24)
    n_valid = 0
    overall_median = None
    dept_rows, excluded = [], []
    if len(win) > 0 and "予定手術時間" in win.columns:
        w = win.copy()
        w["_dur"] = _duration_min(w)
        planned = pd.to_numeric(w["予定手術時間"], errors="coerce")
        valid = w["_dur"].notna() & (w["_dur"] > 0) & planned.notna() & (planned > 0)
        n_valid = int(valid.sum())
        if n_valid > 0:
            w = w.loc[valid].copy()
            w["_ratio"] = w["_dur"] / planned[valid]
            overall_median = round(float(w["_ratio"].median()), 2)
            kept, excluded = _dept_groups(w, "_ratio")
            dept_rows = [{
                "dept": d, "median": round(float(s.median()), 2),
                "p75": round(float(s.quantile(0.75)), 2), "n": n,
            } for d, n, s in kept]
            dept_rows.sort(key=lambda d: -d["median"])

    depts = [d["dept"] for d in dept_rows]
    trace_bar = {
        "name": "中央値（予実比）", "x": [d["median"] for d in dept_rows], "y": depts,
        "type": "bar", "orientation": "h",
        "marker": {"color": [_dept_color(d) for d in depts]},
        "hovertemplate": "%{y}: 中央値%{x}倍<extra></extra>",
    }
    trace_p75 = {
        "name": "p75", "x": [d["p75"] for d in dept_rows], "y": depts,
        "type": "scatter", "mode": "markers",
        "marker": {"color": "#D55E00", "symbol": "diamond", "size": 9},
        "hovertemplate": "%{y}: p75 %{x}倍<extra></extra>",
    }
    layout = _base_layout("", height=max(220, 26 * len(depts) + 92))
    layout["yaxis"]["autorange"] = "reversed"
    layout["xaxis"] = {"gridcolor": "#DCE1E9", "title": {"text": "予定時間に対する倍率", "font": {"size": 10}}}
    layout["shapes"] = [{
        "type": "line", "xref": "x", "yref": "paper", "x0": 1.0, "x1": 1.0, "y0": 0, "y1": 1,
        "line": {"color": "#9daab8", "width": 1.5, "dash": "dot"},
    }]

    med_str = overall_median if overall_median is not None else "—"
    return {
        "chart": {"traces": [trace_bar, trace_p75], "layout": layout, "config": {"responsive": True}},
        "caption": (
            "実際にかかった時間が、予定時間の何倍だったかの中央値です"
            f"（全体の中央値: {med_str}）。1.0より右は予定より長くかかった科です。"
        ),
        "excluded": excluded,
        "n": n_valid,
        "depts": dept_rows,
    }


# ════════════════════════════════════════
# S7: 割り込み率+構成
# ════════════════════════════════════════

def _mix_chart(win_all: pd.DataFrame, col: str, cats: list, colors: list) -> tuple:
    """col の値を月次で100%積み上げにした {traces,layout,config} と有効件数nを返す。"""
    n_valid = 0
    months: list = []
    shares = {c: [] for c in cats}
    if len(win_all) > 0 and col in win_all.columns:
        w = win_all[win_all[col].notna()].copy()
        n_valid = int(len(w))
        if n_valid > 0:
            w["_month"] = w["手術実施日"].dt.to_period("M").astype(str)
            months = sorted(w["_month"].unique().tolist())
            for m in months:
                mw = w[w["_month"] == m]
                total = len(mw)
                for c in cats:
                    cnt = int((mw[col] == c).sum())
                    shares[c].append(round(cnt / total * 100, 1) if total else 0.0)
    traces = [{
        "name": c, "x": months, "y": shares[c], "type": "bar", "marker": {"color": colors[i]},
    } for i, c in enumerate(cats)]
    layout = _base_layout("")
    layout["barmode"] = "stack"
    layout["xaxis"] = {"type": "category", "gridcolor": "#DCE1E9"}
    layout["yaxis"] = {"range": [0, 100], "ticksuffix": "%", "gridcolor": "#DCE1E9"}
    return {"traces": traces, "layout": layout, "config": {"responsive": True}}, n_valid


def interrupt_mix(surg: pd.DataFrame, base_date: pd.Timestamp) -> dict:
    """(a) 割り込み率（緊急+臨時の比率・平日×稼働対象室）の月次線 と、
    (b) 申込区分／入外区分の構成（全室・全日）の月次100%積み上げ、を1つにまとめる。
    直近24ヶ月・月次。
    """
    win_core = _window(_CORE(surg), base_date, months=24)
    n_core = 0
    months_a, rates_a = [], []
    if len(win_core) > 0 and "申込区分" in win_core.columns:
        w = win_core[win_core["申込区分"].notna()].copy()
        n_core = int(len(w))
        if n_core > 0:
            w["_month"] = w["手術実施日"].dt.to_period("M").astype(str)
            urgent = w["申込区分"].isin(_URGENT_KINDS)
            grp = pd.DataFrame({"month": w["_month"].to_numpy(), "urgent": urgent.to_numpy()}) \
                .groupby("month")["urgent"].mean().sort_index()
            months_a = list(grp.index)
            rates_a = [round(float(v) * 100, 1) for v in grp]

    trace_a = {
        "name": "割り込み率", "x": months_a, "y": rates_a,
        "type": "scatter", "mode": "lines", "line": {"color": "#D55E00", "width": 3},
    }
    layout_a = _base_layout("")
    layout_a["xaxis"] = {"type": "category", "gridcolor": "#DCE1E9"}
    layout_a["yaxis"]["ticksuffix"] = "%"

    win_all = _window(surg, base_date, months=24)
    mix_kind, _n_kind = _mix_chart(win_all, "申込区分", ["通常", "臨時", "緊急"],
                                    ["#0072B2", "#E69F00", "#D55E00"])
    mix_io, _n_io = _mix_chart(win_all, "入外区分", ["入院", "外来"], ["#0072B2", "#56B4E9"])

    return {
        "chart": {"traces": [trace_a], "layout": layout_a, "config": {"responsive": True}},
        "mix_kind": mix_kind,
        "mix_io": mix_io,
        "caption": (
            "予定外（緊急・臨時）の手術が全体に占める割合と、"
            "申込区分・入外区分の構成の移り変わりです。"
        ),
        "excluded": [],
        "n": n_core,
    }


# ════════════════════════════════════════
# エントリポイント
# ════════════════════════════════════════

def build_surgery_ops_payload(surg: pd.DataFrame, base_date) -> dict:
    """S1〜S7を集計し、detail.html「手術分析」タブ用の単一payloadを返す。

    Returns: {"s1","s1b","s2","s3","s4","s5","s6","s7","meta"} の9キー。
    s1 は overtime_ratio() が s1/s1b の2チャートを内包して返すため update で展開する。
    """
    base_date = pd.Timestamp(base_date).normalize()
    payload: dict = {}
    payload.update(overtime_ratio(surg, base_date))
    payload["s2"] = turnover_minutes(surg, base_date)
    payload["s3"] = capacity_share(surg, base_date)
    payload["s4"] = urgent_hour_dow(surg, base_date)
    payload["s5"] = or_timeline(surg, base_date)
    payload["s6"] = planned_actual_ratio(surg, base_date)
    payload["s7"] = interrupt_mix(surg, base_date)
    payload["meta"] = {
        "base_date": base_date.strftime("%Y-%m-%d"),
        "core_rooms": OR_ROOM_COUNT,
        "minutes_per_room": OR_MINUTES_PER_ROOM,
    }
    return payload
