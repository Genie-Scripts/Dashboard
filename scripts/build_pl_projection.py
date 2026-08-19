"""
build_pl_projection.py — 医業収支 推計レポート生成

usage:
    python -m scripts.build_pl_projection
    python -m scripts.build_pl_projection --base-date 2026-05-27
    python -m scripts.build_pl_projection --output output/pl_projection.html

出力: output/pl_projection.html (ローカル閲覧のみ・portal からはリンクしない)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

# プロジェクトルートを sys.path に追加（直接実行 / -m どちらでも動かす）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.lib.config import DEFAULT_DATA_DIR  # noqa: E402
from app.lib.data_loader import (  # noqa: E402
    load_admission_data,
    load_surgery_data,
    load_profit_breakdown,
)
from app.lib.preprocess import preprocess_admission, preprocess_surgery  # noqa: E402
from app.lib.pl_history import load_pl_confirmed, clean_pl, quality_flags  # noqa: E402
from app.lib.pl_projection import (  # noqa: E402
    aggregate_profit_monthly,
    compute_delta_series,
    project_monthly_balance,
    backtest,
    prediction_intervals,
    append_residual_log,
    material_cost_monitoring,
)
from app.lib.profit_estimate import (  # noqa: E402
    build_hybrid_payload, apply_recency_calibration, last_complete_driver_date,
)


# ──────────────────────────────────────────
# G_proj 取得（粗利推計 MTDブレンド月末見込み + recency バイアス補正）
# ──────────────────────────────────────────

def fetch_profit_projection(adm: pd.DataFrame,
                              surg: pd.DataFrame,
                              profit_breakdown: pd.DataFrame,
                              base_date: pd.Timestamp,
                              calibrate: bool = True) -> tuple[float, dict]:
    """build_hybrid_payload を呼び、当月末予測 G を百万円→千円に換算して返す。

    G = MTDブレンド月末見込み × recency補正(k12_shrink50)。確定ロジックは
    profit_estimate.apply_recency_calibration に集約し、ダッシュボードと共用・
    同一キャッシュ参照で必ず同じ G になるようにしている。
    """
    payload = build_hybrid_payload(
        profit_breakdown=profit_breakdown,
        surg=surg,
        base_date=base_date,
        adm=adm,
    )
    if not payload:
        raise RuntimeError("粗利推計ペイロードが空でした")
    series_meta = payload.get("meta") or {}
    g = apply_recency_calibration(series_meta, profit_breakdown, surg, adm,
                                  base_date, calibrate=calibrate)
    return g["g_million"] * 1000.0, {  # 千円
        "g_metric": g["g_metric"],
        "latest_projection_total_million": g["blend_million"],
        "mtd_blend_weight": g["mtd_blend_weight"],
        "raw_projection_million": g["raw_projection_million"],
        "calibrated_million": g["g_million"],
        "calibration_factor": g["calibration_factor"],
        "calibration_n_months": g["calibration_n_months"],
        "calibration_raw_median": g["calibration_raw_median"],
        "base_date": base_date.strftime("%Y-%m-%d"),
    }


def build_one_projection(pl: pd.DataFrame,
                          delta: pd.DataFrame,
                          g_monthly: pd.DataFrame,
                          adm: pd.DataFrame,
                          surg: pd.DataFrame,
                          profit_breakdown: pd.DataFrame,
                          target_month: pd.Timestamp,
                          base_date: pd.Timestamp,
                          label: str) -> dict:
    """1か月分の G推計・医業収支予測・予測区間をまとめた entry を返す。

    PL確報の翌月〜ドライバー月を月ごとにループ呼びする薄いラッパー。対象月の粗利が
    確報済み（g_monthly に在る）なら、推計Gでなく**確定粗利**を使い、コストだけ推計する
    （収支見込みの精度が上がり、公表済みの粗利と数字が食い違わない）。
    """
    g_proj, g_meta = fetch_profit_projection(adm, surg, profit_breakdown, base_date)
    # 対象月の粗利が確報済みか（PL未着でも粗利だけ先に確定する端境期がある）
    g_confirmed = g_monthly[g_monthly["月"] == pd.Timestamp(target_month).normalize().replace(day=1)]
    use_actual_g = len(g_confirmed) > 0
    # 確定粗利があれば g_override=None で project_monthly_balance に実績Gを引かせる
    projection = project_monthly_balance(pl, delta, g_monthly, target_month,
                                         g_override=(None if use_actual_g else g_proj))
    pi = prediction_intervals(pl, delta, g_monthly, target_month, projection)
    if use_actual_g:
        g_value = float(g_confirmed["G"].iloc[0])
        g_source = "粗利データ実績（確報）／コストのみ推計"
    else:
        g_value = g_proj
        g_source = (f"粗利推計 MTDブレンド月末見込み(w={g_meta.get('mtd_blend_weight')}, anchor8)"
                    f" ×{g_meta['calibration_factor']}"
                    f"（recency補正 k12_shrink50, n={g_meta['calibration_n_months']}）")
    return {
        "target_month": target_month,
        "base_date": base_date,
        "projection": projection,
        "pi": pi,
        "g_meta": g_meta,
        "g_source": g_source,
        "label": label,
        "g_proj": g_value,
        "g_confirmed": use_actual_g,
    }


# ──────────────────────────────────────────
# HTML 生成
# ──────────────────────────────────────────

def _fmt_yen(v: float, unit: str = "千円") -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    if unit == "百万円":
        return f"{v/1000:,.0f} 百万円"
    return f"{v:,.0f} {unit}"


def _color_balance(v: float) -> str:
    if v is None:
        return "#888"
    return "#0a8a3a" if v >= 0 else "#c13a3a"


def render_html(pl_clean: pd.DataFrame,
                delta_series: pd.DataFrame,
                entries: list,
                bt: pd.DataFrame,
                residual_log_path: Path,
                meta: dict,
                monitor: dict) -> str:
    """シンプル独立 HTML（ローカル閲覧専用）。

    entries: build_one_projection() の戻り値リスト（時系列順）。粗利確報が
    未入力のとき前月＋当月が並ぶ。確報入力で対象が1件に収束する。
    """
    months_iso = [d.strftime("%Y-%m") for d in pl_clean["月"].tolist()]

    # 推移グラフデータ（25か月）
    series = {}
    for col in ["医業収益", "材料費", "給与費", "委託費", "設備関係費",
                "経費", "医業収支"]:
        series[col] = pl_clean[col].fillna(0).round(0).tolist()
    series["δ"] = delta_series["δ"].reindex(
        pl_clean.merge(delta_series[["月"]], on="月", how="left").index
    ).fillna(0).tolist() if False else None
    # 簡素化: δ を pl_clean 月で整列
    delta_aligned = pl_clean.merge(delta_series[["月", "δ"]],
                                    on="月", how="left")
    series["δ"] = delta_aligned["δ"].fillna(0).round(0).tolist()

    # ── 予測対象月ごとのサマリ＋内訳（粗利未確定の月が複数並ぶ） ──
    def _entry_summary_html(entry: dict) -> str:
        proj = entry["projection"]
        pi = entry["pi"]
        proj_month = proj["月"].strftime("%Y-%m")
        proj_balance = proj["予測医業収支"]
        proj_revenue = proj["予測R_minus_M"]
        parts = {k: proj[k] for k in
                 ("G", "δ", "給与費", "委託費", "設備関係費", "経費")}

        if pi.get("available"):
            pi_text = (f"<div class='sub'>"
                       f"σ = ±{pi['sigma']/1000:,.0f}百万円 / "
                       f"80% PI [{pi['pi80_lo']/1000:+,.0f}, {pi['pi80_hi']/1000:+,.0f}] / "
                       f"95% PI [{pi['pi95_lo']/1000:+,.0f}, {pi['pi95_hi']/1000:+,.0f}] 百万円"
                       f"</div>")
        else:
            pi_text = "<div class='sub'>予測区間: バックテスト不足</div>"

        parts_rows = []
        for plabel, info in [
            ("粗利 G (推計)", parts["G"]),
            ("δ (材料収支差)", parts["δ"]),
            ("R−M 予測 = G + δ", {"value": proj_revenue, "source": ""}),
            ("− 給与費", parts["給与費"]),
            ("− 委託費", parts["委託費"]),
            ("− 設備関係費", parts["設備関係費"]),
            ("− 経費", parts["経費"]),
        ]:
            v = info["value"]
            method = info.get("method") or info.get("source") or ""
            parts_rows.append(
                f"<tr><td>{plabel}</td><td style='text-align:right'>{v:+,.0f}</td>"
                f"<td style='color:#888;font-size:0.85em'>{method}</td></tr>"
            )
        parts_rows.append(
            f"<tr style='border-top:2px solid #333;font-weight:700'>"
            f"<td>医業収支 予測</td>"
            f"<td style='text-align:right;color:{_color_balance(proj_balance)}'>"
            f"{proj_balance:+,.0f} 千円</td><td></td></tr>"
        )

        return f"""
