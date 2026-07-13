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
    SURGERY_DISPLAY_DEPTS, NADM_DISPLAY_DEPTS, PROFIT_ONLY_DISPLAY_DEPTS,
)
from .metrics import (
    build_kpi_summary, build_dept_ranking, build_ward_ranking,
    build_surgery_ranking, build_doctor_watch_ranking,
    build_nurse_watch_ranking, build_nurse_load_ranking,
    rolling7_new_admission, rolling7_surgery,
    build_daily_series, build_surgery_daily_series, add_moving_average,
    build_biz_ma30_series, build_prevyear_daily_series,
    build_prevyear_weekly_series,
    week_over_week, achievement_rate, discharge_dow_profile,
    weekend_census_retention,
)
from .charts import (
    build_inpatient_chart, build_new_admission_chart,
    build_surgery_chart_hospital, build_surgery_chart_dept,
    build_surgery_year_compare_chart, build_ward_utilization_heatmap,
    build_discharge_dow_heatmap, build_dow_heatmap, dow_shared_units,
    build_dow_unit_detail,
)
from .profit import build_profit_kpi, build_profit_chart_data
from .profit_estimate import (
    build_estimate_payload as build_profit_estimate_payload,
    build_hybrid_payload as build_profit_hybrid_payload,
    apply_recency_calibration,
    blend_and_calibrate_series,
    last_complete_driver_date,
)
from .month_projection import build_month_projection_payload, profit_target_for_month
from .moves_store import load_latest_moves


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


def _series_value_at(series, date_str: str):
    """日次系列 values_final_total から date_str 以下の直近非None値を返す。

    系列(dates)は昇順前提。各見込み月の「月末見込み」を、当月(6月)基準で既に
    計算済みの日次 values_final_total から min(base_date, 月末) 日の値として取り出す。
    """
    if not series:
        return None
    dates = series.get("dates") or []
    vals = series.get("values_final_total") or []
    if not dates or len(vals) != len(dates):
        return None
    best = None
    for d, v in zip(dates, vals):
        if d <= date_str and v is not None:
            best = v
    return best


def _build_profit_projections(series, profit_monthly, pending_months,
                              profit_base_date, driver_month, dept=None):
    """粗利の見込み対象月（確報待ちの前月＋当月）の {month,projection,target,rate,provisional}
    リストを返す。値は既存の日次 values_final_total から抽出（第2 payload を増やさない）。"""
    out = []
    for m in pending_months:
        month_end = m + pd.offsets.MonthEnd(0)
        date_m = min(pd.Timestamp(profit_base_date), month_end)
        val = _series_value_at(series, date_m.strftime("%Y-%m-%d"))
        if val is None:
            continue
        tgt = profit_target_for_month(profit_monthly, m, dept)
        rate = round(float(val) / tgt * 100, 1) if (tgt and tgt > 0) else None
        out.append({
            "month": m.strftime("%Y-%m"),
            "projection": round(float(val), 1),
            "target": tgt,
            "rate": rate,
            "provisional": bool(m < driver_month),
        })
    return out or None


def _profit_pending_months(profit_monthly, profit_base_date):
    """見込み対象月リスト = 確定粗利の最終月の翌月 〜 ドライバー月（直近3か月上限）。

    粗利が確報入力されると対象から外れ、当月単月へ自動収束する（pl_projection と同ルール）。
    """
    driver_month = pd.Timestamp(profit_base_date).normalize().replace(day=1)
    if profit_monthly is None or len(profit_monthly) == 0:
        return [driver_month], driver_month
    last_g_month = profit_monthly["月"].max()
    pend = pd.date_range(last_g_month + pd.offsets.MonthBegin(1),
                         driver_month, freq="MS").tolist()
    if not pend:
        pend = [driver_month]
    return pend[-3:], driver_month


