"""
profit_translate.py — Track K: 係数読み替え（K1あと何件換算 / K2前年差ウォーターフォール / K3トルネード）
================================================================

detail.html「粗利タブ」用に、粗利の百万円ギャップを fit_profit_estimators() の
係数を使って「あと何件」「あと何人」に読み替える（K1）、前年同月比を要因分解する
（K2）、目標との差を科別に並べる（K3）の3チャートを1モジュールに同居させる
（surgery_ops.py / profit_estimate.py と同形式）。

【単位系】
  profit_breakdown の粗利は千円（data_loader.py）、係数は千円/件等、
  表示は百万円（profit.py の _row_to_dict は /1000）。
  換算式 = gap_百万円 × 1000 ÷ 係数_千円単位

【ラベル】
  係数 d=入院手術件数・β=外来手術件数（入外区分ベース、profit_estimate.py）。
  「全麻」ではなく「入院手術」「外来手術」と表示する。

【信頼度ガードレール】（科ごと・式単位で判定。4条件すべて満たした項目だけ数値を出す）
  G1: r2 is not None かつ r2 >= 0.70
  G2: n >= 10
  G3: 使う係数 > 0（負の項目だけ非表示・他項目は残す）
  G4: 換算結果 <= 直近12ヶ月の当該ドライバー月平均 × 1.0（超過は縮退）
  G1/G2 は式単位（外来式=gairai / 入院式=nyuin）で共有、G3/G4 は係数（項目）単位。
"""
from __future__ import annotations

import pandas as pd
from typing import Optional, Dict, List, Tuple

from .config import operational_days_between
from .charts import _base_layout
from .profit_estimate import fit_profit_estimators, _aggregate_monthly_drivers, _month_floor


# ── ガードレール定数 ──
MIN_R2 = 0.70
MIN_N = 10
LOOKBACK_MONTHS = 12

REASON_LOW_FIT   = "この科は月ごとのばらつきが大きいため、金額を件数に置き換えた目安は出していません。"
REASON_NEG_COEF  = "この項目は過去の関係がはっきりしないため、目安を出していません。"
REASON_OVER_CAP  = "目標との差が大きく、件数の置き換えでは説明しきれません。"
REASON_ACHIEVED  = "目標を上回っています。"

_OK = "#0e7a54"
_DR = "#c4314b"
_OTHER_COLOR = "#a6b3c4"


# ════════════════════════════════════════
# 共通ヘルパー
# ════════════════════════════════════════

def _item_guard(r2: Optional[float], n: int, coef: Optional[float]) -> Optional[str]:
    """G1/G2/G3 を判定。非表示なら理由文字列、表示可なら None。"""
    if r2 is None or r2 < MIN_R2 or (n or 0) < MIN_N:
        return REASON_LOW_FIT
    if coef is None or coef <= 0:
        return REASON_NEG_COEF
    return None


def _lookback_months(profit_breakdown: pd.DataFrame, lookback_months: int = LOOKBACK_MONTHS) -> list:
    """fit_profit_estimators と同じ月窓（末尾lookback_months ヶ月）を再現する。"""
    end_month = pd.to_datetime(profit_breakdown["月"]).apply(_month_floor).max()
    start_month = end_month - pd.DateOffset(months=lookback_months - 1)
    return pd.date_range(start_month, end_month, freq="MS").tolist()


def _driver_monthly_avgs(adm: pd.DataFrame, surg: pd.DataFrame, months: list) -> Dict[str, Dict[str, float]]:
    """科ごとの直近 lookback_months ヶ月ドライバー平均（G4判定用）。"""
    drv = _aggregate_monthly_drivers(adm, surg, months)
    out: Dict[str, Dict[str, float]] = {}
    for dept, g in drv.groupby("診療科名"):
        out[str(dept)] = {
            "入院手術件数": float(g["入院手術件数"].mean()),
            "外来手術件数": float(g["外来手術件数"].mean()),
            "新入院":       float(g["新入院"].mean()),
            "純在院延べ":   float(g["純在院延べ"].mean()),
        }
    return out


def _remaining_biz_days(base_date: pd.Timestamp) -> int:
    """base_date の翌日〜当該月末の営業日数（0なら残日なし）。"""
    month_end = base_date + pd.offsets.MonthEnd(0)
    rem_start = base_date + pd.Timedelta(days=1)
    if rem_start > month_end:
        return 0
    return operational_days_between(rem_start, month_end)


# ════════════════════════════════════════
# K1: あと何件換算
# ════════════════════════════════════════

