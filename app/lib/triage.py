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
from typing import Optional

import pandas as pd

from .config import (
    NADM_DISPLAY_DEPTS, SURGERY_DISPLAY_DEPTS,
    WARD_NAMES, WARD_HIDDEN,
)
from .metrics import (
    rolling7_new_admission, rolling7_surgery,
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


def _make_entity_record(name, entity_type, is_surgery,
                        adm_rate, adm_actual, adm_target,
                        inp_rate, inp_actual, inp_target,
                        op_rate, op_actual, op_target,
                        profit_rate, ward_code=None) -> Optional[dict]:
    """北極星KPI（外科系=op / それ以外=inp）でランクするレコードを生成。

    北極星KPIの達成率が測れない場合は新入院にフォールバックし
    （primary_is_fallback=True）、それも測れなければ None を返す。
    新入院・粗利はランクには用いず、文脈フィールドとして保持する。
    """
    dept_type, primary_kpi = _dept_type(is_surgery)

    primary_rate = op_rate if primary_kpi == "op" else inp_rate
    primary_is_fallback = False
    if primary_rate is None:
        primary_rate = adm_rate
        primary_is_fallback = True
    if primary_rate is None:
        return None   # 北極星も新入院も測れない → 対象外

    return {
        "name": name,
        "entity_type": entity_type,
        "dept_type": dept_type,
        "primary_kpi": primary_kpi,
        "primary_rate": round(primary_rate, 1),
        "primary_is_fallback": primary_is_fallback,
        "priority": _priority_from_rate(primary_rate),
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

    results = []
    for dept in NADM_DISPLAY_DEPTS | SURGERY_DISPLAY_DEPTS:
        is_surgery = dept in SURGERY_DISPLAY_DEPTS

        adm_actual  = r7_nadm["by_dept"].get(dept, 0)
        adm_target  = nadm_tgt.get(dept)
        inp_actual  = inp_by_dept.get(dept, 0)
        inp_target  = inp_tgt.get(dept)
        op_actual   = r7_surg["by_dept"].get(dept, 0) if is_surgery else None
        op_target   = surg_targets.get(dept) if is_surgery else None
        profit_rate = profit_rates.get(dept)

        adm_rate    = achievement_rate(adm_actual, adm_target)
        inp_rate    = achievement_rate(inp_actual, inp_target)
        op_rate     = achievement_rate(op_actual, op_target) if is_surgery else None

        rec = _make_entity_record(
            name=dept, entity_type="dept", is_surgery=is_surgery,
            adm_rate=adm_rate, adm_actual=adm_actual, adm_target=adm_target,
            inp_rate=inp_rate, inp_actual=inp_actual, inp_target=inp_target,
            op_rate=op_rate, op_actual=op_actual, op_target=op_target,
            profit_rate=profit_rate,
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

        rec = _make_entity_record(
            name=wname, entity_type="ward", is_surgery=False,
            adm_rate=adm_rate, adm_actual=adm_actual, adm_target=adm_target,
            inp_rate=inp_rate, inp_actual=inp_actual, inp_target=inp_target,
            op_rate=None, op_actual=None, op_target=None,
            profit_rate=None, ward_code=wcode,
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
    primary_rate < PRIMARY_THRESHOLD の科/病棟を抽出し、北極星KPIを先頭にした
    facts / kpi_summary と WoW ヒントを付与して返す。
    """
    items = [s for s in scored if s["primary_rate"] < PRIMARY_THRESHOLD]

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
            op_fact = (f"全身麻酔手術（直近7日）: 実績{item['op_actual']:.0f}件 / "
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
            op_kpi = f"全麻{item['op_rate']:.0f}%({_gap_s(item.get('op_gap', 0) or 0)}件)"

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
        if is_ward:
            item["href"] = "detail.html#inpatient?axis=ward"
        else:
            item["href"] = f"dept.html#{item['name']}"

    return items


# ════════════════════════════════════════
# LLM ナラティブ
# ════════════════════════════════════════

def _build_triage_prompt(item: dict) -> str:
    facts_block = "\n".join(f"- {f}" for f in item["facts"])
    wow_line = f"\n・前週同曜日比: {item['wow_hint']}" if item.get("wow_hint") else ""
    strong_note = ("\n\n【重要】この科は全身麻酔手術が目標を大きく上回っている。"
                   "手術実績を主軸に肯定的に評価し、在院患者数や退院曜日の偏りは"
                   "強く指摘しないこと。") if item.get("surgery_strong") else ""
    entity_label = item.get("entity_label", "科")
    goal_map = {
        "inp": "在院患者数の増加（レバー: 新入院・紹介患者の確保）",
        "op":  "全身麻酔手術件数の増加（レバー: 手術枠の活用・予約調整）",
    }
    goal_line = goal_map.get(item.get("primary_kpi"), "")
    total_label = f"全{item['total_items']}{entity_label}" if "total_items" in item else f"全{item.get('total_depts', '?')}科"
    return f"""以下の確定事実を要約し、JSON を1つだけ出力してください。

【{item.get('entity_label', '診療科')}】{item['name']}（下位{item['rank_from_bottom']}位 / {total_label}）
【優先度】{item['priority']}
【この{entity_label}の目標KPI】{goal_line}

【確定事実】
{facts_block}{wow_line}{strong_note}

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


def _make_fallback_narrative(item: dict) -> dict:
    """LLM 失敗時の Python 定型文 fallback（北極星KPI主体）"""
    # 北極星KPIが測れずフォールバック中の科は新入院を主題にする
    if item.get("primary_is_fallback"):
        kpi, key = "新入院", "adm"
    elif item.get("primary_kpi") == "op":
        kpi, key = "全身麻酔手術", "op"
    else:
        kpi, key = "在院患者数", "inp"
    return {
        "priority":    item["priority"],
        "headline":    f"{kpi}が目標未達",
        "observation": f"{kpi}が目標を下回っています",
        "suggestion":  FALLBACK_SUGGESTIONS[key],
    }


def _narrate_one(item: dict, model: str, temperature: float) -> Optional[dict]:
    """単一科を LLM で翻訳。失敗時は None"""
    try:
        content = chat_json(
            system=TRIAGE_SYSTEM_PROMPT,
            user=_build_triage_prompt(item),
            model=model,
            temperature=temperature,
            max_tokens=DEFAULT_NUM_PREDICT,
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
    for dept in sorted(NADM_DISPLAY_DEPTS | SURGERY_DISPLAY_DEPTS):
        # 手術目標を大幅にクリアしている外科系は退院曜日集中をうるさく言わない
        if dept in SURGERY_DISPLAY_DEPTS:
            s_rate = achievement_rate(r7_surg.get(dept, 0), surg_targets.get(dept))
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
    try:
        content = chat_json(
            system=LEVELING_SYSTEM_PROMPT,
            user=_build_leveling_prompt(item),
            model=model,
            temperature=temperature,
            max_tokens=DEFAULT_NUM_PREDICT,
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
