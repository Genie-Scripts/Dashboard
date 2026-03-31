"""
html_builder.py — KPI・ランキングHTMLビルダー
Jinja2テンプレートに渡すデータ整形 & 部分HTMLスニペット生成
"""

import json
import pandas as pd
from datetime import datetime
from typing import Optional
from .config import WARD_NAMES


# ────────────────────────────────────────────────────
# 達成状態判定
# ────────────────────────────────────────────────────

def _status(achievement: Optional[float]) -> str:
    """達成率 → ステータスクラス (ok/warn/ng/neutral)
    
    判定基準（仕様書 §9 準拠）:
        105% 以上 → ok   （達成・超過）
         95% 以上 → warn  （注意）
         95% 未満 → ng    （未達）
    """
    if achievement is None:
        return "neutral"
    if achievement >= 105:
        return "ok"
    elif achievement >= 95:
        return "warn"
    return "ng"


def _delta_html(val: Optional[float], suffix: str = "", positive_is_good: bool = True) -> str:
    """前週比などのデルタ表示HTML"""
    if val is None:
        return '<span style="color:var(--muted)">—</span>'
    cls = "up" if (val >= 0) == positive_is_good else "down"
    sign = "+" if val >= 0 else ""
    return f'<span class="{cls}">{sign}{val:.0f}{suffix}</span>'


def _badge_html(text: str, status: str) -> str:
    cls_map = {"ok": "badge-ok", "warn": "badge-warn", "ng": "badge-ng"}
    cls = cls_map.get(status, "")
    return f'<span class="badge {cls}">{text}</span>'


def _badge_label(status: str) -> str:
    """ステータス → 日本語バッジラベル"""
    return {"ok": "達成", "warn": "注意", "ng": "未達"}.get(status, "—")


def _gap_html(gap: Optional[float], unit: str = "", positive_is_good: bool = True) -> str:
    """目標との差分表示（達成時は正、未達は負）"""
    if gap is None:
        return "目標未設定"
    if gap >= 0:
        return f'<span class="gap-positive">目標 +{gap:,.1f}{unit}</span>'
    else:
        return f'<span class="gap-negative">目標まで {gap:,.1f}{unit}</span>'


def _progress_pct(achievement: Optional[float], cap: float = 120.0) -> float:
    if achievement is None:
        return 0.0
    return min(achievement, cap) / cap * 100


# ────────────────────────────────────────────────────
# KPIカード データ構築
# ────────────────────────────────────────────────────

def _gap_pair(actual, target, unit: str) -> tuple:
    """
    目標差分テキストと CSS クラスを返す。
    Returns: (gap_text: str, gap_css: str)
      gap_css は 'gap-pos' / 'gap-neg' / 'gap-neu' のいずれか
    """
    if target is None or actual is None:
        return "目標未設定", "gap-neu"
    diff = actual - target
    if diff >= 0:
        return f"目標 +{diff:,.1f}{unit}", "gap-pos"
    else:
        return f"目標まで {diff:,.1f}{unit}", "gap-neg"


# 後方互換ラッパー（旧コード参照箇所向け）
def _gap_text(actual, target, unit: str, positive_is_good: bool = True) -> str:
    return _gap_pair(actual, target, unit)[0]


def _badge_label(status: str) -> str:
    return {"ok": "達成", "warn": "注意", "ng": "未達", "neutral": "—"}.get(status, "—")


