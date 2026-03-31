"""
charts.py — Plotly JSONグラフ生成
移動平均メイン・目標線・昨年度比較トグル対応
"""

import json
import pandas as pd
import numpy as np
from datetime import timedelta
from typing import Optional
from .config import CHART_COLORS


# ────────────────────────────────────────────────────
# 共通ユーティリティ
# ────────────────────────────────────────────────────

def _fmt_date(d) -> str:
    if hasattr(d, "strftime"):
        return d.strftime("%Y-%m-%d")
    return str(d)


def _base_layout(title: str, yaxis_title: str = "", height: int = 280) -> dict:
    """Plotlyレイアウト共通設定（案Aデザイン準拠）"""
    return {
        "title": {
            "text": title,
            "font": {"size": 13, "color": "#1A2535", "family": "Noto Sans JP"},
            "x": 0,
            "pad": {"l": 4},
        },
        "height": height,
        "margin": {"t": 40, "b": 40, "l": 48, "r": 16},
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "font": {"family": "Noto Sans JP, IBM Plex Mono, sans-serif", "size": 11, "color": "#5A6A82"},
        "xaxis": {
            "gridcolor": "#DCE1E9",
            "gridwidth": 1,
            "linecolor": "#DCE1E9",
            "tickfont": {"size": 10, "color": "#5A6A82"},
            "type": "date",
        },
        "yaxis": {
            "gridcolor": "#DCE1E9",
            "gridwidth": 1,
            "linecolor": "#DCE1E9",
            "tickfont": {"size": 10, "color": "#5A6A82"},
            "title": {"text": yaxis_title, "font": {"size": 10}},
            "rangemode": "tozero",
            "zeroline": False,
        },
        "legend": {
            "orientation": "h",
            "x": 0, "y": -0.18,
            "font": {"size": 10},
            "bgcolor": "rgba(0,0,0,0)",
        },
        "hovermode": "x unified",
        "hoverlabel": {
            "bgcolor": "#1D2B3A",
            "font": {"color": "#E8EEF5", "size": 11},
            "bordercolor": "#3A6EA5",
        },
    }


def _filter_period(df: pd.DataFrame, base_date: pd.Timestamp,
                   period_key: str, date_col: str = "日付") -> pd.DataFrame:
    """期間キーに応じてDataFrameをフィルタ"""
    end = base_date
    if period_key == "7d":
        start = end - timedelta(days=6)
        return df[df[date_col].between(start, end)]
    elif period_key == "12w":
        start = end - timedelta(weeks=12)
        return df[df[date_col] >= start]
    elif period_key == "24w":
        start = end - timedelta(weeks=24)
        return df[df[date_col] >= start]
    elif period_key == "365d":
        start = end - timedelta(days=364)
        return df[df[date_col] >= start]
    elif period_key == "fy":
        # 今年度：4月〜
        fy_start_year = base_date.year if base_date.month >= 4 else base_date.year - 1
        start = pd.Timestamp(f"{fy_start_year}-04-01")
        return df[df[date_col] >= start]
    return df


def _aggregate_for_period(series: pd.DataFrame, period_key: str) -> pd.DataFrame:
    """期間キーに応じて日次→週次集約（12週以上は週次）"""
    if period_key == "7d":
        return series
    # 週次集約
    df = series.copy()
    df["週開始"] = df["日付"] - pd.to_timedelta(df["日付"].dt.weekday, unit="D")
    weekly = df.groupby("週開始")["値"].mean().reset_index()
    weekly.columns = ["日付", "値"]
    return weekly


def _make_target_hline(target: float, x0, x1, label: str = "目標",
                        dash: str = "dash") -> dict:
    """目標線シェイプ"""
    return {
        "type": "line",
        "x0": _fmt_date(x0), "x1": _fmt_date(x1),
        "y0": target, "y1": target,
        "line": {"color": CHART_COLORS["target"], "width": 1.5, "dash": dash},
    }


def _series_to_lists(df: pd.DataFrame, x_col: str = "日付",
                      y_col: str = "値") -> tuple:
    xs = [_fmt_date(d) for d in df[x_col]]
    ys = [round(float(v), 1) if pd.notna(v) else None for v in df[y_col]]
    return xs, ys


def _yoy_shift(df: pd.DataFrame) -> pd.DataFrame:
    """前年度データ（1年前にシフト）"""
    df2 = df.copy()
    df2["日付"] = df2["日付"] + pd.DateOffset(years=1)
    return df2


# ────────────────────────────────────────────────────
# 個別グラフ生成関数
# ────────────────────────────────────────────────────

