"""dept_report.py — 部門別レポートPDF用 コンテキスト構築（種別別・グラフ多用版・A4 1枚）。

generate_html / html_builder と同じ前処理済みデータ(adm, surg, targets, surg_targets,
profit_monthly)から、診療科版・病棟版それぞれ「1部門=1コンテキスト」を組み立てる。

2026-06-20 構成見直し（spec/dept_report_v3_mock.html・memory: project_dept_report_pdf）:
  グラフ5パーツ A.在院 B.新入院 C.全麻手術 D.粗利 E.曜日プロファイル を、ユニット種別ごとの
  優先順で配置する。優先1位=全幅ヒーロー、以降は読み順(=優先順)で半幅2列、⭐一手が端を埋める。
    外科系診療科: C, D, E, B, A   内科系診療科: A, D, B, E   病棟: A(病床利用率), B, E
  - トレンドは当年線＋前年同期(点線)＋目標/定員(破線)＋達成率バッジ(緑≥100/橙<100)。
  - 粗利は診療科のみ・月次cadence(確報ベース)。病棟は粗利/手術なし。種別=SURGERY_DISPLAY_DEPTS。
  - SVGはバッチPDF化のJS実行を避け Python 側で静的描画（render_trend_svg / _render_dow_svg）。
"""
from __future__ import annotations

import math
from datetime import timedelta
from typing import Optional

import pandas as pd

from .config import SURGERY_DISPLAY_DEPTS
from .metrics import (
    weekend_census_retention, rolling7_inpatient_avg,
    rolling7_new_admission, rolling7_surgery,
    build_daily_series,
)
from .charts import build_dow_unit_detail, _dow_unit_candidates
from .ai_narrative import (
    narrate_leveling_actions,
    _q_friday, _q_weekend_adm, _q_state_trend,
)
from .hospital_summary import render_trend_svg
from .profit_estimate import fit_profit_estimators, project_dept_monthend

WK = ["月", "火", "水", "木", "金", "土", "日"]
WEEKS = 12
PREVYEAR_DAYS = 364   # 52週=曜日合わせ

# 配色（dept.html renderDowProfile / hospital_summary と一致）
C_OUT = "#e07a5f"   # 退院/流出
C_IN  = "#3d5a80"   # 入院/流入
C_CEN = "#2a9d8f"   # 在院指数
C_LN  = "#eef2f7"
C_AX  = "#9daab8"
C_INK = "#5f7084"

# グラフパーツ（A-E）の色・アイコン・窓ラベル
PART_COL = {"A": "#2a9d8f", "B": "#3d5a80", "C": "#2b6cb0", "D": "#bf8a2e"}
PART_ICON = {"A": "🛏", "B": "➕", "C": "🔪", "D": "💴", "E": "📊"}
# 窓ラベルは公開版ダッシュボードと同方式（在院/新入院/手術=7日移動平均）。
PART_WIN = {"A": "12週・7日移動平均", "B": "12週・7日移動平均（件/日）",
            "C": "12週・7日移動平均（件/日）", "D": "12か月・月次（確報ベース）",
            "E": "曜日別 日平均（直近8週）"}

# 種別ごとの表示優先順（上＝主役＝全幅ヒーロー）
TYPE_ORDER = {
    "surgical": ["C", "D", "E", "B", "A"],
    "internal": ["A", "D", "B", "E"],
    "ward":     ["A", "B", "E"],
}
TYPE_LABEL = {"surgical": "外科系・診療科版", "internal": "内科系・診療科版", "ward": "病棟版"}
TYPE_SUBTITLE = {"surgical": "診療科パフォーマンスレポート",
                 "internal": "診療科パフォーマンスレポート",
                 "ward": "病棟パフォーマンスレポート"}
TYPE_PRIO_TXT = {
    "surgical": "C 手術 → D 粗利 → E 曜日 → B 新入院 → A 在院",
    "internal": "A 在院 → D 粗利 → B 新入院 → E 曜日",
    "ward":     "A 病床利用率 → B 新入院 → E 曜日",
}


