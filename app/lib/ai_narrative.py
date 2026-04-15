"""
ai_narrative.py — Ollama 経由でアラートを自然言語化

alerts.py が返した「確定事実」を受け取り、ローカルLLMで
{headline, body, action} の JSON を生成してアラートに添える。

設計原則:
    - LLMには「計算」させない。与えた事実のみを翻訳する
    - 数値を文中で再引用させない（ハルシネーション封じ）
    - 出力は JSON 強制、temperature 低め
    - Ollama 未起動・モデル未取得時は無害に失敗（narrative=None）

環境:
    依存: `pip install ollama`
    モデル取得: `ollama pull gemma3:4b`（または qwen2.5:7b 等）
"""

from __future__ import annotations
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ────────────────────────────────────
# 設定（必要に応じて上書き）
# ────────────────────────────────────
DEFAULT_MODEL = "gemma3:27b"
DEFAULT_TEMPERATURE = 0.2
DEFAULT_NUM_PREDICT = 220          # 出力トークン上限
REQUEST_TIMEOUT_SEC = 60           # 1 アラートあたりの上限


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
    facts_block = "\n".join(f"- {f}" for f in alert["facts"])
    return f"""以下の確定事実を翻訳し、JSON を1つだけ出力してください。

【アラート種別】{alert['category']}（重要度: {alert['severity']}）

【確定事実】
{facts_block}

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
        import ollama  # 遅延 import（未インストールでも他の処理に影響しない）
    except ImportError:
        logger.info("ollama パッケージが未インストール: narrative 生成をスキップ")
        return None

    try:
        res = ollama.chat(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": _build_user_prompt(alert)},
            ],
            options={
                "temperature": temperature,
                "num_predict": DEFAULT_NUM_PREDICT,
            },
            format="json",
            keep_alive="5m",
        )
    except Exception as e:
        logger.warning(f"Ollama 呼び出し失敗 ({alert['id']}): {e}")
        return None

    content = (res.get("message") or {}).get("content", "")
    return _extract_json(content)


# ────────────────────────────────────
# エントリポイント
# ────────────────────────────────────

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