def build_inpatient_chart(daily_series: pd.DataFrame,
                           base_date: pd.Timestamp,
                           period_key: str = "12w",
                           target: Optional[float] = 567,
                           ma_window: int = 7,
                           yoy_series: Optional[pd.DataFrame] = None,
                           dept_name: str = "全体") -> dict:
    """在院患者数トレンドグラフ（移動平均メイン）

    Returns:
        {"traces": [...], "layout": {...}, "config": {...}}
        ※ Plotly.newPlot(divId, data.traces, data.layout, data.config) で描画
    """
    filtered = _filter_period(daily_series, base_date, period_key)
    aggregated = _aggregate_for_period(filtered, period_key)

    # 移動平均
    aggregated = aggregated.copy()
    win = min(ma_window, len(aggregated))
    aggregated[f"MA{ma_window}"] = aggregated["値"].rolling(window=win, min_periods=1).mean()

    xs, ys_raw = _series_to_lists(aggregated)
    _, ys_ma = _series_to_lists(aggregated, y_col=f"MA{ma_window}")

    traces = []

    # 棒グラフ（実績・薄め）
    traces.append({
        "type": "bar",
        "name": "実績",
        "x": xs, "y": ys_raw,
        "marker": {"color": CHART_COLORS["bar_fill"]},
        "opacity": 0.5,
        "hovertemplate": "%{y}人<extra>実績</extra>",
    })

    # 移動平均線（メイン）
    traces.append({
        "type": "scatter",
        "mode": "lines",
        "name": f"{ma_window}日移動平均",
        "x": xs, "y": ys_ma,
        "line": {"color": CHART_COLORS["moving_avg"], "width": 2.5},
        "hovertemplate": "%{y:.1f}人<extra>MA</extra>",
    })

    # 昨年度比較
    if yoy_series is not None and len(yoy_series) > 0:
        yoy = _filter_period(yoy_series, base_date, period_key)
        yoy_agg = _aggregate_for_period(yoy, period_key)
        yoy_shifted = _yoy_shift(yoy_agg)
        xs_yoy, ys_yoy = _series_to_lists(yoy_shifted)
        traces.append({
            "type": "scatter",
            "mode": "lines",
            "name": "前年度",
            "x": xs_yoy, "y": ys_yoy,
            "line": {"color": CHART_COLORS["yoy"], "width": 1.5, "dash": "dot"},
            "opacity": 0.75,
            "hovertemplate": "%{y:.0f}人<extra>前年度</extra>",
            "visible": "legendonly",  # 初期非表示（トグルで表示）
        })

    layout = _base_layout(
        f"在院患者数{'（' + dept_name + '）' if dept_name != '全体' else ''}",
        yaxis_title="人",
        height=280,
    )

    # 目標線
    if target and len(aggregated) > 0:
        layout.setdefault("shapes", [])
        layout["shapes"].append(_make_target_hline(
            target,
            aggregated["日付"].min(),
            aggregated["日付"].max(),
            label="目標",
        ))
        # 目標ラベル
        layout.setdefault("annotations", [])
        layout["annotations"].append({
            "x": _fmt_date(aggregated["日付"].max()),
            "y": target,
            "text": f"目標 {int(target)}",
            "showarrow": False,
            "xanchor": "right",
            "yanchor": "bottom",
            "font": {"size": 9, "color": CHART_COLORS["target"]},
        })

    return {
        "traces": traces,
        "layout": layout,
        "config": {"displayModeBar": False, "responsive": True},
    }


def build_new_admission_chart(daily_series: pd.DataFrame,
                               base_date: pd.Timestamp,
                               period_key: str = "12w",
                               weekly_target: Optional[float] = 385,
                               ma_window: int = 7,
                               yoy_series: Optional[pd.DataFrame] = None,
                               dept_name: str = "全体") -> dict:
    """新入院患者数トレンドグラフ"""
    filtered = _filter_period(daily_series, base_date, period_key)
    aggregated = _aggregate_for_period(filtered, period_key)

    aggregated = aggregated.copy()
    win = min(ma_window, len(aggregated))
    aggregated[f"MA{ma_window}"] = aggregated["値"].rolling(window=win, min_periods=1).mean()

    xs, ys_raw = _series_to_lists(aggregated)
    _, ys_ma = _series_to_lists(aggregated, y_col=f"MA{ma_window}")

    traces = []
    traces.append({
        "type": "bar",
        "name": "実績",
        "x": xs, "y": ys_raw,
        "marker": {"color": CHART_COLORS["bar_fill"]},
        "opacity": 0.45,
        "hovertemplate": "%{y}人<extra>実績</extra>",
    })
    traces.append({
        "type": "scatter",
        "mode": "lines",
        "name": f"{ma_window}日移動平均",
        "x": xs, "y": ys_ma,
        "line": {"color": CHART_COLORS["moving_avg"], "width": 2.5},
        "hovertemplate": "%{y:.1f}人<extra>MA</extra>",
    })

    if yoy_series is not None and len(yoy_series) > 0:
        yoy = _filter_period(yoy_series, base_date, period_key)
        yoy_agg = _aggregate_for_period(yoy, period_key)
        yoy_shifted = _yoy_shift(yoy_agg)
        xs_yoy, ys_yoy = _series_to_lists(yoy_shifted)
        traces.append({
            "type": "scatter",
            "mode": "lines",
            "name": "前年度",
            "x": xs_yoy, "y": ys_yoy,
            "line": {"color": CHART_COLORS["yoy"], "width": 1.5, "dash": "dot"},
            "opacity": 0.75,
            "hovertemplate": "%{y:.0f}人<extra>前年度</extra>",
            "visible": "legendonly",
        })

    layout = _base_layout(
        f"新入院患者数{'（' + dept_name + '）' if dept_name != '全体' else ''}",
        yaxis_title="人/日",
        height=280,
    )

    # 週目標を日割換算して目標線表示
    if weekly_target and len(aggregated) > 0:
        daily_target = weekly_target / 7
        layout.setdefault("shapes", [])
        layout["shapes"].append(_make_target_hline(
            daily_target,
            aggregated["日付"].min(),
            aggregated["日付"].max(),
        ))
        layout.setdefault("annotations", [])
        layout["annotations"].append({
            "x": _fmt_date(aggregated["日付"].max()),
            "y": daily_target,
            "text": f"週{int(weekly_target)}/日{daily_target:.0f}",
            "showarrow": False,
            "xanchor": "right",
            "yanchor": "bottom",
            "font": {"size": 9, "color": CHART_COLORS["target"]},
        })

    return {
        "traces": traces,
        "layout": layout,
        "config": {"displayModeBar": False, "responsive": True},
    }