<h2>📅 {proj_month} 予測サマリ <span class="sub">{entry['label']}</span></h2>
<div class="metric-grid">
  <div class="metric">
    <div class="label">医業収益 予測 (R−M = G + δ)</div>
    <div class="value">{_fmt_yen(proj_revenue, "百万円")}</div>
  </div>
  <div class="metric">
    <div class="label">給与費 予測</div>
    <div class="value">{_fmt_yen(parts["給与費"]["value"], "百万円")}</div>
  </div>
  <div class="metric">
    <div class="label">委託費＋設備＋経費 予測</div>
    <div class="value">{_fmt_yen(parts["委託費"]["value"] + parts["設備関係費"]["value"] + parts["経費"]["value"], "百万円")}</div>
  </div>
  <div class="metric">
    <div class="label">医業収支 予測</div>
    <div class="value" style="color:{_color_balance(proj_balance)}">
      {proj_balance/1000:+,.0f} 百万円
    </div>
    {pi_text}
  </div>
</div>
<div class="card">
<table>
<thead><tr><th>項目</th><th>値</th><th>算出方法</th></tr></thead>
<tbody>
{chr(10).join(parts_rows)}
</tbody>
</table>
<p class="sub" style="margin-top:8px">G ({entry['g_source']}) は粗利推計 latest_projection_total から取得。
δ は (R−M) − G の構造的差分（DPC包括分等の購入差）。</p>
</div>
"""

    summary_sections = "\n".join(_entry_summary_html(e) for e in entries)
    proj_points = [{"month": e["projection"]["月"].strftime("%Y-%m"),
                    "balance": float(e["projection"]["予測医業収支"])}
                   for e in entries]
    multi_note = ("" if len(entries) <= 1 else
                  "<br><b>※ 粗利確報が未入力のため、確定前の月を複数併記しています</b>"
                  "（前月＝月末時点のフル月見込み／当月＝進行中の早期見込み）。"
                  "粗利が確定入力されると、その月は自動的に実績へ切り替わり単月表示に戻ります。")

    # バックテスト精度
    if len(bt) > 0:
        mae = bt["誤差"].abs().mean()
        rmse = float(np.sqrt((bt["誤差"] ** 2).mean()))
        rel = mae / bt["実績医業収支"].abs().mean() * 100
        bt_months = bt["月"].dt.strftime("%Y-%m").tolist()
        bt_actual = bt["実績医業収支"].round(0).tolist()
        bt_pred = bt["予測医業収支"].round(0).tolist()
    else:
        mae = rmse = rel = float("nan")
        bt_months = bt_actual = bt_pred = []

    # 残差ログ（過去の予測 vs 実績） 読み込み
    res_rows = []
    if residual_log_path.exists():
        try:
            rdf = pd.read_csv(residual_log_path)
            rdf = rdf[rdf["実績医業収支"].notna()].tail(12)
            res_rows = rdf.to_dict("records")
        except Exception:
            res_rows = []

    # ── 材料費モニタリング（薬剤パススルー除去・インフレ早期検知） ──
    mrows = monitor["rows"]
    mon_months = [d.strftime("%Y-%m") for d in mrows["月"].tolist()]
    mt = monitor["trends"]
    st_kind, st_msg = monitor["status"]
    st_color = {"ok": "#0a8a3a", "watch": "#a55b00", "warn": "#c13a3a"}[st_kind]
    st_label = {"ok": "✓ 良好", "watch": "△ 監視", "warn": "⚠ 警告"}[st_kind]

    def _trend_row(label, key, good_up=True):
        t = mt[key]
        ann = t["annual"]  # 既に百万円/年
        r2 = t["r2"]
        # 良し悪しの色: good_up=True は上昇が良い（収益系）、構造δは上昇=改善
        arrow = "↗" if t["slope_per_month"] > 0 else ("↘" if t["slope_per_month"] < 0 else "→")
        return (f"<tr><td>{label}</td>"
                f"<td style='text-align:right'>{t['slope_per_month']:+,.1f}</td>"
                f"<td style='text-align:right'>{ann:+,.0f}</td>"
                f"<td style='text-align:right'>{arrow}</td>"
                f"<td style='text-align:right;color:#888'>"
                f"{('—' if r2 is None else f'{r2:.2f}')}</td></tr>")

    monitor_trend_rows = (
        _trend_row("構造δ（薬剤購入除去後）", "δ_構造")
        + _trend_row("非薬剤材料（診療材料+消耗品）", "非薬剤材料")
        + _trend_row("医薬品費（パススルー）", "医薬品費")
        + _trend_row("δ 実績（生）", "δ")
    )
    mon_recent = mrows.tail(12).copy()
    mon_recent["月_s"] = mon_recent["月"].dt.strftime("%Y-%m")
    monitor_table_rows = "\n".join(
        f"<tr><td>{r['月_s']}</td>"
        f"<td>{r['医薬品費']/1000:,.0f}</td>"
        f"<td>{r['非薬剤材料']/1000:,.0f}</td>"
        f"<td style='color:{_color_balance(r['δ'])}'>{r['δ']/1000:+,.0f}</td>"
        f"<td style='color:{_color_balance(r['δ_構造'])};font-weight:600'>"
        f"{r['δ_構造']/1000:+,.0f}</td></tr>"
        for _, r in mon_recent.iterrows()
    )

    # チャート用 JSON
    chart_data = {
        "months": months_iso,
        "series": series,
        "projections": proj_points,
        "bt_months": bt_months,
        "bt_actual": bt_actual,
        "bt_pred": bt_pred,
        "mon_months": mon_months,
        "mon_delta": mrows["δ"].round(0).tolist(),
        "mon_delta_struct": mrows["δ_構造"].round(0).tolist(),
        "mon_nondrug": mrows["非薬剤材料"].round(0).tolist(),
    }

    rows_recent = pl_clean.tail(6).copy()
    rows_recent["月_str"] = rows_recent["月"].dt.strftime("%Y-%m")
    # 構造収支 = 医業収支 + (医薬品費 − 薬剤中央値)。高額薬剤の購入計上タイミングを均し、
    # 月間比較を直感に合わせる（薬剤は粗利Gで控除済み＝本来パススルー）。
    _med_drug = monitor["median_drug"]
    table_rows = "\n".join(
        f"<tr><td>{r['月_str']}</td>"
        f"<td>{r['医業収益']:,.0f}</td>"
        f"<td>{r['材料費']:,.0f}</td>"
        f"<td>{r['給与費']:,.0f}</td>"
        f"<td>{r['委託費']:,.0f}</td>"
        f"<td>{r['設備関係費']:,.0f}</td>"
        f"<td>{r['経費']:,.0f}</td>"
        f"<td style='color:{_color_balance(r['医業収支'])};font-weight:600'>"
        f"{r['医業収支']:+,.0f}</td>"
        f"<td style='color:{_color_balance(r['医業収支'] + r['医薬品費'] - _med_drug)}'>"
        f"{r['医業収支'] + r['医薬品費'] - _med_drug:+,.0f}</td></tr>"
        for _, r in rows_recent.iterrows()
    )

    # バックテストテーブル
    bt_rows = "\n".join(
        f"<tr><td>{m}</td>"
        f"<td style='text-align:right'>{a:+,.0f}</td>"
        f"<td style='text-align:right'>{p:+,.0f}</td>"
        f"<td style='text-align:right;color:{'#c13a3a' if abs(p-a)>30000 else '#888'}'>"
        f"{p-a:+,.0f}</td></tr>"
        for m, a, p in zip(bt_months, bt_actual, bt_pred)
    )

    gen = meta.get("generated_at", datetime.now().strftime("%Y/%m/%d %H:%M"))
    base_date = meta.get("base_date", "")

    excluded = meta.get("excluded_months") or []
    if excluded:
        items = "、".join(f"{e['month']}（{e['reason']}）" for e in excluded)
        quality_alert = f"""
