"""
pl_projection.py — 医業収支の月次推計

【推計式】
  医業収支 = (R − M) − 給与費 − 委託費 − 設備関係費 − 経費
         = (G + δ) − 給与費 − 委託費 − 設備関係費 − 経費

  R: 医業収益(PL), M: 材料費(PL, 購入額ベース)
  G: 粗利(粗利データ, 点数表ベース = R − 償還材料点数 − 医薬品費)
  δ = (R − M) − G  ≒ DPC包括分等の購入差分。構造的に負値（中央値約 −12.6%）

【費目モデル】
  給与費      : 当年度4月実績 + 過去年度の月別オフセット（年度内月数 1..12 = 4月..3月）
                4月で病床/診療機能変更により給与費レベルが変動するため、年度起点ベース
                3月には年度末臨時賞与が乗るため、年度内オフセットで自動的に取り込まれる
  委託費      : 直近12か月の robust median（±3-4% の定常費目）
  設備関係費   : 直近12か月 robust median + 月別オフセット
  経費        : 直近12か月 robust median + 月別オフセット
  δ          : 直近12か月 robust median + 月別オフセット

  全費目で MAD（k=2.5）ベース外れ値除外を適用（2025/06 経費 618M 等の one-off を除く）
"""
from __future__ import annotations

from typing import Optional
import numpy as np
import pandas as pd

from .pl_history import load_pl_history, clean_pl


# ──────────────────────────────────────────
# 共通ユーティリティ
# ──────────────────────────────────────────

def _mad_filter(s: pd.Series, k: float = 2.5) -> pd.Series:
    """MAD ベース外れ値除外。中央値±k·MAD·1.4826 の外を捨てる。"""
    s = s.dropna()
    if len(s) < 3:
        return s
    med = s.median()
    mad = (s - med).abs().median()
    if mad == 0:
        return s
    threshold = k * mad * 1.4826
    return s[(s - med).abs() <= threshold]


def _fiscal_year(ts: pd.Timestamp) -> int:
    """4月起算の年度（2024-04..2025-03 → 2024）"""
    return ts.year if ts.month >= 4 else ts.year - 1


def _fy_month_idx(ts: pd.Timestamp) -> int:
    """年度内月インデックス（4月=1, 3月=12）"""
    return ((ts.month - 4) % 12) + 1


# ──────────────────────────────────────────
# 粗利データ → 月計
# ──────────────────────────────────────────

def aggregate_profit_monthly(profit_breakdown: pd.DataFrame) -> pd.DataFrame:
    """粗利データ（科×月×区分）を月計 G に集約。

    Returns:
        DataFrame: 月, G（千円, 外来+入院合算）
    """
    if profit_breakdown is None or len(profit_breakdown) == 0:
        return pd.DataFrame(columns=["月", "G"])
    df = profit_breakdown.copy()
    df["月"] = pd.to_datetime(df["月"])
    out = df.groupby("月", as_index=False)["粗利"].sum().rename(columns={"粗利": "G"})
    return out.sort_values("月").reset_index(drop=True)


def compute_delta_series(pl_clean: pd.DataFrame,
                          g_monthly: pd.DataFrame) -> pd.DataFrame:
    """δ = (R − M) − G の月次系列を返す。"""
    df = pl_clean.merge(g_monthly, on="月", how="inner").copy()
    df["R_minus_M"] = df["医業収益"] - df["材料費"]
    df["δ"] = df["R_minus_M"] - df["G"]
    df["δ率"] = df["δ"] / df["R_minus_M"] * 100
    return df.sort_values("月").reset_index(drop=True)


# ──────────────────────────────────────────
# 費目別予測モデル
# ──────────────────────────────────────────