def build_kpi_card_data(kpi: dict) -> list:
    """
    build_kpi_summary() の出力から判断カード用データリストを返す。

    新構造（仕様書 §7）:
        id, label, period   … カード識別・タイトル・時間軸
        value, unit          … メイン値
        gap                  … 目標差分テキスト（主メッセージ）
        gap_css              … 'gap-pos' / 'gap-neg' / 'gap-neu'（CSS クラス）
        achievement          … 達成率 float | None
        status               … ok / warn / ng / neutral
        badge                … 達成 / 注意 / 未達 テキスト
        meta                 … [{lbl, val}, ...] 参考値（目標・MA・前週比等）
        progress             … プログレスバー幅(%)
    """
    inp  = kpi["inpatient"]
    nadm = kpi["new_admission"]
    surg = kpi["surgery"]
    dis  = kpi["discharge"]

    cards = []

    # ── 在院患者数 ─────────────────────────────────────
    inp_ach = inp["achievement"]
    inp_st  = _status(inp_ach)
    inp_gap, inp_gap_css = _gap_pair(inp["value"], inp["target"], "人")
    _base_date    = kpi.get("base_date")
    _inp_date_lbl = _base_date.strftime("%-m/%-d") if _base_date is not None else "昨日"
    cards.append({
        "id":          "inpatient",
        "label":       "在院患者数",
        "period":      f"{_inp_date_lbl}時点",
        "value":       f"{inp['value']:,}",
        "unit":        "人",
        "gap":         inp_gap,
        "gap_css":     inp_gap_css,
        "achievement": inp_ach,
        "status":      inp_st,
        "badge":       _badge_label(inp_st),
        "progress":    _progress_pct(inp_ach),
        "meta": [
            {"lbl": "目標",     "val": f"{inp['target']:,}人"},
            {"lbl": "7日MA",    "val": f"{inp['ma7']:.1f}人" if inp["ma7"] else "—"},
            {"lbl": "前週比",   "val": _delta_html(inp.get("wow"), "人")},
            {"lbl": "達成率",   "val": f"{inp_ach:.1f}%" if inp_ach else "—"},
        ],
    })

    # ── 新入院患者数（直近7日累計）────────────────────────
    r7_total    = nadm.get("rolling7_total", nadm.get("weekly_total", 0))
    r7_target   = nadm.get("rolling7_target", nadm.get("weekly_target"))
    r7_prog     = nadm.get("rolling7_progress", nadm.get("weekly_progress"))
    r7_st       = _status(r7_prog)
    r7_gap, r7_gap_css = _gap_pair(r7_total, r7_target, "人")
    r7_vs_365w  = nadm.get("rolling7_vs_365w")        # 直近7日 vs 365日週平均
    avg_365w    = nadm.get("nadm_365_weekly_avg")
    cards.append({
        "id":          "new_admission_7d",
        "label":       "新入院患者数",
        "period":      "直近7日累計",
        "value":       f"{r7_total:,}",
        "unit":        "人",
        "gap":         r7_gap,
        "gap_css":     r7_gap_css,
        "achievement": r7_prog,
        "status":      r7_st,
        "badge":       _badge_label(r7_st),
        "progress":    _progress_pct(r7_prog),
        "meta": [
            {"lbl": "7日目標",       "val": f"{int(r7_target):,}人" if r7_target else "—"},
            {"lbl": "365日週平均",   "val": f"{avg_365w:.1f}人" if avg_365w else "—"},
            {"lbl": "vs 365日週平均","val": _delta_html(r7_vs_365w, "人")},
            {"lbl": "達成率",        "val": f"{r7_prog:.1f}%" if r7_prog else "—"},
        ],
    })

    # ── 新入院患者数（昨日）──────────────────────────────
    base_date    = kpi.get("base_date")
    date_label   = base_date.strftime("%-m/%-d") if base_date is not None else "昨日"
    is_weekday   = nadm.get("daily_is_weekday", True)
    day_type_lbl = "平日" if is_weekday else "休日"
    d_value      = nadm.get("daily_value", nadm.get("value", 0))
    # daily_target: metrics側でセット済みだが、旧データ互換のためフォールバックを追加
    d_target = nadm.get("daily_target") or (80 if is_weekday else 40)
    d_prog       = nadm.get("daily_progress") or achievement_rate(d_value, d_target)
    d_st         = _status(d_prog)
    d_gap, d_gap_css = _gap_pair(d_value, d_target, "人")
    d_vs_365     = nadm.get("daily_vs_365")       # 昨日 vs 365日同区分平均
    avg_365d     = nadm.get("daily_365_avg")
    cards.append({
        "id":          "new_admission_daily",
        "label":       "新入院患者数",
        "period":      f"昨日（{date_label}）",
        "value":       f"{d_value:,}",
        "unit":        "人",
        "gap":         d_gap,
        "gap_css":     d_gap_css,
        "achievement": d_prog,
        "status":      d_st,
        "badge":       _badge_label(d_st),
        "progress":    _progress_pct(d_prog),
        "meta": [
            {"lbl": f"{day_type_lbl}目標",     "val": f"{int(d_target):,}人" if d_target else "—"},
            {"lbl": f"365日{day_type_lbl}平均", "val": f"{avg_365d:.1f}人" if avg_365d else "—"},
            {"lbl": f"vs 365日{day_type_lbl}平均","val": _delta_html(d_vs_365, "人")},
            {"lbl": "達成率",                  "val": f"{d_prog:.1f}%" if d_prog else "—"},
        ],
    })

    # ── 全身麻酔手術 ───────────────────────────────────
    ga_avg    = surg.get("ga_rolling_avg")
    ga_target = surg.get("ga_target", 21)
    ga_ach    = surg.get("ga_achievement")
    ga_st     = _status(ga_ach)
    ga_gap, ga_gap_css = (_gap_pair(ga_avg, ga_target, "件/日")
                          if ga_avg is not None else ("目標未設定", "gap-neu"))
    last_cnt  = surg.get("ga_last_biz_count")
    yr_avg    = surg.get("ga_fy_biz_avg")
    cards.append({
        "id":          "surgery",
        "label":       "全身麻酔手術",
        "period":      "直近7平日平均",
        "value":       f"{ga_avg:.1f}" if ga_avg is not None else "—",
        "unit":        "件/日",
        "gap":         ga_gap,
        "gap_css":     ga_gap_css,
        "achievement": ga_ach,
        "status":      ga_st,
        "badge":       _badge_label(ga_st),
        "progress":    _progress_pct(ga_ach),
        "meta": [
            {"lbl": "平日目標",     "val": f"{ga_target}件/日"},
            {"lbl": "直近平日実績", "val": f"{last_cnt}件" if last_cnt is not None else "—"},
            {"lbl": "年度平均",     "val": f"{yr_avg:.1f}件/日" if yr_avg else "—"},
            {"lbl": "今週累計",     "val": f"{surg.get('weekly_ga', 0):,}件"},
        ],
    })

    return cards


# ────────────────────────────────────────────────────
# ハイライト文章生成
# ────────────────────────────────────────────────────

