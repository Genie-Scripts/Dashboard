"""
charts.py — Plotly JSONグラフ生成（v2.1）

v2.1 変更点:
  - bar(棒グラフ)を全面廃止 → 線グラフ中心
  - 手術グラフに二重基準（病院全体=営業平日 / 診療科別=全日）
  - 年度比較を1カラム2系列（line+scatter）併記に変更
  - 目標値を config から動的取得
"""

import json
import pandas as pd
import numpy as np
from .config import (
    CHART_COLORS, TARGET_INPATIENT_ALLDAY, TARGET_ADMISSION_WEEKLY, TARGET_GA_DAILY,
)


def _base_layout(title: str = "", height: int = 360) -> dict:
    """共通レイアウト"""
    return {
        "title": {"text": title, "font": {"size": 14, "color": "#1a2332"}, "x": 0.01},
        "font": {"family": "Noto Sans JP, IBM Plex Mono, sans-serif", "size": 11, "color": "#5A6A82"},
        "xaxis": {"gridcolor": "#DCE1E9", "type": "date"},
        "yaxis": {"rangemode": "tozero", "gridcolor": "#DCE1E9", "zeroline": False},
        "legend": {"orientation": "h", "x": 0, "y": -0.18},
        "hoverlabel": {"bgcolor": "#1D2B3A", "font": {"color": "#E8EEF5", "size": 12}},
        "hovermode": "x unified",
        "margin": {"l": 50, "r": 20, "t": 40, "b": 50},
        "height": height,
        "plot_bgcolor": "#ffffff",
        "paper_bgcolor": "#ffffff",
    }


# ═══════════════════════════════════════
# 在院患者数 推移グラフ（v2.1: bar廃止）
# ═══════════════════════════════════════

def build_inpatient_chart(daily_series: pd.DataFrame, base_date: pd.Timestamp,
                          period_key: str = "24w", target: float = None,
                          ma_window: int = 7, yoy_series: pd.DataFrame = None,
                          dept_name: str = "全体") -> dict:
    """在院患者数 推移グラフ（線グラフ中心）"""
    if target is None:
        target = TARGET_INPATIENT_ALLDAY

    from .metrics import add_moving_average
    series = add_moving_average(daily_series.copy(), 7)
    series = add_moving_average(series, 28)

    # 期間フィルタ
    if period_key == "24w":
        cutoff = base_date - pd.Timedelta(weeks=24)
    elif period_key == "fy":
        fy_year = base_date.year if base_date.month >= 4 else base_date.year - 1
        cutoff = pd.Timestamp(f"{fy_year}-04-01")
    else:
        cutoff = base_date - pd.Timedelta(days=365)
    series = series[series["日付"] >= cutoff]

    xs = [d.strftime("%Y-%m-%d") for d in series["日付"]]
    traces = []

    # 7日移動平均（メイン）
    traces.append({
        "name": "7日移動平均", "x": xs,
        "y": [round(v, 1) if pd.notna(v) else None for v in series["MA7"]],
        "type": "scatter", "mode": "lines",
        "line": {"color": CHART_COLORS["moving_avg"], "width": 2.5},
    })

    # 28日移動平均
    traces.append({
        "name": "28日移動平均", "x": xs,
        "y": [round(v, 1) if pd.notna(v) else None for v in series["MA28"]],
        "type": "scatter", "mode": "lines",
        "line": {"color": CHART_COLORS["moving_avg"], "width": 1.5, "dash": "dash"},
    })

    # 目標ライン
    traces.append({
        "name": f"目標 {target}人", "x": [xs[0], xs[-1]] if xs else [],
        "y": [target, target],
        "type": "scatter", "mode": "lines",
        "line": {"color": CHART_COLORS["target"], "width": 1.5, "dash": "dash"},
    })

    # 前年度
    if yoy_series is not None and len(yoy_series) > 0:
        yoy = yoy_series[yoy_series["日付"] >= cutoff]
        traces.append({
            "name": "前年度", "x": [d.strftime("%Y-%m-%d") for d in yoy["日付"]],
            "y": [round(v, 1) for v in yoy["値"]],
            "type": "scatter", "mode": "lines",
            "line": {"color": CHART_COLORS["yoy"], "width": 1, "dash": "dot"},
        })

    layout = _base_layout(f"在院患者数 推移（{dept_name}）")
    return {"traces": traces, "layout": layout, "config": {"responsive": True}}