# ════════════════════════════════════════════════════════════
# 曜日プロファイル SVG（renderDowProfile の Python 静的版）
# ════════════════════════════════════════════════════════════
def _render_dow_svg(discharge: list, admission: list, census: list) -> str:
    """退院(橙)・入院(紺)の棒＋在院指数(緑・平日平均=100)の谷ラインを SVG 文字列で返す。

    discharge / admission / census は曜日別[月..日]の日平均（dow_unit_detail の w8）。
    左軸=人/日（データに応じてnice上限）、右軸=在院指数（renderDowProfile と同じ範囲ロジック）。
    """
    W, H, L, R, T, B = 720, 270, 46, 672, 14, 232
    n = 7
    bars = [v for v in (list(discharge) + list(admission)) if v is not None]
    mx = max(bars) if bars else 1.0
    step = 2 if mx <= 10 else 4 if mx <= 20 else 5 if mx <= 30 else 10
    yMax = max(step, math.ceil(mx / step) * step)

    wk = census[0:5]
    base = sum(wk) / 5 if sum(wk) > 0 else 0
    idx = [(c / base * 100) if base > 0 else 100 for c in census]
    lo, hi = (min(idx), max(idx)) if idx else (90, 100)
    r0 = min(70, math.floor((lo - 6) / 10) * 10)
    r1 = max(112, math.ceil((hi + 4) / 4) * 4)

    def yL(v): return B - (v / yMax) * (B - T)
    def yR(v): return B - ((v - r0) / (r1 - r0)) * (B - T)
    gw = (R - L) / n
    bw = 17
    el: list[str] = []

    def line(x1, y1, x2, y2, stroke, w=1, dash=None, op=1.0):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        o = f' opacity="{op}"' if op != 1.0 else ""
        el.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                  f'stroke="{stroke}" stroke-width="{w}"{d}{o}/>')

    def text(x, y, s, size, fill, anchor="middle", weight=400):
        el.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" '
                  f'text-anchor="{anchor}" font-weight="{weight}">{s}</text>')

    def rect(x, y, w, h, fill):
        el.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{max(h, 0):.1f}" '
                  f'rx="2" fill="{fill}"/>')

    # 左軸グリッド＋目盛
    v = 0
    while v <= yMax + 1e-6:
        line(L, yL(v), R, yL(v), C_LN, 1)
        text(L - 7, yL(v) + 3.5, int(v), 10, C_AX, "end")
        v += step
    # 在院=100 基準線
    line(L, yR(100), R, yR(100), C_CEN, 1, "3 3", 0.5)
    text(R + 4, yR(100) + 3.5, "100", 9, C_CEN, "start", 700)
    text(L - 30, T + 4, "人/日", 9.5, C_AX, "start", 700)

    # 棒＋値ラベル＋曜日
    for i in range(n):
        cx = L + gw * (i + 0.5)
        for val, off, col in ((discharge[i], -bw - 2, C_OUT), (admission[i], 2, C_IN)):
            rect(cx + off, yL(val), bw, B - yL(val), col)
            if val >= 0.5:
                text(cx + off + bw / 2, yL(val) - 3, f"{val:.1f}", 8.5, col, "middle", 800)
        wc = C_IN if i == 5 else ("#c4314b" if i == 6 else C_INK)
        text(cx, B + 17, WK[i], 12, wc, "middle", 800)

    # 在院指数 ライン＋マーカー＋値
    pts = [(L + gw * (i + 0.5), yR(idx[i])) for i in range(n)]
    d = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts)
    el.append(f'<path d="{d}" fill="none" stroke="{C_CEN}" stroke-width="2.5"/>')
    for i, (x, y) in enumerate(pts):
        el.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.6" fill="{C_CEN}"/>')
        text(x, y - 8, round(idx[i]), 9.5, C_CEN, "middle", 900)

    return (f'<svg viewBox="0 0 {W} {H}" width="100%" style="display:block">'
            + "".join(el) + "</svg>")


