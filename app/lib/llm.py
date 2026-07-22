"""
llm.py — oMLX(OpenAI互換) 共通クライアント

旧構成では各 lib が `ollama.chat(...)` を直接呼んでいたが、要約LLMを
oMLX(OpenAI互換 /v1) に統一。oMLX はホストの 127.0.0.1:8000 で動作する
（Dashboard は非Docker・ホストの python 実行なので localhost で到達）。

設計原則（呼び出し側の従来挙動を維持）:
    - JSON 抽出はしない。生の content 文字列を返し、各 lib の _extract_* に委ねる
    - 例外はそのまま送出 → 呼び出し側が except で None フォールバックする
      （oMLX 未起動・openai 未インストール・モデル未取得 すべて無害に縮退）

環境変数（deploy.sh と一元管理）:
    OMLX_MODEL     使用モデル（既定: 日本語軽量の Swallow-8B）
    OMLX_BASE_URL  既定 http://localhost:8000/v1
    OMLX_API_KEY   既定 sk-ant-omlx-local-key（~/.omlx/settings.json の auth.api_key）
    OMLX_TIMEOUT   1リクエストの上限秒（既定 180）
"""

from __future__ import annotations
import contextlib
import os
import sys

# 協調層（モデル常駐競合の507を「待ち」に変える・業務ハブと共通のモデル管理）。
# ai-apps monorepo 内で実行されるときだけ root の genie_llm を取り込む。単体/公開 Dashboard
# として動かす場合は未配置→fail-open で従来どおり（協調なしの直呼び）動作する。
try:
    _ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    import genie_llm
except Exception:  # noqa: BLE001
    genie_llm = None

def _resolve_default_model() -> str:
    """使用モデルの解決順: 業務ハブのモデルパネルが書く override → OMLX_MODEL env → 既定。
    override は orchestrator が Dashboard/data/model_override.json に書く（他ツールと同じ方式）。
    未配置/未読なら従来どおり env・既定にフォールバック（fail-open）。"""
    try:
        import json as _json
        _dash = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # Dashboard/
        with open(os.path.join(_dash, "data", "model_override.json"), encoding="utf-8") as _f:
            _m = _json.load(_f).get("model")
        if _m:
            return _m
    except Exception:  # noqa: BLE001
        pass
    return os.environ.get("OMLX_MODEL", "Llama-3.1-Swallow-8B-Instruct-v0.5")


DEFAULT_MODEL = _resolve_default_model()
BASE_URL = os.environ.get("OMLX_BASE_URL", "http://localhost:8000/v1")
API_KEY = os.environ.get("OMLX_API_KEY", "sk-ant-omlx-local-key")
# 既定 60→180（2026-07 並列実行導入時に引き上げ）: 並列実行下では個別リクエストの
# レイテンシが数倍に伸びる（実測で単独比 約3.8倍）。60秒のままだとタイムアウト例外が
# ai_narrative.REJECT_STATS["error"] を経て静かに定型文フォールバックへ縮退し、
# 「速くなった代わりに文章の質が落ちる」結果になるため。環境変数での上書きは従来どおり効く。
TIMEOUT_SEC = float(os.environ.get("OMLX_TIMEOUT", "180"))

_client = None


def _get_client():
    """openai クライアントを遅延生成（未インストールなら ImportError を送出）。"""
    global _client
    if _client is None:
        from openai import OpenAI  # 遅延 import: 未導入でも import 時には落とさない
        _client = OpenAI(base_url=BASE_URL, api_key=API_KEY, timeout=TIMEOUT_SEC)
    return _client


def chat_json(system: str, user: str, model: str,
              temperature: float = 0.2, max_tokens: int = 256,
              seed: int = None) -> str:
    """system/user を渡し、アシスタント応答の content 文字列を返す。

    旧 ollama の `format="json"` 相当として response_format(json_object) を付ける。
    モデル/サーバが未対応で弾く場合に備え、一度だけ response_format 無しで再試行する
    （プロンプト側で JSON 指定済みのため、_extract_* が後段で吸収する）。

    seed: oMLX は OpenAI 互換の seed に対応（実測 2026-07-04: 同一seed→同一出力）。
    呼び出し側がプロンプト内容から決定論的に与えると「同じ事実→同じ文」の再現性が得られる。
    None なら従来どおり非決定論。
    """
    client = _get_client()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    extra = {} if seed is None else {"seed": seed}
    # 協調層: focus を取ってからロードし、業務ハブの重い並行要求（会議議事録80B 等）と
    # 直列化して 507 を避ける。Dashboard の生成は基本バッチなので priority=batch。
    _coord = genie_llm.session(model, priority="batch") if genie_llm else contextlib.nullcontext()
    with _coord:
        try:
            res = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
                **extra,
            )
        except Exception:
            # response_format 非対応モデル等へのフォールバック（接続不能ならここでも送出される）
            res = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                **extra,
            )
    return res.choices[0].message.content or ""
