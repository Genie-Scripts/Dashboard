#!/usr/bin/env python3
"""measure_arrow_flips.py — P2受入検証 V1: 矢印反転回数の測り直し（読み取り専用）。

現行方式（P2適用前: 固定閾値・暦日ベースのスプレッド・週次の生discretize・確定なし）と
新方式（P2: 営業日ベースのスプレッド＝`_ma_spread(biz_only=True)`・ユニット別動的閾値＝
`unit_sigma.arrow_threshold`・`confirmed_trend_dir` の2週連続確定＋非対称ヒステリシス）の
双方で、直近 --weeks 週の週次サンプル点における矢印表示状態（up/down/表示なし）の
変化回数（反転）を数える。

⚠️ 仕様策定時のシミュレーション値（1030回→66回等、spec/暦補正と学習ループ改修プラン.md §2）は
策定側が独自に測ったσに基づく参考値。本スクリプトは実際に出荷する app/lib/unit_sigma.py の
UNIT_SIGMA 値・`app/lib/triage.py` の実装で測り直す（司令塔の受入検証用）。

対象ユニット:
  - dept_census  : NADM_DISPLAY_DEPTS（在院・診療科別）
  - ward_census  : WARD_NAMES から WARD_HIDDEN を除いたもの（在院・病棟別）
  - dept_surgery : SURGERY_EVAL_DEPTS（全麻/術数対象・診療科別）

リポジトリの書き換えは一切行わない（data/・output/ への書込み無し・常駐サーバへの
リクエストも行わない。ローカルの data/ フォルダを読むだけ）。

実行: リポジトリルートで
    .venv/bin/python scripts/measure_arrow_flips.py [--data-dir data] [--base-date YYYY-MM-DD] [--weeks 52]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.config import (  # noqa: E402
    DEFAULT_DATA_DIR, NADM_DISPLAY_DEPTS, SURGERY_EVAL_DEPTS,
    WARD_NAMES, WARD_HIDDEN,
)
from app.lib.data_loader import load_all  # noqa: E402
from app.lib.preprocess import preprocess_admission, preprocess_surgery  # noqa: E402
from app.lib.metrics import build_daily_series, rolling28_surgery_dept  # noqa: E402
from app.lib import triage as tg  # noqa: E402

# P2適用前（現在の tg.SURGERY_TREND_MIN_28D は 40 へ変更済）の生件数ゲート。
# 「現行方式」を忠実に再現するためにここで固定する（tg側の定数は新方式が使う）。
OLD_SURGERY_MIN_28D = 8


def _display_state(direction):
    """up/down のみを矢印表示状態として扱う（portal.html は up/down しか矢印を描画
    しないため、flat と None は「表示なし」として同一視する）。"""
    return direction if direction in ("up", "down") else None


def _count_flips(seq) -> int:
    states = [_display_state(d) for d in seq]
    return sum(1 for a, b in zip(states, states[1:]) if a != b)


def _weekly_dates(base_date: pd.Timestamp, weeks: int) -> list:
    base_date = pd.Timestamp(base_date)
    monday = base_date - pd.Timedelta(days=base_date.weekday())
    return [monday - pd.Timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]


def _old_census_dir(series: pd.DataFrame, d: pd.Timestamp):
    """P2適用前: 暦日ベースのスプレッド・固定閾値・確定なし。"""
    spread = tg._ma_spread(series, d, tg.CENSUS_MA_SHORT, tg.CENSUS_MA_LONG, biz_only=False)
    return tg._trend_dir(spread, tg.CENSUS_TREND_PT)


def _new_census_dir(series: pd.DataFrame, d: pd.Timestamp, kind: str, unit: str):
    """P2: 営業日ベース・動的閾値・confirmed_trend_dir。"""
    _, direction = tg._census_trend_from_series(series, d, kind, unit)
    return direction


def _old_surgery_dir(surg: pd.DataFrame, dept: str, d: pd.Timestamp):
    now = rolling28_surgery_dept(surg, d)["by_dept"].get(dept, 0)
    if now < OLD_SURGERY_MIN_28D:
        return None
    prev = rolling28_surgery_dept(surg, d - pd.Timedelta(days=tg.SURGERY_TREND_WIN))["by_dept"].get(dept, 0)
    _, direction = tg._surgery_trend(now, prev, d)
    return direction


def _new_surgery_dir(surg: pd.DataFrame, dept: str, d: pd.Timestamp):
    now = rolling28_surgery_dept(surg, d)["by_dept"].get(dept, 0)
    prev = rolling28_surgery_dept(surg, d - pd.Timedelta(days=tg.SURGERY_TREND_WIN))["by_dept"].get(dept, 0)
    _, direction = tg._surgery_trend(now, prev, d, dept=dept, surg=surg)
    return direction


def measure(adm: pd.DataFrame, surg: pd.DataFrame, base_date: pd.Timestamp, weeks: int) -> dict:
    dates = _weekly_dates(base_date, weeks)
    result: dict = {"dept_census": {}, "ward_census": {}, "dept_surgery": {}}

    for dept in sorted(NADM_DISPLAY_DEPTS):
        s = build_daily_series(adm, "在院患者数", group_col="診療科名", group_val=dept)
        old_seq = [_old_census_dir(s, d) for d in dates]
        new_seq = [_new_census_dir(s, d, "dept_census", dept) for d in dates]
        result["dept_census"][dept] = (_count_flips(old_seq), _count_flips(new_seq))

    for wcode, wname in sorted(WARD_NAMES.items()):
        if wcode in WARD_HIDDEN:
            continue
        s = build_daily_series(adm, "在院患者数", group_col="病棟コード", group_val=wcode)
        old_seq = [_old_census_dir(s, d) for d in dates]
        new_seq = [_new_census_dir(s, d, "ward_census", wcode) for d in dates]
        result["ward_census"][f"{wcode}({wname})"] = (_count_flips(old_seq), _count_flips(new_seq))

    for dept in sorted(SURGERY_EVAL_DEPTS):
        old_seq = [_old_surgery_dir(surg, dept, d) for d in dates]
        new_seq = [_new_surgery_dir(surg, dept, d) for d in dates]
        result["dept_surgery"][dept] = (_count_flips(old_seq), _count_flips(new_seq))

    return result


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="P2受入検証 V1: 矢印反転回数の測り直し（読み取り専用）")
    p.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="データフォルダ")
    p.add_argument("--base-date", default=None, help="基準日 YYYY-MM-DD（既定: adm日付の最大値）")
    p.add_argument("--weeks", type=int, default=52, help="測定週数（既定52）")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    data = load_all(args.data_dir)
    adm = preprocess_admission(data["admission"])
    surg = preprocess_surgery(data["surgery"])
    base_date = pd.Timestamp(args.base_date) if args.base_date else adm["日付"].max()

    print(f"# base_date={base_date.date()} weeks={args.weeks} "
          f"(旧全麻ゲート={OLD_SURGERY_MIN_28D} / 新全麻ゲート={tg.SURGERY_TREND_MIN_28D})")

    result = measure(adm, surg, base_date, args.weeks)

    total_old = total_new = 0
    for kind, units in result.items():
        k_old = sum(o for o, n in units.values())
        k_new = sum(n for o, n in units.values())
        total_old += k_old
        total_new += k_new
        print(f"\n[{kind}] n_units={len(units)} old_total={k_old} new_total={k_new}")
        for unit, (o, n) in sorted(units.items(), key=lambda kv: -kv[1][0]):
            if o or n:
                print(f"    {unit}: old={o} new={n}")

    ratio = (total_new / total_old) if total_old else float("nan")
    verdict = "GO(1/3以下)" if total_old and total_new <= total_old / 3 else "NO-GO/要確認"
    print(f"\n=== 合計: old={total_old} new={total_new} 比率={ratio:.3f} 受入判定={verdict} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