# ═══════════════════════════════════════
# 新入院患者数 推移グラフ（v2.1: bar廃止）
# ═══════════════════════════════════════

def build_new_admission_chart(daily_series: pd.DataFrame, base_date: pd.Timestamp,
                              period_key: str = "24w", weekly_target: float = None,
                              ma_window: int = 7, yoy_series: pd.DataFrame = None,
                              dept_name: str = "全体") -> dict:
    """新入院患者数 推移グラフ"""
    if weekly_target is None:
        weekly_target = TARGET_ADMISSION_WEEKLY
    daily_target = round(weekly_target / 7, 1)

    from .metrics import add_moving_average
    series = add_moving_average(daily_series.copy(), 7)
    series = add_moving_average(series, 28)

    if period_key == "24w":
        cutoff = base_date - pd.Timedelta(weeks=24)
    elif period_key == "fy":
        fy_year = base_date.year if base_date.month >= 4 else base_date.year - 1
        cutoff = pd.Timestamp(f"{fy_year}-04-01")
    else:
        cutoff = base_date - pd.Timedelta(days=365)
    series = series[series["日付"] >= cutoff]

    xs = [d.strftime("%Y-%m-%d") for d in series["日付"]]
    traces = []

    traces.append({
        "name": "7日移動平均", "x": xs,
        "y": [round(v, 1) if pd.notna(v) else None for v in series["MA7"]],
        "type": "scatter", "mode": "lines",
        "line": {"color": CHART_COLORS["moving_avg"], "width": 2.5},
    })

    traces.append({
        "name": "28日移動平均", "x": xs,
        "y": [round(v, 1) if pd.notna(v) else None for v in series["MA28"]],
        "type": "scatter", "mode": "lines",
        "line": {"color": CHART_COLORS["moving_avg"], "width": 1.5, "dash": "dash"},
    })

    traces.append({
        "name": f"目標 {daily_target}人/日", "x": [xs[0], xs[-1]] if xs else [],
        "y": [daily_target, daily_target],
        "type": "scatter", "mode": "lines",
        "line": {"color": CHART_COLORS["target"], "width": 1.5, "dash": "dash"},
    })

    if yoy_series is not None and len(yoy_series) > 0:
        yoy = yoy_series[yoy_series["日付"] >= cutoff]
        traces.append({
            "name": "前年度", "x": [d.strftime("%Y-%m-%d") for d in yoy["日付"]],
            "y": [round(v, 1) for v in yoy["値"]],
            "type": "scatter", "mode": "lines",
            "line": {"color": CHART_COLORS["yoy"], "width": 1, "dash": "dot"},
        })

    layout = _base_layout(f"新入院患者数 推移（{dept_name}）")
    return {"traces": traces, "layout": layout, "config": {"responsive": True}}


# ═══════════════════════════════════════
# 入退院バランス（フロー収支）グラフ
# ═══════════════════════════════════════

