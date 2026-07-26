"""dept_report.py — 部門別レポートPDF用 コンテキスト構築（種別別・グラフ多用版・A4 1枚）。

generate_html / html_builder と同じ前処理済みデータ(adm, surg, targets, surg_targets,
profit_monthly)から、診療科版・病棟版それぞれ「1部門=1コンテキスト」を組み立てる。

2026-06-20 構成見直し（spec/dept_report_v3_mock.html・memory: project_dept_report_pdf）:
  グラフ5パーツ A.在院 B.新入院 C.全麻手術 D.粗利 E.曜日プロファイル を、ユニット種別ごとの
  優先順で配置する。優先1位=全幅ヒーロー、以降は読み順(=優先順)で半幅2列、⭐一手が端を埋める。
    外科系診療科: C, D, E, B, A   内科系診療科: A, D, B, E   病棟: A(病床利用率), B, E
  - トレンドは当年線＋前年同期(点線)＋目標(破線。病棟=目標利用率)＋達成率バッジ(緑≥100/橙<100)。
  - 粗利は診療科のみ・月次cadence(確報ベース)。病棟は粗利/手術なし。種別=SURGERY_DISPLAY_DEPTS。
  - SVGはバッチPDF化のJS実行を避け Python 側で静的描画（render_trend_svg / _render_dow_svg）。
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import math
from datetime import timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from .config import (
    SURGERY_DISPLAY_DEPTS, SURGERY_EVAL_DEPTS, surgery_metric_label, unit_narration_kind,
    TARGET_INPATIENT_ALLDAY, TARGET_ADMISSION_WEEKLY, TARGET_GA_DAILY,
    TARGET_WEEKEND_RETENTION, FEE_REVISION_DATE, FEE_REVISION_PROFIT_UPLIFT,
)
from .metrics import (
    weekend_census_retention, rolling7_inpatient_avg,
    rolling7_new_admission, rolling7_surgery,
    build_daily_series, build_surgery_daily_series,
    build_kpi_summary, dow_event_profile,
    build_dept_ranking, build_surgery_ranking,
    daily_or_utilization,
)
from .charts import build_dow_unit_detail, _dow_unit_candidates
from .ai_narrative import (
    narrate_leveling_actions, narrate_admission_action, narrate_surgery_action,
    narrate_emergency_leveling_action, narrate_emergency_admission_action,
    narrate_critical_care_leveling_action, narrate_critical_care_admission_action,
    narrate_er_leveling_action, narrate_er_admission_action,
    narrate_hospital_summary, NARRATE_WORKERS,
    _q_latewk_discharge, _q_weekend_adm, _q_census_dip, _q_thin_latewk_adm,
    _leveling_levers, _q_state_trend, _q_target_gap, _q_target_gap_trend, _q_yoy,
    _gap_level_tier, _q_ret_level,
)
from .hospital_summary import render_trend_svg, _ma_series, _surg_series
from .profit_estimate import fit_profit_estimators, project_dept_monthend
from .report_overrides import apply_override, is_full_override

logger = logging.getLogger(__name__)

WK = ["月", "火", "水", "木", "金", "土", "日"]
WEEKS = 12
PREVYEAR_DAYS = 364   # 52週=曜日合わせ

# 2026-06-01 診療報酬改定（係数の出所は config.FEE_REVISION_PROFIT_UPLIFT のコメント）。
# 粗利チャートの前年同期線は、当年が改定後・前年同月が改定前のときだけ改定後スケールへ
# 換算して物差しを揃える（実質比較）。詳細は _prev_needs_revision_adjust を参照。
FEE_REVISION_TS = pd.Timestamp(FEE_REVISION_DATE)
_REV_NOTE = ("・前年線は改定換算("
             f"外来×{FEE_REVISION_PROFIT_UPLIFT.get('外来', 1.0):g}"
             f"/入院×{FEE_REVISION_PROFIT_UPLIFT.get('入院', 1.0):g})")

# 前年同期に比較可能なデータがない病棟（再編・新規開棟）＝前年同期線を出さない。
#   ICU(04B)/HCU(04D)は業務実態が一般病棟と異なり前年比較が成り立たず、8階B(08B)は2025年開棟。
NO_PREVYEAR_WARDS = {"04B", "04D", "08B"}

# 配色（dept.html renderDowProfile / hospital_summary と一致）
C_OUT = "#e07a5f"   # 退院/流出
C_IN  = "#3d5a80"   # 入院/流入
C_CEN = "#2a9d8f"   # 在院指数
C_LN  = "#eef2f7"
C_AX  = "#9daab8"
C_INK = "#5f7084"

# グラフパーツ（A-E）の色・窓ラベル
# 当年実績線は中立ブルー1色に統一する。緑/オレンジは達成表現（網掛け・目標線・バッジ）専用とし、
# 線色が「成績」に見える誤読を避ける（種別はカード見出しのタイトル文言で識別。
# アイコンは業務向け表示として2026-07-01に廃止）。
PART_LINE = "#2b6cb0"
# 窓ラベルは公開版ダッシュボード（dept.html 既定線）と同方式。
#   在院/新入院＝日次系列の28日移動平均、手術＝週次合計(件/週)の28日移動平均。
PART_WIN = {"A": "12週・28日移動平均", "B": "12週・28日移動平均（件/日）",
            "C": "12週・28日移動平均（件/週）", "D": "12か月・月次（確報ベース）",
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


def _unit_surg_weekly_series(surg, base_date, dept) -> dict:
    """指定診療科の全麻・週次合計(件/週)の28日移動平均・直近12週＋前年同期。

    公開版ダッシュボード dept.html の診療科手術チャート（renderSurgeryChart の既定線
    ＝28日移動平均）と同方式。build_surgery_daily_series は営業日のみの疎な系列なので、
    直近7日（暦日窓）rolling 合計＝件/週 → 28データ点（営業日）の移動平均。前年同期は
    同系列の date−PREVYEAR_DAYS を引く（昨年度同期 週次合計の28日平滑に相当）。
    """
    ser = build_surgery_daily_series(surg, ga_only=True, dept=dept)
    if ser is None or len(ser) == 0:
        return {"dates": [], "cur": [], "prev": []}
    ser = ser[ser["日付"] <= base_date].sort_values("日付")
    s = ser.set_index("日付")["値"]
    weekly = s.rolling("7D").sum()                    # 直近7日(暦日窓) rolling 合計＝件/週
    ma28 = weekly.rolling(28, min_periods=1).mean()   # 28データ点(営業日)の移動平均
    vmap = dict(zip(s.index, ma28.round(1).to_numpy()))  # 当年線＝手術日のみ(疎・据え置き)
    # 前年同期(d−364)は手術日の疎な系列だと去年の手術日にほぼ一致せず線が途切れる。
    # MAを暦日連続に補間した系列で引き、線をつなぐ（当年線の算出は変えない）。
    ma_daily = (ma28.reindex(pd.date_range(s.index.min(), s.index.max(), freq="D"))
                .interpolate(method="time").ffill().bfill())
    dmap = dict(zip(ma_daily.index, ma_daily.round(1).to_numpy()))
    idx = list(s.index)
    start = base_date - timedelta(days=WEEKS * 7 - 1)
    cur_dates = [d for d in idx if start <= d <= base_date]
    cur = [round(vmap[d], 1) for d in cur_dates]
    prev = [round(dmap[d - timedelta(days=PREVYEAR_DAYS)], 1)
            if (d - timedelta(days=PREVYEAR_DAYS)) in dmap else None for d in cur_dates]
    return {"dates": [d.strftime("%m/%d") for d in cur_dates], "cur": cur, "prev": prev}


def _prev_needs_revision_adjust(m) -> bool:
    """当年の月 m の前年同月が「改定前の点数」で、m 自身が「改定後」か。

    両方とも改定前／両方とも改定後なら物差しが揃っているので換算不要。
    2027-06 以降は前年同月も改定後になり、自動的に False になる（期限切れ）。
    """
    m = pd.Timestamp(m)
    return bool(m >= FEE_REVISION_TS
                and (m - pd.DateOffset(years=1)) < FEE_REVISION_TS)


def _revision_adjusted_prev(gmap, nmap, pm):
    """前年同月 pm の粗利を改定後スケールへ換算（百万円）。

    粗利チャートの前年同期線を当年線と同じ物差しに揃えるための表示用換算。
    内訳(外来/入院)が取れないときは None を返す＝呼び出し側は素の値を使い、
    注記も出さない（不正確な換算をして「換算済み」と偽らない）。
    """
    g = gmap.get(pm) if gmap else None
    n = nmap.get(pm) if nmap else None
    if g is None or n is None or pd.isna(g) or pd.isna(n):
        return None
    adj = (float(g) * FEE_REVISION_PROFIT_UPLIFT.get("外来", 1.0)
           + float(n) * FEE_REVISION_PROFIT_UPLIFT.get("入院", 1.0))
    return round(adj / 1000, 1)


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
    gmap = dict(zip(df["月"], df["外来粗利"])) if "外来粗利" in df.columns else {}
    nmap = dict(zip(df["月"], df["入院粗利"])) if "入院粗利" in df.columns else {}
    dates = [m.strftime("%-m月") for m in months]
    cur = [round(pmap[m] / 1000, 1) for m in months]

    prev_adjusted = False

    def _prev_at(m):
        nonlocal prev_adjusted
        pm = m - pd.DateOffset(years=1)
        if pm not in pmap:
            return None
        if _prev_needs_revision_adjust(m):
            adj = _revision_adjusted_prev(gmap, nmap, pm)
            if adj is not None:
                prev_adjusted = True
                return adj
        return round(pmap[pm] / 1000, 1)

    prev = [_prev_at(m) for m in months]
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
            prev.append(_prev_at(pm))
            proj = p["value"]

    return {"dates": dates, "cur": cur, "prev": prev, "ref": ref, "rate": rate,
            "latest": months[-1], "proj": proj, "proj_month": (p["month"] if proj else None),
            "prev_adjusted": prev_adjusted}


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
    """narrate_leveling_actions が None（oMLX未起動/失敗）のときの定型文。

    2026-07: AI率95%になり出番は減ったが、oMLX停止時に全部門が同一文へ縮退しないよう
    事実の語彙（週後半集中の曜日名・補充3段階・ディップの形）で組み合わせ分岐する。
    レバー文は _leveling_levers（LLMプロンプトと共通）から取り、文言の乖離を防ぐ。"""
    state = _q_state_trend(unit.get("retention"), unit.get("room_delta_4w"))
    latewk = _q_latewk_discharge(dd)
    adm = _q_weekend_adm(dd)
    dip = _q_census_dip(dd)
    thin = _q_thin_latewk_adm(dd)
    room = unit.get("room_per_week", 0) or 0

    if room <= 0.5:
        body = "週末も平日とほぼ同じ在院を保てています。今の入退院のリズムが手本になっています。"
        # 添削フィードバックループ P2: 人手 override は「現状維持。」で完結する action を
        # 例外なく書き換え、良好時でも次に伸ばす一手を添えていた（救急科・総合内科）。
        action = "週末の入退院リズムはこのまま継続しつつ、在院水準のさらなる底上げを図りましょう。"
        return {"body": body, "action": action}

    causes = []
    if latewk and latewk["level"] == "strong":
        causes.append(f"退院が{latewk['days']}に強く集中し")
    elif latewk and latewk["level"] == "mild":
        causes.append(f"退院が{latewk['days']}に寄り")
    if adm and adm["level"] == "none":
        causes.append("週末の入院補充がほとんどなく")
    elif adm and adm["level"] == "limited":
        causes.append("週末の入院補充も限られ")
    if causes:
        body = "".join(c if i == 0 else "、" + c for i, c in enumerate(causes)) \
               + "、週末に在院が落ち込みやすい構造です。"
    else:
        body = f"{state}。週末のタイミングを少し整えると、平日に積み上げた在院を保ちやすくなります。"
    if dip:
        body += f"{dip}形です。"

    disperse, refill, mode = _leveling_levers(entity, latewk, adm, thin)
    if mode == "disperse":
        action = f"{disperse}（退院の平準化を主に）。在院日数は延ばさず、回転で取り戻す。"
    elif mode == "refill":
        action = f"{refill}（週末入院での補充を主に）。"
    else:
        action = f"{disperse}。あわせて{refill}。"
    return {"body": body, "action": action}


# 「達成しているが直近は鈍化」を素通しで「現状維持」に丸めると、傾向を渡した意味が
# 消えて事実と反する安心を伝えてしまう。定型文（oMLX未起動時のみ使用）でもこのケースは
# 分けて注意喚起する。達成側の判定は5段階（達成/大きく上回る）を両方拾う（"上回" or "達成"）。
def _is_met(state: Optional[str]) -> bool:
    return bool(state) and ("達成" in state or "上回" in state)


def _fallback_move_admission(state: Optional[str], peer: Optional[str] = None) -> dict:
    """新入院トピックの定型文（oMLX未起動/ハルシネーション棄却時）。peer=同種科内の相対位置。"""
    lead = f"同種の診療科では{peer}ながら、" if peer else ""
    if _is_met(state) and "鈍って" in state:
        return {"body": f"{lead}新入院は{state}状況です。",
                "action": "受け入れ体制を維持しつつ、直近の受け入れ状況を注視しましょう。"}
    if _is_met(state):
        return {"body": f"{lead}新入院は直近で目標水準を確保できています。",
                "action": "現状の受け入れ体制を維持しましょう。"}
    # state 自体が「直近は〜」の傾向を含むため接頭辞「直近で」は付けない（重複回避）
    return {"body": f"{lead}新入院は{state or '目標を下回っている'}状況です。",
            "action": "地域医療連携での紹介受け入れ強化や予定入院枠の調整で、新入院の患者数増に取り組みましょう。"}


def _fallback_move_ward_admission(state: Optional[str]) -> dict:
    """一般病棟（特例でない病棟）向け・新規受け入れトピックの定型文（oMLX未起動/棄却時）。
    病棟は外来・地域連携の窓口を持たないため紹介・地域医療連携は一切含めない。actionは
    UNIT_ROLE_LEVERS["ward"]の語彙（空床の把握・ベッドコントロール、緊急入院/転入の受け入れ、
    退院・転棟のタイミング調整）で書く。peerは病棟軸に無いため引数を持たない。"""
    if _is_met(state) and "鈍って" in state:
        return {"body": f"新規の受け入れは{state}状況です。",
                "action": "空床の把握とベッドコントロールを続けつつ、直近の受け入れ状況を注視しましょう。"}
    if _is_met(state):
        return {"body": "新規の受け入れは直近で目標水準を確保できています。",
                "action": "空床の把握とベッドコントロールを続け、緊急入院・転入の受け入れ体制を維持しましょう。"}
    # state 自体が「直近は〜」の傾向を含むため接頭辞「直近で」は付けない（重複回避）
    return {"body": f"新規の受け入れは{state or '目標を下回っている'}状況です。",
            "action": "空床の把握を細かく行い、緊急入院・転入の受け入れと退院・転棟のタイミング調整で新規の受け入れを増やしましょう。"}


def _fallback_move_surgery(state: Optional[str], peer: Optional[str] = None,
                           label: str = "全身麻酔手術") -> dict:
    """手術トピックの定型文（oMLX未起動/ハルシネーション棄却時）。peer=同種科内の相対位置。
    label=科ごとの手術KPI名（眼科=全手術 / 他外科系=全身麻酔手術・病院全体=全身麻酔手術）。"""
    lead = f"外科系の診療科では{peer}ながら、" if peer else ""
    if _is_met(state) and "鈍って" in state:
        return {"body": f"{lead}{label}は{state}状況です。",
                "action": "手術枠の運用を維持しつつ、直近の稼働状況を注視しましょう。"}
    if _is_met(state):
        return {"body": f"{lead}{label}は直近で目標水準を確保できています。",
                "action": "現状の手術枠運用を維持しましょう。"}
    return {"body": f"{lead}{label}は{state or '目標を下回っている'}状況です。",
            "action": f"手術枠の稼働状況の確認と執刀医との症例調整で、{label}の件数増に専念しましょう。"}


def _fallback_move_emergency_leveling(unit: dict) -> dict:
    """救命救急系病棟(4A/4C)向け・週末在院トピックの定型文。"""
    room = unit.get("room_per_week", 0) or 0
    if room <= 0.5:
        return {"body": "週末も平日とほぼ同じ在院を保てています。今の受け入れ体制が手本になっています。",
                # 添削由来（P2）: 「現状維持。」で完結させない
                "action": "週末も平日と同水準の受け入れ体制を継続しつつ、受け入れ余力のさらなる拡大を図りましょう。"}
    return {"body": "週末は在院がやや落ち込みやすい状況です。",
            "action": "転棟・転出（下り搬送）の判断を迅速化し、週末の受け入れ余地を確保しましょう。"}


def _fallback_move_emergency_admission(state: Optional[str]) -> dict:
    """救命救急系病棟(4A/4C)向け・新規受け入れトピックの定型文。"""
    if _is_met(state) and "鈍って" in state:
        return {"body": f"緊急入院・転棟の受け入れは{state}状況です。",
                "action": "受け入れ体制を維持しつつ、直近の受け入れ状況を注視しましょう。"}
    if _is_met(state):
        return {"body": "緊急入院・転棟の受け入れは直近で目標水準を確保できています。",
                "action": "現状の受け入れ体制を維持しましょう。"}
    return {"body": f"緊急入院・転棟の受け入れは{state or '目標を下回っている'}状況です。",
            "action": "後方病床との調整や病床運用の見直しにより、受け入れ余地の確保を検討しましょう。"}


# ════════════════════════════════════════════════════════════
# 「この期間の一手」トピック選定（病床平準化に限定しない）
# ════════════════════════════════════════════════════════════
# 病床平準化ののびしろ(room_per_week)だけを常に採用すると、新入院/全麻の方が
# 明確に不足している部門でも「現状維持」の定型文で埋まってしまう。3トピックの
# 目標未達の大きさを比べ、最も目立つものを一手のトピックに選ぶ。
ACTION_TOPIC_MIN_SCORE = 0.12   # これ未満の不足差はノイズ扱い→病床平準化を既定にする
# 全麻(surgery)の優先度は2段階で強化してきた:
#   ①足切りの非対称（診療科98%/病院全体95%で候補入り）→ ただし leveling が相対スコアで
#     ほぼ常に勝ち、外科系でも手術の一手が出にくかった。
#   ②2026-07-22: 外科系診療科は達成状況によらず手術を常に主トピックへ固定
#     （発信方針=外科系の一手は必ず全麻〔眼科=全手術〕コメントで始める）。
# これにより SURGERY_TOPIC_MIN_SCORE は診療科軸では実効を持たない（目標未設定の科は
# forced 分岐に入らず従来選定のまま）。病院全体サマリは②の対象外で、①の
# SURGERY_TOPIC_MIN_SCORE_HOSPITAL による選定を維持する。
SURGERY_TOPIC_MIN_SCORE = 0.02          # 外科系診療科: ②により実効なし（後方互換で残置）
SURGERY_TOPIC_MIN_SCORE_HOSPITAL = 0.05  # 病院全体: 全麻達成率95%未満で一手候補に


def _admission_gap_score(na, na_tgt) -> float:
    if not na_tgt or na is None:
        return 0.0
    return max(0.0, 1.0 - na / na_tgt)


def _surgery_gap_score(sv, surg_tgt) -> float:
    if not surg_tgt or sv is None:
        return 0.0
    return max(0.0, 1.0 - sv / surg_tgt)


def _select_action_topic(type_key: str, room: float, max_room: float,
                         na, na_tgt, sv, surg_tgt,
                         *, surgery_min: float = SURGERY_TOPIC_MIN_SCORE):
    """"leveling"(病床平準化) / "admission"(新入院) / "surgery"(全麻・外科系のみ) の
    うち主トピックを選ぶ。leveling は room_per_week を全ユニット中の相対値、
    admission/surgery は目標比の絶対的な不足率で評価する（スケールが完全には揃わないが、
    いずれも0〜1の「どれだけ気にすべきか」の目安として扱う）。

    外科系（手術目標あり）は達成状況によらず surgery を主トピックに固定する
    （2026-07-22 発信方針: 外科系の一手は必ず全麻〔眼科=全手術〕コメントで始める。
    達成時は状態文＋維持系 action になり、他トピックの未達は副トピックの一行併記へ降格）。

    それ以外（内科系・病棟・手術目標未設定の外科系）は従来どおり、トピックごとの
    最小スコア（＝足切り）を満たす eligible の中で生スコア最大を主トピックに、
    次点を副トピックにする。eligible が無ければ leveling を既定にする
    （room<=0.5 なら _fallback_move が「現状維持」の定型文を返す）。

    戻り値=(primary, secondary, scores)。secondary=主以外で足切りを満たしスコア最大の
    トピック（無ければ None）。複数指標が未達の科で「主トピックの一手＋副トピックを本文で
    軽く併記」する P3(トンネル視野の解消)に使う。内科系・病棟は surgery キーが scores に
    入らないため従来と完全に同一挙動（surgery_min は無関係）。
    """
    scores = {"leveling": (room / max_room) if max_room else 0.0,
              "admission": _admission_gap_score(na, na_tgt)}
    mins = {"leveling": ACTION_TOPIC_MIN_SCORE, "admission": ACTION_TOPIC_MIN_SCORE}
    if type_key == "surgical":
        scores["surgery"] = _surgery_gap_score(sv, surg_tgt)
        mins["surgery"] = surgery_min
        if surg_tgt:
            sec = {k: v for k, v in scores.items()
                   if k != "surgery" and v >= mins[k]}
            return "surgery", (max(sec, key=sec.get) if sec else None), scores
    eligible = {k: v for k, v in scores.items() if v >= mins[k]}
    primary = max(eligible, key=eligible.get) if eligible else "leveling"
    sec = {k: v for k, v in scores.items() if k != primary and v >= mins[k]}
    secondary = max(sec, key=sec.get) if sec else None
    return primary, secondary, scores


def _select_hospital_topic(topic_scores: dict) -> str:
    """病院全体サマリの主トピック選定（_select_action_topic と同じ eligible ルール）。
    全麻は SURGERY_TOPIC_MIN_SCORE_HOSPITAL（達成率95%未満）で候補に入る。
    eligible が無ければ leveling を既定にする。"""
    mins = {"leveling": ACTION_TOPIC_MIN_SCORE, "admission": ACTION_TOPIC_MIN_SCORE,
            "surgery": SURGERY_TOPIC_MIN_SCORE_HOSPITAL}
    eligible = {k: v for k, v in topic_scores.items() if v >= mins.get(k, ACTION_TOPIC_MIN_SCORE)}
    return max(eligible, key=eligible.get) if eligible else "leveling"


def _special_narration_kind(entity: str, code: str, name: str) -> Optional[str]:
    """特例ユニットの種別を返す（予定入院/紹介という業務前提が無く専用文言を使う）。
    None=通常。"emergency"=救命救急病棟(4A/4C)・"critical_care"=重症ケア病棟(ICU/HCU)・
    "er_dept"=救急科。トピック(leveling/admission)の選定は共通ロジックのままで、
    呼び出す narrate_* だけを差し替える（週末平準化バッチの skip 判定と一手ディスパッチの
    両方で同じ判定を使い、二重管理を避ける）。判定は config.unit_narration_kind に一元化
    （triage/eval_rules と同じ判定を共有するための単一の真実。ここは薄いラッパー）。"""
    return unit_narration_kind(entity, code=code, name=name)


# ── P2-b: 同種科内の相対位置（上位/中位/下位・診療科軸のみ） ──
def _peer_tier(name: str, ratio_map: dict, peer_names: list) -> Optional[str]:
    """peer_names(同種科)の達成率(実績/目標)で name の位置を 上位/中位/下位 に離散化。
    比較母数が3科未満、または達成率不明なら None（“分かってる人が書いた感”を出す事実）。

    3-3: 境界(1/3・2/3)の±0.06は**緩衝帯＝None**（peerに言及しない）。定期配布で
    境界付近の科が毎月「中位↔下位」とブレるのが不自然なため、階級をまたぐには
    緩衝帯の幅ぶんの実変化を要するようにする（状態ファイル無しのヒステリシス代替）。"""
    ranked = sorted((n for n in peer_names if ratio_map.get(n) is not None),
                    key=lambda n: ratio_map[n], reverse=True)
    if name not in ranked or len(ranked) < 3:
        return None
    frac = ranked.index(name) / (len(ranked) - 1)
    if frac <= 0.28:
        return "上位"
    if frac < 0.40:
        return None      # 上位/中位の緩衝帯
    if frac <= 0.61:
        return "中位"
    if frac < 0.73:
        return None      # 中位/下位の緩衝帯
    return "下位"


def _same_type_names(ratio_map: dict, type_key: str) -> list:
    """ratio_map のうち type_key(surgical/internal) が一致する診療科名。"""
    return [n for n in ratio_map
            if ("surgical" if n in SURGERY_EVAL_DEPTS else "internal") == type_key]


# ── P3: 副トピックを本文へ併記する決定論クローズ（actionは主トピックに集中） ──
# 接続は極性中立の「なお、〜は」を使う。主文はLLM自由文で「未達でも前年比は前向きに触れる」
# 指示があるためポジティブに終わり得るが、「あわせて、〜も」は直前も同調子である前提を含意し、
# 逆説的な内容を順接で繋ぐ違和感を生んでいた（主文の極性は判定不能ゆえ中立接続に統一）。
def _secondary_clause(topic: Optional[str], na_state: Optional[str],
                      surg_state: Optional[str]) -> Optional[str]:
    if topic == "admission" and na_state:
        return f"なお、新入院は{na_state}状況です。"
    if topic == "surgery" and surg_state:
        return f"なお、全身麻酔手術は{surg_state}状況です。"
    if topic == "leveling":
        return "なお、週末在院の維持には改善余地があります。"
    return None


def _ma_window_trend(cur: list, prior_end: int, pt: float) -> str:
    """MA系列(cur, 末尾=直近)の直近7点平均 vs 「prior_end点前」を終点とする7点平均の
    変化率(%)を 上昇/低下/横ばい に離散化する（トリアージの7d/28dスプレッドと同じ
    "窓平均 vs 窓平均" 方式）。単一点同士の比較（旧方式）は局所的なブレの影響を
    受けやすいため、窓平均に揃えてノイズを抑える。"""
    if len(cur) < prior_end:
        return "—"
    recent = sum(cur[-7:]) / 7
    prior = sum(cur[-prior_end:-(prior_end - 7)]) / 7
    if not prior:
        return "横ばい"
    pct = (recent - prior) / abs(prior) * 100
    if pct > pt:
        return "上昇"
    if pct < -pt:
        return "低下"
    return "横ばい"


def _nadm_highlight(na, na_tgt, na_series) -> Optional[str]:
    """内科系の一手に添える新入院ハイライト1行（数値駆動・AI不要・Tier2-2）。
    外科=_surg_highlight・病棟=_util_highlight と同型で、直近7日累計(件/週) vs 週次目標
    ＋28日線の方向＋目標までの差を1行に。「改善余地」の抽象語を数字で接地する。"""
    if not na_tgt:
        return None
    na = na or 0
    rate = round(na / na_tgt * 100)
    cur = [v for v in (na_series.get("cur") or []) if v is not None] if na_series else []
    trend = _ma_window_trend(cur, prior_end=28, pt=5)   # ≒4週前を終点とする窓（28日MAの日次系列）
    gap = na_tgt - na
    if gap > 0.5:
        gap_phrase = f"あと約{gap:.0f}件/週で目標"
    elif gap < -0.5:
        gap_phrase = f"目標を{-gap:.0f}件/週上回る"
    else:
        gap_phrase = "ほぼ目標どおり"
    return (f"新入院：直近7日 {na:g}件／週目標{na_tgt:g}（{rate}%）。"
            f"28日線は{trend}／{gap_phrase}")


def _surg_highlight(sv, surg_tgt, surg_series, dept: str = None) -> Optional[str]:
    """外科系の一手に添える手術ハイライト1行（数値駆動・AI不要）。
    直近7日累計(件/週) vs 週次目標＋28日線の方向＋目標までの差を1行に。"""
    if not surg_tgt:
        return None
    rate = round((sv or 0) / surg_tgt * 100)
    cur = [v for v in (surg_series.get("cur") or []) if v is not None] if surg_series else []
    trend = _ma_window_trend(cur, prior_end=28, pt=5)   # ≒4週前を終点とする窓（営業日換算）
    gap = surg_tgt - (sv or 0)
    if gap > 0.5:
        gap_phrase = f"あと約{gap:.0f}件/週で目標"
    elif gap < -0.5:
        gap_phrase = f"目標を{-gap:.0f}件/週上回る"
    else:
        gap_phrase = "ほぼ目標どおり"
    label = surgery_metric_label(dept, short=True) if dept else "全麻"
    return (f"{label}：直近7日 {sv:g}件／週目標{surg_tgt:g}（{rate}%）。"
            f"28日線は{trend}／{gap_phrase}")


def _util_highlight(util_now, tgt_util, beds, util_series) -> Optional[str]:
    """病棟の一手に添える病床利用率ハイライト1行（数値駆動・AI不要）。
    直近7日平均の対定員利用率 vs 目標利用率＋28日線の方向＋目標までの差を1行に。"""
    if util_now is None:
        return None
    cur = [v for v in (util_series.get("cur") or []) if v is not None] if util_series else []
    trend = _ma_window_trend(cur, prior_end=35, pt=2)   # ≒4週前を終点とする窓（在院28日MAの日次系列）
    tgt_txt = f"目標{tgt_util:g}%" if tgt_util is not None else "目標未設定"
    beds_txt = f"・{beds:g}床" if beds else ""
    gap_phrase = ""
    if tgt_util is not None:
        gap = tgt_util - util_now
        if gap > 1:
            gap_phrase = f"／あと約{gap:.0f}ポイントで目標"
        elif gap < -1:
            gap_phrase = f"／目標を{-gap:.0f}ポイント上回る"
        else:
            gap_phrase = "／ほぼ目標どおり"
    return (f"病床利用率：直近7日平均 {util_now:g}%（{tgt_txt}{beds_txt}）。"
            f"28日線は{trend}{gap_phrase}")


# ════════════════════════════════════════════════════════════
# ① 新情報系の事実quantizer（予定/緊急内訳・OR稼働・連休文脈）
# ════════════════════════════════════════════════════════════
def _q_planned_mix(adm, base_date, dept_name) -> Optional[str]:
    """新入院に占める予定入院の割合（直近4完全週 vs 前4週・診療科軸のみ）。

    予定=「入院患者数」列・新入院=予定+緊急（preprocess）。実データ較正(2026-07-02):
    Δは概ね±10pt内に分布し、実変化はリウマチ−31pt/皮膚−20pt/消化器−16pt等。
    直近4週の新入院20件未満は None（小規模ノイズ）。トピックのレバー選び
    （緊急中心の科に紹介・予定枠の一般論を当てない）のための事実。"""
    monday = base_date - timedelta(days=base_date.weekday())
    d = adm[(adm["科_表示"]) & (adm["診療科名"] == dept_name)]

    def _share(lo, hi):
        w = d[(d["日付"] >= lo) & (d["日付"] <= hi)]
        na = w["新入院患者数"].sum()
        return ((w["入院患者数"].sum() / na) if na else None), na

    cur, n_cur = _share(monday - timedelta(days=28), monday - timedelta(days=1))
    prev, _ = _share(monday - timedelta(days=56), monday - timedelta(days=29))
    if cur is None or n_cur < 20:
        return None
    if prev is not None:
        dlt = (cur - prev) * 100
        if dlt <= -10:
            return "新入院に占める予定入院の割合が下がってきている（緊急への依存が増えている）"
        if dlt >= 10:
            return "新入院に占める予定入院の割合が持ち直してきている"
    if cur < 0.30:
        return "新入院は緊急入院が中心（予定入院は少ない）"
    return None


def _q_or_load(surg, base_date) -> Optional[str]:
    """手術室全体の直近10営業日平均稼働率→3段階の定性文（全麻トピック共通の文脈）。
    実データ較正(2026-07-02): 平均69.9%・範囲56〜78%。空きがあれば「症例の積み増し」、
    埋まっていれば「枠の調整・効率化」へ action を向けるための事実。"""
    biz = sorted(d for d in surg[surg["平日"]]["手術実施日"].unique()
                 if pd.Timestamp(d) <= base_date)[-10:]
    if len(biz) < 5:
        return None
    u = [daily_or_utilization(surg, pd.Timestamp(d)) for d in biz]
    avg = sum(u) / len(u)
    if avg >= 85:
        return "手術室全体の枠はほぼ埋まっている"
    if avg >= 70:
        return "手術室全体の稼働はおおむね高いが、空き枠もある"
    return "手術室全体の稼働には余裕がある（空き枠がある）"


def _q_holiday_week(adm, base_date) -> Optional[str]:
    """直近7日窓に祝日（暦上の平日だが営業日でない日＝GW/年末年始含む）を含むか。
    含む週の未達を科の不調と誤読させないためのフェアネス文脈（①-4）。"""
    w = adm[(adm["日付"] > base_date - timedelta(days=7)) & (adm["日付"] <= base_date)]
    if len(w) == 0:
        return None
    days = w.groupby("日付")["平日"].first()
    hol = [d for d, biz in days.items() if d.weekday() < 5 and not biz]
    return "集計期間に祝日を含む（予定入院や手術は構造的に少なくなりやすい）" if hol else None


# ════════════════════════════════════════════════════════════
# ① 差分ナラティブ（前回レポート比較・バケット遷移のみ・悪化は控えめ）
# ════════════════════════════════════════════════════════════
# 週1〜2回更新でも比較の地平を約4週に固定する（近接比較はローリング窓の重複で
# ノイズが支配的になり、境界フリップの「鞭打ち」で信頼を損なう）。言及は量子化
# バケットの遷移のみ＝バケット幅が自然なヒステリシスとして働く。文はすべて
# Python生成・数字なし（「約4週前」も数字を含むため「前回レポート時点」と表現）。
_GAP_ORDER = {"poor": 0, "mild": 1, "close": 2, "met": 3, "exceed": 4}
_RET_ORDER = {"poor": 0, "mild": 1, "good": 2}
_LATEWK_ORDER = {"flat": 0, "mild": 1, "strong": 2}   # 大=集中が強い（悪い向き）
_WADM_ORDER = {"none": 0, "limited": 1, "some": 2}    # 大=補充がある（良い向き）


def _gap_delta_fact(label: str, prev, cur) -> Optional[tuple]:
    """結果指標（新入院/全麻/病院KPI）のバケット遷移→(改善フラグ, 事実文)。
    改善は1段階から言及、悪化は「達成圏からの転落 or 2段階以上」だけ言及
    （職員発信トーン・境界ノイズの抑制）。"""
    if prev not in _GAP_ORDER or cur not in _GAP_ORDER:
        return None
    d = _GAP_ORDER[cur] - _GAP_ORDER[prev]
    if d >= 1:
        if _GAP_ORDER[cur] >= _GAP_ORDER["met"] and _GAP_ORDER[prev] < _GAP_ORDER["met"]:
            return (True, f"{label}は前回レポート時点の未達から、目標水準に到達した")
        return (True, f"{label}は、前回レポート時点より改善している")
    if d <= -2 or (_GAP_ORDER[prev] >= _GAP_ORDER["met"] and _GAP_ORDER[cur] <= _GAP_ORDER["mild"]):
        return (False, f"{label}は、前回レポート時点から明確に低下している")
    return None


# 差分事実の次元 → それが自然に属する一手トピック（topic整合の優先付け用）。
# leveling=週末平準化の原因/結果、admission=新入院、surgery=全麻。
_DELTA_DIM_TOPIC = {"latewk": "leveling", "wadm": "leveling", "thin": "leveling",
                    "ret": "leveling", "na": "admission", "surg": "surgery"}


def _delta_facts(prev: Optional[dict], cur: dict) -> list:
    """アンカー時点の状態タグ prev と現在 cur を比べ、(改善フラグ, 次元, 事実文) の候補を返す。

    並び＝原因事実（退院集中/週末補充/薄い曜日/維持率）→結果指標（新入院/全麻）。
    原因事実が先なのは、前回レポートの推奨レバーが動いたかのフィードバック
    （「前回課題とした金曜集中が緩和」）こそ差分ナラティブ固有の価値のため。"""
    if not prev:
        return []
    out = []
    lp, lc = prev.get("latewk"), cur.get("latewk")
    if lp in _LATEWK_ORDER and lc in _LATEWK_ORDER:
        if lp == "strong" and _LATEWK_ORDER[lc] < _LATEWK_ORDER["strong"]:
            out.append((True, "latewk", "前回レポートで課題とした週後半への退院集中は、前回時点より緩和している"))
        elif lp == "mild" and lc == "flat":
            out.append((True, "latewk", "退院曜日の偏りは、前回レポート時点より平準化してきている"))
        elif lp != "strong" and lc == "strong":
            out.append((False, "latewk", "退院の週後半への集中は、前回レポート時点より強まっている"))
    wp, wc = prev.get("wadm"), cur.get("wadm")
    if wp in _WADM_ORDER and wc in _WADM_ORDER:
        if _WADM_ORDER[wc] > _WADM_ORDER[wp] and wp != "some":
            out.append((True, "wadm", "前回レポートで乏しかった週末入院の補充は、前回時点より改善している"))
        elif wc == "none" and wp != "none":
            out.append((False, "wadm", "週末入院の補充は、前回レポート時点より弱まっている"))
    if prev.get("thin") in ("木曜", "金曜") and cur.get("thin") is None:
        out.append((True, "thin", "前回レポートで薄かった週末前の予定入院の谷は、解消してきている"))
    rp, rc = prev.get("ret"), cur.get("ret")
    if rp in _RET_ORDER and rc in _RET_ORDER:
        if _RET_ORDER[rc] > _RET_ORDER[rp]:
            out.append((True, "ret", "週末在院の維持は、前回レポート時点より改善している"))
        elif _RET_ORDER[rp] - _RET_ORDER[rc] >= 2:
            out.append((False, "ret", "週末在院の維持は、前回レポート時点から明確に低下している"))
    for label, key in (("新入院", "na"), ("全身麻酔手術", "surg")):
        g = _gap_delta_fact(label, prev.get(key), cur.get(key))
        if g:
            out.append((g[0], key, g[1]))
    return out


def _pick_delta(prev: Optional[dict], cur: dict, topic: Optional[str] = None) -> Optional[str]:
    """候補から1件だけ選ぶ（事実過載を防ぐ）。優先順:
      1. 選定トピックに次元が一致する改善（コメント本体と噛み合う前向きな変化）
      2. 他次元の改善（前回比の良い知らせ＝士気・フェアネス。褒める方向は他トピックでも歓迎）
      3. 選定トピックに一致する悪化（議論中のレバーそのものの後退＝載せる価値がある）
    他次元の悪化は載せない（admissionコメントに週末補充の後退が混じる等の非連続・
    名指し批判的トーンを避ける＝職員発信の思想）。topic=None は全次元を「一致」扱い。"""
    cands = _delta_facts(prev, cur)
    if not cands:
        return None
    on = [c for c in cands if topic is None or _DELTA_DIM_TOPIC.get(c[1]) == topic]
    off = [c for c in cands if c not in on]
    for imp, _dim, text in on:            # 1. on-topic 改善
        if imp:
            return text
    for imp, _dim, text in off:           # 2. off-topic 改善
        if imp:
            return text
    for imp, _dim, text in on:            # 3. on-topic 悪化
        if not imp:
            return text
    return None


# ── 事実スナップショット（差分ナラティブの状態アーカイブ・dept_reports/_state/） ──
def _select_anchor_date(dates: list, base_date) -> Optional[pd.Timestamp]:
    """アンカー基準日の選定: 21日以上古い候補から、42日以内の同曜日を優先しつつ
    28日（=ちょうど4週・曜日が揃い7日窓の曜日構成バイアスが消える）に最も近いもの。
    候補が無ければ None（初回導入時は差分言及なしで静かに立ち上がる）。"""
    cands = [d for d in dates if (base_date - d).days >= 21]
    if not cands:
        return None
    same_wd = [d for d in cands
               if d.weekday() == base_date.weekday() and (base_date - d).days <= 42]
    pool = same_wd or cands
    return min(pool, key=lambda d: (abs((base_date - d).days - 28), (base_date - d).days))


def load_delta_anchor(state_dir, base_date) -> Optional[dict]:
    """_state/facts_*.json から差分ナラティブのアンカー（前回状態）を読む。"""
    p = Path(state_dir)
    if not p.is_dir():
        return None
    snaps = {}
    for f in p.glob("facts_*.json"):
        try:
            snaps[pd.Timestamp(f.stem[len("facts_"):])] = f
        except ValueError:
            continue
    d = _select_anchor_date(list(snaps), base_date)
    if d is None:
        return None
    try:
        data = json.loads(snaps[d].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    data["_anchor_date"] = f"{d:%Y-%m-%d}"
    return data


def save_facts_snapshot(state_dir, base_date, units: dict, hospital: dict) -> Path:
    """今回の量子化状態タグを保存する（同一基準日は上書き＝再ビルドで増殖しない）。"""
    p = Path(state_dir)
    p.mkdir(parents=True, exist_ok=True)
    out = p / f"facts_{base_date:%Y-%m-%d}.json"
    out.write_text(json.dumps({"base_date": f"{base_date:%Y-%m-%d}",
                               "units": units, "hospital": hospital},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    return out


# ════════════════════════════════════════════════════════════
# チャート組立（描画はヒーロー/半幅の高さ確定後）
# ════════════════════════════════════════════════════════════
def _trend_part(kind, name, series, ref, ref_label, unit, badge, note="") -> Optional[dict]:
    """A/B/C/D 共通のトレンドパーツ仕様。series が空なら None。"""
    if not series or not series.get("cur") or all(v is None for v in series["cur"]):
        return None
    return {"kind": kind, "name": name, "badge": badge, "note": note,
            "is_dow": False, "_data": series, "_ref": ref, "_ref_label": ref_label,
            "_unit": unit, "_win": PART_WIN[kind], "_color": PART_LINE}


def _dow_part(dd) -> Optional[dict]:
    if not dd:
        return None
    svg = _render_dow_svg(dd["discharge"]["w8"], dd["admission"]["w8"], dd["census"]["w8"])
    return {"kind": "E", "name": "曜日プロファイル", "badge": None,
            "note": "", "is_dow": True, "svg": svg}


def _build_parts(adm, surg, base_date, entity, name, code, dd, r7_inp, r7_nadm,
                 r7_surg, targets, surg_targets, profit_series) -> dict:
    """利用可能なグラフパーツ {A,B,C,D,E} を作る（無いものは欠落）。

    トレンド線は公開版ダッシュボード dept.html の既定（28日移動平均）と統一。
    手術は週次合計(件/週)の28日移動平均・目標は週次目標そのもの。KPIは直近7日。
    """
    is_ward = entity == "ward"
    by = "by_ward" if is_ward else "by_dept"
    tgt_axis = "ward" if is_ward else "dept"
    parts = {}

    r7 = r7_inp[by].get(code)
    inp_tgt = targets.get("inpatient", {}).get(tgt_axis, {}).get(code)

    # A: 在院（28日移動平均）。病棟は病床利用率 = 在院28日MA ÷ 稼働床 ×100。
    #    基準線は定員100%でなく「目標利用率＝日平均在院目標 ÷ 病床数 ×100」。
    if is_ward:
        beds = targets.get("inpatient", {}).get("ward_beds", {}).get(code)
        cen = _unit_ma_series(adm, "在院患者数", base_date, "病棟コード", code, 28, "mean")
        if beds:
            scale = lambda v: round(v / beds * 100, 1) if v is not None else None
            cen = {**cen, "cur": [scale(v) for v in cen["cur"]], "prev": [scale(v) for v in cen["prev"]]}
            util_now = _rate(r7, beds)
            tgt_util = round(inp_tgt / beds * 100, 1) if inp_tgt else None
            ref = tgt_util if tgt_util is not None else 100
            ref_label = f"目標 {tgt_util:g}%" if tgt_util is not None else "定員100%"
            badge = ((f"対定員 {util_now:g}%",
                      "ok" if (tgt_util is None or util_now >= tgt_util) else "wr")
                     if util_now else None)
            parts["A"] = _trend_part("A", "病床利用率", cen, ref, ref_label, "%", badge)
    else:
        s = _unit_ma_series(adm, "在院患者数", base_date, "診療科名", name, 28, "mean")
        parts["A"] = _trend_part("A", "在院患者数", s, inp_tgt or 0, f"目標{inp_tgt:g}" if inp_tgt else "",
                                 "人", _ach_badge(r7, inp_tgt))

    # B: 新入院（28日移動平均=件/日、目標=週次÷7。KPI/バッジは直近7日累計）。軸で列が変わる
    na = r7_nadm[by].get(code)
    na_tgt = targets.get("new_admission", {}).get(tgt_axis, {}).get(code)
    daily_na_tgt = round(na_tgt / 7, 1) if na_tgt else 0
    b_col = "新入院患者数_病棟" if is_ward else "新入院患者数"
    b_grp, b_val = ("病棟コード", code) if is_ward else ("診療科名", name)
    bs = _unit_ma_series(adm, b_col, base_date, b_grp, b_val, 28, "mean")
    parts["B"] = _trend_part("B", "新入院患者数", bs, daily_na_tgt,
                             f"目標{daily_na_tgt:g}" if daily_na_tgt else "", "件/日",
                             _ach_badge(na, na_tgt))

    # C: 手術（外科系診療科のみ）。公開版 dept.html と統一＝週次合計(件/週)の28日移動平均、
    #    目標線は週次目標そのもの（flat）。KPI/バッジは直近7日累計(件/週) vs 週次目標。
    if not is_ward and name in SURGERY_EVAL_DEPTS:
        cs = _unit_surg_weekly_series(surg, base_date, name)
        surg_tgt = surg_targets.get(name) if isinstance(surg_targets, dict) else None
        sv = r7_surg["by_dept"].get(name, 0)
        parts["C"] = _trend_part("C", surgery_metric_label(name), cs, surg_tgt or 0,
                                 f"目標{surg_tgt:g}" if surg_tgt else "", "件/週",
                                 _ach_badge(sv, surg_tgt))

    # D: 粗利（診療科のみ・確報＋当月見込み）
    if not is_ward and profit_series:
        rate = profit_series["rate"]
        if profit_series.get("proj") is not None:
            note = (f"実線=確報(最新{profit_series['latest'].strftime('%-m月')})／"
                    f"点線={profit_series['proj_month'].strftime('%-m月')}は診療実績ベースの見込（暫定）")
        else:
            note = f"確報ベース・最新 {profit_series['latest'].strftime('%Y年%-m月')}"
        if profit_series.get("prev_adjusted"):
            note += _REV_NOTE
        badge = (f"達成率 {rate:g}%", "ok" if (rate or 0) >= 100 else "wr") if rate is not None else None
        ref = profit_series["ref"] or 0
        parts["D"] = _trend_part("D", "粗利", profit_series, ref,
                                 f"目標{ref:g}" if ref else "", "百万円", badge, note=note)

    # E: 曜日プロファイル
    e = _dow_part(dd)
    if e:
        parts["E"] = e

    # ICU/HCU/8階B は前年同期に比較可能なデータがない → 前年同期線を出さない
    if is_ward and code in NO_PREVYEAR_WARDS:
        for p in parts.values():
            if p and not p["is_dow"] and p.get("_data"):
                p["_data"]["prev"] = [None] * len(p["_data"]["cur"])
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
        return [_kpi(surgery_metric_label(name), "直近7日累計", _fmt(sv), "件", lead=True,
                     tgt=f"目標 {surg_tgt:g}/週" if surg_tgt else "目標未設定", ok=_ok(sv, surg_tgt)),
                prof_kpi, inp_kpi(False), nadm_kpi]
    if type_key == "internal":
        return [inp_kpi(True), prof_kpi, nadm_kpi, ret_kpi]
    # ward（lead=病床利用率も目標利用率との達成で色付け）
    beds = targets.get("inpatient", {}).get("ward_beds", {}).get(code)
    util = _rate(r7, beds)
    tgt_util = round(inp_tgt / beds * 100, 1) if (inp_tgt and beds) else None
    util_tgt = (f"目標 {tgt_util:g}%・{beds:g}床" if tgt_util is not None
                else (f"稼働 {beds:g}床" if beds else None))
    return [_kpi("病床利用率", "直近7日平均", _fmt(util, 1), "%", lead=True,
                 tgt=util_tgt, ok=(_ok(util, tgt_util) if tgt_util is not None else None)),
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
                               profit_breakdown: pd.DataFrame = None,
                               delta_anchor: Optional[dict] = None,
                               overrides: Optional[dict] = None) -> list:
    """診療科版・病棟版それぞれの 1部門=1コンテキスト を返す（PDF描画用）。

    delta_anchor: load_delta_anchor の戻り値（約4週前の量子化状態）。渡すと各ユニットの
    一手に「前回レポートとの比較」事実が加わる。各コンテキストには "_state"（今回の
    状態タグ）が付き、CLI 側が save_facts_snapshot で次回以降のアンカーとして保存する。

    overrides: report_overrides.parse_overrides の戻り値（(axis, unit)→{body,action}）。
    §6-1 人手オーバーライド。move 確定直後に差し替え・src="manual" 刻印。全文差し替えの
    部門は AI 生成をスキップ（再ビルド高速化）。片方だけの差し替えは AI 生成を止めない
    （決定論seedでレビュー時と同じAI文が再現されるため「承認した文＋修正」が成立する）。
    """
    period_start = (base_date - timedelta(days=WEEKS * 7 - 1)).strftime("%Y/%m/%d")
    period_end = base_date.strftime("%Y/%m/%d")
    r7_inp = rolling7_inpatient_avg(adm, base_date)
    r7_nadm = rolling7_new_admission(adm, base_date)
    r7_surg = rolling7_surgery(surg, base_date)
    # ①-3/①-4: ビルド単位の共通事実（全麻トピックのOR稼働・連休フェアネス文脈）
    or_fact = _q_or_load(surg, base_date)
    holiday_fact = _q_holiday_week(adm, base_date)
    anchor_units = (delta_anchor or {}).get("units", {})

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

        # AI一手（病床平準化）は「のびしろのあるユニット」に限定（room>0.5）。
        # トピックが新入院/全麻に決まるユニットは後段で別途AI生成するため、この一括生成では
        # skip して無駄打ちを避ける（病床平準化が結局のトピックに選ばれるユニットのためだけに、
        # ここで先に一括生成する）。
        max_room = max((u.get("room_per_week", 0) or 0 for u in wl["units"]), default=1) or 1
        by_gap = "by_ward" if entity == "ward" else "by_dept"
        tgt_axis_gap = "ward" if entity == "ward" else "dept"
        n_ai = sum(1 for u in wl["units"] if (u.get("room_per_week", 0) or 0) > 0.5)
        # 1-1(f): 週末在院の維持(retention)の同種科内相対位置（診療科軸のみ・P2-bと同じ中立トーン）。
        # LLMプロンプトの事実にのみ使う（fallbackには入れず、文の複雑化を避ける）。
        lev_peers = {}
        if entity == "dept":
            ret_map = {u["name"]: u["retention"] for u in wl["units"]
                       if u.get("retention") is not None}
            for u in wl["units"]:
                tk = "surgical" if u["name"] in SURGERY_EVAL_DEPTS else "internal"
                lev_peers[u["name"]] = _peer_tier(u["name"], ret_map,
                                                  _same_type_names(ret_map, tk))
        # ① 差分ナラティブ: 各ユニットの量子化状態タグと「前回レポート比較」1文を先に確定
        # （narrate_leveling_actions がバッチで先に走るため deltas を前渡しする）。
        unit_meta = {}
        for u in wl["units"]:
            name0 = u["name"]
            code0 = name2code.get(name0, name0)
            dd0 = det.get(name0)
            tk0 = ("ward" if entity == "ward"
                   else "surgical" if name0 in SURGERY_EVAL_DEPTS else "internal")
            na0 = r7_nadm[by_gap].get(code0)
            na_tgt0 = targets.get("new_admission", {}).get(tgt_axis_gap, {}).get(code0)
            sv0 = r7_surg["by_dept"].get(name0, 0) if tk0 == "surgical" else None
            sv_tgt0 = (surg_targets.get(name0)
                       if (tk0 == "surgical" and isinstance(surg_targets, dict)) else None)
            na_level0 = _q_target_gap(na0, na_tgt0)
            sv_level0 = _q_target_gap(sv0, sv_tgt0) if tk0 == "surgical" else None
            tags = {
                "na": _gap_level_tier(na_level0) if na_level0 else None,
                "surg": _gap_level_tier(sv_level0) if sv_level0 else None,
                "ret": _q_ret_level(u.get("retention")),
                "latewk": (_q_latewk_discharge(dd0) or {}).get("level"),
                "wadm": (_q_weekend_adm(dd0) or {}).get("level"),
                "thin": _q_thin_latewk_adm(dd0),
            }
            # 特例ユニット（救急病棟/重症ケア病棟/救急科）は「予定入院・紹介」前提の
            # 差分ナラティブ・平準化バッチ生成をどれも使わない＝anchor を渡さず skip する。
            special0 = _special_narration_kind(entity, code0, name0)
            is_em0 = special0 is not None
            unit_meta[name0] = {
                "tags": tags,
                "anchor": None if is_em0 else anchor_units.get(f"{entity}:{name0}"),
            }
            # per-unit ループ（後段）と同一の入力から topic を前倒し計算し、
            # leveling バッチの生成が捨てられるユニット（救急病棟／topicが
            # admission・surgeryに決まる／room<=0.5）を skip 対象として拾う
            # （u["narrative"] が読まれるのは非救急×topic=leveling×room>0.5 のときだけ）。
            room0 = u.get("room_per_week", 0) or 0
            topic0, _sec0, _sc0 = _select_action_topic(
                tk0, room0, max_room, na0, na_tgt0, sv0, sv_tgt0)
            unit_meta[name0]["skip_leveling_gen"] = (
                is_em0 or topic0 in ("admission", "surgery") or room0 <= 0.5)
        # leveling バッチは leveling トピック整合の差分を渡す（topic が admission/surgery に
        # 決まるユニットは下の skip 対象になり生成自体を行わない＝この差分は使われない。
        # per-unit 側は topic 整合の差分を別途採り直す）。
        lev_deltas = {n: _pick_delta(m["anchor"], m["tags"], topic="leveling")
                      for n, m in unit_meta.items()}
        lev_deltas = {n: d for n, d in lev_deltas.items() if d}

        # §6-1: body/action 両方を手動差し替え済みの部門はAI生成を省く（skip=生成だけ省き
        # 候補選定・max_room は変えない＝他ユニットの決定論を壊さない）。
        # 加えて、生成しても後段で捨てられるだけのユニット（救急病棟／topicが
        # admission・surgeryに確定／room<=0.5）も同じ skip で無駄打ちを避ける。
        full_ov = {u["name"] for u in wl["units"]
                   if is_full_override((overrides or {}).get((entity, u["name"])))}
        waste_skip = {n for n, m in unit_meta.items() if m["skip_leveling_gen"]}
        if with_ai and n_ai:
            narrate_leveling_actions({entity: wl}, {entity: det}, top_n=n_ai, quiet=quiet,
                                     peers=lev_peers, deltas=lev_deltas,
                                     skip=full_ov | waste_skip)

        # P2-b: 同種科内の相対位置(上位/中位/下位)用の達成率マップ（診療科軸のみ・1回）。
        # 新入院＝タイプ別に、全麻＝外科系内で比較する（テーブルと同じ ranking helper を再利用）。
        peer_nadm, peer_surg = {}, {}
        if entity == "dept":
            for r in build_dept_ranking(adm, base_date, targets, "new_admission").to_dict("records"):
                if r.get("目標") and r.get("実績") is not None:
                    peer_nadm[r["診療科"]] = r["実績"] / r["目標"]
            for r in build_surgery_ranking(surg, base_date, surg_targets, period="7").to_dict("records"):
                if r.get("週目標") and r.get("実績") is not None:
                    peer_surg[r["診療科"]] = r["実績"] / r["週目標"]

        # per-unit ループはフルビルドの律速が narrate_*（oMLX呼び出し）にあるため、
        # 「LLM を呼ぶ直前まで」→「LLM 呼び出しだけ並列」→「move 確定〜contexts 組み立て」
        # の3パスに分ける（pandas/SVG構築は並列化の利益が薄いため逐次のまま）。
        #
        # パス1（逐次）: ユニットごとの中間結果と、呼ぶべき narrate_* 呼び出し（あれば）を
        # unit_states / ai_jobs に積む。contexts の並び順は wl["units"] の順のまま保つため、
        # unit_states はその順で積み、パス3もその順で辿る。
        unit_states = []
        ai_jobs = {}   # unit_states のインデックス -> (func, args, kwargs)
        for u in wl["units"]:
            name = u["name"]
            code = name2code.get(name, name)
            dd = det.get(name)
            room = u.get("room_per_week", 0) or 0
            ret = u.get("retention")
            # §6-1 人手オーバーライド: 全文差し替えなら以降のAI生成もスキップ
            ov = (overrides or {}).get((entity, name))
            unit_ai = with_ai and not is_full_override(ov)

            if entity == "ward":
                type_key = "ward"
            elif name in SURGERY_EVAL_DEPTS:
                type_key = "surgical"
            else:
                type_key = "internal"

            # 「この期間の一手」: 外科系は常に全麻(眼科=全手術)を主トピックに固定し、
            # 内科系・病棟は 病床平準化／新入院 のうち目標未達が大きい方を選ぶ
            # （病床管理一辺倒にしない）。
            na_gap = r7_nadm[by_gap].get(code)
            na_tgt_gap = targets.get("new_admission", {}).get(tgt_axis_gap, {}).get(code)
            sv_gap = r7_surg["by_dept"].get(name, 0) if type_key == "surgical" else None
            surg_tgt_gap = (surg_targets.get(name)
                           if (type_key == "surgical" and isinstance(surg_targets, dict)) else None)
            topic, secondary, _scores = _select_action_topic(type_key, room, max_room,
                                                             na_gap, na_tgt_gap, sv_gap, surg_tgt_gap)

            # parts（グラフA-E）はチャート描画だけでなく、新入院(B)/全麻(C)の一手にも使う。
            # narrate_* より先に組み立て、既存の28日MA系列からトレンド（水準×傾向の第2軸）を
            # 取り出す（新規計算を増やさず既存系列を再利用＝チャートの線と一手の説明を一致させる）。
            profit_series = (None if entity == "ward"
                             else _unit_profit_series(profit_monthly, name, base_date,
                                                      estimators, adm, surg))
            parts = _build_parts(adm, surg, base_date, entity, name, code, dd,
                                 r7_inp, r7_nadm, r7_surg, targets, surg_targets, profit_series)

            def _trend_of(part_key):
                p = parts.get(part_key)
                cur = [v for v in ((p.get("_data") or {}).get("cur") or []) if v is not None] if p else []
                return _ma_window_trend(cur, prior_end=28, pt=5) if cur else "—"

            na_trend = _trend_of("B")
            surg_trend = _trend_of("C") if type_key == "surgical" else None

            # Tier2-1: 前年同期比較（B/Cチャートの前年線を再利用。NO_PREVYEAR_WARDS や
            # 前年データ不足は _q_yoy が None を返し事実に載らない）
            def _yoy_of(part_key):
                p = parts.get(part_key)
                d = (p.get("_data") or {}) if p else {}
                return _q_yoy(d.get("cur"), d.get("prev"))

            na_yoy = _yoy_of("B")
            surg_yoy = _yoy_of("C") if type_key == "surgical" else None
            # 水準×傾向の確定文（fallback・副トピック併記の両方で使う）
            na_state = _q_target_gap_trend(na_gap, na_tgt_gap, na_trend)
            surg_state = (_q_target_gap_trend(sv_gap, surg_tgt_gap, surg_trend)
                          if type_key == "surgical" else None)
            # P2-b: 同種科内の相対位置（診療科軸のみ）
            na_peer = (_peer_tier(name, peer_nadm, _same_type_names(peer_nadm, type_key))
                       if entity == "dept" else None)
            surg_peer = (_peer_tier(name, peer_surg, list(peer_surg.keys()))
                         if type_key == "surgical" else None)

            # ① 差分ナラティブ: 選定トピックに次元が一致する差分を優先して1文に確定
            # （leveling は上のバッチと同じ選び方に一致・admission/surgery は topic整合を採り直す）。
            special = _special_narration_kind(entity, code, name)
            is_emergency = special is not None   # 特例(救急病棟/重症ケア病棟/救急科)の総称
            d_txt = (None if is_emergency
                     else _pick_delta(unit_meta[name]["anchor"], unit_meta[name]["tags"],
                                      topic=topic))

            # 特例ユニットは「予定入院」「地域医療連携」という業務前提が成り立たないため、
            # トピック(leveling/admission)は共通ロジックで選びつつ、文言だけ種別ごとの専用
            # プロンプト（narrate_emergency_* / narrate_critical_care_* / narrate_er_*）に差し替える。
            # ここでは呼び出す narrate_* と引数だけを確定し、実際の呼び出しはパス2で並列に行う。
            _SPECIAL_CALLS = {
                "emergency":     (narrate_emergency_admission_action, narrate_emergency_leveling_action),
                "critical_care": (narrate_critical_care_admission_action, narrate_critical_care_leveling_action),
                "er_dept":       (narrate_er_admission_action, narrate_er_leveling_action),
            }
            call = None
            if special:
                adm_fn, lev_fn = _SPECIAL_CALLS[special]
                if topic == "admission":
                    call = (adm_fn, (name, na_gap, na_tgt_gap), {"trend": na_trend, "quiet": quiet})
                else:
                    call = (lev_fn, (name, ret, u.get("room_delta_4w")), {"quiet": quiet})
            elif topic == "admission":
                mix = _q_planned_mix(adm, base_date, name) if entity == "dept" else None
                call = (narrate_admission_action,
                        (name, entity, na_gap, na_tgt_gap),
                        {"trend": na_trend, "peer": na_peer, "yoy": na_yoy, "delta": d_txt,
                         "mix": mix, "holiday": holiday_fact, "quiet": quiet})
            elif topic == "surgery":
                call = (narrate_surgery_action,
                        (name, sv_gap, surg_tgt_gap),
                        {"trend": surg_trend, "peer": surg_peer, "yoy": surg_yoy, "delta": d_txt,
                         "or_load": or_fact, "holiday": holiday_fact, "quiet": quiet})

            idx = len(unit_states)
            if unit_ai and call is not None:
                ai_jobs[idx] = call
            unit_states.append({
                "u": u, "name": name, "code": code, "dd": dd, "room": room, "ret": ret,
                "ov": ov, "unit_ai": unit_ai, "type_key": type_key,
                "na_gap": na_gap, "na_tgt_gap": na_tgt_gap, "sv_gap": sv_gap,
                "surg_tgt_gap": surg_tgt_gap, "topic": topic, "secondary": secondary,
                "parts": parts, "profit_series": profit_series,
                "na_trend": na_trend, "surg_trend": surg_trend,
                "na_yoy": na_yoy, "surg_yoy": surg_yoy, "na_state": na_state,
                "surg_state": surg_state, "na_peer": na_peer, "surg_peer": surg_peer,
                "is_emergency": is_emergency, "special": special, "d_txt": d_txt,
            })

        # パス2（並列）: パス1で記録した narrate_* 呼び出しを NARRATE_WORKERS 並列で実行する。
        # 1ユニットの失敗（例外）が全ビルドを落とさないよう try/except で包み、失敗時は
        # None（＝呼び出し側のパス3が定型文フォールバックへ無害縮退）にする。
        ai_results = {}
        if ai_jobs:
            with concurrent.futures.ThreadPoolExecutor(max_workers=NARRATE_WORKERS) as ex:
                future_to_idx = {ex.submit(func, *args, **kwargs): idx
                                 for idx, (func, args, kwargs) in ai_jobs.items()}
                for fut in future_to_idx:
                    idx = future_to_idx[fut]
                    try:
                        ai_results[idx] = fut.result()
                    except Exception as e:
                        logger.warning(
                            f"一手生成失敗 ({entity}:{unit_states[idx]['name']}): {e}")
                        ai_results[idx] = None

        # パス3（逐次）: パス1の中間結果とパス2の生成結果から move を確定し、以降は
        # 現行と同じ処理（差分ナラティブ追記〜contexts.append）を同じ順序で行う。
        for idx, st in enumerate(unit_states):
            u, name, code, dd = st["u"], st["name"], st["code"], st["dd"]
            room, ret, ov, unit_ai = st["room"], st["ret"], st["ov"], st["unit_ai"]
            type_key = st["type_key"]
            na_gap, na_tgt_gap = st["na_gap"], st["na_tgt_gap"]
            sv_gap, surg_tgt_gap = st["sv_gap"], st["surg_tgt_gap"]
            topic, secondary = st["topic"], st["secondary"]
            parts, profit_series = st["parts"], st["profit_series"]
            na_trend, surg_trend = st["na_trend"], st["surg_trend"]
            na_yoy, surg_yoy = st["na_yoy"], st["surg_yoy"]
            na_state, surg_state = st["na_state"], st["surg_state"]
            na_peer, surg_peer = st["na_peer"], st["surg_peer"]
            is_emergency, special, d_txt = st["is_emergency"], st["special"], st["d_txt"]

            # unit_ai and narrate_xxx(...) と等価（unit_ai=False は call を記録していないので
            # ai_results に無く、そのケースは ai_out=False として下の `or fallback` に落ちる）。
            ai_out = ai_results.get(idx) if unit_ai else False

            if special:
                # oMLX 未起動/棄却時の定型文は救急病棟用を全特例で共用する（いずれも
                # 「予定入院・紹介」を含まない受け入れ体制ベースの安全な文言。稀な縮退経路）。
                if topic == "admission":
                    move = ai_out or _fallback_move_emergency_admission(na_state)
                else:
                    move = ai_out or _fallback_move_emergency_leveling(u)
            elif topic == "admission":
                # 一般病棟（特例でない病棟）は紹介・地域医療連携を業務として持たないため、
                # oMLX未起動/棄却時の定型文も専用版（_fallback_move_ward_admission）を使う。
                if entity == "ward":
                    move = ai_out or _fallback_move_ward_admission(na_state)
                else:
                    move = ai_out or _fallback_move_admission(na_state, peer=na_peer)
            elif topic == "surgery":
                move = ai_out or _fallback_move_surgery(surg_state, peer=surg_peer,
                                                        label=surgery_metric_label(name))
            else:
                move = (_fallback_move(u, dd, entity) if room <= 0.5
                        else (u.get("narrative") or _fallback_move(u, dd, entity)))

            # ① 差分ナラティブ: AI経路はプロンプトで織り込み済み。定型文経路は決定論で1文追記。
            if d_txt and move.get("src") != "ai" and move.get("body"):
                move = {**move, "body": move["body"].rstrip() + " " + d_txt + "。"}

            # 計測用メタ（テンプレは参照しない）: topic=選定トピック、src=ai(採択)/tpl(定型文)。
            # scripts/report_comment_diversity.py が fallback 率・重複率を axis×topic で集計する。
            move = {**move, "topic": (f"{special}-" if special else "") + topic,
                    "src": move.get("src", "tpl"), "delta": d_txt}

            # P3: 未達が複数ある科は、主トピックの一手に加えて副トピックを本文へ軽く併記
            # （actionは主トピックに集中）。救命救急系は語彙が異なるため対象外。
            if secondary and not is_emergency and move.get("body"):
                also = _secondary_clause(secondary, na_state, surg_state)
                if also:
                    move = {**move, "body": move["body"].rstrip() + " " + also}

            # §6-1: 人手オーバーライドを move 確定直後の1箇所で適用（src="manual" 刻印・
            # 数値行はこの後に付くデータ由来行なので影響しない）。差し替えはログに残す。
            if ov:
                move = apply_override(move, ov)
                if not quiet:
                    print(f"  ✏️ [手動] {entity}:{name} の一手を差し替え "
                          f"({'+'.join(move['ov_fields'])})")

            # 外科系は「一手」に全麻ハイライト1行を常設（週末ならし本文＋全麻の数値）
            if type_key == "surgical":
                c_part = parts.get("C")
                sline = _surg_highlight(r7_surg["by_dept"].get(name, 0),
                                        surg_targets.get(name) if isinstance(surg_targets, dict) else None,
                                        c_part.get("_data") if c_part else None,
                                        dept=name)
                if sline:
                    move = {**move, "surg_line": sline}
            # 病棟は「一手」に病床利用率ハイライト1行を常設
            elif type_key == "ward":
                a_part = parts.get("A")
                w_beds = targets.get("inpatient", {}).get("ward_beds", {}).get(code)
                w_inp_tgt = targets.get("inpatient", {}).get("ward", {}).get(code)
                w_util = _rate(r7_inp["by_ward"].get(code), w_beds)
                w_tgt_util = round(w_inp_tgt / w_beds * 100, 1) if (w_inp_tgt and w_beds) else None
                uline = _util_highlight(w_util, w_tgt_util, w_beds,
                                        a_part.get("_data") if a_part else None)
                if uline:
                    move = {**move, "util_line": uline}
            # 内科系は「一手」に新入院ハイライト1行を常設（Tier2-2・外科/病棟と対称）
            else:
                b_part = parts.get("B")
                nline = _nadm_highlight(na_gap, na_tgt_gap,
                                        b_part.get("_data") if b_part else None)
                if nline:
                    move = {**move, "nadm_line": nline}

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
                "_state": unit_meta[name]["tags"],   # 差分ナラティブ用（CLIがスナップショット保存）
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


# ════════════════════════════════════════════════════════════
# 病院全体サマリ（1ページ目）── 内科系/外科系の構成を1枚に集約
#   KPI4枚（在院・新入院・全麻・粗利）＋ A在院 B新入院 C全麻 D粗利 E曜日 の5チャート。
#   「この期間の一手」は載せない（move=None → テンプレ側で非表示）。
# ════════════════════════════════════════════════════════════
def _hospital_profit_series(profit_monthly, base_date, profit_projection=None) -> Optional[dict]:
    """病院全体の月次粗利（百万円）・直近12か月＋前年同期。目標=月次目標の合計。

    profit_projection（profit_estimate.compute_calibrated_profit_projection の戻り値。
    ダッシュボード/PLレポートと同一の hybrid+recency補正 pipeline）があれば、確報の
    末尾に **当月見込みスロット** を1つ足す（proj=百万円。実線は確報で止め、点線で見込みへ）。
    """
    if profit_monthly is None or len(profit_monthly) == 0:
        return None
    by_month = profit_monthly.groupby("月")
    gp = by_month["粗利"].sum()
    gt = by_month["月次目標"].sum(min_count=1)
    gsum = by_month["外来粗利"].sum(min_count=1) if "外来粗利" in profit_monthly.columns else None
    nsum = by_month["入院粗利"].sum(min_count=1) if "入院粗利" in profit_monthly.columns else None
    gmap = dict(gsum) if gsum is not None else {}
    nmap = dict(nsum) if nsum is not None else {}
    base_m = base_date.to_period("M").to_timestamp()
    months = [m for m in gp.index if m <= base_m]
    if not months:
        return None
    months = sorted(months)[-WEEKS:]
    cur = [round(gp[m] / 1000, 1) for m in months]

    prev_adjusted = False

    def _prev_at(m):
        nonlocal prev_adjusted
        pm = m - pd.DateOffset(years=1)
        if pm not in gp.index:
            return None
        if _prev_needs_revision_adjust(m):
            adj = _revision_adjusted_prev(gmap, nmap, pm)
            if adj is not None:
                prev_adjusted = True
                return adj
        return round(gp[pm] / 1000, 1)

    prev = [_prev_at(m) for m in months]
    dates = [m.strftime("%-m月") for m in months]
    last = months[-1]
    tgt = gt.get(last)
    ref = round(tgt / 1000, 1) if pd.notna(tgt) else 0
    rate = round(gp[last] / tgt * 100, 1) if (pd.notna(tgt) and tgt) else None

    proj = proj_month = None
    if profit_projection and profit_projection.get("hospital_million") is not None:
        pm = profit_projection["month"]
        if pm not in months:
            dates.append(f"{pm.strftime('%-m月')}(見込)")
            cur.append(None)
            prev.append(_prev_at(pm))
            proj = profit_projection["hospital_million"]
            proj_month = pm

    return {"dates": dates, "cur": cur, "prev": prev, "ref": ref, "rate": rate,
            "latest": last.strftime("%Y年%-m月"), "proj": proj, "proj_month": proj_month,
            "prev_adjusted": prev_adjusted}


def _hospital_dow(adm, base_date) -> dict:
    """病院全体（診療科ビュー）の曜日別 日平均（退院・新入院・在院）＝直近8週。"""
    out = {}
    for met, col in (("discharge", "退院患者数"), ("admission", "新入院患者数"),
                     ("census", "在院患者数")):
        p = dow_event_profile(adm, base_date, col, group_col="診療科名",
                              group_val=None, weeks=8)
        w = p["weeks"] or 1
        out[met] = [round(c / w, 1) for c in p["counts"]]
    return out


# 病院全体サマリの「この期間の一手」レバー（打ち手の方向性）。dept_report の単一ユニット
# 向け一手と同じ語彙に統一（病院全体でも打ち手は現場の一手の延長として理解できるように）。
_HOSPITAL_LEVERS = {
    "leveling": "金曜に集中しがちな退院を平日へ分散し、週末の入院受け入れで空床を補充する（在院日数の延長はしない）。",
    "admission": "地域医療連携（紹介元）への働きかけ強化や、予定入院枠の週後半への調整などで、新入院の患者数増に取り組む。",
    "surgery": "手術枠の稼働状況の確認や、執刀医との症例調整などで、全身麻酔手術の件数増に専念する。",
}
_HOSPITAL_TOPIC_LABEL = {"leveling": "週末在院の維持率", "admission": "新入院", "surgery": "全身麻酔手術"}


def _fallback_move_hospital(topic: str, state: Optional[str], ret: Optional[float],
                            leader: Optional[str] = None,
                            leader_label: Optional[str] = None) -> dict:
    """病院全体サマリの「この期間の一手」定型文（oMLX未起動/ハルシネーション棄却時）。

    admission/surgery は単一ユニット向けと同じ定型文を再利用する（ページ見出しに
    「病院全体」と明記済みのため、本文側で主語を繰り返さない）。
    leveling（週末在院）は KPIバッジと同じ目標基準（TARGET_WEEKEND_RETENTION）で達成/未達を
    判定する。ハードコードのしきい値でバッジ「未達」と本文「保てています」が食い違うのを防ぐ。
    leader: 牽引部門（2-3・あれば褒める1文を決定論で追記）。
    """
    if topic == "admission":
        move = _fallback_move_admission(state)
    elif topic == "surgery":
        move = _fallback_move_surgery(state)
    elif ret is not None and round(ret * 100, 1) >= TARGET_WEEKEND_RETENTION:
        move = {"body": "病院全体として週末も平日と同水準の在院を保てており、目標を確保できています。",
                # 添削由来（P2）: 「現状維持。」で完結させない
                "action": "週末の入退院リズムはこのまま継続しつつ、在院水準のさらなる底上げを図りましょう。"}
    else:
        move = {"body": f"病院全体の週末在院の維持率は{state or '目標を下回っている'}状況です。",
                "action": "金曜に集中しがちな退院を平日へ分散し、週末の入院受け入れで空床を補充しましょう。"}
    if leader and leader_label:
        move = {**move, "body": move["body"].rstrip()
                + f" こうした中、{leader}は{leader_label}状況で、手本になっています。"}
    return move


def build_hospital_overview_context(adm, surg, targets, surg_targets, profit_monthly,
                                    base_date, generated_at, *, hospital_name: str = "",
                                    profit_breakdown=None, profit_projection=None,
                                    with_ai: bool = True, quiet: bool = False,
                                    delta_anchor: Optional[dict] = None,
                                    overrides: Optional[dict] = None) -> dict:
    """病院全体サマリ（dept_report.html 1シート）のコンテキスト。

    profit_projection: profit_estimate.compute_calibrated_profit_projection の戻り値
    （病院全体・診療科別の当月見込み粗利＝ダッシュボードと同一 pipeline）。渡すと粗利KPI/
    チャートが「確報の最新月」でなく「当月見込み」で達成率を表示する。

    「この期間の一手」は 週末在院／新入院／全麻 のうち最も目標未達が大きいトピックを選ぶ
    （単一ユニット向け _select_action_topic と同じ設計）。在院水準・粗利は本文の文脈
    （supporting facts）として使うが、打ち手の起点にはしない（対応する運用レバーが無いため）。
    """
    kpi = build_kpi_summary(adm, surg, base_date, targets, surg_targets)
    charts: list = []

    # §6-1 人手オーバーライド（軸ラベル [病院全体:◯◯]・unit＝表示名と一致）。
    # 全文差し替えなら以降のAI生成（narrate_hospital_summary）はスキップ。
    unit_name = hospital_name or "病院全体"
    ov = (overrides or {}).get(("hospital", unit_name))
    with_ai = with_ai and not is_full_override(ov)

    def add(kind, name, series, ref, ref_label, unit, win, badge, note=""):
        if not series or not series.get("cur") or all(v is None for v in series["cur"]):
            return
        charts.append({"kind": kind, "name": name, "badge": badge,
                       "note": note, "is_dow": False, "_data": series, "_ref": ref,
                       "_ref_label": ref_label, "_unit": unit, "_win": win, "_color": PART_LINE})

    # A 在院（28日移動平均・全日目標）
    add("A", "在院患者数", _ma_series(adm, "在院患者数", base_date, 28, "mean"),
        TARGET_INPATIENT_ALLDAY, f"目標{TARGET_INPATIENT_ALLDAY:g}", "人",
        "12週・28日移動平均", _ach_badge(kpi["inpatient_avg_7d"], TARGET_INPATIENT_ALLDAY))

    # B 新入院（28日移動平均=件/日・目標=週次÷7）
    daily_na_tgt = round(TARGET_ADMISSION_WEEKLY / 7, 1)
    add("B", "新入院患者数", _ma_series(adm, "新入院患者数", base_date, 28, "mean"),
        daily_na_tgt, f"目標{daily_na_tgt:g}", "件/日", "12週・28日移動平均（件/日）",
        _ach_badge(kpi["admission_actual_7d"], TARGET_ADMISSION_WEEKLY))

    # C 全麻（病院全体KPIと統一＝30営業平日移動平均・件/日）
    add("C", "全身麻酔手術", _surg_series(surg, base_date),
        TARGET_GA_DAILY, f"目標{TARGET_GA_DAILY:g}", "件/日", "12週・30営業平日移動平均（件/日）",
        _ach_badge(kpi["operation_daily_avg"], TARGET_GA_DAILY))

    # D 粗利（病院全体・確報＋当月見込み）
    ps = _hospital_profit_series(profit_monthly, base_date, profit_projection=profit_projection)
    prof_latest = prof_disp = prof_rate = None
    prof_sub = "確報・最新月"
    if ps:
        prof_latest = next((v for v in reversed(ps["cur"]) if v is not None), None)
        if ps.get("proj") is not None and ps["ref"]:
            prof_disp = ps["proj"]
            prof_rate = round(ps["proj"] / ps["ref"] * 100, 1)
            prof_sub = "当月見込み"
            badge = (f"見込み達成率 {prof_rate:g}%", "ok" if prof_rate >= 100 else "wr")
            note = (f"実線=確報(最新{ps['latest']})／"
                    f"点線={ps['proj_month'].strftime('%-m月')}は診療実績ベースの見込（暫定）")
        else:
            prof_disp = prof_latest
            prof_rate = ps["rate"]
            badge = ((f"達成率 {prof_rate:g}%", "ok" if prof_rate >= 100 else "wr")
                     if prof_rate is not None else None)
            note = f"確報ベース・最新 {ps['latest']}"
        if ps.get("prev_adjusted"):
            note += _REV_NOTE
        add("D", "粗利", ps, ps["ref"], f"目標{ps['ref']:g}" if ps["ref"] else "",
            "百万円", "12か月・月次（確報＋当月見込み）", badge, note=note)

    # E 曜日プロファイル（病院全体）
    dd = _hospital_dow(adm, base_date)
    if any(dd["discharge"]) or any(dd["admission"]):
        charts.append({"kind": "E", "name": "曜日プロファイル",
                       "badge": None, "note": "", "is_dow": True,
                       "svg": _render_dow_svg(dd["discharge"], dd["admission"], dd["census"])})

    # SVG 描画（ヒーロー=256・以降=232）
    for i, p in enumerate(charts):
        if not p["is_dow"]:
            p["svg"] = render_trend_svg(p["_data"], p["_ref"], p["_ref_label"],
                                        p["_unit"], p["_win"], color=p["_color"],
                                        height=256 if i == 0 else 232,
                                        proj=p["_data"].get("proj"))
        p["priority"] = i + 1

    # 週末在院維持率（病院全体・病棟ベース＝ hospital_summary.build_hero_text と同一定義）
    wr = weekend_census_retention(adm, base_date, entity="ward")
    ret = wr.get("total", {}).get("retention")
    ret_pct = round(ret * 100, 1) if ret is not None else None

    # KPI 5枚
    inp_v = kpi["inpatient_avg_7d"]
    kpis = [
        _kpi("在院患者数", "直近7日平均", _fmt(inp_v, 1), "人", lead=True,
             tgt=f"目標 {TARGET_INPATIENT_ALLDAY:g}", ok=_ok(inp_v, TARGET_INPATIENT_ALLDAY)),
        _kpi("新入院", "直近7日累計", _fmt(kpi["admission_actual_7d"]), "件",
             tgt=f"目標 {TARGET_ADMISSION_WEEKLY:g}/週",
             ok=_ok(kpi["admission_actual_7d"], TARGET_ADMISSION_WEEKLY)),
        _kpi("全身麻酔手術", "直近7平日平均", _fmt(kpi["operation_daily_avg"], 1), "件/日",
             tgt=f"目標 {TARGET_GA_DAILY:g}", ok=_ok(kpi["operation_daily_avg"], TARGET_GA_DAILY)),
        _kpi("粗利", prof_sub, _fmt(prof_disp, 1), "百万円",
             tgt=(f"目標 {ps['ref']:g}" if (ps and ps["ref"]) else None),
             ok=(prof_rate >= 100) if prof_rate is not None else None),
        _kpi("週末在院維持率", "直近8週・土日/平日", _fmt(ret_pct, 1), "%",
             tgt=f"目標 {TARGET_WEEKEND_RETENTION:g}", ok=_ok(ret_pct, TARGET_WEEKEND_RETENTION)),
    ]

    # ── この期間の一手（病院全体） ──────────────────────────────
    # A/B/C チャートの28日MA系列から、単一ユニット向けと同じ窓・閾値でトレンドを取り出す
    # （新規計算を増やさず既存系列を再利用＝チャートの線と一手の説明を一致させる）。
    by_kind = {c["kind"]: c for c in charts}

    def _trend_of_kind(kind, prior_end, pt):
        c = by_kind.get(kind)
        cur = [v for v in ((c or {}).get("_data") or {}).get("cur", []) if v is not None]
        return _ma_window_trend(cur, prior_end=prior_end, pt=pt) if cur else "—"

    # 週末在院維持率は病院サマリでは目標(TARGET_WEEKEND_RETENTION)付きKPIとしてバッジ表示される。
    # 一手の事実・定型文も同じ目標基準で語り、バッジ「未達」と本文「保てています」が食い違わない
    # ようにする（room基準の _q_state_trend は per-unit 平準化の“絶対水準”用＝目標非依存なので、
    # ここでは他KPIと同じ目標比の _q_target_gap_trend を使う）。傾向は のびしろΔ から向きを取る
    # （のびしろ拡大 room_delta>0 ＝ 維持率は低下方向）。
    rd_total = wr.get("total", {}).get("room_delta_4w")
    ret_trend = ("上昇" if (rd_total is not None and rd_total < -0.5)
                 else "低下" if (rd_total is not None and rd_total > 0.5) else "横ばい")
    leveling_state = _q_target_gap_trend(ret_pct, TARGET_WEEKEND_RETENTION, ret_trend)
    admission_state = _q_target_gap_trend(kpi["admission_actual_7d"], TARGET_ADMISSION_WEEKLY,
                                          _trend_of_kind("B", 28, 5))
    surgery_state = _q_target_gap_trend(kpi["operation_daily_avg"], TARGET_GA_DAILY,
                                        _trend_of_kind("C", 28, 5))
    census_state = _q_target_gap_trend(inp_v, TARGET_INPATIENT_ALLDAY, _trend_of_kind("A", 35, 2))
    profit_state = _q_target_gap(prof_disp, ps["ref"]) if (ps and ps.get("ref")) else None

    topic_states = {"leveling": leveling_state, "admission": admission_state, "surgery": surgery_state}
    topic_scores = {
        "leveling": max(0.0, 1 - ret) if ret is not None else 0.0,
        "admission": _admission_gap_score(kpi["admission_actual_7d"], TARGET_ADMISSION_WEEKLY),
        "surgery": _surgery_gap_score(kpi["operation_daily_avg"], TARGET_GA_DAILY),
    }
    h_topic = _select_hospital_topic(topic_scores)
    h_primary_state = topic_states.get(h_topic) or leveling_state or "目標を下回っている"

    facts = [f"{_HOSPITAL_TOPIC_LABEL[h_topic]}: {h_primary_state}"]
    for k in ("leveling", "admission", "surgery"):
        if k != h_topic and topic_states.get(k):
            facts.append(f"{_HOSPITAL_TOPIC_LABEL[k]}: {topic_states[k]}")
    if census_state:
        facts.append(f"在院患者数: {census_state}")
    if profit_state:
        facts.append(f"粗利: {profit_state}")
    facts = facts[:4]

    # 2-3: 主トピックの牽引役（目標を達成している最上位のみ・褒める方向だけ名指し）。
    # 達成部門がいなければ名指しなし。下押し側は職員発信トーンのため名指ししない（§5）。
    # 捏造ガード＝leader以外の全部門名を禁止語で渡す（渡していない名前を書いたら棄却）。
    dept_ratio, surg_ratio = {}, {}
    for r in build_dept_ranking(adm, base_date, targets, "new_admission").to_dict("records"):
        if r.get("目標") and r.get("実績") is not None:
            dept_ratio[r["診療科"]] = r["実績"] / r["目標"]
    for r in build_surgery_ranking(surg, base_date, surg_targets, period="7").to_dict("records"):
        if r.get("週目標") and r.get("実績") is not None:
            surg_ratio[r["診療科"]] = r["実績"] / r["週目標"]
    if h_topic == "admission":
        cands = {n: v for n, v in dept_ratio.items() if v >= 1.0}
        leader_label = "新入院の目標を上回っている"
    elif h_topic == "surgery":
        cands = {n: v for n, v in surg_ratio.items() if v >= 1.0}
        leader_label = "全身麻酔手術の目標を上回っている"
    else:
        cands = {u["name"]: u["retention"] for u in wr.get("units", [])
                 if u.get("retention") is not None
                 and u["retention"] * 100 >= TARGET_WEEKEND_RETENTION}
        leader_label = "週末も在院を維持できている"
    leader = max(cands, key=cands.get) if cands else None
    if leader:
        facts.append(f"牽引役: {leader}が{leader_label}")
    all_names = set(dept_ratio) | set(surg_ratio) | {u["name"] for u in wr.get("units", [])}
    other_names = tuple(sorted(all_names - {leader})) if leader else tuple(sorted(all_names))

    # ① 差分ナラティブ（病院全体）: 3トピックの達成度バケットを状態として保存し、
    # アンカー（約4週前）との遷移を主トピックについてのみ言及（単一ユニットと同じ保守則）。
    def _h_tier(v, t):
        level = _q_target_gap(v, t)
        return _gap_level_tier(level) if level else None

    h_tags = {"leveling": _h_tier(ret_pct, TARGET_WEEKEND_RETENTION),
              "admission": _h_tier(kpi["admission_actual_7d"], TARGET_ADMISSION_WEEKLY),
              "surgery": _h_tier(kpi["operation_daily_avg"], TARGET_GA_DAILY)}
    prev_h = (delta_anchor or {}).get("hospital") or {}
    g = _gap_delta_fact(_HOSPITAL_TOPIC_LABEL[h_topic], prev_h.get(h_topic), h_tags.get(h_topic))
    h_delta = g[1] if g else None
    holiday_fact = _q_holiday_week(adm, base_date)
    if h_delta:
        facts.append(f"前回レポートとの比較: {h_delta}")
    if holiday_fact:
        facts.append(f"補足: {holiday_fact}")

    move = ((with_ai and narrate_hospital_summary(facts, _HOSPITAL_LEVERS[h_topic],
                                                  leader=leader, extra_banned=other_names,
                                                  has_delta=bool(h_delta),
                                                  has_holiday=bool(holiday_fact),
                                                  quiet=quiet))
           or _fallback_move_hospital(h_topic, h_primary_state, ret,
                                      leader=leader, leader_label=leader_label if leader else None))
    if h_delta and move.get("src") != "ai" and move.get("body"):
        move = {**move, "body": move["body"].rstrip() + " " + h_delta + "。"}
    move = {**move, "topic": h_topic, "src": move.get("src", "tpl"), "leader": leader,
            "delta": h_delta}

    # §6-1: 人手オーバーライドを move 確定直後の1箇所で適用（src="manual" 刻印）。
    if ov:
        move = apply_override(move, ov)
        if not quiet:
            print(f"  ✏️ [手動] hospital:{unit_name} の一手を差し替え "
                  f"({'+'.join(move['ov_fields'])})")

    return {
        "_state": h_tags,   # 差分ナラティブ用（CLIがスナップショット保存）
        "axis": "hospital", "type_key": "hospital",
        "type_label": "全体サマリ", "subtitle": "病院全体パフォーマンスサマリ",
        "prio_text": "A 在院 → B 新入院 → C 全麻 → D 粗利 → E 曜日",
        "order": 0, "unit": unit_name,
        "hospital_name": hospital_name,
        "base_date": base_date.strftime("%Y/%m/%d"),
        "generated_at": generated_at.strftime("%Y/%m/%d"),
        "kpis": kpis, "charts": charts, "move": move,
    }


def render_summary_table_pages(adm, surg, targets, surg_targets, base_date, *,
                               hospital_name: str = "", profit_monthly=None,
                               profit_breakdown=None, profit_projection=None) -> list:
    """病院全体サマリの 2・3 ページ目（病棟別／診療科別テーブル）の HTML 断片。

    実績まとめPDF（build_hospital_report）の P3/P4 と同一の共通部品を使う。
    返値は extra_pages（dept_report.html）にそのまま渡せる HTML 文字列のリスト。
    profit_projection: profit_estimate.compute_calibrated_profit_projection の戻り値。
    渡すと診療科テーブルの「粗利予測達成率」がダッシュボードと同一の hybrid+recency補正
    値になる（未指定時は従来のOLS単独推計＝project_dept_monthend にフォールバック）。
    """
    from . import hospital_summary as hs
    ctx = hs.build_summary_context(adm, surg, targets, surg_targets, base_date,
                                   profit_monthly=profit_monthly,
                                   profit_breakdown=profit_breakdown,
                                   profit_projection=profit_projection)
    legend = hs.render_legend()
    title = hospital_name or "全病院"
    bd = base_date

    def head(sub):
        return (f'<div style="border-bottom:2px solid #2b5797;padding-bottom:8px;margin-bottom:8px">'
                f'<div style="font-size:12px;color:{hs.SUB};letter-spacing:1px">{title}　全病院 実績まとめ</div>'
                f'<div style="font-size:18px;font-weight:700">{sub}'
                f'<span style="font-size:12px;color:{hs.SUB};font-weight:600">　基準日 {bd:%Y-%m-%d}</span></div></div>')

    ward = (head("病棟別 実績")
            + '<div class="sec">病棟別 実績（在院・病床利用率・入退院フロー・週末在院維持率）</div>'
            + hs.render_ward_table(ctx["ward_rows"]) + legend)
    dept = (head("診療科別 実績")
            + '<div class="sec">診療科別 実績（在院・新入院・入退院フロー・全麻・粗利予測達成率）</div>'
            + hs.render_dept_table(ctx["dept_rows"]) + legend)
    return [ward, dept]