def build_highlight_text(kpi: dict) -> str:
    """
    医師版 Role Brief を生成する。
    数値の再掲ではなく「判断文」3項目（仕様書 §6）。
    """
    inp  = kpi["inpatient"]
    nadm = kpi["new_admission"]
    surg = kpi["surgery"]

    # 在院
    inp_ach  = inp.get("achievement") or 0
    inp_tgt  = inp.get("target", 0)
    inp_val  = inp.get("value", 0)
    inp_diff = inp_val - inp_tgt
    inp_st   = _status(inp_ach)
    if inp_st == "ok":
        inp_msg = f"在院患者数は目標を達成（+{inp_diff:+.0f}人）"
    elif inp_st == "warn":
        inp_msg = f"在院患者数は目標に対し注意（{inp_diff:+.0f}人）"
    else:
        inp_msg = f"在院患者数が目標未達（{inp_diff:+.0f}人）"

    # 新入院（直近7日累計）
    r7_prog  = nadm.get("rolling7_progress") or nadm.get("weekly_progress") or 0
    r7_total = nadm.get("rolling7_total", nadm.get("weekly_total", 0))
    r7_tgt   = nadm.get("rolling7_target", nadm.get("weekly_target")) or 0
    nadm_st  = _status(r7_prog)
    if nadm_st == "ok":
        nadm_msg = f"新入院は直近7日目標に対し達成（7日累計{r7_total}人）"
    elif nadm_st == "warn":
        nadm_msg = f"新入院の進捗は直近7日目標に対しやや遅れ（{r7_total}/{int(r7_tgt)}人）"
    else:
        nadm_msg = f"新入院の進捗が直近7日目標に対し遅れ（{r7_total}/{int(r7_tgt)}人）"

    # 全身麻酔（7平日平均 vs 目標）
    ga_avg    = surg.get("ga_rolling_avg")
    ga_tgt    = surg.get("ga_target", 21)
    ga_ach    = surg.get("ga_achievement")
    ga_st     = _status(ga_ach)
    if ga_avg is None:
        surg_msg = "全麻: データなし"
    elif ga_st == "ok":
        surg_msg = f"全麻は平日目標{ga_tgt}件を達成（直近7平日平均{ga_avg:.1f}件）"
    elif ga_st == "warn":
        surg_msg = f"全麻は平日目標{ga_tgt}件に対しやや不足（直近7平日平均{ga_avg:.1f}件）"
    else:
        surg_msg = f"全麻が平日目標{ga_tgt}件に未達（直近7平日平均{ga_avg:.1f}件）"

    items = [
        {"status": inp_st,  "text": inp_msg},
        {"status": nadm_st, "text": nadm_msg},
        {"status": ga_st,   "text": surg_msg},
    ]
    # テンプレート側でリスト展開できるようにJSONを返す（後方互換でHTMLも）
    import json as _json
    return _json.dumps(items, ensure_ascii=False)


# ────────────────────────────────────────────────────
# ランキング表データ構築
# ────────────────────────────────────────────────────

def build_ranking_data(ranking_df: pd.DataFrame,
                        metric: str = "inpatient") -> list:
    """
    ランキングDFをJS埋め込み用リストに変換

    Returns:
        [{"rank": 1, "name": "総合内科", "actual": 97, "target": 85,
          "achievement": 114.1, "status": "ok"}, ...]
    """
    if len(ranking_df) == 0:
        return []

    rows = []
    name_col = "診療科" if "診療科" in ranking_df.columns else "病棟名"
    target_col = "週目標" if metric == "surgery" else "目標"

    for _, row in ranking_df.iterrows():
        ach = row.get("達成率")
        actual = row.get("実績", 0)
        target = row.get(target_col)
        rows.append({
            "rank": int(row.get("順位", 0)),
            "name": str(row.get(name_col, "")),
            "actual": int(actual) if pd.notna(actual) else 0,
            "target": round(float(target), 1) if pd.notna(target) else None,
            "achievement": round(float(ach), 1) if pd.notna(ach) else None,
            "status": _status(ach if pd.notna(ach) else None),
        })
    return rows


# ────────────────────────────────────────────────────
# インサイト（注目診療科）生成
# ────────────────────────────────────────────────────

def build_insights(ranking_df: pd.DataFrame, top_n: int = 3,
                    bottom_n: int = 2) -> dict:
    """注目診療科（上位・下位）"""
    if len(ranking_df) == 0:
        return {"top": [], "bottom": []}

    name_col = "診療科" if "診療科" in ranking_df.columns else "病棟名"
    valid = ranking_df.dropna(subset=["達成率"])

    top = []
    for _, row in valid.head(top_n).iterrows():
        ach = float(row["達成率"])
        top.append({
            "name": str(row[name_col]),
            "achievement": ach,
            "status": _status(ach),
            "icon": "🏆" if ach >= 110 else ("📈" if ach >= 100 else "✅"),
        })

    bottom = []
    for _, row in valid.tail(bottom_n).iterrows():
        ach = float(row["達成率"])
        bottom.append({
            "name": str(row[name_col]),
            "achievement": ach,
            "status": _status(ach),
            "icon": "⚠️" if ach >= 70 else "🔴",
        })

    return {"top": top, "bottom": bottom}


# ────────────────────────────────────────────────────
# テンプレート用コンテキスト全体構築
# ────────────────────────────────────────────────────