# ════════════════════════════════════════════════════════════
# per-unit トレンド系列（当年＋前年同期）── hospital_summary._ma_series の科/病棟絞り版
# ════════════════════════════════════════════════════════════
def _ma_from_daily(s, base_date, window, agg) -> dict:
    """日次系列(列=日付,値)を window 日 rolling（mean/sum）し、直近12週＋前年同期を返す。

    公開版ダッシュボード（charts.add_moving_average / _trend_dict）と同じ
    『日次系列の移動平均』方式。日付は連続している前提（手術は0埋めで連続化して渡す）。
    """
    s = s.sort_values("日付")
    roll = s["値"].rolling(window, min_periods=1)
    vmap = dict(zip(s["日付"], roll.sum() if agg == "sum" else roll.mean()))
    start = base_date - timedelta(days=WEEKS * 7 - 1)
    cur_dates = [d for d in s["日付"] if start <= d <= base_date]
    cur = [round(vmap[d], 1) for d in cur_dates]
    prev = [round(vmap[d - timedelta(days=PREVYEAR_DAYS)], 1)
            if (d - timedelta(days=PREVYEAR_DAYS)) in vmap else None for d in cur_dates]
    return {"dates": [d.strftime("%m/%d") for d in cur_dates], "cur": cur, "prev": prev}


def _unit_ma_series(adm, col, base_date, group_col, group_val, window, agg) -> dict:
    """指定ユニットの col を window 日 rolling（公開版と同方式）。直近12週＋前年同期。"""
    s = build_daily_series(adm, col, group_col=group_col, group_val=group_val)
    if s.empty:
        return {"dates": [], "cur": [], "prev": []}
    return _ma_from_daily(s, base_date, window, agg)


def _unit_surg_ma7_series(surg, base_date, dept) -> dict:
    """指定診療科の全麻・7日移動平均（件/日）・直近12週＋前年同期。

    公開版ダッシュボードの診療科ドリル（_trend_dict の ma7）と同方式。手術なし日は
    0 埋めで暦日を連続化してから 7日移動平均（KPIの rolling7_surgery と同じ7暦日窓）。
    """
    sd = surg[(surg["実施診療科"] == dept) & surg["全麻"]]
    daily = sd.groupby("手術実施日").size()
    if daily.empty:
        return {"dates": [], "cur": [], "prev": []}
    idx = pd.date_range(daily.index.min(), base_date, freq="D")
    daily = daily.reindex(idx, fill_value=0)
    ser = pd.DataFrame({"日付": idx, "値": daily.to_numpy()})
    return _ma_from_daily(ser, base_date, 7, "mean")


def _unit_profit_series(profit_monthly, name, base_date,
                        estimators=None, adm=None, surg=None) -> Optional[dict]:
    """指定診療科の月次粗利（百万円）・直近12か月＋前年同期。目標線=月次目標、達成率=最新月。

    estimators/adm/surg があれば、確報の末尾に **当月見込み（暫定）スロット** を1つ足す
    （proj=百万円。実線は確報で止め、点線で見込みへつなぐ）。
    """
    if profit_monthly is None or len(profit_monthly) == 0:
        return None
    df = profit_monthly[profit_monthly["診療科名"] == name].sort_values("月")
    if df.empty:
        return None
    base_m = base_date.to_period("M").to_timestamp()
    rows = df[df["月"] <= base_m]
    if rows.empty:
        rows = df
    rows = rows.tail(WEEKS)
    months = list(rows["月"])
    pmap = dict(zip(df["月"], df["粗利"]))
    dates = [m.strftime("%-m月") for m in months]
    cur = [round(pmap[m] / 1000, 1) for m in months]
    prev = [round(pmap[m - pd.DateOffset(years=1)] / 1000, 1)
            if (m - pd.DateOffset(years=1)) in pmap else None for m in months]
    last = rows.iloc[-1]
    tgt = last["月次目標"]
    ref = round(tgt / 1000, 1) if pd.notna(tgt) else None
    rate = last["達成率"] if pd.notna(last["達成率"]) else None

    # 当月見込み（暫定）スロット
    proj = None
    if estimators and adm is not None:
        p = project_dept_monthend(estimators, adm, surg, base_date, name,
                                  profit_monthly=profit_monthly)
        if p and p["month"] not in months:
            pm = p["month"]
            dates.append(f"{pm.strftime('%-m月')}(見込)")
            cur.append(None)
            prev.append(round(pmap[pm - pd.DateOffset(years=1)] / 1000, 1)
                        if (pm - pd.DateOffset(years=1)) in pmap else None)
            proj = p["value"]

    return {"dates": dates, "cur": cur, "prev": prev, "ref": ref, "rate": rate,
            "latest": months[-1], "proj": proj, "proj_month": (p["month"] if proj else None)}


