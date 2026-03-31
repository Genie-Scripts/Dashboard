#!/usr/bin/env python3
"""
generate_html.py — 診療ダッシュボード 静的HTML生成スクリプト
=========================================================

⚠️  このスクリプトは「python コマンド」で直接実行してください。
    streamlit run generate_html.py とすると動作しません。

【基本的な使い方】
    python generate_html.py

【よく使うコマンド】
    python generate_html.py                            # 通常生成
    python generate_html.py --setup                    # 初回: データフォルダ作成
    python generate_html.py --data-dir /path/to/data  # データ場所を指定
    python generate_html.py --base-date 2026-03-26    # 基準日を指定
    python generate_html.py --dry-run                 # 検証のみ（出力なし）
    python generate_html.py --skip-reports            # 高速化（詳細ページスキップ）
    python generate_html.py --dept 整形外科            # 1科のみ詳細ページ再生成
    python generate_html.py --sort-by actual          # ランキングを実績数順に
    python generate_html.py --no-validate             # 検証スキップ（さらに高速化）

【Streamlit アプリの起動】（別のコマンド）
    streamlit run streamlit_app.py
"""

import sys
import os
import argparse
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── パス解決 ─────────────────────────────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from app.lib.config import DEFAULT_DATA_DIR, NADM_DISPLAY_DEPTS
from app.lib.data_loader import load_all, load_profit_data, load_profit_targets
from app.lib.preprocess import (
    preprocess_admission, preprocess_surgery,
    build_target_lookup, build_surgery_target_lookup,
)
from app.lib.metrics import (
    build_kpi_summary, build_dept_ranking, build_surgery_ranking,
    build_ward_ranking,
    build_doctor_watch_ranking, build_doctor_gap_ranking,
    build_nurse_watch_ranking, build_nurse_load_ranking,
)
from app.lib.profit import build_profit_monthly
from app.lib.charts import (
    build_all_chart_data, chart_data_to_json,
    build_doctor_chart_data, build_nurse_chart_data,
)
from app.lib.html_builder import build_template_context, build_dept_report_context
from app.lib.validate import run_all_checks, check_files


# ────────────────────────────────────────────────────
# CLI 引数定義
# ────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="診療ダッシュボード 静的HTML生成",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--data-dir",   default=DEFAULT_DATA_DIR, metavar="DIR",
                   help=f"データディレクトリ（デフォルト: {DEFAULT_DATA_DIR}）")
    p.add_argument("--output",     default="index.html", metavar="FILE",
                   help="トップページ出力先（デフォルト: index.html）")
    p.add_argument("--base-date",  default=None, metavar="YYYY-MM-DD",
                   help="基準日（省略時: データ内の最新日付）")
    p.add_argument("--sort-by",    default="achievement",
                   choices=["achievement", "actual"],
                   help="ランキング並び順（デフォルト: 達成率順）")
    p.add_argument("--dept",       default=None, metavar="診療科名",
                   help="指定した1科の詳細ページのみ再生成")
    p.add_argument("--skip-reports", action="store_true",
                   help="診療科別詳細ページ生成をスキップ")
    p.add_argument("--dry-run",    action="store_true",
                   help="データ検証のみ実行（HTML出力なし）")
    p.add_argument("--no-validate", action="store_true",
                   help="データ検証をスキップ（高速化）")
    p.add_argument("--quiet", "-q", action="store_true",
                   help="進捗ログを抑制")
    p.add_argument("--setup", action="store_true",
                   help="データフォルダを初期化して終了（初回セットアップ用）")
    return p.parse_args()


# ────────────────────────────────────────────────────
# ログユーティリティ
# ────────────────────────────────────────────────────

_VERBOSE = True

def log(msg: str, level: str = "info"):
    if not _VERBOSE:
        return
    ts = datetime.now().strftime("%H:%M:%S")
    icons = {"ok": "✅", "warn": "⚠️ ", "error": "❌", "step": "──"}
    icon = icons.get(level, "")
    sep  = "\n" if level == "step" else ""
    print(f"{sep}[{ts}] {icon} {msg}")


# ────────────────────────────────────────────────────
# データ読込・前処理（共通）
# ────────────────────────────────────────────────────

def _load_profit_safe(data: dict, data_dir: str):
    try:
        # data.get() は DataFrame を返す可能性があるため
        # bool評価できない → None チェックで判定する
        raw  = data.get("profit_data")
        tgts = data.get("profit_targets")
        if raw is None:
            raw  = load_profit_data(data_dir)
        if tgts is None:
            tgts = load_profit_targets(data_dir)
        return raw, tgts
    except Exception as e:
        import traceback as _tb
        log(f"粗利データ読込スキップ: {type(e).__name__}: {e}", "warn")
        if _VERBOSE:
            _tb.print_exc()
        return None, None


