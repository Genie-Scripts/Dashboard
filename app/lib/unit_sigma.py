"""unit_sigma.py — P2 判定閾値: ユニット別σの実測値と閾値変換。

`暦補正と学習ループ改修プラン.md` §2 P2（判定の統計是正）の基盤。ユニット（診療科別
在院・病棟別在院・診療科別手術）ごとに実測したσから、判定の矢印しきい値(%)を
`max(floor, 1.5σ)` で決める。

出典: scripts/measure_unit_sigma.py 実測 2026-09-16（base_date=2026-09-15・52週）
  - dept_census / ward_census: 営業日のみに絞った 7d/28d MA スプレッド(%)のσ
    （triage._ma_spread と同一式。stats_band.census_spread_samples を営業日のみへ
    絞った系列に適用）
  - dept_surgery: 術数対象基準（眼科=全手術、他科=全麻）のレート比スプレッド(%)のσ
    （stats_band.surgery_rate_spread_samples_by_dept。triage._surgery_trend が見ている
    metrics.rolling28_surgery_dept と同一の対象定義。全麻固定基準を眼科にそのまま
    使うとσ=74.4相当に縮退する既知バグの是正値=11.9）。
  年1回（年度替わり）に再計測して更新すること。再計測は
  `.venv/bin/python scripts/measure_unit_sigma.py [--base-date YYYY-MM-DD]` を実行し、
  出力の UNIT_SIGMA ブロックで本ファイルの値を丸ごと置き換える。

floor の出典: triage.py の CENSUS_TREND_PT=3.0 / SURGERY_TREND_PT=15.0（既存の判定閾値の
最低保証値）。UNIT_SIGMA に無いユニット（新病棟の追加等でσ未測定）はこの floor へ
フォールバックする。
"""
from __future__ import annotations

from typing import Optional

CENSUS_FLOOR = 3.0
SURGERY_FLOOR = 15.0

UNIT_SIGMA = {
    "dept_census": {
        "リウマチ膠原病内科": 13.08,
        "一般消化器外科": 7.94,
        "乳腺外科": 21.41,
        "呼吸器内科": 8.75,
        "呼吸器外科": 29.27,
        "小児科": 17.06,
        "形成外科": 27.84,
        "循環器内科": 8.96,
        "心臓血管外科": 22.96,
        "救急科": 11.07,
        "整形外科": 7.19,
        "歯科口腔外科": 20.97,
        "泌尿器科": 13.69,
        "消化器内科": 8.94,
        "産婦人科": 9.6,
        "皮膚科": 16.71,
        "眼科": 20.67,
        "総合内科": 5.46,
        "耳鼻咽喉科": 14.41,
        "脳神経内科": 13.55,
        "脳神経外科": 11.83,
        "腎内科": 19.59,
        "血液内科": 10.6,
    },
    "ward_census": {
        "02A": 4.84,
        "02B": 2.52,
        "03A": 3.07,
        "04A": 8.09,
        "04B": 3.21,
        "04C": 3.14,
        "04D": 3.29,
        "05A": 4.83,
        "05B": 5.86,
        "06A": 2.75,
        "06B": 3.05,
        "07A": 6.26,
        "07B": 5.05,
        "08A": 3.84,
        "08B": 3.65,
        "09A": 10.51,
        "09B": 3.61,
    },
    "dept_surgery": {
        "一般消化器外科": 11.33,
        "乳腺外科": 23.15,
        "呼吸器外科": 36.17,
        "形成外科": 37.28,
        "心臓血管外科": 97.66,
        "整形外科": 7.64,
        "歯科口腔外科": 22.02,
        "泌尿器科": 22.87,
        "産婦人科": 12.31,
        "皮膚科": 25.55,
        "眼科": 11.9,
        "耳鼻咽喉科": 14.76,
        "脳神経外科": 57.76,
    },
}


def sigma_for(kind: str, unit: str) -> Optional[float]:
    """kind: 'dept_census' | 'ward_census' | 'dept_surgery'。
    測定値がなければ None（未知ユニット・kind不明のいずれも None に縮退）。"""
    return UNIT_SIGMA.get(kind, {}).get(unit)


def _floor_for(kind: str) -> float:
    return SURGERY_FLOOR if kind == "dept_surgery" else CENSUS_FLOOR


def arrow_threshold(kind: str, unit: str) -> float:
    """判定に使う矢印しきい値(%)。max(floor, 1.5 * σ)。
    σ不明（UNIT_SIGMA に無いユニット）は floor をそのまま返す
    （新病棟・新診療科が増えても壊れないフォールバック）。"""
    floor = _floor_for(kind)
    sigma = sigma_for(kind, unit)
    if sigma is None:
        return floor
    return max(floor, 1.5 * sigma)
