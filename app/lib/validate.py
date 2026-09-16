"""
validate.py — データ検証モジュール
generate_html.py 起動前に呼び出してデータの整合性を確認する（静的生成へ移行済み）。
"""

from __future__ import annotations
import os
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
import pandas as pd


# ────────────────────────────────────────────────────
# 検証結果クラス
# ────────────────────────────────────────────────────

class ValidationResult:
    """検証結果の集約"""

    def __init__(self):
        self.errors:   list[str] = []
        self.warnings: list[str] = []
        self.infos:    list[str] = []

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0

    def error(self, msg: str):
        self.errors.append(f"❌ {msg}")

    def warn(self, msg: str):
        self.warnings.append(f"⚠️  {msg}")

    def info(self, msg: str):
        self.infos.append(f"ℹ️  {msg}")

    def print_summary(self, verbose: bool = True):
        for line in self.errors:
            print(line)
        for line in self.warnings:
            if verbose:
                print(line)
        for line in self.infos:
            if verbose:
                print(line)
        if self.ok and verbose:
            print(f"✅ 検証OK（警告 {len(self.warnings)} 件）")

    def raise_if_error(self):
        if not self.ok:
            msgs = "\n".join(self.errors)
            raise ValueError(f"データ検証エラー:\n{msgs}")


# ────────────────────────────────────────────────────
# ファイル存在チェック
# ────────────────────────────────────────────────────

def check_files(data_dir: str, result: Optional[ValidationResult] = None) -> ValidationResult:
    """フォルダ構成・ファイル存在確認"""
    from .data_loader import inspect_data_dir
    if result is None:
        result = ValidationResult()

    data_path = Path(data_dir)
    if not data_path.exists():
        result.error(
            f"データディレクトリが存在しません: {data_dir}\n"
            f"   → python generate_html.py --setup  で自動作成できます。"
        )
        return result

    optional_keys = {"profit_data", "profit_target", "outpatient_data", "los_data"}
    labels = {
        "patient_data":   "入院データ",
        "patient_target": "在院・新入院目標",
        "op_data":        "手術データ",
        "op_target":      "手術目標",
        "profit_data":    "粗利データ（省略可）",
        "profit_target":  "粗利目標（省略可）",
        "outpatient_data": "外来件数データ（省略可・隣リポ集計を既定参照）",
        "los_data":       "期間III超え患者数フィード（省略可・回転3指標③・未配置は在院日数近似で代替）",
    }
    # 任意フォルダが無い/空のときの案内文（キー別）。既定は従来どおり粗利レポート向け。
    optional_skip_msg = {"los_data": "回転3指標③は在院日数近似で代替表示します"}

    info = inspect_data_dir(data_dir)
    for key, entry in info.items():
        label  = labels.get(key, key)
        folder = entry["path"]
        files  = entry["files"]

        skip_msg = optional_skip_msg.get(key, "粗利レポートをスキップします")
        if not entry["exists"]:
            if key in optional_keys:
                result.warn(f"{folder.name}/ フォルダなし → {skip_msg}")
            else:
                result.error(
                    f"必須フォルダが見つかりません: {folder}\n"
                    f"   → python generate_html.py --setup  で作成できます"
                )
        elif not files:
            if key in optional_keys:
                result.warn(f"{folder.name}/ が空 → {skip_msg}")
            else:
                result.error(f"必須フォルダが空です: {folder} （{label}を配置してください）")
        else:
            total_kb = sum(f.stat().st_size for f in files) / 1024
            result.info(
                f"  {folder.name}/  {len(files)} ファイル"
                f"  ({', '.join(f.name for f in files[:3])}"
                f"{'...' if len(files) > 3 else ''})  計 {total_kb:.0f} KB"
            )

    return result


# ────────────────────────────────────────────────────
# 入院データ検証
# ────────────────────────────────────────────────────

