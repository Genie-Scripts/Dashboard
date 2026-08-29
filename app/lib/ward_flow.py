"""
ward_flow.py — 病棟フロー（Track W: W1緊急受入シェア/W2転入依存度・純転棟収支/
                                    W3利用率×回転率象限マップ/W4週内変動係数）

detail.html「入退院バランス」タブ第4サブタブ「🏥 病棟フロー」用の集計+Plotly trace生成を
1モジュールに同居させる（surgery_ops.py / profit_translate.py と同形式）。単一エントリは
build_ward_flow_payload()。

母集団の共通規約:
  基底集合 = 病棟_表示==True（preprocess.py）。特例病棟（EMERGENCY_WARDS(04A/04C/07B)∪
  CRITICAL_CARE_WARDS(04B/04D)。判定は必ず config.unit_narration_kind("ward", code=...)
  を使う＝自前の集合リテラルは持たない。07B は config.EMERGENCY_WARDS に昇格済み
  （2026-08-29））の扱いはチャートごとに異なる:
    W1 = 除外（救急・重症ケアは緊急入院比率の解釈が一般病棟と異なるため）
    W2 = 含める（主役。ICU/HCUは他病棟からの転入が中心という業務実態そのもの）
    W3 = 除外（象限解釈＝利用率×回転率が一般病棟の前提でしか成立しないため）
    W4 = 別掲（本体の下に淡色トレース。除外はしない）
  病床数(ward_beds)が未設定の病棟は W3 のみ除外する（他Wは病床数を使わない）。
"""
from __future__ import annotations

import pandas as pd

from .charts import _base_layout
from .config import unit_narration_kind, is_operational_day

_MUTED = "#a6b3c4"   # 参照線・特例病棟の別掲トレース・象限ラベルに共通の薄グレー
_MAIN = "#0072B2"    # 状態色を使わない単色チャートの既定色
_UP = "#0e7a54"      # 発散バー: 正（受け手）
_DOWN = "#c4314b"    # 発散バー: 負（送り手）

# W2a 病棟別ラインの固定7色
_W2A_COLORS = ["#0072B2", "#E69F00", "#009E73", "#D55E00", "#56B4E9", "#CC79A7", "#8C7AA9"]

_W2_CAPTION = (
    "他の病棟から移ってきた患者さんが、その病棟の受け入れ全体にどれくらい含まれるかと、"
    "転入と転出の差です。患者さん一人ひとりの移動をたどったものではなく、"
    "日ごとの人数の集計から間接的に見ているもので、どの病棟からどの病棟へ移ったかまでは"
    "分かりません。"
)


def _short_name(name: str) -> str:
    """点ラベル用の短縮名（detail.html の _lvShort と同規則）。"""
    return str(name).replace("病棟", "").replace("センター", "C")


def _is_special(code: str) -> bool:
    return unit_narration_kind("ward", code=code) is not None


def _is_critical_care(code: str) -> bool:
    return unit_narration_kind("ward", code=code) == "critical_care"


def _ward_name_map(adm: pd.DataFrame) -> dict:
    """病棟コード → 表示名（病棟_表示==True のみ）。"""
    df = adm[adm["病棟_表示"]]
    if len(df) == 0:
        return {}
    return df.drop_duplicates("病棟コード").set_index("病棟コード")["病棟名"].to_dict()


def _window(df: pd.DataFrame, base_date: pd.Timestamp, weeks: int) -> pd.DataFrame:
    """base_date を右端（含む）とする直近 weeks 週（=weeks*7日）で絞り込む
    （surgery_ops._window と同じ「暦日トレイル」規約。週境界には揃えない）。"""
    base_date = pd.Timestamp(base_date).normalize()
    start = base_date - pd.Timedelta(weeks=weeks)
    return df[(df["日付"] > start) & (df["日付"] <= base_date)]


# ════════════════════════════════════════
# W1: 緊急受入シェア
# ════════════════════════════════════════