def _add_adm_breakdown(td: dict, planned_s: pd.DataFrame, emg_s: pd.DataFrame,
                       base_date: pd.Timestamp = None) -> dict:
    """trend dict に予定/緊急入院の内訳配列を追加（日付を key にして安全にアライン）。

    base_date 指定時は種別別の昨年度同期 日次生データ（prev_daily_planned /
    prev_daily_emergency）も付与する。新入院チャートの予定/緊急フィルタ時に
    昨年度同期線も同種別へ追随させるための比較系列（全体 prev_daily と同形式）。
    """
    p_map = ({d.strftime("%Y-%m-%d"): int(v) for d, v in zip(planned_s["日付"], planned_s["値"]) if pd.notna(v)}
             if len(planned_s) > 0 else {})
    e_map = ({d.strftime("%Y-%m-%d"): int(v) for d, v in zip(emg_s["日付"], emg_s["値"]) if pd.notna(v)}
             if len(emg_s) > 0 else {})
    td["planned"]  = [p_map.get(d, 0) for d in td["dates"]]
    td["emergency"] = [e_map.get(d, 0) for d in td["dates"]]
    if base_date is not None:
        td["prev_daily_planned"]   = build_prevyear_daily_series(planned_s, base_date)
        td["prev_daily_emergency"] = build_prevyear_daily_series(emg_s, base_date)
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
                         weekly_story: dict = None,
                         profit_monthly=None,
                         include_triage: bool = True,
                         kpi_history_path=None) -> dict:
    """
    portal.html テンプレート用のコンテキスト辞書を生成。

    Returns:
        Jinja2テンプレートに渡す辞書（headline, kpi_cards, triage, improvement 等）
    """
    kpi = build_kpi_summary(adm, surg, base_date, targets, surg_targets)

    # ★要注視カード（detail.html 用に維持）
    attention = _build_attention_cards(adm, surg, base_date, targets, surg_targets)

    # 改善トピック: 北極星KPIの前週比で各群上位3件（プラスのみ）
    #   内科系 → 在院の前週同曜日比（人）
    #   外科系 → 全麻の直近7日累計 前週比（件。単日だと週末ゼロで不安定なため7日窓）
    #   病棟   → 在院の前週同曜日比（人）
    def _wow_top3(cands):
        c = [x for x in cands if x["delta"] > 0]
        c.sort(key=lambda x: -x["delta"])
        return c[:3]

    r7s_now  = rolling7_surgery(surg, base_date)["by_dept"]
    r7s_prev = rolling7_surgery(surg, base_date - pd.Timedelta(days=7))["by_dept"]

    dept_imp_internal, dept_imp_surgery = [], []
    for dept in NADM_DISPLAY_DEPTS:
        if dept in SURGERY_DISPLAY_DEPTS:
            delta = int(r7s_now.get(dept, 0)) - int(r7s_prev.get(dept, 0))
            if delta > 0:
                dept_imp_surgery.append({
                    "name": dept, "kpi": "operation",
                    "metric_label": "全麻", "unit": "件",
                    "delta": delta, "compare": "前週比（7日累計）",
                    "href": f"dept.html#{dept}",
                })
        else:
            s = build_daily_series(adm, "在院患者数", group_col="診療科名", group_val=dept)
            wow = week_over_week(s, base_date)
            if wow is not None:
                dept_imp_internal.append({
                    "name": dept, "kpi": "inpatient",
                    "metric_label": "在院", "unit": "人",
                    "delta": int(wow), "compare": "前週同曜日比",
                    "href": f"dept.html#{dept}",
                })
    dept_imp_internal = _wow_top3(dept_imp_internal)
    dept_imp_surgery  = _wow_top3(dept_imp_surgery)

    # 改善トピック: 在院の前週同曜日比で上位3件（病棟）
    from .config import WARD_NAMES, WARD_HIDDEN
    ward_imp_candidates = []
    for wcode, wname in WARD_NAMES.items():
        if wcode in WARD_HIDDEN:
            continue
        s = build_daily_series(adm, "在院患者数", group_col="病棟コード", group_val=wcode)
        wow = week_over_week(s, base_date)
        if wow is not None:
            ward_imp_candidates.append({
                "name": wname, "kpi": "inpatient",
                "metric_label": "在院", "unit": "人",
                "delta": int(wow), "compare": "前週同曜日比",
                "href": "detail.html#inpatient?axis=ward",
            })
    ward_improvement = _wow_top3(ward_imp_candidates)

    improvement = {"dept_internal": dept_imp_internal,
                   "dept_surgery": dept_imp_surgery,
                   "ward": ward_improvement}

    # KPIカード情報
    # 在院は「直近7日の平日平均／休日平均」を併記（枠・バッジは平日=主目標基準）
    _inp_wd, _inp_hd = kpi["inpatient_avg_7d_wd"], kpi["inpatient_avg_7d_hd"]
    _inp_wd_rate = achievement_rate(_inp_wd, TARGET_INPATIENT_WEEKDAY)
    _inp_hd_rate = achievement_rate(_inp_hd, TARGET_INPATIENT_HOLIDAY)
    kpi_cards = [
        {
            "id": "inpatient", "icon": KPI_ICONS["inpatient"],
            "label": "在院患者数", "period": "直近7日平均（平日／休日）",
            "value": kpi["inpatient_actual"], "unit": "人",
            "gap": kpi["inpatient_gap"], "gap_unit": "人",
            "status": status_display(_inp_wd_rate),
            "href": "detail.html#inpatient",
            "dual": {
                "wd": {"label": "平日", "value": _inp_wd, "target": TARGET_INPATIENT_WEEKDAY,
                       "status": status_display(_inp_wd_rate)},
                "hd": {"label": "休日", "value": _inp_hd, "target": TARGET_INPATIENT_HOLIDAY,
                       "status": status_display(_inp_hd_rate)},
            },
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

    # ── A2: 前年同期比（build_kpi_summary の既存前年値を再利用）──
    def _yoy(cur, prev, note):
        if cur is None or prev is None or prev == 0:
            return None                     # 前年データ不足 → チップ非表示
        pct = (cur - prev) / abs(prev) * 100.0
        css = "ok" if pct >= 5 else ("dr" if pct <= -5 else "mu")   # dept.htmlのyoyBadgeと同じ±5%
        arrow = "↑" if pct >= 5 else ("↓" if pct <= -5 else "→")
        return {"pct": round(pct, 1), "prev": prev, "css": css, "arrow": arrow, "note": note}

    kpi_cards[0]["yoy"] = _yoy(kpi["inpatient_avg_7d"],  kpi["inpatient_prev_7d_avg"],  "7日平均")
    kpi_cards[1]["yoy"] = _yoy(kpi["admission_actual_7d"], kpi["admission_prev_7d_total"], "7日累計")
    kpi_cards[2]["yoy"] = _yoy(kpi["operation_4w_biz_avg"], kpi["operation_prev_4w_avg"], "4週平日平均")

    # ── A2: 当月着地見込み（detail/deptと同じ month_projection を portal にも）──
    try:
        mp = build_month_projection_payload(adm, surg, profit_monthly, None, None, base_date)
        for card, key in zip(kpi_cards, ("inpatient", "admission", "operation")):
            tile = mp.get(key)
            if tile and tile.get("projection") is not None:
                card["proj"] = {
                    "value": tile["projection"], "target": tile["target"],
                    "rate": tile["rate"], "unit": tile["unit"],
                    "status_css": tile["status_css"], "month": base_date.month,
                }
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"当月着地見込みスキップ: {e}")

    # ── 部門トリアージ（多KPI合成スコアリング + LLMナラティブ）──
    # detail.html では triage を使わない（attention/improvement のみ）。
    # その場合 LLM 計算を丸ごとスキップして二重実行を防ぐ。
    triage = (_build_triage(adm, surg, targets, surg_targets, profit_monthly, base_date)
              if include_triage
              else {"dept_internal": [], "dept_surgery": [], "ward": [],
                    "dept_leveling": [], "ward_leveling": []})

    # ── A4/B4: 継続日数と変化点（失敗しても portal は完走）──
    # detail.html（include_triage=False）ではトリアージ自体を計算しないため対象外。
    # leveling 系（dept_leveling/ward_leveling）は週次性格の別軸のため streak を付けない。
    changes = None
    if include_triage:
        try:
            from .portal_history import build_attention_history, kpi_status_changes, HISTORY_DAYS
            hist = build_attention_history(adm, surg, targets, surg_targets, profit_monthly, base_date)
            for group in ("dept_internal", "dept_surgery", "ward"):
                for item in triage.get(group, []):
                    item["streak_days"] = hist["streaks"].get((item["entity_type"], item["name"]), 1)
                    item["streak_capped"] = item["streak_days"] >= HISTORY_DAYS
            changes = {
                "prev_date": hist["prev_date"],
                "triage_in": hist["entered"], "triage_out": hist["exited"],
                "kpi": kpi_status_changes(kpi_history_path, base_date) if kpi_history_path else [],
            }
            if not (changes["triage_in"] or changes["triage_out"] or changes["kpi"]):
                changes["quiet"] = True     # 「変化なし」表示用
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"変化点/継続日数スキップ: {e}")

    # ── AI アラート（後方互換：include_ai_alerts=True 時のみ。portal では使用しない）──
    ai_alerts = (_build_ai_alerts(adm, surg, targets, surg_targets, base_date)
                 if include_ai_alerts else [])

    return {
        "base_date": base_date.strftime("%Y-%m-%d"),
        "generated_at": (generated_at or datetime.now()).strftime("%Y/%m/%d %H:%M"),
        "headline": kpi["headline"],
        "kpi_cards": kpi_cards,
        "triage": triage,
        "attention": attention,       # detail.html 用に維持
        "improvement": improvement,
        "ai_alerts": ai_alerts,
        "weekly_story": weekly_story,
        "changes": changes,
    }


