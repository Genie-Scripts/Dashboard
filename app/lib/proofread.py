"""proofread.py — レビューHTMLの編集文（一手 body/action）の誤字脱字校正。

app/lib/ai_narrative.py の narrate_* 系（新規文の生成）とは別の用途で、既に確定した
一文の誤字脱字・助詞・変換ミスのみを直す軽量校正。数値・固有名詞・専門用語・文体・意味を
変えないことが最優先のため、LLMの改変が大きすぎる場合は結果を破棄して元文へ fail-soft する。

設計原則:
    - LLM呼び出しは app/lib/llm.py の chat_json を再利用（model 未指定時は llm.DEFAULT_MODEL
      経由で data/model_override.json が自動で効く）
    - temperature=0.0・seed=crc32(text) で決定論化（同じ文は常に同じ校正結果）
    - 機械ガード（数字列不一致・長さ比異常）に1つでも違反したら破棄する
"""
from __future__ import annotations

import json
import re
import zlib
from typing import Optional

from .llm import DEFAULT_MODEL, chat_json

SYSTEM_PROMPT = """あなたは病院運営レポートの一文の校正者です。誤字脱字・助詞・変換ミスのみを
修正してください。数値・固有名詞・専門用語・文体・意味は変更しないでください。
出力は JSON {"text": "<修正後の文>"} のみとし、前置きや説明文は付けないでください。
修正が不要な場合は元の文をそのまま "text" に入れて返してください。"""

_DIGIT_RE = re.compile(r"\d+(?:\.\d+)?")
_WS_RE = re.compile(r"\s+")

_MIN_LEN_RATIO = 0.5
_MAX_LEN_RATIO = 1.6


def _extract_text(content: str) -> Optional[str]:
    """LLM 出力から {"text": ...} を取り出す（前後のゴミに強い局所実装）。"""
    if not content:
        return None
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(content[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "text" not in obj:
        return None
    return str(obj["text"])


def _normalize(text: str) -> str:
    """改行・連続空白を単一スペースに正規化し strip する（overrides.md は1行フォーマットのため）。"""
    return _WS_RE.sub(" ", text).strip()


def proofread_text(text: str, model: Optional[str] = None) -> dict:
    """一文を誤字脱字校正する。

    数値列が変わった／長さが大きく変わった等、機械ガードに違反する結果は破棄して
    元文をそのまま返す（fail-soft）。

    戻り値: {"text": str, "changed": bool, "error": str | None}
    """
    if not text or not text.strip():
        return {"text": text, "changed": False, "error": "空文字"}

    model = model or DEFAULT_MODEL
    seed = zlib.crc32(text.encode("utf-8"))

    try:
        content = chat_json(SYSTEM_PROMPT, text, model,
                            temperature=0.0, max_tokens=512, seed=seed)
    except Exception:  # noqa: BLE001 — oMLX未起動・タイムアウト等すべて無害に縮退
        return {"text": text, "changed": False, "error": "校正LLM呼び出し失敗"}

    fixed = _extract_text(content)
    if fixed is None:
        return {"text": text, "changed": False, "error": "校正LLM応答の解析失敗"}

    fixed = _normalize(fixed)
    if not fixed:
        return {"text": text, "changed": False, "error": "校正結果が空文字"}

    if _DIGIT_RE.findall(text) != _DIGIT_RE.findall(fixed):
        return {"text": text, "changed": False, "error": "数値が変更されたため破棄"}

    ratio = len(fixed) / len(text)
    if ratio < _MIN_LEN_RATIO or ratio > _MAX_LEN_RATIO:
        return {"text": text, "changed": False, "error": "文長が大きく変化したため破棄"}

    if fixed == _normalize(text):
        return {"text": text, "changed": False, "error": None}
    return {"text": fixed, "changed": True, "error": None}
