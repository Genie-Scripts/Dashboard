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


# ── 今期の経営方針（業務方向性）: data/management_policy.yaml（gitignore・院内非公開）──
# evaluation_rules.yaml は PUBLIC リポにコミットされるため、機密の経営判断はここに書かず、
# data/ 側（非公開）へ置いて読み込み時にマージする。テンプレートは config/management_policy.example.yaml。
_POLICY_PATH = Path(__file__).resolve().parents[2] / "data" / "management_policy.yaml"
_policy_cache: Optional[dict] = None


def _load_policy() -> dict:
    global _policy_cache
    if _policy_cache is not None:
        return _policy_cache
    if not _POLICY_PATH.exists():
        _policy_cache = {}
        return _policy_cache
    try:
        import yaml
        raw = yaml.safe_load(_POLICY_PATH.read_text(encoding="utf-8"))
        _policy_cache = raw if isinstance(raw, dict) else {}
    except Exception as e:
        logger.warning(f"management_policy.yaml 読込失敗: {e}")
        _policy_cache = {}
    return _policy_cache


def reload() -> None:
    """キャッシュをクリアして再読込"""
    global _cache, _policy_cache
    _cache = None
    _policy_cache = None
    _load()
    _load_policy()


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


# dept_group_rules のキー → プロンプトに書く群の呼び名。
# 人手 override が「同種診療科」→「内科系診療科」へ4件とも書き換えていたため、
# プロンプト側で最初から具体名を渡す（添削フィードバックループ P2 の環流）。
_GROUP_LABELS = {"surgical": "外科系", "medical": "内科系", "emergency": "救急"}


def dept_group_label(dept: Optional[str]) -> Optional[str]:
    """診療科名 → 「内科系」「外科系」「救急」。未知/読込失敗は None（呼び出し側で従来表現）。"""
    try:
        data = _load()
        if not data:
            return None
        for key, group in (data.get("dept_group_rules") or {}).items():
            if isinstance(group, dict) and dept in (group.get("depts") or []):
                return _GROUP_LABELS.get(key)
    except Exception:  # noqa: BLE001
        return None
    return None


def build_alert_context(alert: dict) -> str:
    """アラート用の追加コンテキストを生成。空なら空文字。"""
    data = _load()
    policy = _load_policy()
    if not data and not policy:
        return ""

    parts = []

    # 今期の経営方針（最優先・業務方向性）。全アラートの評価軸/打ち手の優先順位を枠づける。
    priorities = (policy or {}).get("priorities", [])
    if priorities:
        parts.append("【今期の経営方針（最優先）】")
        parts.append(_format_rules(priorities))
        parts.append("")   # 方針と評価方針の間に空行

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

    # 診療科グループルール ＋ 打ち手（レバー）
    dept = (alert.get("meta") or {}).get("dept")
    group = _find_dept_group(dept, data)
    if group:
        group_rules = group.get("rules", [])
        if group_rules:
            parts.append(f"\n【{dept} の評価方針】")
            parts.append(_format_rules(group_rules))
        # A1: この群が動かせる打ち手。action の起点にさせる（発明ではなく選択＋文脈化）。
        group_levers = group.get("levers", [])
        if group_levers:
            parts.append(f"\n【{dept} で使える打ち手（レバー）】（action はこの中から状況に合うものを選ぶ）")
            parts.append(_format_rules(group_levers))

    return "\n".join(parts)


def build_leveling_context() -> str:
    """退院曜日平準化ナラティブ用の追加コンテキストを生成。空なら空文字。"""
    data = _load()
    if not data:
        return ""

    parts = []

    global_rules = data.get("global_rules", [])
    if global_rules:
        parts.append("【評価方針（全体）】")
        parts.append(_format_rules(global_rules))

    lev_rules = data.get("discharge_leveling_rules", [])
    if lev_rules:
        parts.append("\n【退院曜日平準化の評価方針】")
        parts.append(_format_rules(lev_rules))

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
