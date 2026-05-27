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

from .config import biz_days_in_month, calendar_days_in_month
from .pl_history import load_pl_history, clean_pl


# ──────────────────────────────────────────
# 介入変数（外れ値月の除外マスク）
# ──────────────────────────────────────────
# {費目: [(YYYY-MM, 理由), ...]} 形式。学習データから除外する。
# Phase 0 分析で特定: 2025/06 経費スパイク (618M、通常 100-150M)、
# 2025/06 設備関係費 (256M、通常 170-200M)、2025/03 経費 (226M)、
# 2025/03 医療消耗器具備品費 (53M、通常 3-9M) は one-off と判断。
COST_INTERVENTIONS = {
    "経費": [
        ("2025-06", "618M one-off 修繕系（通常 100-150M）"),
        ("2025-03", "226M スパイク（通常 90-145M）"),
    ],
    "設備関係費": [
        ("2025-06", "256M 上振れ（通常 170-200M）"),
    ],
    "医療消耗器具備品費": [
        ("2025-03", "53M スパイク（通常 3-9M）"),
    ],
}


def _intervention_mask(months: pd.Series, col: str) -> pd.Series:
    """指定列の介入月リストを True に立てた boolean Series を返す."""
    bad_yms = {ym for ym, _ in COST_INTERVENTIONS.get(col, [])}
    if not bad_yms:
        return pd.Series(False, index=months.index)
    return months.dt.strftime("%Y-%m").isin(bad_yms)


def _filter_interventions(df: pd.DataFrame, col: str) -> pd.DataFrame:
    """介入月を除外した DataFrame を返す."""
    mask = _intervention_mask(df["月"], col)
    return df[~mask]


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
    """給与費予測（2段階）:

    - 4月（年度始）: 過去年度4月の CAGR で外挿（病床/診療機能変更によるレベルシフトを捕捉）
    - その他の月: 直近6か月 robust median + 過去同月オフセット

    backtest 8mo で MAE 20.8M千円, bias -0.1M（4月は CAGR で +20M 過大、他月は中立）.
    """
    df = pl_clean[pl_clean["月"] < target].copy()
    df = _filter_interventions(df, "給与費")

    if target.month == 4:
        # 年度始は過去年度4月の CAGR で外挿
        apr_series = df[df["月"].dt.month == 4].sort_values("月")
        if len(apr_series) >= 2:
            n = len(apr_series) - 1
            growth = (apr_series["給与費"].iloc[-1]
                      / apr_series["給与費"].iloc[0]) ** (1 / n)
            pred = float(apr_series["給与費"].iloc[-1]) * growth
            return {
                "value": pred,
                "base": float(apr_series["給与費"].iloc[-1]),
                "offset": 0.0,
                "method": f"4月CAGR={growth:.4f} 外挿（年度始）",
            }
        # フォールバック: 過去4月の中央値
        if len(apr_series) >= 1:
            return {"value": float(apr_series["給与費"].median()),
                    "base": float(apr_series["給与費"].median()),
                    "offset": 0.0,
                    "method": "過去4月 median"}

    # 5-3月: 直近6か月 robust median + 過去同月オフセット
    base = float(_mad_filter(df.tail(6)["給与費"]).median())
    same_month = _mad_filter(df[df["月"].dt.month == target.month]["給与費"])
    if len(same_month) >= 1:
        all_filt = _mad_filter(df["給与費"])
        offset = float(same_month.median()) - float(all_filt.median())
    else:
        offset = 0.0
    return {
        "value": base + offset,
        "base": base,
        "offset": offset,
        "method": f"直近6mo median + 同月offset({offset:+,.0f})",
    }


