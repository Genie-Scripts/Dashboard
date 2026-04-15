"""
alerts.py — AIアラート検知（純Python・LLM不使用）

KPI・ランキング・WoW差分から「注目すべき事実」を抽出し、
構造化 dict で返す。数値判定はここで完結させ、LLMには
翻訳（文章化）のみを任せる前提。

出力スキーマ:
    {
      "id": str,              # 識別子
      "severity": "danger|warn|info",
      "category": "kpi|dept|ward|momentum",
      "icon": str,
      "title_fallback": str,  # LLM失敗時の見出し
      "facts": [str, ...],    # 確定事実（箇条書き、数値は文中不要）
      "meta": dict,           # テンプレート側で利用するメタ情報
    }
"""

from __future__ import annotations
from typing import Optional
import pandas as pd

from .config import (
    WARD_NAMES, WARD_HIDDEN, NADM_DISPLAY_DEPTS,
    TARGET_ADMISSION_WEEKLY,
)
from .metrics import (
    build_kpi_summary, rolling7_new_admission,
    daily_inpatient, build_daily_series, week_over_week,
)


# ────────────────────────────────────
# 閾値（このモジュール内で一元管理）
# ────────────────────────────────────
KPI_DANGER_RATE = 80.0     # 達成率 80%未満 → danger
KPI_WARN_RATE   = 90.0     # 達成率 90%未満 → warn
DEPT_GAP_ABS    = 5        # 診療科の新入院7日差が 5人以上下回ったら対象
WARD_GAP_ABS    = 3        # 病棟の在院差が 3人以上下回ったら対象
MOMENTUM_DELTA  = 3        # 前週同曜日比 +3人以上で改善


# ────────────────────────────────────
# 個別検知関数
# ────────────────────────────────────

def _kpi_alerts(kpi: dict) -> list[dict]:
    """3大KPI（在院・新入院・手術）の未達を検知"""
    out = []
    specs = [
        ("inpatient",  "在院患者数",  "rate",     "inpatient_rate",     "inpatient_trend",    "🛏️"),
        ("admission",  "新入院患者数", "rate_7d",  "admission_rate_7d",  "admission_trend",    "🚪"),
        ("operation",  "全身麻酔手術", "rate",     "operation_rate",     "operation_trend",    "💉"),
    ]
    for kpi_id, label, _, rate_key, trend_key, icon in specs:
        rate = kpi.get(rate_key)
        if rate is None:
            continue
        if rate >= KPI_WARN_RATE:
            continue

        severity = "danger" if rate < KPI_DANGER_RATE else "warn"
        trend = kpi.get(trend_key) or ""

        facts = [
            f"{label}の目標達成率が警戒水準（{KPI_WARN_RATE:.0f}%）を下回っている",
        ]
        if trend == "down":
            facts.append(f"{label}は前週同期より悪化傾向")
        elif trend == "up":
            facts.append(f"{label}は前週同期より改善しつつあるが依然として目標未達")
        else:
            facts.append(f"{label}の水準は先週と同等で、回復の兆しは見られない")

        out.append({
            "id": f"kpi_{kpi_id}_underperform",
            "severity": severity,
            "category": "kpi",
            "icon": icon,
            "title_fallback": f"{label}が目標未達",
            "facts": facts,
            "meta": {
                "kpi": kpi_id,
                "rate": round(float(rate), 1),
                "trend": trend,
                "href": f"detail.html#{kpi_id}",
            },
        })
    return out


def _dept_admission_alerts(adm: pd.DataFrame, base_date: pd.Timestamp,
                            targets: dict) -> list[dict]:
    """診療科別 新入院の目標大幅未達を検知（最大2件）"""
    r7 = rolling7_new_admission(adm, base_date)
    nadm_tgt = targets.get("new_admission", {}).get("dept", {})

    cands = []
    for dept, actual in r7["by_dept"].items():
        if dept not in NADM_DISPLAY_DEPTS:
            continue
        tgt = nadm_tgt.get(dept)
        if not tgt or tgt <= 0:
            continue
        gap = actual - tgt
        if gap >= -DEPT_GAP_ABS:
            continue
        cands.append((dept, actual, tgt, gap))

    cands.sort(key=lambda x: x[3])  # gap 小さい順
    out = []
    for dept, actual, tgt, gap in cands[:2]:
        # WoW 差分も Python 側で事実として付与
        s = build_daily_series(adm, "新入院患者数",
                                group_col="診療科名", group_val=dept)
        wow = week_over_week(s, base_date)
        facts = [f"{dept}の新入院（直近7日累計）が週次目標を下回っている"]
        if wow is not None:
            if wow < -2:
                facts.append(f"{dept}は前週同期比でも減少している")
            elif wow > 2:
                facts.append(f"{dept}は前週同期比では増加しているが依然として目標未達")
        out.append({
            "id": f"dept_admission_{dept}",
            "severity": "warn",
            "category": "dept",
            "icon": "🏥",
            "title_fallback": f"{dept}の新入院が低調",
            "facts": facts,
            "meta": {
                "dept": dept,
                "actual": int(actual),
                "target": round(float(tgt), 1),
                "gap": int(round(gap, 0)),
                "href": f"dept.html#{dept}",
            },
        })
    return out


