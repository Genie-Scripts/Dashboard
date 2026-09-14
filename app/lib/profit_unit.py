"""
profit_unit.py — 入院粗利の「数量×単価」分解（粗利タブ 新設ブロック用）

2026年8月に「延患者数は7月より+2.8%増えたのに入院粗利は−1.8%減った」という事象が
起きた。分解すると 数量効果(延患者数の増減) と 単価効果(粗利/人日の増減) の合成
だったが、延患者数と粗利が別タブにあり「粗利/人日」という単価の概念がどこにも
無いため、この構造が画面から読めなかった。本モジュールはそれを読めるようにする
ための専用ペイロードを組み立てる（表示は detail.html 側の JS 描画）。

外来は「延べ患者数」の概念が無い（日帰りで在院日数を持たない）ため対象外。
入院のみを扱う。

用語の定義（他モジュールとの混同注意）:
  平均在院日数の近似(alos) = 延患者数 ÷ 退院数。app.lib.metrics.alos_proxy と
  同一定義（月単位の窓で呼び出して流用する）。month_projection._alos_28d
  （在院 ÷ 新入院 = Little's law の近似）とは分子・分母が逆の別物なので流用しない。
"""

from typing import Optional

import pandas as pd

from .metrics import build_daily_series, alos_proxy
from .month_projection import calendar_days_in_month, STD_CAL_DAYS_PER_MONTH

# marginal（限界人日単価）の発散防止ガード。分母（前月比の延患者数差）の絶対値が
# これ未満なら None を返す（僅かな延数の揺れで単価が跳ねるのを防ぐ）。
_MIN_DELTA_PATIENT_DAYS = 50.0

# 見込み(projection)のデッドバンド。前月確報ppd比の変化率がこの範囲内なら "flat" 扱い。
_PROJECTION_DEADBAND_PCT = 1.5
# 月内経過日数がこれ未満なら見込みを出さない（初日〜2日はブレが大きすぎるため）。
_PROJECTION_MIN_ELAPSED_DAYS = 3


def _month_end(month_start: pd.Timestamp) -> pd.Timestamp:
    return month_start + pd.offsets.MonthEnd(0)


def _patient_days_in_month(adm: pd.DataFrame, month_start: pd.Timestamp,
                           group_col: Optional[str] = None,
                           unit: Optional[str] = None) -> float:
    """月内の延患者数（在院患者数の日次合計・人日）。データが無ければ0.0。

    display_filter=False（全科）で集計する。粗利/人日の分子である入院粗利は粗利
    データの全科合計で表示科フィルタが掛からないため、分母だけ表示科に絞ると
    母集団がずれる（2026-08 は健診センター等の3人日が分母から落ち、粗利/人日が
    86,713→86,728円と +15円ずれる）。経営会議へ配布済みの分解資料も全科ベース
    （延 17,847人日・前月差 +483人日）なので、画面と資料の数字を一致させる。
    portal の在院KPI は表示科ベースのままなので、月次の延患者数はこの分だけ
    portal 側と一致しない（差は 0.02% 程度・意図的）。
    """
    month_end = _month_end(month_start)
    s = build_daily_series(adm, "在院患者数", group_col=group_col, group_val=unit,
                           display_filter=False)
    w = s[(s["日付"] >= month_start) & (s["日付"] <= month_end)]
    return float(w["値"].sum())


def _alos_month(adm: pd.DataFrame, month_start: pd.Timestamp,
                group_col: Optional[str] = None,
                unit: Optional[str] = None) -> Optional[float]:
    """月次の平均在院日数の近似。alos_proxy(end_date=当月末, window_days=当月日数) を
    呼ぶことで「当月まるごと」の窓に一致させる（alos_proxy 自体は月境界を知らない
    汎用の window 関数のため、呼び出し側で月の日数を渡して月次集計に流用する）。
    display_filter=False は _patient_days_in_month と同じ理由（粗利/人日の分母と
    母集団を揃える）。
    """
    month_end = _month_end(month_start)
    days = (month_end - month_start).days + 1
    return alos_proxy(adm, month_end, window_days=days, group_col=group_col, unit=unit,
                      display_filter=False)