def build_inout_balance_chart(inflow_series: pd.DataFrame, outflow_series: pd.DataFrame,
                              base_date: pd.Timestamp, period_key: str = "24w",
                              ma_window: int = 7, dept_name: str = "全体") -> dict:
    """入退院バランス（フロー収支）グラフ。

    新入院(inflow=正バー) と 退院合計(outflow=負バー) を 0 を挟む発散バーで描き、
    純増減(inflow−outflow)の移動平均を第2軸の線で重ねる。

    設計意図:
      - 新入院・退院は同一単位(人/日)なので「共通の第1軸」に発散バーで置く。
        バーの上下＝在院数を押し上げる/押し下げる力、面積差＝在院数の増減の勢い。
      - 純増減(≒在院数の日次差分)は導出量でスケールが小さいため、0を中心にした
        「第2軸の線」に分離する。線の符号＝在院数の増減基調（先行的な傾き）。

    inflow_series / outflow_series は build_daily_series の出力（列: 日付, 値）。
    病院全体では転入/転出は病棟間移動で相殺するため outflow=退院合計(退院+死亡)を渡す。
    """
    _in = inflow_series[["日付", "値"]].rename(columns={"値": "inflow"})
    _out = outflow_series[["日付", "値"]].rename(columns={"値": "outflow"})
    m = _in.merge(_out, on="日付", how="outer").sort_values("日付").reset_index(drop=True)
    m["inflow"] = m["inflow"].fillna(0)
    m["outflow"] = m["outflow"].fillna(0)
    m["net_ma"] = (m["inflow"] - m["outflow"]).rolling(ma_window, min_periods=1).mean()

    if period_key == "24w":
        cutoff = base_date - pd.Timedelta(weeks=24)
    elif period_key == "fy":
        fy_year = base_date.year if base_date.month >= 4 else base_date.year - 1
        cutoff = pd.Timestamp(f"{fy_year}-04-01")
    else:
        cutoff = base_date - pd.Timedelta(days=365)
    m = m[m["日付"] >= cutoff]

    xs = [d.strftime("%Y-%m-%d") for d in m["日付"]]
    inflow = [int(v) for v in m["inflow"]]
    outflow_neg = [-int(v) for v in m["outflow"]]
    net_ma = [round(v, 1) if pd.notna(v) else None for v in m["net_ma"]]

    traces = [
        {"name": "新入院", "x": xs, "y": inflow, "type": "bar",
         "marker": {"color": "#0072B2"}, "opacity": 0.75, "yaxis": "y"},
        {"name": "退院（死亡含む）", "x": xs, "y": outflow_neg, "type": "bar",
         "marker": {"color": "#E69F00"}, "opacity": 0.75, "yaxis": "y"},
        {"name": f"純増減 {ma_window}日平均", "x": xs, "y": net_ma, "type": "scatter",
         "mode": "lines", "line": {"color": "#D55E00", "width": 2.5}, "yaxis": "y2"},
    ]

    # 第1軸・第2軸とも 0 を中心に対称化（増減の向きを直感的に読めるよう）
    y1abs = (max([abs(v) for v in inflow + outflow_neg]) if inflow else 5) * 1.08 or 5
    _y2 = [abs(v) for v in net_ma if v is not None]
    y2abs = (max(_y2) if _y2 else 3) * 1.18 or 3

    layout = {
        "title": {"text": f"入退院バランス（{dept_name}）",
                  "font": {"size": 14, "color": "#1a2332"}, "x": 0.01},
        "barmode": "relative",
        "font": {"family": "Noto Sans JP, sans-serif", "size": 11, "color": "#5A6A82"},
        "xaxis": {"gridcolor": "#DCE1E9", "type": "date", "tickformat": "%m/%d", "tickangle": -45},
        "yaxis": {"range": [-y1abs, y1abs], "gridcolor": "#DCE1E9",
                  "zeroline": True, "zerolinecolor": "#888", "zerolinewidth": 1.5,
                  "title": {"text": "人/日", "font": {"size": 10}}},
        "yaxis2": {"overlaying": "y", "side": "right", "range": [-y2abs, y2abs],
                   "gridcolor": "transparent", "zeroline": True,
                   "zerolinecolor": "#D55E00", "zerolinewidth": 1,
                   "tickfont": {"size": 10, "color": "#D55E00"},
                   "title": {"text": "純増減", "font": {"size": 10, "color": "#D55E00"}}},
        "legend": {"orientation": "h", "x": 0, "y": -0.22, "font": {"size": 10}},
        "hovermode": "x unified",
        "margin": {"l": 50, "r": 46, "t": 40, "b": 55},
        "height": 360,
        "plot_bgcolor": "#ffffff", "paper_bgcolor": "#ffffff",
    }
    return {"traces": traces, "layout": layout, "config": {"responsive": True}}


# ═══════════════════════════════════════
# 全身麻酔手術 推移グラフ（v2.1: 二重基準）
# ═══════════════════════════════════════

