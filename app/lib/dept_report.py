"""dept_report.py — 部門別レポートPDF用 コンテキスト構築（入退院バランス特化・A4 1枚）。

generate_html / html_builder と同じ前処理済みデータ(adm, surg, targets, surg_targets)から、
診療科版・病棟版それぞれ「1部門=1コンテキスト」を組み立てる。曜日プロファイルのSVGは
バッチPDF化での JS 実行タイミング問題を避けるため Python 側で静的描画する。

設計の確定事項（spec/dept_report_mock.html・memory: project_dept_report_pdf）:
  - レイアウト: A4 1枚。ヘッダ / バランスKPI4 / 曜日プロファイル / この期間の一手 / 部門サマリ参考帯
  - 配布単位: 両軸（dept=診療科版 / ward=病棟版）。対象は weekend_census_retention の
    min_weekday_avg しきい値で自動選別（入院実態のある科・病棟のみ）。
  - トーン: 職員発信（順位/赤の名指しなし）。達成率pillは緑(≥100%)/橙(<100%)の2段。
  - 一手: 全ユニットAI生成（oMLX未起動/失敗時は _fallback_move の定型文に自動フォールバック）。
"""
from __future__ import annotations

import math
from datetime import timedelta
from typing import Optional

import pandas as pd

from .metrics import (
    weekend_census_retention, rolling7_inpatient_avg,
    weekly_new_admission, weekly_surgery,
)
from .charts import build_dow_unit_detail, _dow_unit_candidates
from .ai_narrative import (
    narrate_leveling_actions,
    _q_friday, _q_weekend_adm, _q_state_trend,
)

WK = ["月", "火", "水", "木", "金", "土", "日"]

# 配色（dept.html renderDowProfile と一致）
C_OUT = "#e07a5f"   # 退院/流出
C_IN  = "#3d5a80"   # 入院/流入
C_CEN = "#2a9d8f"   # 在院指数
C_LN  = "#eef2f7"
C_AX  = "#9daab8"
C_INK = "#5f7084"


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
# この期間の一手（oMLX未起動時の定型フォールバック・データ適応）
# ════════════════════════════════════════════════════════════
def _fallback_move(unit: dict, dd: Optional[dict], entity: str) -> dict:
    """narrate_leveling_actions が None（oMLX未起動/失敗）のときの定型文。

    _build_leveling_prompt と同じレバー判定（金曜集中→退院分散／週末補充乏→週末入院）を
    Python 側で再現し、延伸を提案しないデータ適応の {body, action} を返す。
    """
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


def _read_caption(entity: str, dd: Optional[dict], unit: dict) -> str:
    """曜日プロファイル下の『読み方』本文（データ適応・数値入り）。"""
    out_lbl = "退院" if entity == "dept" else "退出"
    in_lbl = "入院" if entity == "dept" else "流入"
    head = (f'棒＝曜日別の日平均人数、<b style="color:#2a9d8f">緑線＝在院指数（平日平均=100）</b>。'
            f'谷ほど週末に在院が落ちています。')
    if not dd:
        return head
    a = dd["admission"]["w8"]
    dis = dd["discharge"]["w8"]
    we_adm = (a[5] + a[6]) / 2
    wd_adm = sum(a[0:5]) / 5 if sum(a[0:5]) else 0
    fri_share = dis[4] / sum(dis) if sum(dis) else 0
    parts = []
    if wd_adm > 0 and we_adm <= wd_adm * 0.6:
        parts.append(f"当科は<b>{in_lbl}が平日に寄り、土日はほぼ停止</b>")
    if fri_share >= 0.22:
        parts.append(f"<b>{out_lbl}が金曜に集中</b>")
    body = "".join(p if i == 0 else "、" + p for i, p in enumerate(parts))
    tail = (f"。一方<span style='color:#e07a5f;font-weight:900'>{out_lbl}は週末も続く</span>ため、"
            f"在院が週末に落ち込みます。" if parts
            else "曜日の偏りは比較的小さく、週末の在院も保てています。")
    return head + (body + tail if parts else tail)