def _build_triage(adm, surg, targets, surg_targets, profit_monthly, base_date) -> dict:
    """部門トリアージを生成。失敗しても空 dict を返す。"""
    try:
        from .triage import build_triage_section
    except ImportError:
        return {"dept_internal": [], "dept_surgery": [], "ward": [], "dept_leveling": [], "ward_leveling": []}
    try:
        return build_triage_section(
            adm, surg, targets, surg_targets, profit_monthly, base_date
        )
    except Exception as e:
        import logging as _logging
        _logging.getLogger(__name__).warning(f"部門トリアージ生成スキップ: {e}")
        return {"dept_internal": [], "dept_surgery": [], "ward": [], "dept_leveling": [], "ward_leveling": []}


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
                      profit_monthly, base_date, generated_at=None,
                      profit_breakdown=None) -> str:
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
    if len(series_surg) > 0:
        full_idx = pd.date_range(series_surg["日付"].min(), base_date, freq="D")
        series_surg = series_surg.set_index("日付").reindex(full_idx, fill_value=0).reset_index().rename(columns={"index": "日付"})
    series_surg = add_moving_average(series_surg, 7)

    # ★平日/休日フラグを追加
    from .config import is_operational_day

    def _trend_dict(s: pd.DataFrame, prevyear: bool = False) -> dict:
        d = {
            "dates": [d.strftime("%Y-%m-%d") for d in s["日付"]],
            "values": [int(v) if pd.notna(v) else 0 for v in s["値"]],
            "ma7": [round(v, 1) if pd.notna(v) else None for v in s.get("MA7", [])],
            "ma28": [round(v, 1) if pd.notna(v) else None for v in s.get("MA28", [])] if "MA28" in s.columns else [],
            "is_weekday": [bool(is_operational_day(d)) for d in s["日付"]],
        }
        # ★昨年度同期の日次生データ（在院・新入院・部門別用）。フロントが当年線と同一の
        #   filterByDayType→calcMA で算出し、平日/休日・入院種別フィルタへ同条件で追随する。
        if prevyear and len(s) > 0:
            d["prev_daily"] = build_prevyear_daily_series(s, base_date)
        return d

    adm_trend = _trend_dict(series_nadm, prevyear=True)
    _add_adm_breakdown(adm_trend, series_planned_hosp, series_emg_hosp, base_date)

    # ★全麻 30平日移動平均（病院全体用）: 当年度 + 昨年度
    biz_ma30_curr = build_biz_ma30_series(surg, base_date, prev_year=False)
    biz_ma30_prev = build_biz_ma30_series(surg, base_date, prev_year=True)

    op_trend = _trend_dict(series_surg)
    op_trend["biz_ma30"] = biz_ma30_curr
    op_trend["biz_ma30_prev"] = biz_ma30_prev

    trend = {
        "inpatient": _trend_dict(series_inp, prevyear=True),
        "admission": adm_trend,
        "operation": op_trend,
    }

    # ── 入退院バランス（フロー収支・病院全体） ──
    # 病院全体では転入/転出は病棟間移動で相殺するため、在院数からの流出は退院合計
    # (退院+死亡)。ΔCensus ≈ 新入院患者数(inflow) − 退院合計(outflow)。
    series_outflow = build_daily_series(adm, "退院合計")
    _bal = (series_nadm[["日付", "値"]].rename(columns={"値": "inflow"})
            .merge(series_outflow[["日付", "値"]].rename(columns={"値": "outflow"}),
                   on="日付", how="outer").sort_values("日付").reset_index(drop=True))
    _bal["inflow"] = _bal["inflow"].fillna(0)
    _bal["outflow"] = _bal["outflow"].fillna(0)
    trend["balance"] = {
        "dates": [d.strftime("%Y-%m-%d") for d in _bal["日付"]],
        "inflow": [int(v) for v in _bal["inflow"]],
        "outflow": [int(v) for v in _bal["outflow"]],
        "is_weekday": [bool(is_operational_day(d)) for d in _bal["日付"]],
    }
    # 直近7日/28日 ネットフロー KPI（在院数の増減基調）
    _b7, _b28 = _bal.tail(7), _bal.tail(28)
    _net7 = round((_b7["inflow"].sum() - _b7["outflow"].sum()) / 7, 1)
    _net28 = round((_b28["inflow"].sum() - _b28["outflow"].sum()) / max(len(_b28), 1), 1)
    if _net7 >= 0.5:
        _bal_status = {"css": "ok", "shape": "▲", "text": "増基調"}
    elif _net7 <= -0.5:
        _bal_status = {"css": "dr", "shape": "▼", "text": "減基調"}
    else:
        _bal_status = {"css": "wr", "shape": "―", "text": "横ばい"}
    if _net7 - _net28 >= 0.3:
        _bal_trend = {"css": "ok", "label": "▲ 加速"}
    elif _net7 - _net28 <= -0.3:
        _bal_trend = {"css": "dr", "label": "▼ 減速"}
    else:
        _bal_trend = {"css": "mu", "label": "→ 一定"}
    balance_kpi = {
        "net_7d": _net7, "net_28d": _net28,
        "inflow_7d": round(_b7["inflow"].mean(), 1),
        "outflow_7d": round(_b7["outflow"].mean(), 1),
        "status": _bal_status, "trend": _bal_trend,
    }

    # ── charts: 特殊グラフ用データ ──
    heatmap = build_ward_utilization_heatmap(adm, base_date, targets)
    discharge_heatmap = build_discharge_dow_heatmap(adm, base_date, entity="ward")
    discharge_heatmap_dept = build_discharge_dow_heatmap(adm, base_date, entity="dept")

    # 退院・入院 曜日ヒートマップ（2段組・指標ラジオ・現状/4週Δトグル）
    # キー: f"{entity}_{metric}_{mode}"。診療科では転入は対象外。
    # 退院・入院で行（病棟/診療科）の並びが揃うよう、共通の順序付きユニット列を渡す。
    dow_heatmaps = {}
    dow_unit_detail = {}
    for ent in ("ward", "dept"):
        units = dow_shared_units(adm, base_date, entity=ent)
        metrics = ["discharge", "admission", "planned", "emergency"]
        if ent == "ward":
            metrics += ["transfer_in"]
        for met in metrics:
            for mode in ("current", "delta4w"):
                dow_heatmaps[f"{ent}_{met}_{mode}"] = build_dow_heatmap(
                    adm, base_date, entity=ent, metric=met, mode=mode, units=units)
        # 行クリック・ドリル用の単一ユニット入退院データ
        dow_unit_detail[ent] = build_dow_unit_detail(adm, base_date, ent, units)

    # 週末(土日)在院ディップ＝平準化アクション層の主データ（維持率/のびしろ/4週Δ）。
    # 在院ディップは土日窓（金曜の在院は平日水準。金曜の退院ラッシュは曜日ヒートで別掲）。
    weekend_leveling = {
        ent: weekend_census_retention(adm, base_date, entity=ent, weeks=8)
        for ent in ("ward", "dept")
    }
    # のびしろ上位ユニットに「今週の一手」narrative を付与（oMLX/Swallow-8B）。
    # 未起動・モデル未取得時は narrative=None → フロントが定型文で代替（無害縮退）。
    from .ai_narrative import narrate_leveling_actions
    weekend_leveling = narrate_leveling_actions(weekend_leveling, dow_unit_detail, top_n=6)

    # ── drill: 診療科ドリルダウン ──
    drill = {}
    # ── A1: 部門レポートPDFの「この期間の一手」確定値（moves 無し/対象なしは非表示に縮退）──
    moves = load_latest_moves(base_date)
    _report_label = moves and pd.Timestamp(moves["base_date"]).strftime("%-m/%-d")
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

    # PROFIT_ONLY 科（放射線治療科・メンタルケア科）は入院/手術の患者データに
    # 出ないが粗利はあるため drill に含める。入院/手術 KPI は target/actual が
    # 無く None/0 になり、フロントは「—」表示・手術行非表示で吸収する。粗利は
    # 後段の profit / profit_hybrid attach で既存経路どおり付与される。
    for dept in NADM_DISPLAY_DEPTS | SURGERY_DISPLAY_DEPTS | PROFIT_ONLY_DISPLAY_DEPTS:
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

        # ── 診療科別 入退院バランス用 流出（退院合計＝退院+死亡） ──
        # 診療科では転入/転出は同一科内の病棟間移動でゼロ和（実データで残差不変を確認）。
        # よって流出は退院合計のみで ΔCensus ≈ 新入院 − 退院合計 が成立。
        dept_outflow_series = build_daily_series(adm, "退院合計", group_col="診療科名", group_val=dept)

        # ── 診療科別推移データ（手術）: 手術対象科のみ ──
        if is_surgery_dept:
            dept_surg_series = build_surgery_daily_series(surg, ga_only=True, dept=dept)
            if len(dept_surg_series) > 0:
                full_idx = pd.date_range(dept_surg_series["日付"].min(), base_date, freq="D")
                dept_surg_series = dept_surg_series.set_index("日付").reindex(full_idx, fill_value=0).reset_index().rename(columns={"index": "日付"})
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

        dept_adm_trend = (_trend_dict(dept_nadm_series, prevyear=True) if len(dept_nadm_series) > 0
                          else {"dates": [], "values": [], "ma7": [], "ma28": []})
        _add_adm_breakdown(dept_adm_trend, dept_planned_series, dept_emg_series, base_date)

        # 全麻（週次合計表示）: 昨年度同期の週次合計線を付与
        if len(dept_surg_series) > 0:
            dept_op_trend = _trend_dict(dept_surg_series)
            dept_op_trend["weekly_prev"] = build_prevyear_weekly_series(dept_surg_series, base_date)
        else:
            dept_op_trend = {"dates": [], "values": [], "ma7": []}

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
                "inpatient": _trend_dict(dept_inp_series, prevyear=True) if len(dept_inp_series) > 0 else {"dates":[],"values":[],"ma7":[],"ma28":[]},
                "operation": dept_op_trend,
                "outflow": (_trend_dict(dept_outflow_series) if len(dept_outflow_series) > 0
                            else {"dates":[],"values":[],"ma7":[],"ma28":[]}),
            },
            "discharge_dow": discharge_dow_profile(adm, base_date, group_col="診療科名", group_val=dept),
            "comment": "、".join(comments),
        }
        mv = moves and moves["units"].get(f"dept:{dept}")
        if mv:
            drill[dept]["move"] = {k: mv[k] for k in ("body", "action", "surg_line", "util_line", "nadm_line") if mv.get(k)}
            drill[dept]["move"]["report_date"] = _report_label

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

        w_adm_trend = (_trend_dict(w_nadm_series, prevyear=True) if len(w_nadm_series) > 0
                       else {"dates": [], "values": [], "ma7": [], "ma28": []})
        _add_adm_breakdown(w_adm_trend, w_planned_series, w_emg_series, base_date)

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
                "inpatient": _trend_dict(w_inp_series, prevyear=True) if len(w_inp_series) > 0 else {"dates":[],"values":[],"ma7":[],"ma28":[]},
                "operation": {"dates":[],"values":[],"ma7":[]},
                "outflow": (_trend_dict(w_out_series) if len(w_out_series) > 0
                            else {"dates":[],"values":[],"ma7":[],"ma28":[]}),
            },
            "discharge_dow": discharge_dow_profile(adm, base_date, group_col="病棟コード", group_val=wcode),
            "comment": "、".join(w_comments),
        }
        mv = moves and moves["units"].get(f"ward:{wname}")
        if mv:
            drill[wname]["move"] = {k: mv[k] for k in ("body", "action", "surg_line", "util_line", "nadm_line") if mv.get(k)}
            drill[wname]["move"]["report_date"] = _report_label

    # ── attention / improvement ──
    # detail.html は attention/improvement のみ使用。AI アラート/トリアージ/
    # 退院平準化（いずれも LLM）は portal.html 専用なのでここでは計算しない。
    portal_ctx = build_portal_context(adm, surg, targets, surg_targets, base_date,
                                       generated_at, include_ai_alerts=False,
                                       include_triage=False,
                                       profit_monthly=profit_monthly)

    # ── profit: 粗利データ ──
    profit_section = None
    if profit_monthly is not None and len(profit_monthly) > 0:
        try:
            p_kpi = build_profit_kpi(profit_monthly)
            p_chart = build_profit_chart_data(profit_monthly)
            from .profit import get_latest_month_summary
            p_latest = get_latest_month_summary(profit_monthly)
            p_ranking = []
            from .config import (
                STD_BIZ_DAYS_PER_MONTH as _STD_BD,
                STD_CAL_DAYS_PER_MONTH as _STD_CD,
            )
            has_bd = "外来粗利" in p_latest.columns
            for i, r in p_latest.iterrows():
                st = status_display(r["達成率"]) if pd.notna(r["達成率"]) else status_display(0)
                biz = r.get("当月営業日数")
                cal = r.get("当月暦日数") if has_bd else None
                dp = (round(float(r["粗利"]) / float(biz) / 10, 1)
                      if pd.notna(r["粗利"]) and pd.notna(biz) and float(biz) > 0 else None)
                dt = (round(float(r["月次目標"]) / _STD_BD / 10, 1)
                      if pd.notna(r["月次目標"]) and float(r["月次目標"]) > 0 else None)
                entry = {
                    "rank": i + 1,
                    "name": r["診療科名"],
                    "actual": round(float(r["粗利"]) / 1000, 1) if pd.notna(r["粗利"]) else 0,
                    "target": round(float(r["月次目標"]) / 1000, 1) if pd.notna(r["月次目標"]) else None,
                    "rate": float(r["達成率"]) if pd.notna(r["達成率"]) else None,
                    "daily_pace": dp,                                      # 万円/営業日
                    "daily_target": dt,                                    # 万円/営業日
                    "biz_days": int(biz) if pd.notna(biz) else None,
                    "mom": round(float(r["前月比"]) / 1000, 1) if pd.notna(r.get("前月比")) else None,
                    "status": st["css"],
                    "shape": st["shape"],
                    "text": st["text"],
                }
                if has_bd:
                    g_val = r.get("外来粗利")
                    n_val = r.get("入院粗利")
                    g_tgt = r.get("外来目標")
                    n_tgt = r.get("入院目標")
                    entry["cal_days"] = int(cal) if pd.notna(cal) else None
                    entry["gairai_daily_pace"]   = (round(float(g_val) / float(biz) / 10, 1)
                                                     if pd.notna(g_val) and pd.notna(biz) and float(biz) > 0 else None)
                    entry["nyuin_daily_pace"]    = (round(float(n_val) / float(cal) / 10, 1)
                                                     if pd.notna(n_val) and pd.notna(cal) and float(cal) > 0 else None)
                    entry["gairai_daily_target"] = (round(float(g_tgt) / _STD_BD / 10, 1)
                                                     if pd.notna(g_tgt) and float(g_tgt) > 0 else None)
                    entry["nyuin_daily_target"]  = (round(float(n_tgt) / _STD_CD / 10, 1)
                                                     if pd.notna(n_tgt) and float(n_tgt) > 0 else None)
                p_ranking.append(entry)
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

    # 粗利推計は全ドライバー(adm/surg)が揃う最終日で行う（片方が欠けた最終日での
    # 過小推計を避け、PLレポートの G と同一日に揃える）。在院/新入院など他KPIは
    # base_date（adm最終）のまま。
    profit_base_date = last_complete_driver_date(adm, surg) or base_date

    # ── profit_estimate: 直近30日 粗利推計（2式・手術入外分離） ──
    profit_estimate_section = None
    if profit_section is not None and profit_breakdown is not None and len(profit_breakdown) > 0:
        try:
            profit_estimate_section = build_profit_estimate_payload(
                profit_breakdown=profit_breakdown,
                adm=adm, surg=surg,
                base_date=profit_base_date,
                rolling_days=30,
            )
        except Exception:
            profit_estimate_section = None

    # ── profit_hybrid: 術式NNLS + 件数OLS + admission 加算層のハイブリッド月次推計 ──
    profit_hybrid_section = None
    profit_g_calibrated = None   # PLレポートと同じ G（MTDブレンド × recency補正, 百万円）
    if profit_breakdown is not None and len(profit_breakdown) > 0 and surg is not None:
        try:
            profit_hybrid_section = build_profit_hybrid_payload(
                profit_breakdown=profit_breakdown,
                surg=surg,
                base_date=profit_base_date,
                adm=adm,
            )
        except Exception:
            profit_hybrid_section = None

    # 月末見込み G を確定（KPI・棒・折れ線で共通の数値にする）。recency 補正を適用し、
    # チャート表示用の「最終月末見込み」系列(values_final_*) を hospital_series に注入。
    if profit_hybrid_section:
        try:
            cal = apply_recency_calibration(
                profit_hybrid_section["meta"], profit_breakdown, surg, adm,
                profit_base_date,
            )
            profit_g_calibrated = cal["g_million"]
            fin = blend_and_calibrate_series(
                profit_hybrid_section["hospital_series"], cal["calibration_factor"],
            )
            profit_hybrid_section["hospital_series"].update(fin["series"])
            profit_hybrid_section["meta"].update(fin["latest"])
            # 科別: 病院係数を流用して values_blend_* → values_final_* に変換。
            # 同一スカラー係数なので Σ科別 final = 病院 final の整合が保たれる。
            # blend は最終 JSON から削除（pop）しサイズ最小化（+3配列/科のみ）。
            cf = cal["calibration_factor"]
            for ser in profit_hybrid_section.get("series_by_dept", {}).values():
                for suf in ("total", "gairai", "nyuin"):
                    bl = ser.pop(f"values_blend_{suf}", None)
                    if bl is not None:
                        ser[f"values_final_{suf}"] = [
                            round(v * cf, 2) if v is not None else None for v in bl
                        ]
        except Exception:
            profit_g_calibrated = None

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
            "balance": balance_kpi,
        },
        "attention": portal_ctx["attention"],
        "improvement": portal_ctx["improvement"],
        "perf": perf,
        "trend": trend,
        "drill": drill,
        "charts": {
            "occupancy_heatmap": heatmap,
            "discharge_dow_heatmap": discharge_heatmap,
            "discharge_dow_heatmap_dept": discharge_heatmap_dept,
            "dow_heatmaps": dow_heatmaps,
            "dow_unit_detail": dow_unit_detail,
            "weekend_leveling": weekend_leveling,
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

    # 直近30日推計を全科 + 診療科別に attach
    if profit_estimate_section:
        data["profit_estimate"] = {
            "meta":        profit_estimate_section["meta"],
            "latest":      profit_estimate_section["latest"].get("_hospital", {}),
            "series":      profit_estimate_section["series"].get("_hospital", {}),
            "fit_quality": profit_estimate_section["fit_quality"],
        }
        est_latest = profit_estimate_section["latest"]
        est_series = profit_estimate_section["series"]
        est_fit    = profit_estimate_section["fit_quality"]
        for dname, drill_entry in data["drill"].items():
            if drill_entry.get("ward_extra"):
                continue
            if dname in est_latest:
                drill_entry["profit_estimate"] = {
                    "latest":      est_latest[dname],
                    "series":      est_series.get(dname, {}),
                    "fit_quality": est_fit.get(dname, {}),
                    "meta":        profit_estimate_section["meta"],
                }

    # ハイブリッド月次推計 + 日次系列を全科 + 診療科別に attach
    if profit_hybrid_section:
        data["profit_hybrid"] = {
            "meta":            profit_hybrid_section["meta"],
            "hospital_total":  profit_hybrid_section.get("hospital_total"),
            "hospital_series": profit_hybrid_section.get("hospital_series"),
        }
        hy_by_dept = profit_hybrid_section.get("by_dept", {})
        hy_series_by_dept = profit_hybrid_section.get("series_by_dept", {})
        for dname, drill_entry in data["drill"].items():
            if drill_entry.get("ward_extra"):
                continue
            if dname in hy_by_dept:
                rec = dict(hy_by_dept[dname])
                if dname in hy_series_by_dept:
                    rec["series"] = hy_series_by_dept[dname]
                drill_entry["profit_hybrid"] = rec

    # 当月予測 (headline 直下の 4 KPI カード)
    try:
        hybrid_meta = profit_hybrid_section.get("meta") if profit_hybrid_section else None
        hybrid_hospital_series = profit_hybrid_section.get("hospital_series") if profit_hybrid_section else None
        # 粗利の月末見込みは KPI・棒・折れ線で共通の G（上で確定済み）を流用。
        data["month_projection"] = build_month_projection_payload(
            adm=adm, surg=surg,
            profit_monthly=profit_monthly,
            profit_hybrid_meta=hybrid_meta,
            profit_hybrid_hospital_series=hybrid_hospital_series,
            base_date=base_date,
            profit_hybrid_g_override=profit_g_calibrated,
        )
    except Exception:
        data["month_projection"] = None

    # 粗利見込みバー/カード用: 確報待ちの前月(5月)＋当月(6月)の月末見込みリスト。
    # 値は当月基準で既に算出済みの日次 values_final_total から抽出（第2 payload 不要）。
    profit_pending, profit_driver_month = _profit_pending_months(
        profit_monthly, profit_base_date)
    data["profit_projections"] = None
    if profit_hybrid_section:
        try:
            data["profit_projections"] = _build_profit_projections(
                profit_hybrid_section.get("hospital_series"), profit_monthly,
                profit_pending, profit_base_date, profit_driver_month,
            )
        except Exception:
            data["profit_projections"] = None

    # 当月予測 (診療科ごと, dept.html dashView 用)
    hy_series_by_dept = (profit_hybrid_section.get("series_by_dept", {})
                         if profit_hybrid_section else {})
    nadm_dept_tgt = targets.get("new_admission", {}).get("dept", {})
    inp_dept_tgt = targets.get("inpatient", {}).get("dept", {})
    for dname, drill_entry in data["drill"].items():
        if drill_entry.get("ward_extra"):
            continue
        dept_proj_total = None
        ser = hy_series_by_dept.get(dname)
        # 病院 G と同方式（MTDブレンド×補正）の values_final_total を優先。
        # 無ければ後方互換で values_projection_total。
        proj_key = ("values_final_total"
                    if (ser and ser.get("values_final_total"))
                    else "values_projection_total")
        if ser and ser.get(proj_key):
            tail = [v for v in ser[proj_key] if v is not None]
            if tail:
                dept_proj_total = tail[-1]
        try:
            drill_entry["month_projection"] = build_month_projection_payload(
                adm=adm, surg=surg,
                profit_monthly=profit_monthly,
                profit_hybrid_meta=None,
                profit_hybrid_hospital_series=None,
                base_date=base_date,
                dept=dname,
                dept_inpatient_target=inp_dept_tgt.get(dname),
                dept_admission_weekly=nadm_dept_tgt.get(dname),
                dept_operation_weekly=surg_targets.get(dname),
                dept_profit_projection_total=dept_proj_total,
            )
        except Exception:
            drill_entry["month_projection"] = None

        # 科別の前月(確報待ち)＋当月 粗利見込みリスト（科別 values_final_total から抽出）
        try:
            drill_entry["profit_projections"] = _build_profit_projections(
                ser, profit_monthly, profit_pending,
                profit_base_date, profit_driver_month, dept=dname,
            ) if ser else None
        except Exception:
            drill_entry["profit_projections"] = None

    return json.dumps(data, ensure_ascii=False, default=_json_safe)