def build_surgery_chart_hospital(daily_series: pd.DataFrame, base_date: pd.Timestamp,
                                 period_key: str = "24w",
                                 yoy_series: pd.DataFrame = None) -> dict:
    """
    病院全体の全麻推移グラフ ★営業平日基準
    """
    from .metrics import add_moving_average

    series = add_moving_average(daily_series.copy(), 7)

    if period_key == "24w":
        cutoff = base_date - pd.Timedelta(weeks=24)
    elif period_key == "fy":
        fy_year = base_date.year if base_date.month >= 4 else base_date.year - 1
        cutoff = pd.Timestamp(f"{fy_year}-04-01")
    else:
        cutoff = base_date - pd.Timedelta(days=365)
    series = series[series["日付"] >= cutoff]

    xs = [d.strftime("%Y-%m-%d") for d in series["日付"]]
    traces = []

    traces.append({
        "name": "営業平日移動平均", "x": xs,
        "y": [round(v, 1) if pd.notna(v) else None for v in series["MA7"]],
        "type": "scatter", "mode": "lines",
        "line": {"color": CHART_COLORS["moving_avg"], "width": 2.5},
    })

    traces.append({
        "name": f"目標 {TARGET_GA_DAILY}件/日", "x": [xs[0], xs[-1]] if xs else [],
        "y": [TARGET_GA_DAILY, TARGET_GA_DAILY],
        "type": "scatter", "mode": "lines",
        "line": {"color": CHART_COLORS["target"], "width": 1.5, "dash": "dash"},
    })

    if yoy_series is not None and len(yoy_series) > 0:
        yoy = yoy_series[yoy_series["日付"] >= cutoff]
        traces.append({
            "name": "前年度", "x": [d.strftime("%Y-%m-%d") for d in yoy["日付"]],
            "y": [round(v, 1) for v in yoy["値"]],
            "type": "scatter", "mode": "lines",
            "line": {"color": CHART_COLORS["yoy"], "width": 1, "dash": "dot"},
        })

    layout = _base_layout("全身麻酔手術 推移（病院全体・営業平日基準）")
    return {"traces": traces, "layout": layout, "config": {"responsive": True}}


def build_surgery_chart_dept(daily_series: pd.DataFrame, base_date: pd.Timestamp,
                             weekly_target: float = None, dept_name: str = "",
                             period_key: str = "24w",
                             yoy_series: pd.DataFrame = None) -> dict:
    """
    診療科別の全麻推移グラフ ★全日（暦日）基準
    """
    from .metrics import add_moving_average

    series = add_moving_average(daily_series.copy(), 7)

    if period_key == "24w":
        cutoff = base_date - pd.Timedelta(weeks=24)
    elif period_key == "fy":
        fy_year = base_date.year if base_date.month >= 4 else base_date.year - 1
        cutoff = pd.Timestamp(f"{fy_year}-04-01")
    else:
        cutoff = base_date - pd.Timedelta(days=365)
    series = series[series["日付"] >= cutoff]

    xs = [d.strftime("%Y-%m-%d") for d in series["日付"]]
    daily_target = round(weekly_target / 7, 2) if weekly_target else None
    traces = []

    traces.append({
        "name": "暦日7日移動平均", "x": xs,
        "y": [round(v, 1) if pd.notna(v) else None for v in series["MA7"]],
        "type": "scatter", "mode": "lines",
        "line": {"color": CHART_COLORS["moving_avg"], "width": 2.5},
    })

    if daily_target:
        traces.append({
            "name": f"週目標日割り {daily_target}件/日",
            "x": [xs[0], xs[-1]] if xs else [],
            "y": [daily_target, daily_target],
            "type": "scatter", "mode": "lines",
            "line": {"color": CHART_COLORS["target"], "width": 1.5, "dash": "dash"},
        })

    if yoy_series is not None and len(yoy_series) > 0:
        yoy = yoy_series[yoy_series["日付"] >= cutoff]
        traces.append({
            "name": "前年度", "x": [d.strftime("%Y-%m-%d") for d in yoy["日付"]],
            "y": [round(v, 1) for v in yoy["値"]],
            "type": "scatter", "mode": "lines",
            "line": {"color": CHART_COLORS["yoy"], "width": 1, "dash": "dot"},
        })

    layout = _base_layout(f"全身麻酔手術 推移（{dept_name}・暦日基準）")
    return {"traces": traces, "layout": layout, "config": {"responsive": True}}


# ═══════════════════════════════════════
# 年度比較（v2.1: 1カラム2系列併記）
# ═══════════════════════════════════════