def build_nurse_kpi_card_data(kpi: dict) -> list:
    """
    看護師タブ用KPIカード（在院・新入院・退院・出入り負荷）。
    仕様書 §8-2: 4枚目は退院関連件数（昨日）。
    医師カードと同じ判断カード構造（gap/badge/period/meta）。
    """
    inp  = kpi["inpatient"]
    nadm = kpi["new_admission"]
    dis  = kpi["discharge"]

    load_val = (nadm["value"] + dis["transfer_in"]
                + dis["value"] + dis["transfer_out"])

    inp_ach  = inp["achievement"]
    inp_st   = _status(inp_ach)
    nadm_prog = nadm.get("weekly_progress")
    nadm_st  = _status(nadm_prog)

    cards = []

    # ── 在院患者数 ─────────────────────────────────────
    inp_gap, inp_gap_css = _gap_pair(inp["value"], inp["target"], "人")
    cards.append({
        "id":          "ward_inpatient",
        "label":       "在院患者数",
        "period":      "昨日時点",
        "value":       f"{inp['value']:,}",
        "unit":        "人",
        "gap":         inp_gap,
        "gap_css":     inp_gap_css,
        "achievement": inp_ach,
        "status":      inp_st,
        "badge":       _badge_label(inp_st),
        "progress":    _progress_pct(inp_ach),
        "meta": [
            {"lbl": "目標",   "val": f"{inp['target']:,}人"},
            {"lbl": "7日MA",  "val": f"{inp['ma7']:.1f}人" if inp["ma7"] else "—"},
            {"lbl": "前週比", "val": _delta_html(inp.get("wow"), "人")},
            {"lbl": "達成率", "val": f"{inp_ach:.1f}%" if inp_ach else "—"},
        ],
    })

    # ── 新入院患者数 ───────────────────────────────────
    wk_total  = nadm["weekly_total"]
    wk_target = nadm.get("weekly_target")
    nadm_gap, nadm_gap_css = _gap_pair(wk_total, wk_target, "人")
    cards.append({
        "id":          "ward_newadm",
        "label":       "新入院患者数",
        "period":      "今週累計",
        "value":       f"{wk_total:,}",
        "unit":        "人",
        "gap":         nadm_gap,
        "gap_css":     nadm_gap_css,
        "achievement": nadm_prog,
        "status":      nadm_st,
        "badge":       _badge_label(nadm_st),
        "progress":    _progress_pct(nadm_prog),
        "meta": [
            {"lbl": "週目標",   "val": f"{int(wk_target):,}人" if wk_target else "—"},
            {"lbl": "昨日",     "val": f"{nadm['value']:,}人"},
            {"lbl": "うち緊急", "val": f"{nadm['emergency']:,}人"},
            {"lbl": "前週比",   "val": _delta_html(nadm.get("wow"), "人")},
        ],
    })

    # ── 退院患者数（昨日）─────────────────────────────
    in_out_diff = nadm["value"] - dis["value"]
    diff_sign   = "+" if in_out_diff >= 0 else ""
    load_gap_css = "gap-neg" if in_out_diff > 5 else ("gap-pos" if in_out_diff < -5 else "gap-neu")
    cards.append({
        "id":          "ward_discharge",
        "label":       "退院関連件数",
        "period":      "昨日",
        "value":       f"{dis['value']:,}",
        "unit":        "人",
        "gap":         f"入退差 {diff_sign}{in_out_diff}人",
        "gap_css":     load_gap_css,
        "achievement": None,
        "status":      "neutral",
        "badge":       "—",
        "progress":    0,
        "meta": [
            {"lbl": "転入",   "val": f"{dis['transfer_in']:,}人"},
            {"lbl": "転出",   "val": f"{dis['transfer_out']:,}人"},
            {"lbl": "入退差", "val": f"{diff_sign}{in_out_diff}人"},
            {"lbl": "—",      "val": "—"},
        ],
    })

    # ── 出入り負荷（昨日）─────────────────────────────
    cards.append({
        "id":          "ward_load",
        "label":       "出入り負荷",
        "period":      "昨日",
        "value":       f"{load_val:,}",
        "unit":        "件",
        "gap":         "—",
        "gap_css":     "gap-neu",
        "achievement": None,
        "status":      "neutral",
        "badge":       "—",
        "progress":    0,
        "meta": [
            {"lbl": "新入院", "val": f"{nadm['value']:,}人"},
            {"lbl": "退院",   "val": f"{dis['value']:,}人"},
            {"lbl": "転入",   "val": f"{dis['transfer_in']:,}人"},
            {"lbl": "転出",   "val": f"{dis['transfer_out']:,}人"},
        ],
    })

    return cards


