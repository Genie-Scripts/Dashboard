"""
weekly_story.py — 週次ストーリー（WoW差分特化）

方針:
    - KPIスナップショットを base_date キーで履歴保存
    - 保存済み「7日前」のスナップショットと比較して Python 側で差分を確定
    - LLM には確定済み差分テキストのみを渡し、150字要約を生成させる
    - 数値計算は一切 LLM にさせない（ハルシネーション封じ）

ファイル:
    output/last_kpi.json  … 履歴付きスナップショット（直近30日）

設計原則（ai_narrative.py と同じ）:
    - Ollama 未起動・未取得時は無害に失敗（story=None を返す）
    - 差分が無ければ LLM 呼び出し自体をスキップ
"""

from __future__ import annotations
import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


DEFAULT_MODEL = "MedAIBase/MedGemma1.5:4b"
DEFAULT_TEMPERATURE = 0.2
DEFAULT_NUM_PREDICT = 260
SNAPSHOT_RETAIN_DAYS = 30
WOW_LOOKBACK_DAYS = 7


SYSTEM_PROMPT = """あなたは病院経営会議向けの週次レポートライターです。以下を厳守してください。

【厳守事項】
1. 与えられた差分事実のみを使い、新しい数値や事実を追加しない
2. 本文は日本語で150字以内（句読点込み）
3. 推測・仮定・原因断定はしない。臨床管理の観点で変化点を淡々と述べる
4. 出力は指定 JSON スキーマのみ。前置きや説明文を付けない

【出力スキーマ】
{"story": "150字以内の要約本文"}"""


# ════════════════════════════════════════
# スナップショット生成
# ════════════════════════════════════════

def _weekly_window_sum(adm: pd.DataFrame, col: str, base_date: pd.Timestamp) -> int:
    start = base_date - timedelta(days=6)
    w = adm[(adm["日付"] >= start) & (adm["日付"] <= base_date)]
    return int(w[col].sum()) if col in w.columns else 0


def _weekly_or_utilization(surg: pd.DataFrame, base_date: pd.Timestamp) -> Optional[float]:
    """直近7暦日のうち平日かつ稼働対象室分を合算して稼働率を算出"""
    try:
        from .config import OR_MINUTES_PER_ROOM, OR_ROOM_COUNT
    except ImportError:
        return None
    start = base_date - timedelta(days=6)
    w = surg[(surg["手術実施日"] >= start) & (surg["手術実施日"] <= base_date)]
    if "稼働対象室" not in w.columns or "平日" not in w.columns or "稼働分" not in w.columns:
        return None
    target = w[w["稼働対象室"] & w["平日"]]
    if len(target) == 0:
        return 0.0
    weekdays = target["手術実施日"].dt.normalize().unique()
    denominator = OR_MINUTES_PER_ROOM * OR_ROOM_COUNT * len(weekdays)
    if denominator == 0:
        return None
    return round(target["稼働分"].sum() / denominator * 100, 1)


def _profit_ranking_snapshot(profit_monthly: pd.DataFrame, top_n: int = 5) -> list:
    if profit_monthly is None or len(profit_monthly) == 0:
        return []
    try:
        from .profit import get_latest_month_summary
        latest = get_latest_month_summary(profit_monthly)
    except Exception:
        return []
    rows = []
    for i, r in latest.head(top_n).iterrows():
        if not pd.notna(r.get("達成率")):
            continue
        rows.append({
            "rank": int(i) + 1,
            "name": str(r["診療科名"]),
            "rate": round(float(r["達成率"]), 1),
        })
    return rows


def build_kpi_snapshot(adm: pd.DataFrame, surg: pd.DataFrame,
                       kpi: dict, profit_monthly: pd.DataFrame,
                       base_date: pd.Timestamp) -> dict:
    """週次ストーリー用のKPIスナップショット"""
    return {
        "base_date": base_date.strftime("%Y-%m-%d"),
        "inpatient": {
            "avg_7d": kpi.get("inpatient_avg_7d"),
            "rate": kpi.get("inpatient_rate"),
        },
        "admission": {
            "actual_7d": kpi.get("admission_actual_7d"),
            "planned_7d": _weekly_window_sum(adm, "入院患者数", base_date),
            "emergency_7d": _weekly_window_sum(adm, "緊急入院患者数", base_date),
            "rate_7d": kpi.get("admission_rate_7d"),
        },
        "operation": {
            "week_total": kpi.get("operation_week_total"),
            "rate": kpi.get("operation_rate"),
            "or_util_7d": _weekly_or_utilization(surg, base_date),
        },
        "profit_top": _profit_ranking_snapshot(profit_monthly),
    }


