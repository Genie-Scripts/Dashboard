"""
triage.py — 部門トリアージ（多KPI合成スコアリング + LLMナラティブ）

設計原則（alerts.py / ai_narrative.py / weekly_story.py と同様）:
    - 数値計算・ランキングは Python で確定
    - LLM には翻訳（事実→自然文）のみを任せる
    - Ollama 未起動時は無害に失敗（Python 生成の fallback を使用）

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
)
from .metrics import (
    rolling7_new_admission, rolling7_surgery,
    daily_inpatient, build_daily_series, week_over_week,
    achievement_rate,
)

logger = logging.getLogger(__name__)

# ────────────────────────────────────
# 設定
# ────────────────────────────────────
DEFAULT_MODEL = "MedAIBase/MedGemma1.5:4b"
DEFAULT_TEMPERATURE = 0.2
DEFAULT_NUM_PREDICT = 200
REQUEST_TIMEOUT_SEC = 60

COMPOSITE_THRESHOLD = 90.0   # composite_rate < 90 → トリアージ対象
PRIORITY_HIGH_THRESHOLD = 80.0
PRIORITY_MID_THRESHOLD  = 90.0

# 重み
WEIGHT_ADM    = 1.1
WEIGHT_INP    = 1.0
WEIGHT_OP     = 1.3
WEIGHT_PROFIT = 0.8

# fallback 文言（未達 KPI ごと）
FALLBACK_SUGGESTIONS = {
    "adm":    "新入院の目標設定・運用の再確認を推奨します",
    "inp":    "病床利用状況・退院調整の点検を推奨します",
    "op":     "手術枠の利用状況と予約調整の確認を推奨します",
    "profit": "収益構造の再レビューを推奨します",
}

TRIAGE_SYSTEM_PROMPT = """あなたは病院経営会議向けの要約ライターです。以下を厳守してください。

【厳守事項】
1. 与えられた確定事実のみを使い、新しい数値・事実・原因を追加しない
2. 推測・仮定・原因断定はしない
3. 特定の人名・治療方針・診療行為を記述しない
4. facts に存在する観点にのみ具体提案を許す
5. 改善傾向がある KPI には肯定的な言及を加える
6. 出力は指定 JSON スキーマのみ。前置きや説明文を付けない
7. 日本語、簡潔・丁寧・事務的なトーン
8. 「合成達成率」という語句およびその数値（パーセント）を
   observation / suggestion に出力しない。
   個別KPI（新入院・在院患者・手術・粗利）の達成状況で表現すること。

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


