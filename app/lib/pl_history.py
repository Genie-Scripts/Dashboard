"""
pl_history.py — data/PL.xlsx ローダ

PL.xlsx は月次確報の損益計算書（25か月程度）。
1行目=月（Excelシリアル）, 縦に勘定科目（経常収益→医業収益→…→医業収支）が並ぶ。
本ローダは月×勘定科目の tidy DataFrame に整形して返す。

【データ品質チェック】
  - 給与費と材料費が同値の月（明らかな入力ミス）を WARN として返す
  - 検算: 医業収支 ≒ 医業収益 − (給与費 + 材料費 + 委託費 + 設備関係費 + 経費)
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import pandas as pd

from .config import DEFAULT_DATA_DIR


# 行位置（PL.xlsx の左寄せヘッダ階層に依存）。列はすべて月（5..）。
PL_ROW_MAP = {
    "医業収益":         3,
    "入院診療収益":     4,
    "室料差額収益":     5,
    "外来診療収益":     6,
    "保健予防活動収益": 7,
    "受託検査収益":     8,
    "その他医業収益":   9,
    "保険等査定減":     12,
    "医業外収益":       13,
    "給与費":           16,
    "材料費":           17,
    "医薬品費":         18,
    "診療材料費":       19,
    "医療消耗器具備品費": 20,
    "委託費":           21,
    "設備関係費":       22,
    "経費":             23,
    "医業収支":         24,
    "経常収支":         25,
    "総収支":           26,
}


def _excel_to_ts(serial) -> Optional[pd.Timestamp]:
    try:
        return pd.Timestamp(datetime(1899, 12, 30) + timedelta(days=int(serial)))
    except (TypeError, ValueError):
        return None


def load_pl_history(data_dir: str = DEFAULT_DATA_DIR,
                    filename: str = "PL.xlsx") -> pd.DataFrame:
    """data/PL.xlsx を読み込み、月×勘定科目の tidy DataFrame を返す。

    Returns:
        DataFrame: columns = 月 + PL_ROW_MAP のキー
    """
    path = Path(data_dir) / filename
    if not path.exists():
        raise FileNotFoundError(f"PL ファイルが見つかりません: {path}")

    raw = pd.read_excel(path, sheet_name=0, header=None)

    # 月ヘッダ: 1行目の col5 以降
    serials = raw.iloc[0, 5:]
    months = [_excel_to_ts(s) for s in serials.tolist()]
    valid_idx = [i for i, m in enumerate(months) if m is not None]
    months = [months[i] for i in valid_idx]

    data = {"月": months}
    for label, row in PL_ROW_MAP.items():
        vals = raw.iloc[row, 5:].tolist()
        vals = [vals[i] for i in valid_idx]
        data[label] = pd.to_numeric(pd.Series(vals), errors="coerce").tolist()

    df = pd.DataFrame(data)
    df = df.sort_values("月").reset_index(drop=True)
    return df


def quality_flags(pl: pd.DataFrame) -> pd.DataFrame:
    """各月のデータ品質チェック結果を返す。

    Returns:
        DataFrame: 月, 検算誤差, 給与費=材料費同値, 異常フラグ
    """
    out = pl[["月"]].copy()
    cost_sum = (pl["給与費"] + pl["材料費"] + pl["委託費"]
                + pl["設備関係費"] + pl["経費"])
    out["検算誤差"] = pl["医業収支"] - (pl["医業収益"] - cost_sum)
    out["給与費=材料費"] = (pl["給与費"] - pl["材料費"]).abs() < 1e-3
    out["異常フラグ"] = out["給与費=材料費"] | (out["検算誤差"].abs() > 10000)
    return out


def clean_pl(pl: pd.DataFrame) -> pd.DataFrame:
    """異常月を除外したクリーンな PL DataFrame を返す。"""
    flags = quality_flags(pl)
    bad_months = flags[flags["異常フラグ"]]["月"].tolist()
    return pl[~pl["月"].isin(bad_months)].reset_index(drop=True)