def _k1_item(key: str, label: str, unit: str, coef: Optional[float],
             r2: Optional[float], n: int, gap_mm: float,
             driver_avg: Optional[float], rem_biz: int, with_pace: bool) -> dict:
    reason = _item_guard(r2, n, coef)
    value = None
    if reason is None:
        value = round(gap_mm * 1000.0 / coef, 1)
        avg = driver_avg if driver_avg is not None else 0.0
        if value > avg * 1.0:
            reason = REASON_OVER_CAP
            value = None
    pace = None
    pace_unit = None
    if reason is None and with_pace and rem_biz > 0:
        pace = round(value / rem_biz, 1)
        pace_unit = f"{unit}/営業日"
    return {
        "key": key, "label": label, "value": value, "unit": unit,
        "pace": pace, "pace_unit": pace_unit,
        "shown": reason is None, "reason": reason,
    }


def _k1_dept_row(name: str, gap_mm: float, est: Optional[dict],
                  driver_avgs: Dict[str, Dict[str, float]], rem_biz: int) -> dict:
    if gap_mm <= 0:
        return {"name": name, "gap_mm": round(gap_mm, 1), "items": [],
                "shown": False, "reason": REASON_ACHIEVED}

    est = est or {}
    g = est.get("gairai") or {}
    n_ = est.get("nyuin") or {}
    avgs = driver_avgs.get(name, {})

    items = [
        _k1_item("nyuin_op", "入院手術", "件", n_.get("d"), n_.get("r2"), n_.get("n", 0),
                 gap_mm, avgs.get("入院手術件数"), rem_biz, True),
        _k1_item("gairai_op", "外来手術", "件", g.get("beta"), g.get("r2"), g.get("n", 0),
                 gap_mm, avgs.get("外来手術件数"), rem_biz, True),
        _k1_item("new_adm", "新入院", "人", n_.get("e"), n_.get("r2"), n_.get("n", 0),
                 gap_mm, avgs.get("新入院"), rem_biz, False),
        _k1_item("bed_days", "在院", "人日", n_.get("f"), n_.get("r2"), n_.get("n", 0),
                 gap_mm, avgs.get("純在院延べ"), rem_biz, False),
    ]
    shown = any(it["shown"] for it in items)
    return {"name": name, "gap_mm": round(gap_mm, 1), "items": items,
            "shown": shown, "reason": None if shown else REASON_LOW_FIT}


def _k1_hospital_row(dept_rows: List[dict]) -> dict:
    """病院全体行 = 表示可能な科（shown=True）の換算値の単純合算。"""
    contributing = [r for r in dept_rows if r["shown"]]
    gap_mm = round(sum(r["gap_mm"] for r in contributing), 1) if contributing else 0.0

    # 病院全体はペースの概念を持たない（値は科別ペースの単純合算では意味が変わるため
    # value のみ合算し、pace/pace_unit は常に None にする）。
    item_defs = [
        ("nyuin_op", "入院手術", "件"),
        ("gairai_op", "外来手術", "件"),
        ("new_adm", "新入院", "人"),
        ("bed_days", "在院", "人日"),
    ]

    items = []
    for key, label, unit in item_defs:
        vals = []
        for row in contributing:
            for it in row["items"]:
                if it["key"] == key and it["shown"]:
                    vals.append(it["value"])
        if vals:
            total = round(sum(vals), 1)
            items.append({"key": key, "label": label, "value": total, "unit": unit,
                          "pace": None, "pace_unit": None, "shown": True, "reason": None})
        else:
            items.append({"key": key, "label": label, "value": None, "unit": unit,
                          "pace": None, "pace_unit": None, "shown": False, "reason": REASON_LOW_FIT})

    # 全科達成（gap<=0）のときは「ばらつき」ではなく達成の文言で縮退させる
    all_achieved = bool(dept_rows) and all(r["gap_mm"] <= 0 for r in dept_rows)
    return {"name": "病院全体", "gap_mm": gap_mm, "items": items,
            "shown": bool(contributing),
            "reason": None if contributing else (
                REASON_ACHIEVED if all_achieved else REASON_LOW_FIT)}


def _fmt1(v) -> str:
    return f"{v:.1f}" if v is not None else "—"