def predict_payroll(pl_clean: pd.DataFrame, target: pd.Timestamp) -> dict:
    """給与費予測: 当年度4月実績 + 過去年度の月別オフセット中央値.

    target が4月の場合は直近12か月の伸びから推定（前年度同月+ベースアップ）.
    """
    fy = _fiscal_year(target)
    fy_idx = _fy_month_idx(target)

    pl = pl_clean.copy()
    pl["年度"] = pl["月"].apply(_fiscal_year)
    pl["FY月"] = pl["月"].apply(_fy_month_idx)

    # 過去年度（target より前の年度）の同 FY月 オフセット = 同年度4月との差分
    past = pl[pl["年度"] < fy]
    offsets = []
    for y in past["年度"].unique():
        sub = past[past["年度"] == y]
        apr = sub[sub["FY月"] == 1]["給与費"]
        m = sub[sub["FY月"] == fy_idx]["給与費"]
        if len(apr) and len(m):
            offsets.append(m.iloc[0] - apr.iloc[0])
    offset = float(np.median(offsets)) if offsets else 0.0

    base = pl[(pl["年度"] == fy) & (pl["FY月"] == 1)]["給与費"]
    if len(base):
        pred = float(base.iloc[0]) + offset
        method = f"FY{fy} 4月実績 + offset(月{target.month})"
    else:
        # 4月実績がまだない（=当年度4月が target 自身）の場合
        # 過去2年4月の平均成長率を直近4月実績に乗せる
        apr_series = pl[pl["FY月"] == 1].sort_values("月")
        if len(apr_series) >= 2:
            growth = (apr_series["給与費"].iloc[-1]
                      / apr_series["給与費"].iloc[0]) ** (1 / (len(apr_series) - 1))
            pred = float(apr_series["給与費"].iloc[-1]) * growth
            if fy_idx != 1:
                pred += offset
            method = f"4月CAGR={growth:.4f} 外挿"
        else:
            pred = float(pl["給与費"].tail(12).median())
            method = "fallback 直近12mo median"
    return {"value": pred, "method": method}


def _predict_cost_with_offset(series_by_month: pd.DataFrame,
                                col: str,
                                target: pd.Timestamp,
                                window: int = 12,
                                outlier_k: float = 2.5) -> dict:
    """直近 window か月の robust median + 同月オフセット.

    オフセット = (過去同月 robust median) − (全期間 robust median)
    """
    df = series_by_month.copy()
    df = df[df["月"] < target]
    recent = _mad_filter(df.tail(window)[col], k=outlier_k)
    base = float(recent.median()) if len(recent) else float(df[col].median())

    same_month = df[df["月"].dt.month == target.month][col]
    same_month = _mad_filter(same_month, k=outlier_k)
    if len(same_month) >= 1:
        all_filt = _mad_filter(df[col], k=outlier_k)
        offset = float(same_month.median()) - float(all_filt.median())
    else:
        offset = 0.0
    return {"value": base + offset, "base": base, "offset": offset}


def predict_consign(pl_clean: pd.DataFrame, target: pd.Timestamp) -> dict:
    """委託費: 直近12か月 robust median."""
    df = pl_clean[pl_clean["月"] < target].copy()
    recent = _mad_filter(df.tail(12)["委託費"])
    val = float(recent.median()) if len(recent) else float(df["委託費"].median())
    return {"value": val, "method": "直近12mo robust median"}


def predict_facility(pl_clean: pd.DataFrame, target: pd.Timestamp) -> dict:
    """設備関係費: 直近12mo median + 同月オフセット."""
    r = _predict_cost_with_offset(pl_clean, "設備関係費", target)
    return {"value": r["value"],
            "method": f"base={r['base']:,.0f} + offset={r['offset']:+,.0f}"}


def predict_misc(pl_clean: pd.DataFrame, target: pd.Timestamp) -> dict:
    """経費: 直近12mo median + 同月オフセット（外れ値除外）."""
    r = _predict_cost_with_offset(pl_clean, "経費", target)
    return {"value": r["value"],
            "method": f"base={r['base']:,.0f} + offset={r['offset']:+,.0f}"}


def predict_delta(delta_series: pd.DataFrame, target: pd.Timestamp) -> dict:
    """δ: 直近12mo median + 同月オフセット."""
    r = _predict_cost_with_offset(delta_series, "δ", target)
    return {"value": r["value"],
            "method": f"base={r['base']:,.0f} + offset={r['offset']:+,.0f}"}


# ──────────────────────────────────────────
# 医業収支予測 + バックテスト
# ──────────────────────────────────────────