# ════════════════════════════════════════
# スナップショットの保存・読込
# ════════════════════════════════════════

def load_history(path: Path) -> list:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"last_kpi.json 読込失敗: {e}")
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict) and "snapshots" in raw:
        return raw["snapshots"]
    if isinstance(raw, dict) and "base_date" in raw:
        return [raw]  # 旧フォーマット互換
    return []


def save_history(path: Path, snapshots: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"snapshots": snapshots}
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def upsert_snapshot(history: list, snap: dict, retain_days: int = SNAPSHOT_RETAIN_DAYS) -> list:
    """base_date が同じものは置換、なければ追加。直近 retain_days 件に切り詰め"""
    bd = snap["base_date"]
    merged = [s for s in history if s.get("base_date") != bd]
    merged.append(snap)
    merged.sort(key=lambda s: s.get("base_date", ""))
    return merged[-retain_days:]


def find_prior_snapshot(history: list, base_date: pd.Timestamp,
                        lookback_days: int = WOW_LOOKBACK_DAYS) -> Optional[dict]:
    """base_date より前のスナップショットのうち、base_date - lookback_days に最も近いもの。

    履歴が十分に溜まっていない初期段階でも表示できるよう、厳密な「7日前以前」を
    要求しない。ラベル側（prior_date）で実際の比較日を明示する想定。
    """
    bd_str = base_date.strftime("%Y-%m-%d")
    earlier = [s for s in history
               if s.get("base_date") and s["base_date"] < bd_str]
    if not earlier:
        return None
    target = base_date - timedelta(days=lookback_days)
    return min(earlier, key=lambda s: abs(
        (pd.Timestamp(s["base_date"]) - target).days
    ))


# ════════════════════════════════════════
# 差分計算
# ════════════════════════════════════════

def _fmt_pt(curr, prev, unit="") -> Optional[str]:
    if curr is None or prev is None:
        return None
    diff = curr - prev
    sign = "+" if diff >= 0 else ""
    return f"{prev}{unit} → {curr}{unit}（{sign}{round(diff, 1)}{unit}）"


def _fmt_count(curr, prev, unit="件") -> Optional[str]:
    if curr is None or prev is None:
        return None
    diff = curr - prev
    sign = "+" if diff >= 0 else ""
    if prev == 0:
        pct = ""
    else:
        pct = f"、{sign}{round(diff / prev * 100)}%"
    return f"{prev}{unit} → {curr}{unit}（{sign}{diff}{unit}{pct}）"


def compute_wow_diffs(current: dict, prior: dict) -> list[str]:
    """LLM に渡す差分事実のテキスト配列"""
    diffs = []

    c_or = current.get("operation", {}).get("or_util_7d")
    p_or = prior.get("operation", {}).get("or_util_7d")
    s = _fmt_pt(c_or, p_or, unit="%")
    if s:
        diffs.append(f"手術室稼働率（直近7日）: {s}")

    c_op = current.get("operation", {}).get("week_total")
    p_op = prior.get("operation", {}).get("week_total")
    s = _fmt_count(c_op, p_op, unit="件")
    if s:
        diffs.append(f"全麻手術件数（週累計）: {s}")

    c_adm = current.get("admission", {}).get("actual_7d")
    p_adm = prior.get("admission", {}).get("actual_7d")
    s = _fmt_count(c_adm, p_adm, unit="人")
    if s:
        diffs.append(f"新入院（直近7日累計）: {s}")

    c_emg = current.get("admission", {}).get("emergency_7d")
    p_emg = prior.get("admission", {}).get("emergency_7d")
    s = _fmt_count(c_emg, p_emg, unit="件")
    if s:
        diffs.append(f"緊急入院（直近7日累計）: {s}")

    c_inp = current.get("inpatient", {}).get("avg_7d")
    p_inp = prior.get("inpatient", {}).get("avg_7d")
    s = _fmt_pt(c_inp, p_inp, unit="人")
    if s:
        diffs.append(f"在院患者数（7日平均）: {s}")

    # 粗利トップ3 の順位変動
    c_top = {r["name"]: r["rank"] for r in current.get("profit_top", [])}
    p_top = {r["name"]: r["rank"] for r in prior.get("profit_top", [])}
    movers = []
    for name, rank in c_top.items():
        if rank > 3:
            continue
        prev_rank = p_top.get(name)
        if prev_rank is None:
            movers.append(f"{name}が圏外→{rank}位")
        elif prev_rank != rank and prev_rank > 3:
            movers.append(f"{name}が{prev_rank}位→{rank}位")
        elif prev_rank != rank:
            movers.append(f"{name}が{prev_rank}位→{rank}位")
    if movers:
        diffs.append("粗利達成率トップ3変動: " + "、".join(movers))

    return diffs