def _k1_caption(hospital: dict, depts_shown: int, depts_total: int) -> str:
    if not hospital or not hospital.get("shown"):
        if hospital and hospital.get("reason") == REASON_ACHIEVED:
            return (f"目標との差がある科はありません。"
                    f"各科とも補正後の目標を上回っています（全{depts_total}科中）。")
        return (f"換算できた科がありません（全{depts_total}科中）。"
                "月ごとのばらつきが大きい科は目安を出していません。")
    items = {it["key"]: it for it in hospital["items"]}
    main = (
        f"目標との差は{_fmt1(hospital['gap_mm'])}百万円です。"
        f"これは入院手術でおよそ{_fmt1(items.get('nyuin_op', {}).get('value'))}件、"
        f"外来手術でおよそ{_fmt1(items.get('gairai_op', {}).get('value'))}件、"
        f"新入院でおよそ{_fmt1(items.get('new_adm', {}).get('value'))}人、"
        f"在院でおよそ{_fmt1(items.get('bed_days', {}).get('value'))}人日にあたります。"
        "過去12か月の関係から計算した目安で、実際にはほかの要因も影響します。"
    )
    foot = (f"換算できた{depts_shown}科の合計です（全{depts_total}科中）。"
            "月ごとのばらつきが大きい科は目安を出していません。")
    return main + " " + foot


def _build_k1(profit_section: dict, estimators: dict,
              driver_avgs: Dict[str, Dict[str, float]], rem_biz: int) -> Tuple[dict, int, int]:
    ranking = profit_section.get("ranking") or []
    depts_total = len(ranking)

    dept_rows = []
    excluded = []
    for r in ranking:
        adj_t = r.get("adj_target")
        tgt = adj_t if adj_t is not None else r.get("target")
        if tgt is None:
            continue
        actual = r.get("actual") or 0.0
        gap_mm = float(tgt) - float(actual)
        name = r.get("name")
        row = _k1_dept_row(name, gap_mm, estimators.get(name), driver_avgs, rem_biz)
        dept_rows.append(row)
        if gap_mm > 0 and not row["shown"]:
            excluded.append(name)

    hospital = _k1_hospital_row(dept_rows)
    depts_shown = sum(1 for r in dept_rows if r["shown"])

    k1 = {
        "hospital": hospital,
        "depts": dept_rows,
        "excluded": sorted(excluded),
        "caption": _k1_caption(hospital, depts_shown, depts_total),
    }
    return k1, depts_shown, depts_total


# ════════════════════════════════════════
# K2: 前年差ウォーターフォール（病院全体のみ）
# ════════════════════════════════════════

_K2_COMPONENTS = (
    ("cal",    "暦"),
    ("outop",  "外来手術"),
    ("inop",   "入院手術"),
    ("newadm", "新入院"),
    ("bed",    "在院"),
)


def _k2_component_sums(estimators: dict, drv2: pd.DataFrame,
                        prev_month: pd.Timestamp, curr_month: pd.Timestamp) -> Dict[str, float]:
    """科ごとにガード通過分だけ係数×Δドライバーを積み上げる（千円単位）。"""
    sums = {k: 0.0 for k, _ in _K2_COMPONENTS}
    for dept, est in estimators.items():
        rows = drv2[drv2["診療科名"] == dept]
        prev_row = rows[rows["月"] == prev_month]
        curr_row = rows[rows["月"] == curr_month]
        if prev_row.empty or curr_row.empty:
            continue  # Δドライバー不明 → 全額が残差へ吸収される（後段の引き算で自動的に）

        d_biz = float(curr_row["営業日数"].iloc[0])       - float(prev_row["営業日数"].iloc[0])
        d_out = float(curr_row["外来手術件数"].iloc[0])   - float(prev_row["外来手術件数"].iloc[0])
        d_in  = float(curr_row["入院手術件数"].iloc[0])   - float(prev_row["入院手術件数"].iloc[0])
        d_new = float(curr_row["新入院"].iloc[0])         - float(prev_row["新入院"].iloc[0])
        d_bed = float(curr_row["純在院延べ"].iloc[0])     - float(prev_row["純在院延べ"].iloc[0])

        g = est.get("gairai") or {}
        r2g, ng = g.get("r2"), g.get("n", 0)
        if r2g is not None and r2g >= MIN_R2 and (ng or 0) >= MIN_N:
            alpha, beta = g.get("alpha"), g.get("beta")
            if alpha is not None and alpha > 0:
                sums["cal"] += alpha * d_biz
            if beta is not None and beta > 0:
                sums["outop"] += beta * d_out

        n_ = est.get("nyuin") or {}
        r2n, nn = n_.get("r2"), n_.get("n", 0)
        if r2n is not None and r2n >= MIN_R2 and (nn or 0) >= MIN_N:
            d_, e_, f_ = n_.get("d"), n_.get("e"), n_.get("f")
            if d_ is not None and d_ > 0:
                sums["inop"] += d_ * d_in
            if e_ is not None and e_ > 0:
                sums["newadm"] += e_ * d_new
            if f_ is not None and f_ > 0:
                sums["bed"] += f_ * d_bed
    return sums