def build_surgery_ga_chart(surgery_daily: pd.DataFrame,
                            base_date: pd.Timestamp,
                            period_key: str = "12w",
                            daily_target: Optional[float] = 21,
                            ma_window: int = 7,
                            yoy_series: Optional[pd.DataFrame] = None,
                            dept_name: str = "全体") -> dict:
    """全身麻酔件数トレンドグラフ"""
    filtered = _filter_period(surgery_daily, base_date, period_key)
    aggregated = _aggregate_for_period(filtered, period_key)

    aggregated = aggregated.copy()
    # 週次の場合は週合計、日次は7日MA
    if period_key == "7d":
        win = min(ma_window, len(aggregated))
        aggregated["MA"] = aggregated["値"].rolling(window=win, min_periods=1).mean()
        ma_label = f"{ma_window}日移動平均"
    else:
        aggregated["MA"] = aggregated["値"]  # 週次集約済
        ma_label = "週合計（平均）"

    xs, ys_raw = _series_to_lists(aggregated)
    _, ys_ma = _series_to_lists(aggregated, y_col="MA")

    traces = []
    traces.append({
        "type": "bar",
        "name": "全麻件数",
        "x": xs, "y": ys_raw,
        "marker": {"color": CHART_COLORS["bar_fill_ga"]},
        "opacity": 0.55,
        "hovertemplate": "%{y}件<extra>実績</extra>",
    })
    traces.append({
        "type": "scatter",
        "mode": "lines+markers",
        "name": ma_label,
        "x": xs, "y": ys_ma,
        "line": {"color": CHART_COLORS["moving_avg"], "width": 2.5},
        "marker": {"size": 5},
        "hovertemplate": "%{y:.1f}件<extra>MA</extra>",
    })

    if yoy_series is not None and len(yoy_series) > 0:
        yoy = _filter_period(yoy_series, base_date, period_key)
        yoy_agg = _aggregate_for_period(yoy, period_key)
        yoy_shifted = _yoy_shift(yoy_agg)
        xs_yoy, ys_yoy = _series_to_lists(yoy_shifted)
        traces.append({
            "type": "scatter",
            "mode": "lines",
            "name": "前年度",
            "x": xs_yoy, "y": ys_yoy,
            "line": {"color": CHART_COLORS["yoy"], "width": 1.5, "dash": "dot"},
            "opacity": 0.7,
            "visible": "legendonly",
        })

    layout = _base_layout(
        f"全身麻酔件数{'（' + dept_name + '）' if dept_name != '全体' else ''}",
        yaxis_title="件",
        height=280,
    )

    if daily_target and len(aggregated) > 0:
        # 週次表示なら週目標（平日5日）
        target_val = daily_target * 5 if period_key != "7d" else daily_target
        layout.setdefault("shapes", [])
        layout["shapes"].append(_make_target_hline(
            target_val,
            aggregated["日付"].min(),
            aggregated["日付"].max(),
        ))
        layout.setdefault("annotations", [])
        layout["annotations"].append({
            "x": _fmt_date(aggregated["日付"].max()),
            "y": target_val,
            "text": f"目標 {int(target_val)}",
            "showarrow": False,
            "xanchor": "right",
            "yanchor": "bottom",
            "font": {"size": 9, "color": CHART_COLORS["target"]},
        })

    return {
        "traces": traces,
        "layout": layout,
        "config": {"displayModeBar": False, "responsive": True},
    }


