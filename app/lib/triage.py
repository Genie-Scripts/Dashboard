"""
triage.py — 部門トリアージ（多KPI合成スコアリング + LLMナラティブ）

設計原則（alerts.py / ai_narrative.py / weekly_story.py と同様）:
    - 数値計算・ランキングは Python で確定
    - LLM には翻訳（事実→自然文）のみを任せる
    - oMLX 未起動時は無害に失敗（Python 生成の fallback を使用）

エントリポイント:
    build_triage_section(adm, surg, targets, surg_targets, profit_monthly, base_date)
    → portal_ctx["triage"] に渡す list[dict]
"""

from __future__ import annotations
import json
import logging
import zlib
from typing import Optional

import pandas as pd

from .config import (
    NADM_DISPLAY_DEPTS, SURGERY_DISPLAY_DEPTS,
    SURGERY_EVAL_DEPTS, surgery_metric_label,
    WARD_NAMES, WARD_HIDDEN,
    unit_narration_kind, UNIT_ROLE_LEVERS, WARD_BANNED_LEVER_TERMS,
    operational_days_between,
)
from .metrics import (
    rolling7_new_admission, rolling7_surgery, rolling28_surgery_dept,
    daily_inpatient, build_daily_series, week_over_week,
    achievement_rate, discharge_dow_profile,
)
from .llm import DEFAULT_MODEL, chat_json

logger = logging.getLogger(__name__)

# ────────────────────────────────────
# 設定
# ────────────────────────────────────
# 使用モデルは llm.DEFAULT_MODEL（環境変数 OMLX_MODEL で一元管理）
DEFAULT_TEMPERATURE = 0.2
DEFAULT_NUM_PREDICT = 200

PRIMARY_THRESHOLD = 90.0   # 北極星KPIの達成率 < 90 → トリアージ対象
PRIORITY_HIGH_THRESHOLD = 80.0
PRIORITY_MID_THRESHOLD  = 90.0

# 全身麻酔手術の達成率がこれ以上なら「大幅クリア」とみなし、
# 在院患者数・退院曜日の指摘を控える（外科系の主軸は手術実績）
SURGERY_OVERACHIEVE_RATE = 120.0

# 北極星KPI（経営目標に直結する単一指標。ランク・優先度の唯一の基準）
#   外科系       → op  （全身麻酔手術件数）
#   内科系・病棟 → inp （在院患者数）
# 新入院は在院を上げるドライバー、粗利は別サブシステムが担うため、
# いずれもランキングには用いず文脈表示のみとする。

# ── 北極星KPIのトレンド（水準 × 傾向の2軸化）──
# 達成中でも失速・反転下降していれば早期警戒(watch)へ昇格し、
# 未達でも改善傾向なら優先度を1段下げる。MAクロスの離散点ではなく、
# 短期MA/長期MA の連続スプレッド(%)の符号で判定（週次観測のウィップソー回避）。
WATCH_CEILING   = 110.0      # 早期警戒(watch)の上限。これ以上の達成率は余裕大として
                             #   悪化傾向でも非対象（breach リスクが低いため騒がない）
CENSUS_MA_SHORT = 7          # 在院: 短期MA(日)
CENSUS_MA_LONG  = 28         # 在院: 長期MA(日)。7d窓で曜日季節性は除去済み
CENSUS_TREND_PT = 3.0        # 在院: スプレッド ±3% 以上で 改善/悪化
SURGERY_TREND_WIN     = 28   # 全麻: 直近28日 vs 前28日 の件数比
SURGERY_TREND_PT      = 15.0 # 全麻: ±15% 以上で 改善/悪化（件数は跳ねるため広め）
SURGERY_TREND_MIN_28D = 8    # 全麻: 直近28日が8件未満の小規模科はノイズのため非対象

# fallback 文言（未達 KPI ごと）
FALLBACK_SUGGESTIONS = {
    "adm":    "新入院・紹介患者の確保に向けた運用の再確認を推奨します",
    "inp":    "在院確保に向け、新入院・紹介患者の確保を推奨します",
    "op":     "手術枠の利用状況と予約調整の確認を推奨します",
    "profit": "収益構造の再レビューを推奨します",
}

TRIAGE_SYSTEM_PROMPT = """あなたは病院経営会議向けの要約ライターです。以下を厳守してください。

【厳守事項】
1. 与えられた確定事実のみを使い、新しい数値・事実・原因を追加しない
2. 推測・仮定・原因断定はしない
3. 特定の人名・治療方針・診療行為を記述しない
4. facts に存在する観点にのみ具体提案を許す
5. 改善傾向がある KPI には肯定的な言及を加える。ただし現状（達成率・水準）と傾向（前週比・改善/悪化）が食い違うときは逆接（「〜が」「〜ものの」）でつなぎ、順接（「〜ており」等）で並べない
6. 出力は指定 JSON スキーマのみ。前置きや説明文を付けない
7. 日本語、簡潔・丁寧・事務的なトーン
8. 「合成達成率」という語句およびその数値（パーセント）を
   observation / suggestion に出力しない。
   個別KPI（新入院・在院患者・手術・粗利）の達成状況で表現すること。
9. headline / observation / suggestion に診療科名・病棟名を含めない。
   科名・病棟名は画面上で別途表示されるため、重複を避けること。
10. 在院患者数の打ち手として「在院日数の短縮」「早期退院」「退院促進」を
    提案しないこと。在院患者数は経営の根幹指標であり、低い場合の打ち手は
    新入院・紹介患者の確保である（在院日数短縮は在院を下げ逆効果）。
11. 「全身麻酔手術が目標を大きく上回っている」旨の注記がある場合は、
    手術実績を主軸に肯定的に評価し、在院患者数や退院曜日の偏りは強く指摘しない。
12. 各科には「目標KPI」が指定される。内科系の目標は在院患者数の増加（レバー＝
    新入院・紹介患者の確保）、外科系の目標は全身麻酔手術件数の増加（レバー＝
    手術枠の活用・予約調整）。suggestion はその科の目標KPIを伸ばすレバーに集中し、
    目標でないKPIを主題にしないこと。
13. 【状況】に「悪化傾向（失速）」かつ目標達成中とある場合は、未達の挽回ではなく、
    達成水準の維持と失速要因の点検を主題にすること。「改善傾向」かつ未達とある場合は、
    まず改善を肯定したうえで勢いの維持・加速を促すこと。
14. 【このユニットで使える打ち手（レバー）】が示されている場合、suggestion はその範囲に
    限定すること。病棟・救急科などのユニットは紹介患者の獲得・地域医療連携を業務として
    担わないため、規則10・12がいう「紹介患者の確保」「地域医療連携」は診療科専用の
    レバーであり、これらのユニットには提案しないこと。本規則14は規則10・12より優先する。

【出力スキーマ】
{
  "priority": "high|mid|low",
  "headline": "20字以内の見出し（体言止め可）",
  "observation": "個別KPIの達成状況を述べる 50字以内",
  "suggestion": "推奨アクション 80字以内（汎用的・実行可能）"
}"""