def _predict_cost_with_offset(series_by_month: pd.DataFrame,
                                col: str,
                                target: pd.Timestamp,
                                window: int = 12,
                                outlier_k: float = 2.5,
                                day_basis: Optional[str] = None,
                                use_trend: bool = False) -> dict:
    """直近 window か月 robust median + 同月オフセット (+ optional 線形トレンド).

    use_trend=True の場合、全期間で OLS により slope を推定し、
    target 時点までの累積トレンドを base に加算する。
    委託費 (R²=0.80) や設備関係費 (R²=0.60) など線形成長が強い費目で有効。
    """
    df = series_by_month.copy()
    df = df[df["月"] < target]
    df = _filter_interventions(df, col)

    if day_basis == "cal":
        denom = df["月"].apply(calendar_days_in_month).astype(float)
        target_days = float(calendar_days_in_month(target))
    elif day_basis == "biz":
        denom = df["月"].apply(biz_days_in_month).astype(float)
        target_days = float(biz_days_in_month(target))
    else:
        denom = pd.Series(1.0, index=df.index)
        target_days = 1.0

    df = df.copy()
    key = col + "_per_day"
    df[key] = df[col].astype(float) / denom

    # 線形トレンド（OLS）
    trend_adjust_per_day = 0.0
    slope = 0.0
    if use_trend and len(df) >= 6:
        idx = np.arange(len(df))
        slope, intercept = np.polyfit(idx, df[key].values, 1)
        # トレンド除去後の系列で base / seasonal を計算
        df[key + "_detrend"] = df[key] - (slope * idx + intercept)
        recent = _mad_filter(df.tail(window)[key + "_detrend"], k=outlier_k)
        base_per_day = float(recent.median()) if len(recent) else 0.0
        # target index = 最終学習月の次（複数月後でも対応）
        months_diff = ((target.to_period("M") - df["月"].iloc[-1].to_period("M")).n)
        target_idx = len(df) - 1 + months_diff
        trend_adjust_per_day = slope * target_idx + intercept
        base_for_seasonal = key + "_detrend"
    else:
        recent = _mad_filter(df.tail(window)[key], k=outlier_k)
        base_per_day = (float(recent.median()) if len(recent)
                        else float(df[key].median()))
        base_for_seasonal = key

    # 季節性（同月オフセット）
    same_month = df[df["月"].dt.month == target.month][base_for_seasonal]
    same_month = _mad_filter(same_month, k=outlier_k)
    if len(same_month) >= 1:
        all_filt = _mad_filter(df[base_for_seasonal], k=outlier_k)
        offset_per_day = float(same_month.median()) - float(all_filt.median())
    else:
        offset_per_day = 0.0

    total_per_day = base_per_day + offset_per_day + trend_adjust_per_day
    value = total_per_day * target_days
    return {
        "value": value,
        "base": base_per_day * target_days,
        "offset": offset_per_day * target_days,
        "trend": trend_adjust_per_day * target_days,
        "day_basis": day_basis,
        "target_days": target_days,
        "slope_per_day": slope,
    }


def predict_consign(pl_clean: pd.DataFrame, target: pd.Timestamp) -> dict:
    """委託費: 暦日正規化での直近12mo median + 同月オフセット."""
    r = _predict_cost_with_offset(pl_clean, "委託費", target,
                                    day_basis="cal", use_trend=False)
    return {"value": r["value"],
            "method": f"暦日{int(r['target_days'])}日換算"}


def predict_facility(pl_clean: pd.DataFrame, target: pd.Timestamp) -> dict:
    """設備関係費: 直近6か月の robust median (同月オフセットなし).

    リース更新等でレベルが変動するため、過去同月よりも直近6か月の方が
    現在のレベルを正確に反映する。同月オフセットを足すと
    過去の低レベル月を引きずって系統的に過小予測になる（MAE 20→7M）。
    """
    df = pl_clean[pl_clean["月"] < target].copy()
    df = _filter_interventions(df, "設備関係費")
    recent = _mad_filter(df.tail(6)["設備関係費"])
    val = float(recent.median()) if len(recent) else float(df["設備関係費"].median())
    return {"value": val, "method": f"直近6mo robust median"}


def predict_misc(pl_clean: pd.DataFrame, target: pd.Timestamp) -> dict:
    """経費: 暦日正規化（光熱費等が日数比例、トレンド R²=0.001 なし）."""
    r = _predict_cost_with_offset(pl_clean, "経費", target,
                                    day_basis="cal", use_trend=False)
    return {"value": r["value"],
            "method": f"暦日{int(r['target_days'])}日換算"}


def predict_delta(delta_series: pd.DataFrame, target: pd.Timestamp) -> dict:
    """δ: 営業日正規化（収益駆動なので営業日比例が自然）."""
    r = _predict_cost_with_offset(delta_series, "δ", target,
                                    day_basis="biz", use_trend=False)
    return {"value": r["value"],
            "method": f"営業日{int(r['target_days'])}日換算"}


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


