#!/usr/bin/env python3
"""build_hospital_report.py — 全病院 実績まとめPDF（詳細・印刷/資料室リンク用）。

Comedix週報サマリーPNG（軽量）の詳細版。共通部品 app/lib/hospital_summary を使い、
A4縦で:
  P1 = ヘッダ＋portal準拠KPI3＋ヘッドライン＋今週の一手
  P2 = 12週トレンド3枚（在院7日MA／新入院 週次ラン／全麻 30営業平日MA・前年同期線つき）
  P3 = 病棟別テーブル（在院 実/目・病床利用率・入退院フロー・週末在院維持率）
  P4 = 診療科別テーブル（在院・新入院・入退院フロー・全麻・粗利予測達成率／タイプ別）
PDF化は headless Chrome --print-to-pdf（build_dept_reports を踏襲・JS不要）。

  python scripts/build_hospital_report.py [--base-date YYYY-MM-DD] [--keep-html]

--base-date 未指定時はデータ最終日を直近日曜（完全週=月〜日の終端）へ丸める
（B12: PDF・掲示の完全週固定。明示指定時は丸めない）。
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.lib.config import (DEFAULT_DATA_DIR, TARGET_INPATIENT_ALLDAY,
                            TARGET_ADMISSION_WEEKLY, TARGET_GA_DAILY,
                            fmt_jp_date, fmt_jp_range)
try:
    from app.lib.config import REPORT_HOSPITAL_NAME
except Exception:
    REPORT_HOSPITAL_NAME = ""
from app.lib.calendar_preview import complete_week_end
from app.lib import hospital_summary as hs

OUT_DIR = ROOT / "output" / "comedix"
SUB = hs.SUB

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


def log(msg, lv="info"):
    p = {"info": "ℹ️ ", "ok": "✅", "warn": "⚠️ ", "err": "❌"}.get(lv, "")
    print(f"  {p} [{datetime.now():%H:%M:%S}] {msg}")


def find_chrome():
    for c in CHROME_CANDIDATES:
        if Path(c).is_file():
            return c
    for n in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        if (p := shutil.which(n)):
            return p
    return None


def html_to_pdf(chrome, html_path: Path, pdf_path: Path) -> bool:
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [chrome, "--headless", "--disable-gpu", "--no-sandbox",
           "--no-pdf-header-footer", f"--print-to-pdf={pdf_path}",
           html_path.resolve().as_uri()]
    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return res.returncode == 0 and pdf_path.exists()


def resolve_base_date(explicit_base_date, base_date):
    """B12: --base-date 未指定時は直近日曜（完全週の終端）へ丸める。

    explicit_base_date は argparse の生値（未指定なら None）。base_date は
    load_and_preprocess が解決済みの値（未指定時はデータ最終日）。明示指定時は
    そのまま返す（丸めない）。
    """
    if explicit_base_date is not None:
        return base_date
    return complete_week_end(base_date)


def build_period_heading(week_start, week_end) -> str:
    """B12: 「対象週 8/24(月)〜8/30(日)｜比較: その前の週 8/17〜8/23」。
    「直近7日」の語は使わない（正本§3・§7裁定）。"""
    prior_start = week_start - timedelta(days=7)
    prior_end = week_end - timedelta(days=7)
    return (f"対象週 {fmt_jp_date(week_start)}〜{fmt_jp_date(week_end)}"
            f"｜比較: その前の週 {fmt_jp_range(prior_start, prior_end)}")


def _headline_banner(hl: dict) -> str:
    lvl = (hl or {}).get("level", "ok")
    bg = {"danger": "#fdf0f2", "warn": "#fef7ee", "ok": "#ecfdf5"}.get(lvl, "#f6f8fb")
    bd = {"danger": "#c4314b", "warn": "#b45309", "ok": "#0e7a54"}.get(lvl, "#9daab8")
    return (f'<div style="background:{bg};border-left:5px solid {bd};padding:10px 14px;border-radius:8px;margin:12px 0">'
            f'<div style="font-size:15px;font-weight:700">{hl.get("icon","")} {hl.get("text","")}</div>'
            f'<div style="font-size:12px;color:{SUB};margin-top:3px">{hl.get("detail","")}</div></div>')


def _calendar_line(cp) -> str:
    """P4暦プレビュー: 来月層・早期警戒層のみ1行で（判定不変・非発火時は空文字）。"""
    if not cp:
        return ""
    texts = [x["text"] for x in (cp.get("early"), cp.get("month")) if x]
    if not texts:
        return ""
    return (f'<div style="font-size:11.5px;color:{SUB};background:#f6f8fb;'
            f'border-radius:8px;padding:7px 11px;margin-top:8px">' + " ".join(texts) + "</div>")


def build_html(ctx) -> str:
    bd = ctx["base_date"]
    period = build_period_heading(bd - timedelta(days=6), bd)
    title = REPORT_HOSPITAL_NAME or "全病院"
    kpi = ctx["kpi"]
    hero = hs.render_hero(ctx["hero"]["headline"], ctx["hero"]["body"], ctx["hero"]["chips"])
    cal_line = _calendar_line(ctx.get("calendar_preview"))
    kpis = hs.render_kpi_cards(kpi)
    profit_line = hs.render_profit_headline_line(ctx.get("profit_headline"))
    banner = _headline_banner(kpi.get("headline"))

    t = ctx["trends"]
    t_inp = hs.render_trend_svg(t["inpatient"], TARGET_INPATIENT_ALLDAY, f"目標{TARGET_INPATIENT_ALLDAY:.0f}",
                                "人", "在院患者数（12週・7日移動平均）")
    t_adm = hs.render_trend_svg(t["admission"], TARGET_ADMISSION_WEEKLY, f"目標{TARGET_ADMISSION_WEEKLY:.0f}",
                                "人/週", "新入院（12週・週合計）")
    t_op = hs.render_trend_svg(t["operation"], TARGET_GA_DAILY, f"目標{TARGET_GA_DAILY:.0f}",
                               "件/日", "全身麻酔手術（12週・30営業平日移動平均）")
    ward = hs.render_ward_table(ctx["ward_rows"])
    dept = hs.render_dept_table(ctx["dept_rows"])
    legend = hs.render_legend()

    def page(inner, last=False):
        cls = "page last" if last else "page"
        return f'<section class="{cls}">{inner}</section>'

    head = (f'<div style="border-bottom:2px solid #2b5797;padding-bottom:8px;margin-bottom:6px">'
            f'<div style="font-size:12px;color:{SUB};letter-spacing:1px">{title}　全病院 実績まとめ</div>'
            f'<div style="font-size:20px;font-weight:700">病院全体KPI　'
            f'<span style="font-size:13px;color:{SUB};font-weight:600">{period}・基準日 {bd:%Y-%m-%d}</span></div></div>')

    p1 = page(head + f'<div style="margin-top:10px">{kpis}</div>' + profit_line + banner
              + '<div class="sec">📣 今週の一手</div>' + hero + cal_line)
    p2 = page('<div class="sec">📈 主要指標の推移（直近12週・点線＝前年同期）</div>'
              + f'<div class="tb">{t_inp}</div><div class="tb">{t_adm}</div><div class="tb">{t_op}</div>')
    p3 = page('<div class="sec">🛏 病棟別 実績（在院・病床利用率・入退院フロー・週末在院維持率）</div>'
              + ward + legend)
    p4 = page('<div class="sec">🩺 診療科別 実績（在院・新入院・入退院フロー・全麻・粗利予測達成率）</div>'
              + dept + legend, last=True)

    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><style>
@page {{ size: A4 portrait; margin: 13mm; }}
{hs.BASE_CSS}
  .page {{ page-break-after: always; }}
  .page.last {{ page-break-after: auto; }}
  .tb {{ margin: 2px 0 8px; }}
</style></head><body>{p1}{p2}{p3}{p4}</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--base-date", default=None)
    ap.add_argument("--output-dir", default=str(OUT_DIR))
    ap.add_argument("--keep-html", action="store_true")
    args = ap.parse_args()

    chrome = find_chrome()
    if not chrome:
        log("Chrome/Chromium が見つかりません。", "err")
        sys.exit(1)

    from generate_html import load_and_preprocess
    log("データ読込・前処理中（load_and_preprocess）...")
    adm, surg, targets, surg_targets, profit_monthly, base_date, profit_breakdown = \
        load_and_preprocess(args.data_dir, args.base_date, no_validate=False)
    resolved_base_date = resolve_base_date(args.base_date, base_date)
    if resolved_base_date != base_date:
        log(f"--base-date 未指定のため直近日曜（{resolved_base_date:%Y-%m-%d}）へ丸めました")
    base_date = resolved_base_date

    # 粗利予測（較正済み hybrid+recency補正 pipeline）: build_dept_reports.py と同じ
    # 組立で、ダッシュボード/PLレポートと同一の値をP4診療科テーブルの粗利予測達成率に
    # 使う（未指定時は hospital_summary 側でOLS単独推計へ後方互換フォールバック）。
    # hybrid 計算自体は下の load_profit_headline が1回だけ行い、ここではその結果から
    # 導出するのみ（§9 #13: 二重計算回避）。
    from app.lib.profit_estimate import (projection_from_hybrid,
                                         last_complete_driver_date)
    profit_base_date = last_complete_driver_date(adm, surg) or base_date

    # 粗利ヘッドライン（P1・入院 粗利/人日／外来 粗利/営業日）: build_comedix_card.py と
    # 同じ手順（fail-soft・目標内訳読込→hybrid→build_profit_headline）を共用する。
    from scripts.build_comedix_card import load_profit_headline
    profit_headline, profit_hybrid = load_profit_headline(
        args.data_dir, adm, surg, profit_monthly, profit_breakdown, base_date)
    profit_projection = projection_from_hybrid(
        profit_hybrid[0], profit_hybrid[1], profit_base_date)

    # 粗利/人日（確報月・科別・P4）: build_profit_unit_payload には hybrid の
    # meta/hospital_series が要る。load_profit_headline（上）が返す hybrid
    # （build_profit_hybrid_calibrated の戻り値そのもの）をそのまま使い、同じ
    # 計算をここでもう一度行わない（以前は ph だけが返っていたため二重計算していた）。
    profit_unit = None
    try:
        from app.lib.data_loader import load_profit_targets_breakdown
        from app.lib.profit_unit import build_profit_unit_payload
        hybrid_section, _ = profit_hybrid
        profit_targets_breakdown = load_profit_targets_breakdown(args.data_dir)
        if hybrid_section:
            profit_unit = build_profit_unit_payload(
                profit_breakdown, adm, base_date=profit_base_date,
                profit_targets_breakdown=profit_targets_breakdown,
                profit_hybrid_meta=hybrid_section["meta"],
                hospital_series=hybrid_section["hospital_series"])
    except Exception:
        profit_unit = None

    ctx = hs.build_summary_context(adm, surg, targets, surg_targets, base_date,
                                   profit_monthly=profit_monthly,
                                   profit_breakdown=profit_breakdown,
                                   profit_projection=profit_projection,
                                   profit_headline=profit_headline,
                                   profit_unit=profit_unit)

    html = build_html(ctx)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = out_dir / f"実績まとめ_{base_date:%Y-%m-%d}.pdf"
    tmp = Path(tempfile.mkdtemp(prefix="hosp_report_"))
    html_path = tmp / "report.html"
    html_path.write_text(html, encoding="utf-8")
    if args.keep_html:
        shutil.copy(html_path, out_dir / "_report_debug.html")

    if html_to_pdf(chrome, html_path, pdf):
        log(f"PDF 生成: {pdf}", "ok")
    else:
        log("PDF 生成に失敗しました。", "err")
        shutil.rmtree(tmp, ignore_errors=True)
        sys.exit(1)
    shutil.rmtree(tmp, ignore_errors=True)
    print()
    log("運用: 印刷配布、または資料室にPDFとしてアップロードしお知らせからリンク。", "info")


if __name__ == "__main__":
    main()