def score_departments(adm: pd.DataFrame, surg: pd.DataFrame,
                      targets: dict, surg_targets: dict,
                      profit_monthly: Optional[pd.DataFrame],
                      base_date: pd.Timestamp) -> list[dict]:
    """
    全科の多KPI合成達成率を計算して返す。

    Returns:
        list of {name, composite_rate, priority,
                 adm_rate, inp_rate, op_rate, profit_rate,
                 adm_actual, adm_target, inp_actual, inp_target,
                 op_actual, op_target, is_surgery_dept}
        composite_rate 昇順（低い順）
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

        # 合成スコア計算（欠損 KPI は重みから除外）
        weighted_sum = 0.0
        weight_total = 0.0
        if adm_rate is not None:
            weighted_sum += adm_rate * WEIGHT_ADM
            weight_total += WEIGHT_ADM
        if inp_rate is not None:
            weighted_sum += inp_rate * WEIGHT_INP
            weight_total += WEIGHT_INP
        if is_surgery and op_rate is not None:
            weighted_sum += op_rate * WEIGHT_OP
            weight_total += WEIGHT_OP
        if profit_rate is not None:
            weighted_sum += profit_rate * WEIGHT_PROFIT
            weight_total += WEIGHT_PROFIT

        if weight_total == 0:
            continue

        composite_rate = weighted_sum / weight_total

        # priority 決定（Python 確定）
        if composite_rate < PRIORITY_HIGH_THRESHOLD:
            priority = "high"
        elif composite_rate < PRIORITY_MID_THRESHOLD:
            priority = "mid"
        else:
            priority = "low"

        results.append({
            "name": dept,
            "composite_rate": round(composite_rate, 1),
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
            "op_target": round(float(op_target), 1) if op_target else None,
            "profit_rate": profit_rate,
        })

    results.sort(key=lambda x: x["composite_rate"])
    return results


# ════════════════════════════════════════
# 対象抽出 + facts 生成
# ════════════════════════════════════════

def pick_targets(scored: list[dict], adm: pd.DataFrame,
                 base_date: pd.Timestamp) -> list[dict]:
    """
    composite_rate < COMPOSITE_THRESHOLD の科を抽出し、
    facts 配列 + WoW ヒントを付与して返す。
    """
    items = [s for s in scored if s["composite_rate"] < COMPOSITE_THRESHOLD]

    for i, item in enumerate(items):
        dept = item["name"]

        # facts 生成
        facts = []
        if item["adm_rate"] is not None and item["adm_target"] is not None:
            facts.append(
                f"新入院（直近7日）: 実績{item['adm_actual']:.0f}人 / "
                f"目標{item['adm_target']:.1f}人（達成率{item['adm_rate']:.0f}%）"
            )
        if item["inp_rate"] is not None and item["inp_target"] is not None:
            facts.append(
                f"在院患者: 実績{item['inp_actual']:.0f}人 / "
                f"目標{item['inp_target']:.1f}人（達成率{item['inp_rate']:.0f}%）"
            )
        if (item["is_surgery_dept"] and item["op_rate"] is not None
                and item["op_target"] is not None):
            facts.append(
                f"手術（直近7日）: 実績{item['op_actual']:.0f}件 / "
                f"目標{item['op_target']:.1f}件（達成率{item['op_rate']:.0f}%）"
            )
        if item["profit_rate"] is not None:
            facts.append(f"粗利: 達成率{item['profit_rate']:.0f}%")

        # WoW ヒント（新入院前週比）
        wow_hint = None
        try:
            s = build_daily_series(adm, "新入院患者数", group_col="診療科名", group_val=dept)
            wow = week_over_week(s, base_date)
            if wow is not None:
                wow_hint = f"新入院が前週比{wow:+.0f}人"
        except Exception:
            pass

        item["rank_from_bottom"] = i + 1
        item["total_depts"] = len(items)
        item["facts"] = facts
        item["wow_hint"] = wow_hint
        item["narrative"] = None   # LLM で後から付与
        item["href"] = f"dept.html#{dept}"

    return items


# ════════════════════════════════════════
# LLM ナラティブ
# ════════════════════════════════════════

def _build_triage_prompt(item: dict) -> str:
    facts_block = "\n".join(f"- {f}" for f in item["facts"])
    wow_line = f"\n・前週同曜日比: {item['wow_hint']}" if item.get("wow_hint") else ""
    return f"""以下の確定事実を要約し、JSON を1つだけ出力してください。

【診療科】{item['name']}（下位{item['rank_from_bottom']}位 / 全{item['total_depts']}科）
【優先度】{item['priority']}

【確定事実】
{facts_block}{wow_line}