def build_surgery_ga_weekly_dept_chart(surgery_daily: pd.DataFrame,
                                        base_date: pd.Timestamp,
                                        period_key: str = "12w",
                                        weekly_target: Optional[float] = None,
                                        dept_name: str = "") -> dict:
    """診療科別 全身麻酔件数 週次棒グラフ

    診療科の目標は「週合計」で設定されているため、
    日次グラフではなく週次集計棒グラフで表示する。
    各バーは月曜始まりの週合計件数。目標線は週目標値。

    Args:
        surgery_daily: 日次全麻件数 DataFrame（日付・値）
        base_date:     基準日
        period_key:    表示期間（"7d"のときは直近7日合計1棒のみ）
        weekly_target: 診療科週目標件数
        dept_name:     診療科名（グラフタイトル用）
    """
    filtered = _filter_period(surgery_daily, base_date, period_key)

    # 週次合計集計（月曜始まり）
    df = filtered.copy()
    if len(df) == 0:
        aggregated = pd.DataFrame(columns=["日付", "値"])
    else:
        df["週開始"] = df["日付"] - pd.to_timedelta(df["日付"].dt.weekday, unit="D")
        aggregated = df.groupby("週開始")["値"].sum().reset_index()
        aggregated.columns = ["日付", "値"]
        aggregated = aggregated.sort_values("日付")

    xs, ys = _series_to_lists(aggregated)

    # バー色: 目標比で色分け
    bar_colors = []
    for v in ys:
        if v is None or weekly_target is None:
            bar_colors.append(CHART_COLORS["bar_fill_ga"])
        elif v >= weekly_target:
            bar_colors.append("rgba(26,158,106,0.65)")
        elif v >= weekly_target * 0.85:
            bar_colors.append("rgba(200,122,0,0.65)")
        else:
            bar_colors.append("rgba(192,41,59,0.55)")

    traces = []
    traces.append({
        "type": "bar",
        "name": "週合計件数",
        "x": xs, "y": ys,
        "marker": {"color": bar_colors},
        "text": [str(int(v)) if v is not None else "" for v in ys],
        "textposition": "outside",
        "textfont": {"size": 9},
        "hovertemplate": "週合計: %{y}件<extra></extra>",
    })

    title_label = f"全身麻酔件数（{dept_name}）週次" if dept_name else "全身麻酔件数 週次"
    layout = _base_layout(title_label, yaxis_title="件/週", height=280)
    layout["xaxis"] = dict(layout["xaxis"], type="category", tickangle=-30)

    if weekly_target and len(aggregated) > 0:
        layout.setdefault("shapes", [])
        layout["shapes"].append({
            "type": "line",
            "x0": 0, "x1": 1,
            "y0": weekly_target, "y1": weekly_target,
            "xref": "paper", "yref": "y",
            "line": {"color": CHART_COLORS["target"], "width": 1.5, "dash": "dash"},
        })
        layout.setdefault("annotations", [])
        layout["annotations"].append({
            "x": 1, "xref": "paper",
            "y": weekly_target, "yref": "y",
            "text": f"週目標 {int(weekly_target)}件",
            "showarrow": False,
            "xanchor": "right",
            "yanchor": "bottom",
            "font": {"size": 9, "color": CHART_COLORS["target"]},
        })

    return {
        "traces": traces,
        "layout": layout,
        "config": {"displayModeBar": False, "responsive": True},
    }



# ────────────────────────────────────────────────────
# 全期間データのシリアライズ（JS埋め込み用）
# ────────────────────────────────────────────────────

