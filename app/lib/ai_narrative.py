"""
ai_narrative.py — oMLX(OpenAI互換) 経由でアラートを自然言語化

alerts.py が返した「確定事実」を受け取り、ローカルLLMで
{headline, body, action} の JSON を生成してアラートに添える。

設計原則:
    - LLMには「計算」させない。与えた事実のみを翻訳する
    - 数値を文中で再引用させない（ハルシネーション封じ）
    - 出力は JSON 強制、temperature 低め
    - oMLX 未起動・モデル未取得時は無害に失敗（narrative=None）

環境:
    依存: `pip install openai`
    モデル: 環境変数 OMLX_MODEL（既定 Llama-3.1-Swallow-8B-Instruct-v0.5）
"""

from __future__ import annotations
import json
import logging
from typing import Optional

from .llm import DEFAULT_MODEL, chat_json

logger = logging.getLogger(__name__)


# ────────────────────────────────────
# 設定（必要に応じて上書き）
# ────────────────────────────────────
# 使用モデルは llm.DEFAULT_MODEL（環境変数 OMLX_MODEL で deploy.sh と一元管理）
DEFAULT_TEMPERATURE = 0.2
DEFAULT_NUM_PREDICT = 220          # 出力トークン上限


SYSTEM_PROMPT = """あなたは病院経営会議向けの要約ライターです。以下を厳守してください。

【厳守事項】
1. 与えられた事実のみを使い、新しい数値や事実を追加しない
2. 本文に具体的な数値を再引用しない（「上昇」「悪化」「目標未達」等の定性語を使う）
3. 推測・仮定・原因断定はしない
4. 出力は指定 JSON スキーマのみ。前置きや説明文を付けない
5. 日本語、簡潔・丁寧・事務的なトーン

【出力スキーマ】
{
  "headline": "20字以内の見出し（体言止め可）",
  "body": "事実を述べる本文 60〜90字（理事会で読み上げ可能な丁寧な日本語）",
  "action": "推奨アクション 50〜80字（具体的・実行可能）"
}"""


def _build_user_prompt(alert: dict) -> str:
    from .eval_rules import build_alert_context
    facts_block = "\n".join(f"- {f}" for f in alert["facts"])
    context = build_alert_context(alert)
    context_block = f"\n\n{context}" if context else ""
    return f"""以下の確定事実を翻訳し、JSON を1つだけ出力してください。

【アラート種別】{alert['category']}（重要度: {alert['severity']}）

【確定事実】
{facts_block}
{context_block}
【注意】
- headline/body/action の3キーを持つ JSON を出力すること
- 事実にない内容（具体数値、原因、人物）を補わないこと
- JSON 以外の文字（```、前置き、末尾コメント）を出力しないこと"""


def _extract_json(text: str) -> Optional[dict]:
    """LLM 出力から JSON オブジェクトを取り出す（前後のゴミに強い）"""
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0 or end <= start:
        return None
    snippet = text[start:end + 1]
    try:
        obj = json.loads(snippet)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    if not all(k in obj for k in ("headline", "body", "action")):
        return None
    return {
        "headline": str(obj["headline"]).strip(),
        "body": str(obj["body"]).strip(),
        "action": str(obj["action"]).strip(),
    }


def _narrate_one(alert: dict, model: str, temperature: float) -> Optional[dict]:
    """単一アラートを LLM で翻訳"""
    try:
        content = chat_json(
            system=SYSTEM_PROMPT,
            user=_build_user_prompt(alert),
            model=model,
            temperature=temperature,
            max_tokens=DEFAULT_NUM_PREDICT,
        )
    except Exception as e:
        # oMLX 未起動 / openai 未インストール / モデル未取得 すべてここで無害に縮退
        logger.warning(f"oMLX 呼び出し失敗 ({alert['id']}): {e}")
        return None

    return _extract_json(content)


# ────────────────────────────────────
# エントリポイント
# ────────────────────────────────────

