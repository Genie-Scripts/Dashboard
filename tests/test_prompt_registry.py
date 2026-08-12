"""プロンプト一括管理 P0 のゲートテスト（開発プラン_プロンプト管理.md §6）。

G1 逐語一致: config/prompts.toml の text が ai_narrative.py の SYSTEM_PROMPT・proofread.py の
SYSTEM_PROMPT とバイト一致することを検証する。
"""
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib import ai_narrative, proofread

TOML_PATH = Path(__file__).resolve().parent.parent / "config" / "prompts.toml"


def _load():
    return tomllib.loads(TOML_PATH.read_text(encoding="utf-8"))


def test_g1_narrative_system_matches_constant():
    data = _load()
    assert data["narrative_system"]["text"] == ai_narrative.SYSTEM_PROMPT


def test_g1_proofread_system_matches_constant():
    data = _load()
    assert data["proofread_system"]["text"] == proofread.SYSTEM_PROMPT


def test_resolve_reads_toml_not_fallback():
    """prompt_kit が取り込めている環境では TOML 側の値を返す（フォールバックへ落ちていない）。
    Dashboard は単体/公開でも動く独立repoのため prompt_kit 未配置もあり得る（fail-open）。
    その場合はこのテストをスキップする（ai-apps monorepo 内での実行では常に取り込める）。"""
    if ai_narrative.prompt_kit is None:
        import pytest
        pytest.skip("prompt_kit 未配置環境（Dashboard 単体/公開デプロイ相当）")
    resolved = ai_narrative.prompt_kit.resolve(ai_narrative._PROMPTS_TOML, "narrative_system",
                                               fallback="__FALLBACK_SENTINEL__")
    assert resolved == ai_narrative.SYSTEM_PROMPT
    assert resolved != "__FALLBACK_SENTINEL__"
