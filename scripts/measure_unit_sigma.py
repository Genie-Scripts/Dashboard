#!/usr/bin/env python3
"""measure_unit_sigma.py — P2 判定閾値用 ユニット別σ 測定ハーネス（読み取り専用）。

実データから52週の週次サンプルでユニット（診療科別在院・病棟別在院・診療科別手術）
ごとのσを測り、`app/lib/unit_sigma.py` の UNIT_SIGMA にそのまま貼れる Python literal
を stdout に出す。リポジトリの書き換えは一切行わない（data/・output/ への書込み無し）。

判定で使う定義そのもので測る:
  - 在院（dept_census / ward_census）: 営業日のみに絞った 7d/28d MA スプレッド
    （triage.py の _ma_spread と同じ式。stats_band.census_spread_samples に
    営業日のみへ絞った系列を渡すことで同一式を再利用する）
  - 手術（dept_surgery）: 術数対象基準のレート比
    （app.lib.stats_band.surgery_rate_spread_samples_by_dept。
    triage._surgery_trend が見ている rolling28_surgery_dept と同一の対象定義）

対象ユニット:
  - dept_census  : NADM_DISPLAY_DEPTS | SURGERY_DISPLAY_DEPTS | PROFIT_ONLY_DISPLAY_DEPTS
  - ward_census  : WARD_NAMES から WARD_HIDDEN を除いたもの（キーは病棟コード）
  - dept_surgery : SURGERY_EVAL_DEPTS（SURGERY_DISPLAY_DEPTS | ALLSURG_NORTH_STAR_DEPTS）

サンプル不足（stats_band.MIN_SAMPLES 未満）でσが求まらないユニットは出力 dict から
除外する（app/lib/unit_sigma.py 側の sigma_for() が floor へフォールバックする設計）。

実行: リポジトリルートで
    .venv/bin/python scripts/measure_unit_sigma.py [--data-dir data] [--base-date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.config import (  # noqa: E402
    DEFAULT_DATA_DIR,
    NADM_DISPLAY_DEPTS, SURGERY_DISPLAY_DEPTS, SURGERY_EVAL_DEPTS,
    ALLSURG_NORTH_STAR_DEPTS, PROFIT_ONLY_DISPLAY_DEPTS, WARD_NAMES, WARD_HIDDEN,
    is_operational_day,
)
from app.lib.data_loader import load_all  # noqa: E402
from app.lib.preprocess import preprocess_admission, preprocess_surgery  # noqa: E402
from app.lib.metrics import build_daily_series  # noqa: E402
from app.lib.stats_band import (  # noqa: E402
    census_spread_samples, surgery_rate_spread_samples,
    surgery_rate_spread_samples_by_dept, unit_sigma,
)

WEEKS = 52


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="P2 ユニット別σ 測定（読み取り専用・stdoutのみ）")
    p.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="データフォルダ")
    p.add_argument("--base-date", default=None, help="基準日 YYYY-MM-DD（既定: adm日付の最大値）")
    return p.parse_args()


def _census_sigma(series: pd.DataFrame, base_date) -> float | None:
    """在院系列（営業日のみに絞る）から σ を測る。"""
    if len(series) == 0:
        return None
    biz_only = series[series["日付"].apply(is_operational_day)]
    samples = census_spread_samples(biz_only, base_date, weeks=WEEKS)
    return unit_sigma(samples)


def measure(adm: pd.DataFrame, surg: pd.DataFrame, base_date) -> dict:
    result: dict = {"dept_census": {}, "ward_census": {}, "dept_surgery": {}}

    for dept in sorted(NADM_DISPLAY_DEPTS | SURGERY_DISPLAY_DEPTS | PROFIT_ONLY_DISPLAY_DEPTS):
        series = build_daily_series(adm, "在院患者数", group_col="診療科名", group_val=dept)
        sigma = _census_sigma(series, base_date)
        if sigma is not None:
            result["dept_census"][dept] = sigma

    for wcode in sorted(WARD_NAMES):
        if wcode in WARD_HIDDEN:
            continue
        series = build_daily_series(adm, "在院患者数", group_col="病棟コード", group_val=wcode)
        sigma = _census_sigma(series, base_date)
        if sigma is not None:
            result["ward_census"][wcode] = sigma

    for dept in sorted(SURGERY_EVAL_DEPTS):
        samples = surgery_rate_spread_samples_by_dept(surg, base_date, dept, weeks=WEEKS)
        sigma = unit_sigma(samples)
        if sigma is not None:
            result["dept_surgery"][dept] = sigma

    return result


def _fmt_dict(d: dict) -> str:
    lines = ["    {"]
    for k, v in d.items():
        lines.append(f"        {k!r}: {v},")
    lines.append("    }")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    data = load_all(args.data_dir)
    adm = preprocess_admission(data["admission"])
    surg = preprocess_surgery(data["surgery"])
    base_date = pd.Timestamp(args.base_date) if args.base_date else adm["日付"].max()

    result = measure(adm, surg, base_date)

    print("# ─── ここから app/lib/unit_sigma.py に貼り付け ───")
    print(f"# 出典: measure_unit_sigma.py 実測 {datetime.now().strftime('%Y-%m-%d')}"
          f"（base_date={base_date.strftime('%Y-%m-%d')}・{WEEKS}週）")
    print("#   dept_census/ward_census = 営業日のみ7d/28d MAスプレッドのσ（%）")
    print("#   dept_surgery            = 術数対象基準のレート比スプレッドのσ（%）")
    print("#   年1回（年度替わり）に再計測して更新すること。")
    print("UNIT_SIGMA = {")
    print('    "dept_census": ' + _fmt_dict(result["dept_census"]).strip() + ",")
    print('    "ward_census": ' + _fmt_dict(result["ward_census"]).strip() + ",")
    print('    "dept_surgery": ' + _fmt_dict(result["dept_surgery"]).strip() + ",")
    print("}")
    print("# ─── ここまで ───")

    def _summary(label, d):
        if not d:
            print(f"{label}: n=0")
            return
        vals = sorted(d.values())
        n = len(vals)
        mid = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
        print(f"{label}: n={n} median={mid:.2f} range=[{vals[0]:.2f}, {vals[-1]:.2f}]")

    print("", file=sys.stderr)
    print("# サマリ（stderr）", file=sys.stderr)
    for label, key in (("dept_census", "dept_census"), ("ward_census", "ward_census"),
                       ("dept_surgery", "dept_surgery")):
        vals = sorted(result[key].values())
        n = len(vals)
        if n == 0:
            print(f"{label}: n=0", file=sys.stderr)
            continue
        mid = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
        print(f"{label}: n={n} median={mid:.2f} range=[{vals[0]:.2f}, {vals[-1]:.2f}]", file=sys.stderr)

    # 参考: 眼科について、既知バグの全麻固定基準（眼科データのみに絞って旧関数へ通した
    # 場合）と術数対象基準を比較表示する（是正の効果を実測で示すため）。
    if "眼科" in ALLSURG_NORTH_STAR_DEPTS:
        eye_surg = surg[surg["実施診療科"] == "眼科"]
        ga_sigma = unit_sigma(surgery_rate_spread_samples(eye_surg, base_date, weeks=WEEKS))
        target_sigma = result["dept_surgery"].get("眼科")
        print(f"眼科 σ比較: 全麻基準={ga_sigma} / 術数対象基準={target_sigma}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