# ────────────────────────────────────
# 週末のびしろ（平準化アクション）の「今週の一手」
# ────────────────────────────────────
# ねらい＝週末(土日)の在院維持＝タイミングの平準化。金曜に集中した退院を平日へ
# 分散し、週末入院で空床を補充する（ベッド回転）。在院日数の延長＝月曜延伸は禁止。
LEVELING_ACTION_SYSTEM_PROMPT = """あなたは病院の病床管理を支援する要約ライターです。各部門の「週末（土日）に在院が落ち込む状況」への“今週の一手”を、与えられた事実だけから日本語で書きます。以下を厳守してください。

【厳守事項】
1. 与えられた事実のみを使う。新しい数値・原因・固有名を足さない。本文に数値を再引用しない（「集中」「低下」等の定性語を使う）。
2. ねらいは週末の在院維持＝タイミングの平準化。具体策は「金曜に集中している退院を月〜木へ分散」＋「週末（土日）の入院受け入れで空床を補充」。
3. 在院日数の延長（退院を月曜まで遅らせる＝月曜延伸）や早期退院の促進は提案しない（禁止）。狙いはベッド回転であって延伸ではない。
4. 診療科は患者の退院曜日と予定入院の曜日設計がレバー（床は持たない）。病棟は相乗り科の退院曜日の交通整理と週末入院の受け入れがレバー。
5. 出力は指定 JSON のみ。前置き・説明・``` を付けない。簡潔・丁寧・事務的なトーン。

【出力スキーマ】
{
  "body": "週末在院の状況を述べる本文 50〜80字（数値を使わない定性的記述）",
  "action": "今週の一手 40〜70字（金曜退院の平日分散＋週末入院補充。延伸は書かない）"
}"""


def _q_retention(r):
    if r is None: return "不明"
    if r >= 0.90: return "おおむね保てている"
    if r >= 0.86: return "やや低下している"
    return "明確に低下している"


def _q_room(room, max_room):
    frac = (room / max_room) if max_room else 0
    if frac >= 0.66: return "大きい"
    if frac >= 0.33: return "中程度"
    return "小さい"


def _q_delta(d):
    if d is None: return "横ばい"
    if d > 0.5: return "週末の落ち込みが拡大（悪化傾向）"
    if d < -0.5: return "改善傾向"
    return "横ばい"


def _q_friday(dd):
    """退院の金曜集中度（dow_unit_detail の discharge.avg[月..日]）"""
    if not dd or not dd.get("discharge", {}).get("avg"):
        return None
    avg = dd["discharge"]["avg"]
    tot = sum(avg)
    if tot <= 0: return None
    share = avg[4] / tot
    if share >= 0.22: return "金曜への退院集中が強い"
    if share >= 0.16: return "金曜にやや集中している"
    return "曜日は比較的平準"


def _q_weekend_adm(dd):
    """週末入院での補充の有無（admission.avg の土日 vs 平日）"""
    if not dd or not dd.get("admission", {}).get("avg"):
        return None
    a = dd["admission"]["avg"]
    wd = sum(a[0:5]) / 5 if len(a) >= 5 else 0
    we = sum(a[5:7]) / 2 if len(a) >= 7 else 0
    if wd <= 0: return None
    return "週末入院での補充はある程度ある" if we >= wd * 0.6 else "週末入院での補充が乏しい"


