"""hospital_summary.py — 病院全体サマリーの共通セクション部品。

Comedix週報サマリーPNG（軽量）と 全病院実績まとめPDF（詳細）が共有する
データ組立＋描画。WEB版/portal と同じ metrics を再利用して数値を一致させる。

データ入口は generate_html.load_and_preprocess（build_dept_reports と同じ）。
描画関数は inline-styled HTML/SVG 断片を返し、Chrome で PNG/PDF 化する。
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

import pandas as pd

from . import metrics, triage
from .config import (
    WARD_NAMES, WARD_HIDDEN, NADM_DISPLAY_DEPTS, SURGERY_DISPLAY_DEPTS,
    KPI_ICONS, status_display,
    TARGET_INPATIENT_ALLDAY, TARGET_INPATIENT_WEEKDAY, TARGET_INPATIENT_HOLIDAY,
    TARGET_ADMISSION_WEEKLY, TARGET_GA_DAILY,
)

# ── 配色（dashboard と一致）──
INK, SUB, LINE, PREV = "#1f2d3d", "#5a6b7b", "#2b6cb0", "#b9c2cd"
OK_FILL, WR_FILL = "rgba(14,122,84,0.13)", "rgba(180,83,9,0.13)"  # 達成ゾーン塗り（目標超=緑/目標割=橙）
WEEKS = 12
PREVYEAR_DAYS = 364   # 52週=曜日合わせ

# 色スケール対象外（業務実態が一般病棟/一般科と異なる＝誤読を避けるためミュート）
COLOR_EXEMPT_WARDS = {"04B", "04D"}   # ICU / HCU
COLOR_EXEMPT_DEPTS = {"眼科"}          # memory: profile 未整備

BASE_CSS = """
  *{box-sizing:border-box;}
  body{margin:0;background:#fff;color:#1f2d3d;
       font-family:'Hiragino Kaku Gothic ProN','Hiragino Sans','Noto Sans JP',sans-serif;}
  .sec{font-size:14px;font-weight:800;margin:10px 0 4px;}
  .sec small{font-size:11.5px;color:#5a6b7b;font-weight:600;}
  .hs-kpis{display:flex;flex-wrap:wrap;gap:9px;}
  .hs-kpi{flex:1 1 180px;border:1px solid #e0e6ee;border-radius:10px;padding:11px 13px;
          box-shadow:0 1px 3px rgba(0,0,0,.06);}
  .hs-kpi .lab{font-size:12px;color:#5a6b7b;}
  .hs-kpi .val{font-size:27px;font-weight:800;line-height:1.1;}
  .ht{border-collapse:collapse;width:100%;font-size:11px;}
  .ht th,.ht td{border:1px solid #e6ebf1;padding:2px 6px;text-align:center;line-height:1.25;}
  .ht th{background:#f2f5f9;font-weight:700;font-size:10.5px;color:#3a4656;}
  .ht td.nm{text-align:left;white-space:nowrap;font-weight:600;}
  .ht .sub{font-size:9px;color:#7a899a;font-weight:400;}
  .ht tr:nth-child(even) td{background:#fcfdfe;}
  .legend{font-size:11px;color:#5a6b7b;margin:6px 0 2px;}
  .legend i{display:inline-block;width:11px;height:11px;border-radius:3px;vertical-align:-1px;margin:0 3px 0 10px;}
  .grp{font-size:11px;color:#41618a;font-weight:800;background:#eef3fb;}
"""


# ════════════════════════════════════════════════════════════
# 色ヘルパ（status_display を流用＝portal/dept と一致）
# ════════════════════════════════════════════════════════════
def _sd(rate, exempt: bool = False) -> dict:
    return status_display(None) if (exempt or rate is None) else status_display(rate)


def _ret_sd(ret: Optional[float], exempt: bool = False) -> dict:
    """週末在院維持率(0..1) → status_display（100%=ディップ無=達成）。"""
    return status_display(None) if (exempt or ret is None) else status_display(ret * 100)


def _redist_sd(redist: Optional[float], exempt: bool = False) -> dict:
    """退院曜日 再配分率（低い=平準＝良）。30/37 を境に ok/warn/danger。"""
    if exempt or redist is None:
        return status_display(None)
    if redist < 30:
        return status_display(100)   # ok
    if redist < 37:
        return status_display(95)    # warn
    return status_display(80)        # danger


def _flow_color(net: float) -> str:
    return "#0e7a54" if net > 0 else ("#b45309" if net < 0 else SUB)


def _cell(sd: dict, html: str) -> str:
    return f'<td style="background:{sd["bg"]};color:{sd["color"]}">{html}</td>'


# ════════════════════════════════════════════════════════════
# 入退院フロー（直近7日・入退院バランスタブと同一定義）
# ════════════════════════════════════════════════════════════
def _flow_7d(adm: pd.DataFrame, date: pd.Timestamp, entity: str) -> dict:
    """直近7日の入(新入院)・出(退院)・純。病棟は転入/転出込みで純が在院変化と整合。"""
    start = date - timedelta(days=6)
    w = adm[(adm["日付"] >= start) & (adm["日付"] <= date)].copy()
    out: dict = {}
    if entity == "ward":
        w = w[w["病棟_表示"]]
        w["_out"] = w["退院合計"] + w["転出患者数"]
        gi = w.groupby("病棟コード")["新入院患者数_病棟"].sum()
        go = w.groupby("病棟コード")["_out"].sum()
        keys = "病棟コード"
    else:
        w = w[w["科_表示"]]
        gi = w.groupby("診療科名")["新入院患者数"].sum()
        go = w.groupby("診療科名")["退院患者数"].sum()
        keys = "診療科名"
    for k in set(gi.index) | set(go.index):
        i, o = float(gi.get(k, 0)), float(go.get(k, 0))
        out[k] = {"in": i, "out": o, "net": i - o}
    return out


# ════════════════════════════════════════════════════════════
# 12週トレンド系列（現在＋前年同期）
# ════════════════════════════════════════════════════════════
def _ma_series(adm: pd.DataFrame, col: str, date: pd.Timestamp,
               window: int, agg: str, weeks: int = WEEKS) -> dict:
    s = metrics.build_daily_series(adm, col).sort_values("日付")
    roll = s["値"].rolling(window, min_periods=1)
    s["v"] = roll.sum() if agg == "sum" else roll.mean()
    vmap = dict(zip(s["日付"], s["v"]))
    start = date - timedelta(days=weeks * 7 - 1)
    cur_dates = [d for d in s["日付"] if start <= d <= date]
    cur = [round(vmap[d], 1) for d in cur_dates]
    prev = [round(vmap[d - timedelta(days=PREVYEAR_DAYS)], 1)
            if (d - timedelta(days=PREVYEAR_DAYS)) in vmap else None for d in cur_dates]
    return {"dates": [d.strftime("%m/%d") for d in cur_dates], "cur": cur, "prev": prev}


def _surg_series(surg: pd.DataFrame, date: pd.Timestamp, weeks: int = WEEKS) -> dict:
    cur = metrics.build_biz_ma30_series(surg, date)
    prv = metrics.build_biz_ma30_series(surg, date, prev_year=True)
    n = weeks * 5
    cd, cv = cur["dates"][-n:], cur["values"][-n:]
    pmap = dict(zip(prv["dates"], prv["values"]))
    return {"dates": [d[5:].replace("-", "/") for d in cd], "cur": cv,
            "prev": [pmap.get(d) for d in cd]}


# ════════════════════════════════════════════════════════════
# 今週の一手（既存 triage / weekend_census_retention 流用・de-named）
# ════════════════════════════════════════════════════════════
def build_hero_text(adm, surg, surg_targets, base_date) -> dict:
    wr = metrics.weekend_census_retention(adm, base_date, entity="ward")
    total = wr.get("total", {})
    ret, room = total.get("retention"), total.get("room_per_week", 0.0)
    dept_items, ward_items = triage.score_leveling(adm, surg, surg_targets, base_date)
    items = sorted(dept_items + ward_items, key=lambda x: -x["redist"])
    if items:
        headline = "土日の在院ディップを埋める"
        body = triage._make_leveling_fallback(items[0])["suggestion"]
    elif ret is not None and ret < 0.95:
        headline = "土日の在院維持を強化"
        body = "週後半に偏りがちな退院を週前半へ。週末は退院準備を進め月曜に実行（総退院数・在院は維持）。"
    else:
        headline = "週末在院の維持は良好"
        body = "現在の退院曜日バランスは良好です。週末も平日水準の在院維持にご協力ください。"
    chips = []
    if ret is not None:
        chips.append(("週末在院維持率", f"{ret*100:.1f}%"))
    if room and room > 0:
        chips.append(("のびしろ", f"{room:.0f}人日/週"))
    return {"headline": headline, "body": body, "chips": chips}


# ════════════════════════════════════════════════════════════
# データ組立
# ════════════════════════════════════════════════════════════
def build_summary_context(adm, surg, targets, surg_targets, base_date) -> dict:
    kpi = metrics.build_kpi_summary(adm, surg, base_date, targets, surg_targets)

    trends = {
        "inpatient": _ma_series(adm, "在院患者数", base_date, 7, "mean"),
        "admission": _ma_series(adm, "新入院患者数", base_date, 7, "sum"),
        "operation": _surg_series(surg, base_date),
    }

    # 病棟テーブル
    wr_rank = metrics.build_ward_ranking(adm, base_date, targets, "inpatient")
    ret_ward = {u["name"]: u for u in
                metrics.weekend_census_retention(adm, base_date, "ward").get("units", [])}
    flow_w = _flow_7d(adm, base_date, "ward")
    rank_by_code = {r["病棟コード"]: r for r in wr_rank.to_dict("records")} if len(wr_rank) else {}
    ward_rows = []
    for code, name in WARD_NAMES.items():
        if code in WARD_HIDDEN:
            continue
        r = rank_by_code.get(code)
        if not r:
            continue
        ru = ret_ward.get(name, {})
        fl = flow_w.get(code, {"in": 0, "out": 0, "net": 0})
        ward_rows.append({
            "name": name, "exempt": code in COLOR_EXEMPT_WARDS,
            "inp_actual": r.get("実績"), "inp_target": r.get("目標"),
            "inp_rate": r.get("達成率"), "util": r.get("利用率"), "beds": r.get("病床数"),
            "retention": ru.get("retention"), "flow": fl,
        })

    # 診療科テーブル（内科系→外科系の固定順）
    inp_rank = {r["診療科"]: r for r in
                metrics.build_dept_ranking(adm, base_date, targets, "inpatient").to_dict("records")}
    nadm_rank = {r["診療科"]: r for r in
                 metrics.build_dept_ranking(adm, base_date, targets, "new_admission").to_dict("records")}
    surg_rank = {r["診療科"]: r for r in
                 metrics.build_surgery_ranking(surg, base_date, surg_targets, period="7").to_dict("records")}
    flow_d = _flow_7d(adm, base_date, "dept")
    medical = sorted(NADM_DISPLAY_DEPTS - SURGERY_DISPLAY_DEPTS)
    surgical = sorted(SURGERY_DISPLAY_DEPTS)
    dept_rows = []
    for dept, dtype in [(d, "内科") for d in medical] + [(d, "外科") for d in surgical]:
        ip, na, sg = inp_rank.get(dept), nadm_rank.get(dept), surg_rank.get(dept)
        prof = metrics.discharge_dow_profile(adm, base_date, "診療科名", dept)
        dept_rows.append({
            "name": dept, "type": dtype, "exempt": dept in COLOR_EXEMPT_DEPTS,
            "inp_actual": ip.get("実績") if ip else None, "inp_target": ip.get("目標") if ip else None,
            "inp_rate": ip.get("達成率") if ip else None,
            "nadm_actual": na.get("実績") if na else None, "nadm_target": na.get("目標") if na else None,
            "nadm_rate": na.get("達成率") if na else None,
            "surg_actual": sg.get("実績") if sg else None, "surg_target": sg.get("週目標") if sg else None,
            "surg_rate": sg.get("達成率") if sg else None,
            "redist": prof.get("redistribution"),
            "flow": flow_d.get(dept, {"in": 0, "out": 0, "net": 0}),
        })

    return {"kpi": kpi, "trends": trends, "ward_rows": ward_rows, "dept_rows": dept_rows,
            "hero": build_hero_text(adm, surg, surg_targets, base_date),
            "base_date": base_date}


# ════════════════════════════════════════════════════════════
# 描画
# ════════════════════════════════════════════════════════════
def render_hero(headline: str, body: str, chips: list) -> str:
    chip_html = "".join(
        f'<div style="background:rgba(255,255,255,.18);border:1px solid rgba(255,255,255,.45);'
        f'border-radius:999px;padding:5px 13px;font-size:13px;">'
        f'<span style="opacity:.85">{k}</span> <b style="font-size:15px">{v}</b></div>'
        for k, v in chips)
    return (
        f'<div style="background:linear-gradient(135deg,#2b6cb0,#1f4e85);color:#fff;'
        f'border-radius:14px;padding:16px 20px;box-shadow:0 2px 8px rgba(31,78,133,.25)">'
        f'<div style="font-size:12px;letter-spacing:2px;opacity:.85;font-weight:700">📣 今週の一手</div>'
        f'<div style="font-size:23px;font-weight:900;margin:3px 0 6px">{headline}</div>'
        f'<div style="font-size:14px;line-height:1.6;opacity:.96">{body}</div>'
        f'<div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:11px">{chip_html}</div></div>'
    )


def render_kpi_cards(kpi: dict) -> str:
    # 在院＝直近7日の平日平均／休日平均を併記（枠は平日=主目標基準）
    wd_v, hd_v = kpi["inpatient_avg_7d_wd"], kpi["inpatient_avg_7d_hd"]
    wd_sd = status_display(metrics.achievement_rate(wd_v, TARGET_INPATIENT_WEEKDAY))
    hd_sd = status_display(metrics.achievement_rate(hd_v, TARGET_INPATIENT_HOLIDAY))

    def dual_row(lab, v, tgt, sd):
        vtxt = "—" if v is None else f"{v:.1f}"
        return (f'<div style="display:flex;align-items:baseline;gap:8px;margin:3px 0">'
                f'<span style="font-size:12px;font-weight:800;color:{SUB};min-width:30px">{lab}</span>'
                f'<span style="font-size:23px;font-weight:900;line-height:1">{vtxt}'
                f'<span style="font-size:12px;font-weight:600;color:{SUB}">人</span></span>'
                f'<span style="font-size:11px;font-weight:800;color:{sd["color"]}">{sd["shape"]}{sd["text"]}</span>'
                f'<span style="font-size:11px;color:#9aa7b4;margin-left:auto">目標{tgt:g}</span></div>')

    inp_card = (
        f'<div class="hs-kpi" style="border-left:5px solid {wd_sd["color"]}">'
        f'<div class="lab">{KPI_ICONS.get("inpatient","")} 在院患者数 '
        f'<span class="sub" style="color:#7a899a">直近7日平均（平日／休日）</span></div>'
        + dual_row("平日", wd_v, TARGET_INPATIENT_WEEKDAY, wd_sd)
        + dual_row("休日", hd_v, TARGET_INPATIENT_HOLIDAY, hd_sd) + '</div>')

    def single(kid, label, period, val, unit, g, gu, sd):
        vtxt = f"{val:.1f}" if unit == "件/日" else f"{val:.0f}"
        return (f'<div class="hs-kpi" style="border-left:5px solid {sd["color"]}">'
                f'<div class="lab">{KPI_ICONS.get(kid,"")} {label} <span class="sub" style="color:#7a899a">{period}</span></div>'
                f'<div class="val">{vtxt}<span style="font-size:13px;font-weight:400;color:{SUB}"> {unit}</span> '
                f'<span style="font-size:12px;color:{sd["color"]};font-weight:700">{sd["shape"]}{sd["text"]}</span></div>'
                f'<div style="margin-top:2px"><span style="color:{SUB};font-size:12px">目標差 {g:+.1f}{gu}</span></div></div>')

    cards = [
        inp_card,
        single("admission", "新入院患者数", "直近7日累計",
               kpi["admission_actual_7d"], "人", kpi["admission_gap"], "人", kpi["admission_status"]),
        single("operation", "全身麻酔手術", "直近7平日平均",
               kpi["operation_daily_avg"], "件/日", kpi["operation_gap"], "件/日", kpi["operation_status"]),
    ]
    return f'<div class="hs-kpis">{"".join(cards)}</div>'


def render_trend_svg(data: dict, ref: float, ref_label: str, unit: str,
                     window_label: str, color: str = LINE, height: int = 210,
                     proj: float = None) -> str:
    """トレンドSVG。proj を渡すと cur 末尾スロットへ点線＋中空マーカー（当月見込み）を描く。"""
    dates, cur, prev = data["dates"], data["cur"], data["prev"]
    pts_all = [v for v in cur if v is not None] + [v for v in prev if v is not None] + [ref]
    if proj is not None:
        pts_all.append(proj)
    if not pts_all:
        return ""
    W, H, L, R, T, B = 760, height, 50, 700, 24, height - 42
    lo, hi = min(pts_all), max(pts_all)
    # パディングは実績変化に追従させる。絶対値2固定だと新入院（〜数件/日）など
    # 小さい指標で変動が潰れる（例: 実測3〜4.5でも軸が1〜7に広がる）ので、
    # データ幅(0.18)を主、系列規模(0.04)を下限にして、平らな指標は平らに見せる。
    pad = max((hi - lo) * 0.18, abs(hi) * 0.04, 0.05)
    y0, y1 = lo - pad, hi + pad
    n = len(cur)

    def X(i): return L + (R - L) * (i / (n - 1)) if n > 1 else (L + R) / 2
    def Y(v): return B - (B - T) * ((v - y0) / (y1 - y0))

    el = [f'<text x="{L}" y="14" font-size="12" font-weight="800" fill="{INK}">{window_label}'
          f'<tspan font-size="10.5" font-weight="400" fill="{SUB}">（{unit}）</tspan></text>']
    for g in range(3):
        v = y0 + (y1 - y0) * (g + 0.5) / 3
        yy = Y(v)
        el.append(f'<line x1="{L}" y1="{yy:.1f}" x2="{R}" y2="{yy:.1f}" stroke="#eef2f7"/>')
        el.append(f'<text x="{L-7:.1f}" y="{yy+3.5:.1f}" font-size="10.5" fill="#9aa7b4" text-anchor="end">{v:.0f}</text>')
    yr = Y(ref)
    # 達成ゾーン: cur線とref線の間を、区間ごとに 目標超=緑 / 目標割=橙 で塗る
    for i in range(n - 1):
        a, b = cur[i], cur[i + 1]
        if a is None or b is None:
            continue
        fill = OK_FILL if (a + b) / 2 >= ref else WR_FILL
        el.append(f'<polygon points="{X(i):.1f},{Y(a):.1f} {X(i+1):.1f},{Y(b):.1f} '
                  f'{X(i+1):.1f},{yr:.1f} {X(i):.1f},{yr:.1f}" fill="{fill}"/>')

    def path(vals, stroke, w, dash=False):
        seg, started = [], False
        for i, v in enumerate(vals):
            if v is None:
                started = False
                continue
            seg.append(f'{"M" if not started else "L"}{X(i):.1f} {Y(v):.1f}')
            started = True
        if not seg:
            return ""
        d = f' stroke-dasharray="4 3"' if dash else ""
        return f'<path d="{" ".join(seg)}" fill="none" stroke="{stroke}" stroke-width="{w}" stroke-linejoin="round"{d}/>'

    el.append(path(prev, PREV, 1.7))   # 前年同期＝グレー実線（破線廃止・色と太さで当年と区別）
    el.append(path(cur, color, 2.6))
    # 目標線はデータ線の上に描く（手前に置くと当年/前年の太線に覆われ破線が切れ切れに見える）
    el.append(f'<line x1="{L}" y1="{yr:.1f}" x2="{R}" y2="{yr:.1f}" stroke="#9aa7b4" stroke-width="1.2" stroke-dasharray="5 4"/>')
    el.append(f'<text x="{R+4:.1f}" y="{yr+3.5:.1f}" font-size="10.5" fill="#9aa7b4" font-weight="700">{ref_label}</text>')
    # 端ラベル
    el.append(f'<text x="{X(0):.1f}" y="{B+15:.1f}" font-size="10" fill="{SUB}" text-anchor="middle">{dates[0]}</text>')
    el.append(f'<text x="{X(n-1):.1f}" y="{B+15:.1f}" font-size="10" fill="{SUB}" text-anchor="middle">{dates[-1]}</text>')
    # 確報の端マーカー（cur の最後の非None＝見込みスロットがある場合はその手前）
    j_last = next((i for i in range(n - 1, -1, -1) if cur[i] is not None), None)
    if j_last is not None:
        yy = Y(cur[j_last])
        el.append(f'<circle cx="{X(j_last):.1f}" cy="{yy:.1f}" r="4" fill="{color}"/>')
        el.append(f'<text x="{X(j_last):.1f}" y="{yy-9:.1f}" font-size="12.5" fill="{color}" text-anchor="end" font-weight="900">{cur[j_last]:.1f}</text>')
    # 前年同期の端マーカー＋数値（ラベルは点の下＝当年ラベルと分離）
    p_last = next((i for i in range(n - 1, -1, -1) if prev[i] is not None), None)
    if p_last is not None:
        yyp = Y(prev[p_last])
        el.append(f'<circle cx="{X(p_last):.1f}" cy="{yyp:.1f}" r="3" fill="{PREV}"/>')
        el.append(f'<text x="{X(p_last):.1f}" y="{yyp+13:.1f}" font-size="10" fill="{SUB}" text-anchor="end" font-weight="700">{prev[p_last]:.1f}</text>')
    # 当月見込み（点線＋中空マーカー）
    if proj is not None and j_last is not None:
        xp, yp = X(n - 1), Y(proj)
        el.append(f'<path d="M{X(j_last):.1f} {Y(cur[j_last]):.1f} L{xp:.1f} {yp:.1f}" '
                  f'fill="none" stroke="{color}" stroke-width="2.2" stroke-dasharray="2 3" opacity="0.85"/>')
        el.append(f'<circle cx="{xp:.1f}" cy="{yp:.1f}" r="4.2" fill="#fff" stroke="{color}" stroke-width="2"/>')
        el.append(f'<text x="{xp:.1f}" y="{yp-9:.1f}" font-size="11.5" fill="{color}" text-anchor="end" font-weight="900">{proj:g}</text>')
    # 凡例
    el.append(f'<text x="{R}" y="14" font-size="10" fill="{PREV}" text-anchor="end">― 前年同期</text>')
    return f'<svg viewBox="0 0 {W} {H}" width="100%" style="display:block">' + "".join(el) + "</svg>"


def _ach(actual, target, rate, sd, emphasize=False) -> str:
    if actual is None:
        return _cell(status_display(None), "—")
    a = f"{actual:.0f}" if (isinstance(actual, (int, float)) and float(actual).is_integer()) else f"{actual:.1f}"
    t = "—" if target is None else (f"{target:.0f}" if float(target).is_integer() else f"{target:.1f}")
    pct = f'{rate:.0f}%' if rate is not None else "—"
    wt = "900" if emphasize else "700"
    return (f'<td style="background:{sd["bg"]};color:{sd["color"]}">'
            f'<span style="font-weight:{wt}">{pct}</span><br><span class="sub">{a}/{t}</span></td>')


def _flow_cell(fl: dict) -> str:
    net = fl["net"]
    return (f'<td><span class="sub">入{fl["in"]:.0f} / 退{fl["out"]:.0f}</span><br>'
            f'<b style="color:{_flow_color(net)}">純{net:+.0f}</b></td>')


def render_ward_table(rows: list) -> str:
    head = ('<tr><th>病棟</th><th>在院<span class="sub">実/目</span></th><th>病床利用率</th>'
            '<th>入退院フロー<span class="sub">直近7日</span></th><th>週末在院維持率</th></tr>')
    body = []
    for r in rows:
        ex = r["exempt"]
        sd = _sd(r["inp_rate"], ex)
        util = "—" if r["util"] is None else f'{r["util"]:.1f}%'
        util_sub = f'<br><span class="sub">{r["beds"]:g}床</span>' if r.get("beds") else ""
        rsd = _ret_sd(r["retention"], ex)
        ret = "—" if r["retention"] is None else f'{r["retention"]*100:.0f}%'
        tag = ' <span class="sub">※</span>' if ex else ""
        body.append(
            f'<tr><td class="nm">{r["name"]}{tag}</td>'
            + _ach(r["inp_actual"], r["inp_target"], r["inp_rate"], sd)
            + _cell(sd, f'{util}{util_sub}')
            + _flow_cell(r["flow"])
            + _cell(rsd, ret) + '</tr>')
    return f'<table class="ht"><thead>{head}</thead><tbody>{"".join(body)}</tbody></table>'


def render_dept_table(rows: list) -> str:
    head = ('<tr><th>診療科</th><th>在院<span class="sub">実/目</span></th>'
            '<th>新入院<span class="sub">実/目</span></th><th>入退院フロー<span class="sub">直近7日</span></th>'
            '<th>退院再配分率</th><th>全麻<span class="sub">実/目</span></th></tr>')
    body, cur_type = [], None
    for r in rows:
        if r["type"] != cur_type:
            cur_type = r["type"]
            body.append(f'<tr><td class="grp" colspan="6">{cur_type}系</td></tr>')
        ex = r["exempt"]
        med = (r["type"] == "内科")
        dsd = _redist_sd(r["redist"], ex)
        rd = "—" if r["redist"] is None else f'{r["redist"]:.0f}%'
        body.append(
            f'<tr><td class="nm">{r["name"]}{" ※" if ex else ""}</td>'
            + _ach(r["inp_actual"], r["inp_target"], r["inp_rate"], _sd(r["inp_rate"], ex), emphasize=med)
            + _ach(r["nadm_actual"], r["nadm_target"], r["nadm_rate"], _sd(r["nadm_rate"], ex), emphasize=med)
            + _flow_cell(r["flow"])
            + _cell(dsd, rd)
            + _ach(r["surg_actual"], r["surg_target"], r["surg_rate"], _sd(r["surg_rate"], ex), emphasize=not med)
            + '</tr>')
    return f'<table class="ht"><thead>{head}</thead><tbody>{"".join(body)}</tbody></table>'


def render_legend() -> str:
    so, sw, sd = status_display(100), status_display(95), status_display(80)
    return (f'<div class="legend">指標の見方：達成率%＋小さく実績/目標。'
            f'<i style="background:{so["bg"]};border:1px solid {so["color"]}"></i>達成'
            f'<i style="background:{sw["bg"]};border:1px solid {sw["color"]}"></i>接近'
            f'<i style="background:{sd["bg"]};border:1px solid {sd["color"]}"></i>未達'
            f'　／　純＝直近7日の入−退（＋増/−減）　／　※＝色評価の対象外（業務実態が異なる）</div>')