# ════════════════════════════════════════
# スコアリング
# ════════════════════════════════════════

def _get_profit_rates(profit_monthly: Optional[pd.DataFrame]) -> dict:
    """診療科名 → 粗利達成率(float) のマップを返す。データなければ {}"""
    if profit_monthly is None or len(profit_monthly) == 0:
        return {}
    try:
        from .profit import get_latest_month_summary
        latest = get_latest_month_summary(profit_monthly)
        result = {}
        for _, r in latest.iterrows():
            if pd.notna(r.get("達成率")) and pd.notna(r.get("診療科名")):
                result[str(r["診療科名"])] = float(r["達成率"])
        return result
    except Exception as e:
        logger.debug(f"粗利達成率取得スキップ: {e}")
        return {}


def _dept_type(is_surgery: bool) -> tuple[str, str]:
    """(dept_type, primary_kpi) を返す。外科系=全麻 / それ以外=在院。"""
    return ("surgery", "op") if is_surgery else ("internal", "inp")


def _priority_from_rate(rate: float) -> str:
    """北極星KPIの達成率から優先度を確定する。"""
    if rate < PRIORITY_HIGH_THRESHOLD:
        return "high"
    if rate < PRIORITY_MID_THRESHOLD:
        return "mid"
    return "low"


def _ma_spread(series: pd.DataFrame, base_date, short: int, long_: int) -> Optional[float]:
    """日次系列の 短期MA vs 長期MA スプレッド(%)。
    正=短期が中期平均を上回る（上昇）。データ不足/長期MA=0 のとき None。"""
    if series is None or len(series) == 0:
        return None

    def _avg(days):
        start = base_date - pd.Timedelta(days=days - 1)
        w = series[(series["日付"] >= start) & (series["日付"] <= base_date)]
        return float(w["値"].mean()) if len(w) >= max(3, days // 4) else None

    ma_s, ma_l = _avg(short), _avg(long_)
    if ma_s is None or ma_l is None or ma_l == 0:
        return None
    return (ma_s - ma_l) / ma_l * 100.0


def _trend_dir(spread: Optional[float], pt: float) -> Optional[str]:
    """スプレッド(%)を 改善(up)/悪化(down)/横ばい(flat) に離散化。None入力はNone。"""
    if spread is None:
        return None
    if spread >= pt:
        return "up"
    if spread <= -pt:
        return "down"
    return "flat"


def _census_trend(adm, base_date, group_col, group_val) -> tuple[Optional[float], Optional[str]]:
    """在院患者数の 7d/28d スプレッド(%)と方向。"""
    s = build_daily_series(adm, "在院患者数", group_col=group_col, group_val=group_val)
    spread = _ma_spread(s, base_date, CENSUS_MA_SHORT, CENSUS_MA_LONG)
    return spread, _trend_dir(spread, CENSUS_TREND_PT)


def adjusted_weekly_target(target: Optional[float], base_date: pd.Timestamp) -> Optional[float]:
    """週目標を「直近7暦日窓の実際の営業日数/5」で割り引いた期待値に変換する（P1暦是正）。

    週目標は週5営業日を前提に設定されているため、祝日で窓内の営業日が減った週は
    そのまま比較すると達成率が不当に下振れる（窓は暦日のまま・期待値側で暦を吸収する
    設計原則。詳細: spec/暦補正と学習ループ改修プラン.md P1）。target が None ならそのまま None。

    biz_days == 5（通常週）は target をそのまま返す短絡を入れている（target*5/5 でも
    数学的には同値だが、float演算を経由させないことで通常週の完全恒等を保証する。
    5段階の達成度バケット境界(_q_target_gap)は比率のわずかな揺れで階級が変わり得るため、
    通常週で挙動が1ビットも変わらないことを型で保証する意図）。
    """
    if target is None:
        return None
    biz_days = operational_days_between(base_date - pd.Timedelta(days=6), base_date)
    if biz_days == 5:
        return target
    return target * biz_days / 5


def _surgery_trend(recent_28d: int, prior_28d: int,
                   base_date: pd.Timestamp) -> tuple[Optional[float], Optional[str]]:
    """全麻の 直近28暦日 vs 前28暦日 の件/営業日レート比(%)と方向（P1暦是正:
    生件数比→レート比。窓内に祝日が偏っていても暦影響を受けにくくする）。
    直近28日が小規模(<MIN・生件数ゲートは現状維持)・前期間の営業日レートが0 は
    ノイズのため非対象(None)。"""
    if recent_28d < SURGERY_TREND_MIN_28D:
        return None, None
    biz_now = operational_days_between(base_date - pd.Timedelta(days=27), base_date)
    biz_prev = operational_days_between(base_date - pd.Timedelta(days=55), base_date - pd.Timedelta(days=28))
    rate_now = recent_28d / biz_now if biz_now > 0 else None
    rate_prev = prior_28d / biz_prev if biz_prev > 0 else None
    if rate_now is None or not rate_prev:
        return None, None
    spread = (rate_now - rate_prev) / rate_prev * 100.0
    return spread, _trend_dir(spread, SURGERY_TREND_PT)


def _triage_status(primary_rate: float, trend_dir: Optional[str]) -> tuple[str, str]:
    """(status_kind, priority) を返す。水準 × 傾向の2軸判定。
    - below: 未達(<90)。rate基準で優先度。改善傾向(up)なら1段下げる
    - watch: 達成中だが悪化傾向(down) → 早期警戒(mid)
    - ok:    達成かつ非悪化 → 対象外(low)
    """
    if primary_rate < PRIMARY_THRESHOLD:
        prio = _priority_from_rate(primary_rate)
        if trend_dir == "up":
            prio = _downgrade_priority(prio)   # 改善傾向は1段下げて評価
        return "below", prio
    if trend_dir == "down" and primary_rate < WATCH_CEILING:
        return "watch", "mid"   # 達成中だが目標近辺で失速 → 早期警戒
    return "ok", "low"


def _make_entity_record(name, entity_type, is_surgery,
                        adm_rate, adm_actual, adm_target,
                        inp_rate, inp_actual, inp_target,
                        op_rate, op_actual, op_target,
                        profit_rate, ward_code=None,
                        primary_trend=None, trend_dir=None) -> Optional[dict]:
    """北極星KPI（外科系=op / それ以外=inp）でランクするレコードを生成。

    北極星KPIの達成率が測れない場合は新入院にフォールバックし
    （primary_is_fallback=True）、それも測れなければ None を返す。
    新入院・粗利はランクには用いず、文脈フィールドとして保持する。
    primary_trend / trend_dir は北極星KPIの傾向（水準×傾向の2軸化）。
    """
    dept_type, primary_kpi = _dept_type(is_surgery)

    primary_rate = op_rate if primary_kpi == "op" else inp_rate
    primary_is_fallback = False
    if primary_rate is None:
        primary_rate = adm_rate
        primary_is_fallback = True
    if primary_rate is None:
        return None   # 北極星も新入院も測れない → 対象外

    # フォールバック中（北極星の目標欠損）はランク指標と傾向指標が食い違うため傾向は使わない
    if primary_is_fallback:
        primary_trend, trend_dir = None, None

    status_kind, priority = _triage_status(primary_rate, trend_dir)

    return {
        "name": name,
        "entity_type": entity_type,
        "dept_type": dept_type,
        "primary_kpi": primary_kpi,
        "primary_rate": round(primary_rate, 1),
        "primary_is_fallback": primary_is_fallback,
        "primary_trend": round(primary_trend, 1) if primary_trend is not None else None,
        "trend_dir": trend_dir,
        "status_kind": status_kind,
        "improving": bool(status_kind == "below" and trend_dir == "up"),
        "worsening": bool(trend_dir == "down"),
        "priority": priority,
        "is_surgery_dept": is_surgery,
        "adm_rate": adm_rate,
        "adm_actual": adm_actual,
        "adm_target": round(float(adm_target), 1) if adm_target else None,
        "inp_rate": inp_rate,
        "inp_actual": inp_actual,
        "inp_target": round(float(inp_target), 1) if inp_target else None,
        "op_rate": op_rate,
        "op_actual": op_actual,
        "op_target": round(float(op_target), 1) if (is_surgery and op_target) else None,
        "profit_rate": profit_rate,
        "adm_gap": round(float(adm_target) - adm_actual, 1) if adm_target else None,
        "inp_gap": round(float(inp_target) - inp_actual, 1) if inp_target else None,
        "op_gap": round(float(op_target) - op_actual, 1) if (is_surgery and op_target) else None,
        "ward_code": ward_code,
    }


def score_departments(adm: pd.DataFrame, surg: pd.DataFrame,
                      targets: dict, surg_targets: dict,
                      profit_monthly: Optional[pd.DataFrame],
                      base_date: pd.Timestamp) -> list[dict]:
    """
    全科を北極星KPIの達成率でスコアリングして返す。
      外科系   → op_rate （全身麻酔手術）
      内科系   → inp_rate（在院患者数）
    新入院はドライバー、粗利は文脈として保持するが、ランク・優先度には用いない。

    Returns:
        list of dict（_make_entity_record のスキーマ。primary_rate 昇順）
    """
    r7_nadm = rolling7_new_admission(adm, base_date)
    r7_surg = rolling7_surgery(surg, base_date)
    inp_by_dept = daily_inpatient(adm, base_date)["by_dept"]
    nadm_tgt = targets.get("new_admission", {}).get("dept", {})
    inp_tgt  = targets.get("inpatient", {}).get("dept", {})
    profit_rates = _get_profit_rates(profit_monthly)
    # 全麻トレンド用: 直近28日 vs 前28日 の科別件数
    r28_now  = rolling28_surgery_dept(surg, base_date)["by_dept"]
    r28_prev = rolling28_surgery_dept(surg, base_date - pd.Timedelta(days=SURGERY_TREND_WIN))["by_dept"]

    results = []
    for dept in NADM_DISPLAY_DEPTS | SURGERY_EVAL_DEPTS:
        is_surgery = dept in SURGERY_EVAL_DEPTS

        adm_actual  = r7_nadm["by_dept"].get(dept, 0)
        adm_target  = nadm_tgt.get(dept)
        inp_actual  = inp_by_dept.get(dept, 0)
        inp_target  = inp_tgt.get(dept)
        op_actual   = r7_surg["by_dept"].get(dept, 0) if is_surgery else None
        # P1暦是正: 週目標は窓内(直近7暦日)の実際の営業日数/5で割り引く（達成率・
        # 目標表示・ギャップのすべてが同じ調整後目標を参照する＝相互不整合を防ぐ）。
        op_target   = (adjusted_weekly_target(surg_targets.get(dept), base_date)
                      if is_surgery else None)
        profit_rate = profit_rates.get(dept)

        adm_rate    = achievement_rate(adm_actual, adm_target)
        inp_rate    = achievement_rate(inp_actual, inp_target)
        op_rate     = achievement_rate(op_actual, op_target) if is_surgery else None

        # 北極星KPIの傾向: 外科系=全麻(第3段)、内科系=在院
        if is_surgery:
            primary_trend, trend_dir = _surgery_trend(
                r28_now.get(dept, 0), r28_prev.get(dept, 0), base_date)
        else:
            primary_trend, trend_dir = _census_trend(adm, base_date, "診療科名", dept)

        rec = _make_entity_record(
            name=dept, entity_type="dept", is_surgery=is_surgery,
            adm_rate=adm_rate, adm_actual=adm_actual, adm_target=adm_target,
            inp_rate=inp_rate, inp_actual=inp_actual, inp_target=inp_target,
            op_rate=op_rate, op_actual=op_actual, op_target=op_target,
            profit_rate=profit_rate,
            primary_trend=primary_trend, trend_dir=trend_dir,
        )
        if rec is not None:
            results.append(rec)

    results.sort(key=lambda x: x["primary_rate"])
    return results


def score_wards(adm: pd.DataFrame, targets: dict,
                base_date: pd.Timestamp) -> list[dict]:
    """
    全病棟を在院患者数（北極星）の達成率でスコアリングして返す。
    床ベースゆえ在院が北極星、新入院はそれを上げるドライバー。

    Returns:
        list of dict（_make_entity_record のスキーマ。primary_rate 昇順）
    """
    r7_nadm = rolling7_new_admission(adm, base_date)
    inp_by_ward = daily_inpatient(adm, base_date)["by_ward"]
    nadm_tgt = targets.get("new_admission", {}).get("ward", {})
    inp_tgt  = targets.get("inpatient", {}).get("ward", {})

    results = []
    for wcode, wname in WARD_NAMES.items():
        if wcode in WARD_HIDDEN:
            continue

        adm_actual = r7_nadm["by_ward"].get(wcode, 0)
        adm_target = nadm_tgt.get(wcode)
        inp_actual = inp_by_ward.get(wcode, 0)
        inp_target = inp_tgt.get(wcode)

        adm_rate = achievement_rate(adm_actual, adm_target)
        inp_rate = achievement_rate(inp_actual, inp_target)
        primary_trend, trend_dir = _census_trend(adm, base_date, "病棟コード", wcode)

        rec = _make_entity_record(
            name=wname, entity_type="ward", is_surgery=False,
            adm_rate=adm_rate, adm_actual=adm_actual, adm_target=adm_target,
            inp_rate=inp_rate, inp_actual=inp_actual, inp_target=inp_target,
            op_rate=None, op_actual=None, op_target=None,
            profit_rate=None, ward_code=wcode,
            primary_trend=primary_trend, trend_dir=trend_dir,
        )
        if rec is not None:
            results.append(rec)

    results.sort(key=lambda x: x["primary_rate"])
    return results


# ════════════════════════════════════════
# 対象抽出 + facts 生成
# ════════════════════════════════════════

def pick_targets(scored: list[dict], adm: pd.DataFrame,
                 base_date: pd.Timestamp) -> list[dict]:
    """
    トリアージ対象（未達 below + 達成だが悪化傾向 watch）を抽出し、北極星KPIを
    先頭にした facts / kpi_summary と WoW ヒント・傾向タグを付与して返す。
    """
    items = [s for s in scored if s.get("status_kind") in ("below", "watch")]

    for i, item in enumerate(items):
        is_ward = item.get("entity_type") == "ward"
        is_surgery = item.get("is_surgery_dept")

        # 各KPIの fact 文（無ければ None）
        adm_fact = inp_fact = op_fact = profit_fact = None
        if item["adm_rate"] is not None and item["adm_target"] is not None:
            adm_gap = item.get("adm_gap", 0) or 0
            adm_gap_str = f"・目標まであと{adm_gap:.1f}人" if adm_gap > 0 else f"・目標+{abs(adm_gap):.1f}人超過"
            adm_fact = (f"新入院（直近7日）: 実績{item['adm_actual']:.0f}人 / "
                        f"目標{item['adm_target']:.1f}人（達成率{item['adm_rate']:.0f}%{adm_gap_str}）")
        if item["inp_rate"] is not None and item["inp_target"] is not None:
            inp_gap = item.get("inp_gap", 0) or 0
            inp_gap_str = f"・目標まであと{inp_gap:.1f}人" if inp_gap > 0 else f"・目標+{abs(inp_gap):.1f}人超過"
            inp_fact = (f"在院患者: 実績{item['inp_actual']:.0f}人 / "
                        f"目標{item['inp_target']:.1f}人（達成率{item['inp_rate']:.0f}%{inp_gap_str}）")
        if is_surgery and item["op_rate"] is not None and item["op_target"] is not None:
            op_gap = item.get("op_gap", 0) or 0
            op_gap_str = f"・目標まであと{op_gap:.1f}件" if op_gap > 0 else f"・目標+{abs(op_gap):.1f}件超過"
            op_fact = (f"{surgery_metric_label(item['name'])}（直近7日）: 実績{item['op_actual']:.0f}件 / "
                       f"目標{item['op_target']:.1f}件（達成率{item['op_rate']:.0f}%{op_gap_str}）")
        if item.get("profit_rate") is not None:
            profit_fact = f"粗利: 達成率{item['profit_rate']:.0f}%（参考・ランク対象外）"

        # 北極星KPIを先頭に並べる（粗利は末尾の文脈）
        if is_surgery:
            facts = [f for f in (op_fact, inp_fact, adm_fact, profit_fact) if f]
        else:
            facts = [f for f in (inp_fact, adm_fact, profit_fact) if f]

        # WoW ヒント（新入院前週比）
        wow_hint = None
        try:
            if is_ward:
                s = build_daily_series(adm, "新入院患者数_病棟",
                                       group_col="病棟コード",
                                       group_val=item["ward_code"])
            else:
                s = build_daily_series(adm, "新入院患者数",
                                       group_col="診療科名",
                                       group_val=item["name"])
            wow = week_over_week(s, base_date)
            if wow is not None:
                wow_hint = f"新入院が前週比{wow:+.0f}人"
        except Exception:
            pass

        # KPI サマリー行（テンプレート headline 用: 北極星KPIを先頭にコンパクト表示）
        def _gap_s(gap):
            return f"▲{gap:.1f}" if gap > 0 else f"+{abs(gap):.1f}"
        adm_kpi = inp_kpi = op_kpi = None
        if item["adm_rate"] is not None:
            adm_kpi = f"新入院{item['adm_rate']:.0f}%({_gap_s(item.get('adm_gap', 0) or 0)}人)"
        if item["inp_rate"] is not None:
            inp_kpi = f"在院{item['inp_rate']:.0f}%({_gap_s(item.get('inp_gap', 0) or 0)}人)"
        if is_surgery and item["op_rate"] is not None:
            op_kpi = f"{surgery_metric_label(item['name'], short=True)}{item['op_rate']:.0f}%({_gap_s(item.get('op_gap', 0) or 0)}件)"

        if is_surgery:
            # 全麻が主軸。在院・新入院は達成率のみを文脈として併記
            ctx = "・".join(
                f"{lbl}{rate:.0f}%"
                for lbl, rate in (("在院", item["inp_rate"]), ("新入院", item["adm_rate"]))
                if rate is not None
            )
            head = op_kpi or ""
            item["kpi_summary"] = f"{head} ｜ {ctx}" if (head and ctx) else (head or ctx)
        else:
            # 内科系・病棟: 在院が主軸、新入院はそれを上げるドライバー（←）
            item["kpi_summary"] = (f"{inp_kpi} ← {adm_kpi}" if (inp_kpi and adm_kpi)
                                   else (inp_kpi or adm_kpi or ""))

        # 傾向タグ（水準×傾向の2軸）: 達成中の悪化＝早期警戒、未達の改善＝緩和
        if item.get("status_kind") == "watch":
            item["kpi_summary"] += "　↘達成中だが悪化傾向"
        elif item.get("worsening"):
            item["kpi_summary"] += "　↘悪化傾向"
        elif item.get("improving"):
            item["kpi_summary"] += "　↗改善傾向"

        # 全身麻酔手術を大幅クリアしている外科系は在院・退院曜日をうるさく言わない
        item["surgery_strong"] = bool(
            not is_ward and item.get("is_surgery_dept")
            and item.get("op_rate") is not None
            and item["op_rate"] >= SURGERY_OVERACHIEVE_RATE
        )

        entity_label = "病棟" if is_ward else "科"
        item["rank_from_bottom"] = i + 1
        item["total_items"] = len(items)
        item["entity_label"] = entity_label
        item["facts"] = facts
        item["wow_hint"] = wow_hint
        item["narrative"] = None   # LLM で後から付与
        # 病棟・診療科とも dept.html の個別ページ（#<名称>）へ遷移する。
        # 旧実装は病棟を汎用の detail.html#inpatient?axis=ward に流していたため
        # 変化点リストの病棟チップが個別ページに飛ばなかった（回帰修正）。
        item["href"] = f"dept.html#{item['name']}"

    return items


# ════════════════════════════════════════
# LLM ナラティブ
# ════════════════════════════════════════

def _unit_kind(item: dict) -> Optional[str]:
    """item の「ユニット役割」種別を返す。診療科（内科系/外科系。救急科を除く）は None。

    unit_narration_kind() を単一の真実として使い、特例（emergency/critical_care/er_dept）
    でない一般病棟は "ward" として返す（診療科向けレバーとの取り違えを防ぐため）。
    """
    kind = unit_narration_kind(item["entity_type"], item.get("ward_code"), item["name"])
    if kind is not None:
        return kind
    return "ward" if item.get("entity_type") == "ward" else None


def _build_triage_prompt(item: dict) -> str:
    facts_block = "\n".join(f"- {f}" for f in item["facts"])
    wow_line = f"\n・前週同曜日比: {item['wow_hint']}" if item.get("wow_hint") else ""
    strong_note = (f"\n\n【重要】この科は{surgery_metric_label(item['name'])}が目標を大きく上回っている。"
                   "手術実績を主軸に肯定的に評価し、在院患者数や退院曜日の偏りは"
                   "強く指摘しないこと。") if item.get("surgery_strong") else ""
    entity_label = item.get("entity_label", "科")
    kind = _unit_kind(item)
    goal_map = {
        "inp": "在院患者数の増加（レバー: 新入院・紹介患者の確保）",
        "op":  f"{surgery_metric_label(item['name'])}件数の増加（レバー: 手術枠の活用・予約調整）",
        "ward": "在院患者数の増加（レバー: 病床管理〔空床の把握・ベッドコントロール〕・"
                "緊急入院や転入の受け入れ・退院や転棟のタイミング調整）",
        "emergency": "病床稼働率の維持（レバー: 救急・緊急入院の受け入れ、転棟・転出判断の"
                     "迅速化、週末の受け入れ体制維持）",
        "critical_care": "在院患者数・病床稼働率の維持（レバー: 院内急変・緊急術後の受け入れ、"
                          "手術部/救急/一般病棟との連携、後方病床への転棟タイミングの適正化）",
        "er_dept": "救急受け入れの増加（レバー: 救急車の応需台数増、受入体制の維持・強化、"
                   "後方病床連携でのER滞在時間短縮）",
    }
    # 診療科（kind=None）は従来どおり primary_kpi で引く。ユニット役割が判る場合はそちらを優先。
    goal_line = goal_map.get(kind, "") if kind is not None else goal_map.get(item.get("primary_kpi"), "")
    # 診療科以外（病棟・特例ユニット）は使える打ち手を明示し、診療科専用レバーを禁止する
    levers_block = ""
    if kind is not None:
        levers_text = "\n".join(f"- {lv}" for lv in UNIT_ROLE_LEVERS.get(kind, []))
        levers_block = (
            f"\n\n【このユニットで使える打ち手（レバー）】\n{levers_text}"
            "\n\n【禁止】このユニットは紹介患者の獲得・地域医療連携を業務として担わない"
            "（いずれも診療科の打ち手）。『紹介元への働きかけ』『地域医療連携の強化』"
            "『紹介患者の確保』は suggestion に書かないこと。"
        )
    if item.get("status_kind") == "watch":
        trend_note = ("\n\n【状況】目標は達成しているが、北極星KPIが直近で悪化傾向（失速）。"
                      "未達ではないため『挽回』ではなく、達成水準の維持と失速要因の点検を促すこと。")
    elif item.get("improving"):
        trend_note = ("\n\n【状況】未達だが北極星KPIは改善傾向。まず改善を肯定し、その勢いを維持・"
                      "加速する打ち手を促すこと（現状の未達と改善傾向は逆接で繋ぐ）。")
    elif item.get("worsening"):
        trend_note = "\n\n【状況】北極星KPIは悪化傾向。失速の歯止めを意識した打ち手を促すこと。"
    else:
        trend_note = ""
    total_label = f"全{item['total_items']}{entity_label}" if "total_items" in item else f"全{item.get('total_depts', '?')}科"
    return f"""以下の確定事実を要約し、JSON を1つだけ出力してください。

【{item.get('entity_label', '診療科')}】{item['name']}（下位{item['rank_from_bottom']}位 / {total_label}）
【優先度】{item['priority']}
【この{entity_label}の目標KPI】{goal_line}

【確定事実】
{facts_block}{wow_line}{strong_note}{trend_note}{levers_block}

【注意】
- priority は必ず "{item['priority']}" を出力すること（Python で再検証する）
- headline / observation / suggestion / priority の4キーを持つ JSON を出力すること
- 「合成達成率」という語句・その数値は出力しないこと
- 事実にない数値・原因・人物を補わないこと
- JSON 以外の文字（```、前置き、末尾コメント）を出力しないこと"""


def _sanitize_narrative_text(text: str, entity_name: str = "") -> str:
    """合成スコア数値の露出・科名重複を防ぐ後処理（多重防衛・層3）"""
    import re
    # "合成達成率XX%" / "合成達成率 XX %" 等を除去
    text = re.sub(r'合成達成率\s*[\d.]+\s*%', '', text)
    # "総合スコアXX%" / "合成スコアXX%" 等のバリエーションも除去
    text = re.sub(r'(総合|合成)[スコア達成率]*\s*[\d.]+\s*%', '', text)
    # 科名・病棟名の重複除去（先頭の「XXX、」「XXXは」「XXXの」パターン）
    if entity_name:
        text = re.sub(rf'^{re.escape(entity_name)}[、はの：:]\s*', '', text)
    return text.strip()


def _extract_triage_json(text: str, entity_name: str = "") -> Optional[dict]:
    """LLM 出力から JSON を取り出し、4キーを検証・サニタイズして返す"""
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    required_keys = ("headline", "observation", "suggestion")
    if not all(k in obj for k in required_keys):
        return None
    return {
        "priority":    str(obj.get("priority", "")).strip(),
        "headline":    _sanitize_narrative_text(str(obj["headline"]), entity_name),
        "observation": _sanitize_narrative_text(str(obj["observation"]), entity_name),
        "suggestion":  _sanitize_narrative_text(str(obj["suggestion"]), entity_name),
    }


def _fallback_suggestion(item: dict) -> str:
    """LLM 失敗時の定型 suggestion。診療科（kind=None）は従来どおり FALLBACK_SUGGESTIONS、
    病棟・特例ユニットは役割別のレバーに基づく文言（紹介・地域医療連携を含めない）。"""
    kind = _unit_kind(item)
    if kind == "ward":
        return "空床の把握とベッドコントロールを徹底し、緊急入院・転入の受け入れ拡大を推奨します"
    if kind == "emergency":
        return "救急・緊急入院の受け入れ体制の維持と、転棟・転出判断の迅速化を推奨します"
    if kind == "critical_care":
        return "院内急変・緊急術後の受け入れ維持と、後方病床への転棟タイミングの適正化を推奨します"
    if kind == "er_dept":
        return "救急車の応需台数増と、後方病床連携によるER滞在時間の短縮を推奨します"

    # 診療科（内科系/外科系）は従来どおり
    if item.get("primary_is_fallback"):
        key = "adm"
    elif item.get("primary_kpi") == "op":
        key = "op"
    else:
        key = "inp"
    return FALLBACK_SUGGESTIONS[key]


def _make_fallback_narrative(item: dict) -> dict:
    """LLM 失敗時の Python 定型文 fallback（北極星KPI主体・水準×傾向）"""
    # 北極星KPIが測れずフォールバック中の科は新入院を主題にする
    if item.get("primary_is_fallback"):
        kpi = "新入院"
    elif item.get("primary_kpi") == "op":
        kpi = surgery_metric_label(item["name"])
    else:
        kpi = "在院患者数"

    # 達成中だが悪化傾向 → 早期警戒（挽回ではなく維持・点検）
    if item.get("status_kind") == "watch":
        return {
            "priority":    item["priority"],
            "headline":    f"{kpi}が悪化傾向（達成中）",
            "observation": f"{kpi}は目標を満たしていますが、直近で悪化傾向です",
            "suggestion":  f"達成水準の維持に向け、{_fallback_suggestion(item)}",
        }
    # 未達だが改善傾向 → まず肯定し勢いの維持
    if item.get("improving"):
        return {
            "priority":    item["priority"],
            "headline":    f"{kpi}は改善傾向（なお未達）",
            "observation": f"{kpi}は目標を下回るものの、改善傾向です",
            "suggestion":  f"この勢いを維持し、{_fallback_suggestion(item)}",
        }
    return {
        "priority":    item["priority"],
        "headline":    f"{kpi}が目標未達",
        "observation": f"{kpi}が目標を下回っています",
        "suggestion":  _fallback_suggestion(item),
    }


def _narrate_one_call(item: dict, model: str, temperature: float) -> Optional[dict]:
    """単一科を LLM で翻訳（1回呼び出し）。失敗時は None"""
    system = TRIAGE_SYSTEM_PROMPT
    user = _build_triage_prompt(item)
    # seed はプロンプト内容の CRC32 で決定論化（ai_narrative.py と同じ流儀・3-3 月次安定性）。
    seed = zlib.crc32((system + user).encode("utf-8")) & 0x7FFFFFFF
    try:
        content = chat_json(
            system=system,
            user=user,
            model=model,
            temperature=temperature,
            max_tokens=DEFAULT_NUM_PREDICT,
            seed=seed,
        )
    except Exception as e:
        # oMLX 未起動 / openai 未インストール / モデル未取得 すべてここで無害に縮退
        logger.warning(f"oMLX triage 呼び出し失敗 ({item['name']}): {e}")
        return None

    result = _extract_triage_json(content, entity_name=item["name"])
    if result is None:
        return None

    # priority は Python 側で強制上書き（LLM は参考のみ）
    result["priority"] = item["priority"]
    return result


def _contains_banned_lever(result: dict) -> bool:
    """headline/observation/suggestion の連結に診療科専用レバー語が含まれるか"""
    combined = f"{result.get('headline', '')}{result.get('observation', '')}{result.get('suggestion', '')}"
    return any(term in combined for term in WARD_BANNED_LEVER_TERMS)


def _narrate_one(item: dict, model: str, temperature: float) -> Optional[dict]:
    """単一科を LLM で翻訳。失敗時は None

    診療科以外（病棟・特例ユニット）は生成後の機械ガード（多重防衛）を通す。
    ローカルLLM(Swallow-8B)はプロンプトの指示だけでは一般知識から「紹介元への
    働きかけ」等を書きがちなため、禁止語（WARD_BANNED_LEVER_TERMS）を検出したら
    温度を下げて1回だけ再試行し、それでも駄目なら None を返す（呼び出し元
    narrate_triage が Python 定型文へ無害縮退する）。診療科の経路はガード無し（従来どおり）。
    """
    result = _narrate_one_call(item, model, temperature)
    if _unit_kind(item) is None or result is None:
        return result
    if not _contains_banned_lever(result):
        return result

    logger.warning(f"triage 出力に禁止語検出、温度を下げて再試行 ({item['name']})")
    retry = _narrate_one_call(item, model, 0.1)
    if retry is not None and not _contains_banned_lever(retry):
        return retry
    return None


def narrate_triage(items: list[dict],
                   model: str = DEFAULT_MODEL,
                   temperature: float = DEFAULT_TEMPERATURE,
                   use_fallback: bool = True,
                   quiet: bool = False) -> list[dict]:
    """
    各科に narrative フィールドを付与して返す。

    - LLM 成功時: narrative = {priority, headline, observation, suggestion}
    - LLM 失敗時: use_fallback=True なら Python 定型文、False なら None
    - oMLX 未起動時は全科 fallback（例外は投げない）
    """
    enriched = []
    for item in items:
        n = _narrate_one(item, model=model, temperature=temperature)
        item2 = dict(item)
        if n is not None:
            item2["narrative"] = n
            status = "✓"
        elif use_fallback:
            item2["narrative"] = _make_fallback_narrative(item)
            status = "fb"
        else:
            item2["narrative"] = None
            status = "—"
        if not quiet:
            print(f"    [triage] {status} {item['name']} ({item['primary_rate']:.0f}%)")
        enriched.append(item2)
    return enriched


# ════════════════════════════════════════
# 退院曜日平準化（composite_rate とは別軸の独立検知）
# ════════════════════════════════════════

LEVELING_REDIST_THRESHOLD = 30.0   # 再配分率(%) これ以上を発信対象
LEVELING_MIN_PER_WEEK     = 10.0   # 週退院がこれ未満は付け替え余地が小さく%も不安定なため除外
LEVELING_MAX_ITEMS        = 5      # 病棟・科それぞれ最大件数
LEVELING_WEEKS            = 8
LEVELING_TREND_WEEKS      = 4      # 改善傾向判定: 直近4週 vs 前4週で再配分率を比較
LEVELING_IMPROVE_PT       = 4.0    # 再配分率が4pt以上 低下/上昇 で 改善/悪化 とみなす

LEVELING_SYSTEM_PROMPT = """あなたは病院の病床管理・退院支援向けの要約ライターです。以下を厳守してください。

【厳守事項】
1. 与えられた確定事実のみを使い、新しい数値・事実・原因を追加しない
2. テーマは「退院の曜日平準化」。土曜など週後半に偏った退院を、谷である月曜（週前半）へ"付け替える"提案に限定する
3. 【最重要】退院を早める・在院日数を短縮する提案は禁止。これは曜日の付け替えであり、総退院数と在院患者数は維持する前提。「早期退院」「退院促進」等の表現は使わない
4. 推測・原因断定・人名・治療方針は書かない
5. 改善の打ち手は「週末の退院準備の前倒し」「月曜の退院枠確保」など曜日付け替えに資する観点に限る
6. headline / observation / suggestion に診療科名・病棟名を含めない（画面に別途表示されるため重複を避ける）
7. 「改善傾向」の事実がある場合は、まず肯定的に評価し、その上でさらなる平準化を促す。現状（土曜偏り・水準）と傾向（改善/悪化）が食い違うときは逆接（「〜が」「〜ものの」）でつなぎ、順接（「〜ており」等）で並べない
8. 出力は指定 JSON のみ。前置き・説明文・``` は付けない
9. 日本語、簡潔・丁寧・事務的なトーン

【出力スキーマ】
{
  "priority": "high|mid|low",
  "headline": "20字以内の見出し（体言止め可）",
  "observation": "曜日偏在の状況を述べる 50字以内",
  "suggestion": "土曜→月曜の付け替え等の具体策 80字以内"
}"""


def _leveling_priority(redist: float) -> str:
    if redist >= 37:
        return "high"
    if redist >= 33:
        return "mid"
    return "low"


def _downgrade_priority(p: str) -> str:
    """改善傾向のユニットは優先度を1段下げる"""
    return {"high": "mid", "mid": "low", "low": "low"}.get(p, p)


def _leveling_trend(adm: pd.DataFrame, base_date: pd.Timestamp,
                    group_col: str, group_val: str) -> Optional[float]:
    """退院曜日の改善傾向。直近4週 vs 前4週の再配分率の差(pt)。
    正=改善（再配分率が低下）。いずれかの窓で退院が少なすぎる場合は None。"""
    recent = discharge_dow_profile(adm, base_date, group_col=group_col,
                                   group_val=group_val, weeks=LEVELING_TREND_WEEKS)
    prior_base = base_date - pd.Timedelta(days=7 * LEVELING_TREND_WEEKS)
    prior = discharge_dow_profile(adm, prior_base, group_col=group_col,
                                  group_val=group_val, weeks=LEVELING_TREND_WEEKS)
    if recent["per_week"] < 3 or prior["per_week"] < 3:
        return None
    return round(prior["redistribution"] - recent["redistribution"], 1)


def _leveling_item(name: str, entity_type: str, prof: dict, href: str,
                   redist_trend: Optional[float] = None) -> Optional[dict]:
    """discharge_dow_profile から triage item スキーマの平準化アイテムを生成。
    閾値未満・小規模ユニットは None。redist_trend>0 は改善傾向。"""
    redist = prof.get("redistribution") or 0.0
    per_week = prof.get("per_week") or 0.0
    if per_week < LEVELING_MIN_PER_WEEK or redist < LEVELING_REDIST_THRESHOLD:
        return None

    shares = prof.get("shares") or [0] * 7
    mon = prof.get("mon_share", 0.0)
    sat = prof.get("sat_share", 0.0)
    fri = shares[4] if len(shares) > 4 else 0.0
    redist_vol = round(redist / 100.0 * per_week, 1)   # 付け替え余地（件/週）

    improving = redist_trend is not None and redist_trend >= LEVELING_IMPROVE_PT
    worsening = redist_trend is not None and redist_trend <= -LEVELING_IMPROVE_PT

    facts = [
        f"退院が週後半に集中: 土曜{sat:.0f}%・金曜{fri:.0f}%に対し、月曜は{mon:.0f}%で谷",
        f"目標(平日均等)からの再配分率{redist:.0f}%、曜日の付け替え余地は週{redist_vol:.0f}件程度",
        f"週次退院は約{per_week:.0f}件（総量・平均在院は維持する前提）",
    ]
    if improving:
        facts.append(f"退院曜日の偏りは改善傾向（再配分率が直近4週で前4週比 {redist_trend:.0f}pt 低下）")
    elif worsening:
        facts.append(f"退院曜日の偏りは悪化傾向（再配分率が直近4週で前4週比 {abs(redist_trend):.0f}pt 上昇）")

    priority = _leveling_priority(redist)
    if improving:
        priority = _downgrade_priority(priority)   # 改善中は優先度を1段下げて評価

    trend_tag = "↘改善" if improving else ("↗悪化" if worsening else "")
    return {
        "name": name,
        "entity_type": entity_type,
        "entity_label": "病棟" if entity_type == "ward" else "科",
        "redist": round(float(redist), 1),
        "redist_trend": redist_trend,
        "improving": improving,
        "priority": priority,
        "facts": facts,
        "kpi_summary": f"土{sat:.0f}% / 月{mon:.0f}% / 再配分{redist:.0f}%（週{redist_vol:.0f}件）{trend_tag}",
        "narrative": None,
        "href": href,
    }


def score_leveling(adm: pd.DataFrame, surg: pd.DataFrame, surg_targets: dict,
                   base_date: pd.Timestamp) -> tuple[list[dict], list[dict]]:
    """退院曜日の偏り（再配分率）が閾値超過の科・病棟を抽出。
    全身麻酔手術を大幅クリアしている外科系（手術目標設定科）は対象外。
    Returns: (dept_items, ward_items)  いずれも redist 降順・上位 LEVELING_MAX_ITEMS 件。"""
    r7_surg = rolling7_surgery(surg, base_date)["by_dept"]

    dept_items: list[dict] = []
    for dept in sorted(NADM_DISPLAY_DEPTS | SURGERY_EVAL_DEPTS):
        # 手術目標を大幅にクリアしている外科系は退院曜日集中をうるさく言わない
        # （P1暦是正: score_departments と同じ調整後週目標で判定を揃える）
        if dept in SURGERY_EVAL_DEPTS:
            s_rate = achievement_rate(r7_surg.get(dept, 0),
                                      adjusted_weekly_target(surg_targets.get(dept), base_date))
            if s_rate is not None and s_rate >= SURGERY_OVERACHIEVE_RATE:
                continue
        prof = discharge_dow_profile(adm, base_date, group_col="診療科名",
                                     group_val=dept, weeks=LEVELING_WEEKS)
        trend = _leveling_trend(adm, base_date, "診療科名", dept)
        it = _leveling_item(dept, "dept", prof, href=f"dept.html#{dept}",
                            redist_trend=trend)
        if it:
            dept_items.append(it)

    ward_items: list[dict] = []
    for wcode, wname in WARD_NAMES.items():
        if wcode in WARD_HIDDEN:
            continue
        prof = discharge_dow_profile(adm, base_date, group_col="病棟コード",
                                     group_val=wcode, weeks=LEVELING_WEEKS)
        trend = _leveling_trend(adm, base_date, "病棟コード", wcode)
        it = _leveling_item(wname, "ward", prof, href=f"dept.html#{wname}",
                            redist_trend=trend)
        if it:
            ward_items.append(it)

    dept_items.sort(key=lambda x: -x["redist"])
    ward_items.sort(key=lambda x: -x["redist"])
    return dept_items[:LEVELING_MAX_ITEMS], ward_items[:LEVELING_MAX_ITEMS]


def _build_leveling_prompt(item: dict) -> str:
    from .eval_rules import build_leveling_context
    facts_block = "\n".join(f"- {f}" for f in item["facts"])
    ctx = build_leveling_context()
    ctx_block = f"\n{ctx}\n" if ctx else ""
    label = item.get("entity_label", "科")
    return f"""以下の確定事実を要約し、JSON を1つだけ出力してください。

【対象】{label}（退院の曜日平準化）
【優先度】{item['priority']}

【確定事実】
{facts_block}
{ctx_block}
【注意】
- priority は必ず "{item['priority']}" を出力すること（Python で再検証する）
- headline / observation / suggestion / priority の4キーを持つ JSON を出力すること
- 退院を早める・在院日数を短縮する提案は禁止。曜日の付け替え（土曜→月曜）に限定すること
- 科名・病棟名は出力しないこと
- 事実にない数値・原因・人物を補わないこと
- JSON 以外の文字（```、前置き、末尾コメント）を出力しないこと"""


def _make_leveling_fallback(item: dict) -> dict:
    """LLM 失敗時の Python 定型文 fallback"""
    if item.get("improving"):
        return {
            "priority":    item["priority"],
            "headline":    "改善傾向（平準化を継続）",
            "observation": "退院の曜日偏りは改善傾向ですが、なお土曜寄りです",
            "suggestion":  "この流れで土曜の退院を月曜へ。週末の退院準備の前倒しを継続してください（総退院数・在院は維持）",
        }
    return {
        "priority":    item["priority"],
        "headline":    "退院が週後半に偏在",
        "observation": "土曜など週後半に退院が集中し、月曜が谷になっています",
        "suggestion":  "土曜予定の退院を月曜へ。週末は退院準備を進め月曜に実行。総退院数と在院は維持してください",
    }


def _narrate_leveling_one(item: dict, model: str, temperature: float) -> Optional[dict]:
    system = LEVELING_SYSTEM_PROMPT
    user = _build_leveling_prompt(item)
    # seed はプロンプト内容の CRC32 で決定論化（ai_narrative.py と同じ流儀・3-3 月次安定性）。
    seed = zlib.crc32((system + user).encode("utf-8")) & 0x7FFFFFFF
    try:
        content = chat_json(
            system=system,
            user=user,
            model=model,
            temperature=temperature,
            max_tokens=DEFAULT_NUM_PREDICT,
            seed=seed,
        )
    except Exception as e:
        logger.warning(f"oMLX leveling 呼び出し失敗 ({item['name']}): {e}")
        return None
    result = _extract_triage_json(content, entity_name=item["name"])
    if result is None:
        return None
    result["priority"] = item["priority"]   # Python 側で強制
    return result


def narrate_leveling(items: list[dict], model: str = DEFAULT_MODEL,
                     temperature: float = DEFAULT_TEMPERATURE,
                     use_fallback: bool = True, quiet: bool = False) -> list[dict]:
    """退院平準化アイテムに narrative を付与（LLM 失敗時は定型文）。"""
    enriched = []
    for item in items:
        n = _narrate_leveling_one(item, model=model, temperature=temperature)
        item2 = dict(item)
        if n is not None:
            item2["narrative"] = n
            status = "✓"
        elif use_fallback:
            item2["narrative"] = _make_leveling_fallback(item)
            status = "fb"
        else:
            item2["narrative"] = None
            status = "—"
        if not quiet:
            print(f"    [leveling] {status} {item['name']} (再配分{item['redist']:.0f}%)")
        enriched.append(item2)
    return enriched


# ════════════════════════════════════════
# エントリポイント
# ════════════════════════════════════════

def _narrate_items(items: list[dict], use_llm_narrative: bool,
                   model: str, quiet: bool) -> list[dict]:
    """items にナラティブを付与して返す（共通処理）"""
    if not items:
        return []
    if use_llm_narrative:
        return narrate_triage(items, model=model, quiet=quiet)
    return [dict(item, narrative=_make_fallback_narrative(item)) for item in items]


def build_triage_section(adm: pd.DataFrame, surg: pd.DataFrame,
                         targets: dict, surg_targets: dict,
                         profit_monthly: Optional[pd.DataFrame],
                         base_date: pd.Timestamp,
                         model: str = DEFAULT_MODEL,
                         use_llm_narrative: bool = True,
                         quiet: bool = False) -> dict:
    """
    portal_ctx["triage"] に渡す dict を生成するエントリポイント。

    Returns:
        {"dept_internal": [...], "dept_surgery": [...], "ward": [...],
         "dept_leveling": [...], "ward_leveling": [...]}
        各リストは primary_rate < 90 の要素（primary_rate 昇順）。
        診療科は北極星KPIで内科系（在院）／外科系（全麻）に分割。
        各要素に priority バッジ・facts・narrative が付与済み。
    """
    # 診療科（北極星KPIでランク → 内科系/外科系に分割）
    dept_scored = score_departments(adm, surg, targets, surg_targets, profit_monthly, base_date)
    dept_items  = pick_targets(dept_scored, adm, base_date)
    dept_items  = _narrate_items(dept_items, use_llm_narrative, model, quiet)
    dept_internal = [d for d in dept_items if d.get("dept_type") == "internal"]
    dept_surgery  = [d for d in dept_items if d.get("dept_type") == "surgery"]

    # 病棟
    ward_scored = score_wards(adm, targets, base_date)
    ward_items  = pick_targets(ward_scored, adm, base_date)
    ward_items  = _narrate_items(ward_items, use_llm_narrative, model, quiet)

    # 退院曜日平準化（composite_rate とは別軸。閾値超過ユニットのみ。
    # 手術を大幅クリアの外科系は対象外、改善傾向は優先度を下げて評価）
    lev_dept, lev_ward = score_leveling(adm, surg, surg_targets, base_date)
    if use_llm_narrative:
        lev_dept = narrate_leveling(lev_dept, model=model, quiet=quiet)
        lev_ward = narrate_leveling(lev_ward, model=model, quiet=quiet)
    else:
        lev_dept = [dict(it, narrative=_make_leveling_fallback(it)) for it in lev_dept]
        lev_ward = [dict(it, narrative=_make_leveling_fallback(it)) for it in lev_ward]

    return {"dept_internal": dept_internal, "dept_surgery": dept_surgery,
            "ward": ward_items,
            "dept_leveling": lev_dept, "ward_leveling": lev_ward}
