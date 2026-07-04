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


def _strip_serve_argv(argv: list) -> list:
    """--serve/--port を除いた再ビルド用引数（レビューUIの「PDF再作成」が同条件で再実行）。"""
    out, skip = [], 0
    for a in argv:
        if skip:
            skip -= 1
            continue
        if a == "--serve" or a.startswith("--port="):
            continue
        if a == "--port":
            skip = 1
            continue
        out.append(a)
    return out


def serve_review(output_dir: Path, review_rel: str, port: int, rebuild_argv: list):
    """§6-1 レビューUI用の軽量ローカルサーバ（127.0.0.1限定・Ctrl+Cで終了）。

    - GET: output_dir（dept_reports/）配下の静的配信（レビューHTML/PDF）。
    - POST /rebuild: このスクリプトを同一引数（--serve/--port除く）で再実行（1本のみ）。
      リクエスト内容は使わない＝任意コマンド実行の余地なし。
    - GET /rebuild/status: {state: idle|running|ok|error, log: 直近行} を返す。
    """
    import http.server
    import json
    import threading
    import webbrowser
    from collections import deque
    from urllib.parse import quote

    root = output_dir.resolve()
    repo = Path(__file__).resolve().parent.parent
    status = {"state": "idle"}
    log_buf = deque(maxlen=100)
    lock = threading.Lock()

    def run_rebuild():
        try:
            proc = subprocess.Popen(rebuild_argv, cwd=str(repo), text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            for line in proc.stdout:
                log_buf.append(line.rstrip())
            rc = proc.wait()
            with lock:
                status["state"] = "ok" if rc == 0 else "error"
        except Exception as e:
            log_buf.append(f"再作成プロセスの起動に失敗: {e}")
            with lock:
                status["state"] = "error"
        log(f"再作成 {'完了' if status['state'] == 'ok' else '失敗'}",
            "ok" if status["state"] == "ok" else "err")

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(root), **kw)

        def log_message(self, *_a):
            pass

        def _json(self, code: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/rebuild":
                self.send_error(404)
                return
            with lock:
                if status["state"] == "running":
                    self._json(409, {"state": "running"})
                    return
                status["state"] = "running"
                log_buf.clear()
            log("レビューUIから再作成を開始…")
            threading.Thread(target=run_rebuild, daemon=True).start()
            self._json(202, {"state": "running"})

        def do_GET(self):
            if self.path == "/rebuild/status":
                with lock:
                    st = status["state"]
                self._json(200, {"state": st, "log": list(log_buf)[-15:]})
            else:
                super().do_GET()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/" + quote(review_rel)
    log(f"レビューサーバ起動: {url}（終了は Ctrl+C）", "ok")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("レビューサーバを終了しました")


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
    p.add_argument("--no-overrides", action="store_true",
                   help="overrides.md（一手の手動差し替え）を読み込まない")
    p.add_argument("--serve", action="store_true",
                   help="ビルド後にレビューUI用ローカルサーバを起動"
                        "（「PDF再作成」ボタンが使える・Ctrl+Cで終了）")
    p.add_argument("--port", type=int, default=8765, help="--serve のポート（既定8765）")
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

    # ── 差分ナラティブ用アンカー（約4週前の量子化状態）──
    from app.lib.dept_report import load_delta_anchor, save_facts_snapshot
    state_dir = Path(args.output_dir) / "_state"
    anchor = load_delta_anchor(state_dir, base_date)
    if anchor:
        log(f"差分ナラティブ: アンカー {anchor['_anchor_date']} と比較")
    else:
        log("差分ナラティブ: アンカーなし（21日以上前のスナップショット未蓄積）")

    # ── §6-1 人手オーバーライド（レビューHTML/手編集の overrides.md）──
    from app.lib.report_overrides import parse_overrides
    overrides_path = Path(args.output_dir) / "overrides.md"
    overrides = {}
    if not args.no_overrides:
        overrides, ov_notes = parse_overrides(overrides_path, base_date)
        for level, msg in ov_notes:
            log(f"overrides.md: {msg}", level)
        if overrides:
            log(f"一手の手動差し替え {len(overrides)} 部門（{overrides_path}）")

    # ── コンテキスト構築（AI一手は全ユニット）──
    log(f"レポート構築中… axes={axes} AI={'OFF' if args.no_ai else 'ON(全ユニット)'}")
    contexts = build_dept_report_contexts(
        adm, surg, targets, surg_targets, profit_monthly, base_date, generated_at,
        hospital_name=args.hospital_name, with_ai=not args.no_ai,
        axes=axes, quiet=args.quiet, profit_breakdown=profit_breakdown,
        delta_anchor=anchor, overrides=overrides,
    )
    if args.only:
        contexts = [c for c in contexts if c["unit"] == args.only]
    if args.limit:
        contexts = contexts[:args.limit]
    log(f"対象 {len(contexts)} 部門")

    # 部門名の打ち間違い等で適用されなかったオーバーライドを警告（手編集の事故検知）
    if overrides and not (args.only or args.limit):
        applied = {(c["axis"], c["unit"]) for c in contexts
                   if c["move"].get("src") == "manual"}
        for key in set(overrides) - applied:
            ax_jp = "診療科" if key[0] == "dept" else "病棟"
            log(f"overrides.md: [{ax_jp}:{key[1]}] に一致する部門がありません（未適用）", "warn")

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

    def emit_html(html, pdf_path, pages):
        """完成HTMLを1つのPDFへ（keep-html/Chrome無し時はHTMLも残す）。"""
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
                    log(f"{pdf_path.relative_to(out_root)}（{pages}ページ）", "ok")
            else:
                log(f"PDF生成失敗: {pdf_path.name}", "warn")

    def emit(sheets, pdf_path):
        """1つ以上のシートを1つのHTML→PDFに（連結時はシート間で改ページ）。"""
        emit_html(tmpl.render(sheets=sheets), pdf_path, len(sheets))

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

    # ── §6-1 一手レビューHTML（全部門を1ファイル・PDFと同テンプレ＝同じ見た目）──
    # 責任者が印刷されるそのままのシート上でコメントを直し「保存」で overrides.md へ
    # 直接書き込む（File System Access API）。--only/--limit の部分ビルドでは省略。
    if not (args.only or args.limit):
        from app.lib.report_overrides import default_expires
        review_sheets = sorted(contexts, key=lambda c: (c["axis"] != "dept", c["order"]))
        review_html = tmpl.render(
            sheets=review_sheets, review=True,
            review_base=date_str, review_expires=default_expires(base_date))
        review_path = out_root / f"レビュー_{date_str}.html"
        review_path.write_text(review_html, encoding="utf-8")
        n_manual = sum(1 for c in contexts if c["move"].get("src") == "manual")
        log(f"一手レビューHTML: {review_path.name}（{len(review_sheets)}部門・"
            f"手動差し替え中 {n_manual}件）", "ok")

    # ── 病院全体サマリ 3ページPDF（常に追加出力。確認用の --only/--limit 時は省略）──
    if not (args.only or args.limit):
        from app.lib import hospital_summary as hs
        from app.lib.dept_report import (build_hospital_overview_context,
                                         render_summary_table_pages)
        from app.lib.profit_estimate import (compute_calibrated_profit_projection,
                                             last_complete_driver_date)
        log("病院全体サマリ（3ページ）を生成中…")
        # 粗利予測は adm/surg 両方が揃う最終日で行う（本番ダッシュボードと同じ日で揃える）
        profit_base_date = last_complete_driver_date(adm, surg) or base_date
        profit_projection = None
        if profit_breakdown is not None and len(profit_breakdown):
            try:
                profit_projection = compute_calibrated_profit_projection(
                    profit_breakdown, surg, adm, profit_base_date)
            except Exception:
                profit_projection = None
        hosp_ctx = build_hospital_overview_context(
            adm, surg, targets, surg_targets, profit_monthly, base_date, generated_at,
            hospital_name=args.hospital_name, profit_breakdown=profit_breakdown,
            profit_projection=profit_projection, with_ai=not args.no_ai, quiet=args.quiet,
            delta_anchor=anchor)
        extra = render_summary_table_pages(
            adm, surg, targets, surg_targets, base_date,
            hospital_name=args.hospital_name, profit_monthly=profit_monthly,
            profit_breakdown=profit_breakdown, profit_projection=profit_projection)
        hosp_html = tmpl.render(sheets=[hosp_ctx], extra_pages=extra, table_css=hs.BASE_CSS)
        emit_html(hosp_html, out_root / f"病院全体サマリ_{date_str}.pdf", 3)

        # ── 事実スナップショット保存（次回以降の差分ナラティブのアンカー材料）──
        # --only/--limit の部分ビルドでは保存しない（不完全な状態を残さない）
        units_state = {f"{c['axis']}:{c['unit']}": c["_state"]
                       for c in contexts if c.get("_state")}
        snap = save_facts_snapshot(state_dir, base_date, units_state,
                                   hosp_ctx.get("_state") or {})
        log(f"事実スナップショット保存: {snap.relative_to(Path(args.output_dir))}")

    if not args.no_ai:
        from app.lib.ai_narrative import REJECT_STATS
        if REJECT_STATS:
            log(f"AI一手 採択/棄却内訳: {dict(REJECT_STATS)}")

    print(f"\n{'='*52}")
    print(f"  部門別レポート生成完了 — {generated_at.strftime('%Y/%m/%d %H:%M')}")
    print(f"  基準日: {date_str}")
    print(f"  形式: {'部門ごと個別' if individual else '軸ごと連結（一括印刷用）'}")
    print(f"  出力先: {out_root.resolve()}")
    print(f"  PDF: {counts['pdf']} 件" + (f" / HTML: {counts['html']} 件" if counts['html'] else ""))
    print(f"{'='*52}\n")

    # ── §6-1 レビューサーバ（--serve）: HTMLの「PDF再作成」ボタンを有効化 ──
    if args.serve:
        if args.only or args.limit:
            log("--serve は --only/--limit（部分ビルド）では使えません", "warn")
        else:
            rebuild_argv = [sys.executable, str(Path(__file__).resolve())] \
                           + _strip_serve_argv(sys.argv[1:])
            serve_review(Path(args.output_dir),
                         f"{date_str}/レビュー_{date_str}.html",
                         args.port, rebuild_argv)


if __name__ == "__main__":
    main()