def _nyuin_profit_map(nyuin_df: pd.DataFrame, dept: Optional[str] = None) -> dict:
    """区分=入院に絞った月次粗利（千円）の {月Timestamp: 粗利} 辞書。dept=Noneは全科合計。"""
    df = nyuin_df if dept is None else nyuin_df[nyuin_df["診療科名"] == dept]
    if len(df) == 0:
        return {}
    g = df.groupby("月")["粗利"].sum()
    return {pd.Timestamp(k): float(v) for k, v in g.items()}


def _inpatient_target_mm(profit_targets_breakdown: Optional[pd.DataFrame],
                         anchor_month: pd.Timestamp, dept: Optional[str] = None) -> Optional[float]:
    """入院粗利目標（百万円）。month_projection.profit_target_for_month の入院側と同じ式
    （月次目標[千円]の合計 × 当月暦日数 / STD_CAL_DAYS_PER_MONTH）を、外来目標を含まず
    区分=入院だけに絞って計算する（式を重複させないよう calendar_days_in_month /
    STD_CAL_DAYS_PER_MONTH は month_projection から import して流用）。

    breakdown が無い・入院行が無い・合計が0以下のいずれかなら None。
    """
    if profit_targets_breakdown is None or len(profit_targets_breakdown) == 0:
        return None
    if "区分" not in profit_targets_breakdown.columns:
        return None
    df = profit_targets_breakdown[profit_targets_breakdown["区分"] == "入院"]
    if dept is not None:
        df = df[df["診療科名"] == dept]
    if len(df) == 0:
        return None
    total_千円 = float(df["月次目標"].fillna(0).sum())
    if total_千円 <= 0:
        return None
    cal_days_total = calendar_days_in_month(anchor_month)
    target_千円 = total_千円 * cal_days_total / STD_CAL_DAYS_PER_MONTH
    return round(target_千円 / 1000.0, 1)


def _marginal_caption(delta_patient_days: Optional[float], delta_profit_mm: Optional[float],
                      marginal_value: Optional[float], avg_ppd_yen: Optional[float]) -> str:
    """最新月の限界人日単価を1文のキャプションにする（テキストは検証対象外の表示専用）。"""
    if marginal_value is None or delta_patient_days is None or delta_profit_mm is None:
        return "延患者数の変化が小さく、1人日あたりの増減は算出していません。"
    days_dir = "増えて" if delta_patient_days >= 0 else "減って"
    profit_dir = "増" if delta_profit_mm >= 0 else "減"
    sign = "+" if marginal_value > 0 else ""
    avg_txt = f"（平均 {avg_ppd_yen / 1000:.1f}千円）" if avg_ppd_yen is not None else ""
    return (f"先月から延患者数が{abs(delta_patient_days):.0f}人日{days_dir}"
            f"粗利は{abs(delta_profit_mm):.1f}百万円{profit_dir}。"
            f"1人日あたり{sign}{marginal_value:.0f}千円{avg_txt}。")


