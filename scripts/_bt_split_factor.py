"""入院=暦日換算／外来=営業日換算 の分割係数プロトタイプ検証。

現行: 月末見込み = (外来_raw + 入院_raw) × (当月営業日 / 窓内営業日)
変種: 外来_raw × (当月営業日 / 窓内営業日)  +  入院_raw × (当月暦日 / 窓内暦日=30)

既存 backtest_profit_projection.backtest_months と同じ月ループで payload を
1月=1回だけ再現し、外来/入院 raw と営業日系列を取り出して両方式を時点別に比較。
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.lib.config import DEFAULT_DATA_DIR
from app.lib.data_loader import (load_profit_breakdown, load_admission_data,
                                  load_surgery_data)
from app.lib.preprocess import preprocess_admission, preprocess_surgery
from app.lib.profit_estimate import build_hybrid_payload, biz_days_in_month

ROLLING_DAYS = 30  # 窓内暦日 = build_hybrid_payload の rolling_days


def _ms(d):
    return pd.Timestamp(d).normalize().replace(day=1)


def _actual(pb, m):
    g = pb[pb["月"] == m]
    return float(g["粗利"].sum()) / 1000.0 if len(g) else None


def _biz_elapsed(m_start, d):
    rng = pd.date_range(m_start, d)
    from app.lib.profit_estimate import is_operational_day
    return int(sum(1 for x in rng if is_operational_day(x)))


def run():
    pb = load_profit_breakdown(DEFAULT_DATA_DIR)
    pb["月"] = pd.to_datetime(pb["月"]).apply(_ms)
    adm = preprocess_admission(load_admission_data(DEFAULT_DATA_DIR))
    surg = preprocess_surgery(load_surgery_data(DEFAULT_DATA_DIR))
    adm["日付"] = pd.to_datetime(adm["日付"])
    surg["手術実施日"] = pd.to_datetime(surg["手術実施日"])

    # 日次ホスピタル純在院延べ（入院MTDブレンド用）
    dnet = adm.groupby("日付").agg(在院=("在院患者数", "sum"),
                                   新入院=("新入院患者数", "sum"))
    net = (dnet["在院"] - dnet["新入院"]).asfreq("D").fillna(0.0)
    net_roll30 = net.rolling(30, min_periods=1).sum()
    net_roll7 = net.rolling(7, min_periods=1).mean()
    net_mtd = net.groupby(net.index.to_period("M")).cumsum()

    adm_min = pd.Timestamp(adm["日付"].min()).normalize()
    pb_months = sorted(pb["月"].unique())
    first_ok = _ms(adm_min) + pd.DateOffset(months=1)
    target = [m for m in pb_months if m >= first_ok]

    adm_max = pd.Timestamp(adm["日付"].max()).normalize()
    surg_max = pd.Timestamp(surg["手術実施日"].max()).normalize()

    rows = []
    for m_start in target:
        actual = _actual(pb, m_start)
        if not actual or actual <= 0:
            continue
        pb_train = pb[pb["月"] < m_start]
        if pb_train["月"].nunique() < 6:
            continue
        month_end = m_start + pd.offsets.MonthEnd(0)
        base_date = min(month_end, adm_max, surg_max)
        if base_date < m_start:
            continue
        adm_bt = adm[adm["日付"] <= base_date]
        surg_bt = surg[surg["手術実施日"] <= base_date]
        payload = build_hybrid_payload(profit_breakdown=pb_train, surg=surg_bt,
                                       base_date=base_date, adm=adm_bt)
        if not payload or not payload.get("hospital_series"):
            continue
        hs = payload["hospital_series"]
        dates = pd.to_datetime(hs["dates"])
        g_raw = hs["values_gairai"]      # 外来 30日raw
        n_raw = hs["values_nyuin"]       # 入院 30日raw
        proj = hs["values_projection_total"]
        mbz = hs["month_biz_days_series"]
        wbz = hs["window_biz_days_series"]
        days_in_month = month_end.day

        total_biz = biz_days_in_month(m_start)
        for d, gr, nr, pv, mb, wb in zip(dates, g_raw, n_raw, proj, mbz, wbz):
            if pv is None or d < m_start or d > month_end:
                continue
            opf = mb / wb if wb else 0.0            # 営業日係数
            calf = days_in_month / ROLLING_DAYS     # 暦日係数（入院用）
            cur = gr * opf + nr * opf               # 旧（営業日係数, step1前）
            split = gr * opf + nr * calf            # step1（入院=暦日, 本番現行）
            # step2: 入院に MTDブレンド（暦日 calendar と MTD実績アンカーを w で混合）
            nr30 = float(net_roll30.get(d, np.nan))
            n_cal = nr * calf
            if nr30 and np.isfinite(nr30) and nr30 > 0:
                remaining = days_in_month - d.day
                n_anchor = nr * (float(net_mtd.get(d, 0.0))
                                 + float(net_roll7.get(d, 0.0)) * remaining) / nr30
                w = min(1.0, d.day / 10.0)
                n_blend = w * n_anchor + (1 - w) * n_cal
            else:
                n_blend = n_cal
            split2 = gr * opf + n_blend             # step1 + 入院MTDブレンド
            be = _biz_elapsed(m_start, d)
            rows.append({
                "月": m_start.strftime("%Y-%m"),
                "日付": d.strftime("%Y-%m-%d"),
                "be": be, "total_biz": total_biz,
                "frac": be / total_biz if total_biz else 0.0,
                "actual": actual,
                "cur": cur, "split": split, "split2": split2,
                "opf": opf, "calf": calf,
                "n_share": nr * opf / cur if cur else 0,
            })

    dr = pd.DataFrame(rows)

    # ── 既存 recency 補正(k12_shrink50)を各方式の月末アンカーで重ねる（leakage-free）──
    def _mshift(m, back):
        return (pd.Timestamp(m + "-01") - pd.DateOffset(months=back)).strftime("%Y-%m")

    def add_calibrated(col):
        me = dr.sort_values("日付").groupby("月").tail(1)
        ref = {r["月"]: (r["actual"] / r[col]) for _, r in me.iterrows() if r[col] > 0}
        def cfac(month):
            vals = [ref[_mshift(month, j)] for j in range(1, 13)
                    if _mshift(month, j) in ref and np.isfinite(ref[_mshift(month, j)])]
            if not vals:
                return 1.0
            c = float(np.median(vals))
            return 1.0 + (c - 1.0) * 0.5  # shrink50
        dr[col + "_cal"] = dr.apply(lambda r: r[col] * cfac(r["月"]), axis=1)

    add_calibrated("cur")
    add_calibrated("split")
    add_calibrated("split2")

    # 時点別代表日: 月初=最早, 中旬≈0.5, 月末=最終
    def point(method, kind):
        es = []
        for _, g in dr.groupby("月"):
            g = g.sort_values("日付")
            if kind == "first":
                r = g.iloc[0]
            elif kind == "last":
                r = g.iloc[-1]
            else:
                r = g.loc[(g["frac"] - 0.5).abs().idxmin()]
            es.append((r[method] - r["actual"]) / r["actual"])
        a = np.array(es)
        return round(np.abs(a).mean() * 100, 1), round(a.mean() * 100, 1)

    def allday(method):
        e = (dr[method] - dr["actual"]) / dr["actual"]
        return round(e.abs().mean() * 100, 1), round(e.mean() * 100, 1)

    print(f"再現月数: {dr['月'].nunique()} / 日次サンプル: {len(dr)}")
    print(f"入院シェア(中央値): {dr['n_share'].median()*100:.0f}%")
    print()
    hdr = f"{'方式':<10}{'月初MAPE':>9}{'中旬MAPE':>9}{'月末MAPE':>9}{'全日MAPE':>9}{'全日bias':>10}"
    print(hdr)
    for label, key in (("旧(営業日)", "cur"), ("旧+補正", "cur_cal"),
                       ("step1分割", "split"), ("step1+補正", "split_cal"),
                       ("step2入院MTD", "split2"), ("step2+補正", "split2_cal")):
        m0 = point(key, "first"); m5 = point(key, "mid"); m9 = point(key, "last")
        am = allday(key)
        print(f"{label:<14}{str(m0[0])+'%':>9}{str(m5[0])+'%':>9}"
              f"{str(m9[0])+'%':>9}{str(am[0])+'%':>9}{am[1]:>+9.1f}%")
    print()
    print("時点別バイアス（見込み−実績）")
    print(f"{'方式':<10}{'月初':>9}{'中旬':>9}{'月末':>9}")
    for label, key in (("現行", "cur"), ("分割(入院=暦)", "split")):
        m0 = point(key, "first"); m5 = point(key, "mid"); m9 = point(key, "last")
        print(f"{label:<10}{m0[1]:>+8.1f}%{m5[1]:>+8.1f}%{m9[1]:>+8.1f}%")

    # 月初の月別差分（どの月が動くか）
    print()
    print("月初時点の月別比較（現行→分割, 誤差率%）GW影響月に注目")
    print(f"{'月':<9}{'実績':>8}{'現行':>8}{'分割':>8}{'現行%':>8}{'分割%':>8}{'入院係数差':>10}")
    fr = dr.sort_values("日付").groupby("月").head(1)
    for _, r in fr.iterrows():
        ce = (r["cur"] - r["actual"]) / r["actual"] * 100
        se = (r["split"] - r["actual"]) / r["actual"] * 100
        print(f"{r['月']:<9}{r['actual']:>8.0f}{r['cur']:>8.0f}{r['split']:>8.0f}"
              f"{ce:>+7.1f}%{se:>+7.1f}%{(r['opf']-r['calf']):>+10.3f}")


if __name__ == "__main__":
    run()