# ════════════════════════════════════════════════════════════
# 達成率バッジ
# ════════════════════════════════════════════════════════════
def _rate(actual, target):
    if target in (None, 0) or actual is None:
        return None
    return round(actual / target * 100, 1)


def _ach_badge(actual, target, prefix="達成率 "):
    pct = _rate(actual, target)
    if pct is None:
        return None
    return (f"{prefix}{pct:g}%", "ok" if pct >= 100 else "wr")


# ════════════════════════════════════════════════════════════
# この期間の一手（oMLX未起動時の定型フォールバック・データ適応）
# ════════════════════════════════════════════════════════════
def _fallback_move(unit: dict, dd: Optional[dict], entity: str) -> dict:
    """narrate_leveling_actions が None（oMLX未起動/失敗）のときの定型文。"""
    state = _q_state_trend(unit.get("retention"), unit.get("room_delta_4w"))
    fri = _q_friday(dd)
    adm = _q_weekend_adm(dd)
    fri_strong = bool(fri) and "強い" in fri
    adm_weak = bool(adm) and "乏しい" in adm
    room = unit.get("room_per_week", 0) or 0

    if room <= 0.5:
        body = "週末も平日とほぼ同じ在院を保てています。今の入退院のリズムが手本になっています。"
        action = "現状維持。週末の入退院リズムをこのまま継続しましょう。"
        return {"body": body, "action": action}

    causes = []
    if fri_strong:
        causes.append("退院が金曜に集中し")
    if adm_weak:
        causes.append("週末の入院補充が乏しく")
    if causes:
        body = "".join(c if i == 0 else "、" + c for i, c in enumerate(causes)) \
               + "、週末に在院が落ち込みやすい構造です。タイミングの平準化で取り戻せる余地があります。"
    else:
        body = f"{state}。週末のタイミングを少し整えると、平日に積み上げた在院を保ちやすくなります。"

    disperse = "金曜に寄った退院を月〜木へ少し分散" if entity == "dept" else "相乗り科の金曜退院を平日へ分散"
    refill = "予定入院を週後半〜週末へ寄せて空床を補充" if entity == "dept" else "週末の入院受け入れを強化して空床を補充"
    if fri_strong and not adm_weak:
        action = f"{disperse}（退院の平準化を主に）。在院日数は延ばさず、回転で取り戻す。"
    elif adm_weak and not fri_strong:
        action = f"{refill}（週末入院での補充を主に）。"
    else:
        action = f"{disperse}し、あわせて{refill}。"
    return {"body": body, "action": action}


# ════════════════════════════════════════════════════════════
# チャート組立（描画はヒーロー/半幅の高さ確定後）
# ════════════════════════════════════════════════════════════
def _trend_part(kind, name, series, ref, ref_label, unit, badge, note="") -> Optional[dict]:
    """A/B/C/D 共通のトレンドパーツ仕様。series が空なら None。"""
    if not series or not series.get("cur") or all(v is None for v in series["cur"]):
        return None
    return {"kind": kind, "icon": PART_ICON[kind], "name": name, "badge": badge, "note": note,
            "is_dow": False, "_data": series, "_ref": ref, "_ref_label": ref_label,
            "_unit": unit, "_win": PART_WIN[kind], "_color": PART_COL[kind]}


def _dow_part(dd) -> Optional[dict]:
    if not dd:
        return None
    svg = _render_dow_svg(dd["discharge"]["w8"], dd["admission"]["w8"], dd["census"]["w8"])
    return {"kind": "E", "icon": PART_ICON["E"], "name": "曜日プロファイル", "badge": None,
            "note": "", "is_dow": True, "svg": svg}


