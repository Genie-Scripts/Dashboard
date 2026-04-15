"""
html_builder.py — テンプレートコンテキスト生成（v2.1）

v2.1 変更点:
  - build_portal_context() 新設 → portal.html 用
  - build_detail_json()    新設 → detail.html 用（全データJSON一括）
  - 旧 build_doctor_context / build_nurse_context は廃止
  - ステータス判定を config.status_display() に委譲
"""

import json
import pandas as pd
import numpy as np
from datetime import datetime
from typing import Optional

from .config import (
    TARGET_INPATIENT_WEEKDAY, TARGET_INPATIENT_HOLIDAY,
    TARGET_INPATIENT_ALLDAY, TARGET_ADMISSION_WEEKLY, TARGET_GA_DAILY,
    KPI_ICONS, AXIS_ICONS, status_display, status_label,
    SURGERY_DISPLAY_DEPTS, NADM_DISPLAY_DEPTS,
)
from .metrics import (
    build_kpi_summary, build_dept_ranking, build_ward_ranking,
    build_surgery_ranking, build_doctor_watch_ranking,
    build_nurse_watch_ranking, build_nurse_load_ranking,
    rolling7_new_admission, rolling7_surgery,
    build_daily_series, build_surgery_daily_series, add_moving_average,
    build_biz_ma30_series,
    week_over_week, achievement_rate,
)
from .charts import (
    build_inpatient_chart, build_new_admission_chart,
    build_surgery_chart_hospital, build_surgery_chart_dept,
    build_surgery_year_compare_chart, build_ward_utilization_heatmap,
)
from .profit import build_profit_kpi, build_profit_chart_data


def _json_safe(obj):
    """JSON シリアライズ用のデフォルト変換"""
    if isinstance(obj, (pd.Timestamp, datetime)):
        return obj.isoformat()
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if pd.isna(obj):
        return None
    return str(obj)


def _add_adm_breakdown(td: dict, planned_s: pd.DataFrame, emg_s: pd.DataFrame) -> dict:
    """trend dict に予定/緊急入院の内訳配列を追加（日付を key にして安全にアライン）"""
    p_map = ({d.strftime("%Y-%m-%d"): int(v) for d, v in zip(planned_s["日付"], planned_s["値"]) if pd.notna(v)}
             if len(planned_s) > 0 else {})
    e_map = ({d.strftime("%Y-%m-%d"): int(v) for d, v in zip(emg_s["日付"], emg_s["値"]) if pd.notna(v)}
             if len(emg_s) > 0 else {})
    td["planned"]  = [p_map.get(d, 0) for d in td["dates"]]
    td["emergency"] = [e_map.get(d, 0) for d in td["dates"]]
    return td


def _ranking_to_list(df: pd.DataFrame, name_col: str = "診療科",
                     actual_col: str = "実績", target_col: str = "目標") -> list:
    """ランキングDataFrameをJSON用リストに変換"""
    rows = []
    for _, r in df.iterrows():
        rate = r.get("達成率")
        st = status_display(rate)
        rows.append({
            "rank": int(r.get("順位", 0)),
            "name": r[name_col],
            "actual": float(r[actual_col]) if pd.notna(r[actual_col]) else 0,
            "target": float(r[target_col]) if pd.notna(r[target_col]) else None,
            "rate": float(rate) if pd.notna(rate) else None,
            "status": st["css"],
            "shape": st["shape"],
            "text": st["text"],
        })
    return rows


# ═══════════════════════════════════════
# 要注視カード選出（絶対差ベース）
# ═══════════════════════════════════════