def check_admission(adm: pd.DataFrame,
                     result: Optional[ValidationResult] = None) -> ValidationResult:
    """前処理済み入院DataFrameの整合性チェック"""
    if result is None:
        result = ValidationResult()

    required_cols = [
        "日付", "病棟コード", "診療科名",
        "在院患者数", "入院患者数", "緊急入院患者数",
        "転入患者数", "退院患者数", "転出患者数",
        "新入院患者数", "病棟名", "科_表示", "病棟_表示",
    ]
    missing = [c for c in required_cols if c not in adm.columns]
    if missing:
        result.error(f"入院データに必須列がありません: {missing}")
        return result

    # 日付範囲
    min_d = adm["日付"].min()
    max_d = adm["日付"].max()
    result.info(f"入院データ: {len(adm):,} 行 / 日付範囲 {min_d.date()} ～ {max_d.date()}")

    # 基準日が古すぎないか（30日以上前で警告）
    if (datetime.now() - max_d).days > 30:
        result.warn(f"入院データの最新日付が {(datetime.now()-max_d).days} 日前です: {max_d.date()}")

    # 在院患者数が全ゼロでないか
    if adm["在院患者数"].sum() == 0:
        result.error("在院患者数が全て0です。データの読込・列名を確認してください。")

    # 負の値チェック
    num_cols = ["在院患者数", "入院患者数", "緊急入院患者数", "退院患者数"]
    for col in num_cols:
        neg = (adm[col] < 0).sum()
        if neg > 0:
            result.warn(f"{col} に負の値が {neg} 件あります。")

    # 診療科名の確認
    visible_depts = adm[adm["科_表示"]]["診療科名"].dropna().unique()
    result.info(f"表示診療科: {len(visible_depts)} 科 — {', '.join(sorted(visible_depts)[:8])}...")

    check_bed_balance(adm, result)

    return result


# ────────────────────────────────────────────────────
# 病床収支恒等式チェック
# ────────────────────────────────────────────────────

# 不一致率がこの値未満なら「散発的」（info）、以上なら「系統的な崩れの疑い」（warn）とみなす閾値。
# 日跨ぎの計上タイミング差（患者異動が前日/当日どちらに計上されるかの境界差）により、
# 数%程度の散発的な不一致は常態で生じる（実データ988日で29日=2.9%、大半は連続2日で符号が
# 逆転するペア）。一方、エクスポート元の定義変更（例: 緊急入院が入院の内数化）による崩れは
# 毎日ちょうど不足するため不一致率がほぼ100%になる。このため件数ではなく率で切り分ける。
BED_BALANCE_SYSTEMATIC_RATE = 0.10