def _build_parts(adm, surg, base_date, entity, name, code, dd, r7_inp, r7_nadm,
                 r7_surg, targets, surg_targets, profit_series) -> dict:
    """利用可能なグラフパーツ {A,B,C,D,E} を作る（無いものは欠落）。

    在院/新入院/手術の窓は公開版ダッシュボードと統一（7日移動平均、KPIは直近7日）。
    """
    is_ward = entity == "ward"
    by = "by_ward" if is_ward else "by_dept"
    tgt_axis = "ward" if is_ward else "dept"
    parts = {}

    r7 = r7_inp[by].get(code)
    inp_tgt = targets.get("inpatient", {}).get(tgt_axis, {}).get(code)

    # A: 在院（病棟は病床利用率 = 在院7日MA ÷ 稼働床 ×100）
    if is_ward:
        beds = targets.get("inpatient", {}).get("ward_beds", {}).get(code)
        cen = _unit_ma_series(adm, "在院患者数", base_date, "病棟コード", code, 7, "mean")
        if beds:
            scale = lambda v: round(v / beds * 100, 1) if v is not None else None
            cen = {**cen, "cur": [scale(v) for v in cen["cur"]], "prev": [scale(v) for v in cen["prev"]]}
            util_now = _rate(r7, beds)
            badge = (f"対定員 {util_now:g}%", "ok" if (util_now or 0) >= 90 else "wr") if util_now else None
            parts["A"] = _trend_part("A", "病床利用率", cen, 100, "定員100%", "%", badge)
    else:
        s = _unit_ma_series(adm, "在院患者数", base_date, "診療科名", name, 7, "mean")
        parts["A"] = _trend_part("A", "在院患者数", s, inp_tgt or 0, f"目標{inp_tgt:g}" if inp_tgt else "",
                                 "人", _ach_badge(r7, inp_tgt))

    # B: 新入院（7日移動平均=件/日、目標=週次÷7。KPI/バッジは直近7日累計）。軸で列が変わる
    na = r7_nadm[by].get(code)
    na_tgt = targets.get("new_admission", {}).get(tgt_axis, {}).get(code)
    daily_na_tgt = round(na_tgt / 7, 1) if na_tgt else 0
    b_col = "新入院患者数_病棟" if is_ward else "新入院患者数"
    b_grp, b_val = ("病棟コード", code) if is_ward else ("診療科名", name)
    bs = _unit_ma_series(adm, b_col, base_date, b_grp, b_val, 7, "mean")
    parts["B"] = _trend_part("B", "新入院患者数", bs, daily_na_tgt,
                             f"目標{daily_na_tgt:g}" if daily_na_tgt else "", "件/日",
                             _ach_badge(na, na_tgt))

    # C: 全麻手術（外科系診療科のみ）。7日移動平均=件/日、目標=週次÷7。バッジは直近7日累計
    if not is_ward and name in SURGERY_DISPLAY_DEPTS:
        cs = _unit_surg_ma7_series(surg, base_date, name)
        surg_tgt = surg_targets.get(name) if isinstance(surg_targets, dict) else None
        daily_tgt = round(surg_tgt / 7, 1) if surg_tgt else 0
        sv = r7_surg["by_dept"].get(name, 0)
        parts["C"] = _trend_part("C", "全身麻酔手術", cs, daily_tgt,
                                 f"目標{daily_tgt:g}" if daily_tgt else "", "件/日",
                                 _ach_badge(sv, surg_tgt))

    # D: 粗利（診療科のみ・確報＋当月見込み）
    if not is_ward and profit_series:
        rate = profit_series["rate"]
        if profit_series.get("proj") is not None:
            note = (f"実線=確報(最新{profit_series['latest'].strftime('%-m月')})／"
                    f"点線={profit_series['proj_month'].strftime('%-m月')}は診療実績ベースの見込（暫定）")
        else:
            note = f"確報ベース・最新 {profit_series['latest'].strftime('%Y年%-m月')}"
        badge = (f"達成率 {rate:g}%", "ok" if (rate or 0) >= 100 else "wr") if rate is not None else None
        ref = profit_series["ref"] or 0
        parts["D"] = _trend_part("D", "粗利", profit_series, ref,
                                 f"目標{ref:g}" if ref else "", "百万円", badge, note=note)

    # E: 曜日プロファイル
    e = _dow_part(dd)
    if e:
        parts["E"] = e
    return parts


