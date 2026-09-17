"""
profit_headline.py — 粗利ヘッドライン正本モジュール（P0.5 新設）

粗利の主役指標を「入院 粗利/人日」「外来 粗利/営業日」にする改修で、数値と文言の
正本をここに1本化する。portal / Comedix / PDF / 週次ダイジェストは全てここから読む
（detail.html だけは既存の DATA.profit_unit / DATA.month_projection 経路を維持する。
同じ hybrid セクションから派生するので値は一致する）。

新しい推計器は作らない。既存関数の再利用のみ:
  - profit_unit.build_profit_unit_payload         … 入院側（粗利/人日・見込み・確報）
  - month_projection.build_month_projection_payload … 合計（月末見込み・目標・達成率）
  - html_builder.build_profit_hybrid_calibrated    … hybrid セクション（recency 校正後 meta）
  - config.biz_days_in_month                       … 営業日数

外来側（粗利/営業日）だけは既存モジュールに payload が無いため、本モジュールで
hybrid meta（latest_final_gairai 等）と profit_breakdown/profit_targets_breakdown
から直接組み立てる（新しい推計はしない。確報実績・hybrid の値をそのまま使うだけ）。
"""
from typing import Optional

import pandas as pd

from .profit_unit import build_profit_unit_payload, _PROJECTION_DEADBAND_PCT
from .month_projection import build_month_projection_payload
from .config import biz_days_in_month, STD_BIZ_DAYS_PER_MONTH


# labels は app/templates/detail.html の既存文言から写経（新語彙を作らない）。
# tests/test_profit_headline_wording.py がこの辞書を detail.html と突き合わせる。
_LABELS_STATIC = {
    "main_nyuin": "入院 粗利/人日",
    "main_gairai": "外来 粗利/営業日",
    "target": "月次目標",
    "target_gairai": "月次目標(補正)",
    "tolerance": "（±2%）",
    "note": "※ 見込みの誤差は月間 ±2%程度。単価そのものの変化（加算・コーディング・改定）は映りません",
    "guard": "※ 在院患者を減らして上げる数字ではありません。同じ病床で回転を上げると上がります。",
    "scope_nyuin": "入院のみ・全科",
    "scope_gairai": "外来のみ・全科",
}


def _direction(vs_prev_pct: Optional[float]) -> str:
    """profit_unit._build_projection と同じ語彙・デッドバンド（±_PROJECTION_DEADBAND_PCT%）。"""
    if vs_prev_pct is None:
        return "flat"
    if vs_prev_pct >= _PROJECTION_DEADBAND_PCT:
        return "up"
    if vs_prev_pct <= -_PROJECTION_DEADBAND_PCT:
        return "down"
    return "flat"


def _gairai_target_total_千円(profit_targets_breakdown: Optional[pd.DataFrame]) -> Optional[float]:
    """外来の月次目標合計（千円・未補正）。_gairai_target_mm_nominal / _gairai_target_mm
    の共通の生値取得。breakdown が無い・外来行が無い・合計が0以下なら None。
    """
    if profit_targets_breakdown is None or len(profit_targets_breakdown) == 0:
        return None
    if "区分" not in profit_targets_breakdown.columns:
        return None
    df = profit_targets_breakdown[profit_targets_breakdown["区分"] == "外来"]
    if len(df) == 0:
        return None
    total_千円 = float(df["月次目標"].fillna(0).sum())
    if total_千円 <= 0:
        return None
    return total_千円


def _gairai_target_mm_nominal(profit_targets_breakdown: Optional[pd.DataFrame]) -> Optional[float]:
    """外来の月次目標（百万円・補正前）。profit_targets_breakdown の 区分=="外来" 行の
    月次目標[千円] 合計をそのまま百万円換算する（1桁丸め）。
    """
    total_千円 = _gairai_target_total_千円(profit_targets_breakdown)
    if total_千円 is None:
        return None
    return round(total_千円 / 1000.0, 1)