def _build_leveling_prompt(unit: dict, entity: str, max_room: float, dd: Optional[dict]) -> str:
    label = "診療科" if entity == "dept" else "病棟"
    facts = [
        f"週末在院の維持: {_q_retention(unit.get('retention'))}",
        f"取り戻せる在院（のびしろ）の大きさ: {_q_room(unit.get('room_per_week', 0), max_room)}",
        f"直近4週の傾向: {_q_delta(unit.get('room_delta_4w'))}",
    ]
    fri = _q_friday(dd)
    if fri: facts.append(f"退院の曜日: {fri}")
    adm = _q_weekend_adm(dd)
    if adm: facts.append(f"週末入院での空床補充: {adm}")
    facts_block = "\n".join(f"- {f}" for f in facts)
    # レバーは事実に適応：金曜集中なら退院分散、補充が乏しければ週末入院強化を主にする
    fri_strong = bool(fri) and "強い" in fri
    adm_weak = bool(adm) and "乏しい" in adm
    disperse = ("金曜に集中した退院を月〜木へ分散する"
                if entity == "dept" else "相乗り科の金曜退院を平日へ分散する")
    refill = ("予定入院を週後半（木金）へ寄せ、週末の空床を埋める"
              if entity == "dept" else "週末の入院受け入れを強化して空床を埋める")
    if fri_strong and not adm_weak:
        lever = f"{disperse}（退院の平準化を主に）。"
    elif adm_weak and not fri_strong:
        lever = f"{refill}（週末入院による補充を主に）。退院曜日は比較的平準なので退院分散は強調しない。"
    else:
        lever = f"{disperse}。あわせて{refill}。"
    return f"""以下の事実から、{label}「{unit['name']}」の“今週の一手”を JSON で1つだけ出力してください。

【対象】{label}: {unit['name']}
【週末(土日)在院の状況】
{facts_block}

【書き方】
- body=週末在院の状況の要約（数値を使わない定性的記述）。
- action=今週の一手。レバーの軸: {lever}
- 月曜延伸・早期退院は禁止。JSON 以外（```・前置き・末尾コメント）を出力しない。"""


def _extract_body_action(text: str) -> Optional[dict]:
    """LLM 出力から {body, action} を取り出す"""
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "body" not in obj or "action" not in obj:
        return None
    return {"body": str(obj["body"]).strip(), "action": str(obj["action"]).strip()}


def narrate_leveling_actions(weekend_leveling: dict,
                             dow_unit_detail: Optional[dict] = None,
                             top_n: int = 6,
                             model: str = DEFAULT_MODEL,
                             temperature: float = DEFAULT_TEMPERATURE,
                             quiet: bool = False) -> dict:
    """週末のびしろ payload の各エンティティについて、のびしろ上位 top_n ユニットに
    `narrative`={body, action}（or None）を付与する（破壊的更新して返す）。

    - 上位以外は narrative を持たず、フロント側がテンプレート定型文で代替表示する。
    - oMLX 未起動/モデル未取得時は全て None。例外は投げない（無害縮退）。
    - 数値はフロント（KPI/ランキング/曜日棒）が表示し、LLM は定性的な一手のみ。
    """
    if not weekend_leveling:
        return weekend_leveling
    for entity in ("dept", "ward"):
        block = weekend_leveling.get(entity) or {}
        units = block.get("units") or []
        if not units:
            continue
        max_room = max((u.get("room_per_week", 0) for u in units), default=1) or 1
        det = (dow_unit_detail or {}).get(entity, {}) or {}
        targets = sorted(units, key=lambda u: u.get("room_per_week", 0), reverse=True)[:top_n]
        for u in targets:
            try:
                content = chat_json(
                    system=LEVELING_ACTION_SYSTEM_PROMPT,
                    user=_build_leveling_prompt(u, entity, max_room, det.get(u["name"])),
                    model=model, temperature=temperature, max_tokens=DEFAULT_NUM_PREDICT,
                )
                u["narrative"] = _extract_body_action(content)
            except Exception as e:
                logger.warning(f"oMLX 呼び出し失敗 (leveling {entity}:{u['name']}): {e}")
                u["narrative"] = None
            if not quiet:
                print(f"    [AI] {'✓' if u.get('narrative') else '—'} leveling {entity}:{u['name']}")
    return weekend_leveling


def narrate_alerts(alerts: list[dict],
                    model: str = DEFAULT_MODEL,
                    temperature: float = DEFAULT_TEMPERATURE,
                    quiet: bool = False) -> list[dict]:
    """
    各アラートに `narrative` フィールド（dict or None）を付与して返す。

    - narrative が None のアラートは、テンプレート側で title_fallback と
      facts を使って代替表示する前提。
    - LLM 未起動時は全て None になるが、例外は投げない。
    """
    if not alerts:
        return alerts

    enriched = []
    for a in alerts:
        n = _narrate_one(a, model=model, temperature=temperature)
        a2 = dict(a)
        a2["narrative"] = n
        enriched.append(a2)
        if not quiet:
            status = "✓" if n else "—"
            print(f"    [AI] {status} {a['id']}")
    return enriched