def check_bed_balance(adm: pd.DataFrame,
                       result: Optional[ValidationResult] = None) -> ValidationResult:
    """病床収支恒等式（在院増減 == 出入りの差）の整合性チェック

    日付ごとに病院全体で合計したうえで
        在院(t) − 在院(t−1) == 新入院患者数(t) + 転入患者数(t) − 退院合計(t) − 転出患者数(t)
    が成り立つかを確認する。不一致率が BED_BALANCE_SYSTEMATIC_RATE 未満なら
    日跨ぎの計上タイミング差による散発的な不一致とみなし info、以上なら
    エクスポート元の定義変更などによる系統的な崩れの疑いとして warn する。
    ビルドは止めない（不一致は warn 止まりでエラーにはしない）。
    """
    if result is None:
        result = ValidationResult()

    required_cols = ["日付", "在院患者数", "新入院患者数", "転入患者数", "退院合計", "転出患者数"]
    missing = [c for c in required_cols if c not in adm.columns]
    if missing:
        result.warn(f"病床収支恒等式チェックに必須列がありません: {missing}")
        return result

    daily = (adm.groupby("日付")[required_cols[1:]]
             .sum()
             .sort_index())

    if len(daily) <= 1:
        result.info("病床収支恒等式チェック: 比較可能な日数がないためスキップします。")
        return result

    census_diff = daily["在院患者数"].diff()
    expected_diff = (daily["新入院患者数"] + daily["転入患者数"]
                      - daily["退院合計"] - daily["転出患者数"])
    # 初日は前日が無いため対象外（diff() の NaN を dropna で除外）
    gap = (census_diff - expected_diff).dropna()

    mismatch = gap[gap != 0]
    total = len(gap)
    if len(mismatch) == 0:
        result.info(f"病床収支恒等式: {total}/{total} 日一致")
    else:
        mismatch_rate = len(mismatch) / total
        worst_date = mismatch.abs().idxmax()
        worst_val = mismatch.loc[worst_date]
        if mismatch_rate < BED_BALANCE_SYSTEMATIC_RATE:
            result.info(
                f"病床収支恒等式: 散発的な不一致 {len(mismatch)}/{total} 日"
                f"（{mismatch_rate:.1%}）、"
                f"最大乖離 {abs(int(round(worst_val))):,} 人（{worst_date.date()}）。"
                "日跨ぎの計上タイミング差とみられます。"
            )
        else:
            mean_gap = mismatch.abs().mean()
            result.warn(
                f"病床収支恒等式: 系統的な崩れの疑い、不一致 {len(mismatch)}/{total} 日"
                f"（{mismatch_rate:.1%}）、"
                f"最大乖離 {abs(int(round(worst_val))):,} 人（{worst_date.date()}）、"
                f"平均乖離 {mean_gap:.1f} 人。"
                "エクスポート元の列定義変更を確認してください。"
            )

    return result


# ────────────────────────────────────────────────────
# 手術データ検証
# ────────────────────────────────────────────────────

def check_surgery(surg: pd.DataFrame,
                   result: Optional[ValidationResult] = None) -> ValidationResult:
    if result is None:
        result = ValidationResult()

    if len(surg) == 0:
        result.error("手術データが空です。")
        return result

    required_cols = ["手術実施日", "実施診療科", "手術室", "全麻", "稼働対象室", "平日"]
    missing = [c for c in required_cols if c not in surg.columns]
    if missing:
        result.error(f"手術データに必須列がありません: {missing}")

    min_d = surg["手術実施日"].min()
    max_d = surg["手術実施日"].max()
    ga_cnt = surg["全麻"].sum()
    result.info(f"手術データ: {len(surg):,} 件 / 日付範囲 {min_d.date()} ～ {max_d.date()}")
    result.info(f"  全麻件数: {ga_cnt:,} 件 / 稼働対象室: {surg['稼働対象室'].sum():,} 件")

    if (datetime.now() - max_d).days > 30:
        result.warn(f"手術データの最新日付が {(datetime.now()-max_d).days} 日前です: {max_d.date()}")

    if ga_cnt == 0:
        result.warn("全身麻酔件数が0件です。麻酔種別列または全麻キーワードを確認してください。")

    return result


# ────────────────────────────────────────────────────
# 目標値検証
# ────────────────────────────────────────────────────

def check_targets(targets: dict, surg_targets: dict,
                   result: Optional[ValidationResult] = None) -> ValidationResult:
    if result is None:
        result = ValidationResult()

    # 在院目標
    hosp_inp = targets.get("inpatient", {}).get("hospital", {})
    if not hosp_inp:
        result.error("病院全体の在院目標が設定されていません。")
    else:
        result.info(f"在院目標: 平日{hosp_inp.get('平日','—')} / 休日{hosp_inp.get('休日','—')} / 全日{hosp_inp.get('全日','—')}")

    # 新入院目標
    hosp_nadm = targets.get("new_admission", {}).get("hospital", {})
    if not hosp_nadm:
        result.warn("病院全体の新入院目標が設定されていません。デフォルト値385を使用します。")
    else:
        result.info(f"新入院目標: {hosp_nadm.get('全日','—')}人/週")

    # 診療科別目標件数
    dept_inp_cnt  = len(targets.get("inpatient",  {}).get("dept",  {}))
    dept_nadm_cnt = len(targets.get("new_admission", {}).get("dept", {}))
    ward_cnt      = len(targets.get("inpatient",  {}).get("ward",  {}))
    result.info(f"目標設定数: 在院{dept_inp_cnt}科 / 新入院{dept_nadm_cnt}科 / 病棟{ward_cnt}棟")

    # 手術目標
    if not surg_targets:
        result.warn("手術目標が設定されていません。")
    else:
        result.info(f"手術目標設定: {len(surg_targets)} 科")

    return result


