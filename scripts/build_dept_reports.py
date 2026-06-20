#!/usr/bin/env python3
"""build_dept_reports.py — 部門別レポートPDF 一括生成（入退院バランス特化・A4 1枚）。

ダッシュボードとは別立ての印刷ハンドアウト。月1〜2回・ローカル印刷運用。
既定は **軸ごとに1つのPDFへ連結**（一括印刷用）:
  {output-dir}/{基準日}/診療科版_{基準日}.pdf （21ページ）
  {output-dir}/{基準日}/病棟版_{基準日}.pdf   （17ページ）

  python scripts/build_dept_reports.py                  # 軸ごと連結（既定・全科/全病棟）
  python scripts/build_dept_reports.py --split          # 部門ごとの個別PDFに分割
  python scripts/build_dept_reports.py --no-ai          # 一手は定型文のみ（oMLX不要・高速）
  python scripts/build_dept_reports.py --axes dept      # 診療科版だけ
  python scripts/build_dept_reports.py --only 消化器内科 # 1ユニットだけ（確認用・個別出力）
  python scripts/build_dept_reports.py --keep-html      # 中間HTMLも残す

PDF化は headless Chrome の --print-to-pdf。SVG/レイアウトは Python 側で完成済みなので
JS 実行は不要（タイミング問題なし）。
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.lib.config import DEFAULT_DATA_DIR
try:
    from app.lib.config import REPORT_HOSPITAL_NAME
except ImportError:
    REPORT_HOSPITAL_NAME = ""
from app.lib.dept_report import build_dept_report_contexts

AXIS_DIR = {"dept": "診療科", "ward": "病棟"}

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]


def find_chrome() -> str | None:
    for c in CHROME_CANDIDATES:
        if Path(c).is_file():
            return c
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    return None


def log(msg, level="info"):
    ts = datetime.now().strftime("%H:%M:%S")
    prefix = {"info": "ℹ️ ", "ok": "✅", "warn": "⚠️ ", "err": "❌"}.get(level, "")
    print(f"  {prefix} [{ts}] {msg}")


def html_to_pdf(chrome: str, html_path: Path, pdf_path: Path) -> bool:
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        chrome, "--headless", "--disable-gpu", "--no-sandbox",
        "--no-pdf-header-footer", f"--print-to-pdf={pdf_path}",
        html_path.resolve().as_uri(),
    ]
    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return res.returncode == 0 and pdf_path.exists()


def main():
    p = argparse.ArgumentParser(description="部門別レポートPDF 一括生成")
    p.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    p.add_argument("--output-dir", default="dept_reports")
    p.add_argument("--base-date", default=None, help="基準日 YYYY-MM-DD")
    p.add_argument("--axes", default="dept,ward", help="dept,ward のいずれか/両方")
    p.add_argument("--hospital-name", default=REPORT_HOSPITAL_NAME)
    p.add_argument("--no-ai", action="store_true", help="一手をAI生成せず定型文のみ")
    p.add_argument("--only", default=None, help="特定ユニット名のみ生成（確認用）")
    p.add_argument("--limit", type=int, default=None, help="先頭N件のみ生成（確認用）")
    p.add_argument("--keep-html", action="store_true", help="中間HTMLを出力先に残す")
    p.add_argument("--split", action="store_true",
                   help="軸ごと連結せず、部門ごとの個別PDF（{基準日}/{軸}/{名前}.pdf）に分割")
    p.add_argument("--quiet", "-q", action="store_true")
    args = p.parse_args()

    axes = tuple(a.strip() for a in args.axes.split(",") if a.strip() in ("dept", "ward"))
    if not axes:
        log("axes は dept / ward を指定してください", "err"); sys.exit(1)

    chrome = find_chrome()
    if not chrome:
        log("Chrome/Chromium が見つかりません。HTMLのみ出力します（--keep-html 相当）", "warn")

    # ── データ読込（generate_html を流用）──
    from generate_html import load_and_preprocess
    adm, surg, targets, surg_targets, profit_monthly, base_date, profit_breakdown = \
        load_and_preprocess(args.data_dir, args.base_date, no_validate=False)
    generated_at = datetime.now()

    # ── コンテキスト構築（AI一手は全ユニット）──
    log(f"レポート構築中… axes={axes} AI={'OFF' if args.no_ai else 'ON(全ユニット)'}")
    contexts = build_dept_report_contexts(
        adm, surg, targets, surg_targets, profit_monthly, base_date, generated_at,
        hospital_name=args.hospital_name, with_ai=not args.no_ai,
        axes=axes, quiet=args.quiet, profit_breakdown=profit_breakdown,
    )
    if args.only:
        contexts = [c for c in contexts if c["unit"] == args.only]
    if args.limit:
        contexts = contexts[:args.limit]
    log(f"対象 {len(contexts)} 部門")

    # ── Jinja ──
    from jinja2 import Environment, FileSystemLoader
    env = Environment(
        loader=FileSystemLoader(str(Path(__file__).resolve().parent.parent / "app" / "templates")),
        autoescape=False,
    )
    tmpl = env.get_template("dept_report.html")

    out_root = Path(args.output_dir) / base_date.strftime("%Y-%m-%d")
    out_root.mkdir(parents=True, exist_ok=True)
    individual = args.split or bool(args.only)   # --only/--split は個別出力
    counts = {"pdf": 0, "html": 0}

    def emit(sheets, pdf_path):
        """1つ以上のシートを1つのHTML→PDFに（連結時はシート間で改ページ）。"""
        html = tmpl.render(sheets=sheets)
        if args.keep_html or not chrome:
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            pdf_path.with_suffix(".html").write_text(html, encoding="utf-8")
            counts["html"] += 1
        if chrome:
            with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False,
                                             encoding="utf-8") as tf:
                tf.write(html)
                tmp = Path(tf.name)
            ok = html_to_pdf(chrome, tmp, pdf_path)
            tmp.unlink(missing_ok=True)
            if ok:
                counts["pdf"] += 1
                if not args.quiet:
                    log(f"{pdf_path.relative_to(out_root)}（{len(sheets)}ページ）", "ok")
            else:
                log(f"PDF生成失敗: {pdf_path.name}", "warn")

    date_str = base_date.strftime("%Y-%m-%d")
    if individual:
        for c in contexts:
            safe = c["unit"].replace("/", "／")
            emit([c], out_root / AXIS_DIR[c["axis"]] / f"{safe}.pdf")
    else:
        # 軸ごとに1ファイルへ連結（固定順＝コード/フロア順で月次のページ位置を安定化）
        for ax in axes:
            sheets = sorted((c for c in contexts if c["axis"] == ax),
                            key=lambda c: c["order"])
            if sheets:
                emit(sheets, out_root / f"{AXIS_DIR[ax]}版_{date_str}.pdf")

    print(f"\n{'='*52}")
    print(f"  部門別レポート生成完了 — {generated_at.strftime('%Y/%m/%d %H:%M')}")
    print(f"  基準日: {date_str}")
    print(f"  形式: {'部門ごと個別' if individual else '軸ごと連結（一括印刷用）'}")
    print(f"  出力先: {out_root.resolve()}")
    print(f"  PDF: {counts['pdf']} 件" + (f" / HTML: {counts['html']} 件" if counts['html'] else ""))
    print(f"{'='*52}\n")


if __name__ == "__main__":
    main()