def _build_attention_cards(adm, surg, base_date, targets, surg_targets):
    """
    要注視カードを「目標との絶対差」が大きい順に選出。
    目標以上（gap >= 0）は除外。最大5件（診療科+病棟を合算してソート）。
    """
    from .config import WARD_NAMES, WARD_HIDDEN

    candidates = []

    # ── 診療科: 新入院直近7日の絶対差が大きい順 ──
    r7 = rolling7_new_admission(adm, base_date)
    nadm_tgt = targets.get("new_admission", {}).get("dept", {})
    for dept, actual in r7["by_dept"].items():
        tgt = nadm_tgt.get(dept)
        if tgt is None or tgt == 0:
            continue
        gap = actual - tgt
        if gap >= 0:
            continue  # 目標以上は除外
        candidates.append({
            "name": dept,
            "kpi": "admission",
            "icon": "🚪",
            "gap": round(float(gap), 0),
            "actual": actual,
            "target": round(float(tgt), 1),
            "period_label": "新入院（直近7日累計）",
            "reason": f"新入院の目標差{abs(gap):.0f}人が大きい",
            "href": f"dept.html#{dept}",
        })

    # ── 病棟: 在院患者数の絶対差が大きい順 ──
    from .metrics import daily_inpatient
    inp_by_ward = daily_inpatient(adm, base_date)["by_ward"]
    ward_inp_tgt = targets.get("inpatient", {}).get("ward", {})
    for wcode, actual in inp_by_ward.items():
        if wcode in WARD_HIDDEN:
            continue
        tgt = ward_inp_tgt.get(wcode)
        if tgt is None or tgt == 0:
            continue
        gap = actual - tgt
        if gap >= 0:
            continue  # 目標以上は除外
        wname = WARD_NAMES.get(wcode, wcode)
        candidates.append({
            "name": wname,
            "kpi": "inpatient",
            "icon": "🛏️",
            "gap": round(float(gap), 0),
            "actual": actual,
            "target": round(float(tgt), 1),
            "period_label": f"在院患者数（{base_date.strftime('%m/%d')}時点）",
            "reason": f"在院の目標差{abs(gap):.0f}人が大きい",
            "href": f"dept.html#{wname}",
        })

    candidates.sort(key=lambda x: x["gap"])  # 負の大きい順（差が大きい＝先頭）
    return candidates[:5]


# ═══════════════════════════════════════
# Portal用コンテキスト
# ═══════════════════════════════════════

def build_portal_context(adm, surg, targets, surg_targets,
                         base_date, generated_at=None,
                         include_ai_alerts: bool = True,
                         weekly_story: dict = None) -> dict:
    """
    portal.html テンプレート用のコンテキスト辞書を生成。

    Returns:
        Jinja2テンプレートに渡す辞書（headline, kpi_cards, attention, improvement 等）
    """
    kpi = build_kpi_summary(adm, surg, base_date, targets, surg_targets)

    # ★要注視カード: 絶対差（人・件）が大きい順で選出、目標以上は除外
    attention = _build_attention_cards(adm, surg, base_date, targets, surg_targets)

    # 改善トピック: 新入院の前週同曜日比で上位3件を配列で返す
    series_nadm = build_daily_series(adm, "新入院患者数")
    improvement_candidates = []
    for dept in NADM_DISPLAY_DEPTS:
        s = build_daily_series(adm, "新入院患者数", group_col="診療科名", group_val=dept)
        wow = week_over_week(s, base_date)
        if wow is not None and wow > 0:
            improvement_candidates.append({
                "name": dept, "kpi": "admission",
                "delta": int(wow), "compare": "前週同曜日比",
                "href": f"dept.html#{dept}",
            })
    improvement_candidates.sort(key=lambda x: -x["delta"])
    improvement = improvement_candidates[:3] if improvement_candidates else []

    # KPIカード情報
    kpi_cards = [
        {
            "id": "inpatient", "icon": KPI_ICONS["inpatient"],
            "label": "在院患者数",
            "period": (
                f"{base_date.strftime('%m/%d')} 時点"
                f"（{'平日' if kpi['inpatient_is_weekday'] else '休日'}目標"
                f"{kpi['inpatient_target']}人）"
            ),
            "value": kpi["inpatient_actual"], "unit": "人",
            "gap": kpi["inpatient_gap"], "gap_unit": "人",
            "status": kpi["inpatient_status"],
            "href": "detail.html#inpatient",
        },
        {
            "id": "admission", "icon": KPI_ICONS["admission"],
            "label": "新入院患者数", "period": "直近7日累計",
            "value": kpi["admission_actual_7d"], "unit": "人",
            "gap": kpi["admission_gap"], "gap_unit": "人",
            "status": kpi["admission_status"],
            "href": "detail.html#admission",
        },
        {
            "id": "operation", "icon": KPI_ICONS["operation"],
            "label": "全身麻酔手術", "period": "直近7平日平均",
            "value": kpi["operation_daily_avg"], "unit": "件/日",
            "gap": kpi["operation_gap"], "gap_unit": "件/日",
            "status": kpi["operation_status"],
            "href": "detail.html#operation",
        },
    ]

    # ── AI アラート（Ollama未起動時は空リストで無害に継続） ──
    ai_alerts = (_build_ai_alerts(adm, surg, targets, surg_targets, base_date)
                 if include_ai_alerts else [])

    return {
        "base_date": base_date.strftime("%Y-%m-%d"),
        "generated_at": (generated_at or datetime.now()).strftime("%Y/%m/%d %H:%M"),
        "headline": kpi["headline"],
        "kpi_cards": kpi_cards,
        "attention": attention,
        "improvement": improvement,
        "ai_alerts": ai_alerts,
        "weekly_story": weekly_story,
    }