def _build_unit_block(nyuin_df: pd.DataFrame, adm: pd.DataFrame, anchor_month: pd.Timestamp,
                      months: int, group_col: Optional[str] = None,
                      unit: Optional[str] = None,
                      profit_targets_breakdown: Optional[pd.DataFrame] = None) -> Optional[dict]:
    """病院全体 または 診療科1つぶんの ppd/alos/decomp/marginal をまとめて返す。

    入院粗利データが1つも無い場合は None（呼び出し側で非表示に縮退させる）。
    """
    dept = unit if group_col == "診療科名" else None
    profit_map = _nyuin_profit_map(nyuin_df, dept=dept)
    if not profit_map:
        return None

    month_list = [anchor_month - pd.DateOffset(months=i) for i in range(months)][::-1]

    def _ppd(m: pd.Timestamp) -> Optional[float]:
        profit_千円 = profit_map.get(m)
        if profit_千円 is None:
            return None
        pd_days = _patient_days_in_month(adm, m, group_col=group_col, unit=unit)
        if not pd_days:
            return None
        return profit_千円 * 1000.0 / pd_days

    ppd_values = [_ppd(m) for m in month_list]
    if all(v is None for v in ppd_values):
        return None
    ppd_prev_values = [_ppd(m - pd.DateOffset(years=1)) for m in month_list]

    alos_values = [_alos_month(adm, m, group_col=group_col, unit=unit) for m in month_list]
    alos_prev_values = [_alos_month(adm, m - pd.DateOffset(years=1), group_col=group_col, unit=unit)
                        for m in month_list]

    # ── decomp: 直近12か月・各月の前年同月差を 数量効果+単価効果 に分解 ──
    decomp_months = month_list[-12:]
    volume_effect, price_effect, actual_delta = [], [], []
    for m in decomp_months:
        pm = m - pd.DateOffset(years=1)
        cur_profit = profit_map.get(m)
        prev_profit = profit_map.get(pm)
        cur_pd = _patient_days_in_month(adm, m, group_col=group_col, unit=unit)
        prev_pd = _patient_days_in_month(adm, pm, group_col=group_col, unit=unit)
        if cur_profit is None or prev_profit is None or not cur_pd or not prev_pd:
            volume_effect.append(None)
            price_effect.append(None)
            actual_delta.append(None)
            continue
        cur_ppd = cur_profit * 1000.0 / cur_pd
        prev_ppd = prev_profit * 1000.0 / prev_pd
        vol_mm = (cur_pd - prev_pd) * prev_ppd / 1_000_000.0
        price_mm = (cur_ppd - prev_ppd) * cur_pd / 1_000_000.0
        actual_mm = (cur_profit - prev_profit) / 1000.0
        volume_effect.append(round(vol_mm, 1))
        price_effect.append(round(price_mm, 1))
        actual_delta.append(round(actual_mm, 1))

    # ── marginal: 最新月 vs 前月の限界人日単価（千円/人日） ──
    latest_m = month_list[-1]
    prev_m = latest_m - pd.DateOffset(months=1)
    cur_profit = profit_map.get(latest_m)
    prev_profit = profit_map.get(prev_m)
    cur_pd = (_patient_days_in_month(adm, latest_m, group_col=group_col, unit=unit)
             if cur_profit is not None else None)
    prev_pd = (_patient_days_in_month(adm, prev_m, group_col=group_col, unit=unit)
              if prev_profit is not None else None)

    marginal_value = None
    delta_profit_mm = None
    delta_patient_days = None
    if cur_profit is not None and prev_profit is not None and cur_pd and prev_pd:
        delta_profit_mm = round((cur_profit - prev_profit) / 1000.0, 1)
        delta_patient_days = round(cur_pd - prev_pd, 1)
        if abs(delta_patient_days) >= _MIN_DELTA_PATIENT_DAYS:
            marginal_value = round((cur_profit - prev_profit) / (cur_pd - prev_pd), 1)

    caption = _marginal_caption(delta_patient_days, delta_profit_mm, marginal_value,
                                ppd_values[-1])

    # ── latest: 最新月（アンカー月）の確報4値＋前年同月＋前年同月比＋目標達成率 ──
    # cur_profit/cur_pd は直上の marginal ブロックで算出済みの latest_m 分をそのまま流用
    # （profit_map.get(latest_m) と _patient_days_in_month(adm, latest_m, ...) の再計算を避ける）。
    latest_profit_千円 = cur_profit
    latest_pd = cur_pd
    latest_ppd = ppd_values[-1]
    latest_alos = alos_values[-1]

    py_m = latest_m - pd.DateOffset(years=1)
    py_profit_千円 = profit_map.get(py_m)
    prev_year = None
    yoy_pct = {"profit": None, "patient_days": None, "ppd": None}
    if py_profit_千円 is not None:
        py_pd = _patient_days_in_month(adm, py_m, group_col=group_col, unit=unit)
        py_ppd = ppd_prev_values[-1]
        py_alos = alos_prev_values[-1]
        prev_year = {
            "profit_mm": round(py_profit_千円 / 1000.0, 1),
            "patient_days": int(round(py_pd)) if py_pd else None,
            "ppd": round(py_ppd) if py_ppd is not None else None,
            "alos": round(py_alos, 1) if py_alos is not None else None,
        }
        if latest_profit_千円 is not None and py_profit_千円:
            yoy_pct["profit"] = round(
                (latest_profit_千円 - py_profit_千円) / py_profit_千円 * 100, 1)
        if latest_pd is not None and py_pd:
            yoy_pct["patient_days"] = round((latest_pd - py_pd) / py_pd * 100, 1)
        if latest_ppd is not None and py_ppd:
            yoy_pct["ppd"] = round((latest_ppd - py_ppd) / py_ppd * 100, 1)

    target_mm = _inpatient_target_mm(profit_targets_breakdown, latest_m, dept=dept)
    latest_profit_mm = round(latest_profit_千円 / 1000.0, 1) if latest_profit_千円 is not None else None
    achievement_pct = (round(latest_profit_mm / target_mm * 100, 1)
                       if latest_profit_mm is not None and target_mm else None)

    latest_block = {
        "month": latest_m.strftime("%Y-%m"),
        "profit_mm": latest_profit_mm,
        "patient_days": int(round(latest_pd)) if latest_pd else None,
        "ppd": round(latest_ppd) if latest_ppd is not None else None,
        "alos": round(latest_alos, 1) if latest_alos is not None else None,
        "prev_year": prev_year,
        "yoy_pct": yoy_pct,
        "target_mm": target_mm,
        "achievement_pct": achievement_pct,
    }

    fmt = lambda m: m.strftime("%Y-%m")  # noqa: E731
    return {
        "ppd": {
            "months": [fmt(m) for m in month_list],
            "values": [round(v) if v is not None else None for v in ppd_values],
            "prev_values": [round(v) if v is not None else None for v in ppd_prev_values],
        },
        "alos": {
            "months": [fmt(m) for m in month_list],
            "values": [round(v, 1) if v is not None else None for v in alos_values],
            "prev_values": [round(v, 1) if v is not None else None for v in alos_prev_values],
        },
        "decomp": {
            "months": [fmt(m) for m in decomp_months],
            "volume_effect": volume_effect,
            "price_effect": price_effect,
            "actual_delta": actual_delta,
        },
        "marginal": {
            "month": fmt(latest_m),
            "prev_month": fmt(prev_m),
            "value": marginal_value,
            "delta_profit_mm": delta_profit_mm,
            "delta_patient_days": delta_patient_days,
            "caption": caption,
        },
        "latest": latest_block,
    }


