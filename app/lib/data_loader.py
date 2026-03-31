"""
data_loader.py — データ読込（フォルダ単位・複数ファイル自動マージ）
======================================================================

【フォルダ構成】
    data/
      patient_data/      ← 入院データ（xlsx / csv）を複数置いてOK
      patient_target/    ← 在院・新入院目標（csv）
      op_data/           ← 手術データ（csv）を複数置いてOK
      op_target/         ← 手術目標（csv）
      profit_data/       ← 粗利データ（xlsx）
      profit_target/     ← 粗利目標（xlsx）

【マージ動作】
    同一フォルダ内のファイルは全て読み込んで結合します。
    入院・手術データで同一日付のレコードが複数ある場合、
    ファイルの更新日時が「新しいファイルのデータ」を優先します。
    （例: base_data.xlsx の 2026-01 と add_data.xlsx の 2026-01 が
          重複する場合、add_data.xlsx 側を採用）
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Optional
import pandas as pd

from .config import DEFAULT_DATA_DIR, DATA_FOLDERS


# ────────────────────────────────────────────────────
# フォルダ解決ユーティリティ
# ────────────────────────────────────────────────────

def _folder(data_dir: str, key: str) -> Path:
    return Path(data_dir) / DATA_FOLDERS[key]


def _list_files(folder: Path, extensions: list) -> list:
    """フォルダ内の指定拡張子ファイルを更新日時昇順（古い順）で返す"""
    files = []
    for ext in extensions:
        files.extend(folder.glob(f"*{ext}"))
        files.extend(folder.glob(f"*{ext.upper()}"))
    seen, unique = set(), []
    for f in sorted(files, key=lambda p: p.stat().st_mtime):
        if f not in seen:
            seen.add(f)
            unique.append(f)
    return unique


def _check_folder(folder: Path, label: str) -> None:
    if not folder.exists():
        raise FileNotFoundError(
            f"データフォルダが見つかりません: {folder}\n"
            f"  → '{label}' フォルダを作成してデータを配置してください。\n"
            f"  → python generate_html.py --setup  で自動作成できます。"
        )
    if not any(folder.iterdir()):
        raise FileNotFoundError(
            f"データフォルダが空です: {folder}\n"
            f"  → {label} データファイルを配置してください。"
        )


# ────────────────────────────────────────────────────
# 単一ファイル読込（内部用）
# ────────────────────────────────────────────────────

def _read_admission_file(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path, engine="openpyxl")
    else:
        df = pd.read_csv(path, encoding="utf-8-sig", engine="python",
                          on_bad_lines="skip")
    df["日付"] = pd.to_datetime(df["日付"], errors="coerce")
    df = df.dropna(subset=["日付"])
    num_cols = ["在院患者数", "入院患者数", "緊急入院患者数",
                "転入患者数", "退院患者数", "転出患者数", "死亡患者数"]
    for c in num_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)
    return df


def _normalize_time_str(series: pd.Series) -> pd.Series:
    """
    入室時刻・退室時刻の文字列を「HH:MM」形式に統一する。
    ファイルによって '9:02' と '09:02' の書式揺れがあり、
    そのままマージキーに使うと重複除去が機能しなくなるため正規化する。
    """
    def _fix(t):
        if pd.isna(t):
            return t
        s = str(t).strip()
        parts = s.split(":")
        if len(parts) >= 2 and len(parts[0]) == 1:
            s = "0" + s
        return s
    return series.apply(_fix)


def _read_surgery_file(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path, engine="openpyxl")
    else:
        df = pd.read_csv(path, encoding="cp932", engine="python",
                          on_bad_lines="skip")
    df["手術実施日"] = pd.to_datetime(df["手術実施日"], errors="coerce")
    df = df.dropna(subset=["手術実施日"])
    # 入室・退室時刻の書式をHH:MMに統一（ファイル間の書式揺れを吸収）
    for col in ["入室時刻", "退室時刻"]:
        if col in df.columns:
            df[col] = _normalize_time_str(df[col])
    return df


# ────────────────────────────────────────────────────
# マージ戦略
# ────────────────────────────────────────────────────

def _merge_admission_files(frames: list) -> pd.DataFrame:
    """
    入院データの複数ファイルをマージ。

    【重複除去の方針】
    入院データは1つの病棟に同一診療科が複数行存在するケースがある
    （例: 04C病棟に「救急科」が2行、それぞれ在院患者数が異なる）。
    これは正当なデータ構造であり、(日付・病棟・診療科) をキーにした
    drop_duplicates では正当な行まで削除してしまう。

    そのため、全列が完全に一致する行（=ファイル間の真の重複）のみを除去する。
    値が異なる行は複数ファイル由来であっても両方保持する。
    """
    if len(frames) == 1:
        return frames[0].reset_index(drop=True)

    combined = pd.concat(frames, ignore_index=True)
    # 全列一致の完全重複のみ除去（正当な複数行は保持）
    combined = combined.drop_duplicates(keep="last")
    return combined.sort_values("日付").reset_index(drop=True)


def _merge_surgery_files(frames: list) -> pd.DataFrame:
    """
    手術データの複数ファイルをマージ。
    同一（手術実施日・診療科・手術室・入室時刻）の重複は後ファイル優先。

    【注意】 旧キーに「実施術者」を含めていたが、術者列に改行文字が混入している
    データでは全術者が同一文字列とみなされ、同科・同手術室の手術が
    誤って重複削除されるバグがあった。入室時刻は手術ごとに一意性が高く
    より安全なキーとして採用。
    """
    if len(frames) == 1:
        return frames[0].reset_index(drop=True)

    combined = pd.concat(frames, ignore_index=True)
    key_cols = [c for c in ["手術実施日", "実施診療科", "実施手術室", "入室時刻"]
                if c in combined.columns]
    if key_cols:
        combined = combined.drop_duplicates(subset=key_cols, keep="last")
    return combined.sort_values("手術実施日").reset_index(drop=True)


# ────────────────────────────────────────────────────
# 公開 load 関数
# ────────────────────────────────────────────────────

def load_admission_data(data_dir: str = DEFAULT_DATA_DIR) -> pd.DataFrame:
    """
    patient_data/ フォルダ内の全入院データファイルを読み込んでマージ。

    対応形式: .xlsx / .csv（UTF-8 BOM）
    マージ: 同一(日付・病棟・科)の重複は新しいファイルのデータ優先

    Returns:
        DataFrame: 日付, 病棟コード, 診療科名, 在院患者数,
                   入院患者数, 緊急入院患者数, 転入患者数,
                   退院患者数, 転出患者数, 死亡患者数
    """
    folder = _folder(data_dir, "patient_data")
    _check_folder(folder, "patient_data（入院）")

    files = _list_files(folder, [".xlsx", ".csv"])
    if not files:
        raise FileNotFoundError(f"{folder} に .xlsx / .csv ファイルがありません。")

    frames, loaded = [], []
    for f in files:
        try:
            df = _read_admission_file(f)
            frames.append(df)
            loaded.append(f.name)
        except Exception as e:
            warnings.warn(f"入院ファイル読込スキップ: {f.name} — {e}")

    if not frames:
        raise ValueError(f"{folder} 内に読み込めるファイルがありませんでした。")

    merged = _merge_admission_files(frames)
    # 読込サマリーをデータフレームの属性として付与（validate.py で利用）
    merged.attrs["source_files"] = loaded
    return merged


def load_surgery_data(data_dir: str = DEFAULT_DATA_DIR) -> pd.DataFrame:
    """
    op_data/ フォルダ内の全手術データファイルを読み込んでマージ。

    対応形式: .csv (CP932) / .xlsx
    マージ: 同一(手術実施日・診療科・手術室・入室時刻)の重複は新しいファイル優先

    Returns:
        DataFrame: 手術実施日, 実施診療科, 実施手術室, 麻酔科関与,
                   入外区分, 申込区分, 実施術者, 麻酔種別,
                   入室時刻, 退室時刻, 予定手術時間, 予定手術時間(OR)
    """
    folder = _folder(data_dir, "op_data")
    _check_folder(folder, "op_data（手術）")

    files = _list_files(folder, [".csv", ".xlsx"])
    if not files:
        raise FileNotFoundError(f"{folder} に .csv / .xlsx ファイルがありません。")

    frames, loaded = [], []
    for f in files:
        try:
            df = _read_surgery_file(f)
            frames.append(df)
            loaded.append(f.name)
        except Exception as e:
            warnings.warn(f"手術ファイル読込スキップ: {f.name} — {e}")

    if not frames:
        raise ValueError(f"{folder} 内に読み込めるファイルがありませんでした。")

    merged = _merge_surgery_files(frames)
    merged.attrs["source_files"] = loaded
    return merged


def load_surgery_targets(data_dir: str = DEFAULT_DATA_DIR) -> pd.DataFrame:
    """
    op_target/ フォルダ内の手術目標CSVを読込。
    複数ある場合は最新ファイルを使用。

    Returns:
        DataFrame: 実施診療科, 週目標
    """
    folder = _folder(data_dir, "op_target")
    _check_folder(folder, "op_target（手術目標）")

    files = _list_files(folder, [".csv"])
    if not files:
        raise FileNotFoundError(f"{folder} に .csv ファイルがありません。")

    frames = []
    for f in files:
        try:
            df = pd.read_csv(f, encoding="utf-8-sig")
            df.columns = ["実施診療科", "週目標"]
            df["週目標"] = pd.to_numeric(df["週目標"], errors="coerce")
            frames.append(df)
        except Exception as e:
            warnings.warn(f"手術目標ファイル読込スキップ: {f.name} — {e}")

    # 目標は最新ファイルを優先
    return frames[-1].reset_index(drop=True) if frames else pd.DataFrame(
        columns=["実施診療科", "週目標"])


def load_inpatient_targets(data_dir: str = DEFAULT_DATA_DIR) -> pd.DataFrame:
    """
    patient_target/ フォルダ内の在院目標CSVを読込。
    複数ある場合は最新ファイルを使用。

    Returns:
        DataFrame: 部門コード, 部門名, 部門種別, 指標タイプ,
                   期間区分, 単位, 目標値, 病床数
    """
    folder = _folder(data_dir, "patient_target")
    _check_folder(folder, "patient_target（在院目標）")

    files = _list_files(folder, [".csv"])
    if not files:
        raise FileNotFoundError(f"{folder} に .csv ファイルがありません。")

    frames = []
    for f in files:
        try:
            df = pd.read_csv(f, encoding="utf-8-sig")
            df["目標値"] = pd.to_numeric(df["目標値"], errors="coerce")
            if "病床数" in df.columns:
                df["病床数"] = pd.to_numeric(df["病床数"], errors="coerce")
            frames.append(df)
        except Exception as e:
            warnings.warn(f"在院目標ファイル読込スキップ: {f.name} — {e}")

    return frames[-1].reset_index(drop=True) if frames else pd.DataFrame()


def load_profit_data(data_dir: str = DEFAULT_DATA_DIR) -> pd.DataFrame:
    """
    profit_data/ フォルダ内の粗利データを読込・縦持ち変換。
    複数xlsxがある場合は最新ファイルを使用。

    Returns:
        DataFrame: 診療科名, 月, 粗利（千円）
    """
    folder = _folder(data_dir, "profit_data")
    _check_folder(folder, "profit_data（粗利）")

    files = _list_files(folder, [".xlsx"])
    if not files:
        raise FileNotFoundError(f"{folder} に .xlsx ファイルがありません。")

    path = files[-1]  # 最新ファイルを使用
    df = pd.read_excel(path, engine="openpyxl")
    id_col = df.columns[0]
    melted = df.melt(id_vars=[id_col], var_name="月", value_name="粗利")
    melted.columns = ["診療科名", "月", "粗利"]
    melted["月"]   = pd.to_datetime(melted["月"], errors="coerce")
    melted["粗利"] = pd.to_numeric(melted["粗利"], errors="coerce")
    melted = melted.dropna(subset=["月"])
    return melted.sort_values(["診療科名", "月"]).reset_index(drop=True)


def load_profit_targets(data_dir: str = DEFAULT_DATA_DIR) -> pd.DataFrame:
    """
    profit_target/ フォルダ内の粗利目標を読込。
    複数ある場合は最新ファイルを使用。

    Returns:
        DataFrame: 診療科名, 月次目標（千円）
    """
    folder = _folder(data_dir, "profit_target")
    _check_folder(folder, "profit_target（粗利目標）")

    files = _list_files(folder, [".xlsx"])
    if not files:
        raise FileNotFoundError(f"{folder} に .xlsx ファイルがありません。")

    path = files[-1]
    df = pd.read_excel(path, engine="openpyxl")
    result = df.iloc[:, :2].copy()
    result.columns = ["診療科名", "月次目標"]
    result["月次目標"] = pd.to_numeric(result["月次目標"], errors="coerce")
    return result.dropna(subset=["診療科名"])


# ────────────────────────────────────────────────────
# 一括読込
# ────────────────────────────────────────────────────

def load_all(data_dir: str = DEFAULT_DATA_DIR) -> dict:
    """
    全データを一括読込。

    Returns:
        dict with keys:
            admission         — 入院データ（複数ファイルマージ済み）
            surgery           — 手術データ（複数ファイルマージ済み）
            surgery_targets   — 手術目標
            inpatient_targets — 在院・新入院目標
            profit_data       — 粗利データ
            profit_targets    — 粗利目標
    """
    return {
        "admission":         load_admission_data(data_dir),
        "surgery":           load_surgery_data(data_dir),
        "surgery_targets":   load_surgery_targets(data_dir),
        "inpatient_targets": load_inpatient_targets(data_dir),
        "profit_data":       load_profit_data(data_dir),
        "profit_targets":    load_profit_targets(data_dir),
    }


# ────────────────────────────────────────────────────
# ディレクトリセットアップ補助
# ────────────────────────────────────────────────────

def setup_data_dir(data_dir: str = DEFAULT_DATA_DIR) -> None:
    """
    必要なサブフォルダを全て作成する（初回セットアップ用）。
    python generate_html.py --setup  から呼び出される。
    """
    base = Path(data_dir)
    base.mkdir(exist_ok=True)
    descriptions = {
        "patient_data":   "入院日報 xlsx/csv（複数可・自動マージ）",
        "patient_target": "在院・新入院目標 csv（最新ファイルを使用）",
        "op_data":        "手術データ csv/xlsx（複数可・自動マージ）",
        "op_target":      "手術目標 csv（最新ファイルを使用）",
        "profit_data":    "粗利データ xlsx（最新ファイルを使用）",
        "profit_target":  "粗利目標 xlsx（最新ファイルを使用）",
    }
    for key, folder_name in DATA_FOLDERS.items():
        folder = base / folder_name
        folder.mkdir(exist_ok=True)
        desc = descriptions.get(key, "")
        print(f"  ✅  {folder}/  ← {desc}")

    print(f"\n📁 {base.resolve()} を初期化しました。")
    print("   各フォルダにデータファイルを配置してから")
    print("   python generate_html.py を実行してください。\n")


def inspect_data_dir(data_dir: str = DEFAULT_DATA_DIR) -> dict:
    """
    データディレクトリの内容を確認して辞書で返す（validate.py から呼び出す）。

    Returns:
        {folder_key: {"path": Path, "files": [Path], "exists": bool}}
    """
    result = {}
    ext_map = {
        "patient_data":   [".xlsx", ".csv"],
        "patient_target": [".csv"],
        "op_data":        [".csv", ".xlsx"],
        "op_target":      [".csv"],
        "profit_data":    [".xlsx"],
        "profit_target":  [".xlsx"],
    }
    for key, folder_name in DATA_FOLDERS.items():
        folder = Path(data_dir) / folder_name
        exts   = ext_map.get(key, [".xlsx", ".csv"])
        files  = _list_files(folder, exts) if folder.exists() else []
        result[key] = {"path": folder, "files": files, "exists": folder.exists()}
    return result