def emergency_share(adm: pd.DataFrame, base_date: pd.Timestamp) -> dict:
    """W1: 緊急受入シェア。窓=直近13週。特例4病棟は母集団から除外。
    分母（窓内新入院合計=入院+緊急入院）<30の病棟は非表示+excluded。
    参照線は「対象病棟合算」＝特例除外後の全対象病棟の集計比率（表示可否に関わらず合算）。
    """
    MIN_DENOM = 30
    base_date = pd.Timestamp(base_date).normalize()
    name_map = _ward_name_map(adm)
    win = _window(adm[adm["病棟_表示"]], base_date, 13)

    rows, excluded, role_excluded = [], [], []
    total_emg, total_denom = 0.0, 0.0
    if len(win) > 0:
        emg_by_ward = win.groupby("病棟コード")["緊急入院患者数"].sum()
        denom_by_ward = win.groupby("病棟コード")["新入院患者数"].sum()
        for code, denom in denom_by_ward.items():
            if _is_special(code):
                role_excluded.append(name_map.get(code, code))
                continue
            name = name_map.get(code, code)
            denom = float(denom)
            emg = float(emg_by_ward.get(code, 0.0))
            total_emg += emg
            total_denom += denom
            if denom < MIN_DENOM:
                excluded.append(name)
                continue
            rows.append({"name": name, "rate": round(emg / denom * 100, 1)})

    rows.sort(key=lambda r: -r["rate"])
    names = [r["name"] for r in rows]
    vals = [r["rate"] for r in rows]
    overall = round(total_emg / total_denom * 100, 1) if total_denom > 0 else None

    trace = {
        "x": vals, "y": names, "type": "bar", "orientation": "h",
        "marker": {"color": _MAIN},
        "hovertemplate": "%{y}: %{x}%<extra></extra>",
    }
    layout = _base_layout("", height=max(220, 26 * len(names) + 92))
    layout["xaxis"] = {"type": "linear", "gridcolor": "#DCE1E9", "ticksuffix": "%"}
    layout["yaxis"]["autorange"] = "reversed"
    layout["yaxis"]["automargin"] = True
    if overall is not None:
        layout["shapes"] = [{
            "type": "line", "xref": "x", "yref": "paper",
            "x0": overall, "x1": overall, "y0": 0, "y1": 1,
            "line": {"color": _MUTED, "width": 1.5, "dash": "dot"},
        }]

    return {
        "chart": {"traces": [trace], "layout": layout, "config": {"responsive": True}},
        "caption": (
            "病棟ごとに、新しく入院した患者さんのうち緊急入院がどれくらいを占めるかです。"
            "割合の高い低いは病棟の役割の違いを映していることが多く、"
            "それ自体の良し悪しを表すものではありません。"
            + (f"救急受け入れや重症ケアを役割とする{('・'.join(sorted(role_excluded)))}は対象外です。"
               if role_excluded else "")
        ),
        "excluded": sorted(excluded),
    }


# ════════════════════════════════════════
# W2a: 転入依存度
# ════════════════════════════════════════

