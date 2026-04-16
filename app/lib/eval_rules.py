"""
eval_rules.py — 評価ルール定義ファイルの読み込み

config/evaluation_rules.yaml を読み込み、LLMプロンプトに注入する
テキストブロックを生成する。ファイルが無い場合は空文字を返す。
"""

from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "evaluation_rules.yaml"
_cache: Optional[dict] = None


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    if not _RULES_PATH.exists():
        logger.info("evaluation_rules.yaml が見つかりません: ルール注入をスキップ")
        _cache = {}
        return _cache
    try:
        import yaml
        raw = yaml.safe_load(_RULES_PATH.read_text(encoding="utf-8"))
        _cache = raw if isinstance(raw, dict) else {}
    except Exception as e:
        logger.warning(f"evaluation_rules.yaml 読込失敗: {e}")
        _cache = {}
    return _cache


def reload() -> None:
    """キャッシュをクリアして再読込"""
    global _cache
    _cache = None
    _load()


def _format_rules(rules: list) -> str:
    if not rules:
        return ""
    return "\n".join(f"- {r}" for r in rules if isinstance(r, str))


def _find_dept_group(dept: Optional[str], data: dict) -> Optional[dict]:
    """診療科名からグループルールを検索"""
    if not dept:
        return None
    groups = data.get("dept_group_rules", {})
    for group in groups.values():
        if isinstance(group, dict) and dept in (group.get("depts") or []):
            return group
    return None


def build_alert_context(alert: dict) -> str:
    """アラート用の追加コンテキストを生成。空なら空文字。"""
    data = _load()
    if not data:
        return ""

    parts = []

    # グローバルルール
    global_rules = data.get("global_rules", [])
    if global_rules:
        parts.append("【評価方針（全体）】")
        parts.append(_format_rules(global_rules))

    # KPIカテゴリ別ルール
    kpi_id = (alert.get("meta") or {}).get("kpi")
    kpi_rules = (data.get("kpi_rules") or {}).get(kpi_id, []) if kpi_id else []
    if kpi_rules:
        parts.append(f"\n【{kpi_id} の評価ルール】")
        parts.append(_format_rules(kpi_rules))

    # 診療科グループルール
    dept = (alert.get("meta") or {}).get("dept")
    group = _find_dept_group(dept, data)
    if group:
        group_rules = group.get("rules", [])
        if group_rules:
            parts.append(f"\n【{dept} の評価方針】")
            parts.append(_format_rules(group_rules))

    return "\n".join(parts)


def build_weekly_context() -> str:
    """週次ストーリー用の追加コンテキストを生成。空なら空文字。"""
    data = _load()
    if not data:
        return ""

    parts = []

    global_rules = data.get("global_rules", [])
    if global_rules:
        parts.append("【評価方針（全体）】")
        parts.append(_format_rules(global_rules))

    weekly_rules = data.get("weekly_story_rules", [])
    if weekly_rules:
        parts.append("\n【週次レポートの評価方針】")
        parts.append(_format_rules(weekly_rules))

    return "\n".join(parts)