def build_all_chart_data(adm: pd.DataFrame,
                          surg: pd.DataFrame,
                          base_date: pd.Timestamp,
                          targets: dict,
                          surgery_targets: dict,
                          depts: list) -> dict:
    """
    全期間・全科のグラフデータを一括生成してJSONシリアライズ可能な辞書で返す。
    JS側でperiod_key・dept_nameに応じてfilter/replaceする。

    Returns:
        {
            "global": {
                "inpatient": {series},
                "new_admission": {series},
                "surgery_ga": {series},
            },
            "by_dept": {
                "総合内科": {"inpatient": {series}, "new_admission": {series}, "surgery_ga": {series}},
                ...
            },
            "targets": {
                "inpatient": {"全日": 567, "平日": 580, "休日": 540},
                "new_admission_weekly": 385,
                "surgery_ga_daily": 21,
                "dept_inpatient": {"総合内科": 85, ...},
                "dept_new_admission": {"総合内科": 50.8, ...},
                "dept_surgery": {"整形外科": 27.2, ...},
            }
        }
    """
    from .metrics import build_daily_series, build_surgery_daily_series

    def _series_to_dict(df: pd.DataFrame) -> dict:
        return {
            "dates": [_fmt_date(d) for d in df["日付"]],
            "values": [round(float(v), 1) if pd.notna(v) else None for v in df["値"]],
        }

    # 全体時系列
    s_inp  = build_daily_series(adm, "在院患者数")
    s_nadm = build_daily_series(adm, "新入院患者数")
    s_ga   = build_surgery_daily_series(surg, ga_only=True)

    global_data = {
        "inpatient":    _series_to_dict(s_inp),
        "new_admission": _series_to_dict(s_nadm),
        "surgery_ga":   _series_to_dict(s_ga),
    }

    # ── 昨年度比較 YoY シリーズ（Phase 3）────────────────
    # 1年前のデータが存在する場合のみ生成。日付を+1年シフトして返す。
    def _build_yoy(series_df: pd.DataFrame) -> dict:
        """日付を1年後にシフトしたYoYシリーズを返す（JS側でオーバーレイ用）"""
        if len(series_df) == 0:
            return {"dates": [], "values": []}
        shifted = series_df.copy()
        shifted["日付"] = shifted["日付"] + pd.DateOffset(years=1)
        return {
            "dates":  [_fmt_date(d) for d in shifted["日付"]],
            "values": [round(float(v), 1) if pd.notna(v) else None for v in shifted["値"]],
        }

    global_data["yoy_inpatient"]    = _build_yoy(s_inp)
    global_data["yoy_new_admission"] = _build_yoy(s_nadm)
    global_data["yoy_surgery_ga"]   = _build_yoy(s_ga)

    # 診療科別時系列
    by_dept = {}
    for dept in depts:
        d_inp = build_daily_series(adm, "在院患者数",
                                    group_col="診療科名", group_val=dept)
        d_nadm = build_daily_series(adm, "新入院患者数",
                                     group_col="診療科名", group_val=dept)
        d_ga = build_surgery_daily_series(surg, ga_only=True, dept=dept)

        # 週次集計（月曜始まり週合計）
        d_ga_weekly = {}
        if len(d_ga) > 0:
            _wdf = d_ga.copy()
            _wdf["週開始"] = _wdf["日付"] - pd.to_timedelta(_wdf["日付"].dt.weekday, unit="D")
            _wk = _wdf.groupby("週開始")["値"].sum().reset_index()
            _wk.columns = ["日付", "値"]
            d_ga_weekly = _series_to_dict(_wk)
        else:
            d_ga_weekly = {"dates": [], "values": []}

        by_dept[dept] = {
            "inpatient": _series_to_dict(d_inp),
            "new_admission": _series_to_dict(d_nadm),
            "surgery_ga": _series_to_dict(d_ga),
            "surgery_ga_weekly": d_ga_weekly,  # 診療科別週次全麻（週目標対比用）
        }

    # 病棟別時系列（看護師タブ用）
    from .config import WARD_NAMES, WARD_HIDDEN
    all_wards = [c for c in adm["病棟コード"].dropna().unique()
                 if c not in WARD_HIDDEN]

    # 全体退院・転入転出の日次時系列
    s_discharge = build_daily_series(adm, "退院合計", display_filter=False)
    s_transfer_in = build_daily_series(adm, "転入患者数", display_filter=False)
    s_transfer_out = build_daily_series(adm, "転出患者数", display_filter=False)
    s_load = build_daily_series(adm, "出入り負荷", display_filter=False)

    global_data["discharge"]     = _series_to_dict(s_discharge)
    global_data["transfer_in"]   = _series_to_dict(s_transfer_in)
    global_data["transfer_out"]  = _series_to_dict(s_transfer_out)
    global_data["ward_load"]     = _series_to_dict(s_load)

    by_ward = {}
    for wcode in all_wards:
        w_inp  = build_daily_series(adm, "在院患者数",
                                     group_col="病棟コード", group_val=wcode,
                                     display_filter=False)
        w_nadm = build_daily_series(adm, "新入院患者数",
                                     group_col="病棟コード", group_val=wcode,
                                     display_filter=False)
        w_dis  = build_daily_series(adm, "退院合計",
                                     group_col="病棟コード", group_val=wcode,
                                     display_filter=False)
        w_load = build_daily_series(adm, "出入り負荷",
                                     group_col="病棟コード", group_val=wcode,
                                     display_filter=False)
        w_tin  = build_daily_series(adm, "転入患者数",
                                     group_col="病棟コード", group_val=wcode,
                                     display_filter=False)
        w_tout = build_daily_series(adm, "転出患者数",
                                     group_col="病棟コード", group_val=wcode,
                                     display_filter=False)
        wname = WARD_NAMES.get(wcode, wcode)
        by_ward[wcode] = {
            "name": wname,
            "inpatient":    _series_to_dict(w_inp),
            "new_admission": _series_to_dict(w_nadm),
            "discharge":    _series_to_dict(w_dis),
            "ward_load":    _series_to_dict(w_load),
            "transfer_in":  _series_to_dict(w_tin),
            "transfer_out": _series_to_dict(w_tout),
        }

    result = {
        "global": global_data,
        "by_dept": by_dept,
        "by_ward": by_ward,
        "targets": {
            "inpatient": targets.get("inpatient", {}).get("hospital", {}),
            "new_admission_weekly": (targets.get("new_admission", {})
                                     .get("hospital", {}).get("全日", 385)),
            "surgery_ga_daily": 21,
            "dept_inpatient": targets.get("inpatient", {}).get("dept", {}),
            "dept_new_admission": targets.get("new_admission", {}).get("dept", {}),
            "dept_surgery": surgery_targets,
            # 看護師タブ用
            "ward_inpatient": targets.get("inpatient", {}).get("ward", {}),
            "ward_new_admission": targets.get("new_admission", {}).get("ward", {}),
            "ward_beds": targets.get("inpatient", {}).get("ward_beds", {}),
        },
        "base_date": _fmt_date(base_date),
        # 病棟リスト（コード→名称）
        "ward_list": {wcode: WARD_NAMES.get(wcode, wcode) for wcode in all_wards},
    }
    return result


def build_profit_plotly_chart(series_months: list, series_values: list,
                               target: Optional[float] = None,
                               dept_name: str = "全体",
                               achievements: list = None) -> dict:
    """粗利月次推移Plotlyグラフ（静的HTML / Streamlit共用）

    Args:
        series_months:  ["2024-04", "2024-05", ...]
        series_values:  [百万円, ...]
        target:         月次目標（百万円）
        achievements:   [達成率%, ...]  オプション
    Returns:
        {"traces": [...], "layout": {...}, "config": {...}}
    """
    traces = []

    # 棒グラフ（実績）
    hover = [
        f"{v:.1f}百万円" + (f"（{a:.1f}%）" if achievements and i < len(achievements) and achievements[i] else "")
        for i, v in enumerate(series_values)
    ]
    bar_colors = []
    if achievements:
        for a in achievements:
            if a is None:            bar_colors.append(CHART_COLORS["bar_fill"])
            elif a >= 100:           bar_colors.append("rgba(26,158,106,0.65)")
            elif a >= 85:            bar_colors.append("rgba(200,122,0,0.65)")
            else:                    bar_colors.append("rgba(192,41,59,0.55)")
    else:
        bar_colors = [CHART_COLORS["bar_fill"]] * len(series_values)

    traces.append({
        "type": "bar",
        "name": "粗利（百万円）",
        "x": series_months,
        "y": series_values,
        "marker": {"color": bar_colors},
        "text": [f"{v:.1f}" for v in series_values],
        "textposition": "outside",
        "textfont": {"size": 9},
        "hovertext": hover,
        "hovertemplate": "%{hovertext}<extra></extra>",
    })

    # 目標線
    shapes, annotations = [], []
    if target and series_months:
        shapes.append({
            "type": "line",
            "x0": series_months[0], "x1": series_months[-1],
            "y0": target, "y1": target,
            "line": {"color": CHART_COLORS["target"], "width": 1.5, "dash": "dash"},
        })
        annotations.append({
            "x": series_months[-1], "y": target,
            "text": f"目標 {target:.0f}M",
            "showarrow": False,
            "xanchor": "right", "yanchor": "bottom",
            "font": {"size": 9, "color": CHART_COLORS["target"]},
        })

    layout = _base_layout(
        f"月次粗利{'（' + dept_name + '）' if dept_name != '全体' else ''}",
        yaxis_title="百万円",
        height=300,
    )
    layout["xaxis"] = dict(layout["xaxis"], type="category")  # 月カテゴリ
    layout["shapes"]      = shapes
    layout["annotations"] = annotations

    return {
        "traces": traces,
        "layout": layout,
        "config": {"displayModeBar": False, "responsive": True},
    }