def build_ward_highlight_text(kpi: dict) -> str:
    """
    看護師版 Role Brief を生成する。
    病棟運営・入退院負荷に特化した判断文3項目（仕様書 §6-1 看護師版）。
    """
    inp  = kpi["inpatient"]
    nadm = kpi["new_admission"]
    dis  = kpi["discharge"]

    import json as _json

    # 在院状況
    inp_ach  = inp.get("achievement") or 0
    inp_val  = inp.get("value", 0)
    inp_tgt  = inp.get("target", 0)
    inp_diff = inp_val - inp_tgt
    inp_st   = _status(inp_ach)
    if inp_st == "ok":
        inp_msg = f"病棟稼働は目標を達成（在院+{inp_diff:+.0f}人）"
    elif inp_st == "warn":
        inp_msg = f"病棟稼働はやや余裕あり（在院{inp_diff:+.0f}人）"
    else:
        inp_msg = f"病棟稼働が目標を下回っている（在院{inp_diff:+.0f}人）"

    # 入退院負荷
    in_out = nadm["value"] - dis["value"]
    sign   = "+" if in_out >= 0 else ""
    if abs(in_out) <= 5:
        load_msg = f"入退院バランスは均衡（入退差{sign}{in_out}人）"
    elif in_out > 5:
        load_msg = f"入院超過により在院増加傾向（入退差{sign}{in_out}人）"
    else:
        load_msg = f"退院超過により在院減少傾向（入退差{sign}{in_out}人）"
    load_st = "warn" if abs(in_out) > 10 else "neutral"

    # 週進捗
    wk_prog  = nadm.get("weekly_progress") or 0
    wk_total = nadm.get("weekly_total", 0)
    wk_tgt   = nadm.get("weekly_target") or 0
    nadm_st  = _status(wk_prog)
    if nadm_st == "ok":
        nadm_msg = f"新入院は週目標達成ペース（週累計{wk_total}人）"
    elif nadm_st == "warn":
        nadm_msg = f"新入院は週目標にやや遅れ（週{wk_total}/{int(wk_tgt)}人）"
    else:
        nadm_msg = f"新入院が週目標に対し遅れている（週{wk_total}/{int(wk_tgt)}人）"

    items = [
        {"status": inp_st,   "text": inp_msg},
        {"status": load_st,  "text": load_msg},
        {"status": nadm_st,  "text": nadm_msg},
    ]
    return _json.dumps(items, ensure_ascii=False)


def build_template_context(kpi: dict,
                            dept_ranking_inp: pd.DataFrame,
                            dept_ranking_nadm: pd.DataFrame,
                            surgery_ranking: pd.DataFrame,
                            chart_data_json: str,
                            all_depts: list,
                            ward_ranking_inp: Optional[pd.DataFrame] = None,
                            ward_ranking_nadm: Optional[pd.DataFrame] = None,
                            generated_at: Optional[datetime] = None) -> dict:
    """
    Jinja2テンプレートに渡す全コンテキストを組み立てる。
    Phase 3: 看護師タブ用データを追加。
    """
    if generated_at is None:
        generated_at = datetime.now()

    base_date = kpi["base_date"]

    kpi_cards = build_kpi_card_data(kpi)
    highlight = build_highlight_text(kpi)

    # 看護師タブ用
    nurse_kpi_cards = build_nurse_kpi_card_data(kpi)
    nurse_highlight = build_ward_highlight_text(kpi)

    rank_inp = build_ranking_data(dept_ranking_inp, "inpatient")
    rank_nadm = build_ranking_data(dept_ranking_nadm, "new_admission")
    rank_surg = build_ranking_data(surgery_ranking, "surgery")

    # 病棟ランキング（看護師タブ）
    rank_ward_inp  = build_ranking_data(ward_ranking_inp,  "inpatient")  if ward_ranking_inp  is not None else []
    rank_ward_nadm = build_ranking_data(ward_ranking_nadm, "new_admission") if ward_ranking_nadm is not None else []

    insights_inp = build_insights(dept_ranking_inp)
    insights_ward = build_insights(ward_ranking_inp) if ward_ranking_inp is not None else {"top": [], "bottom": []}

    return {
        # メタ
        "generated_at": generated_at.strftime("%Y/%m/%d %H:%M"),
        "base_date": base_date.strftime("%Y/%m/%d") if hasattr(base_date, "strftime") else str(base_date),
        "base_date_raw": base_date.strftime("%Y-%m-%d") if hasattr(base_date, "strftime") else str(base_date),
        # 医師タブ
        "highlight": highlight,
        "kpi_cards": kpi_cards,
        "kpi_cards_json": json.dumps(kpi_cards, ensure_ascii=False, default=str),
        "rank_inp_json": json.dumps(rank_inp, ensure_ascii=False),
        "rank_nadm_json": json.dumps(rank_nadm, ensure_ascii=False),
        "rank_surg_json": json.dumps(rank_surg, ensure_ascii=False),
        "insights": insights_inp,
        # 看護師タブ
        "nurse_highlight": nurse_highlight,
        "nurse_kpi_cards": nurse_kpi_cards,
        "nurse_kpi_cards_json": json.dumps(nurse_kpi_cards, ensure_ascii=False, default=str),
        "rank_ward_inp_json":  json.dumps(rank_ward_inp,  ensure_ascii=False),
        "rank_ward_nadm_json": json.dumps(rank_ward_nadm, ensure_ascii=False),
        "insights_ward": insights_ward,
        # グラフデータ（医師・看護師共用）
        "chart_data_json": chart_data_json,
        # 診療科リスト（プルダウン用）
        "all_depts": all_depts,
        "all_depts_json": json.dumps(all_depts, ensure_ascii=False),
    }