def build_k2(profit_breakdown: pd.DataFrame, adm: pd.DataFrame, surg: pd.DataFrame,
             estimators: dict) -> Optional[dict]:
    pb = profit_breakdown.copy()
    pb["月"] = pd.to_datetime(pb["月"]).apply(_month_floor)
    curr_month = pb["月"].max()
    prev_month = curr_month - pd.DateOffset(months=12)

    adm_min_month = _month_floor(pd.Timestamp(adm["日付"].min()))
    if prev_month < adm_min_month:
        return None

    curr_total = float(pb[pb["月"] == curr_month]["粗利"].sum())
    prev_total = float(pb[pb["月"] == prev_month]["粗利"].sum())
    actual_delta_mm = (curr_total - prev_total) / 1000.0

    drv2 = _aggregate_monthly_drivers(adm, surg, [prev_month, curr_month])
    sums_千円 = _k2_component_sums(estimators, drv2, prev_month, curr_month)
    comp_mm = {k: v / 1000.0 for k, v in sums_千円.items()}

    prev_r = round(prev_total / 1000.0, 1)
    curr_r = round(curr_total / 1000.0, 1)
    comp_r = {k: round(v, 1) for k, v in comp_mm.items()}
    # 残差は「丸め後の実測Δ − 丸め後の成分和」として定義し、恒等式を丸め後同士で厳密に保つ。
    residual_r = round(round(actual_delta_mm, 1) - sum(comp_r.values()), 1)

    labels = ["前年実績"] + [lbl for _, lbl in _K2_COMPONENTS] + ["その他", "当年実績"]
    values = [prev_r] + [comp_r[k] for k, _ in _K2_COMPONENTS] + [residual_r, curr_r]

    cum = prev_r
    bases = [0.0]
    for v in values[1:-1]:
        bases.append(cum)
        cum += v
    bases.append(0.0)

    is_other = [False] + [False] * len(_K2_COMPONENTS) + [True, False]
    inc_x, inc_y, inc_base = [], [], []
    dec_x, dec_y, dec_base = [], [], []
    abs_x, abs_y, abs_base = [], [], []
    for lbl, v, b, oth in zip(labels, values, bases, is_other):
        if oth or lbl in ("前年実績", "当年実績"):
            abs_x.append(lbl); abs_y.append(v); abs_base.append(b)
        elif v >= 0:
            inc_x.append(lbl); inc_y.append(v); inc_base.append(b)
        else:
            dec_x.append(lbl); dec_y.append(v); dec_base.append(b)

    traces = [
        {"name": "増加", "x": inc_x, "y": inc_y, "base": inc_base, "type": "bar",
         "marker": {"color": _OK}, "hovertemplate": "%{x}: %{y:+.1f}百万円<extra></extra>"},
        {"name": "減少", "x": dec_x, "y": dec_y, "base": dec_base, "type": "bar",
         "marker": {"color": _DR}, "hovertemplate": "%{x}: %{y:+.1f}百万円<extra></extra>"},
        {"name": "実績・その他", "x": abs_x, "y": abs_y, "base": abs_base, "type": "bar",
         "marker": {"color": _OTHER_COLOR}, "hovertemplate": "%{x}: %{y:.1f}百万円<extra></extra>"},
    ]

    layout = _base_layout("", height=360)
    layout["xaxis"] = {"type": "category", "gridcolor": "#DCE1E9", "categoryarray": labels}
    layout["yaxis"]["title"] = {"text": "百万円", "font": {"size": 10}}

    shapes = []
    tops = [b + v for b, v in zip(bases, values)]
    for i in range(len(labels) - 1):
        y_line = tops[i]
        shapes.append({
            "type": "line", "xref": "x", "yref": "y",
            "x0": labels[i], "x1": labels[i + 1], "y0": y_line, "y1": y_line,
            "line": {"color": "#dfe5ed", "width": 1, "dash": "dot"},
        })
    layout["shapes"] = shapes

    caption = (
        "前年同月からの差を、要因ごとに分けたものです。"
        "いちばん左の『暦』は営業日数の違いによる分です。"
        "『その他』には、ここで扱っていない要因と計算の誤差が入ります。"
    )

    return {
        "chart": {"traces": traces, "layout": layout, "config": {"responsive": True}},
        "caption": caption,
        "months": {"current": curr_month.strftime("%Y-%m"), "prev": prev_month.strftime("%Y-%m")},
    }


