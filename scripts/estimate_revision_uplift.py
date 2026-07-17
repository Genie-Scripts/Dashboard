"""
estimate_revision_uplift.py — 2026-06診療報酬改定 換算係数の再測定
================================================================

【背景】
  profit_estimate.py の学習パイプラインは、改定前(月 < FEE_REVISION_DATE)の
  確定粗利に区分別係数 FEE_REVISION_PROFIT_UPLIFT（config.py）を乗じて
  改定後スケールへ換算してから学習する（改定換算）。この係数は改定後確定月が
  積み上がるたびに再測定し、config.py を更新する必要がある。

【測定方法（すべて無換算・改定前のみ学習の投影で測る）】
  各対象月 m（改定直前 --baseline-months 件 + 改定後の確定月すべて）について、
  「m 未満 かつ 改定前」の確報だけで学習した hybrid payload（fee_revision_adjust=False）
  から m の月末見込み（MTDブレンド, leakage-free）を再現し、確定実績との比
  ratio(m) = actual(m) / proj(m) を区分別に求める。

  改定直前の baseline 比の中央値を「無換算モデルの地の誤差（バイアス）」の
  基準とし、改定後各月の比をこの基準で正規化した uplift(m) = ratio(m) / base_ratio
  の中央値を推奨係数として提示する（= 改定前後で共通のモデルバイアスを相殺し、
  改定による単価シフトだけを抽出する）。

【リーク防止】
  各対象月 m の学習用 pb は "m 未満 かつ 改定前" のみに切詰める。改定後月を
  post 側で評価する際も、改定後の確定実績が学習側の12ヶ月窓に混入して
  「改定前価格の参照モデル」を汚染しないようにするため（スケール純粋性）。

usage:
    python -m scripts.estimate_revision_uplift
    python -m scripts.estimate_revision_uplift --baseline-months 3 --min-history 6
    python -m scripts.estimate_revision_uplift --out-json output/revision_uplift.json
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

from app.lib.config import DEFAULT_DATA_DIR, FEE_REVISION_DATE  # noqa: E402
from app.lib.data_loader import (  # noqa: E402
    load_admission_data, load_surgery_data, load_profit_breakdown,
)
from app.lib.preprocess import preprocess_admission, preprocess_surgery  # noqa: E402
from app.lib.profit_estimate import build_hybrid_payload  # noqa: E402

FEE_REVISION_TS = pd.Timestamp(FEE_REVISION_DATE)
KINDS = ("外来", "入院")


def _month_start(d) -> pd.Timestamp:
    return pd.Timestamp(d).normalize().replace(day=1)


def _month_actual_by_kind(pb: pd.DataFrame, m: pd.Timestamp, kind: str) -> float | None:
    """対象月・区分の確定粗利（百万円）。無ければ None。"""
    sub = pb[(pb["月"] == m) & (pb["区分"] == kind)]
    if sub.empty:
        return None
    return float(sub["粗利"].sum()) / 1000.0


def measure_ratios(pb: pd.DataFrame,
                    adm: pd.DataFrame,
                    surg: pd.DataFrame,
                    target_months: list[pd.Timestamp],
                    min_history: int) -> list[dict]:
    """各対象月について、無換算・改定前のみ学習の投影から actual/proj 比を測る。

    Returns: 月別・区分別の行（月, 区分, actual, proj, ratio）
    """
    rows: list[dict] = []
    adm_max = pd.Timestamp(adm["日付"].max()).normalize()
    surg_max = pd.Timestamp(surg["手術実施日"].max()).normalize()

    for m in target_months:
        # 学習用 pb は m 未満 かつ 改定前のみ（leakage-free かつスケール純粋）
        pb_train = pb[(pb["月"] < m) & (pb["月"] < FEE_REVISION_TS)]
        n_hist = pb_train["月"].nunique()
        if n_hist < min_history:
            print(f"  [skip] {m.strftime('%Y-%m')}: 学習用の履歴月数 {n_hist} < {min_history}")
            continue

        month_end = m + pd.offsets.MonthEnd(0)
        base_date = min(month_end, adm_max, surg_max)
        if base_date < m:
            print(f"  [skip] {m.strftime('%Y-%m')}: ドライバーデータが月初に届いていない")
            continue

        adm_bt = adm[adm["日付"] <= base_date]
        surg_bt = surg[surg["手術実施日"] <= base_date]

        payload = build_hybrid_payload(
            profit_breakdown=pb_train, surg=surg_bt, base_date=base_date, adm=adm_bt,
            fee_revision_adjust=False,
        )
        if not payload or not payload.get("meta"):
            print(f"  [skip] {m.strftime('%Y-%m')}: hybrid payload を構築できない")
            continue
        meta = payload["meta"]
        proj_by_kind = {
            "外来": meta.get("latest_mtdblend_gairai"),
            "入院": meta.get("latest_mtdblend_nyuin"),
        }

        for kind in KINDS:
            proj = proj_by_kind.get(kind)
            actual = _month_actual_by_kind(pb, m, kind)
            if proj is None or proj <= 0 or actual is None:
                continue
            rows.append({
                "月":    m.strftime("%Y-%m"),
                "区分":  kind,
                "実績":  round(actual, 2),
                "見込み": round(float(proj), 2),
                "ratio": actual / float(proj),
                "n_hist_months": int(n_hist),
                "base_date": base_date.strftime("%Y-%m-%d"),
            })
    return rows


def compute_uplift(rows: list[dict],
                    baseline_months: list[str],
                    post_months: list[str]) -> tuple[dict, dict]:
    """baseline の ratio 中央値を基準に、post の uplift(月, 区分) の中央値を係数として算出。

    Returns: (base_ratio_by_kind, recommendation)
        recommendation: {kind: {"uplift": float, "n_post": int}}
    """
    df = pd.DataFrame(rows)
    base_ratio: dict[str, float] = {}
    for kind in KINDS:
        sub = df[(df["区分"] == kind) & (df["月"].isin(baseline_months))]
        if len(sub):
            base_ratio[kind] = float(np.median(sub["ratio"]))

    # 全行に uplift 列を付与（baseline行は 1.0 付近になるはず = QC 用）
    df["uplift"] = df.apply(
        lambda r: r["ratio"] / base_ratio[r["区分"]] if r["区分"] in base_ratio else None,
        axis=1)
    rows_with_uplift = df.to_dict("records")

    recommendation: dict[str, dict] = {}
    for kind in KINDS:
        sub = df[(df["区分"] == kind) & (df["月"].isin(post_months))]
        vals = sub["uplift"].dropna().tolist()
        if vals:
            recommendation[kind] = {
                "uplift": round(float(np.median(vals)), 3),
                "n_post": len(vals),
            }
    return rows_with_uplift, recommendation


def print_table(rows: list[dict]):
    if not rows:
        print("(測定できた月がありません)")
        return
    print(f"{'月':<9}{'区分':<6}{'実績(百万)':>12}{'見込み(百万)':>14}{'ratio':>9}{'uplift':>9}")
    for r in rows:
        uplift = r.get("uplift")
        uplift_str = f"{uplift:.4f}" if uplift is not None else "—"
        print(f"{r['月']:<9}{r['区分']:<6}{r['実績']:>12.2f}{r['見込み']:>14.2f}"
              f"{r['ratio']:>9.4f}{uplift_str:>9}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--baseline-months", type=int, default=3,
                    help="改定直前・基準比として使う確報月数")
    ap.add_argument("--min-history", type=int, default=6,
                    help="各対象月の投影に必要な最小の学習履歴月数（改定前のみ）")
    ap.add_argument("--out-json", default=None,
                    help="指定時のみ JSON 出力（例: output/revision_uplift.json）")
    args = ap.parse_args()

    print("[1/3] データ読込中...")
    pb = load_profit_breakdown(args.data_dir)
    if pb is None or len(pb) == 0:
        print("profit_breakdown が取得できません（profit_data の内訳シート未整備）。中止します。")
        return
    pb["月"] = pd.to_datetime(pb["月"]).apply(_month_start)
    adm = preprocess_admission(load_admission_data(args.data_dir))
    surg = preprocess_surgery(load_surgery_data(args.data_dir))
    adm["日付"] = pd.to_datetime(adm["日付"])
    surg["手術実施日"] = pd.to_datetime(surg["手術実施日"])

    pb_months = sorted(pb["月"].unique())
    baseline_candidates = [m for m in pb_months if m < FEE_REVISION_TS]
    post_candidates = [m for m in pb_months if m >= FEE_REVISION_TS]

    if not baseline_candidates:
        print(f"改定前(< {FEE_REVISION_DATE})の確報月がありません。中止します。")
        return
    if not post_candidates:
        print(f"改定後(>= {FEE_REVISION_DATE})の確報月がまだありません。中止します。"
              "\n  → 改定後確定月が出てから再実行してください。")
        return

    baseline_target = baseline_candidates[-args.baseline_months:]
    post_target = post_candidates
    target_months = baseline_target + post_target
    baseline_keys = [m.strftime("%Y-%m") for m in baseline_target]
    post_keys = [m.strftime("%Y-%m") for m in post_target]

    print(f"  baseline 対象月: {baseline_keys}")
    print(f"  post     対象月: {post_keys}")

    print("[2/3] 無換算・改定前のみ学習の投影を各対象月で再現中...")
    rows = measure_ratios(pb, adm, surg, target_months, args.min_history)

    print("[3/3] 係数算出・出力...")
    rows_with_uplift, recommendation = compute_uplift(rows, baseline_keys, post_keys)

    print("\n" + "=" * 72)
    print("改定アップリフト係数 再測定")
    print("=" * 72)
    print_table(rows_with_uplift)

    print("\n推奨係数（config.py FEE_REVISION_PROFIT_UPLIFT へ反映）:")
    if len(recommendation) == len(KINDS):
        formatted = ", ".join(f'"{k}": {recommendation[k]["uplift"]:.3f}' for k in KINDS)
        print(f'FEE_REVISION_PROFIT_UPLIFT = {{{formatted}}}')
    else:
        missing = [k for k in KINDS if k not in recommendation]
        print(f"  算出不可（区分 {missing} の post 実績/見込みが揃いませんでした）")

    n_post_months = len(post_keys)
    if n_post_months < 3:
        print(f"\n※ 改定後確定月 n={n_post_months} のため暫定。"
              "2〜3か月たまったら再実行して更新のこと。")

    if args.out_json:
        out_path = Path(args.out_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({
            "baseline_months": baseline_keys,
            "post_months": post_keys,
            "rows": rows_with_uplift,
            "recommendation": recommendation,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  → {out_path.resolve()}")


if __name__ == "__main__":
    main()