def _projected_patient_days_this_month(adm: pd.DataFrame, base_date: pd.Timestamp) -> float:
    """当月・病院全体の月末見込み延患者数（人日・合計。平均ではない）。

    month_projection.build_month_projection_payload の「在院 日平均」ブロック
    （month_projection.py:157-168 付近）と全く同じ式（MTD人日の合計 ＋ 残暦日 ×
    直近30日の在院日平均）。month_projection 側は cal_days_total で割った「平均」
    しか公開していないため、ここでは割る前の「合計」を自前で計算する。式が
    乖離しないよう変数名も month_projection 側に合わせている。
    ★MTD人日の合計だけを分母にしてはいけない（月初ほど極端に小さく、粗利/人日が
      破綻値になる）。必ず残暦日ぶんの見込みを加算した「月末時点の合計」を使う。
    """
    base_date = pd.Timestamp(base_date).normalize()
    month_start = base_date.replace(day=1)
    cal_days_total = calendar_days_in_month(month_start)
    cal_days_elapsed = (base_date - month_start).days + 1
    cal_days_remaining = max(0, cal_days_total - cal_days_elapsed)
    win30_start = base_date - pd.Timedelta(days=29)

    inp_mtd_daily = (adm[(adm["日付"] >= month_start) & (adm["日付"] <= base_date)]
                     .groupby("日付")["在院患者数"].sum())
    inp_mtd_sum = float(inp_mtd_daily.sum())

    inp_win30_daily = (adm[(adm["日付"] >= win30_start) & (adm["日付"] <= base_date)]
                       .groupby("日付")["在院患者数"].sum())
    inp_pace = float(inp_win30_daily.mean()) if len(inp_win30_daily) > 0 else 0.0
    inp_remaining_sum = inp_pace * cal_days_remaining
    return inp_mtd_sum + inp_remaining_sum


