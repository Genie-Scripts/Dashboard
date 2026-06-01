"""
backtest_outpatient_profit.py — 外来件数は外来粗利の駆動量として有効か？
================================================================

【問い】
  外来粗利の月末見込みは、本番では大半が「単価 × 営業日数」の比推定
  （profit_estimate.build_hybrid_payload の ratio_fallback）で出ている。
  外来の **受診件数**（Outpatient-Dashboard 由来）を駆動量に使うと、
  営業日数だけより当たるのか？を leakage-free のバックテストで定量化する。

【比較する3アプローチ（外来粗利を予測）】
  - biz         : 単価 = Σ_train 外来粗利 / Σ_train 営業日数   → pred = 単価 × 営業日数_M
                  （= 現行 ratio_fallback の再現。ベースライン）
  - visit_total : 単価 = Σ_train 外来粗利 / Σ_train 外来件数   → pred = 単価 × 外来件数_M
  - visit_split : 外来粗利 ≈ u初·初診件数 + u再·再診件数（OLS無切片, 非負クリップ）
                  → pred = u初·初診_M + u再·再診_M

【リーク防止】
  対象月 M の予測は、M 未満の確定実績（外来粗利・外来件数・営業日数）だけで学習。
  本番でも M の粗利・件数は未確定なので同条件。

【出力】
  - 病院全体（matched 25科の合計）の時点別ではなく「月末確定値」ベースの
    MAPE・平均バイアス（外来件数は月次なので月内カーブは出さない）
  - naive 比較（前月・前年同月）
  - 科別: 最良アプローチと biz→best_visit の改善幅
  - output/outpatient_profit_backtest.{json,csv}（gitignore済）

usage:
    python -m scripts.backtest_outpatient_profit
    python -m scripts.backtest_outpatient_profit --min-history 4
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

from app.lib.config import DEFAULT_DATA_DIR, biz_days_in_month  # noqa: E402
from app.lib.data_loader import load_profit_breakdown  # noqa: E402
from app.lib.outpatient import load_outpatient_monthly, outpatient_coverage  # noqa: E402

# 評価する予測アプローチ（病院合計・科別で共通）
APPROACHES = ("biz", "visit_total", "visit_split", "selective")


# ──────────────────────────────────────────
# データ準備
# ──────────────────────────────────────────

def _profit_gairai_monthly(pb: pd.DataFrame) -> pd.DataFrame:
    """外来粗利を (診療科名, 月(月初), 外来粗利_百万) に整形。"""
    sub = pb[pb["区分"] == "外来"].copy()
    sub["月"] = pd.to_datetime(sub["月"]).dt.to_period("M").dt.to_timestamp()
    sub["外来粗利"] = sub["粗利"].astype(float) / 1000.0  # 千円→百万円
    return (sub.groupby(["診療科名", "月"], as_index=False)["外来粗利"].sum())


def build_panel(data_dir: str, agg_dir: str | None) -> tuple[pd.DataFrame, dict]:
    """matched 科 × 月 のパネルを構築。

    Returns:
        panel: [診療科名, 月, 外来粗利, 外来件数, 初診件数, 再診件数, 営業日数]
        coverage: outpatient_coverage の診断
    """
    pb = load_profit_breakdown(data_dir)
    op = load_outpatient_monthly(agg_dir)
    cov = outpatient_coverage(pb, op)

    g = _profit_gairai_monthly(pb)
    matched = set(cov["matched"])
    g = g[g["診療科名"].isin(matched)]
    op = op[op["診療科名"].isin(matched)]

    panel = g.merge(op, on=["診療科名", "月"], how="inner")  # 両方揃う月のみ
    panel["営業日数"] = panel["月"].apply(biz_days_in_month).astype(int)
    panel = panel.sort_values(["診療科名", "月"]).reset_index(drop=True)
    return panel, cov


# ──────────────────────────────────────────
# 学習 → 予測（科別・月別, leakage-free）
# ──────────────────────────────────────────

def _fit_ratio(num: float, den: float) -> float | None:
    """単価 = num/den（den<=0 なら None）。"""
    return (num / den) if den > 0 else None


def _fit_split(train: pd.DataFrame) -> tuple[float, float] | None:
    """外来粗利 ≈ u初·初診 + u再·再診 を OLS無切片で。非負クリップ。"""
    if len(train) < 2:
        return None
    X = train[["初診件数", "再診件数"]].astype(float).values
    y = train["外来粗利"].astype(float).values
    if np.all(X == 0) or np.all(y == 0):
        return None
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    u初, u再 = float(coef[0]), float(coef[1])
    return max(0.0, u初), max(0.0, u再)


def _insample_mape(train: pd.DataFrame, unit: float, driver_col: str) -> float | None:
    """単価×driver を train 自身で当てた in-sample MAPE。"""
    a = train["外来粗利"].astype(float).values
    pred = unit * train[driver_col].astype(float).values
    mask = a > 0
    if not mask.any():
        return None
    return float(np.abs((pred[mask] - a[mask]) / a[mask]).mean())


def predict_dept_month(train: pd.DataFrame, row: pd.Series) -> dict:
    """1 (科, 月) に対する各アプローチの予測。学習不能は None。

    selective: train の in-sample 誤差で biz / visit_total の良い方を科ごとに選ぶ
    （本番 hybrid の demote と同型のゲート, leakage-free）。
    """
    preds = {"biz": None, "visit_total": None, "visit_split": None, "selective": None}
    chosen = None

    unit_biz = _fit_ratio(train["外来粗利"].sum(), train["営業日数"].sum())
    if unit_biz is not None:
        preds["biz"] = max(0.0, unit_biz * row["営業日数"])

    unit_v = _fit_ratio(train["外来粗利"].sum(), train["外来件数"].sum())
    if unit_v is not None:
        preds["visit_total"] = max(0.0, unit_v * row["外来件数"])

    split = _fit_split(train)
    if split is not None:
        u初, u再 = split
        preds["visit_split"] = max(0.0, u初 * row["初診件数"] + u再 * row["再診件数"])

    # selective: 科ごとに train in-sample で biz vs visit_total を選択
    if unit_biz is not None and unit_v is not None:
        m_biz = _insample_mape(train, unit_biz, "営業日数")
        m_v = _insample_mape(train, unit_v, "外来件数")
        if m_biz is not None and m_v is not None:
            chosen = "visit_total" if m_v < m_biz else "biz"
            preds["selective"] = preds[chosen]
    if preds["selective"] is None:
        preds["selective"] = preds["biz"]  # 比較不能なら現行(biz)維持
        chosen = chosen or "biz"

    preds["_chosen"] = chosen
    return preds


def run_backtest(panel: pd.DataFrame, min_history: int) -> tuple[list[dict], list[dict]]:
    """拡大窓バックテスト。

    Returns:
        dept_rows:  科×月×アプローチ の予測/実績/誤差
        month_rows: 病院全体（matched合計）の月別 実績/予測/誤差（naive 付き）
    """
    all_months = sorted(panel["月"].unique())
    dept_rows: list[dict] = []
    month_rows: list[dict] = []

    # 病院全体の月次実績（naive 用に matched 合計を月で）
    hosp_actual = panel.groupby("月")["外来粗利"].sum()

    for M in all_months:
        train_all = panel[panel["月"] < M]
        if train_all["月"].nunique() < min_history:
            continue
        cur = panel[panel["月"] == M]
        if cur.empty:
            continue

        hosp = {"月": pd.Timestamp(M).strftime("%Y-%m"), "実績": 0.0}
        for k in APPROACHES:
            hosp[k] = 0.0
        hosp["_ok"] = {k: True for k in APPROACHES}

        for _, row in cur.iterrows():
            dept = row["診療科名"]
            train = train_all[train_all["診療科名"] == dept]
            if train.empty:
                continue
            preds = predict_dept_month(train, row)
            actual = float(row["外来粗利"])
            hosp["実績"] += actual
            dept_rows.append({
                "月": hosp["月"], "診療科名": dept, "実績": round(actual, 3),
                **{k: (round(preds[k], 3) if preds[k] is not None else None) for k in APPROACHES},
                "selected": preds.get("_chosen"),
                "n_train_months": int(train["月"].nunique()),
            })
            for k in APPROACHES:
                if preds[k] is None:
                    hosp["_ok"][k] = False  # 1科でも欠ければ病院合計は不能扱い
                else:
                    hosp[k] += preds[k]

        actual_h = hosp["実績"]
        if actual_h <= 0:
            continue
        rec = {"月": hosp["月"], "実績": round(actual_h, 2)}
        for k in APPROACHES:
            if hosp["_ok"][k]:
                pred = hosp[k]
                rec[f"予測_{k}"] = round(pred, 2)
                rec[f"誤差率_{k}"] = round((pred - actual_h) / actual_h, 4)
            else:
                rec[f"予測_{k}"] = None
                rec[f"誤差率_{k}"] = None

        # naive
        prev = hosp_actual.get(pd.Timestamp(M) - pd.DateOffset(months=1))
        prevy = hosp_actual.get(pd.Timestamp(M) - pd.DateOffset(months=12))
        rec["naive_前月"] = round(float(prev), 2) if prev is not None else None
        rec["naive_前月_誤差率"] = round((float(prev) - actual_h) / actual_h, 4) if prev is not None else None
        rec["naive_前年同月"] = round(float(prevy), 2) if prevy is not None else None
        rec["naive_前年同月_誤差率"] = round((float(prevy) - actual_h) / actual_h, 4) if prevy is not None else None
        month_rows.append(rec)

    return dept_rows, month_rows


# ──────────────────────────────────────────
# 集計
# ──────────────────────────────────────────

def _mape_bias(errs: list[float]) -> dict:
    a = np.array([e for e in errs if e is not None], dtype=float)
    if len(a) == 0:
        return {"MAPE": None, "平均バイアス": None, "n": 0}
    return {"MAPE": round(float(np.abs(a).mean()) * 100, 1),
            "平均バイアス": round(float(a.mean()) * 100, 1),
            "n": int(len(a))}


def summarize(dept_rows: list[dict], month_rows: list[dict]) -> dict:
    mr = pd.DataFrame(month_rows)
    hospital = {}
    if len(mr):
        for k in APPROACHES:
            hospital[k] = _mape_bias(mr[f"誤差率_{k}"].tolist())
        hospital["naive_前月"] = _mape_bias(mr["naive_前月_誤差率"].tolist())
        hospital["naive_前年同月"] = _mape_bias(mr["naive_前年同月_誤差率"].tolist())

    # 科別: アプローチ別 MAPE と best
    dr = pd.DataFrame(dept_rows)
    by_dept = []
    if len(dr):
        for dept, g in dr.groupby("診療科名"):
            actual = g["実績"].astype(float)
            row = {"診療科名": dept, "n_months": int(len(g))}
            mapes = {}
            for k in ("biz", "visit_total", "visit_split"):
                pred = g[k]
                ok = pred.notna() & (actual > 0)
                if ok.any():
                    ape = (pred[ok].astype(float) - actual[ok]).abs() / actual[ok]
                    mapes[k] = round(float(ape.mean()) * 100, 1)
                else:
                    mapes[k] = None
            row.update({f"MAPE_{k}": v for k, v in mapes.items()})
            visit_opts = {k: mapes[k] for k in ("visit_total", "visit_split") if mapes[k] is not None}
            best_visit = min(visit_opts, key=visit_opts.get) if visit_opts else None
            row["best_visit"] = best_visit
            if best_visit is not None and mapes["biz"] is not None:
                row["改善pt_vs_biz"] = round(mapes["biz"] - mapes[best_visit], 1)  # +で外来件数が改善
            else:
                row["改善pt_vs_biz"] = None
            by_dept.append(row)
        by_dept.sort(key=lambda r: (r["改善pt_vs_biz"] is None, -(r["改善pt_vs_biz"] or 0)))

    return {"hospital": hospital, "by_dept": by_dept}


# ──────────────────────────────────────────
# main
# ──────────────────────────────────────────

def _fmt(d: dict) -> str:
    if not d or d.get("MAPE") is None:
        return "  —"
    return f"MAPE {d['MAPE']:>5}%  bias {d['平均バイアス']:+5}%  (n={d['n']})"


def main():
    ap = argparse.ArgumentParser(description="外来件数×外来粗利 バックテスト")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--agg-dir", default=None, help="外来集計CSVルート（既定: config.OUTPATIENT_AGG_DIR）")
    ap.add_argument("--min-history", type=int, default=4, help="学習に要する最小月数")
    args = ap.parse_args()

    panel, cov = build_panel(args.data_dir, args.agg_dir)
    if panel.empty:
        print("❌ パネルが空です。外来集計CSV(OUTPATIENT_AGG_DIR)と粗利データを確認してください。")
        print(f"   coverage: {cov}")
        sys.exit(1)

    dept_rows, month_rows = run_backtest(panel, args.min_history)
    summary = summarize(dept_rows, month_rows)

    # ── コンソール出力 ──
    print("=" * 68)
    print("外来件数は外来粗利の駆動量として有効か（leakage-free バックテスト）")
    print("=" * 68)
    print(f"対象科: matched {len(cov['matched'])} 科 / 外来データ {cov['month_min']}〜{cov['month_max']} "
          f"({cov['n_months']}ヶ月) / 名寄れ漏れ: {cov['profit_only'] or 'なし'}")
    print(f"評価月数: {len(month_rows)}（min_history={args.min_history}）")
    print()
    print("── 病院全体（matched合計）外来粗利の月末確定値 予測精度 ──")
    h = summary["hospital"]
    print(f"  biz (営業日数・現行)      : {_fmt(h.get('biz'))}")
    print(f"  visit_total (外来件数)    : {_fmt(h.get('visit_total'))}")
    print(f"  visit_split (初診/再診)   : {_fmt(h.get('visit_split'))}")
    print(f"  selective (科別best,demote): {_fmt(h.get('selective'))}")
    print(f"  naive 前月                : {_fmt(h.get('naive_前月'))}")
    print(f"  naive 前年同月            : {_fmt(h.get('naive_前年同月'))}")
    print()
    print("── 科別: 外来件数で改善する科 上位/下位（改善pt = biz MAPE − best_visit MAPE, +で改善）──")
    print(f"  {'診療科':<14}{'biz':>7}{'v_total':>9}{'v_split':>9}{'best':>12}{'改善pt':>8}")
    bd = summary["by_dept"]
    rows = bd if len(bd) <= 12 else (bd[:8] + ["..."] + bd[-4:])

    def _s(x):
        return f"{x:>7}" if x is not None else f"{'—':>7}"

    for r in rows:
        if r == "...":
            print("  ...")
            continue
        imp = r.get("改善pt_vs_biz")
        imp_s = f"{imp:+.1f}" if imp is not None else "—"
        print(f"  {r['診療科名']:<14}{_s(r.get('MAPE_biz'))}"
              f"{_s(r.get('MAPE_visit_total'))}{_s(r.get('MAPE_visit_split'))}"
              f"{str(r.get('best_visit') or '—'):>12}{imp_s:>8}")

    # ── 成果物書き出し ──
    out_dir = ROOT / "output"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "outpatient_profit_backtest.json").write_text(
        json.dumps({"coverage": cov, "summary": summary, "month_rows": month_rows},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(dept_rows).to_csv(
        out_dir / "outpatient_profit_backtest_monthly.csv", index=False, encoding="utf-8-sig")
    print()
    print(f"✅ 出力: output/outpatient_profit_backtest.json / _monthly.csv")


if __name__ == "__main__":
    main()
