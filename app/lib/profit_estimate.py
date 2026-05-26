"""
profit_estimate.py — 直近30日 粗利推計（2式・手術入外分離）
================================================================

【モデル】
  外来粗利_m = α·営業日数_m + β·外来手術件数_m
  入院粗利_m = d·入院手術件数_m + e·新入院_m + f·純在院延べ_m
  （切片なし、過去最大12ヶ月で診療科ごとに OLS フィット）

【ドライバー】
  - 営業日数:    config.is_operational_day で平日判定
  - 外来手術件数: 入外区分 == "外来" の手術件数
  - 入院手術件数: 入外区分 == "入院" の手術件数
  - 新入院:      preprocess_admission の「新入院患者数」(入院+緊急入院)
  - 純在院延べ:  Σ在院患者数 - Σ新入院（新入院当日分の二重計上を控除）

【出力】
  直近30日（base_date 終端）の推計値 + 日次ローリング系列を返す。
  推計はあくまで「過去12ヶ月の単価構造」前提のため、実績との
  乖離は構造変化（診療体制・コーディング変更）のサイン。

【注意】
  - 学習サンプル数 ≦ 12 → 多重共線性で係数が不安定になる科がある。
    fit_quality.R² を併記してUI側で信頼度を見せる前提。
  - 推計値が負になった場合は 0 にクリップする（外挿の暴走防止）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from typing import Optional, Dict, Any

from .config import biz_days_in_month, is_operational_day


# ────────────────────────────────────────────────────
# 内部ユーティリティ
# ────────────────────────────────────────────────────

def _month_floor(d) -> pd.Timestamp:
    return pd.Timestamp(d).normalize().replace(day=1)


def _surg_split_masks(surg: pd.DataFrame):
    """入外区分の有無を吸収。列がなければ全件「入院」扱い。"""
    if "入外区分" in surg.columns:
        is_in  = surg["入外区分"] == "入院"
        is_out = surg["入外区分"] == "外来"
    else:
        is_in  = pd.Series(True,  index=surg.index)
        is_out = pd.Series(False, index=surg.index)
    return is_in, is_out


def _aggregate_monthly_drivers(adm: pd.DataFrame,
                                surg: pd.DataFrame,
                                months: list) -> pd.DataFrame:
    """各 (診療科, 月) のドライバー集計値。学習・バックテスト両用。"""
    adm = adm.copy()
    adm["月"] = adm["日付"].apply(_month_floor)
    adm_m = (adm.groupby(["診療科名", "月"], as_index=False)
                 .agg(新入院=("新入院患者数", "sum"),
                      在院延べ=("在院患者数", "sum")))
    adm_m["純在院延べ"] = (adm_m["在院延べ"] - adm_m["新入院"]).clip(lower=0)

    surg = surg.copy()
    surg["月"] = surg["手術実施日"].apply(_month_floor)
    is_in, is_out = _surg_split_masks(surg)
    inp_op = (surg[is_in].groupby(["実施診療科", "月"], as_index=False).size()
                .rename(columns={"実施診療科": "診療科名", "size": "入院手術件数"}))
    out_op = (surg[is_out].groupby(["実施診療科", "月"], as_index=False).size()
                .rename(columns={"実施診療科": "診療科名", "size": "外来手術件数"}))

    out = (adm_m.merge(inp_op, on=["診療科名", "月"], how="outer")
                 .merge(out_op, on=["診療科名", "月"], how="outer"))
    for c in ("入院手術件数", "外来手術件数", "新入院", "在院延べ", "純在院延べ"):
        if c not in out.columns:
            out[c] = 0
        out[c] = out[c].fillna(0).astype(int)
    out["営業日数"] = out["月"].apply(biz_days_in_month).astype(int)

    if months:
        out = out[out["月"].isin(months)]
    return out.reset_index(drop=True)


def _fit_ols_no_intercept(X: np.ndarray, y: np.ndarray) -> dict:
    """OLS（切片なし）。R² は通常の決定係数。"""
    n = int(len(y))
    k = X.shape[1]
    if n < max(3, k) or np.all(y == 0) or np.all(X == 0):
        return {"coef": [0.0] * k, "r2": None, "n": n}
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    y_pred = X @ coef
    ss_res = float(np.sum((y - y_pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = (1.0 - ss_res / ss_tot) if ss_tot > 0 else None
    if r2 is not None:
        # 過学習で 1.0 を超えることはないが、負値は学習失敗扱いで None に
        if r2 < 0:
            r2 = None
        else:
            r2 = round(r2, 3)
    return {"coef": [float(c) for c in coef], "r2": r2, "n": n}


# ────────────────────────────────────────────────────
# 係数フィット
# ────────────────────────────────────────────────────

def fit_profit_estimators(profit_breakdown: pd.DataFrame,
                           adm: pd.DataFrame,
                           surg: pd.DataFrame,
                           lookback_months: int = 12) -> Dict[str, Any]:
    """診療科ごとに外来/入院 2式の係数をフィット。

    Returns:
        { dept: {
            "gairai": {"alpha": float, "beta": float, "r2": float|None, "n": int},
            "nyuin":  {"d":     float, "e":    float, "f": float, "r2": float|None, "n": int},
        }, ... }
    """
    if profit_breakdown is None or len(profit_breakdown) == 0:
        return {}

    pb = profit_breakdown.copy()
    pb["月"] = pd.to_datetime(pb["月"]).apply(_month_floor)
    end_month   = pb["月"].max()
    start_month = end_month - pd.DateOffset(months=lookback_months - 1)
    months = pd.date_range(start_month, end_month, freq="MS").tolist()

    drivers = _aggregate_monthly_drivers(adm, surg, months)

    pb_g = pb[pb["区分"] == "外来"].rename(columns={"粗利": "外来粗利"})
    pb_n = pb[pb["区分"] == "入院"].rename(columns={"粗利": "入院粗利"})
    merged = (drivers
              .merge(pb_g[["診療科名", "月", "外来粗利"]], on=["診療科名", "月"], how="inner")
              .merge(pb_n[["診療科名", "月", "入院粗利"]], on=["診療科名", "月"], how="inner"))

    estimators: Dict[str, Any] = {}
    for dept, grp in merged.groupby("診療科名"):
        X_g = grp[["営業日数", "外来手術件数"]].astype(float).values
        y_g = grp["外来粗利"].astype(float).values
        fit_g = _fit_ols_no_intercept(X_g, y_g)

        X_n = grp[["入院手術件数", "新入院", "純在院延べ"]].astype(float).values
        y_n = grp["入院粗利"].astype(float).values
        fit_n = _fit_ols_no_intercept(X_n, y_n)

        estimators[dept] = {
            "gairai": {"alpha": fit_g["coef"][0], "beta": fit_g["coef"][1],
                       "r2": fit_g["r2"], "n": fit_g["n"]},
            "nyuin":  {"d":     fit_n["coef"][0], "e":    fit_n["coef"][1],
                       "f":     fit_n["coef"][2], "r2": fit_n["r2"], "n": fit_n["n"]},
        }
    return estimators


# ────────────────────────────────────────────────────
# 推計の合成
# ────────────────────────────────────────────────────

def _predict_kpis(est: dict, drv: dict) -> dict:
    g = est["gairai"]; n = est["nyuin"]
    gairai = g["alpha"] * drv["営業日数"] + g["beta"] * drv["外来手術件数"]
    nyuin  = (n["d"] * drv["入院手術件数"]
              + n["e"] * drv["新入院"]
              + n["f"] * drv["純在院延べ"])
    gairai = max(0.0, float(gairai))
    nyuin  = max(0.0, float(nyuin))
    return {"gairai": gairai, "nyuin": nyuin, "total": gairai + nyuin}


def _window_drivers_daily(adm: pd.DataFrame,
                           surg: pd.DataFrame,
                           end_date: pd.Timestamp,
                           window_days: int) -> pd.DataFrame:
    """[end_date - (window_days-1), end_date] の科別ドライバー集計（点）"""
    start_date = end_date - pd.Timedelta(days=window_days - 1)
    a = adm[(adm["日付"] >= start_date) & (adm["日付"] <= end_date)]
    s = surg[(surg["手術実施日"] >= start_date) & (surg["手術実施日"] <= end_date)]
    biz = sum(1 for d in pd.date_range(start_date, end_date, freq="D")
              if is_operational_day(d))

    a_g = (a.groupby("診療科名", as_index=False)
            .agg(新入院=("新入院患者数", "sum"),
                 在院延べ=("在院患者数", "sum")))
    a_g["純在院延べ"] = (a_g["在院延べ"] - a_g["新入院"]).clip(lower=0)

    is_in, is_out = _surg_split_masks(s)
    inp = (s[is_in].groupby("実施診療科", as_index=False).size()
            .rename(columns={"実施診療科": "診療科名", "size": "入院手術件数"}))
    out = (s[is_out].groupby("実施診療科", as_index=False).size()
            .rename(columns={"実施診療科": "診療科名", "size": "外来手術件数"}))
    df = a_g.merge(inp, on="診療科名", how="outer").merge(out, on="診療科名", how="outer")
    for c in ("入院手術件数", "外来手術件数", "新入院", "在院延べ", "純在院延べ"):
        if c not in df.columns:
            df[c] = 0
        df[c] = df[c].fillna(0).astype(int)
    df["営業日数"] = biz
    return df


def _daily_rolling_drivers(adm: pd.DataFrame,
                            surg: pd.DataFrame,
                            dates: pd.DatetimeIndex,
                            window_days: int) -> dict:
    """全期間の日次ドライバーを科別に保持して rolling sum を取る。

    Returns:
        {
          "biz_roll":  pd.Series(index=dates),                   # 全科共通
          dept: {
            "new_roll":  pd.Series, "pure_bed_roll": pd.Series,
            "inp_roll":  pd.Series, "out_roll":     pd.Series,
          }, ...
        }
    """
    biz_flag = pd.Series([1 if is_operational_day(d) else 0 for d in dates], index=dates)
    biz_roll = biz_flag.rolling(window_days, min_periods=1).sum()

    adm_d = (adm.groupby(["診療科名", "日付"], as_index=False)
                 .agg(新入院=("新入院患者数", "sum"),
                      在院延べ=("在院患者数", "sum")))

    is_in, is_out = _surg_split_masks(surg)
    s_in  = (surg[is_in].groupby(["実施診療科", "手術実施日"], as_index=False).size()
              .rename(columns={"実施診療科": "診療科名",
                                "手術実施日": "日付", "size": "入院手術件数"}))
    s_out = (surg[is_out].groupby(["実施診療科", "手術実施日"], as_index=False).size()
              .rename(columns={"実施診療科": "診療科名",
                                "手術実施日": "日付", "size": "外来手術件数"}))

    by_dept: Dict[str, dict] = {}
    depts = set(adm_d["診療科名"].unique()) | set(s_in.get("診療科名", pd.Series([])).unique()) \
                                              | set(s_out.get("診療科名", pd.Series([])).unique())
    for dept in depts:
        a = (adm_d[adm_d["診療科名"] == dept]
              .set_index("日付")[["新入院", "在院延べ"]]
              .reindex(dates, fill_value=0))
        sin = (s_in[s_in["診療科名"] == dept]
                .set_index("日付")["入院手術件数"]
                .reindex(dates, fill_value=0))
        sou = (s_out[s_out["診療科名"] == dept]
                .set_index("日付")["外来手術件数"]
                .reindex(dates, fill_value=0))
        new_roll  = a["新入院"].rolling(window_days, min_periods=1).sum()
        bed_roll  = a["在院延べ"].rolling(window_days, min_periods=1).sum()
        pure_bed  = (bed_roll - new_roll).clip(lower=0)
        inp_roll  = sin.rolling(window_days, min_periods=1).sum()
        out_roll  = sou.rolling(window_days, min_periods=1).sum()
        by_dept[dept] = {
            "new_roll":      new_roll,
            "pure_bed_roll": pure_bed,
            "inp_roll":      inp_roll,
            "out_roll":      out_roll,
        }
    return {"biz_roll": biz_roll, "by_dept": by_dept}


# ────────────────────────────────────────────────────
# 公開関数
# ────────────────────────────────────────────────────

def build_estimate_payload(profit_breakdown: pd.DataFrame,
                            adm: pd.DataFrame,
                            surg: pd.DataFrame,
                            base_date,
                            rolling_days: int = 30,
                            history_days: Optional[int] = None) -> Optional[dict]:
    """直近 rolling_days 日の推計 + 日次ローリング推計の系列を返す。

    Args:
        profit_breakdown: [診療科名,月,区分,粗利(千円)]
        adm:  前処理済み入院データ（日付,診療科名,在院患者数,新入院患者数,...）
        surg: 前処理済み手術データ（手術実施日,実施診療科,入外区分,...）
        base_date: 推計の終端日
        rolling_days: ローリング窓（日）
        history_days: 系列の長さ（base_date から遡る日数）

    Returns:
        None もしくは
        {
          "latest":  { dept: {...}, "_hospital": {...} },
          "series":  { dept: {dates,values_total,values_gairai,values_nyuin},
                       "_hospital": {...} },
          "fit_quality": { dept: {gairai_r2, nyuin_r2, n} },
          "meta":   { rolling_days, window_start, window_end, lookback_months, depts_modeled },
        }
        百万円換算。系列のうち最初の (rolling_days-1) 日は None。
    """
    if profit_breakdown is None or len(profit_breakdown) == 0:
        return None
    if adm is None or len(adm) == 0:
        return None

    base_date = pd.Timestamp(base_date).normalize()
    estimators = fit_profit_estimators(profit_breakdown, adm, surg)
    if not estimators:
        return None

    # 日次系列は admission の最古日からカバーする（指定があればその日数に制限）
    earliest = pd.Timestamp(adm["日付"].min()).normalize()
    full_days = int((base_date - earliest).days) + 1
    if history_days is None or history_days > full_days:
        history_days = max(full_days, rolling_days)

    # ── 直近 rolling_days 日（点） ──
    drv_latest = _window_drivers_daily(adm, surg, base_date, rolling_days)
    latest: Dict[str, Any] = {}
    hosp_g = hosp_n = 0.0
    for _, row in drv_latest.iterrows():
        dept = row["診療科名"]
        if dept not in estimators:
            continue
        drivers = {c: int(row[c]) for c in
                   ("営業日数", "外来手術件数", "入院手術件数", "新入院", "純在院延べ")}
        pred = _predict_kpis(estimators[dept], drivers)
        latest[dept] = {
            "gairai": round(pred["gairai"] / 1000, 1),
            "nyuin":  round(pred["nyuin"]  / 1000, 1),
            "total":  round(pred["total"]  / 1000, 1),
            "drivers": drivers,
        }
        hosp_g += pred["gairai"]
        hosp_n += pred["nyuin"]
    latest["_hospital"] = {
        "gairai": round(hosp_g / 1000, 1),
        "nyuin":  round(hosp_n / 1000, 1),
        "total":  round((hosp_g + hosp_n) / 1000, 1),
    }

    # ── 日次ローリング系列 ──
    history_start = base_date - pd.Timedelta(days=history_days - 1)
    dates = pd.date_range(history_start, base_date, freq="D")
    pre = _daily_rolling_drivers(adm, surg, dates, rolling_days)
    biz_roll = pre["biz_roll"]

    cutoff = dates[rolling_days - 1] if len(dates) >= rolling_days else dates[-1]
    series: Dict[str, Any] = {}
    hosp_g_series = pd.Series(0.0, index=dates)
    hosp_n_series = pd.Series(0.0, index=dates)

    for dept, est in estimators.items():
        drv = pre["by_dept"].get(dept)
        if drv is None:
            continue
        g = est["gairai"]; n = est["nyuin"]
        v_g = (g["alpha"] * biz_roll + g["beta"] * drv["out_roll"]).clip(lower=0)
        v_n = (n["d"] * drv["inp_roll"]
               + n["e"] * drv["new_roll"]
               + n["f"] * drv["pure_bed_roll"]).clip(lower=0)
        hosp_g_series = hosp_g_series.add(v_g, fill_value=0)
        hosp_n_series = hosp_n_series.add(v_n, fill_value=0)

        mask = v_g.index >= cutoff
        series[dept] = {
            "dates":         [d.strftime("%Y-%m-%d") for d in dates],
            "values_total":  [round((gv + nv) / 1000, 2) if m else None
                              for gv, nv, m in zip(v_g, v_n, mask)],
            "values_gairai": [round(v / 1000, 2) if m else None
                              for v, m in zip(v_g, mask)],
            "values_nyuin":  [round(v / 1000, 2) if m else None
                              for v, m in zip(v_n, mask)],
        }

    mask = hosp_g_series.index >= cutoff
    series["_hospital"] = {
        "dates":         [d.strftime("%Y-%m-%d") for d in dates],
        "values_total":  [round((gv + nv) / 1000, 2) if m else None
                          for gv, nv, m in zip(hosp_g_series, hosp_n_series, mask)],
        "values_gairai": [round(v / 1000, 2) if m else None
                          for v, m in zip(hosp_g_series, mask)],
        "values_nyuin":  [round(v / 1000, 2) if m else None
                          for v, m in zip(hosp_n_series, mask)],
    }

    fit_quality = {
        dept: {"gairai_r2": e["gairai"]["r2"],
               "nyuin_r2":  e["nyuin"]["r2"],
               "n":         e["gairai"]["n"]}
        for dept, e in estimators.items()
    }

    return {
        "latest":      latest,
        "series":      series,
        "fit_quality": fit_quality,
        "meta": {
            "rolling_days":     rolling_days,
            "window_start":     (base_date - pd.Timedelta(days=rolling_days - 1)).strftime("%Y-%m-%d"),
            "window_end":       base_date.strftime("%Y-%m-%d"),
            "lookback_months":  12,
            "depts_modeled":    sorted(estimators.keys()),
        },
    }


# ════════════════════════════════════════════════════
# ハイブリッド推計（術式NNLS + 件数OLS を holdout で科ごとに採用）
# ════════════════════════════════════════════════════

def _profit_long_by_kind(profit_breakdown: pd.DataFrame, kind: str) -> pd.DataFrame:
    """profit_breakdown を (科, 月, 粗利_百万) のロング形式に。kind: '外来' | '入院'."""
    sub = profit_breakdown[profit_breakdown["区分"] == kind].copy()
    sub["月"] = pd.to_datetime(sub["月"]).dt.strftime("%Y-%m")
    sub = sub.rename(columns={"診療科名": "科"})
    sub["粗利_百万"] = sub["粗利"] / 1000.0
    return sub[["科", "月", "粗利_百万"]]


def _baseline_monthly(profit_breakdown: pd.DataFrame,
                       kind: str,
                       lookback_months: int = 6) -> Dict[str, float]:
    """科ごとの (kind: '外来'|'入院') 粗利 直近 N か月平均（百万円/月）を返す。

    手術件数がほぼ0で件数/術式モデルが学習できない科（例: 歯科口腔外科の
    外来粗利）について、合計値・日次ローリングに実績ベースの代替値を
    供給するために使う。
    """
    sub = profit_breakdown[profit_breakdown["区分"] == kind].copy()
    if sub.empty:
        return {}
    sub["月"] = pd.to_datetime(sub["月"])
    sub = sub.sort_values("月")
    out: Dict[str, float] = {}
    for dept, g in sub.groupby("診療科名"):
        recent = g.tail(lookback_months)
        if recent.empty:
            continue
        out[dept] = float(recent["粗利"].mean()) / 1000.0
    return out


def _hybrid_beats_ratio(rec: Dict[str, Any],
                          prof_long: pd.DataFrame,
                          surg_k: pd.DataFrame,
                          adm_monthly: pd.DataFrame,
                          dept: str,
                          kind: str) -> bool:
    """test_months で hybrid (NNLS/件数OLS) と ratio_fallback の MAPE を比較。

    hybrid を維持: True（MAPE が同等以下）
    demote 推奨:    False（ratio が勝つ）

    比較不能な場合（adm 不足等）は True（hybrid 維持＝従来挙動）を返す。
    """
    from .profit_surgery import predict_monthly_profit_nnls

    test_months = rec.get("test_months") or []
    train_months = rec.get("train_months") or []
    if not test_months or not train_months:
        return True

    p_s_full = prof_long[prof_long["科"] == dept]
    if p_s_full.empty:
        return True
    p_s = p_s_full.set_index("月")["粗利_百万"]

    a_dept = adm_monthly[adm_monthly["診療科名"] == dept]
    if a_dept.empty:
        return True
    a_s = a_dept.set_index("月")
    driver_col = "純在院延べ" if kind == "入院" else "営業日数"
    if driver_col not in a_s.columns:
        return True

    # train_months ∩ (prof, adm) で ratio 単価を算出
    train_ok = [m for m in train_months if m in p_s.index and m in a_s.index]
    if len(train_ok) < 3:
        return True
    total_p = float(p_s.loc[train_ok].sum())
    total_d = float(a_s.loc[train_ok, driver_col].sum())
    if total_d <= 0:
        return True
    unit = total_p / total_d

    # test_months 各月で hybrid 予測と ratio 予測を作り、actual と MAPE 比較
    hyb_preds, ratio_preds, actuals = [], [], []
    for m in test_months:
        if m not in p_s.index or m not in a_s.index:
            continue
        actual = float(p_s.loc[m])
        mstart = pd.Timestamp(m + "-01")
        mend = mstart + pd.offsets.MonthEnd(0)
        window = surg_k[(surg_k["手術実施日"] >= mstart) &
                          (surg_k["手術実施日"] <= mend)]
        if rec.get("model") == "nnls":
            hp = predict_monthly_profit_nnls(rec, window, dept)
        else:
            if "麻酔種別" in window.columns:
                ga = window[window["麻酔種別"].fillna("")
                             .str.contains("全身麻酔", na=False)]
                ga = ga[ga["実施診療科"] == dept]
            else:
                ga = window[window["実施診療科"] == dept]
            cnt = float(len(ga))
            biz = biz_days_in_month(mstart)
            hp = float(max(0.0, rec.get("ols_count_coef", 0.0) * cnt
                                  + rec.get("ols_biz_coef", 0.0) * biz))
        rp = unit * float(a_s.loc[m, driver_col])
        hyb_preds.append(hp)
        ratio_preds.append(rp)
        actuals.append(actual)

    if not actuals:
        return True

    a_arr = np.array(actuals)
    mask = a_arr > 0
    if not mask.any():
        return True
    mape_h = float(np.mean(np.abs((a_arr[mask] - np.array(hyb_preds)[mask]) / a_arr[mask])))
    mape_r = float(np.mean(np.abs((a_arr[mask] - np.array(ratio_preds)[mask]) / a_arr[mask])))
    return mape_h <= mape_r


def build_hybrid_payload(profit_breakdown: pd.DataFrame,
                          surg: pd.DataFrame,
                          base_date,
                          adm: Optional[pd.DataFrame] = None,
                          test_months: int = 2,
                          min_count: int = 30,
                          rolling_days: int = 30,
                          history_days: Optional[int] = None) -> Optional[dict]:
    """ハイブリッド推計を構築。

    外来/入院それぞれで profit_surgery.fit_hybrid_models を呼び、
    科ごとに NNLS と件数OLS の holdout 評価結果と最終予測を返す。

    Returns:
        {
          "by_dept": {
            dept: {
              "外来": {model, r2_out_nnls, r2_out_ols, mape_nnls, mape_ols,
                      latest_month_actual, latest_month_pred, ...},
              "入院": {同上},
              "合計": {latest_actual, latest_ols_pred, latest_hybrid_pred,
                      ols_err_pct, hybrid_err_pct},
            }
          },
          "hospital_total": {
            latest_month, actual, ols_pred, hybrid_pred,
            ols_err_pct, hybrid_err_pct,
          },
          "meta": {test_months, min_count, generated_at, base_date},
        }
    """
    from .profit_surgery import (
        fit_hybrid_models_auto, predict_monthly_profit_nnls,
        predict_daily_rolling_per_dept,
        aggregate_monthly_admission, fit_ratio_fallback,
        evaluate_ratio_fallback_month,
    )
    if profit_breakdown is None or len(profit_breakdown) == 0:
        return None
    if surg is None or len(surg) == 0:
        return None
    if "区分" not in profit_breakdown.columns:
        return None

    base_date = pd.Timestamp(base_date).normalize()

    # adm 月次集計を kind ループ前に用意（weak hybrid demote 判定で使用）
    adm_monthly_pre = (aggregate_monthly_admission(adm)
                        if adm is not None and len(adm) > 0
                        else pd.DataFrame())

    out_by_dept: Dict[str, Dict[str, Any]] = {}
    fit_models = {"外来": {}, "入院": {}}
    demoted_log: Dict[str, list] = {"外来": [], "入院": []}

    for kind in ("外来", "入院"):
        prof_long = _profit_long_by_kind(profit_breakdown, kind)
        surg_k = surg[surg.get("入外区分") == kind] if "入外区分" in surg.columns else surg
        if len(prof_long) == 0 or len(surg_k) == 0:
            continue
        models = fit_hybrid_models_auto(prof_long, surg_k,
                                          test_months=test_months,
                                          min_count=min_count)
        # ── weak hybrid demote ──
        # 各 dept で「NNLS/件数OLS の test_months MAPE」vs「ratio_fallback の同 MAPE」を
        # 比較し、ratio が勝ったら fit_models から外して後段の fit_ratio_fallback に流す。
        # 救急科のように手術データは存在するが粗利の主因が在院数の科を自動検出する。
        if not adm_monthly_pre.empty:
            demoted = []
            for dept, rec in list(models.items()):
                if not _hybrid_beats_ratio(rec, prof_long, surg_k, adm_monthly_pre, dept, kind):
                    demoted.append(dept)
            for dept in demoted:
                del models[dept]
            demoted_log[kind] = demoted
        fit_models[kind] = models

        for dept, rec in models.items():
            # 直近 test 月の最初の月で予測再現（モデル検証用）
            last_month = rec["test_months"][-1] if rec["test_months"] else None
            actual = pred_hybrid = pred_ols = None
            if last_month:
                a = prof_long[(prof_long["科"] == dept) & (prof_long["月"] == last_month)]
                if len(a) > 0:
                    actual = float(a["粗利_百万"].iloc[0])
                # 月境界で術式データをスライス
                mstart = pd.Timestamp(last_month + "-01")
                mend = mstart + pd.offsets.MonthEnd(0)
                window = surg_k[(surg_k["手術実施日"] >= mstart) &
                                  (surg_k["手術実施日"] <= mend)]
                # OLS 予測
                if "麻酔種別" in window.columns:
                    ga_cnt = window[window["麻酔種別"].fillna("")
                                      .str.contains("全身麻酔", na=False)]
                    ga_cnt = ga_cnt[ga_cnt["実施診療科"] == dept]
                else:
                    ga_cnt = window[window["実施診療科"] == dept]
                cnt = float(len(ga_cnt))
                if "ols_count_coef" in rec:
                    biz = biz_days_in_month(mstart)
                    pred_ols = float(max(0.0, rec["ols_count_coef"] * cnt
                                                + rec["ols_biz_coef"] * biz))
                # ハイブリッド予測（採用モデル）
                if rec["model"] == "nnls":
                    pred_hybrid = predict_monthly_profit_nnls(rec, window, dept)
                else:
                    pred_hybrid = pred_ols if pred_ols is not None else 0.0

            dept_rec = out_by_dept.setdefault(dept, {"外来": None, "入院": None, "合計": {}})
            dept_rec[kind] = {
                "model":           rec["model"],
                "r2_out_nnls":     rec["r2_out_nnls"],
                "r2_out_ols":      rec["r2_out_ols"],
                "mape_nnls":       rec["mape_nnls"],
                "mape_ols":        rec["mape_ols"],
                "n_procedures":    rec.get("n_procedures"),
                "lookback_months": rec.get("lookback_months"),
                "last_month":      last_month,
                "actual":          round(actual, 2) if actual is not None else None,
                "ols_pred":        round(pred_ols, 2) if pred_ols is not None else None,
                "hybrid_pred":     round(pred_hybrid, 2) if pred_hybrid is not None else None,
            }

    # last_month を先に確定（baseline 補完で last_month の actual を引くのに必要）
    last_month_g = None
    for rec in out_by_dept.values():
        for k in ("外来", "入院"):
            if rec.get(k) and rec[k].get("last_month"):
                last_month_g = rec[k]["last_month"]

    # ── 外来/入院 baseline 補完 ──
    # 手術データが乏しく件数/術式モデルが学習できなかった (区分, 科) について、
    # profit_breakdown から直近6か月平均を baseline として埋める。
    # これにより合計値・日次ローリングから外来/入院粗利が脱落するのを防ぐ。
    baseline_g = _baseline_monthly(profit_breakdown, "外来")
    baseline_n = _baseline_monthly(profit_breakdown, "入院")
    pb_all_depts = set(profit_breakdown["診療科名"].dropna().unique())
    for dept in pb_all_depts:
        dept_rec = out_by_dept.setdefault(dept, {"外来": None, "入院": None, "合計": {}})
        for kind, bmap in (("外来", baseline_g), ("入院", baseline_n)):
            if dept_rec.get(kind) is not None:
                continue
            if dept not in bmap:
                continue
            actual = None
            if last_month_g:
                mts = pd.Timestamp(last_month_g + "-01")
                sub = profit_breakdown[
                    (profit_breakdown["診療科名"] == dept)
                    & (profit_breakdown["区分"] == kind)
                    & (pd.to_datetime(profit_breakdown["月"]) == mts)
                ]
                if not sub.empty:
                    actual = float(sub["粗利"].iloc[0]) / 1000.0
            base = bmap[dept]
            dept_rec[kind] = {
                "model":            "baseline",
                "r2_out_nnls":      None, "r2_out_ols": None,
                "mape_nnls":        None, "mape_ols":   None,
                "n_procedures":     0,
                "last_month":       last_month_g,
                "actual":           round(actual, 2) if actual is not None else None,
                "ols_pred":         round(base, 2),
                "hybrid_pred":      round(base, 2),
                "baseline_monthly": round(base, 2),
            }

    # ── 比推定フォールバック（hybrid 不在科 + demoted 科） ──
    # 入院: 単価 = Σ_6m 粗利 / Σ_6m 純在院延べ  → 日次は 単価 × 在院数
    # 外来: 単価 = Σ_6m 粗利 / Σ_6m 営業日数    → 日次は 単価 × 営業日
    # base hybrid のある科には何もしない（残差層は廃止）。
    adm_monthly = adm_monthly_pre
    fallback_layers: Dict[str, Dict[str, Dict[str, Any]]] = {"外来": {}, "入院": {}}
    if not adm_monthly.empty:
        for kind in ("外来", "入院"):
            prof_long_k = _profit_long_by_kind(profit_breakdown, kind)
            if len(prof_long_k) == 0:
                continue
            fallback_layers[kind] = fit_ratio_fallback(
                prof_long_k, fit_models.get(kind), adm_monthly, kind
            )
        # フォールバック適用月次 hybrid_pred の置換（baseline 定数の代わり）。
        # base hybrid のある科の hybrid_pred は変更しない。
        for dept, rec in out_by_dept.items():
            for kind in ("外来", "入院"):
                entry = rec.get(kind)
                if not entry:
                    continue
                # base hybrid あり: 何もしない（残差層を廃止）
                if entry.get("model") not in (None, "baseline"):
                    continue
                layer = fallback_layers.get(kind, {}).get(dept)
                if not layer:
                    continue
                lm = entry.get("last_month")
                if not lm:
                    continue
                layer_val = evaluate_ratio_fallback_month(layer, adm_monthly, dept, lm)
                entry["hybrid_pred"] = round(max(0.0, layer_val), 2)
                entry["ols_pred"]    = round(max(0.0, layer_val), 2)
                entry["model"]       = "ratio_fallback"
                entry["fallback_layer"] = {
                    "driver":   layer.get("driver"),
                    "unit":     layer.get("unit"),
                    "n_months": layer.get("n_months"),
                    "month_val": round(layer_val, 2),
                }

    # 合計（外来+入院）の集約と病院全体
    hosp_actual = hosp_ols = hosp_hybrid = 0.0
    hosp_has_any = False
    for dept, rec in out_by_dept.items():
        a = (rec["外来"]["actual"] if rec["外来"] else 0) or 0
        a += (rec["入院"]["actual"] if rec["入院"] else 0) or 0
        o = (rec["外来"]["ols_pred"] if rec["外来"] else 0) or 0
        o += (rec["入院"]["ols_pred"] if rec["入院"] else 0) or 0
        h = (rec["外来"]["hybrid_pred"] if rec["外来"] else 0) or 0
        h += (rec["入院"]["hybrid_pred"] if rec["入院"] else 0) or 0
        rec["合計"] = {
            "actual":       round(a, 2),
            "ols_pred":     round(o, 2),
            "hybrid_pred":  round(h, 2),
            "ols_err_pct":     round((o - a) / a * 100, 1) if a > 0 else None,
            "hybrid_err_pct":  round((h - a) / a * 100, 1) if a > 0 else None,
        }
        if a > 0:
            hosp_actual += a; hosp_ols += o; hosp_hybrid += h
            hosp_has_any = True

    hospital_total = None
    if hosp_has_any and hosp_actual > 0:
        hospital_total = {
            "last_month":      last_month_g,
            "actual":          round(hosp_actual, 2),
            "ols_pred":        round(hosp_ols, 2),
            "hybrid_pred":     round(hosp_hybrid, 2),
            "ols_err_pct":     round((hosp_ols - hosp_actual) / hosp_actual * 100, 1),
            "hybrid_err_pct":  round((hosp_hybrid - hosp_actual) / hosp_actual * 100, 1),
        }

    # ── 日次ローリング 30日 ハイブリッド推計 ──
    # 出力サイズ抑制のため、history_days 未指定なら 365 日に制限
    series_by_dept, hospital_series, series_meta = _build_hybrid_daily_series(
        fit_models, surg, base_date, rolling_days,
        history_days if history_days is not None else 365,
        baseline_g=baseline_g, baseline_n=baseline_n,
        adm=adm, fallback_layers=fallback_layers,
    )

    return {
        "by_dept":         out_by_dept,
        "hospital_total":  hospital_total,
        "series_by_dept":  series_by_dept,
        "hospital_series": hospital_series,
        "meta": {
            "test_months":     test_months,
            "min_count":       min_count,
            "rolling_days":    rolling_days,
            "base_date":       base_date.strftime("%Y-%m-%d"),
            "n_depts_modeled": len(out_by_dept),
            **series_meta,
        },
    }


def _build_hybrid_daily_series(fit_models: Dict[str, Dict[str, Any]],
                                 surg: pd.DataFrame,
                                 base_date: pd.Timestamp,
                                 rolling_days: int,
                                 history_days: Optional[int],
                                 baseline_g: Optional[Dict[str, float]] = None,
                                 baseline_n: Optional[Dict[str, float]] = None,
                                 adm: Optional[pd.DataFrame] = None,
                                 fallback_layers: Optional[Dict[str, Dict[str, Any]]] = None):
    """日次ローリング 30日 ハイブリッド推計の系列を構築。

    fallback_layers: hybrid 不在科向け比推定（単価×在院数/営業日）。
      adm が提供されれば日次系列に乗せて水平線を回避。base hybrid のある
      科には適用しない（残差層の暴走を防ぐため）。

    baseline_g / baseline_n: fallback が組めなかった場合の最終フォールバック
      （月平均の定数 = 旧 baseline）。
    """
    from .profit_surgery import (
        predict_daily_rolling_per_dept, predict_ratio_fallback_daily,
    )

    if surg is None or len(surg) == 0:
        return {}, None, {"window_start": None, "window_end": None}

    s_dates = pd.to_datetime(surg["手術実施日"])
    earliest = s_dates.min().normalize() if len(s_dates) else base_date
    full_days = int((base_date - earliest).days) + 1
    if history_days is None or history_days > full_days:
        history_days = max(full_days, rolling_days)
    history_start = base_date - pd.Timedelta(days=history_days - 1)
    dates = pd.date_range(history_start, base_date, freq="D")
    cutoff = dates[rolling_days - 1] if len(dates) >= rolling_days else dates[-1]

    # 入外でフィルタした surg を予め用意（NaN 排除）
    surg_safe = surg.dropna(subset=["実施診療科"]).copy()
    surg_safe["実施診療科"] = surg_safe["実施診療科"].astype(str).str.strip()
    if "入外区分" in surg_safe.columns:
        surg_safe["入外区分"] = surg_safe["入外区分"].astype(str).str.strip()
        s_gairai = surg_safe[surg_safe["入外区分"] == "外来"]
        s_nyuin  = surg_safe[surg_safe["入外区分"] == "入院"]
    else:
        s_gairai = surg_safe.iloc[0:0]
        s_nyuin  = surg_safe

    baseline_g = baseline_g or {}
    baseline_n = baseline_n or {}
    fallback_layers = fallback_layers or {}
    fb_g_map = fallback_layers.get("外来", {}) or {}
    fb_n_map = fallback_layers.get("入院", {}) or {}

    # 月末見込み変換のため biz_roll（30日窓内営業日数の日次系列）を常時用意
    biz_flag = pd.Series([1 if is_operational_day(d) else 0 for d in dates], index=dates)
    biz_roll_self = biz_flag.rolling(rolling_days, min_periods=1).sum()

    # 比推定 fallback の日次ドライバー（adm 提供時のみ計算）
    by_dept_drv: Dict[str, Dict[str, pd.Series]] = {}
    biz_roll: Optional[pd.Series] = None
    if adm is not None and len(adm) > 0 and (fb_g_map or fb_n_map):
        pre = _daily_rolling_drivers(adm, surg, dates, rolling_days)
        by_dept_drv = pre.get("by_dept", {})
        biz_roll = pre.get("biz_roll")
    if biz_roll is None:
        biz_roll = biz_roll_self

    # 各日が属する月の営業日数
    month_biz = pd.Series([biz_days_in_month(d.replace(day=1)) for d in dates], index=dates).astype(float)
    # 見込み変換係数: 当月営業日数 / 30日窓内営業日数
    factor = month_biz.divide(biz_roll.replace(0, np.nan)).fillna(0.0)

    # 全対象科の集合（モデル化できた科 ∪ baseline がある科 ∪ fallback がある科）
    all_depts = (set(fit_models.get("外来", {}).keys())
                 | set(fit_models.get("入院", {}).keys())
                 | set(baseline_g.keys()) | set(baseline_n.keys())
                 | set(fb_g_map.keys()) | set(fb_n_map.keys()))

    hosp_g_hy = pd.Series(0.0, index=dates)
    hosp_n_hy = pd.Series(0.0, index=dates)
    hosp_g_ols = pd.Series(0.0, index=dates)
    hosp_n_ols = pd.Series(0.0, index=dates)

    def _baseline_series(value: Optional[float]) -> pd.Series:
        return pd.Series(float(value) if value else 0.0, index=dates)

    def _fallback_or_baseline(layer_rec: Optional[Dict[str, Any]],
                                baseline_value: Optional[float],
                                dept: str) -> pd.Series:
        """hybrid 不在時: 比推定 fallback があればそれ、無ければ baseline 定数。"""
        if layer_rec:
            return predict_ratio_fallback_daily(
                layer_rec, dept, by_dept_drv, biz_roll, dates
            )
        return _baseline_series(baseline_value)

    series_by_dept: Dict[str, Dict[str, Any]] = {}
    for dept in sorted(all_depts):
        g_model = fit_models.get("外来", {}).get(dept)
        n_model = fit_models.get("入院", {}).get(dept)
        if g_model:
            g_hy  = predict_daily_rolling_per_dept(g_model, s_gairai, dept, dates, rolling_days)
            g_ols = predict_daily_rolling_per_dept(g_model, s_gairai, dept, dates, rolling_days, force_kind="ols")
        else:
            g_hy  = _fallback_or_baseline(fb_g_map.get(dept), baseline_g.get(dept), dept)
            g_ols = g_hy
        if n_model:
            n_hy  = predict_daily_rolling_per_dept(n_model, s_nyuin,  dept, dates, rolling_days)
            n_ols = predict_daily_rolling_per_dept(n_model, s_nyuin,  dept, dates, rolling_days, force_kind="ols")
        else:
            n_hy  = _fallback_or_baseline(fb_n_map.get(dept), baseline_n.get(dept), dept)
            n_ols = n_hy
        hosp_g_hy = hosp_g_hy.add(g_hy, fill_value=0)
        hosp_n_hy = hosp_n_hy.add(n_hy, fill_value=0)
        hosp_g_ols = hosp_g_ols.add(g_ols, fill_value=0)
        hosp_n_ols = hosp_n_ols.add(n_ols, fill_value=0)
        mask = g_hy.index >= cutoff
        # 月末見込み変換: 直近30日値 × (当月営業日数 / 30日窓内営業日数)
        g_hy_proj = g_hy * factor
        n_hy_proj = n_hy * factor
        # 科レベルは hybrid のみ（ファイルサイズ抑制）
        series_by_dept[dept] = {
            "dates":         [d.strftime("%Y-%m-%d") for d in dates],
            "values_total":  [round((gv + nv), 2) if m else None
                                for gv, nv, m in zip(g_hy, n_hy, mask)],
            "values_gairai": [round(v, 2) if m else None
                                for v, m in zip(g_hy, mask)],
            "values_nyuin":  [round(v, 2) if m else None
                                for v, m in zip(n_hy, mask)],
            "values_projection_total":  [round((gv + nv), 2) if m else None
                                for gv, nv, m in zip(g_hy_proj, n_hy_proj, mask)],
            "values_projection_gairai": [round(v, 2) if m else None
                                for v, m in zip(g_hy_proj, mask)],
            "values_projection_nyuin":  [round(v, 2) if m else None
                                for v, m in zip(n_hy_proj, mask)],
        }

    mask = hosp_g_hy.index >= cutoff
    # 月末見込み変換（病院全体）
    hosp_g_hy_proj = hosp_g_hy * factor
    hosp_n_hy_proj = hosp_n_hy * factor
    hospital_series = {
        "dates":             [d.strftime("%Y-%m-%d") for d in dates],
        "values_total":      [round((gv + nv), 2) if m else None
                                for gv, nv, m in zip(hosp_g_hy, hosp_n_hy, mask)],
        "values_gairai":     [round(v, 2) if m else None
                                for v, m in zip(hosp_g_hy, mask)],
        "values_nyuin":      [round(v, 2) if m else None
                                for v, m in zip(hosp_n_hy, mask)],
        "ols_total":         [round((gv + nv), 2) if m else None
                                for gv, nv, m in zip(hosp_g_ols, hosp_n_ols, mask)],
        "ols_gairai":        [round(v, 2) if m else None
                                for v, m in zip(hosp_g_ols, mask)],
        "ols_nyuin":         [round(v, 2) if m else None
                                for v, m in zip(hosp_n_ols, mask)],
        "values_projection_total":  [round((gv + nv), 2) if m else None
                                for gv, nv, m in zip(hosp_g_hy_proj, hosp_n_hy_proj, mask)],
        "values_projection_gairai": [round(v, 2) if m else None
                                for v, m in zip(hosp_g_hy_proj, mask)],
        "values_projection_nyuin":  [round(v, 2) if m else None
                                for v, m in zip(hosp_n_hy_proj, mask)],
        "month_biz_days_series":    [int(v) for v in month_biz.tolist()],
        "window_biz_days_series":   [int(v) for v in biz_roll.tolist()],
    }

    base_month_biz   = int(month_biz.iloc[-1])
    base_window_biz  = int(biz_roll.iloc[-1])
    base_factor      = (base_month_biz / base_window_biz) if base_window_biz > 0 else 0.0
    series_meta = {
        "window_start": (base_date - pd.Timedelta(days=rolling_days - 1)).strftime("%Y-%m-%d"),
        "window_end":   base_date.strftime("%Y-%m-%d"),
        "latest_hybrid_total":  round(float(hosp_g_hy.iloc[-1] + hosp_n_hy.iloc[-1]), 2),
        "latest_hybrid_gairai": round(float(hosp_g_hy.iloc[-1]), 2),
        "latest_hybrid_nyuin":  round(float(hosp_n_hy.iloc[-1]), 2),
        "latest_ols_total":     round(float(hosp_g_ols.iloc[-1] + hosp_n_ols.iloc[-1]), 2),
        "latest_ols_gairai":    round(float(hosp_g_ols.iloc[-1]), 2),
        "latest_ols_nyuin":     round(float(hosp_n_ols.iloc[-1]), 2),
        "current_month_biz_days":   base_month_biz,
        "window_biz_days":          base_window_biz,
        "projection_factor":        round(base_factor, 4),
        "latest_projection_total":  round(float(hosp_g_hy_proj.iloc[-1] + hosp_n_hy_proj.iloc[-1]), 2),
        "latest_projection_gairai": round(float(hosp_g_hy_proj.iloc[-1]), 2),
        "latest_projection_nyuin":  round(float(hosp_n_hy_proj.iloc[-1]), 2),
    }
    return series_by_dept, hospital_series, series_meta