def build_doctor_context(kpi: dict,
                          dept_ranking_inp: pd.DataFrame,
                          dept_ranking_nadm: pd.DataFrame,
                          surgery_ranking: pd.DataFrame,
                          doctor_watch_rank: list,
                          doctor_gap_rank: dict,
                          chart_data_json: str,
                          all_depts: list,
                          doctor_chart_json: str = "{}",
                          generated_at: Optional[datetime] = None) -> dict:
    """
    doctor.html 用テンプレートコンテキストを構築する。
    """
    if generated_at is None:
        generated_at = datetime.now()

    base_date   = kpi["base_date"]
    kpi_cards   = build_kpi_card_data(kpi)
    highlight   = build_highlight_text(kpi)
    rank_inp    = build_ranking_data(dept_ranking_inp,  "inpatient")
    rank_nadm   = build_ranking_data(dept_ranking_nadm, "new_admission")
    rank_surg   = build_ranking_data(surgery_ranking,   "surgery")

    return {
        # メタ
        "generated_at":     generated_at.strftime("%Y/%m/%d %H:%M"),
        "base_date":        base_date.strftime("%Y/%m/%d") if hasattr(base_date, "strftime") else str(base_date),
        "base_date_raw":    base_date.strftime("%Y-%m-%d") if hasattr(base_date, "strftime") else str(base_date),
        "page_role":        "doctor",
        # KPI
        "highlight":        highlight,
        "kpi_cards":        kpi_cards,
        "kpi_cards_json":   json.dumps(kpi_cards, ensure_ascii=False, default=str),
        # 既存ランキング（チャート用）
        "rank_inp_json":    json.dumps(rank_inp,  ensure_ascii=False),
        "rank_nadm_json":   json.dumps(rank_nadm, ensure_ascii=False),
        "rank_surg_json":   json.dumps(rank_surg, ensure_ascii=False),
        # 新ランキング
        "doctor_watch_json":       json.dumps(doctor_watch_rank, ensure_ascii=False),
        "doctor_surgery_gap_json": json.dumps(doctor_gap_rank.get("surgery_gap", []),   ensure_ascii=False),
        "doctor_nadm_gap_json":    json.dumps(doctor_gap_rank.get("admission_gap", []), ensure_ascii=False),
        # グラフデータ（時系列・フィルタ用）
        "chart_data_json":  chart_data_json,
        # 役割別チャートデータ（Phase 2）
        "doctor_chart_json": doctor_chart_json,
        # 診療科プルダウン
        "all_depts":        all_depts,
        "all_depts_json":   json.dumps(all_depts, ensure_ascii=False),
    }


def build_nurse_context(kpi: dict,
                         ward_ranking_inp: pd.DataFrame,
                         ward_ranking_nadm: pd.DataFrame,
                         nurse_watch_rank: list,
                         nurse_load_rank: list,
                         chart_data_json: str,
                         nurse_chart_json: str = "{}",
                         generated_at: Optional[datetime] = None) -> dict:
    """
    nurse.html 用テンプレートコンテキストを構築する。
    """
    if generated_at is None:
        generated_at = datetime.now()

    base_date   = kpi["base_date"]
    nurse_cards = build_nurse_kpi_card_data(kpi)
    nurse_hl    = build_ward_highlight_text(kpi)
    rank_wi     = build_ranking_data(ward_ranking_inp,  "inpatient")
    rank_wn     = build_ranking_data(ward_ranking_nadm, "new_admission")

    # 病棟リスト {コード: 名称}
    ward_list = {r["ward_code"]: r["ward_name"] for r in nurse_watch_rank}

    return {
        # メタ
        "generated_at":      generated_at.strftime("%Y/%m/%d %H:%M"),
        "base_date":         base_date.strftime("%Y/%m/%d") if hasattr(base_date, "strftime") else str(base_date),
        "base_date_raw":     base_date.strftime("%Y-%m-%d") if hasattr(base_date, "strftime") else str(base_date),
        "page_role":         "nurse",
        # KPI
        "nurse_highlight":   nurse_hl,
        "nurse_kpi_cards":   nurse_cards,
        "nurse_kpi_cards_json": json.dumps(nurse_cards, ensure_ascii=False, default=str),
        # 既存ランキング
        "rank_ward_inp_json":  json.dumps(rank_wi, ensure_ascii=False),
        "rank_ward_nadm_json": json.dumps(rank_wn, ensure_ascii=False),
        # 新ランキング
        "nurse_watch_json":  json.dumps(nurse_watch_rank, ensure_ascii=False),
        "nurse_load_json":   json.dumps(nurse_load_rank,  ensure_ascii=False),
        # 病棟リスト
        "ward_list_json":    json.dumps(ward_list, ensure_ascii=False),
        # グラフデータ（時系列・フィルタ用）
        "chart_data_json":   chart_data_json,
        # 役割別チャートデータ（Phase 2）
        "nurse_chart_json":  nurse_chart_json,
    }


# ────────────────────────────────────────────────────
# 診療科別詳細ページ コンテキスト構築
# ────────────────────────────────────────────────────

def _pct_class(ach, ok_threshold: float = 105) -> str:
    """達成率 → CSSクラス。
    ok_threshold: 達成とみなす閾値（デフォルト105%）
      在院患者数 → 105（上振れも達成評価）
      新入院・全麻・粗利 → 100（多いほど良い指標）
    注意: warn の下限は常に 95%。
    """
    if ach is None or (hasattr(ach, '__class__') and str(type(ach)) == "<class 'float'>" and ach != ach):
        return "neutral"
    try:
        v = float(ach)
        return "ok" if v >= ok_threshold else ("warn" if v >= 95 else "ng")
    except Exception:
        return "neutral"