# ════════════════════════════════════════
# K3: トルネード（目標との差・確定最新月）
# ════════════════════════════════════════

def build_k3(profit_section: dict) -> Optional[dict]:
    ranking = profit_section.get("ranking") or []
    if not ranking:
        return None
    base_month = (profit_section.get("kpi") or {}).get("base_month")

    valid = []
    other_sum = 0.0
    other_n = 0
    for r in ranking:
        actual = r.get("actual")
        if actual is None:
            continue
        adj_t = r.get("adj_target")
        if adj_t is None:
            other_sum += float(actual)
            other_n += 1
            continue
        valid.append({"name": r.get("name"), "value": round(float(actual) - float(adj_t), 1)})

    if not valid and other_n == 0:
        return None

    valid.sort(key=lambda x: -x["value"])
    if len(valid) > 16:
        top = valid[:8]
        bottom = valid[-8:]
        middle = valid[8:-8]
        other_sum += sum(x["value"] for x in middle)
        other_n += len(middle)
        shown = top + bottom
    else:
        shown = list(valid)

    if other_n > 0:
        shown = shown + [{"name": f"その他{other_n}科", "value": round(other_sum, 1)}]

    shown.sort(key=lambda x: -x["value"])
    names = [x["name"] for x in shown]
    values = [x["value"] for x in shown]
    colors = [_OK if v >= 0 else _DR for v in values]

    trace = {
        "x": values, "y": names, "type": "bar", "orientation": "h",
        "marker": {"color": colors},
        "hovertemplate": "%{y}: %{x:+.1f}百万円<extra></extra>",
    }
    layout = _base_layout("", height=max(220, 26 * len(names) + 92))
    layout["xaxis"] = {"type": "linear", "gridcolor": "#DCE1E9",
                        "zeroline": True, "zerolinecolor": "#888"}
    layout["yaxis"]["autorange"] = "reversed"
    layout["yaxis"]["automargin"] = True

    if base_month:
        ts = pd.Timestamp(str(base_month) + "-01")
        month_label = f"{ts.year}年{ts.month}月"
    else:
        month_label = "直近月"

    caption = (
        f"{month_label}の実績が、補正後の目標とどれだけ離れていたかを科ごとに並べたものです。"
        "右が目標を上回った分、左が届かなかった分です。"
        "上の『粗利を上げている/下げている科』は過去との比較で、こちらは目標との比較です。"
    )

    return {
        "chart": {"traces": [trace], "layout": layout, "config": {"responsive": True}},
        "caption": caption,
        "base_month": base_month,
    }


# ════════════════════════════════════════
# エントリポイント
# ════════════════════════════════════════

def build_translate_payload(profit_breakdown: Optional[pd.DataFrame],
                             adm: Optional[pd.DataFrame],
                             surg: Optional[pd.DataFrame],
                             profit_section: Optional[dict],
                             base_date) -> Optional[dict]:
    """K1（あと何件換算）/ K2（前年差ウォーターフォール）/ K3（トルネード）を集計し、
    detail.html「粗利タブ」用の単一payloadを返す。前提データが無ければ None。
    """
    if profit_breakdown is None or len(profit_breakdown) == 0:
        return None
    if profit_section is None:
        return None
    if adm is None or surg is None or len(adm) == 0 or len(surg) == 0:
        return None

    base_date = pd.Timestamp(base_date).normalize()
    estimators = fit_profit_estimators(profit_breakdown, adm, surg)

    months = _lookback_months(profit_breakdown)
    driver_avgs = _driver_monthly_avgs(adm, surg, months)
    rem_biz = _remaining_biz_days(base_date)

    k1, depts_shown, depts_total = _build_k1(profit_section, estimators, driver_avgs, rem_biz)
    k2 = build_k2(profit_breakdown, adm, surg, estimators)
    k3 = build_k3(profit_section)

    return {
        "k1": k1,
        "k2": k2,
        "k3": k3,
        "meta": {
            "unit": "百万円",
            "lookback_months": LOOKBACK_MONTHS,
            "guard": {"min_n": MIN_N, "min_r2": MIN_R2},
            "depts_shown": depts_shown,
            "depts_total": depts_total,
        },
    }