# ════════════════════════════════════════════════════════════
# KPI band（種別別・上段4枚）
# ════════════════════════════════════════════════════════════
def _kpi(label, sub, val, unit, *, lead=False, tgt=None, ok=None):
    """KPIカード。tgt=実績の右に小さく併記する目標値文字列、ok=達成可否(色)。"""
    return {"label": label, "sub": sub, "val": val, "unit": unit,
            "lead": lead, "tgt": tgt, "ok": ok}


def _fmt(v, nd=0):
    if v is None:
        return "—"
    return f"{v:.{nd}f}" if nd else f"{v:g}"


def _ok(actual, target):
    if actual is None or not target:
        return None
    return actual >= target


def _kpi_band(type_key, entity, name, code, dd, r7_inp, r7_nadm, r7_surg,
              targets, surg_targets, profit_series, retention, total_ret_pct) -> list:
    """種別別の上段KPI 4枚。値＝公開版と同じ直近7日（在院=平均／新入院・手術=累計）。"""
    is_ward = entity == "ward"
    by = "by_ward" if is_ward else "by_dept"
    tgt_axis = "ward" if is_ward else "dept"
    r7 = r7_inp[by].get(code)
    inp_tgt = targets.get("inpatient", {}).get(tgt_axis, {}).get(code)
    na = r7_nadm[by].get(code)
    na_tgt = targets.get("new_admission", {}).get(tgt_axis, {}).get(code)
    ret_pct = round(retention * 100, 1) if retention is not None else None

    inp_kpi = lambda lead: _kpi("在院患者数", "直近7日平均", _fmt(r7, 1), "人", lead=lead,
                                tgt=f"目標 {inp_tgt:g}" if inp_tgt else "目標未設定", ok=_ok(r7, inp_tgt))
    nadm_kpi = _kpi("新入院", "直近7日累計", _fmt(na), "件",
                    tgt=f"目標 {na_tgt:g}/週" if na_tgt else "目標未設定", ok=_ok(na, na_tgt))
    ret_kpi = _kpi("週末 在院維持率", "土日/平日", _fmt(ret_pct, 1), "%",
                   tgt=f"全体 {total_ret_pct:g}%" if total_ret_pct else None)

    # 粗利: 実績(百万円) + 目標(百万円)。cur 末尾は見込みスロット(None)なので最後の確報値を使う
    if profit_series:
        latest = next((v for v in reversed(profit_series["cur"]) if v is not None), None)
        rate = profit_series["rate"]
        prof_kpi = _kpi("粗利", "確報・最新月", _fmt(latest, 1), "百万円",
                        tgt=f"目標 {profit_series['ref']:g}" if profit_series["ref"] else None,
                        ok=(rate >= 100) if rate is not None else None)
    else:
        prof_kpi = _kpi("粗利", "—", "—", "", tgt="対象外")

    if type_key == "surgical":
        sv = r7_surg["by_dept"].get(name, 0)
        surg_tgt = surg_targets.get(name) if isinstance(surg_targets, dict) else None
        return [_kpi("手術（全麻）", "直近7日累計", _fmt(sv), "件", lead=True,
                     tgt=f"目標 {surg_tgt:g}/週" if surg_tgt else "目標未設定", ok=_ok(sv, surg_tgt)),
                prof_kpi, inp_kpi(False), nadm_kpi]
    if type_key == "internal":
        return [inp_kpi(True), prof_kpi, nadm_kpi, ret_kpi]
    # ward
    beds = targets.get("inpatient", {}).get("ward_beds", {}).get(code)
    util = _rate(r7, beds)
    return [_kpi("病床利用率", "直近7日平均", _fmt(util, 1), "%", lead=True,
                 tgt=f"稼働 {beds:g}床" if beds else None),
            inp_kpi(False), nadm_kpi, ret_kpi]


