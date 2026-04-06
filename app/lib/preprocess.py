"""
preprocess.py — 前処理
診療科名合算、除外、病棟名変換、手術室名正規化、全麻フラグ付与
"""

import pandas as pd
import re
from datetime import datetime, time, timedelta
from .config import (
    DEPT_MERGE, DEPT_HIDDEN, WARD_NAMES, WARD_HIDDEN,
    SURGERY_DISPLAY_DEPTS, OR_ROOMS_ACTIVE, GA_KEYWORD,
    ZEN2HAN, OR_START_HOUR, OR_START_MIN, OR_END_HOUR, OR_END_MIN,
    OR_MINUTES_PER_ROOM, OR_ROOM_COUNT, is_operational_day,
)


def preprocess_admission(df: pd.DataFrame) -> pd.DataFrame:
    """入院データ前処理
    
    - 診療科名の合算（感染症・内科 → 総合内科）
    - 非表示科フラグ付与
    - 病棟名変換
    - 新入院患者数カラム追加
    """
    df = df.copy()
    
    # 診療科合算
    df["診療科名_orig"] = df["診療科名"]
    df["診療科名"] = df["診療科名"].map(lambda x: DEPT_MERGE.get(x, x))
    
    # 非表示フラグ
    df["科_表示"] = ~df["診療科名"].isin(DEPT_HIDDEN)
    
    # 病棟名変換
    df["病棟名"] = df["病棟コード"].map(WARD_NAMES).fillna(df["病棟コード"])
    df["病棟_表示"] = ~df["病棟コード"].isin(WARD_HIDDEN)
    
    # 新入院患者数 = 入院 + 緊急入院
    df["新入院患者数"] = df["入院患者数"] + df["緊急入院患者数"]
    # 病棟用新入院（転入含む）: 病棟単位の実態に合わせて転入を加算
    df["新入院患者数_病棟"] = df["新入院患者数"] + df["転入患者数"]
    
    # 退院合計（死亡含む）
    df["退院合計"] = df["退院患者数"] + df["死亡患者数"]

    # 退出合計（転出含む）= 退院合計 + 転出患者数
    df["退出合計"] = df["退院合計"] + df["転出患者数"]

    # 出入り負荷 = 新入院 + 転入 + 退院合計 + 転出
    df["出入り負荷"] = (df["新入院患者数"] + df["転入患者数"]
                       + df["退院合計"] + df["転出患者数"])
    
    # 曜日・平日/休日（祝日・年末年始を含む正確な営業平日判定）
    df["曜日"] = df["日付"].dt.weekday  # 0=月
    df["平日"] = df["日付"].apply(is_operational_day)
    
    # 年度週番号（月曜始まり）
    df["年度"] = df["日付"].apply(
        lambda d: d.year if d.month >= 4 else d.year - 1)
    df["週開始"] = df["日付"] - pd.to_timedelta(df["曜日"], unit="D")
    
    return df


def normalize_or_name(raw: str) -> str:
    """手術室名の正規化: 'ＯＰ−１' → 'OP-1'"""
    if pd.isna(raw):
        return ""
    s = str(raw).translate(ZEN2HAN)
    s = s.replace("OP", "OP").replace("Ｏ", "O").replace("Ｐ", "P")
    # 残りの全角英字を半角に
    s = re.sub(r'[Ａ-Ｚ]', lambda m: chr(ord(m.group()) - 0xFEE0), s)
    # スペース除去
    s = s.replace(" ", "").replace("　", "")
    return s


def _parse_time(val, ref_date=None):
    """時刻文字列をdatetimeに変換"""
    if pd.isna(val):
        return None
    s = str(val).strip()
    try:
        parts = s.split(":")
        h, m = int(parts[0]), int(parts[1])
        if ref_date:
            return datetime.combine(ref_date, time(h, m))
        return time(h, m)
    except:
        return None