def build_surgery_year_compare_chart(current_series: pd.DataFrame,
                                     prev_series: pd.DataFrame,
                                     current_label: str = "今年度",
                                     prev_label: str = "昨年度") -> dict:
    """年度比較: 1カラム内に今年度(line) + 前年度(scatter)"""
    from .metrics import add_moving_average

    cur = add_moving_average(current_series.copy(), 7) if len(current_series) > 0 else current_series
    prv = add_moving_average(prev_series.copy(), 7) if len(prev_series) > 0 else prev_series

    traces = []

    if len(cur) > 0:
        xs = [d.strftime("%Y-%m-%d") for d in cur["日付"]]
        traces.append({
            "name": f"{current_label} 移動平均", "x": xs,
            "y": [round(v, 1) if pd.notna(v) else None for v in cur["MA7"]],
            "type": "scatter", "mode": "lines",
            "line": {"color": CHART_COLORS["moving_avg"], "width": 2.5},
        })

    if len(prv) > 0:
        xs_p = [d.strftime("%Y-%m-%d") for d in prv["日付"]]
        traces.append({
            "name": f"{prev_label} 移動平均", "x": xs_p,
            "y": [round(v, 1) if pd.notna(v) else None for v in prv["MA7"]],
            "type": "scatter", "mode": "lines+markers",
            "line": {"color": CHART_COLORS["yoy"], "width": 1.5, "dash": "dot"},
            "marker": {"size": 3},
        })

    traces.append({
        "name": f"目標 {TARGET_GA_DAILY}件/日",
        "x": [xs[0], xs[-1]] if len(cur) > 0 else [],
        "y": [TARGET_GA_DAILY, TARGET_GA_DAILY],
        "type": "scatter", "mode": "lines",
        "line": {"color": CHART_COLORS["target"], "width": 1.5, "dash": "dash"},
    })

    layout = _base_layout("全身麻酔手術 年度比較（営業平日基準）")
    return {"traces": traces, "layout": layout, "config": {"responsive": True}}


# ═══════════════════════════════════════
# 病棟別利用率ヒートマップ（v2.1: 稼働率→利用率）
# ═══════════════════════════════════════

def build_ward_utilization_heatmap(adm: pd.DataFrame, base_date: pd.Timestamp,
                                   targets: dict, weeks: int = 8) -> dict:
    """病棟別利用率ヒートマップ"""
    from .config import WARD_NAMES, WARD_HIDDEN

    cutoff = base_date - pd.Timedelta(weeks=weeks)
    beds_map = targets.get("inpatient", {}).get("ward_beds", {})

    data = adm[(adm["日付"] >= cutoff) & (adm["日付"] <= base_date) & adm["病棟_表示"]]
    ward_daily = data.groupby(["日付", "病棟コード"])["在院患者数"].sum().reset_index()

    wards = sorted([w for w in WARD_NAMES if w not in WARD_HIDDEN])
    dates = sorted(ward_daily["日付"].unique())

    z = []
    for wcode in wards:
        row = []
        for d in dates:
            val = ward_daily[(ward_daily["病棟コード"] == wcode) & (ward_daily["日付"] == d)]["在院患者数"]
            beds = beds_map.get(wcode, 1)
            util = round(int(val.iloc[0]) / beds * 100, 1) if len(val) > 0 and beds else 0
            row.append(util)
        z.append(row)

    # v2.1色スケール: 高利用率=緑（良好）
    colorscale = [
        [0.0, "#fca5a5"],     # 赤系（低利用率=悪い）
        [0.55, "#fed7aa"],    # オレンジ
        [0.75, "#fef08a"],    # 黄
        [0.90, "#bbf7d0"],    # 薄緑
        [1.0, "#16a34a"],     # 濃緑（高利用率=良い）
    ]

    traces = [{
        "type": "heatmap",
        "z": z,
        "x": [d.strftime("%m/%d") for d in dates],
        "y": [WARD_NAMES.get(w, w) for w in wards],
        "colorscale": colorscale,
        "zmin": 60, "zmax": 110,
        "hovertemplate": "%{y}<br>%{x}: %{z}%<extra></extra>",
    }]

    layout = _base_layout("病棟別利用率ヒートマップ", height=280)
    layout["xaxis"]["type"] = "category"
    layout["yaxis"]["autorange"] = "reversed"

    return {"traces": traces, "layout": layout, "config": {"responsive": True}}