<div class="alert">
⚠ <strong>データ品質異常により {len(excluded)} か月の PL確報を除外中: {items}</strong><br>
除外月は確報が反映されず、予測サマリが「粗利確定・PL確報待ち」のまま停滞します。
<code>data/PL_確定.xlsx</code> の医業収支行を確認してください
（未転記 0 / 千円値をそのまま貼付＝×1000漏れ が典型。正値は月次 PL.xlsx の医業収支×1000）。
</div>"""
    else:
        quality_alert = ""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>医業収支 推計レポート (ローカル)</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
body {{font-family:-apple-system,BlinkMacSystemFont,'Hiragino Sans',sans-serif;
       max-width:1280px;margin:24px auto;padding:0 20px;color:#222;line-height:1.5}}
h1 {{font-size:1.5em;border-bottom:2px solid #333;padding-bottom:8px}}
h2 {{font-size:1.15em;margin-top:32px;color:#444}}
.card {{background:#f7f7f8;border:1px solid #e0e0e3;border-radius:10px;
        padding:16px 20px;margin:12px 0}}
.headline {{font-size:1.8em;font-weight:700;letter-spacing:-0.02em}}
.sub {{color:#666;font-size:0.9em}}
table {{border-collapse:collapse;width:100%;margin-top:10px;font-size:0.92em}}
th,td {{padding:6px 10px;text-align:right;border-bottom:1px solid #eee}}
th:first-child,td:first-child {{text-align:left}}
.warn {{color:#a55b00;font-size:0.85em}}
.metric-grid {{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:16px 0}}
.metric {{background:#fff;border:1px solid #e0e0e3;border-radius:8px;padding:12px}}
.metric .label {{font-size:0.8em;color:#666}}
.metric .value {{font-size:1.4em;font-weight:700;margin-top:4px}}
canvas {{max-height:340px}}
.note {{background:#fffaf0;border-left:3px solid #d4a017;padding:10px 14px;
        font-size:0.9em;color:#553}}
.alert {{background:#fdecea;border:1px solid #f5c6cb;border-left:4px solid #c0392b;
         border-radius:8px;padding:12px 16px;margin:14px 0;color:#7b241c;
         font-size:0.95em}}
</style>
</head>
<body>

<h1>医業収支 推計レポート <span class="sub">(ローカル閲覧専用 / 公開しない)</span></h1>
<p class="sub">生成: {gen} | 基準日: {base_date}</p>
{quality_alert}

<div class="note">
本レポートは <code>data/PL_確定.xlsx</code>（年度別確報, FY2023〜）と粗利推計を組み合わせた
医業収支の月末予測です。費目モデルの誤差により絶対値は±25-50百万円程度の振れを想定。
方向性把握（黒字／赤字／前月比）が主目的です。{multi_note}
</div>

{summary_sections}

<h2>材料費モニタリング <span class="sub">(薬剤パススルー除去・インフレ早期検知)</span></h2>
<div class="card">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
    <span style="background:{st_color};color:#fff;padding:3px 10px;border-radius:6px;
                 font-weight:700;font-size:0.9em">{st_label}</span>
    <span style="color:#444">{st_msg}</span>
  </div>
  <p class="sub">高額薬剤（医薬品費）は粗利 G から控除済みで本来パススルーだが、材料費(購入額)と
  償還(点数)の計上ズレで δ に一時的に漏れる（例: 2026-04 は医薬品費スパイクで δ −260、
  薬剤購入を戻した<b>構造δは −94</b>）。構造δと非薬剤材料のトレンドで、原油・ナフサ等による
  <b>材料インフレが医業収支に効き始める前兆</b>を監視する。推計式（δは生の中央値）は不変。</p>
  <table style="margin-top:6px">
    <thead><tr><th>系列</th><th>月次傾き(百万/月)</th><th>年率(百万)</th><th></th><th>R²</th></tr></thead>
    <tbody>{monitor_trend_rows}</tbody>
  </table>
  <p class="sub" style="margin-top:6px">※ 構造δは「上昇=改善」。R²が低い間はトレンド未確立（生δのノイズは薬剤購入が支配）。
  構造δが <b>傾き ≤ −3 かつ R² ≥ 0.30</b> になったら δ にトレンド導入を検討。</p>
  <canvas id="deltaChart" style="margin-top:10px"></canvas>
  <table style="margin-top:12px">
    <thead><tr><th>月</th><th>医薬品費</th><th>非薬剤材料</th><th>δ実績</th><th>構造δ</th></tr></thead>
    <tbody>{monitor_table_rows}</tbody>
  </table>
</div>

<h2>過去6か月の PL 実績（千円）</h2>
<div class="card">
<table>
<thead><tr><th>月</th><th>医業収益</th><th>材料費</th><th>給与費</th>
<th>委託費</th><th>設備関係費</th><th>経費</th><th>医業収支</th>
<th>構造収支</th></tr></thead>
<tbody>
{table_rows}
</tbody>
</table>
<p class="sub" style="margin-top:8px">構造収支 = 医業収支 + (医薬品費 − 薬剤中央値 {monitor["median_drug"]/1000:,.0f}百万)。
高額薬剤の購入計上タイミングを均した収支。薬剤は粗利 G から控除済み＝本来パススルーのため、
薬剤購入が嵩んだ月（例 2026-04）の医業収支が一時的に沈む見かけを補正し、月間比較を直感に合わせる。
薬剤と収支は実は無相関（r=+0.10）で、構造収支は薬剤購入の偶発を除いた基調を示す。</p>
</div>

<h2>費目推移（25か月）</h2>
<div class="card"><canvas id="trendChart"></canvas></div>

<h2>医業収支 推移と予測</h2>
<div class="card"><canvas id="balanceChart"></canvas></div>

<h2>予測精度の累積記録（残差ログ）</h2>
<div class="card">
{(
  '<table><thead><tr><th>実行日</th><th>対象月</th><th>予測</th><th>実績</th><th>誤差</th></tr></thead><tbody>' +
  ''.join(f"<tr><td>{r['run_date']}</td><td>{r['target_month']}</td>"
           f"<td style='text-align:right'>{r['予測医業収支']:+,.0f}</td>"
           f"<td style='text-align:right'>{r['実績医業収支']:+,.0f}</td>"
           f"<td style='text-align:right;color:{'#c13a3a' if abs(r['誤差'])>30000 else '#888'}'>"
           f"{r['誤差']:+,.0f}</td></tr>" for r in res_rows) +
  '</tbody></table>'
) if res_rows else '<p class="sub">まだ記録がありません。月次PL確報を更新するとここに蓄積されます。</p>'}
<p class="sub" style="margin-top:8px">毎回 make pl 実行時に当月予測+前月以前確報の差分を <code>{str(residual_log_path)}</code> に追記しています。</p>
</div>

<h2>バックテスト（G 実績ベース）</h2>
<div class="card">
<p>
  MAE: <strong>{mae:,.0f} 千円</strong> ({mae/1000:.0f} 百万円) /
  RMSE: <strong>{rmse:,.0f} 千円</strong> /
  相対誤差: <strong>{rel:.1f}%</strong>
</p>
<table>
<thead><tr><th>月</th><th>実績</th><th>予測</th><th>誤差</th></tr></thead>
<tbody>
{bt_rows}
</tbody>
</table>
<p class="sub" style="margin-top:8px">注: バックテストでは G を実績値で固定しているため、
費目モデル単体の誤差を測定。実運用では G 自体の推計誤差も加算される。</p>
</div>

<script>
const data = {json.dumps(chart_data, ensure_ascii=False)};

new Chart(document.getElementById('trendChart'), {{
  type: 'line',
  data: {{
    labels: data.months,
    datasets: [
      {{label:'医業収益', data:data.series['医業収益'], borderColor:'#1f77b4',backgroundColor:'#1f77b410',tension:0.2}},
      {{label:'給与費', data:data.series['給与費'], borderColor:'#d62728',backgroundColor:'#d6272810',tension:0.2}},
      {{label:'材料費', data:data.series['材料費'], borderColor:'#ff7f0e',backgroundColor:'#ff7f0e10',tension:0.2}},
      {{label:'委託費', data:data.series['委託費'], borderColor:'#2ca02c',backgroundColor:'#2ca02c10',tension:0.2}},
      {{label:'設備関係費', data:data.series['設備関係費'], borderColor:'#9467bd',backgroundColor:'#9467bd10',tension:0.2}},
      {{label:'経費', data:data.series['経費'], borderColor:'#8c564b',backgroundColor:'#8c564b10',tension:0.2}},
    ]
  }},
  options: {{
    responsive:true,
    plugins:{{legend:{{position:'bottom'}}}},
    scales:{{y:{{ticks:{{callback:v=>(v/1000).toFixed(0)+'M'}}}}}}
  }}
}});

new Chart(document.getElementById('deltaChart'), {{
  type: 'line',
  data: {{
    labels: data.mon_months,
    datasets: [
      {{label:'δ 実績（生）', data:data.mon_delta, borderColor:'#c13a3a',
        backgroundColor:'#c13a3a10', borderDash:[4,3], tension:0.2, yAxisID:'y'}},
      {{label:'構造δ（薬剤購入除去後）', data:data.mon_delta_struct, borderColor:'#1f77b4',
        backgroundColor:'#1f77b410', borderWidth:2, tension:0.2, yAxisID:'y'}},
      {{label:'非薬剤材料（右軸）', data:data.mon_nondrug, borderColor:'#9467bd',
        backgroundColor:'#9467bd10', tension:0.2, yAxisID:'y1'}},
    ]
  }},
  options: {{
    responsive:true,
    plugins:{{legend:{{position:'bottom'}}}},
    scales:{{
      y:{{position:'left',title:{{display:true,text:'δ (百万)'}},
          ticks:{{callback:v=>(v/1000).toFixed(0)}}}},
      y1:{{position:'right',title:{{display:true,text:'非薬剤材料 (百万)'}},
           grid:{{drawOnChartArea:false}},ticks:{{callback:v=>(v/1000).toFixed(0)}}}},
    }}
  }}
}});

new Chart(document.getElementById('balanceChart'), {{
  type: 'bar',
  data: {{
    labels: [...data.months, ...data.projections.map(p=>p.month)],
    datasets: [
      {{
        label:'医業収支 実績',
        data: [...data.series['医業収支'], ...data.projections.map(_=>null)],
        backgroundColor: data.series['医業収支'].map(v=>v>=0?'#0a8a3a':'#c13a3a'),
      }},
      {{
        label:'医業収支 予測',
        data: [...data.series['医業収支'].map(_=>null), ...data.projections.map(p=>p.balance)],
        backgroundColor: data.projections.map(p=>p.balance>=0?'#0a8a3a99':'#c13a3a99'),
        borderColor: '#333',
        borderWidth: 1,
      }},
    ]
  }},
  options: {{
    responsive:true,
    plugins:{{legend:{{position:'bottom'}}}},
    scales:{{y:{{ticks:{{callback:v=>(v/1000).toFixed(0)+'M'}}}}}}
  }}
}});
</script>

</body></html>"""