def transfer_dependency(adm: pd.DataFrame, base_date: pd.Timestamp) -> dict:
    """W2a: 転入依存度（週次・月曜始まり・直近26完全週）の4週移動平均。
    分母は preprocess.py の「新入院患者数_病棟」列（=新入院患者数+転入患者数）をそのまま使う
    （自前で足し直さない=二重加算禁止）。対象=依存度上位5＋ICU(04B)＋HCU(04D)の最大7本固定。
    依存度は各病棟の窓内合計（Σ転入 / Σ新入院患者数_病棟）で順位付けする。
    有効週（分母>0の週）が13週未満の病棟は候補から外す（ICU/HCUの強制採用もこのゲートに従う）。
    分母0の週はNoneにして線を切る（4週移動平均で穴を埋めない）。
    """
    WEEKS = 26
    MIN_VALID_WEEKS = 13
    base_date = pd.Timestamp(base_date).normalize()
    name_map = _ward_name_map(adm)

    monday = base_date - pd.Timedelta(days=base_date.weekday())
    win_start = monday - pd.Timedelta(weeks=WEEKS)
    week_starts = [win_start + pd.Timedelta(weeks=i) for i in range(WEEKS)]
    win_end = week_starts[-1] + pd.Timedelta(days=6)

    df = adm[adm["病棟_表示"] & (adm["週開始"] >= week_starts[0]) & (adm["週開始"] <= week_starts[-1])]
    xs = [ws.strftime("%Y-%m-%d") for ws in week_starts]

    candidates = {}
    if len(df) > 0:
        den = df.groupby(["病棟コード", "週開始"])["新入院患者数_病棟"].sum()
        num = df.groupby(["病棟コード", "週開始"])["転入患者数"].sum()
        for code in df["病棟コード"].unique():
            ratios, valid = [], 0
            agg_num, agg_den = 0.0, 0.0
            for ws in week_starts:
                d = float(den.get((code, ws), 0.0))
                n = float(num.get((code, ws), 0.0))
                agg_num += n
                agg_den += d
                if d > 0:
                    ratios.append(round(n / d * 100, 2))
                    valid += 1
                else:
                    ratios.append(None)
            candidates[code] = {
                "ratios": ratios, "valid": valid,
                "dependency": round(agg_num / agg_den * 100, 1) if agg_den > 0 else 0.0,
            }

    eligible = {c: v for c, v in candidates.items() if v["valid"] >= MIN_VALID_WEEKS}
    ranked = sorted(eligible.keys(), key=lambda c: -eligible[c]["dependency"])
    selected = list(ranked[:5])
    for extra in ("04B", "04D"):
        if extra in eligible and extra not in selected:
            selected.append(extra)
    selected.sort(key=lambda c: -eligible[c]["dependency"])
    selected = selected[:7]

    traces, ward_meta = [], []
    for i, code in enumerate(selected):
        s = pd.Series(eligible[code]["ratios"], dtype="float64")
        ma = s.rolling(4, min_periods=1).mean()
        ma = ma.where(s.notna())  # 自週の分母が0なら移動平均も欠損にして線を切る
        ys = [round(float(v), 1) if pd.notna(v) else None for v in ma]
        name = name_map.get(code, code)
        legend_name = f"{name}（重症ケア）" if _is_critical_care(code) else name
        traces.append({
            "name": legend_name, "x": xs, "y": ys,
            "type": "scatter", "mode": "lines", "connectgaps": False,
            "line": {"color": _W2A_COLORS[i % len(_W2A_COLORS)], "width": 2.5},
        })
        ward_meta.append({"code": code, "name": name, "dependency": eligible[code]["dependency"]})

    layout = _base_layout("", height=380)
    layout["yaxis"]["ticksuffix"] = "%"

    return {
        "chart": {"traces": traces, "layout": layout, "config": {"responsive": True}},
        "caption": _W2_CAPTION,
        "wards": ward_meta,
    }


# ════════════════════════════════════════
# W2b: 純転棟収支
# ════════════════════════════════════════

def transfer_balance(adm: pd.DataFrame, base_date: pd.Timestamp) -> dict:
    """W2b: 純転棟収支（人/週換算）。窓=直近13週。特例4病棟を含む全表示病棟が対象
    （除外閾値なし）。正=受け手（緑）/負=送り手（赤）の発散横棒。"""
    WEEKS = 13
    base_date = pd.Timestamp(base_date).normalize()
    name_map = _ward_name_map(adm)
    win = _window(adm[adm["病棟_表示"]], base_date, WEEKS)

    rows = []
    if len(win) > 0:
        in_by_ward = win.groupby("病棟コード")["転入患者数"].sum()
        out_by_ward = win.groupby("病棟コード")["転出患者数"].sum()
        for code, name in name_map.items():
            net = (float(in_by_ward.get(code, 0.0)) - float(out_by_ward.get(code, 0.0))) / WEEKS
            rows.append({"name": name, "net": round(net, 1)})

    rows.sort(key=lambda r: -r["net"])
    names = [r["name"] for r in rows]
    vals = [r["net"] for r in rows]
    colors = [_UP if v >= 0 else _DOWN for v in vals]

    trace = {
        "x": vals, "y": names, "type": "bar", "orientation": "h",
        "marker": {"color": colors},
        "hovertemplate": "%{y}: %{x:+.1f}人/週<extra></extra>",
    }
    layout = _base_layout("", height=max(220, 26 * len(names) + 92))
    layout["xaxis"] = {
        "type": "linear", "gridcolor": "#DCE1E9",
        "zeroline": True, "zerolinecolor": "#888",
        "title": {"text": "人/週", "font": {"size": 10}},
    }
    layout["yaxis"]["autorange"] = "reversed"
    layout["yaxis"]["automargin"] = True

    return {
        "chart": {"traces": [trace], "layout": layout, "config": {"responsive": True}},
        "caption": _W2_CAPTION,
    }


# ════════════════════════════════════════
# W3: 利用率×回転率 象限マップ
# ════════════════════════════════════════