def _series_value_at(series: Optional[dict], key: str, date_str: str) -> Optional[float]:
    """日次系列 series[key] から date_str 以下の直近非None値を返す。

    html_builder._series_value_at と同じ規則（dates は昇順前提・"月末見込み"を
    min(base_date, 月末) 日の値として取り出す）を入院のみの系列にも使えるよう
    key 引数を汎用化した写し。html_builder → profit_unit の import は循環になる
    ため式だけこちらに複製する（元は html_builder.py:82-98）。
    """
    if not series:
        return None
    dates = series.get("dates") or []
    vals = series.get(key) or []
    if not dates or len(vals) != len(dates):
        return None
    best = None
    for d, v in zip(dates, vals):
        if d <= date_str and v is not None:
            best = v
    return best


def _build_prev_month_pending(hospital_series: Optional[dict], adm: pd.DataFrame,
                              anchor_month: pd.Timestamp, prev_month: pd.Timestamp,
                              base_date) -> Optional[dict]:
    """Phase4 S2（確報遅れ: anchor_month(A) < prev_month(P)）用。まだ確報が入っていない
    P月の「見込み（確報待ち・暫定）」を、新規推計を作らずに hospital_series（recency
    補正済み values_final_nyuin）の P月末日の値 ÷ P月の実績延患者数（両方とも確定済み
    実績データ由来）で組み立てる。

    _projected_patient_days_this_month のような「残暦日ぶんの見込みを足す」処理は
    行わない（P月は base_date 時点で既に暦月が終わっているため月末までの実績が
    全部揃っている＝在院日数は見込みではなく確定値）。

    値が取れない（hospital_series が無い/系列が短い等）場合は None（静かに縮退）。
    """
    try:
        p_month_end = _month_end(prev_month)
        base_date = pd.Timestamp(base_date).normalize()
        date_m = min(base_date, p_month_end)
        val = _series_value_at(hospital_series, "values_final_nyuin", date_m.strftime("%Y-%m-%d"))
        if val is None:
            return None
        profit_mm = round(float(val), 1)

        patient_days_raw = _patient_days_in_month(adm, prev_month)
        if not patient_days_raw:
            return None
        patient_days = int(round(patient_days_raw))
        ppd = round(profit_mm * 1_000_000.0 / patient_days_raw)

        return {
            "month": prev_month.strftime("%Y-%m"),
            "confirmed_month": anchor_month.strftime("%Y-%m"),
            "profit_mm": profit_mm,
            "patient_days": patient_days,
            "ppd": ppd,
        }
    except Exception:
        return None