def _gairai_target_mm(profit_targets_breakdown: Optional[pd.DataFrame],
                      month_start: pd.Timestamp) -> Optional[float]:
    """外来の月次目標（百万円・営業日補正後）。profit.py の外来補正目標と同式
    （外来目標[千円] × 当月営業日数 / STD_BIZ_DAYS_PER_MONTH。profit.py 160〜175行付近
    の 外来補正目標 = 外来目標 × biz / STD_BIZ_DAYS_PER_MONTH と同じ。式を重複させず
    STD_BIZ_DAYS_PER_MONTH は config から流用する）。
    breakdown が無い・外来行が無い・合計が0以下・当月営業日数が0なら None。
    """
    total_千円 = _gairai_target_total_千円(profit_targets_breakdown)
    if total_千円 is None:
        return None
    biz_days = biz_days_in_month(month_start)
    if not biz_days:
        return None
    total_千円_adj = total_千円 * biz_days / STD_BIZ_DAYS_PER_MONTH
    return round(total_千円_adj / 1000.0, 1)


def _gairai_prev_confirmed_ppd_biz(profit_breakdown: Optional[pd.DataFrame],
                                   prev_month: pd.Timestamp) -> Optional[float]:
    """前月の外来「確報」粗利/営業日（百万円/営業日）。前月の確報が無ければ None。"""
    if profit_breakdown is None or len(profit_breakdown) == 0:
        return None
    if "区分" not in profit_breakdown.columns or "月" not in profit_breakdown.columns:
        return None
    df = profit_breakdown[profit_breakdown["区分"] == "外来"].copy()
    df["月"] = pd.to_datetime(df["月"])
    row = df[df["月"] == prev_month]
    if len(row) == 0:
        return None
    total_千円 = float(row["粗利"].sum())
    biz_days = biz_days_in_month(prev_month)
    if not biz_days:
        return None
    return total_千円 / 1000.0 / biz_days


def _build_gairai_block(meta: dict, profit_breakdown: Optional[pd.DataFrame],
                        profit_targets_breakdown: Optional[pd.DataFrame],
                        month_start: pd.Timestamp) -> Optional[dict]:
    """外来 粗利/営業日 ブロック。hybrid meta の latest_final_gairai
    （無ければ latest_mtdblend_gairai）が取れない場合は None（fail-soft）。
    """
    profit_mm_raw = meta.get("latest_final_gairai")
    if profit_mm_raw is None:
        profit_mm_raw = meta.get("latest_mtdblend_gairai")
    if profit_mm_raw is None:
        return None
    profit_mm = round(float(profit_mm_raw), 1)

    biz_days = biz_days_in_month(month_start)
    if not biz_days:
        return None
    ppd_biz_exact = profit_mm / biz_days
    ppd_biz = round(ppd_biz_exact, 1)

    target_mm_nominal = _gairai_target_mm_nominal(profit_targets_breakdown)
    target_mm = _gairai_target_mm(profit_targets_breakdown, month_start)
    target_per_biz_day = round(target_mm / biz_days, 1) if target_mm else None
    achievement_pct = round(profit_mm / target_mm * 100, 1) if target_mm else None

    prev_month = month_start - pd.DateOffset(months=1)
    prev_ppd_biz = _gairai_prev_confirmed_ppd_biz(profit_breakdown, prev_month)
    vs_prev_pct = (round((ppd_biz_exact - prev_ppd_biz) / prev_ppd_biz * 100, 1)
                  if prev_ppd_biz else None)
    direction = _direction(vs_prev_pct)

    return {
        "ppd_biz": ppd_biz,
        "biz_days": biz_days,
        "profit_mm": profit_mm,
        "target_mm_nominal": target_mm_nominal,
        "target_mm": target_mm,
        "target_per_biz_day": target_per_biz_day,
        "achievement_pct": achievement_pct,
        "vs_prev_pct": vs_prev_pct,
        "direction": direction,
    }


def _period_label(month_str: str, as_of_ts: pd.Timestamp) -> str:
    """detail.html のヘッドライン期間文言と同じ組み立て（月ラベル=headlineの対象月、
    ◯月◯日=as_of。html_builder/detail.html 側の periodNote と同じ式）。"""
    y, m = month_str.split("-")
    return f"{int(y)}年{int(m)}月 月末見込み（{as_of_ts.month}/{as_of_ts.day}時点・診療実績ベース・暫定）"


def _build_labels(month_str: str, as_of_ts: pd.Timestamp,
                  prev_month_pending: Optional[dict]) -> dict:
    labels = dict(_LABELS_STATIC)
    labels["period"] = _period_label(month_str, as_of_ts)
    labels["cmp"] = "前月(見込み)比" if prev_month_pending is not None else "前月比"
    return labels