# ──────────────────────────────────────────
# main
# ──────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--output", default="output/pl_projection.html")
    ap.add_argument("--base-date", default=None,
                    help="YYYY-MM-DD（既定: データ最終日）")
    args = ap.parse_args()

    print("[1/5] PL_確定.xlsx 読込中...")
    pl_raw = load_pl_confirmed(args.data_dir)
    flags = quality_flags(pl_raw)
    bad = flags[flags["異常フラグ"]]
    excluded_months = []
    for _, row in bad.iterrows():
        reason = ("給与費=材料費 同値" if row["給与費=材料費"]
                  else f"検算誤差 {row['検算誤差']/1000:+,.0f}百万円")
        excluded_months.append({"month": row["月"].strftime("%Y-%m"),
                                "reason": reason})
    if excluded_months:
        print(f"  ⚠ 異常月を除外: "
              f"{', '.join(e['month'] for e in excluded_months)}")
    pl = clean_pl(pl_raw)
    print(f"  PL clean rows: {len(pl)}")

    print("[2/5] 粗利データ・日次データ読込中...")
    profit_breakdown = load_profit_breakdown(args.data_dir)
    adm_raw = load_admission_data(args.data_dir)
    surg_raw = load_surgery_data(args.data_dir)
    adm = preprocess_admission(adm_raw)
    surg = preprocess_surgery(surg_raw)
    g_monthly = aggregate_profit_monthly(profit_breakdown)
    delta = compute_delta_series(pl, g_monthly)
    print(f"  δ rows: {len(delta)} | δ率中央値: "
          f"{delta['δ率'].median():.2f}%")

    print("[3/5] 予測対象月の決定 + 当月/未確定前月 G 推計...")
    if args.base_date:
        # 明示指定時は従来どおり単月（その月のみ）。デバッグ・再現用。
        base_date = pd.Timestamp(args.base_date)
        driver_month = base_date.normalize().replace(day=1)
        target_months = [driver_month]
    else:
        # G は全ドライバーが揃う最終日で推計（adm/surg のどちらかが欠けた日を避ける）。
        # ダッシュボードと同一日になり G が一致する。
        base_date = last_complete_driver_date(adm, surg)
        driver_month = base_date.normalize().replace(day=1)
        # 起点は「PL確報（医業収支の実績）の最終月」の翌月〜ドライバー月。粗利確報基準だと
        # 粗利は確定したが PL 未着の月（粗利確定直後〜PL確報まで）が予測対象から外れ、
        # 実績にも無いため収支見込みが空白になる。PL 基準なら、その月も「確定粗利×推計
        # コスト」で出続け、PL確報で実績へ自動収束する（収束トリガー=PL確報）。
        last_pl_month = pl["月"].max()
        target_months = pd.date_range(last_pl_month + pd.offsets.MonthBegin(1),
                                      driver_month, freq="MS").tolist()
        if not target_months:
            target_months = [driver_month]
        target_months = target_months[-3:]  # データ遅延暴走防止（直近3か月上限）
    print(f"  基準日(ドライバー最終): {base_date.strftime('%Y-%m-%d')} / "
          f"予測対象: {', '.join(m.strftime('%Y-%m') for m in target_months)}")

    entries = []
    for m in target_months:
        month_end = m + pd.offsets.MonthEnd(0)
        base_date_m = min(base_date, month_end)
        is_complete = base_date_m >= month_end
        g_confirmed = bool((g_monthly["月"] == m).any())
        if g_confirmed:
            label = "粗利確定・PL確報待ち（確定粗利×推計コスト）"
        elif is_complete:
            label = "月末時点・粗利確報待ちの推計（暫定値）"
        else:
            label = f"進行中・基準日 {base_date_m.strftime('%Y-%m-%d')}"
        entry = build_one_projection(pl, delta, g_monthly, adm, surg,
                                      profit_breakdown, m, base_date_m, label)
        entries.append(entry)
        gm = entry["g_meta"]
        print(f"  [{m.strftime('%Y-%m')}] base={base_date_m.strftime('%Y-%m-%d')} "
              f"w={gm.get('mtd_blend_weight')} ×{gm['calibration_factor']} "
              f"G={entry['g_proj']/1000:,.0f}百万 "
              f"収支={entry['projection']['予測医業収支']/1000:+,.0f}百万")

    print("[4/5] バックテスト + 材料費モニタリング...")
    bt = backtest(pl, delta, g_monthly, n_holdout=24)
    monitor = material_cost_monitoring(pl, g_monthly)
    _mst = monitor["status"]
    print(f"  材料費モニタ [{_mst[0]}]: 構造δトレンド "
          f"{monitor['trends']['δ_構造']['slope_per_month']:+.1f}百万/月 "
          f"(R²={monitor['trends']['δ_構造']['r2']}) / 非薬剤材料 "
          f"{monitor['trends']['非薬剤材料']['annual']:+.0f}百万/年")
    for e in entries:
        pi = e["pi"]
        if pi.get("available"):
            print(f"  [{e['target_month'].strftime('%Y-%m')}] 予測区間 "
                  f"σ=±{pi['sigma']/1000:,.0f}百万円  "
                  f"80% PI: [{pi['pi80_lo']/1000:+,.0f}, {pi['pi80_hi']/1000:+,.0f}]")

    print("[5/5] 残差ログ + HTML 出力...")
    # 残差ログ追記（過去月の予測実施時のみ意味あり。当月/前月予測ではほぼ no-op）
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    residual_log = out_path.parent / "pl_residuals.csv"
    now_ts = pd.Timestamp(datetime.now())
    for e in entries:
        append_residual_log(str(residual_log), e["projection"], pl, now_ts)
    print(f"  残差ログ: {residual_log}")

    html = render_html(pl, delta, entries, bt, residual_log, {
        "generated_at": datetime.now().strftime("%Y/%m/%d %H:%M"),
        "base_date": base_date.strftime("%Y-%m-%d"),
        "excluded_months": excluded_months,
    }, monitor)
    out_path.write_text(html, encoding="utf-8")
    print(f"  → {out_path.resolve()}")
    print("\n完了。ブラウザで HTML を開いて確認してください。")


if __name__ == "__main__":
    main()