def _build_projection(adm: pd.DataFrame, base_date, profit_hybrid_meta: Optional[dict],
                      latest_ppd: Optional[float],
                      profit_targets_breakdown: Optional[pd.DataFrame] = None,
                      anchor_month: Optional[pd.Timestamp] = None) -> Optional[dict]:
    """病院全体・当月（進行中）の粗利/人日 見込み。global にのみ付与する（科別は
    latest_mtdblend_nyuin を科別で持たず比例フォールバックになり嘘をつくため作らない）。

    月内経過日数が _PROJECTION_MIN_ELAPSED_DAYS 未満、hybrid meta から入院のみ月末
    見込み（百万円）が取れない、または anchor_month（入院粗利の最新確報月）が当月
    そのものと一致する（Phase4 S3: 見込み不可・確報が既に当月ぶんまで入っている
    という通常はあり得ない状態）場合は None。例外は投げない。

    分子は recency 補正済みの latest_final_nyuin（blend_and_calibrate_series。
    profit_estimate.py:1602）を優先し、無ければ未補正の latest_mtdblend_nyuin
    （profit_estimate.py:1273）へフォールバックする（後方互換）。

    target_mm/achievement_pct（Phase3: 見込みタイルの目標比バッジ用）は latest.target_mm
    （確報月＝前月の目標）を流用せず、_inpatient_target_mm を「当月（進行中月）」を
    anchor に呼び直して算出する（月が違えば暦日数が違い目標も変わるため）。
    profit_targets_breakdown が無ければ従来どおり None で静かに縮退する。
    """
    try:
        if not profit_hybrid_meta:
            return None
        base_date = pd.Timestamp(base_date).normalize()
        month_start = base_date.replace(day=1)
        if anchor_month is not None and pd.Timestamp(anchor_month) == month_start:
            return None
        elapsed_days = (base_date - month_start).days + 1
        if elapsed_days < _PROJECTION_MIN_ELAPSED_DAYS:
            return None

        profit_mm_raw = profit_hybrid_meta.get("latest_final_nyuin")
        if profit_mm_raw is None:
            profit_mm_raw = profit_hybrid_meta.get("latest_mtdblend_nyuin")
        if profit_mm_raw is None:
            return None
        profit_mm = round(float(profit_mm_raw), 1)

        patient_days_raw = _projected_patient_days_this_month(adm, base_date)
        if not patient_days_raw:
            return None
        patient_days = int(round(patient_days_raw))

        ppd = round(profit_mm * 1_000_000.0 / patient_days_raw)

        vs_prev_pct = None
        direction = "flat"
        if latest_ppd:
            vs_prev_pct = round((ppd - latest_ppd) / latest_ppd * 100, 1)
            if vs_prev_pct >= _PROJECTION_DEADBAND_PCT:
                direction = "up"
            elif vs_prev_pct <= -_PROJECTION_DEADBAND_PCT:
                direction = "down"
            else:
                direction = "flat"

        target_mm = _inpatient_target_mm(profit_targets_breakdown, month_start, dept=None)
        achievement_pct = (round(profit_mm / target_mm * 100, 1)
                          if target_mm else None)

        return {
            "month": month_start.strftime("%Y-%m"),
            "as_of": base_date.strftime("%Y-%m-%d"),
            "elapsed_days": elapsed_days,
            "profit_mm": profit_mm,
            "patient_days": patient_days,
            "ppd": ppd,
            "vs_prev_pct": vs_prev_pct,
            "direction": direction,
            "target_mm": target_mm,
            "achievement_pct": achievement_pct,
        }
    except Exception:
        return None


