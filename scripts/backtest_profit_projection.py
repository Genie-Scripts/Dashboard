"""
backtest_profit_projection.py — 月末見込み G の歴史バックテスト
================================================================

本番で医業収支予測の G に使う `meta.latest_projection_total`
( = 直近30日推計 × 当月営業日数 / 30日窓内営業日数 ) そのものを、
過去の各月・各基準日で再現し、確定実績 G と突合する。

【なぜ必要か】
  既存の hospital_total.hybrid_pred は「月次NNLS/件数OLSモデルが過去月の
  実績を当てたか」を測るが、実際に G へ流れる "月末見込み"（rolling×factor）
  は一度も実績検証されていない。本スクリプトがその穴を塞ぐ。

【リーク防止】
  - profit_breakdown は対象月 M 未満に切詰めてフィット（M の粗利は本番でも未確定）
  - adm / surg は base_date 以下に切詰め
  - 日次ローリングは後方参照（因果的）なので、月 M の各日の月末見込みは
    その日までのドライバーだけで決まる → 月 M につき1呼び出しで月内全日を再現可能

【出力】
  - 月内経過（営業日ベース）に対する誤差カーブ（コールドスタート診断）
  - 月別サマリ（実績 / 月初・中旬・月末時点の見込み / naive 比較）
  - 全体 MAPE・バイアス（時点別）と naive ベースライン比較
  - output/profit_projection_backtest.{json,csv}

usage:
    python -m scripts.backtest_profit_projection
    python -m scripts.backtest_profit_projection --months 12
    python -m scripts.backtest_profit_projection --min-history 6
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.lib.config import DEFAULT_DATA_DIR, is_operational_day, biz_days_in_month  # noqa: E402
from app.lib.data_loader import (  # noqa: E402
    load_admission_data, load_surgery_data, load_profit_breakdown,
)
from app.lib.preprocess import preprocess_admission, preprocess_surgery  # noqa: E402
from app.lib.profit_estimate import build_hybrid_payload  # noqa: E402


def _month_start(d) -> pd.Timestamp:
    return pd.Timestamp(d).normalize().replace(day=1)


def _month_actual_total(pb: pd.DataFrame, m_start: pd.Timestamp) -> float | None:
    """対象月の確定 G（外来+入院 全科合計, 百万円）。無ければ None。"""
    sub = pb[pb["月"] == m_start]
    if sub.empty:
        return None
    return float(sub["粗利"].sum()) / 1000.0


def _biz_elapsed(m_start: pd.Timestamp, d: pd.Timestamp) -> int:
    """月初から d まで（d 含む）の営業日数。"""
    return sum(1 for x in pd.date_range(m_start, d, freq="D") if is_operational_day(x))


def backtest_months(pb: pd.DataFrame,
                    adm: pd.DataFrame,
                    surg: pd.DataFrame,
                    target_months: list[pd.Timestamp],
                    min_history: int) -> tuple[list[dict], list[dict]]:
    """各対象月で月末見込みを再現。

    Returns:
        daily_rows: 月内各日の (月, 日付, 営業日経過, 経過率, 見込み, 実績, 誤差, 誤差率)
        month_rows: 月別サマリ
    """
    daily_rows: list[dict] = []
    month_rows: list[dict] = []

    adm_max = pd.Timestamp(adm["日付"].max()).normalize()
    surg_max = pd.Timestamp(surg["手術実施日"].max()).normalize()

    for m_start in target_months:
        actual = _month_actual_total(pb, m_start)
        if actual is None or actual <= 0:
            continue

        # フィット用 pb は対象月未満（本番でも M の粗利は未確定）
        pb_train = pb[pb["月"] < m_start]
        n_hist = pb_train["月"].nunique()
        if n_hist < min_history:
            continue

        month_end = m_start + pd.offsets.MonthEnd(0)
        base_date = min(month_end, adm_max, surg_max)
        if base_date < m_start:
            continue  # ドライバーデータが月初に届いていない

        adm_bt = adm[adm["日付"] <= base_date]
        surg_bt = surg[surg["手術実施日"] <= base_date]

        payload = build_hybrid_payload(
            profit_breakdown=pb_train, surg=surg_bt, base_date=base_date, adm=adm_bt,
        )
        if not payload or not payload.get("hospital_series"):
            continue
        hs = payload["hospital_series"]
        dates = pd.to_datetime(hs["dates"])
        proj = hs["values_projection_total"]   # 月末見込み（本番 G の素）
        raw = hs["values_total"]               # 直近30日推計（変換前・参考）
        mtd = hs.get("values_mtd_total") or [None] * len(dates)  # MTD 外挿（項目2）

        total_biz = biz_days_in_month(m_start)

        # naive ベースライン（月内一定）
        prev_actual = _month_actual_total(pb, _month_start(m_start - pd.DateOffset(months=1)))
        ma3 = [
            _month_actual_total(pb, _month_start(m_start - pd.DateOffset(months=k)))
            for k in (1, 2, 3)
        ]
        ma3 = [v for v in ma3 if v is not None]
        naive_ma3 = float(np.mean(ma3)) if ma3 else None
        prevyear = _month_actual_total(pb, _month_start(m_start - pd.DateOffset(months=12)))

        # 月内各日
        in_month = []
        for d, pv, rv, mv in zip(dates, proj, raw, mtd):
            if pv is None or d < m_start or d > month_end:
                continue
            be = _biz_elapsed(m_start, d)
            frac = be / total_biz if total_biz else 0.0
            err = pv - actual
            in_month.append({
                "月": m_start.strftime("%Y-%m"),
                "日付": d.strftime("%Y-%m-%d"),
                "営業日経過": be,
                "当月営業日数": total_biz,
                "経過率": round(frac, 3),
                "見込み": round(pv, 2),
                "MTD見込み": round(mv, 2) if mv is not None else None,
                "直近30日推計": round(rv, 2) if rv is not None else None,
                "実績": round(actual, 2),
                "誤差": round(err, 2),
                "誤差率": round(err / actual, 4),
            })
        if not in_month:
            continue
        daily_rows.extend(in_month)

        # 代表時点（月初 / 中旬 / 月末）の見込みを抽出
        def _at_frac(target: float) -> dict | None:
            cand = [r for r in in_month]
            if not cand:
                return None
            return min(cand, key=lambda r: abs(r["経過率"] - target))

        first_pt = in_month[0]
        mid_pt = _at_frac(0.5)
        last_pt = in_month[-1]

        def _ape(pred):
            return abs(pred - actual) / actual if pred is not None else None

        month_rows.append({
            "月": m_start.strftime("%Y-%m"),
            "実績": round(actual, 2),
            "見込み_月初": first_pt["見込み"],
            "見込み_中旬": mid_pt["見込み"] if mid_pt else None,
            "見込み_月末": last_pt["見込み"],
            "誤差率_月初": round(first_pt["誤差率"], 4),
            "誤差率_中旬": round(mid_pt["誤差率"], 4) if mid_pt else None,
            "誤差率_月末": round(last_pt["誤差率"], 4),
            "naive_前月": round(prev_actual, 2) if prev_actual is not None else None,
            "naive_前月_誤差率": round(_ape(prev_actual), 4) if prev_actual is not None else None,
            "naive_3か月平均": round(naive_ma3, 2) if naive_ma3 is not None else None,
            "naive_3か月平均_誤差率": round(_ape(naive_ma3), 4) if naive_ma3 is not None else None,
            "naive_前年同月": round(prevyear, 2) if prevyear is not None else None,
            "naive_前年同月_誤差率": round(_ape(prevyear), 4) if prevyear is not None else None,
            "base_date": base_date.strftime("%Y-%m-%d"),
            "n_hist_months": int(n_hist),
        })

    return daily_rows, month_rows


def summarize(daily_rows: list[dict], month_rows: list[dict]) -> dict:
    """誤差カーブと時点別 MAPE/バイアスを集計。"""
    if not month_rows:
        return {}

    mr = pd.DataFrame(month_rows)

    def _stats(col):
        s = mr[col].dropna().abs()
        bias = mr[col.replace("_誤差率", "")] if False else None  # placeholder
        return {
            "MAPE": round(float(s.mean()) * 100, 1) if len(s) else None,
            "median_APE": round(float(s.median()) * 100, 1) if len(s) else None,
            "n": int(len(s)),
        }

    # 時点別 MAPE と平均バイアス（符号付き）
    def _point(col):
        signed = mr[col].dropna()
        return {
            "MAPE": round(float(signed.abs().mean()) * 100, 1) if len(signed) else None,
            "中央絶対誤差率": round(float(signed.abs().median()) * 100, 1) if len(signed) else None,
            "平均バイアス": round(float(signed.mean()) * 100, 1) if len(signed) else None,
            "n": int(len(signed)),
        }

    points = {
        "月初時点":  _point("誤差率_月初"),
        "中旬時点":  _point("誤差率_中旬"),
        "月末時点":  _point("誤差率_月末"),
    }

    def _naive(col):
        s = mr[col].dropna()
        return {"MAPE": round(float(s.abs().mean()) * 100, 1) if len(s) else None,
                "n": int(len(s))}

    naive = {
        "前月":     _naive("naive_前月_誤差率"),
        "3か月平均": _naive("naive_3か月平均_誤差率"),
        "前年同月":  _naive("naive_前年同月_誤差率"),
    }

    # 営業日経過ビン別の誤差カーブ
    dr = pd.DataFrame(daily_rows)
    curve = []
    if len(dr):
        dr["bin"] = pd.cut(dr["経過率"], bins=[0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1.01],
                           labels=["~10%", "~20%", "~30%", "~40%", "~50%",
                                   "~60%", "~70%", "~80%", "~90%", "~100%"])
        for b, g in dr.groupby("bin", observed=True):
            curve.append({
                "経過ビン": str(b),
                "MAPE": round(float(g["誤差率"].abs().mean()) * 100, 1),
                "平均バイアス": round(float(g["誤差率"].mean()) * 100, 1),
                "n": int(len(g)),
            })

    return {"時点別": points, "naive比較": naive, "誤差カーブ": curve}


# ──────────────────────────────────────────
# 項目4: recency 乗法キャリブレーション（バイアス補正）プロトタイプ
# ──────────────────────────────────────────
#   target 月 M の補正係数 c_M を「直近 k 月の 実績/月末見込み 比」から作り、
#   M の見込みを一律 c_M 倍する。leakage-free（M 未満の確定実績のみ使用）。
#   4月レベルシフトは比が遅れて追従することで自動吸収される。

_CAL_VARIANTS = {
    "k3_median":   {"k": 3,  "agg": "median"},
    "k6_median":   {"k": 6,  "agg": "median"},
    "k12_median":  {"k": 12, "agg": "median"},
    "ewma_hl4":    {"k": 24, "agg": "ewma", "halflife": 4},
    "k6_shrink50": {"k": 6,  "agg": "median", "shrink": 0.5},  # 係数を1へ半分縮約
    "k12_shrink50":{"k": 12, "agg": "median", "shrink": 0.5},
}


def _month_shift(m: str, back: int) -> str:
    ts = pd.Timestamp(m + "-01") - pd.DateOffset(months=back)
    return ts.strftime("%Y-%m")


def _calib_factor(month: str, ref_ratio: dict, cfg: dict) -> float:
    """直近 k 月の (実績/月末見込み) 比から補正係数を作る。履歴不足なら 1.0。"""
    vals, ages = [], []
    for j in range(1, cfg["k"] + 1):
        p = _month_shift(month, j)
        r = ref_ratio.get(p)
        if r is None or not np.isfinite(r) or r <= 0:
            continue
        vals.append(r)
        ages.append(j)
    if not vals:
        return 1.0
    if cfg["agg"] == "median":
        c = float(np.median(vals))
    elif cfg["agg"] == "ewma":
        hl = cfg.get("halflife", 4)
        w = np.power(0.5, np.array(ages) / hl)
        c = float(np.average(vals, weights=w))
    else:
        c = float(np.mean(vals))
    shrink = cfg.get("shrink")
    if shrink:
        c = 1.0 + (c - 1.0) * shrink   # 1 に向けて縮約しノイズ注入を抑える
    return c


def apply_calibration(month_rows: list[dict], daily_rows: list[dict]) -> dict:
    """各バリアントで補正後の時点別 MAPE/バイアスと誤差カーブを算出。"""
    # 補正の基準比: 各月の 実績/月末見込み（月末見込みをアンカーに使う）
    ref_ratio = {}
    for r in month_rows:
        pe = r.get("見込み_月末")
        if pe and pe > 0:
            ref_ratio[r["月"]] = r["実績"] / pe

    out = {}
    for name, cfg in _CAL_VARIANTS.items():
        cfac = {r["月"]: _calib_factor(r["月"], ref_ratio, cfg) for r in month_rows}

        # 時点別（月初/中旬/月末）
        pts = {}
        for label, col in (("月初時点", "見込み_月初"),
                           ("中旬時点", "見込み_中旬"),
                           ("月末時点", "見込み_月末")):
            errs = []
            for r in month_rows:
                v = r.get(col)
                if v is None:
                    continue
                corr = v * cfac[r["月"]]
                errs.append((corr - r["実績"]) / r["実績"])
            errs = np.array(errs)
            pts[label] = {
                "MAPE": round(float(np.abs(errs).mean()) * 100, 1) if len(errs) else None,
                "平均バイアス": round(float(errs.mean()) * 100, 1) if len(errs) else None,
                "n": int(len(errs)),
            }

        # 誤差カーブ（日次に c_M を適用）
        dr = pd.DataFrame(daily_rows).copy()
        dr["c"] = dr["月"].map(cfac).fillna(1.0)
        dr["corr_err"] = (dr["見込み"] * dr["c"] - dr["実績"]) / dr["実績"]
        dr["bin"] = pd.cut(dr["経過率"], bins=[0, .25, .5, .75, 1.01],
                           labels=["前半", "中盤", "後半", "終盤"])
        curve = []
        for b, g in dr.groupby("bin", observed=True):
            curve.append({"区間": str(b),
                          "MAPE": round(float(g["corr_err"].abs().mean()) * 100, 1),
                          "バイアス": round(float(g["corr_err"].mean()) * 100, 1)})
        # 全体（全日次サンプル）
        overall_mape = round(float(dr["corr_err"].abs().mean()) * 100, 1)
        overall_bias = round(float(dr["corr_err"].mean()) * 100, 1)
        out[name] = {"時点別": pts, "誤差カーブ": curve,
                     "全日次_MAPE": overall_mape, "全日次_バイアス": overall_bias,
                     "係数例": {m: round(c, 4) for m, c in list(cfac.items())[-3:]}}
    return out


def print_calibration(cal: dict, baseline: dict, daily_rows: list[dict]):
    if not cal:
        return
    # ベースライン全日次
    dr = pd.DataFrame(daily_rows)
    base_all_mape = round(float(dr["誤差率"].abs().mean()) * 100, 1)
    base_all_bias = round(float(dr["誤差率"].mean()) * 100, 1)

    print("\n" + "=" * 72)
    print("項目4: recency 乗法キャリブレーション — before/after")
    print("=" * 72)
    bp = baseline["時点別"]
    print(f"{'方式':<14}{'月初MAPE':>9}{'中旬MAPE':>9}{'月末MAPE':>9}"
          f"{'全日MAPE':>9}{'全日bias':>10}")
    print(f"{'現行(無補正)':<14}{str(bp['月初時点']['MAPE'])+'%':>9}"
          f"{str(bp['中旬時点']['MAPE'])+'%':>9}{str(bp['月末時点']['MAPE'])+'%':>9}"
          f"{str(base_all_mape)+'%':>9}{_fmt_pct(base_all_bias):>10}")
    for name, v in cal.items():
        p = v["時点別"]
        print(f"{name:<14}{str(p['月初時点']['MAPE'])+'%':>9}"
              f"{str(p['中旬時点']['MAPE'])+'%':>9}{str(p['月末時点']['MAPE'])+'%':>9}"
              f"{str(v['全日次_MAPE'])+'%':>9}{_fmt_pct(v['全日次_バイアス']):>10}")
    print("\n  バイアス（平均誤差率, 補正で 0 に近づくほど良い）")
    print(f"{'方式':<14}{'月初':>9}{'中旬':>9}{'月末':>9}")
    print(f"{'現行(無補正)':<14}{_fmt_pct(bp['月初時点']['平均バイアス']):>9}"
          f"{_fmt_pct(bp['中旬時点']['平均バイアス']):>9}"
          f"{_fmt_pct(bp['月末時点']['平均バイアス']):>9}")
    for name, v in cal.items():
        p = v["時点別"]
        print(f"{name:<14}{_fmt_pct(p['月初時点']['平均バイアス']):>9}"
              f"{_fmt_pct(p['中旬時点']['平均バイアス']):>9}"
              f"{_fmt_pct(p['月末時点']['平均バイアス']):>9}")


# ──────────────────────────────────────────
# 項目2: MTD ブレンドのスイープ（分散低減）プロトタイプ
# ──────────────────────────────────────────
#   blended(d) = w(d)·MTD(d) + (1-w(d))·proj(d)
#   w(d) は月内の営業日完了度で増やす。早期は MTD のラン率が荒いので proj を残す。

_BLEND_VARIANTS = {
    "pure_mtd":     None,          # w=1（常に MTD）
    "w_完了率":      "completion",  # w = 経過営業日 / 当月営業日数
    "w_anchor5":    5,             # w = min(1, 経過営業日/5)
    "w_anchor8":    8,
    "w_anchor10":   10,
}


def _blend_weight(be: int, total_biz: int, spec) -> float:
    if spec is None:
        return 1.0
    if spec == "completion":
        return min(1.0, be / total_biz) if total_biz else 0.0
    return min(1.0, be / float(spec))


def sweep_mtd_blend(daily_rows: list[dict]) -> dict:
    """各ブレンド比で時点別・全日の MAPE/バイアスを算出。MTD 欠落行は proj を使用。"""
    dr = pd.DataFrame(daily_rows)
    if "MTD見込み" not in dr.columns or dr["MTD見込み"].isna().all():
        return {}
    dr = dr.copy()
    dr["mtd_eff"] = dr["MTD見込み"].fillna(dr["見込み"])

    out = {}
    for name, spec in _BLEND_VARIANTS.items():
        w = dr.apply(lambda r: _blend_weight(r["営業日経過"], r["当月営業日数"], spec), axis=1)
        blended = w * dr["mtd_eff"] + (1 - w) * dr["見込み"]
        err = (blended - dr["実績"]) / dr["実績"]
        d2 = dr.assign(_e=err)
        # 時点別はベースライン(summarize)と同じ代表日: 月初=最早日, 月末=最終日, 中旬~0.5
        def _pt(kind):
            rows = []
            for _, g in d2.groupby("月"):
                g = g.sort_values("日付")
                if kind == "first":
                    r = g.iloc[0]
                elif kind == "last":
                    r = g.iloc[-1]
                else:
                    r = g.loc[(g["経過率"] - 0.5).abs().idxmin()]
                rows.append(r["_e"])
            a = np.array(rows)
            return (round(float(np.abs(a).mean()) * 100, 1),
                    round(float(a.mean()) * 100, 1))
        m0, b0 = _pt("first")
        m5, b5 = _pt("mid")
        m9, b9 = _pt("last")
        out[name] = {
            "月初": {"MAPE": m0, "bias": b0},
            "中旬": {"MAPE": m5, "bias": b5},
            "月末": {"MAPE": m9, "bias": b9},
            "全日_MAPE": round(float(err.abs().mean()) * 100, 1),
            "全日_bias": round(float(err.mean()) * 100, 1),
        }
    return out


def print_mtd_blend(sweep: dict, baseline: dict, daily_rows: list[dict]):
    if not sweep:
        print("\n(MTD 系列が payload に無いため項目2スイープはスキップ)")
        return
    dr = pd.DataFrame(daily_rows)
    base_all_mape = round(float(dr["誤差率"].abs().mean()) * 100, 1)
    base_all_bias = round(float(dr["誤差率"].mean()) * 100, 1)
    bp = baseline["時点別"]

    print("\n" + "=" * 72)
    print("項目2: MTD ブレンド — before/after（分散低減ねらい）")
    print("=" * 72)
    print(f"{'方式':<12}{'月初MAPE':>9}{'中旬MAPE':>9}{'月末MAPE':>9}{'全日MAPE':>9}{'全日bias':>10}")
    print(f"{'現行(proj)':<12}{str(bp['月初時点']['MAPE'])+'%':>9}"
          f"{str(bp['中旬時点']['MAPE'])+'%':>9}{str(bp['月末時点']['MAPE'])+'%':>9}"
          f"{str(base_all_mape)+'%':>9}{_fmt_pct(base_all_bias):>10}")
    for name, v in sweep.items():
        print(f"{name:<12}{str(v['月初']['MAPE'])+'%':>9}"
              f"{str(v['中旬']['MAPE'])+'%':>9}{str(v['月末']['MAPE'])+'%':>9}"
              f"{str(v['全日_MAPE'])+'%':>9}{_fmt_pct(v['全日_bias']):>10}")
    print("  注: MTD は手術モデル科のみ外挿、fallback/baseline 科は proj 踏襲（MTD効果を隔離）")


def combo_mtd_calibration(daily_rows: list[dict], blend_spec=8,
                          k: int = 12, shrink: float = 0.5) -> dict:
    """MTD ブレンド（分散↓）に recency 補正（バイアス↓）を重ねた最終形を評価。

    補正の基準比は「実績 / MTDブレンド月末見込み」（= 校正対象と同じ推計）。
    """
    dr = pd.DataFrame(daily_rows)
    if "MTD見込み" not in dr.columns or dr["MTD見込み"].isna().all():
        return {}
    dr = dr.copy()
    dr["mtd_eff"] = dr["MTD見込み"].fillna(dr["見込み"])
    w = dr.apply(lambda r: _blend_weight(r["営業日経過"], r["当月営業日数"], blend_spec), axis=1)
    dr["blended"] = w * dr["mtd_eff"] + (1 - w) * dr["見込み"]

    # 各月の月末ブレンド値（最終日）→ ref_ratio
    me = dr.sort_values("日付").groupby("月").tail(1)
    ref_ratio = {r["月"]: (r["実績"] / r["blended"])
                 for _, r in me.iterrows() if r["blended"] > 0}
    cfg = {"k": k, "agg": "median", "shrink": shrink}
    cfac = {m: _calib_factor(m, ref_ratio, cfg) for m in dr["月"].unique()}
    dr["c"] = dr["月"].map(cfac).fillna(1.0)
    dr["final"] = dr["blended"] * dr["c"]
    dr["_e"] = (dr["final"] - dr["実績"]) / dr["実績"]

    def _pt(kind):
        rows = []
        for _, g in dr.groupby("月"):
            g = g.sort_values("日付")
            r = g.iloc[0] if kind == "first" else (g.iloc[-1] if kind == "last"
                 else g.loc[(g["経過率"] - 0.5).abs().idxmin()])
            rows.append(r["_e"])
        a = np.array(rows)
        return round(float(np.abs(a).mean()) * 100, 1), round(float(a.mean()) * 100, 1)

    m0, b0 = _pt("first"); m5, b5 = _pt("mid"); m9, b9 = _pt("last")
    return {"blend_spec": blend_spec,
            "月初": {"MAPE": m0, "bias": b0},
            "中旬": {"MAPE": m5, "bias": b5},
            "月末": {"MAPE": m9, "bias": b9},
            "全日_MAPE": round(float(dr["_e"].abs().mean()) * 100, 1),
            "全日_bias": round(float(dr["_e"].mean()) * 100, 1)}


def print_combo(combo: dict, baseline: dict, daily_rows: list[dict]):
    if not combo:
        return
    dr = pd.DataFrame(daily_rows)
    base_all_mape = round(float(dr["誤差率"].abs().mean()) * 100, 1)
    base_all_bias = round(float(dr["誤差率"].mean()) * 100, 1)
    bp = baseline["時点別"]
    print("\n" + "=" * 72)
    print(f"最終形: MTDブレンド(anchor{combo['blend_spec']}) + recency補正(k12_shrink50)")
    print("=" * 72)
    print(f"{'方式':<22}{'月初':>8}{'中旬':>8}{'月末':>8}{'全日MAPE':>9}{'全日bias':>10}")
    print(f"{'現行(無補正proj)':<22}{str(bp['月初時点']['MAPE'])+'%':>8}"
          f"{str(bp['中旬時点']['MAPE'])+'%':>8}{str(bp['月末時点']['MAPE'])+'%':>8}"
          f"{str(base_all_mape)+'%':>9}{_fmt_pct(base_all_bias):>10}")
    print(f"{'MTD+補正(最終形)':<22}{str(combo['月初']['MAPE'])+'%':>8}"
          f"{str(combo['中旬']['MAPE'])+'%':>8}{str(combo['月末']['MAPE'])+'%':>8}"
          f"{str(combo['全日_MAPE'])+'%':>9}{_fmt_pct(combo['全日_bias']):>10}")
    print(f"{'(参考)バイアス最終形':<22}{_fmt_pct(combo['月初']['bias']):>8}"
          f"{_fmt_pct(combo['中旬']['bias']):>8}{_fmt_pct(combo['月末']['bias']):>8}")


def _fmt_pct(v):
    return "—" if v is None else f"{v:+.1f}%"


def print_report(summary: dict, month_rows: list[dict]):
    if not summary:
        print("対象月がありません。")
        return
    pts = summary["時点別"]
    print("\n" + "=" * 72)
    print("月末見込み G バックテスト — 時点別精度（病院全体・全科合計）")
    print("=" * 72)
    print(f"{'時点':<10}{'MAPE':>10}{'中央絶対%':>12}{'平均バイアス':>14}{'n':>5}")
    for k, v in pts.items():
        print(f"{k:<10}{(str(v['MAPE'])+'%' if v['MAPE'] is not None else '—'):>10}"
              f"{(str(v['中央絶対誤差率'])+'%' if v['中央絶対誤差率'] is not None else '—'):>12}"
              f"{_fmt_pct(v['平均バイアス']):>14}{v['n']:>5}")

    print("\nnaive ベースライン（月内一定）との比較（MAPE）")
    nv = summary["naive比較"]
    for k, v in nv.items():
        print(f"  {k:<10}: {(str(v['MAPE'])+'%') if v['MAPE'] is not None else '—':>8}  (n={v['n']})")
    print("  → モデル月末時点 MAPE がこれらを下回らなければ、複雑なモデルの価値は薄い")

    print("\n月内経過に対する誤差カーブ（コールドスタート診断）")
    print(f"{'経過':<10}{'MAPE':>10}{'平均バイアス':>14}{'n':>6}")
    for c in summary["誤差カーブ"]:
        print(f"{c['経過ビン']:<10}{str(c['MAPE'])+'%':>10}{_fmt_pct(c['平均バイアス']):>14}{c['n']:>6}")

    print("\n月別サマリ（百万円, 誤差率は見込み−実績）")
    print(f"{'月':<9}{'実績':>9}{'月初見込':>9}{'中旬見込':>9}{'月末見込':>9}"
          f"{'月初%':>8}{'中旬%':>8}{'月末%':>8}{'前月%':>8}")
    for r in month_rows:
        print(f"{r['月']:<9}{r['実績']:>9.0f}"
              f"{r['見込み_月初']:>9.0f}"
              f"{(r['見込み_中旬'] if r['見込み_中旬'] is not None else float('nan')):>9.0f}"
              f"{r['見込み_月末']:>9.0f}"
              f"{_fmt_pct(r['誤差率_月初']*100):>8}"
              f"{(_fmt_pct(r['誤差率_中旬']*100) if r['誤差率_中旬'] is not None else '—'):>8}"
              f"{_fmt_pct(r['誤差率_月末']*100):>8}"
              f"{(_fmt_pct(r['naive_前月_誤差率']*100) if r['naive_前月_誤差率'] is not None else '—'):>8}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--months", type=int, default=0,
                    help="検証する直近の対象月数（0=可能な全月）")
    ap.add_argument("--min-history", type=int, default=6,
                    help="フィットに必要な最小の過去月数")
    ap.add_argument("--out-json", default="output/profit_projection_backtest.json")
    ap.add_argument("--out-csv", default="output/profit_projection_backtest_daily.csv")
    args = ap.parse_args()

    print("[1/3] データ読込中...")
    pb = load_profit_breakdown(args.data_dir)
    pb["月"] = pd.to_datetime(pb["月"]).apply(_month_start)
    adm = preprocess_admission(load_admission_data(args.data_dir))
    surg = preprocess_surgery(load_surgery_data(args.data_dir))
    adm["日付"] = pd.to_datetime(adm["日付"])
    surg["手術実施日"] = pd.to_datetime(surg["手術実施日"])

    # 対象月: pb に実績があり & adm が月初をカバーする月
    adm_min = pd.Timestamp(adm["日付"].min()).normalize()
    pb_months = sorted(pb["月"].unique())
    # adm が30日窓を組めるよう、adm_min + 1か月 以降の月のみ
    first_ok = (_month_start(adm_min) + pd.DateOffset(months=1))
    target = [m for m in pb_months if m >= first_ok]
    if args.months > 0:
        target = target[-args.months:]
    print(f"  対象月: {len(target)} 件 "
          f"({target[0].strftime('%Y-%m')} 〜 {target[-1].strftime('%Y-%m')})")

    print("[2/3] 各月の月末見込みを再現中（1月=1呼び出し）...")
    daily_rows, month_rows = backtest_months(pb, adm, surg, target, args.min_history)
    print(f"  再現できた月: {len(month_rows)} 件 / 日次サンプル: {len(daily_rows)} 件")

    print("[3/3] 集計・出力...")
    summary = summarize(daily_rows, month_rows)
    print_report(summary, month_rows)

    cal = apply_calibration(month_rows, daily_rows)
    print_calibration(cal, summary, daily_rows)

    blend = sweep_mtd_blend(daily_rows)
    print_mtd_blend(blend, summary, daily_rows)

    combo = combo_mtd_calibration(daily_rows, blend_spec=8)
    print_combo(combo, summary, daily_rows)

    out_json = Path(args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(
        {"summary": summary, "calibration": cal, "mtd_blend": blend,
         "combo": combo, "months": month_rows},
        ensure_ascii=False, indent=2),
        encoding="utf-8")
    pd.DataFrame(daily_rows).to_csv(args.out_csv, index=False, encoding="utf-8-sig")
    print(f"\n  → {out_json.resolve()}")
    print(f"  → {Path(args.out_csv).resolve()}")


if __name__ == "__main__":
    main()