def load_and_preprocess(data_dir: str,
                         base_date_str: Optional[str] = None,
                         no_validate: bool = False):
    import pandas as pd

    # ファイル存在確認
    if not no_validate:
        log("データファイル確認中...", "step")
        fr = check_files(data_dir)
        fr.print_summary(verbose=_VERBOSE)
        fr.raise_if_error()

    # データ読込
    log("データ読込中...", "step")
    try:
        data = load_all(data_dir)
    except FileNotFoundError as e:
        log(str(e), "error")
        raise
    log(f"入院: {len(data['admission']):,} 行 / 手術: {len(data['surgery']):,} 件")

    # 前処理
    log("前処理中...", "step")
    adm  = preprocess_admission(data["admission"])
    surg = preprocess_surgery(data["surgery"])
    targets      = build_target_lookup(data["inpatient_targets"])
    surg_targets = build_surgery_target_lookup(data["surgery_targets"])

    # 基準日決定
    if base_date_str:
        try:
            base_date = pd.Timestamp(base_date_str)
        except ValueError:
            raise ValueError(f"基準日の形式が不正です（YYYY-MM-DD）: {base_date_str}")
        if base_date > adm["日付"].max():
            log(f"指定基準日 {base_date.date()} がデータ範囲外 → 最新日 {adm['日付'].max().date()} を使用", "warn")
            base_date = adm["日付"].max()
    else:
        base_date = adm["日付"].max()
    log(f"基準日: {base_date.date()}", "ok")

    # 検証
    profit_raw, profit_tgts = _load_profit_safe(data, data_dir)
    if not no_validate:
        log("データ整合性検証中...", "step")
        vr = run_all_checks(
            data_dir=data_dir,
            adm=adm, surg=surg,
            targets=targets, surg_targets=surg_targets,
            profit_data=profit_raw,
            profit_targets=profit_tgts,
            verbose=_VERBOSE,
        )
        vr.raise_if_error()

    # 粗利月次計算
    profit_monthly = None
    profit_raw_ok = (profit_raw is not None
                     and hasattr(profit_raw, '__len__')
                     and len(profit_raw) > 0)
    if profit_raw_ok:
        try:
            # profit_tgts が None の場合は空DataFrameで代替
            _tgts = profit_tgts if profit_tgts is not None else pd.DataFrame(
                columns=["診療科名", "月次目標"])
            profit_monthly = build_profit_monthly(profit_raw, _tgts)
            log(f"粗利: {len(profit_monthly):,} 行 "
                f"({profit_monthly['月'].min().strftime('%Y-%m')}"
                f"〜{profit_monthly['月'].max().strftime('%Y-%m')})")
        except Exception as e:
            log(f"粗利月次計算失敗（スキップ、粗利レポートなしで続行）: {e}", "warn")

    return adm, surg, targets, surg_targets, base_date, profit_monthly, data


# ────────────────────────────────────────────────────
# 各ページ生成
# ────────────────────────────────────────────────────