# ════════════════════════════════════════
# LLM ナラティブ
# ════════════════════════════════════════

def _build_user_prompt(diffs: list[str], base_date: str, prior_date: str) -> str:
    from .eval_rules import build_weekly_context
    body = "\n".join(f"- {d}" for d in diffs)
    context = build_weekly_context()
    context_block = f"\n\n{context}" if context else ""
    return f"""以下は病院KPIの今週（{base_date}基準）と前回保存時（{prior_date}基準）の確定差分です。
この変化を臨床管理の観点で150字以内にまとめ、JSON を1つだけ出力してください。

【確定差分】
{body}
{context_block}
【注意】
- 差分事実にない数値・原因・人物を補わない
- 出力は {{"story": "..."}} の JSON のみ（```や前置き禁止）"""


def _extract_story(text: str) -> Optional[str]:
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
    story = obj.get("story") if isinstance(obj, dict) else None
    return str(story).strip() if story else None


def narrate_weekly_story(diffs: list[str], base_date: str, prior_date: str,
                         model: str = DEFAULT_MODEL,
                         temperature: float = DEFAULT_TEMPERATURE) -> Optional[str]:
    """差分事実からLLMで150字要約を生成。未起動時やエラー時は None"""
    if not diffs:
        return None
    try:
        import ollama
    except ImportError:
        logger.info("ollama 未インストール: 週次ストーリーをスキップ")
        return None
    try:
        res = ollama.chat(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(diffs, base_date, prior_date)},
            ],
            options={"temperature": temperature, "num_predict": DEFAULT_NUM_PREDICT},
            format="json",
            keep_alive="5m",
        )
    except Exception as e:
        logger.warning(f"Ollama 週次ストーリー呼び出し失敗: {e}")
        return None
    content = (res.get("message") or {}).get("content", "")
    return _extract_story(content)


# ════════════════════════════════════════
# エントリポイント
# ════════════════════════════════════════

def build_weekly_story(adm: pd.DataFrame, surg: pd.DataFrame,
                       kpi: dict, profit_monthly: pd.DataFrame,
                       base_date: pd.Timestamp, snapshot_path: Path,
                       model: str = DEFAULT_MODEL,
                       quiet: bool = False) -> dict:
    """
    スナップショット保存 + WoW差分計算 + LLM要約を一括実行。

    Returns:
        {
          "base_date": str,
          "prior_date": str | None,
          "diffs": [str, ...],
          "story": str | None,   # LLM が生成した150字要約（未生成時 None）
        }
    """
    current = build_kpi_snapshot(adm, surg, kpi, profit_monthly, base_date)
    history = load_history(snapshot_path)
    prior = find_prior_snapshot(history, base_date)

    diffs: list[str] = []
    story: Optional[str] = None
    prior_date: Optional[str] = None

    if prior is not None:
        prior_date = prior.get("base_date")
        diffs = compute_wow_diffs(current, prior)
        if diffs:
            story = narrate_weekly_story(diffs, current["base_date"], prior_date, model=model)
            if not quiet:
                status = "✓" if story else "—"
                print(f"    [AI] {status} weekly_story ({len(diffs)} diffs vs {prior_date})")
        elif not quiet:
            print(f"    [AI] — weekly_story (no material diffs vs {prior_date})")
    elif not quiet:
        print("    [AI] — weekly_story (no prior snapshot)")

    # 保存（毎回更新）
    history = upsert_snapshot(history, current)
    try:
        save_history(snapshot_path, history)
    except Exception as e:
        logger.warning(f"last_kpi.json 保存失敗: {e}")

    return {
        "base_date": current["base_date"],
        "prior_date": prior_date,
        "diffs": diffs,
        "story": story,
    }
