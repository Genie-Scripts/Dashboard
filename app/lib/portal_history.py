"""
portal_history.py — トリアージ状態の履歴（決定論の再計算）と日次変化点

方針:
    - 状態ファイルを増やさない。過去日の要注視判定は triage.score_departments /
      score_wards を base_date をずらして呼び直すだけ（同一データ・同一閾値＝決定論）。
    - adm は数万行規模なので 14日分の再計算でも軽い（実測して遅ければ HISTORY_DAYS=7 に）。
    - KPIの達成状態遷移は weekly_story が毎日保存する output/last_kpi.json を読む。
    - LLM 不使用。ここの結果を既存プロンプトに注入しない（§1.7-2）。

設計意図（state-file 方式より優れる点）:
    トリアージの閾値（PRIMARY_THRESHOLD 等）が将来変わっても、streak は
    「現在の閾値で過去 N 日を毎回再評価」した結果になる。状態ファイルに
    当時の判定結果を保存する方式だと、閾値変更後に過去の記録と現在の
    判定基準がずれて streak の意味が不整合になる（例: 閾値を緩めた翌日に
    「連続14日」の顔ぶれが急に変わって見える）。再計算方式ならこの矛盾が
    構造的に起きない。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import pandas as pd

from . import triage
from .config import TARGET_INPATIENT_ALLDAY
from .weekly_story import load_history

logger = logging.getLogger(__name__)

HISTORY_DAYS = 14
ATTENTION_KINDS = {"below", "watch"}

# 再計算コストがこれを超えたら HISTORY_DAYS を下げる方針（§3.5）
SLOW_THRESHOLD_SEC = 10.0


def _item_lite(key: tuple[str, str], status_kind: str) -> dict:
    """(entity, name) + status_kind から item-lite を組み立てる。
    href は triage.pick_targets と同じ規約。"""
    entity, name = key
    if entity == "ward":
        href = "detail.html#inpatient?axis=ward"
        entity_label = "病棟"
    else:
        href = f"dept.html#{name}"
        entity_label = "科"
    return {
        "name": name,
        "entity": entity,
        "entity_label": entity_label,
        "href": href,
        "status_kind": status_kind,
    }


def _attention_map(adm: pd.DataFrame, surg: pd.DataFrame, targets: dict,
                   surg_targets: dict, profit_monthly: Optional[pd.DataFrame],
                   d: pd.Timestamp) -> dict:
    """ある1日 d の要注視集合 {(entity, name): status_kind} を返す
    （status_kind in ATTENTION_KINDS のみ）。"""
    day_map: dict = {}
    for rec in triage.score_departments(adm, surg, targets, surg_targets, profit_monthly, d):
        if rec.get("status_kind") in ATTENTION_KINDS:
            day_map[("dept", rec["name"])] = rec["status_kind"]
    for rec in triage.score_wards(adm, targets, d):
        if rec.get("status_kind") in ATTENTION_KINDS:
            day_map[("ward", rec["name"])] = rec["status_kind"]
    return day_map


def build_attention_history(adm: pd.DataFrame, surg: pd.DataFrame,
                            targets: dict, surg_targets: dict,
                            profit_monthly: Optional[pd.DataFrame],
                            base_date: pd.Timestamp, days: int = HISTORY_DAYS) -> dict:
    """
    直近 days 日分のトリアージ状態を base_date をずらして再計算し、
    継続日数（streak）と前日からの出入り（entered/exited）を求める。

    Returns:
      {
        "streaks":  {(entity, name): int},      # 今日を含む連続要注視日数
        "entered":  [item-lite, ...],           # 今日入り（昨日は非対象だった）
        "exited":   [item-lite, ...],           # 今日抜け（昨日は対象だった）
        "prev_date": "YYYY-MM-DD" | None,       # 比較に使った前日
      }
      item-lite = {"name", "entity", "entity_label", "href", "status_kind"}
    """
    t0 = time.perf_counter()

    adm_min = None
    try:
        if adm is not None and len(adm) > 0:
            adm_min = adm["日付"].min()
    except Exception:
        adm_min = None

    daily_sets: dict[int, dict] = {}
    for k in range(days):
        d = base_date - pd.Timedelta(days=k)
        if adm_min is not None and d < adm_min:
            break
        daily_sets[k] = _attention_map(adm, surg, targets, surg_targets, profit_monthly, d)

    elapsed = time.perf_counter() - t0
    logger.info(f"portal_history: 過去{len(daily_sets)}日分の再計算 {elapsed:.2f}秒")
    if elapsed > SLOW_THRESHOLD_SEC:
        logger.warning(
            f"portal_history: 再計算に{elapsed:.1f}秒（{SLOW_THRESHOLD_SEC:.0f}秒超）。"
            f"HISTORY_DAYS={days} を7へ下げることを検討してください。"
        )

    today_map = daily_sets.get(0, {})
    max_k = len(daily_sets)

    # ── streak: 今日対象のユニットごとに、連続して対象である日数を数える ──
    streaks: dict[tuple[str, str], int] = {}
    for key in today_map:
        streak = 0
        for k in range(max_k):
            if key in daily_sets[k]:
                streak += 1
            else:
                break
        streaks[key] = streak

    # ── entered / exited: k=0 と k=1 の対象集合の差（k=1 が無ければ両方とも空） ──
    entered: list[dict] = []
    exited: list[dict] = []
    prev_date: Optional[str] = None
    if 1 in daily_sets:
        prev_date = (base_date - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        prev_map = daily_sets[1]
        for key, status_kind in today_map.items():
            if key not in prev_map:
                entered.append(_item_lite(key, status_kind))
        for key, status_kind in prev_map.items():
            if key not in today_map:
                exited.append(_item_lite(key, status_kind))

    return {
        "streaks": streaks,
        "entered": entered,
        "exited": exited,
        "prev_date": prev_date,
    }


# ════════════════════════════════════════
# KPI達成バケットの日次遷移（B4・病院全体KPI）
# ════════════════════════════════════════

_KPI_BUCKET_LABELS = {"inpatient": "在院", "admission": "新入院", "operation": "全麻"}


def _bucket(rate: Optional[float]) -> Optional[str]:
    """達成率(%) を 達成(>=100) / 接近(>=90) / 未達(<90) に離散化。"""
    if rate is None:
        return None
    if rate >= 100:
        return "達成"
    if rate >= 90:
        return "接近"
    return "未達"


def _kpi_rate(snapshot: dict, kind: str) -> Optional[float]:
    """weekly_story のスナップショットから病院KPIの達成率(%)を取り出す。
    在院は単日rateが曜日で暴れるため avg_7d を TARGET_INPATIENT_ALLDAY で割って判定。"""
    if kind == "inpatient":
        avg_7d = snapshot.get("inpatient", {}).get("avg_7d")
        if avg_7d is None or not TARGET_INPATIENT_ALLDAY:
            return None
        return avg_7d / TARGET_INPATIENT_ALLDAY * 100.0
    if kind == "admission":
        return snapshot.get("admission", {}).get("rate_7d")
    if kind == "operation":
        return snapshot.get("operation", {}).get("rate")
    return None


def kpi_status_changes(history_path, base_date) -> list[dict]:
    """
    output/last_kpi.json（weekly_story.load_history 再利用）から、基準日と
    直近過去スナップショットの間で達成バケットが変わった病院KPIを返す。

    バケット: rate>=100 →「達成」, >=90 →「接近」, それ未満 →「未達」
      - 在院:   avg_7d / TARGET_INPATIENT_ALLDAY * 100
      - 新入院: admission.rate_7d
      - 全麻:   operation.rate

    current = base_date と一致するスナップショット。無ければ空リスト。
    prior   = base_date 未満で最大の base_date のスナップショット
              （昨日が欠けていても直近を使う）。

    Returns: [{"label": "新入院", "from": "接近", "to": "達成",
               "from_rate": 97.4, "to_rate": 101.2, "improved": True}, ...]
    履歴が無い/壊れている/現在・過去のいずれかが欠ける場合は空リスト（縮退）。
    """
    try:
        history = load_history(Path(history_path))
    except Exception as e:
        logger.warning(f"kpi_status_changes: 履歴読込失敗 ({e})")
        return []
    if not history:
        return []

    bd_str = pd.Timestamp(base_date).strftime("%Y-%m-%d")
    current = next((s for s in history if s.get("base_date") == bd_str), None)
    if current is None:
        return []

    earlier = [s for s in history if s.get("base_date") and s["base_date"] < bd_str]
    if not earlier:
        return []
    prior = max(earlier, key=lambda s: s["base_date"])

    order = {"未達": 0, "接近": 1, "達成": 2}
    changes = []
    for kind, label in _KPI_BUCKET_LABELS.items():
        cur_rate = _kpi_rate(current, kind)
        prev_rate = _kpi_rate(prior, kind)
        cur_bucket, prev_bucket = _bucket(cur_rate), _bucket(prev_rate)
        if cur_bucket is None or prev_bucket is None or cur_bucket == prev_bucket:
            continue
        changes.append({
            "label": label,
            "from": prev_bucket, "to": cur_bucket,
            "from_rate": round(prev_rate, 1), "to_rate": round(cur_rate, 1),
            "improved": order[cur_bucket] > order[prev_bucket],
        })
    return changes
