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