def build_discharge_dow_heatmap(adm: pd.DataFrame, base_date: pd.Timestamp,
                                entity: str = "ward", weeks: int = 8,
                                min_per_week: float = 5.0) -> dict:
    """退院の 曜日×ユニット ヒートマップ（曜日平準化の全体俯瞰）。

    セル値 z = 目標(平日均等[月〜金20%]・週末最小[土日0])からの乖離
              （実績シェア − 目標, ポイント）。赤=過多 / 青=過少。
    表示テキストは実績シェア%。行は金土シェア降順（偏りが大きい順＝上）。
    分子は退院患者数のみ（死亡・転出を除外）= discharge_dow_profile に準拠。
    """
    from .metrics import discharge_dow_profile
    from .config import (WARD_NAMES, WARD_HIDDEN,
                         NADM_DISPLAY_DEPTS, SURGERY_DISPLAY_DEPTS)

    labels = ["月", "火", "水", "木", "金", "土", "日"]
    target = [20.0, 20.0, 20.0, 20.0, 20.0, 0.0, 0.0]

    rows = []  # (name, shares[7])
    if entity == "ward":
        for wcode, wname in WARD_NAMES.items():
            if wcode in WARD_HIDDEN:
                continue
            p = discharge_dow_profile(adm, base_date, group_col="病棟コード",
                                      group_val=wcode, weeks=weeks)
            if p["per_week"] >= min_per_week:
                rows.append((wname, p["shares"]))
    else:
        for dept in sorted(NADM_DISPLAY_DEPTS | SURGERY_DISPLAY_DEPTS):
            p = discharge_dow_profile(adm, base_date, group_col="診療科名",
                                      group_val=dept, weeks=weeks)
            if p["per_week"] >= min_per_week:
                rows.append((dept, p["shares"]))

    # 金土シェア降順（偏りが大きい順を上に）
    rows.sort(key=lambda r: -(r[1][4] + r[1][5]))

    names = [r[0] for r in rows]
    z = [[round(s - t, 1) for s, t in zip(sh, target)] for _, sh in rows]
    text = [[f"{s:.0f}" for s in sh] for _, sh in rows]

    # 発散配色: 青(過少) → 白(目標通り) → 赤(過多)
    colorscale = [
        [0.0, "#2166ac"], [0.30, "#92c5de"], [0.5, "#f7f7f7"],
        [0.70, "#f4a582"], [1.0, "#b2182b"],
    ]

    traces = [{
        "type": "heatmap",
        "z": z, "x": labels, "y": names,
        "text": text, "texttemplate": "%{text}%", "textfont": {"size": 10},
        "colorscale": colorscale, "zmid": 0, "zmin": -15, "zmax": 15,
        "xgap": 2, "ygap": 2,
        "colorbar": {"title": {"text": "目標比", "side": "right"}, "ticksuffix": "pt",
                     "thickness": 12, "len": 0.9},
        "hovertemplate": "%{y}<br>%{x}曜: 実績%{text}%（目標比 %{z:+.1f}pt）<extra></extra>",
    }]

    layout = _base_layout("", height=max(260, 26 * len(names) + 96))
    layout["xaxis"] = {"type": "category", "side": "top", "gridcolor": "transparent",
                       "tickfont": {"size": 12}}
    layout["yaxis"] = {"autorange": "reversed", "gridcolor": "transparent",
                       "tickfont": {"size": 10}}
    layout["margin"] = {"l": 92, "r": 64, "t": 26, "b": 14}
    layout["hovermode"] = "closest"

    return {"traces": traces, "layout": layout,
            "config": {"responsive": True, "displayModeBar": False}}


# 退院・入院 曜日ヒートマップ：指標 → (集計列, 表示ラベル, アイコン)
DOW_METRICS = {
    "discharge":    ("退院患者数",   "退院",     "🚪"),
    "admission":    ("新入院患者数", "入院(全)", "🛏️"),
    "planned":      ("入院患者数",   "予定入院", "🗓️"),
    "emergency":    ("緊急入院患者数", "緊急入院", "🚑"),
    "transfer_in":  ("転入患者数",   "転入",     "↘"),
}

# 発散配色（青=少/減 → 白=0 → 赤=多/増）。目標比・4週Δ 共通。
_DHM_DIVERGENT = [
    [0.0, "#2166ac"], [0.30, "#92c5de"], [0.5, "#f7f7f7"],
    [0.70, "#f4a582"], [1.0, "#b2182b"],
]
# 連続配色（薄→濃インディゴ）。入院系の「負荷シェア」現状表示用。
_DHM_LOAD = [[0.0, "#eef2ff"], [0.5, "#818cf8"], [1.0, "#312e81"]]