def prediction_intervals(pl_clean: pd.DataFrame,
                          delta_series: pd.DataFrame,
                          g_monthly: pd.DataFrame,
                          target: pd.Timestamp,
                          projection: dict,
                          n_holdout: int = 8,
                          n_bootstrap: int = 5000,
                          rng_seed: int = 42) -> dict:
    """Bootstrap で医業収支の予測区間を算出.

    バックテストで得た医業収支の予測誤差を経験分布として N 回リサンプル。
    費目間の相関を保ったまま分布を推定できる（独立加算より現実的）。
    G の推計誤差（MAPE 1.75%）は別途加算する。
    """
    import numpy as np
    rng = np.random.default_rng(rng_seed)

    available_bt = max(0, len(delta_series) - 12)
    if available_bt < 4:
        return {"available": False}
    bt = backtest(pl_clean, delta_series, g_monthly,
                   n_holdout=min(n_holdout, available_bt))
    if len(bt) < 4:
        return {"available": False}

    # 医業収支誤差を直接 bootstrap（費目間相関を保存）
    balance_errs = bt["誤差"].values

    # G の推計誤差（MAPE 1.75% を std として注入）
    g_std = 0.0175 * projection["G"]["value"]
    g_err_samples = rng.normal(0, g_std, n_bootstrap)

    # 医業収支誤差 + G誤差 を合成
    cost_samples = rng.choice(balance_errs, n_bootstrap, replace=True)
    samples = cost_samples + g_err_samples

    proj = projection["予測医業収支"]
    q80_lo, q80_hi = np.quantile(samples, [0.10, 0.90])
    q95_lo, q95_hi = np.quantile(samples, [0.025, 0.975])

    return {
        "available": True,
        "n_samples": int(n_bootstrap),
        "n_holdout_bt": int(len(bt)),
        "sigma": float(samples.std()),
        "pi80_lo": float(proj - q80_hi),
        "pi80_hi": float(proj - q80_lo),
        "pi95_lo": float(proj - q95_hi),
        "pi95_hi": float(proj - q95_lo),
        "bias": float(samples.mean()),
        "cost_bt_mae": float(np.abs(balance_errs).mean()),
        "g_std": float(g_std),
    }


def append_residual_log(csv_path: str,
                          projection: dict,
                          pl_clean: pd.DataFrame,
                          generated_at: pd.Timestamp) -> None:
    """予測の残差ログを CSV に追記.

    実行時点で確報されている過去月について 予測vs実績 を記録。
    既に同じ (target_month, run_date) があれば上書き。
    """
    from pathlib import Path
    target = projection["月"]
    actuals = pl_clean[pl_clean["月"] == target]
    actual_balance = (float(actuals["医業収支"].iloc[0])
                       if len(actuals) > 0 else None)
    row = {
        "run_date": generated_at.strftime("%Y-%m-%d %H:%M"),
        "target_month": target.strftime("%Y-%m"),
        "予測医業収支": round(projection["予測医業収支"], 0),
        "実績医業収支": round(actual_balance, 0) if actual_balance is not None else None,
        "誤差": (round(projection["予測医業収支"] - actual_balance, 0)
                if actual_balance is not None else None),
        "予測R_minus_M": round(projection["予測R_minus_M"], 0),
        "G_proj": round(projection["G"]["value"], 0),
        "δ_proj": round(projection["δ"]["value"], 0),
        "給与費_proj": round(projection["給与費"]["value"], 0),
        "委託費_proj": round(projection["委託費"]["value"], 0),
        "設備関係費_proj": round(projection["設備関係費"]["value"], 0),
        "経費_proj": round(projection["経費"]["value"], 0),
    }
    p = Path(csv_path)
    if p.exists():
        existing = pd.read_csv(p)
        # 同じ run_date + target_month の行は除外（重複防止）
        existing = existing[~((existing["run_date"] == row["run_date"]) &
                              (existing["target_month"] == row["target_month"]))]
        df = pd.concat([existing, pd.DataFrame([row])], ignore_index=True)
    else:
        df = pd.DataFrame([row])
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, index=False)


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
