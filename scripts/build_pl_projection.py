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
from app.lib.pl_history import load_pl_history, clean_pl, quality_flags  # noqa: E402
from app.lib.pl_projection import (  # noqa: E402
    aggregate_profit_monthly,
    compute_delta_series,
    project_monthly_balance,
    backtest,
    prediction_intervals,
    append_residual_log,
)
from app.lib.profit_estimate import build_hybrid_payload  # noqa: E402


# ──────────────────────────────────────────
# G_proj 取得（粗利推計 latest_projection_total）
# ──────────────────────────────────────────

def fetch_profit_projection(adm: pd.DataFrame,
                              surg: pd.DataFrame,
                              profit_breakdown: pd.DataFrame,
                              base_date: pd.Timestamp) -> tuple[float, dict]:
    """build_hybrid_payload を呼び、当月末予測 G を百万円→千円に換算して返す。"""
    payload = build_hybrid_payload(
        profit_breakdown=profit_breakdown,
        surg=surg,
        base_date=base_date,
        adm=adm,
    )
    if not payload:
        raise RuntimeError("粗利推計ペイロードが空でした")
    series_meta = payload.get("meta") or {}
    # latest_projection_total は百万円（当月末見込み = 直近30日推計 × 当月営業日/窓内営業日）
    proj_mil = series_meta.get("latest_projection_total")
    if proj_mil is None:
        raise RuntimeError(
            "meta.latest_projection_total が取得できませんでした "
            "(hospital_total.hybrid_pred は前月バックテスト値なので G の代用にしない)"
        )
    return float(proj_mil) * 1000.0, {  # 千円
        "latest_projection_total_million": float(proj_mil),
        "base_date": base_date.strftime("%Y-%m-%d"),
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
                projection: dict,
                bt: pd.DataFrame,
                pi: dict,
                residual_log_path: Path,
                meta: dict) -> str:
    """シンプル独立 HTML（ローカル閲覧専用）"""
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

    # 予測値
    proj_month = projection["月"].strftime("%Y-%m")
    proj_balance = projection["予測医業収支"]
    proj_revenue = projection["予測R_minus_M"]

    # 予測区間
    if pi.get("available"):
        pi_text = (f"<div class='sub'>"
                   f"σ = ±{pi['sigma']/1000:,.0f}百万円 / "
                   f"80% PI [{pi['pi80_lo']/1000:+,.0f}, {pi['pi80_hi']/1000:+,.0f}] / "
                   f"95% PI [{pi['pi95_lo']/1000:+,.0f}, {pi['pi95_hi']/1000:+,.0f}] 百万円"
                   f"</div>")
        pi80_lo = pi["pi80_lo"]
        pi80_hi = pi["pi80_hi"]
        pi95_lo = pi["pi95_lo"]
        pi95_hi = pi["pi95_hi"]
    else:
        pi_text = "<div class='sub'>予測区間: バックテスト不足</div>"
        pi80_lo = pi80_hi = pi95_lo = pi95_hi = None
    parts = {
        "G": projection["G"],
        "δ": projection["δ"],
        "給与費": projection["給与費"],
        "委託費": projection["委託費"],
        "設備関係費": projection["設備関係費"],
        "経費": projection["経費"],
    }

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

    # チャート用 JSON
    chart_data = {
        "months": months_iso,
        "series": series,
        "projection_month": proj_month,
        "projection_balance": float(proj_balance),
        "pi80_lo": pi80_lo,
        "pi80_hi": pi80_hi,
        "pi95_lo": pi95_lo,
        "pi95_hi": pi95_hi,
        "bt_months": bt_months,
        "bt_actual": bt_actual,
        "bt_pred": bt_pred,
    }

    rows_recent = pl_clean.tail(6).copy()
    rows_recent["月_str"] = rows_recent["月"].dt.strftime("%Y-%m")
    table_rows = "\n".join(
        f"<tr><td>{r['月_str']}</td>"
        f"<td>{r['医業収益']:,.0f}</td>"
        f"<td>{r['材料費']:,.0f}</td>"
        f"<td>{r['給与費']:,.0f}</td>"
        f"<td>{r['委託費']:,.0f}</td>"
        f"<td>{r['設備関係費']:,.0f}</td>"
        f"<td>{r['経費']:,.0f}</td>"
        f"<td style='color:{_color_balance(r['医業収支'])};font-weight:600'>"
        f"{r['医業収支']:+,.0f}</td></tr>"
        for _, r in rows_recent.iterrows()
    )

    # 予測内訳テーブル
    parts_rows = []
    for label, info in [
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
            f"<tr><td>{label}</td><td style='text-align:right'>{v:+,.0f}</td>"
            f"<td style='color:#888;font-size:0.85em'>{method}</td></tr>"
        )
    parts_rows.append(
        f"<tr style='border-top:2px solid #333;font-weight:700'>"
        f"<td>医業収支 予測</td>"
        f"<td style='text-align:right;color:{_color_balance(proj_balance)}'>"
        f"{proj_balance:+,.0f} 千円</td><td></td></tr>"
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
    g_source = meta.get("g_source", "")

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
</style>
</head>
<body>

<h1>医業収支 推計レポート <span class="sub">(ローカル閲覧専用 / 公開しない)</span></h1>
<p class="sub">生成: {gen} | 基準日: {base_date}</p>

<div class="note">
本レポートは <code>data/PL.xlsx</code>（月次確報）と粗利推計を組み合わせた
医業収支の月末予測です。費目モデルの誤差により絶対値は±25-50百万円程度の振れを想定。
方向性把握（黒字／赤字／前月比）が主目的です。
</div>

<h2>📅 当月予測サマリ ({proj_month})</h2>
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

<h2>予測内訳（千円）</h2>
<div class="card">
<table>
<thead><tr><th>項目</th><th>値</th><th>算出方法</th></tr></thead>
<tbody>
{chr(10).join(parts_rows)}
</tbody>
</table>
<p class="sub" style="margin-top:8px">G ({g_source}) は粗利推計 latest_projection_total から取得。
δ は (R−M) − G の構造的差分（DPC包括分等の購入差）。</p>
</div>

<h2>過去6か月の PL 実績（千円）</h2>
<div class="card">
<table>
<thead><tr><th>月</th><th>医業収益</th><th>材料費</th><th>給与費</th>
<th>委託費</th><th>設備関係費</th><th>経費</th><th>医業収支</th></tr></thead>
<tbody>
{table_rows}
</tbody>
</table>
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

new Chart(document.getElementById('balanceChart'), {{
  type: 'bar',
  data: {{
    labels: [...data.months, data.projection_month],
    datasets: [
      {{
        label:'医業収支 実績',
        data: [...data.series['医業収支'], null],
        backgroundColor: data.series['医業収支'].map(v=>v>=0?'#0a8a3a':'#c13a3a'),
      }},
      {{
        label:'医業収支 予測',
        data: [...data.series['医業収支'].map(_=>null), data.projection_balance],
        backgroundColor: data.projection_balance>=0?'#0a8a3a99':'#c13a3a99',
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

    print("[1/5] PL.xlsx 読込中...")
    pl_raw = load_pl_history(args.data_dir)
    flags = quality_flags(pl_raw)
    bad = flags[flags["異常フラグ"]]
    if len(bad) > 0:
        print(f"  ⚠ 異常月を除外: "
              f"{', '.join(bad['月'].dt.strftime('%Y-%m').tolist())}")
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

    print("[3/5] 当月 G 推計（粗利推計から）...")
    if args.base_date:
        base_date = pd.Timestamp(args.base_date)
    else:
        base_date = pd.Timestamp(max(adm["日付"].max(), surg["手術実施日"].max()))
    print(f"  基準日: {base_date.strftime('%Y-%m-%d')}")
    g_proj, g_meta = fetch_profit_projection(adm, surg, profit_breakdown, base_date)
    print(f"  G_proj: {g_proj:,.0f} 千円 ({g_proj/1000:.0f} 百万円)")

    target_month = base_date.normalize().replace(day=1)
    print(f"[4/5] {target_month.strftime('%Y-%m')} 医業収支予測...")
    projection = project_monthly_balance(pl, delta, g_monthly,
                                          target_month, g_override=g_proj)
    print(f"  予測医業収支: {projection['予測医業収支']/1000:+,.0f} 百万円")

    print("[5/5] バックテスト + 予測区間 + HTML 出力...")
    bt = backtest(pl, delta, g_monthly, n_holdout=8)
    pi = prediction_intervals(pl, delta, g_monthly, target_month, projection)
    if pi.get("available"):
        print(f"  予測区間 σ=±{pi['sigma']/1000:,.0f}百万円  "
              f"80% PI: [{pi['pi80_lo']/1000:+,.0f}, {pi['pi80_hi']/1000:+,.0f}]")

    # 残差ログ追記（過去月の予測実施時のみ意味あり。当月予測ではほぼ no-op）
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    residual_log = out_path.parent / "pl_residuals.csv"
    append_residual_log(str(residual_log), projection, pl,
                          pd.Timestamp(datetime.now()))
    print(f"  残差ログ: {residual_log}")

    html = render_html(pl, delta, projection, bt, pi, residual_log, {
        "generated_at": datetime.now().strftime("%Y/%m/%d %H:%M"),
        "base_date": base_date.strftime("%Y-%m-%d"),
        "g_source": "粗利推計 latest_projection_total",
    })
    out_path.write_text(html, encoding="utf-8")
    print(f"  → {out_path.resolve()}")
    print("\n完了。ブラウザで HTML を開いて確認してください。")


if __name__ == "__main__":
    main()