def chart_data_to_json(data: dict) -> str:
    """JSONシリアライズ（NaN/Inf安全）"""
    return json.dumps(data, ensure_ascii=False, default=str)


# ────────────────────────────────────────────────────
# 医師版 新チャートデータ（Phase 2）
# ────────────────────────────────────────────────────

def build_doctor_chart_data(adm: pd.DataFrame,
                             surg: pd.DataFrame,
                             base_date: pd.Timestamp,
                             targets: dict,
                             surgery_targets: dict,
                             depts: list) -> dict:
    """
    医師版ダッシュボード用チャートデータを生成する。

    Returns:
        {
          "cumulative_progress": {...},
          "waterfall":           [...],
          "surgery_bubble":      {...},
          "department_heatmap":  {...},
        }
    """
    from .metrics import (
        weekly_new_admission, weekly_surgery,
        rolling7_new_admission, rolling7_surgery,
        build_daily_series, achievement_rate,
    )
    from .config import SURGERY_DISPLAY_DEPTS

    # ─ 1. 累積進捗チャート ─────────────────────────
    # 直近7日（基準日を含む過去7日）の累積進捗。
    # metrics.py の rolling7 ウィンドウと完全に一致する。
    import jpholiday as _jpholiday
    rolling7_start = base_date - pd.Timedelta(days=6)

    # 新入院日次（直近7日分）
    week_adm = adm[(adm["日付"] >= rolling7_start) & (adm["日付"] <= base_date)]
    nadm_daily = (week_adm.groupby("日付")["新入院患者数"]
                  .sum().reset_index().sort_values("日付"))

    # 全麻日次（直近7日分）
    week_surg = surg[(surg["手術実施日"] >= rolling7_start) & (surg["手術実施日"] <= base_date) & surg["全麻"]]
    surg_daily = week_surg.groupby("手術実施日").size().reset_index(name="件数").sort_values("手術実施日")

    # 直近7日の日付レンジ（常に7点）
    days = pd.date_range(rolling7_start, base_date, freq="D")
    nadm_wk_target = targets.get("new_admission", {}).get("hospital", {}).get("全日", 385)
    surg_day_target = 21  # 全麻日次目標

    nadm_actuals, nadm_targets = [], []
    surg_actuals, surg_targets_cum = [], []
    cum_nadm = cum_surg = cum_nadm_tgt = cum_surg_tgt = 0
    dates_str = []

    for d in days:
        d_ts = pd.Timestamp(d)
        dates_str.append(d_ts.strftime("%Y-%m-%d"))

        # 新入院実績
        row = nadm_daily[nadm_daily["日付"] == d_ts]
        cum_nadm += int(row["新入院患者数"].iloc[0]) if len(row) > 0 else 0
        nadm_actuals.append(cum_nadm)

        # 新入院目標（7日均等割）
        cum_nadm_tgt += nadm_wk_target / 7
        nadm_targets.append(round(cum_nadm_tgt, 1))

        # 全麻実績
        row2 = surg_daily[surg_daily["手術実施日"] == d_ts]
        cum_surg += int(row2["件数"].iloc[0]) if len(row2) > 0 else 0
        surg_actuals.append(cum_surg)

        # 全麻目標は平日のみ
        is_biz = (d_ts.weekday() < 5
                  and not _jpholiday.is_holiday(d_ts.date())
                  and not (d_ts.month == 12 and d_ts.day >= 29)
                  and not (d_ts.month == 1 and d_ts.day <= 3))
        cum_surg_tgt += surg_day_target if is_biz else 0
        surg_targets_cum.append(cum_surg_tgt)

    cumulative_progress = {
        "dates":           dates_str,
        "nadm_actual":     nadm_actuals,
        "nadm_target":     nadm_targets,
        "surg_actual":     surg_actuals,
        "surg_target":     surg_targets_cum,
        "rolling7_start":  rolling7_start.strftime("%Y-%m-%d"),
        "base_date":       base_date.strftime("%Y-%m-%d"),
        "nadm_wk_target":  float(nadm_wk_target),
        "surg_day_target": float(surg_day_target),
    }

    # ─ 2. ウォーターフォール（新入院 目標差分・直近7日）────
    r7_nadm      = rolling7_new_admission(adm, base_date)
    nadm_by_dept = r7_nadm["by_dept"]
    nadm_tgt_map = targets.get("new_admission", {}).get("dept", {})
    WATERFALL_HIDDEN = {"内科"}
    waterfall = []
    for dept in sorted(nadm_tgt_map.keys()):
        if dept in WATERFALL_HIDDEN:
            continue
        actual = nadm_by_dept.get(dept, 0)
        tgt    = nadm_tgt_map.get(dept)
        if tgt is None:
            continue
        gap = actual - tgt
        ach = round(actual / tgt * 100, 1) if tgt else None
        waterfall.append({
            "dept":        dept,
            "actual":      actual,
            "target":      round(float(tgt), 1),
            "gap":         round(float(gap), 1),
            "achievement": ach,
            "color":       "#16a34a" if gap >= 0 else "#dc2626",
        })
    waterfall.sort(key=lambda r: r["gap"])
    # 未達（gap<0）上位10件 + 達成（gap>=0）上位5件に絞る
    ng_rows = [r for r in waterfall if r["gap"] < 0][:10]
    ok_rows = [r for r in waterfall if r["gap"] >= 0][:5]
    waterfall = ng_rows + ok_rows

    # ─ 3. 全麻件数バブル散布図（直近7日ローリング）─────
    r7_surg_data = rolling7_surgery(surg, base_date)
    surg_by_dept  = r7_surg_data["by_dept"]

    # 前比較：さらに7日前の直近7日
    prev_base    = base_date - pd.Timedelta(days=7)
    prev_surg    = rolling7_surgery(surg, prev_base)
    prev_by_dept = prev_surg["by_dept"]

    bubble = {"x": [], "y": [], "size": [], "color": [], "labels": []}
    color_map = {"ok": "#16a34a", "warn": "#d97706", "ng": "#dc2626"}

    for dept in SURGERY_DISPLAY_DEPTS:
        actual  = surg_by_dept.get(dept, 0)
        tgt     = surgery_targets.get(dept)
        if tgt is None or actual == 0:
            continue
        ach   = round(actual / tgt * 100, 1)
        prev  = prev_by_dept.get(dept, 0)
        diff  = abs(actual - prev)
        size  = max(diff * 4 + 10, 12)
        st    = "ok" if ach >= 105 else ("warn" if ach >= 95 else "ng")
        bubble["x"].append(actual)
        bubble["y"].append(ach)
        bubble["size"].append(size)
        bubble["color"].append(color_map[st])
        bubble["labels"].append(dept)

    # ─ 4. 診療科ヒートマップ（直近12期間の新入院達成率・直近7日ローリング）─
    HEATMAP_HIDDEN = {"内科"}
    heat_depts = [d for d in depts if d in nadm_tgt_map and d not in HEATMAP_HIDDEN][:15]
    # 直近12期間：各期間の末日（base_date から7日刻みで遡る）
    heat_ends = [base_date - pd.Timedelta(weeks=i) for i in range(11, -1, -1)]
    heat_x    = [d.strftime("%m/%d") for d in heat_ends]

    # 集計範囲: 最も古い期間の開始日 〜 base_date
    heat_start_all = heat_ends[0] - pd.Timedelta(days=6)
    heat_adm = adm[(adm["日付"] >= heat_start_all) & (adm["日付"] <= base_date)
                   & adm["診療科名"].isin(heat_depts)].copy()

    # 各期間末日ごとに7日ウィンドウの合計を pivot で一括計算
    # 各行に「属する期間末日」を付与（各日は複数期間にまたがる場合があるが
    # ここでは各日を「その日が末日となる期間」に割り当てない代わりに
    # period_end ごとにフィルタして集計するシンプル方式を採用）
    heat_z = []
    for dept in heat_depts:
        tgt_d = nadm_tgt_map.get(dept)
        dept_adm = heat_adm[heat_adm["診療科名"] == dept]
        row_ach = []
        for end_d in heat_ends:
            start_d = end_d - pd.Timedelta(days=6)
            actual_wk = int(dept_adm[
                (dept_adm["日付"] >= start_d) & (dept_adm["日付"] <= end_d)
            ]["新入院患者数"].sum())
            ach = round(actual_wk / tgt_d * 100, 1) if tgt_d and tgt_d > 0 else None
            row_ach.append(ach)
        heat_z.append(row_ach)

    department_heatmap = {
        "x":     heat_x,
        "y":     heat_depts,
        "z":     heat_z,
        "ends":  [d.strftime("%Y-%m-%d") for d in heat_ends],
    }

    return {
        "cumulative_progress": cumulative_progress,
        "waterfall":           waterfall,
        "surgery_bubble":      bubble,
        "department_heatmap":  department_heatmap,
    }