def _build_ai_alerts(adm, surg, targets, surg_targets, base_date) -> list:
    """AIアラート検知 + LLM ナラティブ生成。失敗しても空リストを返す。"""
    try:
        from .alerts import detect_alerts
        from .ai_narrative import narrate_alerts
    except ImportError:
        return []
    try:
        raw = detect_alerts(adm, surg, targets, surg_targets, base_date)
        if not raw:
            return []
        return narrate_alerts(raw)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"AI アラート生成スキップ: {e}")
        return []


# ═══════════════════════════════════════
# Detail用 JSON一括生成
# ═══════════════════════════════════════

def build_detail_json(adm, surg, targets, surg_targets,
                      profit_monthly, base_date, generated_at=None) -> str:
    """
    detail.html に埋め込む DATA JSON 文字列を生成。
    仕様書 付録D のスキーマに準拠。
    """
    kpi = build_kpi_summary(adm, surg, base_date, targets, surg_targets)

    # ── perf: ランキングデータ（直近7日固定） ──
    perf = {"admission": {}, "inpatient": {}, "operation": {}}

    # 新入院ランキング（直近7日累計）
    dept_adm = build_dept_ranking(adm, base_date, targets, metric="new_admission")
    perf["admission"]["dept"] = _ranking_to_list(dept_adm)
    ward_adm = build_ward_ranking(adm, base_date, targets, metric="new_admission")
    perf["admission"]["ward"] = _ranking_to_list(ward_adm, name_col="病棟名")

    # 在院ランキング（基準日1日実績）
    dept_inp = build_dept_ranking(adm, base_date, targets, metric="inpatient")
    perf["inpatient"]["dept"] = _ranking_to_list(dept_inp)
    ward_inp = build_ward_ranking(adm, base_date, targets, metric="inpatient")
    perf["inpatient"]["ward"] = _ranking_to_list(ward_inp, name_col="病棟名")

    # 手術ランキング（直近7日）
    surg_rank = build_surgery_ranking(surg, base_date, surg_targets, period="7")
    perf["operation"]["dept"] = _ranking_to_list(surg_rank, target_col="週目標")

    # ── trend: 推移データ ──
    series_inp = build_daily_series(adm, "在院患者数")
    series_inp = add_moving_average(series_inp, 7)
    series_inp = add_moving_average(series_inp, 28)

    series_nadm = build_daily_series(adm, "新入院患者数")
    series_nadm = add_moving_average(series_nadm, 7)
    series_nadm = add_moving_average(series_nadm, 28)
    # 新入院内訳（病院全体）
    series_planned_hosp  = build_daily_series(adm, "入院患者数")
    series_emg_hosp      = build_daily_series(adm, "緊急入院患者数")

    series_surg = build_surgery_daily_series(surg)
    series_surg = add_moving_average(series_surg, 7)

    # ★平日/休日フラグを追加
    from .config import is_operational_day

    def _trend_dict(s: pd.DataFrame) -> dict:
        return {
            "dates": [d.strftime("%Y-%m-%d") for d in s["日付"]],
            "values": [int(v) if pd.notna(v) else 0 for v in s["値"]],
            "ma7": [round(v, 1) if pd.notna(v) else None for v in s.get("MA7", [])],
            "ma28": [round(v, 1) if pd.notna(v) else None for v in s.get("MA28", [])] if "MA28" in s.columns else [],
            "is_weekday": [bool(is_operational_day(d)) for d in s["日付"]],
        }

    adm_trend = _trend_dict(series_nadm)
    _add_adm_breakdown(adm_trend, series_planned_hosp, series_emg_hosp)

    # ★全麻 30平日移動平均（病院全体用）: 当年度 + 昨年度
    biz_ma30_curr = build_biz_ma30_series(surg, base_date, prev_year=False)
    biz_ma30_prev = build_biz_ma30_series(surg, base_date, prev_year=True)

    op_trend = _trend_dict(series_surg)
    op_trend["biz_ma30"] = biz_ma30_curr
    op_trend["biz_ma30_prev"] = biz_ma30_prev

    trend = {
        "inpatient": _trend_dict(series_inp),
        "admission": adm_trend,
        "operation": op_trend,
    }

    # ── charts: 特殊グラフ用データ ──
    heatmap = build_ward_utilization_heatmap(adm, base_date, targets)

    # ── drill: 診療科ドリルダウン ──
    drill = {}
    r7_nadm = rolling7_new_admission(adm, base_date)
    r7_surg = rolling7_surgery(surg, base_date)
    from .metrics import daily_inpatient
    inp_by_dept = daily_inpatient(adm, base_date)["by_dept"]
    nadm_tgt = targets.get("new_admission", {}).get("dept", {})
    inp_tgt = targets.get("inpatient", {}).get("dept", {})

    # 直近7日 予定/緊急 内訳（診療科・病棟別）
    from datetime import timedelta as _td
    _w7_start = base_date - _td(days=6)
    _w7 = adm[(adm["日付"] >= _w7_start) & (adm["日付"] <= base_date)]
    r7_planned_dept  = (_w7[_w7["科_表示"]].groupby("診療科名")["入院患者数"].sum().astype(int).to_dict())
    r7_emg_dept      = (_w7[_w7["科_表示"]].groupby("診療科名")["緊急入院患者数"].sum().astype(int).to_dict())
    r7_planned_ward  = (_w7[_w7["病棟_表示"]].groupby("病棟コード")["入院患者数"].sum().astype(int).to_dict())
    r7_emg_ward      = (_w7[_w7["病棟_表示"]].groupby("病棟コード")["緊急入院患者数"].sum().astype(int).to_dict())

    for dept in NADM_DISPLAY_DEPTS | SURGERY_DISPLAY_DEPTS:
        is_surgery_dept = dept in SURGERY_DISPLAY_DEPTS
        adm_actual = r7_nadm["by_dept"].get(dept, 0)
        adm_target = nadm_tgt.get(dept)
        inp_actual = inp_by_dept.get(dept, 0)
        inp_target = inp_tgt.get(dept)
        # 手術データは SURGERY_DISPLAY_DEPTS のみ（op_target 対象科）
        surg_actual = r7_surg["by_dept"].get(dept, 0) if is_surgery_dept else None
        surg_target = surg_targets.get(dept) if is_surgery_dept else None

        # ── 診療科別推移データ（新入院）＋内訳 ──
        dept_nadm_series = build_daily_series(
            adm, "新入院患者数", group_col="診療科名", group_val=dept
        )
        dept_nadm_series = add_moving_average(dept_nadm_series, 7)
        dept_planned_series = build_daily_series(adm, "入院患者数", group_col="診療科名", group_val=dept)
        dept_emg_series     = build_daily_series(adm, "緊急入院患者数", group_col="診療科名", group_val=dept)

        # ── 診療科別推移データ（在院） ──
        dept_inp_series = build_daily_series(
            adm, "在院患者数", group_col="診療科名", group_val=dept
        )
        dept_inp_series = add_moving_average(dept_inp_series, 7)

        # ── 診療科別推移データ（手術）: 手術対象科のみ ──
        if is_surgery_dept:
            dept_surg_series = build_surgery_daily_series(surg, ga_only=True, dept=dept)
            dept_surg_series = add_moving_average(dept_surg_series, 7)
        else:
            dept_surg_series = pd.DataFrame(columns=["日付", "値"])

        # ── 注視理由・コメント自動生成 ──
        comments = []
        adm_rate = achievement_rate(adm_actual, adm_target)
        inp_rate = achievement_rate(inp_actual, inp_target)
        surg_rate = achievement_rate(surg_actual, surg_target)
        if adm_rate is not None and adm_rate < 90:
            comments.append(f"新入院が目標の{adm_rate:.0f}%（{adm_actual}/{adm_target:.1f}）")
        if inp_rate is not None and inp_rate < 90:
            comments.append(f"在院患者が目標の{inp_rate:.0f}%（{inp_actual}/{inp_target:.1f}）")
        if surg_rate is not None and surg_rate < 90:
            comments.append(f"全麻手術が目標の{surg_rate:.0f}%（{surg_actual}/{surg_target:.1f}）")
        if not comments:
            # 達成している場合
            best_rate = max(filter(None, [adm_rate, inp_rate, surg_rate]), default=0)
            if best_rate >= 100:
                comments.append("目標を達成しています")
            else:
                comments.append("目標に接近しています")

        dept_adm_trend = (_trend_dict(dept_nadm_series) if len(dept_nadm_series) > 0
                          else {"dates": [], "values": [], "ma7": [], "ma28": []})
        _add_adm_breakdown(dept_adm_trend, dept_planned_series, dept_emg_series)

        drill[dept] = {
            "admission": {
                "actual_7d": adm_actual,
                "planned_7d": r7_planned_dept.get(dept, 0),
                "emergency_7d": r7_emg_dept.get(dept, 0),
                "target": round(float(adm_target), 1) if adm_target else None,
                "rate": adm_rate,
            },
            "inpatient": {
                "actual": inp_actual,
                "target": round(float(inp_target), 1) if inp_target else None,
                "rate": inp_rate,
            },
            "operation": {
                "actual": surg_actual,
                "target": round(float(surg_target), 1) if surg_target else None,
                "rate": surg_rate,
            },
            "trend": {
                "admission": dept_adm_trend,
                "inpatient": _trend_dict(dept_inp_series) if len(dept_inp_series) > 0 else {"dates":[],"values":[],"ma7":[],"ma28":[]},
                "operation": _trend_dict(dept_surg_series) if len(dept_surg_series) > 0 else {"dates":[],"values":[],"ma7":[]},
            },
            "comment": "、".join(comments),
        }

    # ── drill: 病棟ドリルダウン ──
    from .config import WARD_NAMES, WARD_HIDDEN
    from .metrics import daily_new_admission
    inp_by_ward = daily_inpatient(adm, base_date)["by_ward"]
    nadm_day = daily_new_admission(adm, base_date)
    r7_nadm_ward = rolling7_new_admission(adm, base_date)["by_ward"]
    ward_inp_tgt = targets.get("inpatient", {}).get("ward", {})
    ward_nadm_tgt = targets.get("new_admission", {}).get("ward", {})
    ward_beds = targets.get("inpatient", {}).get("ward_beds", {})

    for wcode in WARD_NAMES:
        if wcode in WARD_HIDDEN:
            continue
        wname = WARD_NAMES[wcode]

        w_inp = inp_by_ward.get(wcode, 0)
        w_inp_tgt = ward_inp_tgt.get(wcode)
        w_nadm = r7_nadm_ward.get(wcode, 0)
        w_nadm_tgt = ward_nadm_tgt.get(wcode)
        w_beds = ward_beds.get(wcode)
        w_util = round(w_inp / w_beds * 100, 1) if w_beds else None
        w_load = nadm_day["by_ward_load"].get(wcode, 0)
        w_discharge = nadm_day["by_ward_discharge"].get(wcode, 0)

        # 病棟別推移（新入院は転入含む新入院患者数_病棟）
        w_inp_series = build_daily_series(adm, "在院患者数", group_col="病棟コード", group_val=wcode)
        w_inp_series = add_moving_average(w_inp_series, 7)
        w_nadm_series = build_daily_series(adm, "新入院患者数_病棟", group_col="病棟コード", group_val=wcode)
        w_nadm_series = add_moving_average(w_nadm_series, 7)
        # 内訳（予定・緊急）: 転入は含まない
        w_planned_series = build_daily_series(adm, "入院患者数", group_col="病棟コード", group_val=wcode)
        w_emg_series     = build_daily_series(adm, "緊急入院患者数", group_col="病棟コード", group_val=wcode)
        # 退出合計（退院+死亡+転出）
        w_out_series = build_daily_series(adm, "退出合計", group_col="病棟コード", group_val=wcode)
        w_out_series = add_moving_average(w_out_series, 7)

        # コメント
        w_inp_rate = achievement_rate(w_inp, w_inp_tgt)
        w_comments = []
        if w_util is not None and w_util < 85:
            w_comments.append(f"利用率{w_util:.0f}%（目標85%以上）")
        if w_inp_rate is not None and w_inp_rate < 90:
            w_comments.append(f"在院患者が目標の{w_inp_rate:.0f}%（{w_inp}/{w_inp_tgt:.1f}）")
        if w_load >= 15:
            w_comments.append(f"入退院負荷が高い（{w_load}件）")
        if not w_comments:
            if w_util and w_util >= 95:
                w_comments.append(f"利用率{w_util:.0f}%で良好")
            else:
                w_comments.append("目標に接近しています")

        w_adm_trend = (_trend_dict(w_nadm_series) if len(w_nadm_series) > 0
                       else {"dates": [], "values": [], "ma7": [], "ma28": []})
        _add_adm_breakdown(w_adm_trend, w_planned_series, w_emg_series)

        drill[wname] = {
            "admission": {
                "actual_7d": w_nadm,
                "planned_7d": r7_planned_ward.get(wcode, 0),
                "emergency_7d": r7_emg_ward.get(wcode, 0),
                "target": round(float(w_nadm_tgt), 1) if w_nadm_tgt else None,
                "rate": achievement_rate(w_nadm, w_nadm_tgt),
            },
            "inpatient": {
                "actual": w_inp,
                "target": round(float(w_inp_tgt), 1) if w_inp_tgt else None,
                "rate": w_inp_rate,
            },
            "operation": {
                "actual": w_discharge,
                "target": None,
                "rate": None,
                "label": "退院関連",
            },
            "ward_extra": {
                "beds": w_beds,
                "util_rate": w_util,
                "load": w_load,
            },
            "trend": {
                "admission": w_adm_trend,
                "inpatient": _trend_dict(w_inp_series) if len(w_inp_series) > 0 else {"dates":[],"values":[],"ma7":[],"ma28":[]},
                "operation": {"dates":[],"values":[],"ma7":[]},
                "outflow": (_trend_dict(w_out_series) if len(w_out_series) > 0
                            else {"dates":[],"values":[],"ma7":[],"ma28":[]}),
            },
            "comment": "、".join(w_comments),
        }

    # ── attention / improvement ──
    # detail.html では AI アラートは不要（portal.html 専用）
    portal_ctx = build_portal_context(adm, surg, targets, surg_targets, base_date,
                                       generated_at, include_ai_alerts=False)

    # ── profit: 粗利データ ──
    profit_section = None
    if profit_monthly is not None and len(profit_monthly) > 0:
        try:
            p_kpi = build_profit_kpi(profit_monthly)
            p_chart = build_profit_chart_data(profit_monthly)
            from .profit import get_latest_month_summary
            p_latest = get_latest_month_summary(profit_monthly)
            p_ranking = []
            for i, r in p_latest.iterrows():
                st = status_display(r["達成率"]) if pd.notna(r["達成率"]) else status_display(0)
                p_ranking.append({
                    "rank": i + 1,
                    "name": r["診療科名"],
                    "actual": round(float(r["粗利"]) / 1000, 1) if pd.notna(r["粗利"]) else 0,
                    "target": round(float(r["月次目標"]) / 1000, 1) if pd.notna(r["月次目標"]) else None,
                    "rate": float(r["達成率"]) if pd.notna(r["達成率"]) else None,
                    "mom": round(float(r["前月比"]) / 1000, 1) if pd.notna(r.get("前月比")) else None,
                    "status": st["css"],
                    "shape": st["shape"],
                    "text": st["text"],
                })
            profit_section = {
                "kpi": p_kpi,
                "chart": p_chart,
                "ranking": p_ranking,
            }
            # Timestamp を文字列に変換
            if "base_month" in profit_section["kpi"]:
                profit_section["kpi"]["base_month"] = profit_section["kpi"]["base_month"].strftime("%Y-%m")
        except Exception:
            pass

    # ── assemble ──
    data = {
        "meta": {
            "base_date": base_date.strftime("%Y-%m-%d"),
            "generated": (generated_at or datetime.now()).isoformat(),
        },
        "headline": kpi["headline"],
        "kpi": {
            "inpatient": {
                "actual": kpi["inpatient_actual"],
                "target": kpi["inpatient_target"],
                "target_allday": kpi["inpatient_target_allday"],
                "target_weekday": TARGET_INPATIENT_WEEKDAY,
                "target_holiday": TARGET_INPATIENT_HOLIDAY,
                "is_weekday": kpi["inpatient_is_weekday"],
                "rate": kpi["inpatient_rate"],
                "avg_7d": kpi["inpatient_avg_7d"],
                "avg_28d": kpi["inpatient_avg_28d"],
                "fy_avg": kpi["inpatient_fy_avg"],
                "prev_avg": kpi["inpatient_prev_avg"],
                "prev_7d_avg": kpi["inpatient_prev_7d_avg"],
                "prev_28d_avg": kpi["inpatient_prev_28d_avg"],
                "prior_range_avg": kpi["inpatient_prior_range_avg"],
                "avg_7d_wd": kpi["inpatient_avg_7d_wd"],
                "avg_7d_hd": kpi["inpatient_avg_7d_hd"],
                "avg_28d_wd": kpi["inpatient_avg_28d_wd"],
                "avg_28d_hd": kpi["inpatient_avg_28d_hd"],
                "fy_avg_wd": kpi["inpatient_fy_avg_wd"],
                "fy_avg_hd": kpi["inpatient_fy_avg_hd"],
                "gap": kpi["inpatient_gap"],
                "trend": kpi["inpatient_trend"],
                "status": kpi["inpatient_status"],
            },
            "admission": {
                "actual_7d": kpi["admission_actual_7d"],
                "actual_14d_weekly": kpi["admission_actual_14d_weekly"],
                "prior_range_weekly": kpi["admission_prior_range_weekly"],
                "actual_28d": kpi["admission_actual_28d"],
                "target_weekly": kpi["admission_target_weekly"],
                "rate_7d": kpi["admission_rate_7d"],
                "fy_avg": kpi["admission_fy_avg"],
                "fy_rate": kpi["admission_fy_rate"],
                "prev_avg": kpi["admission_prev_avg"],
                "prev_7d_total": kpi["admission_prev_7d_total"],
                "prev_28d_total": kpi["admission_prev_28d_total"],
                "prev_fy_avg": kpi["admission_prev_fy_avg"],
                "gap": kpi["admission_gap"],
                "daily_actual": kpi["admission_daily_actual"],
                "trend": kpi["admission_trend"],
                "status": kpi["admission_status"],
            },
            "operation": {
                "daily_avg": kpi["operation_daily_avg"],
                "target": kpi["operation_target"],
                "rate": kpi["operation_rate"],
                "week_total": kpi["operation_week_total"],
                "fy_avg": kpi["operation_fy_avg"],
                "4w_biz_avg": kpi["operation_4w_biz_avg"],
                "gap": kpi["operation_gap"],
                "prev_4w_avg": kpi["operation_prev_4w_avg"],
                "prev_week_total": kpi["operation_prev_week_total"],
                "prev_fy_avg": kpi["operation_fy_prev_avg"],
                "trend": kpi["operation_trend"],
                "status": kpi["operation_status"],
            },
        },
        "attention": portal_ctx["attention"],
        "improvement": portal_ctx["improvement"],
        "perf": perf,
        "trend": trend,
        "drill": drill,
        "charts": {
            "occupancy_heatmap": heatmap,
        },
    }

    if profit_section:
        data["profit"] = profit_section
        # 各診療科の drill に粗利データを付与
        profit_by_dept = {r["name"]: r for r in profit_section.get("ranking", [])}
        profit_chart_by_dept = profit_section.get("chart", {}).get("by_dept", {})
        for dname, drill_entry in data["drill"].items():
            if drill_entry.get("ward_extra"):
                continue  # 病棟はスキップ
            if dname in profit_by_dept:
                pr = profit_by_dept[dname]
                drill_entry["profit"] = {
                    "actual": pr["actual"],
                    "target": pr["target"],
                    "rate": pr["rate"],
                    "status": pr["status"],
                    "shape": pr["shape"],
                    "text": pr["text"],
                }
            if dname in profit_chart_by_dept:
                drill_entry["profit_chart"] = profit_chart_by_dept[dname]

    return json.dumps(data, ensure_ascii=False, default=_json_safe)
