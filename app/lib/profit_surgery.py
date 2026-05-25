"""
profit_surgery.py — 術式ベース粗利モデル（NNLS + Ridge）と OLS のハイブリッド選択
================================================================

【背景】
  既存の profit_estimate.py は「件数」だけで粗利を推計するが、
  産婦人科・眼科などケースミックスが大きく動く科では精度が出ない。
  術式名（主術式）+ 手術時間を説明変数に加えた NNLS で単価を学習し、
  holdout で件数OLSを上回る科に限り採用する。

【モデル】
  科ごとに月次:
    粗利_m ≒ Σ_j β_j × 件数_jm + γ × 総手術時間_m + α (切片＝手術外粗利)
    β_j ≥ 0, γ ≥ 0, α ≥ 0  （非負最小二乗）

  Ridge 正則化（切片を除く）で過適合を抑える。

【ハイブリッド選択】
  科ごとに直近 N_test ヶ月で holdout 評価し、
  NNLS の R²_out が件数 OLS を上回れば NNLS を採用。

【公開API】
  fit_hybrid_models(prof_monthly, surg, ...)
    → {科: {model: "nnls"|"ols", ...}} の辞書
  predict_monthly_profit(models, surg_window, month)
    → {科: 推計粗利}
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from typing import Dict, Any, List, Optional


# ────────────────────────────────────────────────────
# 内部: NNLS (Lawson-Hanson, scipy 非依存)
# ────────────────────────────────────────────────────

def _nnls(X: np.ndarray, y: np.ndarray, max_iter: int = 500, tol: float = 1e-10) -> np.ndarray:
    m, n = X.shape
    x = np.zeros(n)
    P = np.zeros(n, dtype=bool)
    R = np.ones(n, dtype=bool)
    w = X.T @ (y - X @ x)
    it = 0
    while R.any() and (w[R].max() if R.any() else -1) > tol and it < max_iter:
        it += 1
        idxs = np.where(R)[0]
        j = idxs[np.argmax(w[idxs])]
        P[j] = True
        R[j] = False
        inner = 0
        while inner < max_iter:
            inner += 1
            Xp = X[:, P]
            s_p, *_ = np.linalg.lstsq(Xp, y, rcond=None)
            if (s_p >= 0).all():
                x = np.zeros(n)
                x[P] = s_p
                break
            neg = s_p < 0
            ratios = x[P][neg] / (x[P][neg] - s_p[neg])
            alpha = ratios.min()
            x[P] = x[P] + alpha * (s_p - x[P])
            zero_idx = np.where((np.abs(x) < 1e-12) & P)[0]
            for zi in zero_idx:
                P[zi] = False
                R[zi] = True
        w = X.T @ (y - X @ x)
    return x


def _nnls_ridge(X: np.ndarray, y: np.ndarray,
                 lam: float = 5.0,
                 penalize_intercept: bool = False) -> np.ndarray:
    """Ridge付き NNLS。最後列を切片とみなす場合は penalize_intercept=False。"""
    n_feat = X.shape[1]
    reg_rows = np.sqrt(lam) * np.eye(n_feat)
    if not penalize_intercept:
        reg_rows[-1, -1] = 0.0
    X_aug = np.vstack([X, reg_rows])
    y_aug = np.concatenate([y, np.zeros(n_feat)])
    return _nnls(X_aug, y_aug)


# ────────────────────────────────────────────────────
# 内部: 術式抽出と (科×月) ピボット
# ────────────────────────────────────────────────────

def _extract_primary(surg: pd.DataFrame) -> pd.DataFrame:
    """確定術式から主術式（改行区切りの1行目）と手術時間を抽出。
    NaN行は除外、必要列は保持。"""
    if "確定術式" not in surg.columns:
        return surg.iloc[0:0].copy()
    s = surg.dropna(subset=["実施診療科", "入外区分", "確定術式"]).copy()
    s["主術式"] = s["確定術式"].astype(str).str.split(r"[\r\n]+").str[0].str.strip()
    s = s[s["主術式"] != ""].copy()
    s["実施診療科"] = s["実施診療科"].astype(str).str.strip()
    s["入外区分"]  = s["入外区分"].astype(str).str.strip()
    s["手術時間_h"] = pd.to_numeric(s.get("予定手術時間"), errors="coerce").fillna(0) / 60.0
    s["術式キー"]  = s["実施診療科"] + "|" + s["入外区分"] + "|" + s["主術式"]
    s["月"] = pd.to_datetime(s["手術実施日"]).dt.strftime("%Y-%m")
    return s


def _reduce_low_freq(surg_ext: pd.DataFrame,
                      train_months: List[str],
                      min_count: int = 30) -> pd.DataFrame:
    """学習期間中の頻度が min_count 未満の術式キーを 科|入外|その他 に集約。"""
    freq = surg_ext[surg_ext["月"].isin(train_months)].groupby("術式キー").size()
    low = set(freq[freq < min_count].index)
    def reduce_(k):
        if k in low:
            d, io_, _ = k.split("|", 2)
            return f"{d}|{io_}|その他"
        return k
    out = surg_ext.copy()
    out["術式キー_集約"] = out["術式キー"].apply(reduce_)
    return out


def _build_dept_pivot(surg_ext: pd.DataFrame, dept: str):
    """科の (月 × 術式キー集約) ピボットと (月 → 総手術時間) を返す。"""
    sd = surg_ext[surg_ext["実施診療科"] == dept]
    if len(sd) == 0:
        return None, None
    pv = sd.pivot_table(index="月", columns="術式キー_集約",
                          values="主術式", aggfunc="count", fill_value=0).sort_index()
    tm = sd.groupby("月")["手術時間_h"].sum()
    return pv, tm


# ────────────────────────────────────────────────────
# 内部: R² 計算
# ────────────────────────────────────────────────────

def _r2_out(y_true: np.ndarray, y_pred: np.ndarray, y_train_mean: float) -> Optional[float]:
    denom = float(np.sum((y_true - y_train_mean) ** 2))
    if denom <= 0:
        return None
    ss_res = float(np.sum((y_true - y_pred) ** 2))
    return 1.0 - ss_res / denom


def _mape(y_true: np.ndarray, y_pred: np.ndarray) -> Optional[float]:
    mask = y_true > 0
    if not mask.any():
        return None
    return float(np.mean(np.abs((y_true[mask] - y_pred[mask]) / y_true[mask])) * 100)


# ────────────────────────────────────────────────────
# 公開API: 科ごとのハイブリッド学習
# ────────────────────────────────────────────────────

def fit_hybrid_models(prof_monthly: pd.DataFrame,
                       surg: pd.DataFrame,
                       test_months: int = 2,
                       min_count: int = 30,
                       ridge_lambda: float = 5.0,
                       min_train_months: int = 6) -> Dict[str, Any]:
    """科ごとに NNLS と件数OLS を holdout 評価し、勝った方を採用。

    Args:
        prof_monthly: columns = ['科', '月', '粗利_百万']
        surg: 手術データ（確定術式・予定手術時間 列を含む）
        test_months: holdout する直近月数
        min_count: 低頻度集約閾値
        ridge_lambda: NNLS Ridge 強度
        min_train_months: 学習に必要な最小月数

    Returns:
        {科: {
            'model': 'nnls' | 'ols',
            'r2_out_nnls': float|None, 'r2_out_ols': float|None,
            'mape_nnls': float|None,  'mape_ols': float|None,
            'features': [術式キー_集約, ...],  # NNLS の場合
            'coef': [β_j, γ_time, α_intercept],  # NNLS の場合
            'ols_slope': float, 'ols_intercept': float,  # OLS の場合
            'train_months': [..], 'test_months': [..],
        }}
    """
    se = _extract_primary(surg)
    if len(se) == 0 or len(prof_monthly) == 0:
        return {}

    all_months = sorted(se["月"].unique())
    if len(all_months) < min_train_months + test_months:
        return {}
    train_months = all_months[:-test_months]
    test_ms      = all_months[-test_months:]
    se = _reduce_low_freq(se, train_months, min_count=min_count)

    # 件数OLS 用: 全麻件数 (麻酔種別含む)
    if "麻酔種別" in se.columns:
        ga_mask = se["麻酔種別"].fillna("").str.contains("全身麻酔", na=False)
    else:
        ga_mask = pd.Series(True, index=se.index)
    ga_count_by_dm = se[ga_mask].groupby(["実施診療科", "月"]).size().rename("全麻件数")

    out = {}
    for dept in sorted(se["実施診療科"].unique()):
        pv, tm = _build_dept_pivot(se, dept)
        if pv is None:
            continue
        p_d = prof_monthly[prof_monthly["科"] == dept].set_index("月")["粗利_百万"]
        tr_idx = [m for m in train_months if m in pv.index and m in p_d.index]
        te_idx = [m for m in test_ms      if m in pv.index and m in p_d.index]
        if len(tr_idx) < min_train_months or len(te_idx) < 1:
            continue
        # NNLS 特徴量: [術式件数..., 手術時間, 切片]
        X_tr = pv.loc[tr_idx].values.astype(float)
        X_te = pv.loc[te_idx].values.astype(float)
        t_tr = tm.reindex(tr_idx).fillna(0).values.astype(float).reshape(-1, 1)
        t_te = tm.reindex(te_idx).fillna(0).values.astype(float).reshape(-1, 1)
        ones_tr = np.ones((len(tr_idx), 1))
        ones_te = np.ones((len(te_idx), 1))
        Xb_tr = np.hstack([X_tr, t_tr, ones_tr])
        Xb_te = np.hstack([X_te, t_te, ones_te])
        y_tr = p_d.loc[tr_idx].values.astype(float)
        y_te = p_d.loc[te_idx].values.astype(float)
        beta = _nnls_ridge(Xb_tr, y_tr, lam=ridge_lambda)
        yp_te_nnls = Xb_te @ beta
        r2_nnls = _r2_out(y_te, yp_te_nnls, y_tr.mean())
        mape_nnls = _mape(y_te, yp_te_nnls)

        # 件数 OLS（切片あり、件数のみ）
        ga = ga_count_by_dm.xs(dept, level=0, drop_level=False) if dept in ga_count_by_dm.index.get_level_values(0) else None
        if ga is not None and not ga.empty:
            ga = ga.droplevel(0)  # 月だけ残す
        else:
            ga = pd.Series(dtype=float)
        x_tr = ga.reindex(tr_idx).fillna(0).values.astype(float)
        x_te = ga.reindex(te_idx).fillna(0).values.astype(float)
        A_tr = np.vstack([x_tr, np.ones_like(x_tr)]).T
        A_te = np.vstack([x_te, np.ones_like(x_te)]).T
        ols_coef, *_ = np.linalg.lstsq(A_tr, y_tr, rcond=None)
        yp_te_ols = A_te @ ols_coef
        r2_ols = _r2_out(y_te, yp_te_ols, y_tr.mean())
        mape_ols = _mape(y_te, yp_te_ols)

        # 採用判定: R²_out が高い方。両方 None なら NNLS（過適合警告付き）
        def _key(x):
            return -np.inf if x is None else x
        chosen = "nnls" if _key(r2_nnls) >= _key(r2_ols) else "ols"

        rec = {
            "model": chosen,
            "r2_out_nnls": round(r2_nnls, 3) if r2_nnls is not None else None,
            "r2_out_ols":  round(r2_ols, 3)  if r2_ols  is not None else None,
            "mape_nnls": round(mape_nnls, 1) if mape_nnls is not None else None,
            "mape_ols":  round(mape_ols, 1)  if mape_ols  is not None else None,
            "train_months": tr_idx,
            "test_months": te_idx,
            "n_procedures": int(pv.shape[1]),
            # 比較用に両モデルの係数を常に保存
            "features":      list(pv.columns) + ["手術時間_h", "切片"],
            "coef":          [float(b) for b in beta],
            "ols_slope":     float(ols_coef[0]),
            "ols_intercept": float(ols_coef[1]),
        }
        out[dept] = rec

    return out


def predict_monthly_profit_nnls(model_rec: Dict[str, Any],
                                  surg_window: pd.DataFrame,
                                  dept: str) -> float:
    """学習済み NNLS モデルで「指定された手術ウィンドウ」の粗利を推計。"""
    if model_rec.get("model") != "nnls":
        return 0.0
    se = _extract_primary(surg_window)
    sd = se[se["実施診療科"] == dept]
    if len(sd) == 0:
        return float(model_rec["coef"][-1])  # 切片のみ
    feats = model_rec["features"]
    coef = np.array(model_rec["coef"])
    proc_feats = feats[:-2]
    known = set(proc_feats)
    def to_feat(k):
        if k in known:
            return k
        d, io_, _ = k.split("|", 2)
        other = f"{d}|{io_}|その他"
        return other if other in known else None
    counts = {f: 0 for f in proc_feats}
    for _, row in sd.iterrows():
        k = row["術式キー"]
        f = to_feat(k)
        if f is not None:
            counts[f] += 1
    time_sum = float(sd["手術時間_h"].sum())
    x = np.array([counts[f] for f in proc_feats] + [time_sum, 1.0])
    pred = float(np.maximum(0.0, np.dot(x, coef)))
    return pred


# ────────────────────────────────────────────────────
# 公開API: 日次ローリング推計
# ────────────────────────────────────────────────────

def _to_other_feat(k: str, known: set) -> Optional[str]:
    if k in known:
        return k
    parts = k.split("|", 2)
    if len(parts) < 3:
        return None
    d, io_, _ = parts
    other = f"{d}|{io_}|その他"
    return other if other in known else None


def predict_daily_rolling_per_dept(model_rec: Dict[str, Any],
                                     surg_kind: pd.DataFrame,
                                     dept: str,
                                     dates: pd.DatetimeIndex,
                                     rolling_days: int = 30,
                                     force_kind: Optional[str] = None) -> pd.Series:
    """1科×1区分（外来/入院）の日次ローリング推計を返す。

    Args:
        force_kind: "nnls" | "ols" を指定すると、採用モデルを上書きして
                    指定モデルで予測する（比較用）。None なら model_rec['model'] を使う。
    """
    zero = pd.Series(0.0, index=dates)
    if model_rec is None or len(surg_kind) == 0:
        return zero

    kind = force_kind if force_kind else model_rec["model"]

    if kind == "ols":
        # 全麻件数を日次集計 → rolling sum → slope*x + intercept
        if "麻酔種別" in surg_kind.columns:
            ga = surg_kind[surg_kind["麻酔種別"].fillna("")
                              .str.contains("全身麻酔", na=False)]
        else:
            ga = surg_kind
        ga = ga[ga["実施診療科"] == dept]
        if len(ga) == 0:
            base = float(model_rec.get("ols_intercept", 0.0))
            return pd.Series(max(0.0, base), index=dates)
        daily = (ga.assign(_d=pd.to_datetime(ga["手術実施日"]).dt.normalize())
                    .groupby("_d").size().reindex(dates, fill_value=0))
        roll = daily.rolling(rolling_days, min_periods=1).sum()
        pred = roll.values * model_rec["ols_slope"] + model_rec["ols_intercept"]
        return pd.Series(np.maximum(0.0, pred), index=dates)

    # NNLS
    se = _extract_primary(surg_kind)
    sd = se[se["実施診療科"] == dept].copy()
    if len(sd) == 0:
        return pd.Series(max(0.0, float(model_rec["coef"][-1])), index=dates)
    feats = model_rec["features"]
    coef = np.array(model_rec["coef"])
    proc_feats = feats[:-2]
    known = set(proc_feats)
    sd["_feat"] = sd["術式キー"].apply(lambda k: _to_other_feat(k, known))
    sd = sd[sd["_feat"].notna()]
    sd["_d"] = pd.to_datetime(sd["手術実施日"]).dt.normalize()

    # 術式件数 日次ピボット → ローリング合計
    pv = (sd.pivot_table(index="_d", columns="_feat",
                           values="主術式", aggfunc="count", fill_value=0)
            .reindex(columns=proc_feats, fill_value=0)
            .reindex(dates, fill_value=0))
    roll_counts = pv.rolling(rolling_days, min_periods=1).sum()

    # 手術時間 日次合計 → ローリング合計
    time_daily = sd.groupby("_d")["手術時間_h"].sum().reindex(dates, fill_value=0)
    time_roll = time_daily.rolling(rolling_days, min_periods=1).sum()

    pred = (roll_counts.values @ coef[:-2]) + coef[-2] * time_roll.values + coef[-1]
    return pd.Series(np.maximum(0.0, pred), index=dates)