def build_profit_unit_payload(profit_breakdown: Optional[pd.DataFrame], adm: pd.DataFrame,
                              base_date, months: int = 13,
                              profit_targets_breakdown: Optional[pd.DataFrame] = None,
                              profit_hybrid_meta: Optional[dict] = None,
                              hospital_series: Optional[dict] = None) -> dict:
    """粗利タブ新設ブロック（数量×単価分解）用ペイロード。病院全体＋診療科別を返す。

    粗利データが無い/入院区分が取れない場合は {"global": None, "by_dept": {}}。
    診療科は入院粗利データがある科だけを収録する（延患者数が取れない科はその科の
    ブロックだけ None 相当で除外＝静かに縮退）。

    profit_targets_breakdown（診療科名・区分・月次目標）を渡すと、latest.target_mm /
    achievement_pct が入院目標込みで埋まる。未指定（None）なら目標なしで静かに縮退。

    profit_hybrid_meta（build_hybrid_payload の meta。latest_final_nyuin または
    latest_mtdblend_nyuin を含む）を渡すと、global にのみ当月（進行中）の粗利/人日
    見込み "projection" が付く。未指定または当月経過3日未満なら None で静かに縮退。
    by_dept には付与しない（科別の latest_final_nyuin/latest_mtdblend_nyuin が無く、
    比例フォールバックで一律値になり嘘をつくため）。

    hospital_series（apply_recency_calibration 後の profit_hybrid_section["hospital_series"]。
    values_final_nyuin を含む・呼び出し側は校正ブロックの後で渡すこと）を渡すと、
    Phase4 S2（確報遅れ: 入院粗利の最新確報月(anchor_month) が前月より1か月以上古い）
    のとき global にのみ "prev_month_pending"（前月＝未確報月の見込み。新規推計器は
    作らず既存の日次系列から抽出）が付く。S1（通常）では None。

    Returns:
        {
          "global":  {ppd, alos, decomp, marginal, latest, projection,
                      prev_month_pending} または None,
          "by_dept": {診療科名: {ppd, alos, decomp, marginal, latest}, ...}（データがある科のみ）,
        }
    """
    empty = {"global": None, "by_dept": {}}
    if profit_breakdown is None or len(profit_breakdown) == 0:
        return empty
    if "区分" not in profit_breakdown.columns or "診療科名" not in profit_breakdown.columns:
        return empty

    nyuin_df = profit_breakdown[profit_breakdown["区分"] == "入院"].copy()
    if len(nyuin_df) == 0:
        return empty
    nyuin_df["月"] = pd.to_datetime(nyuin_df["月"])

    # アンカー月 = min(入院粗利の最新確報月, base_date の月)。base_date より先の
    # （まだ確報が無い）月を窓に含めない一方、base_date が過去日でもデータの
    # 無い未来月を作らないための両側ガード。
    anchor_month = min(nyuin_df["月"].max(), pd.Timestamp(base_date).replace(day=1))

    global_block = _build_unit_block(nyuin_df, adm, anchor_month, months,
                                     group_col=None, unit=None,
                                     profit_targets_breakdown=profit_targets_breakdown)
    if global_block is not None:
        # ── Phase4 S2: 確報遅れ（A=anchor_month が前月Pより古い）なら、まだ確報が
        #   入っていないP月の「見込み（確報待ち）」を付与する。S1（A==P・通常）は None。
        d_month = pd.Timestamp(base_date).normalize().replace(day=1)
        p_month = d_month - pd.DateOffset(months=1)
        prev_month_pending = (
            _build_prev_month_pending(hospital_series, adm, anchor_month, p_month, base_date)
            if anchor_month < p_month else None
        )
        global_block["prev_month_pending"] = prev_month_pending

        # 当月見込み(D)の前月比の比較対象は「直近の分かっている月」＝S2ならP(見込み)、
        # S1ならA(確報)。S1では常に A==P なので従来どおり latest.ppd と同じ値になり、
        # 挙動は変わらない（S2でのみ効く後方互換の一般化）。
        reference_ppd = ((prev_month_pending or {}).get("ppd")
                         if prev_month_pending
                         else (global_block.get("latest") or {}).get("ppd"))
        global_block["projection"] = _build_projection(adm, base_date, profit_hybrid_meta,
                                                        reference_ppd,
                                                        profit_targets_breakdown=profit_targets_breakdown,
                                                        anchor_month=anchor_month)

    by_dept = {}
    for dept in sorted(nyuin_df["診療科名"].unique()):
        blk = _build_unit_block(nyuin_df, adm, anchor_month, months,
                                group_col="診療科名", unit=dept,
                                profit_targets_breakdown=profit_targets_breakdown)
        if blk is not None:
            by_dept[dept] = blk

    return {"global": global_block, "by_dept": by_dept}