# ════════════════════════════════════════════════════════════
# 部門サマリ（参考・粗利除く）
# ════════════════════════════════════════════════════════════
def _rate(actual, target):
    if target in (None, 0) or actual is None:
        return None, None
    pct = round(actual / target * 100, 1)
    return pct, ("ok" if pct >= 100 else "wr")


def _chip(label, sub, val, unit, *, rate=None, rate_cls=None, tgt="", muted=False):
    return {"label": label, "sub": sub, "val": val, "unit": unit,
            "rate": rate, "rate_cls": rate_cls, "tgt": tgt, "muted": muted}


def _summary_band(entity, name, code, dd, r7_inp, wk_nadm, wk_surg,
                  targets, surg_targets) -> list:
    """在院(直近7日平均 vs目標) / 新入院(週 vs目標) / 退院(週) / 手術 or 病床利用率。

    4枚目は診療科タイプで可変: 診療科=手術(全麻週・外科系のみ、内科は対象外)、病棟=病床利用率。
    """
    is_ward = entity == "ward"
    by = "by_ward" if is_ward else "by_dept"
    tgt_axis = "ward" if is_ward else "dept"

    # 1. 在院（直近7日平均）
    r7 = r7_inp[by].get(code)
    inp_tgt = targets.get("inpatient", {}).get(tgt_axis, {}).get(code)
    pct, cls = _rate(r7, inp_tgt)
    c1 = _chip("在院患者数", "直近7日平均", f"{r7:.1f}" if r7 is not None else "—", "人",
               rate=f"{pct:g}%" if pct else None, rate_cls=cls,
               tgt=f"目標 {inp_tgt:g} 人" if inp_tgt else "目標未設定")

    # 2. 新入院（週）
    na = wk_nadm[by].get(code)
    na_tgt = targets.get("new_admission", {}).get(tgt_axis, {}).get(code)
    pct, cls = _rate(na, na_tgt)
    c2 = _chip("新入院", "週", na if na is not None else "—", "件",
               rate=f"{pct:g}%" if pct else None, rate_cls=cls,
               tgt=f"目標 {na_tgt:g} 件／週" if na_tgt else "目標未設定")

    # 3. 退院（週）= 直近7日の流出件数
    dis_pw = (dd["discharge"]["per_week"].get("w7") if dd else None)
    out_lbl = "退院" if not is_ward else "退出"
    c3 = _chip(out_lbl, "週", round(dis_pw) if dis_pw is not None else "—", "件",
               tgt=("死亡・転出を除く" if not is_ward else "退院+死亡+転出"))

    # 4. 診療科=手術 / 病棟=病床利用率
    if is_ward:
        beds = targets.get("inpatient", {}).get("ward_beds", {}).get(code)
        util = (r7 / beds * 100) if (r7 is not None and beds) else None
        c4 = _chip("病床利用率", "直近7日平均", f"{util:.1f}" if util is not None else "—", "%",
                   tgt=f"稼働床 {beds:g} 床" if beds else "床数未設定", muted=(util is None))
    else:
        surg_tgt = surg_targets.get(name) if isinstance(surg_targets, dict) else None
        if surg_tgt:
            sv = wk_surg["by_dept"].get(name, 0)
            pct, cls = _rate(sv, surg_tgt)
            c4 = _chip("手術", "全麻・週", sv, "件", rate=f"{pct:g}%" if pct else None,
                       rate_cls=cls, tgt=f"目標 {surg_tgt:g} 件／週")
        else:
            c4 = _chip("手術", "全麻・週", "—", "", tgt="対象外（内科）", muted=True)
    return [c1, c2, c3, c4]