def project_monthly_balance(pl_clean: pd.DataFrame,
                              delta_series: pd.DataFrame,
                              g_monthly: pd.DataFrame,
                              target: pd.Timestamp,
                              g_override: Optional[float] = None) -> dict:
    """指定月の医業収支予測.

    Args:
        pl_clean: PL 履歴（クリーン済）
        delta_series: δ 系列
        g_monthly: 粗利月計
        target: 予測対象月（YYYY-MM-01）
        g_override: G を外部から指定（粗利推計 latest_projection_total など）
                    None なら g_monthly から取得（=過去月の実績検証用）

    Returns:
        dict: 各費目の予測値 + 医業収支予測 + 内訳
    """
    target = pd.Timestamp(target).normalize().replace(day=1)
    payroll = predict_payroll(pl_clean, target)
    consign = predict_consign(pl_clean, target)
    facility = predict_facility(pl_clean, target)
    misc = predict_misc(pl_clean, target)
    delta = predict_delta(delta_series, target)

    if g_override is not None:
        g_val = float(g_override)
        g_source = "external (粗利推計)"
    else:
        g_row = g_monthly[g_monthly["月"] == target]
        if len(g_row) == 0:
            raise ValueError(f"G の値が取れません: {target.strftime('%Y-%m')} "
                             "（g_override を指定してください）")
        g_val = float(g_row["G"].iloc[0])
        g_source = "粗利データ実績"

    op_income = g_val + delta["value"]  # = R − M の予測
    op_balance = (op_income - payroll["value"] - consign["value"]
                  - facility["value"] - misc["value"])

    return {
        "月": target,
        "予測医業収支": op_balance,
        "予測R_minus_M": op_income,
        "G": {"value": g_val, "source": g_source},
        "δ": delta,
        "給与費": payroll,
        "委託費": consign,
        "設備関係費": facility,
        "経費": misc,
    }


def backtest(pl_clean: pd.DataFrame,
              delta_series: pd.DataFrame,
              g_monthly: pd.DataFrame,
              n_holdout: int = 8) -> pd.DataFrame:
    """直近 n か月をホールドアウトし、医業収支予測の精度を測定。

    G は実績を使うため、ここでの誤差は「費目モデルの誤差」のみ反映。
    """
    months = delta_series["月"].sort_values().tolist()
    test_months = months[-n_holdout:]

    rows = []
    for tm in test_months:
        train_pl = pl_clean[pl_clean["月"] < tm]
        train_d = delta_series[delta_series["月"] < tm]
        if len(train_pl) < 12 or len(train_d) < 6:
            continue
        actual = pl_clean[pl_clean["月"] == tm]
        if len(actual) == 0:
            continue
        result = project_monthly_balance(train_pl, train_d, g_monthly, tm)
        actual_bal = float(actual["医業収支"].iloc[0])
        rows.append({
            "月": tm,
            "実績医業収支": actual_bal,
            "予測医業収支": result["予測医業収支"],
            "誤差": result["予測医業収支"] - actual_bal,
            "G実績": result["G"]["value"],
            "δ予測": result["δ"]["value"],
            "給与費予測": result["給与費"]["value"],
            "給与費実績": float(actual["給与費"].iloc[0]),
            "委託費予測": result["委託費"]["value"],
            "委託費実績": float(actual["委託費"].iloc[0]),
            "設備関係費予測": result["設備関係費"]["value"],
            "設備関係費実績": float(actual["設備関係費"].iloc[0]),
            "経費予測": result["経費"]["value"],
            "経費実績": float(actual["経費"].iloc[0]),
        })
    df = pd.DataFrame(rows)
    return df


# ──────────────────────────────────────────
# エントリポイント（テスト用）
# ──────────────────────────────────────────

def load_and_project(data_dir: str = "data",
                      target: Optional[pd.Timestamp] = None,
                      g_override: Optional[float] = None) -> dict:
    """データを読み込み、指定月の医業収支予測を返す（公開API）。"""
    from .data_loader import load_profit_breakdown
    pl = load_pl_history(data_dir)
    pl_c = clean_pl(pl)
    pb = load_profit_breakdown(data_dir)
    g = aggregate_profit_monthly(pb)
    delta = compute_delta_series(pl_c, g)
    if target is None:
        target = (pl_c["月"].max() + pd.offsets.MonthBegin(1))
    return project_monthly_balance(pl_c, delta, g, target, g_override=g_override)