def _dow_unit_candidates(entity: str):
    """ヒートマップ対象ユニットの候補 (code, name) リスト。"""
    from .config import (WARD_NAMES, WARD_HIDDEN,
                         NADM_DISPLAY_DEPTS, SURGERY_DISPLAY_DEPTS)
    if entity == "ward":
        return "病棟コード", [(w, n) for w, n in WARD_NAMES.items() if w not in WARD_HIDDEN]
    return "診療科名", [(d, d) for d in sorted(NADM_DISPLAY_DEPTS | SURGERY_DISPLAY_DEPTS)]


def dow_shared_units(adm: pd.DataFrame, base_date: pd.Timestamp,
                     entity: str = "ward", min_per_week: float = 5.0,
                     weeks: int = 8) -> list:
    """退院・入院 両ヒートマップで共通に使う、行順を揃えたユニット (code, name) 列。

    退院 or 全入院 のいずれかが週平均 min_per_week 以上のユニットを採用する。
    行順は **候補の固定順（病棟=フロア順 / 診療科=コード順）** をそのまま使い、
    ボリュームや集中度によるデータ依存の並べ替えはしない。
    （週ごとに行が入れ替わると特定ユニットを探しづらく週次比較もしにくいため、
    位置を固定する。指標 discharge/admission/planned… を切り替えても並びは不変。
    build_dow_heatmap は yaxis autorange='reversed' のため units[0]=最上段。）
    """
    from .metrics import dow_event_profile
    group_col, cand = _dow_unit_candidates(entity)
    units = []
    for code, name in cand:  # cand は既に固定順（ward=フロア順 / dept=コード順）
        dis = dow_event_profile(adm, base_date, "退院患者数",
                                group_col=group_col, group_val=code, weeks=weeks)["per_week"]
        adm_pw = dow_event_profile(adm, base_date, "新入院患者数",
                                   group_col=group_col, group_val=code, weeks=weeks)["per_week"]
        if dis >= min_per_week or adm_pw >= min_per_week:
            units.append((code, name))
    return units


def build_dow_heatmap(adm: pd.DataFrame, base_date: pd.Timestamp,
                      entity: str = "ward", metric: str = "discharge",
                      mode: str = "current", weeks: int = 8,
                      min_per_week: float = 5.0, units: list = None) -> dict:
    """退院・入院 曜日×ユニット ヒートマップ（汎用）。

    metric: discharge / admission / planned / emergency / transfer_in
    mode:
      - "current": 退院=目標比（平日均等[月〜金20%]・週末最小）の発散配色。
                   入院系=曜日シェアの負荷連続配色（濃いほど集中）。
      - "delta4w": 直近4週 − その前4週 のシェア差(Δpt) を発散配色（赤=増/青=減）。
    units: (code, name) の順序付きリスト。指定時はその並び・集合をそのまま使う
           （退院・入院ヒートで行を揃えるため dow_shared_units の結果を渡す）。
           None のときは候補から per_week>=min を抽出し指標別に並べ替える（後方互換）。
    """
    from .metrics import dow_event_profile

    value_col, _label, _icon = DOW_METRICS[metric]
    labels = ["月", "火", "水", "木", "金", "土", "日"]
    target = [20.0, 20.0, 20.0, 20.0, 20.0, 0.0, 0.0]

    if units is not None:
        group_col = "病棟コード" if entity == "ward" else "診療科名"
        rows = [(name, dow_event_profile(adm, base_date, value_col,
                                         group_col=group_col, group_val=code, weeks=weeks))
                for code, name in units]
    else:
        group_col, cand = _dow_unit_candidates(entity)
        rows = []  # (name, profile)
        for code, name in cand:
            p = dow_event_profile(adm, base_date, value_col,
                                  group_col=group_col, group_val=code, weeks=weeks)
            if p["per_week"] >= min_per_week:
                rows.append((name, p))
        if metric == "discharge":
            rows.sort(key=lambda r: -(r[1]["shares"][4] + r[1]["shares"][5]))  # 金土集中順
        else:
            rows.sort(key=lambda r: -r[1]["per_week"])  # 負荷の大きい順

    names = [r[0] for r in rows]

    if mode == "delta4w":
        z = [r[1]["delta"] for r in rows]
        text = [[f"{v:+.0f}" for v in r[1]["delta"]] for r in rows]
        z_kw = {"zmid": 0, "zmin": -10, "zmax": 10, "colorscale": _DHM_DIVERGENT}
        cbtitle = "4週Δ"
        hov = "%{y}<br>%{x}曜: Δ%{z:+.1f}pt（直近4週−前4週シェア）<extra></extra>"
    elif metric == "discharge":
        z = [[round(s - t, 1) for s, t in zip(r[1]["shares"], target)] for r in rows]
        text = [[f"{s:.0f}" for s in r[1]["shares"]] for r in rows]
        z_kw = {"zmid": 0, "zmin": -15, "zmax": 15, "colorscale": _DHM_DIVERGENT}
        cbtitle = "目標比"
        hov = "%{y}<br>%{x}曜: 実績%{text}%（目標比 %{z:+.1f}pt）<extra></extra>"
    else:
        z = [r[1]["shares"] for r in rows]
        text = [[f"{s:.0f}" for s in r[1]["shares"]] for r in rows]
        z_kw = {"zmin": 0, "zmax": 35, "colorscale": _DHM_LOAD}
        cbtitle = "シェア"
        hov = "%{y}<br>%{x}曜: シェア%{z:.0f}%<extra></extra>"

    traces = [dict({
        "type": "heatmap",
        "z": z, "x": labels, "y": names,
        "text": text, "texttemplate": "%{text}", "textfont": {"size": 10},
        "xgap": 2, "ygap": 2,
        "colorbar": {"title": {"text": cbtitle, "side": "right"},
                     "ticksuffix": "pt" if mode == "delta4w" or metric == "discharge" else "%",
                     "thickness": 12, "len": 0.9},
        "hovertemplate": hov,
    }, **z_kw)]

    layout = _base_layout("", height=max(220, 26 * len(names) + 92))
    layout["xaxis"] = {"type": "category", "side": "top", "gridcolor": "transparent",
                       "tickfont": {"size": 12}}
    layout["yaxis"] = {"autorange": "reversed", "gridcolor": "transparent",
                       "tickfont": {"size": 10}}
    layout["margin"] = {"l": 92, "r": 64, "t": 26, "b": 14}
    layout["hovermode"] = "closest"

    return {"traces": traces, "layout": layout,
            "config": {"responsive": True, "displayModeBar": False}}