# ════════════════════════════════════════════════════════════
# メイン: 全ユニットのレポートコンテキスト
# ════════════════════════════════════════════════════════════
def build_dept_report_contexts(adm: pd.DataFrame, surg: pd.DataFrame,
                               targets: dict, surg_targets: dict,
                               profit_monthly: pd.DataFrame,
                               base_date: pd.Timestamp, generated_at,
                               *, hospital_name: str = "", with_ai: bool = True,
                               axes=("dept", "ward"), quiet: bool = False,
                               profit_breakdown: pd.DataFrame = None) -> list:
    """診療科版・病棟版それぞれの 1部門=1コンテキスト を返す（PDF描画用）。"""
    period_start = (base_date - timedelta(days=WEEKS * 7 - 1)).strftime("%Y/%m/%d")
    period_end = base_date.strftime("%Y/%m/%d")
    r7_inp = rolling7_inpatient_avg(adm, base_date)
    r7_nadm = rolling7_new_admission(adm, base_date)
    r7_surg = rolling7_surgery(surg, base_date)

    # 粗利の当月見込み（暫定）用 per-dept 推計器を1回だけフィット
    estimators = {}
    if profit_breakdown is not None and len(profit_breakdown):
        try:
            estimators = fit_profit_estimators(profit_breakdown, adm, surg)
        except Exception:
            estimators = {}

    contexts = []
    for entity in axes:
        wl = weekend_census_retention(adm, base_date, entity=entity, weeks=8)
        if not wl.get("units"):
            continue
        _gcol, cand = _dow_unit_candidates(entity)
        name2code = {name: c for c, name in cand}
        order_idx = {name: i for i, (c, name) in enumerate(cand)}  # 固定順(コード/フロア順)
        report_units = [(name2code.get(u["name"], u["name"]), u["name"]) for u in wl["units"]]
        det = build_dow_unit_detail(adm, base_date, entity, report_units)
        total = wl.get("total", {})
        total_ret = total.get("retention")
        total_ret_pct = round(total_ret * 100, 1) if total_ret else None

        # AI一手は「のびしろのあるユニット」に限定（room>0.5）。
        n_ai = sum(1 for u in wl["units"] if (u.get("room_per_week", 0) or 0) > 0.5)
        if with_ai and n_ai:
            narrate_leveling_actions({entity: wl}, {entity: det}, top_n=n_ai, quiet=quiet)

        for u in wl["units"]:
            name = u["name"]
            code = name2code.get(name, name)
            dd = det.get(name)
            room = u.get("room_per_week", 0) or 0
            move = (_fallback_move(u, dd, entity) if room <= 0.5
                    else (u.get("narrative") or _fallback_move(u, dd, entity)))
            ret = u.get("retention")

            if entity == "ward":
                type_key = "ward"
            elif name in SURGERY_DISPLAY_DEPTS:
                type_key = "surgical"
            else:
                type_key = "internal"

            profit_series = (None if entity == "ward"
                             else _unit_profit_series(profit_monthly, name, base_date,
                                                      estimators, adm, surg))
            parts = _build_parts(adm, surg, base_date, entity, name, code, dd,
                                 r7_inp, r7_nadm, r7_surg, targets, surg_targets, profit_series)

            # 優先順にチャートを並べ、利用可能なものだけ採用
            ordered = [parts[k] for k in TYPE_ORDER[type_key] if k in parts and parts[k]]
            if not ordered:
                continue
            # ヒーロー(先頭)=高さ256、以降=232 で SVG 描画
            for i, p in enumerate(ordered):
                if not p["is_dow"]:
                    p["svg"] = render_trend_svg(p["_data"], p["_ref"], p["_ref_label"],
                                                p["_unit"], p["_win"], color=p["_color"],
                                                height=256 if i == 0 else 232,
                                                proj=p["_data"].get("proj"))
                p["priority"] = i + 1

            kpis = _kpi_band(type_key, entity, name, code, dd, r7_inp, r7_nadm, r7_surg,
                             targets, surg_targets, profit_series, ret, total_ret_pct)

            contexts.append({
                "axis": entity,
                "type_key": type_key,
                "type_label": TYPE_LABEL[type_key],
                "subtitle": TYPE_SUBTITLE[type_key],
                "prio_text": TYPE_PRIO_TXT[type_key],
                "order": order_idx.get(name, 999),
                "unit": name,
                "hospital_name": hospital_name,
                "period_start": period_start,
                "period_end": period_end,
                "base_date": base_date.strftime("%Y/%m/%d"),
                "generated_at": generated_at.strftime("%Y/%m/%d"),
                "total_retention_pct": round(total_ret * 100, 1) if total_ret else None,
                "kpis": kpis,
                "charts": ordered,
                "move": move,
            })
    return contexts