def _build_jinja_env():
    from jinja2 import Environment, FileSystemLoader
    env = Environment(
        loader=FileSystemLoader(str(ROOT / "app" / "templates")),
        autoescape=False,
    )
    env.filters["min"] = lambda lst: min(lst) if hasattr(lst, "__iter__") else lst

    # 安全な数値フォーマットフィルタ: テンプレート内で {{ val|numfmt(".1f") }}
    def _numfmt(v, fmt=".1f", fallback="—"):
        if v is None:
            return fallback
        try:
            return format(float(v), fmt)
        except (TypeError, ValueError):
            return fallback
    env.filters["numfmt"] = _numfmt

    # 安全な数値変換フィルタ: テンプレート内で {{ val|to_float }}
    def _to_float(v, default=0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default
    env.filters["to_float"] = _to_float

    return env


def _build_kpi_and_rankings(adm, surg, targets, surg_targets, base_date, sort_by, all_depts):
    """KPI・全ランキングを一括算出して返す"""
    kpi       = build_kpi_summary(adm, surg, base_date, targets, surg_targets)
    rank_inp  = build_dept_ranking(adm, base_date, targets, "inpatient",     sort_by)
    rank_nadm = build_dept_ranking(adm, base_date, targets, "new_admission", sort_by)
    rank_surg = build_surgery_ranking(surg, base_date, surg_targets, sort_by)
    rank_wi   = build_ward_ranking(adm, base_date, targets, "inpatient",     sort_by)
    rank_wn   = build_ward_ranking(adm, base_date, targets, "new_admission", sort_by)
    # 新ランキング
    doc_watch = build_doctor_watch_ranking(adm, surg, base_date, targets, surg_targets)
    doc_gap   = build_doctor_gap_ranking(adm, surg, base_date, targets, surg_targets)
    nur_watch = build_nurse_watch_ranking(adm, base_date, targets)
    nur_load  = build_nurse_load_ranking(adm, base_date)
    return {
        "kpi": kpi,
        "rank_inp": rank_inp, "rank_nadm": rank_nadm, "rank_surg": rank_surg,
        "rank_wi": rank_wi, "rank_wn": rank_wn,
        "doc_watch": doc_watch, "doc_gap": doc_gap,
        "nur_watch": nur_watch, "nur_load": nur_load,
    }


def _generate_doctor(ranks, chart_json, doctor_chart_json, all_depts, env, out_path: Path) -> Path:
    """doctor.html 生成"""
    from app.lib.html_builder import build_doctor_context
    ctx  = build_doctor_context(
        kpi=ranks["kpi"],
        dept_ranking_inp=ranks["rank_inp"], dept_ranking_nadm=ranks["rank_nadm"],
        surgery_ranking=ranks["rank_surg"],
        doctor_watch_rank=ranks["doc_watch"],
        doctor_gap_rank=ranks["doc_gap"],
        chart_data_json=chart_json,
        doctor_chart_json=doctor_chart_json,
        all_depts=all_depts,
        generated_at=datetime.now(),
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html = env.get_template("doctor.html").render(**ctx)
    out_path.write_text(html, encoding="utf-8")
    log(f"doctor.html → {out_path}  ({out_path.stat().st_size // 1024} KB)", "ok")
    return out_path


def _generate_admission(data_dir: str, out_path: Path) -> Path:
    """
    新入院患者ダッシュボード HTML を生成する。
    app.py の load_and_process_from_dir / generate_html を再利用。
    """
    import importlib.util, sys as _sys

    # admission_app.py をモジュールとして動的ロード
    spec = importlib.util.spec_from_file_location(
        "admission_app", str(ROOT / "admission_app.py"))
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    kpi, chart_data, perf, meta, dept_chart, dept_targets, ward_chart, ward_targets =         mod.load_and_process_from_dir(data_dir)

    html_str = mod.generate_html(kpi, chart_data, perf, meta, dept_chart, dept_targets, ward_chart, ward_targets)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_str, encoding="utf-8")
    log(f"admission/index.html → {out_path}  ({out_path.stat().st_size // 1024} KB)", "ok")
    return out_path


def _generate_inpatient(data_dir: str, out_path: Path) -> Path:
    """
    在院患者数ダッシュボード HTML を生成する。
    inpatient_app.py の load_and_process_from_dir / generate_html を再利用。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "inpatient_app", str(ROOT / "inpatient_app.py"))
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    kpi, chart_data, perf, meta, dept_chart, dept_targets, ward_chart, ward_targets = \
        mod.load_and_process_from_dir(data_dir)

    html_str = mod.generate_html(kpi, chart_data, perf, meta, dept_chart, dept_targets, ward_chart, ward_targets)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_str, encoding="utf-8")
    log(f"inpatient/index.html → {out_path}  ({out_path.stat().st_size // 1024} KB)", "ok")
    return out_path


def _generate_portal(kpi: dict, base_date, generated_at: datetime,
                      admission_html_exists: bool, inpatient_html_exists: bool,
                      env, out_path: Path) -> Path:
    """portal.html 生成"""
    from app.lib.html_builder import build_kpi_card_data

    cards = build_kpi_card_data(kpi)

    # ポータル用に4指標をサマリー化
    portal_kpis = []
    for card in cards:
        if card["id"] in ("inpatient", "new_admission_7d", "surgery"):
            portal_kpis.append(card)

    ctx = {
        "generated_at":           generated_at.strftime("%Y/%m/%d %H:%M"),
        "base_date":              base_date.strftime("%Y/%m/%d"),
        "base_date_raw":          base_date.strftime("%Y-%m-%d"),
        "portal_kpis":            portal_kpis,
        "admission_html_exists":  admission_html_exists,
        "inpatient_html_exists":  inpatient_html_exists,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html = env.get_template("portal.html").render(**ctx)
    out_path.write_text(html, encoding="utf-8")
    log(f"portal.html → {out_path}  ({out_path.stat().st_size // 1024} KB)", "ok")
    return out_path


def _generate_nurse(ranks, chart_json, nurse_chart_json, env, out_path: Path) -> Path:
    """nurse.html 生成"""
    from app.lib.html_builder import build_nurse_context
    ctx  = build_nurse_context(
        kpi=ranks["kpi"],
        ward_ranking_inp=ranks["rank_wi"], ward_ranking_nadm=ranks["rank_wn"],
        nurse_watch_rank=ranks["nur_watch"],
        nurse_load_rank=ranks["nur_load"],
        chart_data_json=chart_json,
        nurse_chart_json=nurse_chart_json,
        generated_at=datetime.now(),
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html = env.get_template("nurse.html").render(**ctx)
    out_path.write_text(html, encoding="utf-8")
    log(f"nurse.html → {out_path}  ({out_path.stat().st_size // 1024} KB)", "ok")
    return out_path


def _build_dept_index_html(depts: list, generated_at: str, base_date: str) -> str:
    """
    reports/index.html を Python 文字列として生成する。
    Jinja2 テンプレートファイルに依存しないためどの環境でも動作する。
    depts: [{"name", "filename", "inp_status", "inp_ach",
              "nadm_status", "nadm_ach", "surg_status", "surg_ach",
              "profit_status", "profit_ach"}, ...]
    """

    def _badge(status):
        labels = {"ok": "達成", "warn": "注意", "ng": "未達"}
        return labels.get(status, "—")

    def _pct(val):
        if val is None or val == 0:
            return "—"
        return f"{val:.0f}%"

    def _bar_w(val):
        if val is None:
            return "0"
        return f"{min(max(val, 0), 120) / 120 * 100:.0f}"

    rows_html = ""
    for d in depts:
        overall = d["inp_status"]
        rows_html += f"""
  <a href="{d['filename']}" class="dept-card {overall}" data-name="{d['name']}">
    <div class="dept-card-head">
      <div class="dept-name">{d['name']}</div>
      <span class="dept-badge {overall}">{_badge(overall)}</span>
    </div>
    <div class="kpi-mini">
      <div class="kpi-mini-item">
        <div class="kmi-label">在院</div>
        <div class="kmi-value {d['inp_status']}">{_pct(d['inp_ach'])}</div>
      </div>
      <div class="kpi-mini-item">
        <div class="kmi-label">新入院</div>
        <div class="kmi-value {d['nadm_status']}">{_pct(d['nadm_ach'])}</div>
      </div>
      <div class="kpi-mini-item">
        <div class="kmi-label">全麻</div>
        <div class="kmi-value {d['surg_status']}">{_pct(d['surg_ach'])}</div>
      </div>
      <div class="kpi-mini-item">
        <div class="kmi-label">粗利</div>
        <div class="kmi-value {d['profit_status']}">{_pct(d['profit_ach'])}</div>
      </div>
    </div>
    <div class="bar-wrap">
      <div class="bar-track"><div class="bar-fill {d['inp_status']}"    style="width:{_bar_w(d['inp_ach'])}%"></div></div>
      <div class="bar-track"><div class="bar-fill {d['nadm_status']}"   style="width:{_bar_w(d['nadm_ach'])}%"></div></div>
      <div class="bar-track"><div class="bar-fill {d['surg_status']}"   style="width:{_bar_w(d['surg_ach'])}%"></div></div>
      <div class="bar-track"><div class="bar-fill {d['profit_status']}" style="width:{_bar_w(d['profit_ach'])}%"></div></div>
    </div>
  </a>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>診療科別レポート一覧 | 診療ダッシュボード</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@300;400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{{margin:0;padding:0;box-sizing:border-box}}
html{{scroll-behavior:smooth}}
body{{font-family:'Noto Sans JP',sans-serif;background:var(--bg);color:var(--txt);font-size:14px;line-height:1.6;min-height:100vh}}
:root{{
  --bg:#F0F2F5;--card:#FFF;--elevated:#F7F9FC;
  --hdr:#1D2B3A;--border:#DCE1E9;--accent:#3A6EA5;--accent-lt:#EEF3FA;
  --txt:#1A2535;--txt2:#5A6A82;--muted:#94A3B8;
  --ok:#1A9E6A;--warn:#C87A00;--ng:#C0293B;
  --status-good:#16a34a;--status-warn:#d97706;--status-bad:#dc2626;
  --mono:'IBM Plex Mono',monospace;--r:10px;
  --shadow:0 1px 4px rgba(0,0,0,.06);--shadow2:0 4px 16px rgba(0,0,0,.10);
}}
.hdr{{background:var(--hdr);padding:0 28px;height:56px;display:flex;align-items:center;justify-content:space-between;border-bottom:2px solid var(--accent);position:sticky;top:0;z-index:100}}
.hdr-left{{display:flex;align-items:center;gap:12px}}
.hdr-badge{{background:var(--accent);color:#fff;font-size:.58rem;font-weight:700;letter-spacing:.14em;text-transform:uppercase;padding:3px 9px;border-radius:4px}}
.hdr-title{{color:#E8EEF5;font-size:1rem;font-weight:600}}
.hdr-meta{{color:#7A90A8;font-size:.72rem;font-family:var(--mono)}}
.hdr-nav a{{color:#7A90A8;font-size:.78rem;text-decoration:none;padding:6px 12px;border-radius:6px;border:1px solid rgba(255,255,255,.1);transition:all .15s}}
.hdr-nav a:hover{{background:rgba(255,255,255,.08);color:#E8EEF5}}
.wrapper{{max-width:1100px;margin:0 auto;padding:28px 24px 64px}}
.section-label{{font-size:.67rem;font-weight:700;letter-spacing:.14em;color:var(--muted);text-transform:uppercase;display:flex;align-items:center;gap:10px;margin-bottom:16px}}
.section-label::after{{content:'';flex:1;height:1px;background:var(--border)}}
.search-input{{width:100%;max-width:360px;padding:8px 14px;border:1px solid var(--border);border-radius:var(--r);background:var(--card);color:var(--txt);font-size:.88rem;font-family:'Noto Sans JP',sans-serif;outline:none;transition:border-color .15s;margin-bottom:20px;display:block}}
.search-input:focus{{border-color:var(--accent)}}
.legend{{display:flex;gap:16px;flex-wrap:wrap;font-size:.72rem;color:var(--txt2);margin-bottom:18px;align-items:center}}
.legend-dot{{width:8px;height:8px;border-radius:50%;flex-shrink:0;display:inline-block;margin-right:4px}}
.dept-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}}
.dept-card{{background:var(--card);border-radius:var(--r);border:1px solid var(--border);border-left:4px solid var(--muted);box-shadow:var(--shadow);text-decoration:none;color:inherit;display:block;padding:16px 18px 14px;transition:transform .15s,box-shadow .15s}}
.dept-card:hover{{transform:translateY(-2px);box-shadow:var(--shadow2)}}
.dept-card.ok{{border-left-color:var(--status-good)}}
.dept-card.warn{{border-left-color:var(--status-warn)}}
.dept-card.ng{{border-left-color:var(--status-bad)}}
.dept-card.hidden{{display:none}}
.dept-card-head{{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}}
.dept-name{{font-size:.96rem;font-weight:700;color:var(--txt)}}
.dept-badge{{font-size:.65rem;font-weight:700;padding:2px 9px;border-radius:999px;white-space:nowrap}}
.dept-badge.ok{{background:#dcfce7;color:#166534}}
.dept-badge.warn{{background:#fef3c7;color:#92400e}}
.dept-badge.ng{{background:#fee2e2;color:#991b1b}}
.dept-badge.neutral{{background:var(--elevated);color:var(--muted)}}
.kpi-mini{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:10px}}
.kpi-mini-item{{text-align:center}}
.kmi-label{{font-size:.6rem;font-weight:700;letter-spacing:.06em;color:var(--muted);text-transform:uppercase;margin-bottom:2px}}
.kmi-value{{font-size:.82rem;font-weight:700;font-family:var(--mono)}}
.kmi-value.ok{{color:var(--status-good)}}
.kmi-value.warn{{color:var(--status-warn)}}
.kmi-value.ng{{color:var(--status-bad)}}
.kmi-value.neutral{{color:var(--muted)}}
.bar-wrap{{display:grid;grid-template-columns:repeat(4,1fr);gap:4px}}
.bar-track{{height:3px;background:var(--elevated);border-radius:99px;overflow:hidden}}
.bar-fill{{height:100%;border-radius:99px;transition:width .6s}}
.bar-fill.ok{{background:var(--status-good)}}
.bar-fill.warn{{background:var(--status-warn)}}
.bar-fill.ng{{background:var(--status-bad)}}
.bar-fill.neutral{{background:var(--muted)}}
.page-footer{{margin-top:32px;padding-top:16px;border-top:1px solid var(--border);font-size:.72rem;color:var(--muted);display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px}}
@media(max-width:640px){{.dept-grid{{grid-template-columns:1fr}}.hdr-meta{{display:none}}.wrapper{{padding:16px 14px 48px}}}}
</style>
</head>
<body>
<header class="hdr">
  <div class="hdr-left">
    <span class="hdr-badge">科別</span>
    <span class="hdr-title">📋 診療科別レポート一覧</span>
  </div>
  <div style="display:flex;align-items:center;gap:16px">
    <nav class="hdr-nav">
      <a href="../portal.html">🏠 ポータル</a>
      <a href="../doctor.html">🩺 医師版</a>
      <a href="../nurse.html">👩‍⚕️ 看護師版</a>
    </nav>
    <span class="hdr-meta">📅 {generated_at} 更新 ｜ 基準日: {base_date}</span>
  </div>
</header>
<div class="wrapper">
  <div class="legend">
    <span style="font-weight:600;color:var(--txt)">達成率（在院患者数基準）</span>
    <span><span class="legend-dot" style="background:var(--status-good)"></span>≥ 105%（達成）</span>
    <span><span class="legend-dot" style="background:var(--status-warn)"></span>95〜104%（注意）</span>
    <span><span class="legend-dot" style="background:var(--status-bad)"></span>&lt; 95%（未達）</span>
  </div>
  <input type="text" class="search-input" placeholder="🔍 診療科名で絞り込み..." oninput="filterCards(this.value)" />
  <div class="section-label">全 {len(depts)} 診療科</div>
  <div class="dept-grid" id="deptGrid">{rows_html}
  </div>
  <div class="page-footer">
    <span>📋 診療科別レポート一覧</span>
    <span>生成: {generated_at} ｜ 基準日: {base_date}</span>
  </div>
</div>
<script>
function filterCards(q) {{
  const term = q.trim().toLowerCase();
  document.querySelectorAll('#deptGrid .dept-card').forEach(card => {{
    const name = (card.dataset.name || '').toLowerCase();
    card.classList.toggle('hidden', term !== '' && !name.includes(term));
  }});
}}
</script>
</body>
</html>"""


def _generate_dept_reports(adm, surg, targets, surg_targets, profit_monthly,
                             base_date, all_depts, env, out_dir: Path,
                             target_dept: Optional[str] = None) -> list:
    import pandas as pd

    report_dir = out_dir / "reports"
    report_dir.mkdir(exist_ok=True)

    depts = [target_dept] if target_dept else all_depts
    if target_dept and target_dept not in all_depts:
        log(f"診療科 '{target_dept}' がデータにありません。利用可能: {all_depts}", "error")
        return []

    log(f"診療科別詳細ページ生成中 ({len(depts)} 科)...", "step")
    tmpl = env.get_template("dept_report.html")
    # 粗利データがない場合でも build_dept_report_context が安全に動くよう
    # 必要な列を持った空DataFrameを渡す
    empty_df = pd.DataFrame(columns=["診療科名", "月", "粗利", "月次目標", "達成率", "前月比"])
    generated, skipped = [], []
    index_rows = []   # reports/index.html 用サマリー

    for dept in depts:
        try:
            ctx = build_dept_report_context(
                dept_name=dept, adm=adm, surg=surg,
                targets=targets, surg_targets=surg_targets,
                profit_monthly=profit_monthly if profit_monthly is not None else empty_df,
                base_date=base_date, generated_at=datetime.now(),
            )
            html  = tmpl.render(**ctx)
            fname = dept.replace("/", "_").replace(" ", "_")
            path  = report_dir / f"dept_{fname}.html"
            path.write_text(html, encoding="utf-8")
            generated.append(path)
            # インデックス用にKPIサマリーを収集（NADM_DISPLAY_DEPTS の23科のみ）
            if dept in NADM_DISPLAY_DEPTS:
                index_rows.append({
                    "name":         dept,
                    "filename":     f"dept_{fname}.html",
                    "inp_ach":      ctx["inp_achievement"],
                    "inp_status":   ctx["inp_status"],
                    "nadm_ach":     ctx["nadm_progress"],
                    "nadm_status":  ctx["nadm_status"],
                    "surg_ach":     ctx["surg_achievement"] if ctx["has_surgery"] else None,
                    "surg_status":  ctx["surg_status"]      if ctx["has_surgery"] else "neutral",
                    "profit_ach":   ctx["profit_achievement"] if ctx["profit_status"] != "neutral" else None,
                    "profit_status":ctx["profit_status"],
                })
        except Exception as e:
            log(f"{dept}: スキップ ({type(e).__name__}: {e})", "warn")
            skipped.append(dept)

    # reports/index.html 生成（全診療科ビルド時のみ）
    if not target_dept and index_rows:
        try:
            idx_html = _build_dept_index_html(
                depts=sorted(index_rows, key=lambda r: r["name"]),
                generated_at=datetime.now().strftime("%Y/%m/%d %H:%M"),
                base_date=base_date.strftime("%Y/%m/%d"),
            )
            idx_path = report_dir / "index.html"
            idx_path.write_text(idx_html, encoding="utf-8")
            log(f"reports/index.html → {idx_path}", "ok")
        except Exception as e:
            log(f"reports/index.html 生成スキップ: {e}", "warn")

    log(f"詳細ページ → {report_dir}/ "
        f"({len(generated)} 件"
        + (f", {len(skipped)} 件スキップ" if skipped else "")
        + ")", "ok")
    return generated


# ────────────────────────────────────────────────────
# メイン生成関数（外部から import して使える）
# ────────────────────────────────────────────────────

def generate(data_dir: str = DEFAULT_DATA_DIR,
             output: str = "index.html",
             base_date_str: Optional[str] = None,
             sort_by: str = "achievement",
             skip_reports: bool = False,
             target_dept: Optional[str] = None,
             dry_run: bool = False,
             no_validate: bool = False,
             verbose: bool = True) -> dict:
    """
    ダッシュボードHTMLを生成する。

    Returns:
        {"index": Path|None, "reports": list[Path],
         "base_date": Timestamp, "elapsed_sec": float}
    """
    global _VERBOSE
    _VERBOSE = verbose

    t0 = datetime.now()
    log(f"診療ダッシュボード HTML生成 開始 — {t0.strftime('%Y/%m/%d %H:%M:%S')}")
    if dry_run:
        log("【DRY-RUNモード】検証のみ実行（HTML出力なし）", "warn")

    # ── 1. データ読込・前処理 ─────────────────────────
    adm, surg, targets, surg_targets, base_date, profit_monthly, data = \
        load_and_preprocess(data_dir, base_date_str, no_validate)

    if dry_run:
        elapsed = (datetime.now() - t0).total_seconds()
        log(f"DRY-RUN完了 ({elapsed:.1f}秒)", "ok")
        return {"index": None, "reports": [], "base_date": base_date, "elapsed_sec": elapsed}

    # ── 2. グラフデータ ───────────────────────────────
    log("グラフデータ生成中...", "step")
    all_depts = sorted(adm[adm["科_表示"]]["診療科名"].dropna().unique().tolist())
    chart_data = build_all_chart_data(adm, surg, base_date, targets, surg_targets, all_depts)
    chart_json = chart_data_to_json(chart_data)
    log(f"グラフJSON: {len(chart_json):,} bytes ({len(all_depts)} 科)")

    # ── 2b. 役割別チャートデータ（Phase 2）────────────
    log("役割別チャートデータ生成中...", "step")
    doctor_chart = build_doctor_chart_data(
        adm, surg, base_date, targets, surg_targets, all_depts)
    nurse_chart  = build_nurse_chart_data(adm, base_date, targets)
    doctor_chart_json = chart_data_to_json(doctor_chart)
    nurse_chart_json  = chart_data_to_json(nurse_chart)
    log(f"医師版チャートJSON: {len(doctor_chart_json):,} bytes / "
        f"看護師版: {len(nurse_chart_json):,} bytes")

    # ── 3. Jinja2環境 ─────────────────────────────────
    env      = _build_jinja_env()
    out_path = Path(output)
    # output が index.html 等旧名の場合は出力ディレクトリのみ使用
    out_dir  = out_path.parent

    # ── 3b. JSON ファイル出力 ─────────────────────────
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "doctor_summary.json").write_text(
        doctor_chart_json, encoding="utf-8")
    (out_dir / "nurse_summary.json").write_text(
        nurse_chart_json, encoding="utf-8")
    log(f"doctor_summary.json / nurse_summary.json → {out_dir}/", "ok")

    # ── 4. KPI・ランキング算出 ────────────────────────
    log("KPI・ランキング算出中...", "step")
    ranks = _build_kpi_and_rankings(
        adm, surg, targets, surg_targets, base_date, sort_by, all_depts)

    # ── 4b. doctor.html / nurse.html 生成 ─────────────
    log("doctor.html レンダリング中...", "step")
    doctor_path = _generate_doctor(
        ranks, chart_json, doctor_chart_json, all_depts, env, out_dir / "doctor.html")

    log("nurse.html レンダリング中...", "step")
    nurse_path = _generate_nurse(
        ranks, chart_json, nurse_chart_json, env, out_dir / "nurse.html")

    # ── 4c. admission/index.html 生成 ─────────────────
    log("admission/index.html（新入院ダッシュボード）レンダリング中...", "step")
    try:
        admission_path = _generate_admission(
            data_dir=data_dir,
            out_path=out_dir / "admission" / "index.html",
        )
        admission_exists = True
    except Exception as e:
        log(f"admission/index.html 生成スキップ: {type(e).__name__}: {e}", "warn")
        admission_path  = None
        admission_exists = False

    # ── 4c-2. inpatient/index.html 生成 ───────────────
    log("inpatient/index.html（在院患者ダッシュボード）レンダリング中...", "step")
    try:
        inpatient_path = _generate_inpatient(
            data_dir=data_dir,
            out_path=out_dir / "inpatient" / "index.html",
        )
        inpatient_exists = True
    except Exception as e:
        log(f"inpatient/index.html 生成スキップ: {type(e).__name__}: {e}", "warn")
        inpatient_path  = None
        inpatient_exists = False

    # ── 4d. portal.html 生成 ──────────────────────────
    log("portal.html レンダリング中...", "step")
    portal_path = _generate_portal(
        kpi=ranks["kpi"],
        base_date=base_date,
        generated_at=datetime.now(),
        admission_html_exists=admission_exists,
        inpatient_html_exists=inpatient_exists,
        env=env,
        out_path=out_dir / "portal.html",
    )

    # ── 5. 診療科別詳細ページ ─────────────────────────
    reports = []
    if not skip_reports:
        reports = _generate_dept_reports(
            adm, surg, targets, surg_targets, profit_monthly,
            base_date, all_depts, env,
            out_dir=out_dir,
            target_dept=target_dept,
        )

    elapsed = (datetime.now() - t0).total_seconds()

    # ── 6. 完了サマリー ───────────────────────────────
    print("\n" + "="*60)
    print("  🏥 診療ダッシュボード 生成完了")
    print("="*60)
    print(f"  基準日      : {base_date.date()}")
    print(f"  portal.html          : {portal_path.resolve()}")
    print(f"  doctor.html          : {doctor_path.resolve()}")
    print(f"  nurse.html           : {nurse_path.resolve()}")
    if admission_path:
        print(f"  admission/index.html : {admission_path.resolve()}")
    if inpatient_path:
        print(f"  inpatient/index.html : {inpatient_path.resolve()}")
    if reports:
        print(f"  詳細ページ  : {len(reports)} 件  ({reports[0].parent}/)")
    print(f"  処理時間    : {elapsed:.1f} 秒")
    print("="*60)
    print(f"\n  ポータル          : file://{portal_path.resolve()}")
    if admission_path:
        print(f"  新入院ダッシュボード: file://{admission_path.resolve()}")
    print(f"  医師版            : file://{doctor_path.resolve()}")
    print(f"  看護師版          : file://{nurse_path.resolve()}\n")

    return {
        "portal":      portal_path,
        "doctor":      doctor_path,
        "nurse":       nurse_path,
        "admission":   admission_path,
        "inpatient":   inpatient_path,
        "reports":     reports,
        "base_date":   base_date,
        "elapsed_sec": elapsed,
    }


# ────────────────────────────────────────────────────
# CLI エントリポイント
# ────────────────────────────────────────────────────

def main():
    args = parse_args()

    # --setup: データフォルダ初期化
    if args.setup:
        from app.lib.data_loader import setup_data_dir
        print(f"\n📁 データフォルダを初期化します: {args.data_dir}\n")
        setup_data_dir(args.data_dir)
        sys.exit(0)

    try:
        generate(
            data_dir     = args.data_dir,
            output       = args.output,
            base_date_str= args.base_date,
            sort_by      = args.sort_by,
            skip_reports = args.skip_reports,
            target_dept  = args.dept,
            dry_run      = args.dry_run,
            no_validate  = args.no_validate,
            verbose      = not args.quiet,
        )
        sys.exit(0)

    except FileNotFoundError as e:
        print(f"\n❌ ファイルが見つかりません: {e}")
        print(f"   --data-dir でデータフォルダを指定してください。")
        print(f"   例: python generate_html.py --data-dir /Users/name/data")
        sys.exit(1)
    except ValueError as e:
        print(f"\n❌ 入力値エラー: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\n⚠️  中断されました。")
        sys.exit(130)
    except Exception as e:
        print(f"\n❌ エラー: {type(e).__name__}: {e}")
        if not args.quiet:
            print("\n--- スタックトレース ---")
            traceback.print_exc()
        print("\n💡 --dry-run で検証のみ実行して原因を絞り込めます。")
        sys.exit(1)


if __name__ == "__main__":
    main()
