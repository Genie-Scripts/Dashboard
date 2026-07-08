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

import sys
import warnings
from pathlib import Path
from typing import Optional
import pandas as pd

from .config import DEFAULT_DATA_DIR, DATA_FOLDERS, DEPT_MERGE


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


def _warn_partial_load(label: str, loaded: list, skipped: list) -> None:
    """一部ファイルのみ読込成功（＝履歴短縮の恐れ）を目立つ形で通知。

    通年ファイルが読めず直近ファイルだけになると、月末見込みの日次系列が
    30日ローリング窓のウォームアップに飲まれ、過去月の見込みライン/塗り分けが
    丸ごと欠落する。黙って進むと気付けないため stderr へも明示する。
    """
    if not skipped:
        return
    msg = (f"⚠ {label}データの一部ファイルを読み込めませんでした。"
           f"履歴が短縮し、月末見込みの過去分が欠落する恐れがあります。\n"
           f"   スキップ: {skipped}\n   読込成功: {loaded}")
    warnings.warn(msg)
    print(msg, file=sys.stderr)


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


def _read_csv_robust(path: Path, skiprows: int = 0) -> pd.DataFrame:
    """CSV を頑健に読む。utf-8-sig → cp932 → cp932(不正バイト置換) の順で試す。

    通年ファイルに混在エンコード等で数バイトの不正バイトが紛れると、
    utf-8-sig も cp932 も UnicodeDecodeError で全体が読めず、呼び出し側の
    per-file try/except が「通年ファイルを丸ごと黙ってスキップ」→ 履歴が直近
    ファイルだけに縮み、月末見込みの日次系列が 30日ウォームアップに飲まれて
    過去月が欠落する事故があった。最終段は encoding_errors='replace' で破損
    バイトを置換してでも行を保全する（孤立した数バイトの破損なら実害なし）。
    """
    last_err: Exception | None = None
    for kwargs in (
        dict(encoding="utf-8-sig"),
        dict(encoding="cp932"),
        dict(encoding="cp932", encoding_errors="replace"),
    ):
        try:
            return pd.read_csv(path, engine="python", on_bad_lines="skip",
                               skiprows=skiprows, **kwargs)
        except (UnicodeDecodeError, UnicodeError) as e:
            last_err = e
    # ここには通常到達しない（最終段は置換で必ず読める）が、保険で送出
    raise last_err  # type: ignore[misc]


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """先頭BOM/置換文字('?')と前後空白を列名から除去。

    Excel等から UTF-8 BOM 付きCSVを cp932 で再保存すると、BOM (U+FEFF)
    が cp932 で表現できず literal '?' に置換されて保存される事例がある。
    その状態で読み込むと先頭列名が '?部門コード' のようになり KeyError を
    引き起こすため、CSV読込直後に正規化する。
    """
    df.columns = [str(c).lstrip("﻿?").strip() for c in df.columns]
    return df


# ────────────────────────────────────────────────────
# 単一ファイル読込（内部用）
# ────────────────────────────────────────────────────

