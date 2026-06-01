"""
outpatient.py — 外来件数ブリッジ（粗利推計の特徴量用）
================================================================

別運用の隣リポ ``Outpatient-Dashboard`` が生成する月次集計CSV
``data/aggregated/YYYY-MM/02_dept_monthly.csv``
（列: 診療科名, 月, 初再診区分, 紹介状有無, 件数）を読み込み、
粗利推計（``profit_estimate.py``）が使える月次 tidy 表に整形する。

【役割】
  - 外来の **受診件数** を診療科×月で供給する。Dashboard 本体の入院/手術データには
    外来患者数の列が無く（memory: project_profit_projection_next_steps「項目3」）、
    外来粗利モデルは従来 ``α·営業日数 + β·外来手術件数`` のみで件数を未使用だった。
  - 表示統合はしない。あくまで粗利推計の裏側の特徴量として使う。

【名寄せ】
  外来側の診療科名を ``config.OUTPATIENT_DEPT_MERGE`` で粗利側の科定義に寄せる
  （感染症/内科→総合内科、アレルギー科→呼吸器内科、糖尿病内分泌内科→腎内科 等）。

【鮮度】
  集計は月次・手動運用で最新月が遅れる（例: 2026-04）。粗利実績も月次・同程度の
  鮮度なので **係数フィットの窓は重なる**。当月ライブ予測に使うには日次フィードが要る
  （本モジュールは月次が主。日次フィードは将来 data/outpatient_data/ で対応予定）。
"""
from __future__ import annotations

import glob
from pathlib import Path
from typing import Optional

import pandas as pd

from .config import OUTPATIENT_AGG_DIR, OUTPATIENT_DEPT_MERGE

# tidy 表の列定義（呼び出し側が安定して参照できるよう固定）
OUTPATIENT_COLUMNS = ["診療科名", "月", "初診件数", "再診件数", "外来件数"]


def _clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    """BOM・前後空白を除去した列名に正規化。"""
    df = df.copy()
    df.columns = [str(c).replace("﻿", "").strip() for c in df.columns]
    return df


def _empty() -> pd.DataFrame:
    """正しい列を持つ空フレーム（ソース不在時の安全な戻り値）。"""
    return pd.DataFrame(columns=OUTPATIENT_COLUMNS)


def load_outpatient_monthly(agg_dir: Optional[str] = None) -> pd.DataFrame:
    """外来 02_dept_monthly.csv を全月読み込み、月次 tidy 表に整形して返す。

    Args:
        agg_dir: 集計CSVルート。None なら ``config.OUTPATIENT_AGG_DIR``。

    Returns:
        DataFrame[診療科名, 月(月初 Timestamp), 初診件数, 再診件数, 外来件数]。
        ソースが無ければ空フレーム（列は OUTPATIENT_COLUMNS）。
        診療科名は OUTPATIENT_DEPT_MERGE で粗利側に名寄せ済み・(科, 月) で集約済み。
    """
    root = Path(agg_dir or OUTPATIENT_AGG_DIR).expanduser()
    if not root.exists():
        return _empty()

    files = sorted(glob.glob(str(root / "*" / "02_dept_monthly.csv")))
    if not files:
        return _empty()

    frames = []
    for f in files:
        try:
            frames.append(_clean_columns(pd.read_csv(f)))
        except Exception:
            # 壊れた月は黙ってスキップ（運用中の部分書き込み等に頑健）
            continue
    if not frames:
        return _empty()

    raw = pd.concat(frames, ignore_index=True)
    needed = {"診療科名", "月", "初再診区分", "件数"}
    if not needed.issubset(raw.columns):
        return _empty()

    raw = raw[["診療科名", "月", "初再診区分", "件数"]].copy()
    raw["診療科名"] = raw["診療科名"].astype(str).str.strip().replace(OUTPATIENT_DEPT_MERGE)
    raw["月"] = pd.to_datetime(raw["月"]).dt.to_period("M").dt.to_timestamp()
    raw["件数"] = pd.to_numeric(raw["件数"], errors="coerce").fillna(0).astype(int)

    # 初再診区分（初診/再診）を列に展開しつつ、総件数も保持
    区分 = raw["初再診区分"].astype(str).str.strip()
    raw["初診件数"] = raw["件数"].where(区分 == "初診", 0)
    raw["再診件数"] = raw["件数"].where(区分 == "再診", 0)

    out = (raw.groupby(["診療科名", "月"], as_index=False)
              .agg(初診件数=("初診件数", "sum"),
                   再診件数=("再診件数", "sum"),
                   外来件数=("件数", "sum")))
    return out[OUTPATIENT_COLUMNS].sort_values(["診療科名", "月"]).reset_index(drop=True)


def outpatient_coverage(profit_breakdown: pd.DataFrame,
                        op_monthly: Optional[pd.DataFrame] = None) -> dict:
    """粗利(外来)の科が外来件数データでどれだけカバーされるか診断する。

    バックテスト/検証で「名寄せ漏れ」を可視化するための補助。

    Returns:
        {matched: [...], profit_only: [...], outpatient_only: [...],
         month_min, month_max, n_months}
    """
    if op_monthly is None:
        op_monthly = load_outpatient_monthly()

    pb_g = set()
    if profit_breakdown is not None and "区分" in getattr(profit_breakdown, "columns", []):
        pb_g = set(profit_breakdown[profit_breakdown["区分"] == "外来"]["診療科名"].dropna().unique())
    op_set = set(op_monthly["診療科名"].unique()) if len(op_monthly) else set()

    months = op_monthly["月"] if len(op_monthly) else pd.Series([], dtype="datetime64[ns]")
    return {
        "matched":          sorted(pb_g & op_set),
        "profit_only":      sorted(pb_g - op_set),   # 外来データに無い粗利科（=名寄せ漏れの疑い）
        "outpatient_only":  sorted(op_set - pb_g),   # 粗利に出ない外来科（運用系等・無視可）
        "month_min":        (months.min().strftime("%Y-%m") if len(months) else None),
        "month_max":        (months.max().strftime("%Y-%m") if len(months) else None),
        "n_months":         int(months.dt.to_period("M").nunique()) if len(months) else 0,
    }