def utilization_turnover_quadrant(adm: pd.DataFrame, base_date: pd.Timestamp, targets: dict) -> dict:
    """W3: 利用率×回転率 象限マップ。窓=直近8週。特例4病棟と病床数(ward_beds)未設定の
    病棟を除外。x=在院日平均÷病床数×100(%)・y=(新入院+転入)週平均÷病床数(回/週/床)。
    基準線: x=固定85%・y=表示対象の中央値。
    """
    WEEKS = 8
    UTIL_THRESHOLD = 85.0
    base_date = pd.Timestamp(base_date).normalize()
    beds_map = targets.get("inpatient", {}).get("ward_beds", {})
    name_map = _ward_name_map(adm)
    win = _window(adm[adm["病棟_表示"]], base_date, WEEKS)

    rows, excluded = [], []
    if len(win) > 0:
        # 在院患者数は (日付×病棟コード) で合算してから平均する（1病棟×複数診療科の行重複対策）。
        census_daily = win.groupby(["日付", "病棟コード"])["在院患者数"].sum().reset_index()
        census_avg = census_daily.groupby("病棟コード")["在院患者数"].mean()
        adm_ward_total = win.groupby("病棟コード")["新入院患者数_病棟"].sum()
        for code, name in name_map.items():
            if _is_special(code):
                continue
            beds = beds_map.get(code)
            if not beds:
                excluded.append(name)
                continue
            util = round(float(census_avg.get(code, 0.0)) / beds * 100, 1)
            turnover = round(float(adm_ward_total.get(code, 0.0)) / WEEKS / beds, 2)
            rows.append({"name": name, "x": util, "y": turnover})

    ys = [r["y"] for r in rows]
    turnover_median = round(float(pd.Series(ys).median()), 2) if ys else None

    labels = [_short_name(r["name"]) for r in rows]
    trace = {
        "x": [r["x"] for r in rows], "y": ys, "text": labels,
        "type": "scatter", "mode": "markers+text", "textposition": "top center",
        "textfont": {"size": 10, "color": "#5A6A82"},
        "marker": {"size": 11, "color": _MAIN},
        "hovertemplate": "%{text}<br>利用率 %{x:.1f}%<br>回転 %{y:.2f} 回/週/床<extra></extra>",
    }
    layout = _base_layout("", height=420)
    layout["xaxis"] = {"type": "linear", "gridcolor": "#DCE1E9",
                        "title": {"text": "利用率(%)", "font": {"size": 10}}}
    layout["yaxis"]["title"] = {"text": "回転(回/週/床)", "font": {"size": 10}}

    shapes = [{
        "type": "line", "xref": "x", "yref": "paper",
        "x0": UTIL_THRESHOLD, "x1": UTIL_THRESHOLD, "y0": 0, "y1": 1,
        "line": {"color": _MUTED, "width": 1.5, "dash": "dash"},
    }]
    if turnover_median is not None:
        shapes.append({
            "type": "line", "xref": "paper", "yref": "y",
            "x0": 0, "x1": 1, "y0": turnover_median, "y1": turnover_median,
            "line": {"color": _MUTED, "width": 1.5, "dash": "dash"},
        })
    layout["shapes"] = shapes
    layout["annotations"] = [
        {"xref": "paper", "yref": "paper", "x": 0.98, "y": 0.98, "xanchor": "right", "yanchor": "top",
         "text": "利用高・回転高", "showarrow": False, "font": {"size": 10, "color": _MUTED}},
        {"xref": "paper", "yref": "paper", "x": 0.02, "y": 0.98, "xanchor": "left", "yanchor": "top",
         "text": "利用低・回転高＝空床が出やすい", "showarrow": False, "font": {"size": 10, "color": _MUTED}},
        {"xref": "paper", "yref": "paper", "x": 0.98, "y": 0.02, "xanchor": "right", "yanchor": "bottom",
         "text": "利用高・回転低＝滞在長め", "showarrow": False, "font": {"size": 10, "color": _MUTED}},
        {"xref": "paper", "yref": "paper", "x": 0.02, "y": 0.02, "xanchor": "left", "yanchor": "bottom",
         "text": "利用低・回転低", "showarrow": False, "font": {"size": 10, "color": _MUTED}},
    ]

    return {
        "chart": {"traces": [trace], "layout": layout, "config": {"responsive": True}},
        "caption": (
            "横は病床の使われ具合、縦は1床あたり1週間に何人を受け入れたかです。"
            "右下は在院が長めの傾向、左上は空床が出やすい傾向を示します。"
            "病棟ごとの診療内容の違いを映すため、位置そのものが良し悪しを表すものではありません。"
        ),
        "excluded": sorted(excluded),
        "thresholds": {"utilization": UTIL_THRESHOLD, "turnover_median": turnover_median},
    }