def _read_admission_file(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path, engine="openpyxl")
    else:
        # 文字コードを自動判別（utf-8-sig → cp932 → cp932置換で頑健に）
        def _read_csv_with_enc(skiprows=0):
            return _read_csv_robust(path, skiprows=skiprows)

        df = _read_csv_with_enc()
        df = _normalize_columns(df)
        # 1行目がメタデータ行の場合（ヘッダーに「日付」列がない）は skiprows=1 で再読込
        if "日付" not in df.columns:
            df = _normalize_columns(_read_csv_with_enc(skiprows=1))

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
        df = _read_csv_robust(path)
    df = _normalize_columns(df)
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

    【重複除去の方針（v2: ファイル内の多重行を保持）】
    入院データは看護師が勤務帯ごとに手入力するため、1病棟・同一診療科が
    複数行に分かれて入力されるケースがある（例: 04C病棟。エラー修正時の
    分割入力など）。これは正当なデータで、行を合算して在院数を出す必要がある。

    一方、複数ファイル（例: 通年ファイルと直近ファイル）は日付範囲が重なり、
    同一行がそのまま二重に存在する（＝ファイル間の真の重複）。これは除去したい。

    旧実装は「全列一致＝真の重複」とみなして drop_duplicates(keep='last') して
    いたが、1ファイル内に値まで完全一致する正当な複数行（例: 在院1が2行）が
    あると、それも誤って1行に潰し在院数を過少計上していた
    （直近データで 72日中26日 / −1〜−3人）。

    そこで各ファイル内での同一行の出現順位(_occ)を付けてから結合し、
    (全列 + _occ) で重複除去する。これにより:
      - ファイル内の正当な同一行 … _occ が異なるため両方保持
      - ファイル間の真の重複     … _occ まで一致するため1つに集約
    """
    if len(frames) == 1:
        return frames[0].reset_index(drop=True)

    # 列の和集合で各フレームを揃える（ファイル間で列差があっても安全に結合）
    value_cols = list(pd.concat([f.iloc[:0] for f in frames]).columns)
    parts = []
    for f in frames:
        f = f.reindex(columns=value_cols)
        # ファイル内での同一行の出現順位（NaN もキーに含める）
        f["_occ"] = f.groupby(value_cols, dropna=False).cumcount()
        parts.append(f)

    combined = pd.concat(parts, ignore_index=True)
    # (全列 + _occ) で重複除去 → ファイル間の真の重複のみ集約、ファイル内多重行は保持
    combined = combined.drop_duplicates(subset=value_cols + ["_occ"], keep="last")
    combined = combined.drop(columns="_occ")
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

    frames, loaded, skipped = [], [], []
    for f in files:
        try:
            df = _read_admission_file(f)
            frames.append(df)
            loaded.append(f.name)
        except Exception as e:
            skipped.append(f.name)
            warnings.warn(f"入院ファイル読込スキップ: {f.name} — {e}")

    if not frames:
        raise ValueError(f"{folder} 内に読み込めるファイルがありませんでした。")
    _warn_partial_load("入院", loaded, skipped)

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

    frames, loaded, skipped = [], [], []
    for f in files:
        try:
            df = _read_surgery_file(f)
            frames.append(df)
            loaded.append(f.name)
        except Exception as e:
            skipped.append(f.name)
            warnings.warn(f"手術ファイル読込スキップ: {f.name} — {e}")

    if not frames:
        raise ValueError(f"{folder} 内に読み込めるファイルがありませんでした。")
    _warn_partial_load("手術", loaded, skipped)

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
            try:
                df = pd.read_csv(f, encoding="utf-8-sig")
            except (UnicodeDecodeError, UnicodeError):
                df = pd.read_csv(f, encoding="cp932")
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
            try:
                df = pd.read_csv(f, encoding="utf-8-sig")
            except (UnicodeDecodeError, UnicodeError):
                df = pd.read_csv(f, encoding="cp932")
            df = _normalize_columns(df)
            df["目標値"] = pd.to_numeric(df["目標値"], errors="coerce")
            if "病床数" in df.columns:
                df["病床数"] = pd.to_numeric(df["病床数"], errors="coerce")
            frames.append(df)
        except Exception as e:
            warnings.warn(f"在院目標ファイル読込スキップ: {f.name} — {e}")

    return frames[-1].reset_index(drop=True) if frames else pd.DataFrame()

# 粗利内訳シート名（外来/入院の2系統）
PROFIT_SHEET_GAIRAI = "外来"
PROFIT_SHEET_NYUIN  = "入院"
PROFIT_TARGET_SHEET_GAIRAI = "外来目標"
PROFIT_TARGET_SHEET_NYUIN  = "入院目標"


def _coerce_month_col(series: pd.Series) -> pd.Series:
    """月列を datetime に揃える。Excel シリアル日付（整数）にも対応。"""
    parsed = pd.to_datetime(series, errors="coerce")
    # 1990年未満（=シリアル整数を nanoseconds 解釈してしまった場合）は再変換
    EXCEL_EPOCH = pd.Timestamp("1899-12-30")
    needs_serial = parsed.isna() | (parsed.dt.year < 1990)
    if needs_serial.any():
        numeric = pd.to_numeric(series, errors="coerce")
        serial = EXCEL_EPOCH + pd.to_timedelta(numeric, unit="D")
        parsed = parsed.where(~needs_serial, serial)
    return parsed


def _melt_profit_grid(df: pd.DataFrame) -> pd.DataFrame:
    """粗利グリッド1シート（1列目=診療科名・以降=月列）を縦持ち化"""
    id_col = df.columns[0]
    melted = df.melt(id_vars=[id_col], var_name="月", value_name="粗利")
    melted.columns = ["診療科名", "月", "粗利"]
    melted["月"]   = _coerce_month_col(melted["月"])
    melted["粗利"] = pd.to_numeric(melted["粗利"], errors="coerce")
    melted["診療科名"] = melted["診療科名"].map(lambda x: DEPT_MERGE.get(x, x))
    return melted.dropna(subset=["月"])


def _take_profit_target_cols(df: pd.DataFrame) -> pd.DataFrame:
    """粗利目標1シートから (診療科名, 月次目標) を抽出"""
    result = df.iloc[:, :2].copy()
    result.columns = ["診療科名", "月次目標"]
    result["月次目標"] = pd.to_numeric(result["月次目標"], errors="coerce")
    return result.dropna(subset=["診療科名"])


def load_profit_data(data_dir: str = DEFAULT_DATA_DIR) -> pd.DataFrame:
    """
    profit_data/ フォルダ内の粗利データを読込・縦持ち変換。
    複数xlsxがある場合は最新ファイルを使用。

    シート「外来」「入院」が揃っている場合は両者の合算を返す。
    旧形式（単一シート）の場合は最初のシートを合計として読む。

    Returns:
        DataFrame: 診療科名, 月, 粗利（千円）
    """
    folder = _folder(data_dir, "profit_data")
    _check_folder(folder, "profit_data（粗利）")

    files = _list_files(folder, [".xlsx"])
    if not files:
        raise FileNotFoundError(f"{folder} に .xlsx ファイルがありません。")

    path = files[-1]  # 最新ファイルを使用
    sheets = pd.read_excel(path, engine="openpyxl", sheet_name=None)

    if PROFIT_SHEET_GAIRAI in sheets and PROFIT_SHEET_NYUIN in sheets:
        gairai = _melt_profit_grid(sheets[PROFIT_SHEET_GAIRAI])
        nyuin  = _melt_profit_grid(sheets[PROFIT_SHEET_NYUIN])
        merged = (pd.concat([gairai, nyuin], ignore_index=True)
                    .groupby(["診療科名", "月"], as_index=False)["粗利"].sum())
        return merged.sort_values(["診療科名", "月"]).reset_index(drop=True)

    first_sheet = next(iter(sheets.values()))
    return (_melt_profit_grid(first_sheet)
            .groupby(["診療科名", "月"], as_index=False)["粗利"].sum()
            .sort_values(["診療科名", "月"])
            .reset_index(drop=True))


def load_profit_breakdown(data_dir: str = DEFAULT_DATA_DIR) -> Optional[pd.DataFrame]:
    """
    粗利の外来/入院内訳を取得。内訳シートが揃っていない場合は None を返す。

    Returns:
        DataFrame: 診療科名, 月, 区分(外来/入院), 粗利（千円） または None
    """
    folder = _folder(data_dir, "profit_data")
    if not folder.exists():
        return None
    files = _list_files(folder, [".xlsx"])
    if not files:
        return None

    path = files[-1]
    sheets = pd.read_excel(path, engine="openpyxl", sheet_name=None)
    if PROFIT_SHEET_GAIRAI not in sheets or PROFIT_SHEET_NYUIN not in sheets:
        return None

    gairai = _melt_profit_grid(sheets[PROFIT_SHEET_GAIRAI]).assign(区分="外来")
    nyuin  = _melt_profit_grid(sheets[PROFIT_SHEET_NYUIN]).assign(区分="入院")
    # DEPT_MERGE 適用で同一キーの重複が出る可能性があるため集計
    return (pd.concat([gairai, nyuin], ignore_index=True)
              .groupby(["診療科名", "月", "区分"], as_index=False)["粗利"].sum()
              .sort_values(["診療科名", "月", "区分"])
              .reset_index(drop=True))


def load_profit_targets(data_dir: str = DEFAULT_DATA_DIR) -> pd.DataFrame:
    """
    profit_target/ フォルダ内の粗利目標を読込。
    複数ある場合は最新ファイルを使用。

    シート「外来目標」「入院目標」が揃っている場合は両者の合算を返す。
    旧形式（単一シート）の場合は最初のシートを合計として読む。

    Returns:
        DataFrame: 診療科名, 月次目標（千円）
    """
    folder = _folder(data_dir, "profit_target")
    _check_folder(folder, "profit_target（粗利目標）")

    files = _list_files(folder, [".xlsx"])
    if not files:
        raise FileNotFoundError(f"{folder} に .xlsx ファイルがありません。")

    path = files[-1]
    sheets = pd.read_excel(path, engine="openpyxl", sheet_name=None)

    if PROFIT_TARGET_SHEET_GAIRAI in sheets and PROFIT_TARGET_SHEET_NYUIN in sheets:
        gairai = _take_profit_target_cols(sheets[PROFIT_TARGET_SHEET_GAIRAI])
        nyuin  = _take_profit_target_cols(sheets[PROFIT_TARGET_SHEET_NYUIN])
        merged = (pd.concat([gairai, nyuin], ignore_index=True)
                    .groupby("診療科名", as_index=False)["月次目標"].sum())
        return merged

    first_sheet = next(iter(sheets.values()))
    return _take_profit_target_cols(first_sheet)


def load_profit_targets_breakdown(data_dir: str = DEFAULT_DATA_DIR) -> Optional[pd.DataFrame]:
    """
    粗利目標の外来/入院内訳を取得。内訳シートが揃っていない場合は None を返す。

    Returns:
        DataFrame: 診療科名, 区分(外来/入院), 月次目標（千円） または None
    """
    folder = _folder(data_dir, "profit_target")
    if not folder.exists():
        return None
    files = _list_files(folder, [".xlsx"])
    if not files:
        return None

    path = files[-1]
    sheets = pd.read_excel(path, engine="openpyxl", sheet_name=None)
    if PROFIT_TARGET_SHEET_GAIRAI not in sheets or PROFIT_TARGET_SHEET_NYUIN not in sheets:
        return None

    gairai = _take_profit_target_cols(sheets[PROFIT_TARGET_SHEET_GAIRAI]).assign(区分="外来")
    nyuin  = _take_profit_target_cols(sheets[PROFIT_TARGET_SHEET_NYUIN]).assign(区分="入院")
    return (pd.concat([gairai, nyuin], ignore_index=True)
              .sort_values(["診療科名", "区分"])
              .reset_index(drop=True))


# ────────────────────────────────────────────────────
# 一括読込
# ────────────────────────────────────────────────────

def load_all(data_dir: str = DEFAULT_DATA_DIR) -> dict:
    """
    全データを一括読込。

    Returns:
        dict with keys:
            admission                 — 入院データ（複数ファイルマージ済み）
            surgery                   — 手術データ（複数ファイルマージ済み）
            surgery_targets           — 手術目標
            inpatient_targets         — 在院・新入院目標
            profit_data               — 粗利データ（外来＋入院合算）
            profit_targets            — 粗利目標（外来＋入院合算）
            profit_breakdown          — 粗利の外来/入院内訳（None 可）
            profit_targets_breakdown  — 粗利目標の外来/入院内訳（None 可）
    """
    return {
        "admission":                load_admission_data(data_dir),
        "surgery":                  load_surgery_data(data_dir),
        "surgery_targets":          load_surgery_targets(data_dir),
        "inpatient_targets":        load_inpatient_targets(data_dir),
        "profit_data":              load_profit_data(data_dir),
        "profit_targets":           load_profit_targets(data_dir),
        "profit_breakdown":         load_profit_breakdown(data_dir),
        "profit_targets_breakdown": load_profit_targets_breakdown(data_dir),
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