def preprocess_surgery(df: pd.DataFrame) -> pd.DataFrame:
    """手術データ前処理
    
    - 手術室名正規化
    - 全身麻酔フラグ
    - 入退室時刻パース
    - 稼働対象フラグ
    - 表示対象科フラグ
    """
    df = df.copy()
    
    # 手術室名正規化
    df["手術室"] = df["実施手術室"].apply(normalize_or_name)
    
    # 全身麻酔フラグ
    df["全麻"] = df["麻酔種別"].fillna("").str.contains(
        re.escape(GA_KEYWORD), na=False)
    
    # 稼働対象手術室フラグ
    df["稼働対象室"] = df["手術室"].isin(OR_ROOMS_ACTIVE)
    
    # 表示対象科フラグ
    df["科_表示"] = df["実施診療科"].isin(SURGERY_DISPLAY_DEPTS)
    
    # 曜日・平日（祝日・年末年始を含む正確な営業平日判定）
    df["曜日"] = df["手術実施日"].dt.weekday
    df["平日"] = df["手術実施日"].apply(is_operational_day)
    
    # 週開始
    df["週開始"] = df["手術実施日"] - pd.to_timedelta(df["曜日"], unit="D")
    
    # 年度
    df["年度"] = df["手術実施日"].apply(
        lambda d: d.year if d.month >= 4 else d.year - 1)
    
    # 入退室時刻と稼働時間（分）の計算
    or_start = time(OR_START_HOUR, OR_START_MIN)
    or_end = time(OR_END_HOUR, OR_END_MIN)
    
    def calc_or_minutes(row):
        """平日の稼働時間帯(8:45-17:15)内の占有分数を計算"""
        if not row["平日"] or not row["稼働対象室"]:
            return 0
        enter = _parse_time(row["入室時刻"])
        leave = _parse_time(row["退室時刻"])
        if enter is None or leave is None:
            return 0
        # 稼働時間帯でクリップ
        eff_start = max(enter, or_start)
        eff_end = min(leave, or_end)
        if leave < enter:  # 翌日にまたがる場合 → 当日17:15まで
            eff_end = or_end
        if eff_start >= eff_end:
            return 0
        delta = datetime.combine(datetime.today(), eff_end) - datetime.combine(datetime.today(), eff_start)
        return max(0, delta.total_seconds() / 60)
    
    df["稼働分"] = df.apply(calc_or_minutes, axis=1)
    
    return df


def build_target_lookup(inpatient_targets: pd.DataFrame) -> dict:
    """統合目標マスタからルックアップ辞書を構築
    
    Returns:
        {
            "inpatient": {
                "hospital": {"全日": 567, "平日": 580, "休日": 540},
                "dept": {"総合内科": 85, ...},
                "ward": {"02A": 47.5, ...},
                "ward_beds": {"02A": 50, ...}
            },
            "new_admission": {
                "hospital": {"全日": 385},
                "dept": {"総合内科": 50.8, ...},
                "ward": {"02A": 32, ...}
            }
        }
    """
    targets = {"inpatient": {}, "new_admission": {}}
    
    # 在院患者数目標
    inp = inpatient_targets[
        inpatient_targets["指標タイプ"] == "日平均在院患者数"]
    
    # 病院全体
    hosp = inp[inp["部門コード"] == "全体"]
    targets["inpatient"]["hospital"] = {
        row["期間区分"]: row["目標値"]
        for _, row in hosp.iterrows()
    }
    
    # 診療科別
    dept = inp[(inp["部門種別"] == "診療科") & (inp["部門コード"] != "全体")]
    targets["inpatient"]["dept"] = {
        row["部門名"]: row["目標値"]
        for _, row in dept.iterrows()
    }
    
    # 病棟別
    ward = inp[(inp["部門種別"] == "病棟") & (inp["部門コード"] != "全体")]
    targets["inpatient"]["ward"] = {
        row["部門コード"]: row["目標値"]
        for _, row in ward.iterrows()
    }
    targets["inpatient"]["ward_beds"] = {
        row["部門コード"]: row["病床数"]
        for _, row in ward.iterrows()
        if pd.notna(row["病床数"])
    }
    
    # 新入院目標
    nadm = inpatient_targets[
        inpatient_targets["指標タイプ"] == "週間新入院患者数"]
    
    hosp_n = nadm[nadm["部門コード"] == "全体"]
    targets["new_admission"]["hospital"] = {
        "全日": hosp_n["目標値"].iloc[0] if len(hosp_n) > 0 else None
    }
    
    dept_n = nadm[(nadm["部門種別"] == "診療科") & (nadm["部門コード"] != "全体")]
    targets["new_admission"]["dept"] = {
        row["部門名"]: row["目標値"]
        for _, row in dept_n.iterrows()
    }
    
    ward_n = nadm[(nadm["部門種別"] == "病棟") & (nadm["部門コード"] != "全体")]
    targets["new_admission"]["ward"] = {
        row["部門コード"]: row["目標値"]
        for _, row in ward_n.iterrows()
    }
    
    return targets


def build_surgery_target_lookup(surgery_targets: pd.DataFrame) -> dict:
    """手術目標マスタからルックアップ辞書を構築
    
    Returns:
        {"整形外科": 27.2, "産婦人科": 12.5, ...}
    """
    return {
        row["実施診療科"]: row["週目標"]
        for _, row in surgery_targets.iterrows()
    }