# ════════════════════════════════════════
# W4: 週内変動係数
# ════════════════════════════════════════

def weekday_cv(adm: pd.DataFrame, base_date: pd.Timestamp) -> dict:
    """W4: 週内変動係数（CV=σ÷μ, %）。窓=直近8週・平日（is_operational_day）の日次在院のみ。
    一般病棟=本体トレース／特例4病棟=淡色の別トレースで下に別掲（除外はしない）。
    μ==0 または平日データ<20日の病棟は非表示+excluded。
    """
    MIN_DAYS = 20
    base_date = pd.Timestamp(base_date).normalize()
    name_map = _ward_name_map(adm)
    win = _window(adm[adm["病棟_表示"]], base_date, 8)

    rows, excluded = [], []
    if len(win) > 0:
        # 在院患者数は (日付×病棟コード) で合算してから平日抽出・統計量を取る（行重複対策）。
        daily = win.groupby(["日付", "病棟コード"])["在院患者数"].sum().reset_index()
        daily = daily[daily["日付"].apply(is_operational_day)]
        for code, sub in daily.groupby("病棟コード"):
            name = name_map.get(code, code)
            if len(sub) < MIN_DAYS:
                excluded.append(name)
                continue
            mu = float(sub["在院患者数"].mean())
            if mu == 0:
                excluded.append(name)
                continue
            sigma = float(sub["在院患者数"].std())
            rows.append({"name": name, "cv": round(sigma / mu * 100, 1),
                         "special": _is_special(code)})

    general = sorted([r for r in rows if not r["special"]], key=lambda r: -r["cv"])
    special = sorted([r for r in rows if r["special"]], key=lambda r: -r["cv"])
    names_order = [r["name"] for r in general] + [r["name"] for r in special]

    trace_general = {
        "name": "一般病棟",
        "x": [r["cv"] for r in general], "y": [r["name"] for r in general],
        "type": "bar", "orientation": "h", "marker": {"color": _MAIN},
        "hovertemplate": "%{y}: CV %{x}%<extra></extra>",
    }
    trace_special = {
        "name": "救急受入・重症ケア病棟",
        "x": [r["cv"] for r in special], "y": [r["name"] for r in special],
        "type": "bar", "orientation": "h", "marker": {"color": _MUTED},
        "hovertemplate": "%{y}: CV %{x}%<extra></extra>",
    }
    layout = _base_layout("", height=max(240, 24 * len(names_order) + 100))
    layout["xaxis"] = {"type": "linear", "gridcolor": "#DCE1E9", "ticksuffix": "%"}
    layout["yaxis"]["type"] = "category"
    layout["yaxis"]["categoryorder"] = "array"
    layout["yaxis"]["categoryarray"] = names_order
    layout["yaxis"]["autorange"] = "reversed"
    layout["yaxis"]["automargin"] = True

    return {
        "chart": {"traces": [trace_general, trace_special], "layout": layout, "config": {"responsive": True}},
        "caption": (
            "平日の在院患者数が日によってどれくらい振れているかです"
            "（週末は別の指標で見ているため含めていません）。"
            "値が大きいほど日ごとの増減が大きく、病床の運用が読みにくい状態を表します。"
        ),
        "excluded": sorted(excluded),
    }


# ════════════════════════════════════════
# エントリポイント
# ════════════════════════════════════════

def build_ward_flow_payload(adm: pd.DataFrame, targets: dict, base_date) -> dict | None:
    """W1〜W4を集計し、detail.html「病棟フロー」サブタブ用の単一payloadを返す。
    adm・targets のいずれかが空なら None（呼び出し側で無害縮退・タブ非表示）。
    """
    if adm is None or len(adm) == 0 or not targets:
        return None
    base_date = pd.Timestamp(base_date).normalize()

    return {
        "w1": emergency_share(adm, base_date),
        "w2a": transfer_dependency(adm, base_date),
        "w2b": transfer_balance(adm, base_date),
        "w3": utilization_turnover_quadrant(adm, base_date, targets),
        "w4": weekday_cv(adm, base_date),
        "meta": {
            "base_date": base_date.strftime("%Y-%m-%d"),
            "weeks": {"w1": 13, "w2a": 26, "w2b": 13, "w3": 8, "w4": 8},
        },
    }
