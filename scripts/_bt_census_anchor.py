"""入院月末見込み: 現行(暦日換算) vs 在院数アンカー の検証（入院単独）。

現行(暦日換算, 本番反映済み):
    入院見込み = 単価 × (直近30日 純在院延べ) × (当月暦日数 / 30)
在院数アンカー:
    入院見込み = 単価 × ( 月初〜当日の純在院延べ実績
                          + 直近K日の日次net平均 × 当月の残り暦日数 )

単価は両方式とも同じ（直近6か月の Σ入院粗利 / Σ純在院延べ, leakage-free）。
入院は粗利の約78%。同一27か月・同一単価で純粋に外挿方式だけを比較する。
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.lib.config import DEFAULT_DATA_DIR
from app.lib.data_loader import load_profit_breakdown, load_admission_data
from app.lib.preprocess import preprocess_admission

ROLLING_DAYS = 30
ANCHOR_K = 7  # 在院水準の平滑化窓（日）


def _ms(d):
    return pd.Timestamp(d).normalize().replace(day=1)


def run():
    pb = load_profit_breakdown(DEFAULT_DATA_DIR)
    pb["月"] = pd.to_datetime(pb["月"]).apply(_ms)
    nyuin_m = (pb[pb["区分"] == "入院"].groupby("月")["粗利"].sum() / 1000.0)  # 百万/月

    a = preprocess_admission(load_admission_data(DEFAULT_DATA_DIR))
    a["日付"] = pd.to_datetime(a["日付"])
    daily = a.groupby("日付").agg(在院=("在院患者数", "sum"),
                                  新入院=("新入院患者数", "sum"))
    daily["net"] = daily["在院"] - daily["新入院"]
    net = daily["net"].asfreq("D").fillna(0.0)
    net_roll30 = net.rolling(ROLLING_DAYS, min_periods=1).sum()
    net_roll7 = net.rolling(ANCHOR_K, min_periods=1).mean()
    mtd = net.groupby(net.index.to_period("M")).cumsum()

    adm_max = net.index.max()
    months = sorted(nyuin_m.index)

    net_start = _ms(net.index.min())
    # 月別 純在院延べ（単価分母）を先に作る
    net_monthly = net.groupby(net.index.to_period("M")).sum()

    def tanka(m, window=6, ewma_hl=None):
        """単価 = Σ入院粗利 / Σ純在院延べ（leakage-free, 分子分母同一月）。
        window: 直近何か月を使うか。ewma_hl 指定時は月別単価のEWMA。"""
        pms = []
        for k in range(1, window + 1):
            pm = _ms(m - pd.DateOffset(months=k))
            if pm not in nyuin_m.index or pm < net_start:
                continue
            seg = net_monthly.get(pm.to_period("M"), 0.0)
            if seg > 0:
                pms.append((k, nyuin_m[pm], seg))
        if len(pms) < 3:
            return None
        if ewma_hl:  # 月別単価をEWMA（直近重視）
            rates = np.array([p / s for _, p, s in pms])
            ages = np.array([k for k, _, _ in pms])
            w = np.power(0.5, ages / ewma_hl)
            return float(np.average(rates, weights=w))
        num = sum(p for _, p, _ in pms)
        den = sum(s for _, _, s in pms)
        return num / den  # 百万/bed-day（プール平均）

    def build_rows(tanka_kwargs):
        rows = []
        for m in months:
            actual = float(nyuin_m.get(m, np.nan))
            if not np.isfinite(actual) or actual <= 0:
                continue
            t = tanka(m, **tanka_kwargs)
            if t is None:
                continue
            month_end = m + pd.offsets.MonthEnd(0)
            base = min(month_end, adm_max)
            if base < m:
                continue
            days_in_month = month_end.day
            for d in pd.date_range(m, base):
                cal_elapsed = d.day
                remaining = days_in_month - cal_elapsed
                cur = t * net_roll30.get(d, np.nan) * (days_in_month / ROLLING_DAYS)
                anchor = t * (mtd.get(d, np.nan) + net_roll7.get(d, np.nan) * remaining)
                if not (np.isfinite(cur) and np.isfinite(anchor)):
                    continue
                row = {"月": m.strftime("%Y-%m"), "日付": d,
                       "frac": cal_elapsed / days_in_month,
                       "actual": actual, "cur": cur, "anchor": anchor}
                w = min(1.0, cal_elapsed / 10.0)   # 入院MTDブレンド W=10
                row["blend10"] = w * anchor + (1 - w) * cur
                rows.append(row)
        return pd.DataFrame(rows)

    def metrics(dr, col):
        def pt(kind):
            es = []
            for _, g in dr.groupby("月"):
                g = g.sort_values("日付")
                r = (g.iloc[0] if kind == "first" else g.iloc[-1] if kind == "last"
                     else g.loc[(g["frac"] - 0.5).abs().idxmin()])
                es.append((r[col] - r["actual"]) / r["actual"])
            a = np.array(es)
            return round(np.abs(a).mean() * 100, 1), round(a.mean() * 100, 1)
        e = (dr[col] - dr["actual"]) / dr["actual"]
        return pt("first"), pt("mid"), pt("last"), \
            (round(e.abs().mean() * 100, 1), round(e.mean() * 100, 1))

    # ── 方式比較（単価=6moプール固定）──
    dr = build_rows({"window": 6})
    print(f"入院単独・再現月数 {dr['月'].nunique()} / 日次 {len(dr)}  (単価=直近6mo, K={ANCHOR_K}日)")
    print()
    print(f"{'方式':<18}{'月初MAPE':>9}{'中旬MAPE':>9}{'月末MAPE':>9}{'全日MAPE':>9}{'全日bias':>10}")
    for label, key in (("現行(暦日換算)", "cur"), ("アンカー単独", "anchor"),
                       ("入院MTDブレンドW10", "blend10")):
        m0, m5, m9, am = metrics(dr, key)
        print(f"{label:<18}{str(m0[0])+'%':>9}{str(m5[0])+'%':>9}"
              f"{str(m9[0])+'%':>9}{str(am[0])+'%':>9}{am[1]:>+9.1f}%")

    # ── B: 単価recency スイープ（方式=入院MTDブレンドW10 固定）──
    print()
    print("B. 入院単価のrecency化（方式=入院MTDブレンドW10 固定）")
    print(f"{'単価窓':<16}{'月初MAPE':>9}{'中旬MAPE':>9}{'月末MAPE':>9}{'全日MAPE':>9}{'全日bias':>10}")
    specs = [("6moプール(現行)", {"window": 6}), ("3moプール", {"window": 3}),
             ("9moプール", {"window": 9}), ("ewma_hl3", {"window": 9, "ewma_hl": 3}),
             ("ewma_hl2", {"window": 6, "ewma_hl": 2})]
    for label, kw in specs:
        d2 = build_rows(kw)
        m0, m5, m9, am = metrics(d2, "blend10")
        print(f"{label:<16}{str(m0[0])+'%':>9}{str(m5[0])+'%':>9}"
              f"{str(m9[0])+'%':>9}{str(am[0])+'%':>9}{am[1]:>+9.1f}%")


if __name__ == "__main__":
    run()