def build_profit_headline(adm, surg, profit_monthly, profit_breakdown,
                          profit_targets_breakdown, base_date, *,
                          hybrid: Optional[tuple] = None) -> Optional[dict]:
    """粗利ヘッドライン（入院 粗利/人日・外来 粗利/営業日・合計月末見込み）の正本 payload。

    数値の出所は既存関数のみ（新しい推計器は作らない）:
      入院 = profit_unit.build_profit_unit_payload の global.projection/latest/prev_month_pending
      外来 = hybrid meta（latest_final_gairai）+ profit_breakdown/profit_targets_breakdown
      合計 = month_projection.build_month_projection_payload の "profit"

    hybrid は (profit_hybrid_section, profit_g_calibrated) のタプル。None のときだけ
    html_builder.build_profit_hybrid_calibrated を遅延 import して内部で計算する
    （html_builder ⇄ profit_headline の循環回避のため、import はこの関数内に閉じる。
    モジュール import 時には html_builder を読み込まない）。

    section が None／必要キー欠落／例外時は None を返す（fail-soft。例外を外に出さない）。
    """
    try:
        base_date = pd.Timestamp(base_date).normalize()

        if hybrid is None:
            from .html_builder import build_profit_hybrid_calibrated, last_complete_driver_date
            profit_base_date = last_complete_driver_date(adm, surg) or base_date
            hybrid = build_profit_hybrid_calibrated(profit_breakdown, surg, adm, profit_base_date)

        section, g_million = hybrid
        if not section:
            return None
        meta = section.get("meta") or {}
        if not meta:
            return None
        hospital_series = section.get("hospital_series")

        unit_payload = build_profit_unit_payload(
            profit_breakdown, adm, base_date,
            profit_targets_breakdown=profit_targets_breakdown,
            profit_hybrid_meta=meta,
            hospital_series=hospital_series,
        )
        global_block = (unit_payload or {}).get("global")
        projection = (global_block or {}).get("projection")
        if not projection:
            return None

        month_start = base_date.replace(day=1)
        gairai = _build_gairai_block(meta, profit_breakdown, profit_targets_breakdown, month_start)
        if gairai is None:
            return None

        mp_payload = build_month_projection_payload(
            adm=adm, surg=surg, profit_monthly=profit_monthly,
            profit_hybrid_meta=meta,
            profit_hybrid_hospital_series=hospital_series,
            base_date=base_date,
            profit_hybrid_g_override=g_million,
        )
        profit_tile = mp_payload.get("profit")
        if not profit_tile:
            return None

        nyuin = {
            "ppd": projection.get("ppd"),
            "patient_days": projection.get("patient_days"),
            "profit_mm": projection.get("profit_mm"),
            "target_mm": projection.get("target_mm"),
            "achievement_pct": projection.get("achievement_pct"),
            "vs_prev_pct": projection.get("vs_prev_pct"),
            "direction": projection.get("direction"),
        }
        total = {
            "proj_mm": profit_tile.get("projection"),
            "target_mm": profit_tile.get("target"),
            "rate": profit_tile.get("rate"),
            "status": {
                "css": profit_tile.get("status_css"),
                "shape": profit_tile.get("status_shape"),
                "text": profit_tile.get("status_text"),
            },
        }

        identity_ok = False
        if (total["proj_mm"] is not None and nyuin["profit_mm"] is not None
                and gairai["profit_mm"] is not None):
            identity_ok = abs(total["proj_mm"] - (nyuin["profit_mm"] + gairai["profit_mm"])) <= 0.1 + 1e-6

        target_identity_ok = None
        if (total["target_mm"] is not None and nyuin["target_mm"] is not None
                and gairai["target_mm"] is not None):
            target_identity_ok = (
                abs(total["target_mm"] - (nyuin["target_mm"] + gairai["target_mm"])) <= 0.1 + 1e-6)

        window_end = meta.get("window_end")
        as_of_ts = pd.Timestamp(window_end) if window_end else base_date
        month_str = projection.get("month") or month_start.strftime("%Y-%m")
        prev_month_pending = global_block.get("prev_month_pending")

        return {
            "month": month_str,
            "as_of": as_of_ts.strftime("%Y-%m-%d"),
            "nyuin": nyuin,
            "gairai": gairai,
            "total": total,
            "latest": global_block.get("latest"),
            "prev_month_pending": prev_month_pending,
            "identity_ok": identity_ok,
            "target_identity_ok": target_identity_ok,
            "labels": _build_labels(month_str, as_of_ts, prev_month_pending),
        }
    except Exception:
        return None