【注意】
- priority は必ず "{item['priority']}" を出力すること（Python で再検証する）
- headline / observation / suggestion / priority の4キーを持つ JSON を出力すること
- 「合成達成率」という語句・その数値は出力しないこと
- 事実にない数値・原因・人物を補わないこと
- JSON 以外の文字（```、前置き、末尾コメント）を出力しないこと"""


def _sanitize_narrative_text(text: str) -> str:
    """合成スコア数値の露出を防ぐ後処理（多重防衛・層3）"""
    import re
    # "合成達成率XX%" / "合成達成率 XX %" 等を除去
    text = re.sub(r'合成達成率\s*[\d.]+\s*%', '', text)
    # "総合スコアXX%" / "合成スコアXX%" 等のバリエーションも除去
    text = re.sub(r'(総合|合成)[スコア達成率]*\s*[\d.]+\s*%', '', text)
    return text.strip()


def _extract_triage_json(text: str) -> Optional[dict]:
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
        "headline":    _sanitize_narrative_text(str(obj["headline"])),
        "observation": _sanitize_narrative_text(str(obj["observation"])),
        "suggestion":  _sanitize_narrative_text(str(obj["suggestion"])),
    }


def _make_fallback_narrative(item: dict) -> dict:
    """LLM 失敗時の Python 定型文 fallback（合成スコアを表に出さない）"""
    # 未達 KPI を列挙して observation を生成
    underfulfilled = []
    suggestions = []
    for fact in item["facts"]:
        if "新入院" in fact:
            underfulfilled.append("新入院")
            suggestions.append(FALLBACK_SUGGESTIONS["adm"])
        elif "在院" in fact:
            underfulfilled.append("在院患者")
            suggestions.append(FALLBACK_SUGGESTIONS["inp"])
        elif "手術" in fact:
            underfulfilled.append("手術")
            suggestions.append(FALLBACK_SUGGESTIONS["op"])
        elif "粗利" in fact:
            underfulfilled.append("粗利")
            suggestions.append(FALLBACK_SUGGESTIONS["profit"])

    kpi_list = "・".join(dict.fromkeys(underfulfilled)) or "複数KPI"
    observation = f"{kpi_list}で目標を下回っています"
    suggestion = "、".join(dict.fromkeys(suggestions)) or "目標達成に向けた状況確認を推奨します"
    return {
        "priority":    item["priority"],
        "headline":    f"{item['name']}の目標未達",
        "observation": observation,
        "suggestion":  suggestion,
    }


def _narrate_one(item: dict, model: str, temperature: float) -> Optional[dict]:
    """単一科を LLM で翻訳。失敗時は None"""
    try:
        import ollama
    except ImportError:
        logger.info("ollama 未インストール: triage narrative をスキップ")
        return None
    try:
        res = ollama.chat(
            model=model,
            messages=[
                {"role": "system", "content": TRIAGE_SYSTEM_PROMPT},
                {"role": "user",   "content": _build_triage_prompt(item)},
            ],
            options={
                "temperature": temperature,
                "num_predict": DEFAULT_NUM_PREDICT,
            },
            format="json",
            keep_alive="5m",
        )
    except Exception as e:
        logger.warning(f"Ollama triage 呼び出し失敗 ({item['name']}): {e}")
        return None

    content = (res.get("message") or {}).get("content", "")
    result = _extract_triage_json(content)
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
    - Ollama 未起動時は全科 fallback（例外は投げない）
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
            print(f"    [triage] {status} {item['name']} ({item['composite_rate']:.0f}%)")
        enriched.append(item2)
    return enriched


# ════════════════════════════════════════
# エントリポイント
# ════════════════════════════════════════

def build_triage_section(adm: pd.DataFrame, surg: pd.DataFrame,
                         targets: dict, surg_targets: dict,
                         profit_monthly: Optional[pd.DataFrame],
                         base_date: pd.Timestamp,
                         model: str = DEFAULT_MODEL,
                         use_llm_narrative: bool = True,
                         quiet: bool = False) -> list[dict]:
    """
    portal_ctx["triage"] に渡すリストを生成するエントリポイント。

    Returns:
        composite_rate < 90 の科リスト（composite_rate 昇順）。
        各要素に priority バッジ・facts・narrative が付与済み。
    """
    scored = score_departments(adm, surg, targets, surg_targets, profit_monthly, base_date)
    items  = pick_targets(scored, adm, base_date)

    if not items:
        return []

    if use_llm_narrative:
        items = narrate_triage(items, model=model, quiet=quiet)
    else:
        # fallback のみ付与
        items = [dict(item, narrative=_make_fallback_narrative(item)) for item in items]

    return items