# ────────────────────────────────────────────────────
# 粗利データ検証
# ────────────────────────────────────────────────────

def check_profit(profit_data: pd.DataFrame, profit_targets: pd.DataFrame,
                  result: Optional[ValidationResult] = None) -> ValidationResult:
    if result is None:
        result = ValidationResult()

    if len(profit_data) == 0:
        result.warn("粗利データが空です。粗利レポートをスキップします。")
        return result

    min_m = profit_data["月"].min()
    max_m = profit_data["月"].max()
    depts = profit_data["診療科名"].dropna().nunique()
    result.info(f"粗利データ: {len(profit_data):,} 行 / {depts} 科 / {min_m.strftime('%Y-%m')} ～ {max_m.strftime('%Y-%m')}")

    # 目標との科名マッチング確認
    profit_depts  = set(profit_data["診療科名"].dropna().unique())
    target_depts  = set(profit_targets["診療科名"].dropna().unique())
    unmatched = profit_depts - target_depts
    if unmatched:
        result.warn(f"粗利データに目標未設定の科があります: {sorted(unmatched)}")
    no_data = target_depts - profit_depts
    if no_data:
        result.warn(f"粗利目標はあるがデータのない科: {sorted(no_data)}")

    return result


# ────────────────────────────────────────────────────
# 日付整合性チェック
# ────────────────────────────────────────────────────

def check_date_alignment(adm: pd.DataFrame, surg: pd.DataFrame,
                          result: Optional[ValidationResult] = None) -> ValidationResult:
    """入院データと手術データの基準日が揃っているか確認"""
    if result is None:
        result = ValidationResult()

    adm_max  = adm["日付"].max()
    surg_max = surg["手術実施日"].max()
    diff     = abs((adm_max - surg_max).days)

    if diff == 0:
        result.info(f"入院・手術データの最新日付が一致: {adm_max.date()}")
    elif diff <= 3:
        result.warn(f"入院({adm_max.date()})と手術({surg_max.date()})の最新日付が {diff} 日ずれています。")
    else:
        result.error(f"入院({adm_max.date()})と手術({surg_max.date()})の最新日付が {diff} 日ずれています。"
                     " データを確認してください。")

    return result


# ────────────────────────────────────────────────────
# 一括検証（エントリーポイント）
# ────────────────────────────────────────────────────

def run_all_checks(data_dir: str,
                   adm: pd.DataFrame,
                   surg: pd.DataFrame,
                   targets: dict,
                   surg_targets: dict,
                   profit_data: Optional[pd.DataFrame] = None,
                   profit_targets: Optional[pd.DataFrame] = None,
                   verbose: bool = True) -> ValidationResult:
    """全検証を実行して結果を返す"""
    result = ValidationResult()

    check_admission(adm, result)
    check_surgery(surg, result)
    check_targets(targets, surg_targets, result)
    check_date_alignment(adm, surg, result)

    if profit_data is not None and not (hasattr(profit_data, 'empty') and profit_data.empty) and len(profit_data) > 0:
        _pt = (profit_targets if profit_targets is not None
               else pd.DataFrame(columns=["診療科名"]))
        check_profit(profit_data, _pt, result)

    result.print_summary(verbose=verbose)
    return result