def _ward_inpatient_alerts(adm: pd.DataFrame, base_date: pd.Timestamp,
                            targets: dict) -> list[dict]:
    """病棟別 在院患者数の目標大幅未達を検知（最大1件）"""
    inp = daily_inpatient(adm, base_date)
    ward_tgt = targets.get("inpatient", {}).get("ward", {})

    cands = []
    for wcode, actual in inp["by_ward"].items():
        if wcode in WARD_HIDDEN:
            continue
        tgt = ward_tgt.get(wcode)
        if not tgt or tgt <= 0:
            continue
        gap = actual - tgt
        if gap >= -WARD_GAP_ABS:
            continue
        cands.append((wcode, actual, tgt, gap))

    cands.sort(key=lambda x: x[3])
    out = []
    for wcode, actual, tgt, gap in cands[:1]:
        wname = WARD_NAMES.get(wcode, wcode)
        facts = [
            f"{wname}の在院患者数が目標を下回っている",
            f"{wname}の空床が想定以上に発生しており、稼働改善の余地がある",
        ]
        out.append({
            "id": f"ward_inpatient_{wcode}",
            "severity": "warn",
            "category": "ward",
            "icon": "🛏️",
            "title_fallback": f"{wname}の在院低下",
            "facts": facts,
            "meta": {
                "ward": wname,
                "ward_code": wcode,
                "actual": int(actual),
                "target": round(float(tgt), 1),
                "gap": int(round(gap, 0)),
                "href": f"dept.html#{wname}",
            },
        })
    return out


def _momentum_alerts(adm: pd.DataFrame, base_date: pd.Timestamp) -> list[dict]:
    """前週同曜日比で大きく改善した診療科を検知（最大1件）"""
    best = None
    for dept in NADM_DISPLAY_DEPTS:
        s = build_daily_series(adm, "新入院患者数",
                                group_col="診療科名", group_val=dept)
        wow = week_over_week(s, base_date)
        if wow is None or wow < MOMENTUM_DELTA:
            continue
        if best is None or wow > best[1]:
            best = (dept, wow)

    if best is None:
        return []

    dept, wow = best
    return [{
        "id": f"momentum_{dept}",
        "severity": "info",
        "category": "momentum",
        "icon": "📈",
        "title_fallback": f"{dept}の新入院が回復",
        "facts": [
            f"{dept}の新入院が前週同曜日比で明確に増加した",
            "この勢いを維持できれば月次目標への寄与が見込める",
        ],
        "meta": {
            "dept": dept,
            "delta": int(round(wow, 0)),
            "href": f"dept.html#{dept}",
        },
    }]


# ────────────────────────────────────
# エントリポイント
# ────────────────────────────────────

def detect_alerts(adm: pd.DataFrame, surg: pd.DataFrame,
                  targets: dict, surg_targets: dict,
                  base_date: pd.Timestamp,
                  max_alerts: int = 4) -> list[dict]:
    """
    各種アラートを検知してリストで返す。
    severity 優先（danger → warn → info）、カテゴリ多様性を担保。
    """
    kpi = build_kpi_summary(adm, surg, base_date, targets, surg_targets)

    alerts: list[dict] = []
    alerts += _kpi_alerts(kpi)
    alerts += _dept_admission_alerts(adm, base_date, targets)
    alerts += _ward_inpatient_alerts(adm, base_date, targets)
    alerts += _momentum_alerts(adm, base_date)

    severity_rank = {"danger": 0, "warn": 1, "info": 2}
    alerts.sort(key=lambda a: severity_rank.get(a["severity"], 9))
    return alerts[:max_alerts]