# ════════════════════════════════════════════════════════════
# メイン: 全ユニットのレポートコンテキスト
# ════════════════════════════════════════════════════════════
def build_dept_report_contexts(adm: pd.DataFrame, surg: pd.DataFrame,
                               targets: dict, surg_targets: dict,
                               base_date: pd.Timestamp, generated_at,
                               *, hospital_name: str = "", with_ai: bool = True,
                               axes=("dept", "ward"), quiet: bool = False) -> list:
    """診療科版・病棟版それぞれの 1部門=1コンテキスト を返す（PDF描画用）。"""
    period_start = (base_date - timedelta(days=55)).strftime("%Y/%m/%d")
    period_end = base_date.strftime("%Y/%m/%d")
    r7_inp = rolling7_inpatient_avg(adm, base_date)
    wk_nadm = weekly_new_admission(adm, base_date)
    wk_surg = weekly_surgery(surg, base_date)

    contexts = []
    for entity in axes:
        wl = weekend_census_retention(adm, base_date, entity=entity, weeks=8)
        if not wl.get("units"):
            continue
        # レポート対象 = weekend_census のユニット全件。dow_shared_units（週次フロー≥5）では
        # 高在院・低回転の科（外科系等）が落ち曜日プロファイルが空になるため、候補superset
        # から code を引き、build_dow_unit_detail を対象全件で組む。
        _gcol, cand = _dow_unit_candidates(entity)
        name2code = {name: code for code, name in cand}
        order_idx = {name: i for i, (code, name) in enumerate(cand)}  # 固定順(コード/フロア順)
        report_units = [(name2code.get(u["name"], u["name"]), u["name"]) for u in wl["units"]]
        det = build_dow_unit_detail(adm, base_date, entity, report_units)
        total = wl.get("total", {})
        total_ret = total.get("retention")

        # AI一手は「のびしろのあるユニット」に限定（room>0.5）。手本ユニット（room≈0）は
        # 週末ディップが無く、汎用是正文はそぐわないため肯定の定型文を使う。
        # wl["units"] は room 降順なので先頭 n_ai 件がAI対象。oMLX未起動時は narrative=None。
        n_ai = sum(1 for u in wl["units"] if (u.get("room_per_week", 0) or 0) > 0.5)
        if with_ai and n_ai:
            narrate_leveling_actions({entity: wl}, {entity: det},
                                     top_n=n_ai, quiet=quiet)

        for u in wl["units"]:
            name = u["name"]
            code = name2code.get(name, name)
            dd = det.get(name)
            room = u.get("room_per_week", 0) or 0
            move = (_fallback_move(u, dd, entity) if room <= 0.5
                    else (u.get("narrative") or _fallback_move(u, dd, entity)))
            svg = _render_dow_svg(dd["discharge"]["w8"], dd["admission"]["w8"],
                                  dd["census"]["w8"]) if dd else ""
            ret = u.get("retention")
            rd = u.get("room_delta_4w", 0) or 0
            contexts.append({
                "axis": entity,
                "axis_label": "診療科版" if entity == "dept" else "病棟版",
                "order": order_idx.get(name, 999),
                "unit": name,
                "hospital_name": hospital_name,
                "period_start": period_start,
                "period_end": period_end,
                "base_date": base_date.strftime("%Y/%m/%d"),
                "generated_at": generated_at.strftime("%Y/%m/%d"),
                "out_lbl": "退院" if entity == "dept" else "退出",
                "in_lbl": "入院" if entity == "dept" else "流入",
                "kpi": {
                    "weekday_avg": u["weekday_avg"],
                    "weekend_avg": u["weekend_avg"],
                    "weekend_gap": round(u["weekday_avg"] - u["weekend_avg"], 1),
                    "retention_pct": round(ret * 100, 1) if ret is not None else None,
                    "total_retention_pct": round(total_ret * 100, 1) if total_ret else None,
                    "room": u["room_per_week"],
                    "room_delta": rd,
                    "room_delta_abs": abs(round(rd, 1)),
                    "room_delta_dir": "down" if rd < -0.05 else ("up" if rd > 0.05 else "flat"),
                },
                "chart_svg": svg,
                "read": _read_caption(entity, dd, u),
                "move": move,
                "summary": _summary_band(entity, name, code, dd, r7_inp,
                                         wk_nadm, wk_surg, targets, surg_targets),
            })
    return contexts