# ────────────────────────────────────────────────────
# 看護師版 新チャートデータ（Phase 2）
# ────────────────────────────────────────────────────

def build_nurse_chart_data(adm: pd.DataFrame,
                            base_date: pd.Timestamp,
                            targets: dict) -> dict:
    """
    看護師版ダッシュボード用チャートデータを生成する。

    Returns:
        {
          "occupancy_heatmap": {...},
          "load_stack":        {...},
          "flow_balance":      {...},
        }
    """
    from .metrics import build_daily_series
    from .config import WARD_NAMES, WARD_HIDDEN

    all_wards = [c for c in adm["病棟コード"].dropna().unique()
                 if c not in WARD_HIDDEN]
    beds_map  = targets.get("inpatient", {}).get("ward_beds", {})
    inp_tgt_map = targets.get("inpatient", {}).get("ward", {})

    # ─ 1. 病棟別稼働ヒートマップ（直近8週） ─────────
    heat_weeks = []
    for i in range(7, -1, -1):
        bd_off = base_date - pd.Timedelta(weeks=i)
        wday   = bd_off.weekday()
        mon    = bd_off - pd.Timedelta(days=wday)
        heat_weeks.append(mon)

    heat_x = [mon.strftime("%m/%d") for mon in heat_weeks]

    # ── 一括集計：病棟×週の在院患者数（週平均）──────────
    heat_start = heat_weeks[0]
    heat_end   = min(heat_weeks[-1] + pd.Timedelta(days=6), base_date)
    heat_adm_w = adm[(adm["日付"] >= heat_start) & (adm["日付"] <= heat_end)
                     & adm["病棟コード"].isin(all_wards)].copy()
    heat_adm_w["週月曜"] = heat_adm_w["日付"] - pd.to_timedelta(
        heat_adm_w["日付"].dt.weekday, unit="D")
    # 週平均在院患者数のピボット
    pv_ward = (heat_adm_w.groupby(["病棟コード", "週月曜"])["在院患者数"]
               .mean().reset_index())
    pv_ward_pivot = pv_ward.pivot(index="病棟コード", columns="週月曜", values="在院患者数")

    occ_z, occ_wards = [], []
    for wcode in sorted(all_wards):
        beds = beds_map.get(wcode)
        if not beds:
            continue
        row_occ = []
        for mon in heat_weeks:
            if wcode in pv_ward_pivot.index and mon in pv_ward_pivot.columns:
                avg_inp = pv_ward_pivot.loc[wcode, mon]
                if pd.notna(avg_inp):
                    row_occ.append(round(float(avg_inp) / beds * 100, 1))
                else:
                    row_occ.append(None)
            else:
                row_occ.append(None)
        occ_z.append(row_occ)
        occ_wards.append(WARD_NAMES.get(wcode, wcode))

    occupancy_heatmap = {
        "x": heat_x,
        "y": occ_wards,
        "z": occ_z,
        "weeks": [mon.strftime("%Y-%m-%d") for mon in heat_weeks],
    }

    # ─ 2. 病棟別 入退院負荷積み上げ棒（昨日） ────────
    day = adm[adm["日付"] == base_date]
    stack_wards, stack_nadm, stack_emg, stack_tin, stack_dis, stack_tout = [], [], [], [], [], []
    for wcode in sorted(all_wards):
        row = day[day["病棟コード"] == wcode]
        if len(row) == 0:
            continue
        nadm_v  = int(row["新入院患者数"].sum())
        emg_v   = int(row["緊急入院患者数"].sum()) if "緊急入院患者数" in row.columns else 0
        tin_v   = int(row["転入患者数"].sum())
        dis_v   = int(row["退院合計"].sum())
        tout_v  = int(row["転出患者数"].sum())
        if nadm_v + tin_v + dis_v + tout_v == 0:
            continue
        stack_wards.append(WARD_NAMES.get(wcode, wcode))
        stack_nadm.append(nadm_v)
        stack_emg.append(emg_v)
        stack_tin.append(tin_v)
        stack_dis.append(dis_v)
        stack_tout.append(tout_v)

    load_stack = {
        "wards":    stack_wards,
        "nadm":     stack_nadm,
        "emg":      stack_emg,
        "tin":      stack_tin,
        "dis":      stack_dis,
        "tout":     stack_tout,
        "date":     base_date.strftime("%Y-%m-%d"),
    }

    # ─ 3. フローバランスチャート（直近24週 全体） ────
    s_nadm = build_daily_series(adm, "新入院患者数", display_filter=False)
    s_dis  = build_daily_series(adm, "退院合計",     display_filter=False)
    s_inp  = build_daily_series(adm, "在院患者数",   display_filter=False)

    start_24w = base_date - pd.Timedelta(weeks=24)

    def _weekly_agg_sum(df, start):
        df = df[df["日付"] >= start].copy()
        df["週開始"] = df["日付"] - pd.to_timedelta(df["日付"].dt.weekday, unit="D")
        wk = df.groupby("週開始")["値"].sum().reset_index()
        return wk["週開始"].dt.strftime("%Y-%m-%d").tolist(), [int(v) for v in wk["値"]]

    def _weekly_agg_mean(df, start):
        df = df[df["日付"] >= start].copy()
        df["週開始"] = df["日付"] - pd.to_timedelta(df["日付"].dt.weekday, unit="D")
        wk = df.groupby("週開始")["値"].mean().reset_index()
        return wk["週開始"].dt.strftime("%Y-%m-%d").tolist(), [round(float(v), 1) for v in wk["値"]]

    flow_dates_nadm, flow_nadm = _weekly_agg_sum(s_nadm, start_24w)
    flow_dates_dis,  flow_dis  = _weekly_agg_sum(s_dis,  start_24w)
    flow_dates_inp,  flow_inp  = _weekly_agg_mean(s_inp,  start_24w)

    flow_balance = {
        "dates_nadm": flow_dates_nadm,
        "nadm":       flow_nadm,
        "dates_dis":  flow_dates_dis,
        "dis":        flow_dis,
        "dates_inp":  flow_dates_inp,
        "inp":        flow_inp,
        "base_date":  base_date.strftime("%Y-%m-%d"),
    }

    return {
        "occupancy_heatmap": occupancy_heatmap,
        "load_stack":        load_stack,
        "flow_balance":      flow_balance,
    }