def build_dow_unit_detail(adm: pd.DataFrame, base_date: pd.Timestamp,
                          entity: str, units: list, weeks: int = 8) -> dict:
    """単一ユニットの入退院 曜日比較（行クリック・ドリル）用データ。

    返値: { ユニット名: { metric: {avg:[月..日 日平均人数], per_week, delta:[Δpt]} } }
    metric は discharge / admission / planned / emergency（病棟は transfer_in も）。
    avg は 8週合計を実集計週数で割った「その曜日の日平均人数」。
    """
    from .metrics import dow_event_profile
    group_col = "病棟コード" if entity == "ward" else "診療科名"
    metrics = ["discharge", "admission", "planned", "emergency"]
    if entity == "ward":
        metrics.append("transfer_in")

    out = {}
    for code, name in units:
        md = {}
        for met in metrics:
            value_col = DOW_METRICS[met][0]
            p = dow_event_profile(adm, base_date, value_col,
                                  group_col=group_col, group_val=code, weeks=weeks)
            w = p["weeks"] or 1
            md[met] = {
                "avg": [round(c / w, 1) for c in p["counts"]],
                "per_week": p["per_week"],
                "delta": p["delta"],
            }
        out[name] = md
    return out


# ═══════════════════════════════════════
# 粗利チャート（変更なし）
# ═══════════════════════════════════════

def build_profit_chart(series_months, series_values, target=None,
                       dept_name="全体", achievements=None) -> dict:
    """月次粗利チャート"""
    traces = [{
        "name": "粗利", "x": series_months, "y": series_values,
        "type": "bar",
        "marker": {"color": CHART_COLORS["bar_fill"]},
    }]
    if target:
        traces.append({
            "name": f"目標 {target}M",
            "x": [series_months[0], series_months[-1]] if series_months else [],
            "y": [target, target],
            "type": "scatter", "mode": "lines",
            "line": {"color": CHART_COLORS["target"], "dash": "dash"},
        })
    layout = _base_layout(f"粗利 推移（{dept_name}）")
    return {"traces": traces, "layout": layout, "config": {"responsive": True}}