def _fmt_optional(v, fmt=".1f", fallback="—") -> str:
    if v is None:
        return fallback
    try:
        return format(float(v), fmt)
    except Exception:
        return fallback


def build_dept_report_context(dept_name: str,
                               adm: "pd.DataFrame",
                               surg: "pd.DataFrame",
                               targets: dict,
                               surg_targets: dict,
                               profit_monthly: "pd.DataFrame",
                               base_date: "pd.Timestamp",
                               generated_at: Optional[datetime] = None) -> dict:
    """
    診療科別詳細ページ (dept_report.html) 用コンテキストを構築。

    Args:
        dept_name:       診療科名（例: "整形外科"）
        adm:             前処理済み入院DataFrame
        surg:            前処理済み手術DataFrame
        targets:         build_target_lookup() の出力
        surg_targets:    build_surgery_target_lookup() の出力
        profit_monthly:  build_profit_monthly() の出力
        base_date:       基準日

    Returns:
        Jinja2テンプレート変数の辞書
    """
    import pandas as pd
    from .metrics import (
        daily_inpatient, daily_new_admission,
        rolling7_new_admission, rolling7_surgery,
        achievement_rate,
        build_daily_series, build_surgery_daily_series,
    )
    from .profit import build_profit_chart_data

    if generated_at is None:
        generated_at = datetime.now()

    # ── 在院KPI ──────────────────────────────────────
    inp_kpi  = daily_inpatient(adm, base_date)
    inp_val  = inp_kpi["by_dept"].get(dept_name, 0)
    inp_tgt  = targets.get("inpatient", {}).get("dept", {}).get(dept_name)
    inp_ach  = achievement_rate(inp_val, inp_tgt)
    inp_st   = _pct_class(inp_ach)
    inp_prog = min(float(inp_ach or 0) / 120 * 100, 100)

    # ── 新入院KPI ─────────────────────────────────────
    # 直近7日（ローリング）合計 ÷ 7 = 週平均 → 週目標と比較
    # 曜日バイアスなし（今週月曜から集計だと週初に達成率が低くなる問題を回避）
    r7_nadm  = rolling7_new_admission(adm, base_date)
    nadm_day = daily_new_admission(adm, base_date)
    nadm_val = nadm_day["by_dept"].get(dept_name, 0)
    nadm_r7  = r7_nadm["by_dept"].get(dept_name, 0)
    nadm_wk  = round(nadm_r7 / 7 * 7, 1)   # 直近7日合計（週換算）
    nadm_tgt = targets.get("new_admission", {}).get("dept", {}).get(dept_name)
    nadm_prog_pct = achievement_rate(nadm_wk, nadm_tgt)
    nadm_st  = _pct_class(nadm_prog_pct, ok_threshold=100)

    # ── 手術KPI ───────────────────────────────────────
    # 直近7日（ローリング）合計を週合計として週目標と比較
    r7_surg  = rolling7_surgery(surg, base_date)
    surg_wk  = r7_surg["by_dept"].get(dept_name, 0)
    surg_tgt = surg_targets.get(dept_name)
    surg_ach = achievement_rate(surg_wk, surg_tgt)
    surg_st  = _pct_class(surg_ach, ok_threshold=100)
    surg_prog = min(float(surg_ach or 0) / 120 * 100, 100)
    has_surgery = dept_name in surg_targets

    # ── 粗利KPI ───────────────────────────────────────
    # profit_monthly が空または列未定義の場合に備えて安全にフィルタ
    if profit_monthly is not None and "診療科名" in profit_monthly.columns and len(profit_monthly) > 0:
        dept_profit = profit_monthly[profit_monthly["診療科名"] == dept_name]
    else:
        dept_profit = pd.DataFrame()

    if len(dept_profit) > 0:
        latest_p   = dept_profit.sort_values("月").iloc[-1]
        p_val      = round(float(latest_p["粗利"]) / 1000, 1)   # 百万円
        p_tgt      = round(float(latest_p["月次目標"]) / 1000, 1) if pd.notna(latest_p["月次目標"]) else None
        p_ach      = float(latest_p["達成率"]) if pd.notna(latest_p["達成率"]) else None
        p_mom_raw  = latest_p.get("前月比")
        p_mom      = round(float(p_mom_raw) / 1000, 1) if pd.notna(p_mom_raw) else None
        p_month    = latest_p["月"].strftime("%Y-%m")
        p_st       = _pct_class(p_ach, ok_threshold=100)
        p_prog     = min(float(p_ach or 0) / 120 * 100, 100)
    else:
        p_val = p_tgt = p_ach = p_mom = None
        p_month = "—"
        p_st = "neutral"
        p_prog = 0

    mom_html = "—"
    if p_mom is not None:
        sign = "+" if p_mom >= 0 else ""
        cls  = "up" if p_mom >= 0 else "down"
        mom_html = f'<span class="{cls}">{sign}{p_mom}M</span>'

    # ── 在院ページ全体達成率（ページタイトル用） ──────
    dept_ach   = inp_ach
    dept_status = inp_st

    # ── 時系列データ（JS埋め込み用） ─────────────────
    s_inp  = build_daily_series(adm, "在院患者数",  group_col="診療科名", group_val=dept_name)
    s_nadm = build_daily_series(adm, "新入院患者数", group_col="診療科名", group_val=dept_name)
    s_ga   = build_surgery_daily_series(surg, ga_only=True, dept=dept_name)

    # 診療科別全麻の週次集計（週目標対比用）
    if len(s_ga) > 0:
        _wdf = s_ga.copy()
        _wdf["週開始"] = _wdf["日付"] - pd.to_timedelta(_wdf["日付"].dt.weekday, unit="D")
        _wk = _wdf.groupby("週開始")["値"].sum().reset_index()
        _wk.columns = ["日付", "値"]
        s_ga_weekly_dates  = [d.strftime("%Y-%m-%d") for d in _wk["日付"]]
        s_ga_weekly_values = [int(v) for v in _wk["値"]]
    else:
        s_ga_weekly_dates  = []
        s_ga_weekly_values = []

    def _s(df):
        return {
            "dates":  [d.strftime("%Y-%m-%d") for d in df["日付"]],
            "values": [float(v) if pd.notna(v) else None for v in df["値"]],
        }

    series = {
        "inpatient":         _s(s_inp),
        "new_admission":     _s(s_nadm),
        "surgery_ga":        _s(s_ga),
        # 診療科別全麻は週次集計で目標対比（日次グラフは単位不一致のため）
        "surgery_ga_weekly": {
            "dates":  s_ga_weekly_dates,
            "values": s_ga_weekly_values,
        },
        "targets": {
            "inpatient":            inp_tgt,
            "new_admission_weekly": nadm_tgt,
            "surgery_ga_weekly":    surg_tgt,   # 診療科の目標は週単位
        },
    }

    # ── 粗利グラフデータ ──────────────────────────────
    if (profit_monthly is not None
            and "診療科名" in profit_monthly.columns
            and len(profit_monthly) > 0):
        profit_chart = build_profit_chart_data(profit_monthly)
        dept_profit_chart = profit_chart["by_dept"].get(dept_name, {})
    else:
        dept_profit_chart = {}

    return {
        # メタ
        "generated_at":    generated_at.strftime("%Y/%m/%d %H:%M"),
        "base_date":       base_date.strftime("%Y/%m/%d"),
        "base_date_raw":   json.dumps(base_date.strftime("%Y-%m-%d")),
        "dept_name":       dept_name,
        "dept_name_json":  json.dumps(dept_name, ensure_ascii=False),
        "dept_status":     dept_status,
        "dept_achievement": _fmt_optional(dept_ach),
        "dept_achievement_raw": float(dept_ach) if dept_ach is not None else 0,
        # 在院
        "inp_value":       inp_val,
        "inp_target":      float(inp_tgt) if inp_tgt is not None else 0,
        "inp_target_fmt":  _fmt_optional(inp_tgt, ".0f"),
        "inp_achievement": float(inp_ach) if inp_ach is not None else 0,
        "inp_achievement_fmt": _fmt_optional(inp_ach),
        "inp_status":      inp_st,
        "inp_progress":    round(inp_prog, 0),
        # 新入院
        "nadm_value":      nadm_val,
        "nadm_weekly":     nadm_r7,   # 直近7日合計（実数表示用）
        "nadm_target":     float(nadm_tgt) if nadm_tgt is not None else 0,
        "nadm_target_fmt": _fmt_optional(nadm_tgt, ".0f"),
        "nadm_progress":   float(nadm_prog_pct) if nadm_prog_pct is not None else 0,
        "nadm_progress_fmt": _fmt_optional(nadm_prog_pct),
        "nadm_status":     nadm_st,
        # 手術
        "surg_weekly":     surg_wk,
        "surg_target":     float(surg_tgt) if surg_tgt is not None else 0,
        "surg_target_fmt": _fmt_optional(surg_tgt, ".1f"),
        "surg_achievement": float(surg_ach) if surg_ach is not None else 0,
        "surg_achievement_fmt": _fmt_optional(surg_ach),
        "surg_status":     surg_st,
        "surg_progress":   round(surg_prog, 0),
        "has_surgery":     has_surgery,
        # 粗利
        "profit_value":       float(p_val) if p_val is not None else 0,
        "profit_value_fmt":   _fmt_optional(p_val),
        "profit_target":      float(p_tgt) if p_tgt is not None else 0,
        "profit_target_fmt":  _fmt_optional(p_tgt),
        "profit_achievement": float(p_ach) if p_ach is not None else 0,
        "profit_achievement_fmt": _fmt_optional(p_ach),
        "profit_status":      p_st,
        "profit_progress":    round(p_prog, 0),
        "profit_month":       p_month,
        "profit_mom":         mom_html,
        # JS埋め込みデータ
        "series_json":    json.dumps(series, ensure_ascii=False, default=str),
        "profit_json":    json.dumps(dept_profit_chart, ensure_ascii=False, default=str),
        # 週次全麻サマリー（テンプレート側でテーブル表示に使用）
        "surg_weekly_dates":  s_ga_weekly_dates,
        "surg_weekly_values": s_ga_weekly_values,
    }
