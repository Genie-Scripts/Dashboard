#!/usr/bin/env python3
"""
自己完結HTML生成（追加機能・既存パイプラインに非干渉）

ビルド済みの portal.html / detail.html / dept.html を *入力として読むだけ* で、
  - Google Analytics(gtag)ブロックを除去
  - Plotly CDN参照をインライン同梱へ置換（detail/dept、--inline-plotly 時）
して output/selfcontained/ に「自己完結版」を書き出す。

目的:
  臨床VLAN（インターネット無し）で file:// もしくは Comedix資料室経由で開いても
  描画・操作が完全に動くHTMLを得る。Comedixのログインが閲覧制限を担う。

非干渉の保証:
  元の *.html / generate_html.py / deploy フローには一切書き込まない。
  本スクリプトは出力先（output/selfcontained/, gitignore済）にしか書かない。

使い方:
  python scripts/build_selfcontained.py                # portal のみ（既定・最小）
  python scripts/build_selfcontained.py --pages portal detail dept --inline-plotly
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))  # スクリプト直実行では repo 直下が path に無いため（S2用）
OUT_DIR = REPO / "output" / "selfcontained"
VENDOR_DIR = OUT_DIR / "_vendor"  # Plotly等のローカルキャッシュ（gitignore配下）

try:
    from app.lib.moves_store import load_latest_moves, MOVE_PUBLIC_KEYS
except Exception:  # fail-soft: moves_store が無い/壊れていてもselfcontained自体は動く
    load_latest_moves = None
    MOVE_PUBLIC_KEYS = ("body", "action", "surg_line", "util_line", "nadm_line")

# GAブロック判定マーカー（このいずれかを含む <script> を丸ごと除去）
GA_MARKERS = ("googletagmanager", "google-analytics", "gtag(")

# Plotly CDN の参照（version は URL から検出）
PLOTLY_CDN_RE = re.compile(
    r'<script\b[^>]*\bsrc="https://cdn\.plot\.ly/(plotly-[\d.]+\.min\.js)"[^>]*>\s*</script>',
    re.IGNORECASE,
)

SCRIPT_BLOCK_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
EXTERNAL_REF_RE = re.compile(r'(?:src|href)="(https?://[^"]+)"', re.IGNORECASE)

# 埋め込みDATAの生成日時 "generated": "YYYY-MM-DD..." を拾う（ファイル名の日付に使用）
GENERATED_DATE_RE = re.compile(r'"generated"\s*:\s*"(\d{4}-\d{2}-\d{2})')
# portal.html には "generated" マーカーが無い（DATA埋め込み無し）→ フッターの基準日で代用
PORTAL_FOOTER_DATE_RE = re.compile(r"基準日\s*(\d{4}-\d{2}-\d{2})")

# 他ページ用ナビを隠すCSS（単一ページ配信時にデッドリンク化するため）
# 削除ではなく display:none にする理由: backLink は JS が getElementById で参照しており、
# 要素ごと消すと null 参照エラーになる。!important でJS側のinline styleにも勝つ。
NAV_HIDE_STYLE = (
    '<style id="selfcontained-nav-hide">'
    "#backLink,#pageNav{display:none!important}"
    "</style>"
)


def strip_ga(html: str) -> tuple[str, int]:
    """gtag/GA を参照する <script> ブロックを除去。除去数を返す。"""
    removed = 0

    def repl(m: re.Match) -> str:
        nonlocal removed
        block = m.group(0)
        if any(mk in block for mk in GA_MARKERS):
            removed += 1
            return ""
        return block

    return SCRIPT_BLOCK_RE.sub(repl, html), removed


def fetch_plotly(filename: str) -> str:
    """Plotly min.js をローカルキャッシュ（無ければCDNから取得）して中身を返す。"""
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    cache = VENDOR_DIR / filename
    if not cache.exists():
        url = f"https://cdn.plot.ly/{filename}"
        print(f"  ↓ Plotly取得: {url}")
        with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310 (信頼済CDN)
            cache.write_bytes(r.read())
    return cache.read_text(encoding="utf-8")


def inline_plotly(html: str) -> tuple[str, int]:
    """Plotly CDN <script src> をインライン <script> に置換。置換数を返す。"""
    matches = PLOTLY_CDN_RE.findall(html)
    if not matches:
        return html, 0
    js = fetch_plotly(matches[0])
    # </script> がJS内に出ても閉じタグと誤認されないようにエスケープ
    js = js.replace("</script>", "<\\/script>")
    inlined = f"<script>/* inlined {matches[0]} */\n{js}\n</script>"
    # 置換テンプレートにせず関数で返す（Plotly内の \s 等が置換エスケープと誤解されるのを防ぐ）
    new_html, n = PLOTLY_CDN_RE.subn(lambda _m: inlined, html)
    return new_html, n


def hide_cross_page_nav(html: str) -> tuple[str, int]:
    """他ページへのナビ(#backLink/#pageNav)をCSSで非表示にする。注入数を返す。"""
    if "</head>" in html:
        return html.replace("</head>", NAV_HIDE_STYLE + "\n</head>", 1), 1
    return html, 0


def extract_generated_date(html: str) -> str:
    """埋め込みDATAの generated 日付(YYYY-MM-DD)。無ければ portal フッターの基準日。
    どちらも無ければ本日。"""
    m = GENERATED_DATE_RE.search(html)
    if m:
        return m.group(1)
    m = PORTAL_FOOTER_DATE_RE.search(html)
    return m.group(1) if m else date.today().isoformat()


# portal 単体配布用: dept.html/detail.html への直リンクを capture フェーズで遮断し、
# トリアージ行クリックは narrative 開閉へフォールバックさせる（§8.2）。
NEUTRALIZE_LINKS_BLOCK = (
    '<style id="selfcontained-links-off">\n'
    "  .kc .cta{display:none!important}\n"
    '  a[href^="dept.html"],a[href^="detail.html"]{cursor:default}\n'
    "</style>\n"
    '<script id="selfcontained-links-off-js">\n'
    "document.addEventListener('click', function(e){\n"
    "  if (e.target.closest('.triage-acc-btn')) return;\n"
    "  var a = e.target.closest('a[href^=\"dept.html\"],a[href^=\"detail.html\"]');\n"
    "  if (!a) return;\n"
    "  e.preventDefault(); e.stopPropagation();\n"
    "  var t = a.querySelector('.triage-acc-btn'); if (t) t.click();\n"
    "}, true);\n"
    "</script>"
)


def neutralize_links(html: str) -> tuple[str, int]:
    """dept.html/detail.html への直リンクを無効化するstyle/scriptを</body>直前に注入。注入数を返す。"""
    if "</body>" in html:
        return html.replace("</body>", NEUTRALIZE_LINKS_BLOCK + "\n</body>", 1), 1
    return html, 0


def moves_patch(html: str) -> tuple[str, int]:
    """最新 moves を DATA.drill[unit].move に上書きする <script> を </body> 直前に注入。
    パッチしたユニット数を返す。moves 無し/読めない/対象なし → (html, 0)。"""
    if load_latest_moves is None:
        return html, 0
    base = extract_generated_date(html)
    moves = load_latest_moves(base)
    if not moves:
        return html, 0
    label = f"{int(moves['base_date'][5:7])}/{int(moves['base_date'][8:10])}"
    units = {}
    for key, mv in (moves.get("units") or {}).items():
        name = key.split(":", 1)[1]  # "dept:消化器内科" → drill キーは名前のみ
        lite = {k: mv[k] for k in MOVE_PUBLIC_KEYS if mv.get(k)}
        if lite:
            lite["report_date"] = label
            units[name] = lite
    if not units:
        return html, 0
    # 既存の注入があれば除去（defensive・二重注入防止）
    html = re.sub(
        r'\s*<script id="selfcontained-moves-patch">.*?</script>',
        "",
        html,
        flags=re.DOTALL,
    )
    payload = json.dumps(units, ensure_ascii=False).replace("</", "<\\/")  # </script>割れ防止
    script = (
        '<script id="selfcontained-moves-patch">(function(){try{'
        f"var M={payload};"
        "for(var n in M){if(DATA.drill&&DATA.drill[n])DATA.drill[n].move=M[n];}"
        "}catch(e){}})();</script>"
    )
    return html.replace("</body>", script + "\n</body>", 1), len(units)


def remaining_external(html: str) -> list[str]:
    """起動時にブラウザが自動取得する外部 http(s) 参照のみを返す。

    自動取得されるのは HTML属性（<script src>/<link href>/<img src> 等）。
    <script> 本文内のJS文字列（インラインPlotly内部の地図タイル/mapbox/webgl 等の
    休眠URL）は自動取得されないため、本文を除去してから走査する。
    スキーマ宣言の w3.org/schema.org は対象外。
    """
    # <script ...>…</script> の本文だけ除去し、開始タグ(src属性)は残す
    stripped = re.sub(
        r"(<script\b[^>]*>).*?</script>",
        lambda m: m.group(1) + "</script>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    refs = []
    for u in EXTERNAL_REF_RE.findall(stripped):
        if any(d in u for d in ("w3.org", "schema.org")):
            continue
        refs.append(u)
    return sorted(set(refs))


def build_page(
    name: str,
    *,
    do_inline_plotly: bool = False,
    do_hide_nav: bool = False,
    do_neutralize_links: bool = False,
    do_refresh_moves: bool = False,
    out_name: str | None = None,
) -> int:
    src = REPO / f"{name}.html"
    if not src.exists():
        print(f"✗ {src} が無い（先に make build で生成してください）", file=sys.stderr)
        return 1

    html = src.read_text(encoding="utf-8")
    html, n_ga = strip_ga(html)

    n_plotly = 0
    if do_inline_plotly:
        html, n_plotly = inline_plotly(html)

    n_nav = 0
    if do_hide_nav:
        html, n_nav = hide_cross_page_nav(html)

    n_links_off = 0
    if do_neutralize_links:
        html, n_links_off = neutralize_links(html)

    n_moves = 0
    if do_refresh_moves:
        html, n_moves = moves_patch(html)

    # 出力ファイル名（{date}=generated日付, {page}=ページ名 を置換可）
    gen_date = extract_generated_date(html)
    fname = (out_name or "{page}.html").format(page=name, date=gen_date)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dst = OUT_DIR / fname
    dst.write_text(html, encoding="utf-8")

    ext = remaining_external(html)
    size_mb = dst.stat().st_size / 1024 / 1024
    has_noindex = 'name="robots"' in html and "noindex" in html
    print(f"✓ {name}.html → {dst.relative_to(REPO)}  ({size_mb:.2f} MB)")
    print(
        f"    GA除去: {n_ga}  Plotlyインライン: {n_plotly}  "
        f"ナビ非表示: {n_nav}  リンク無効化: {n_links_off}  noindex維持: {has_noindex}"
    )
    if do_refresh_moves:
        print(f"    movesパッチ: {n_moves} ユニット（moves_{gen_date}.json 以下の最新）")
    if ext:
        print(f"    ⚠ 残存外部参照（オフラインで読込不可）: {ext}")
    else:
        print("    外部参照: なし（完全オフライン動作可）")
    return 0


# よく使う配信構成のプリセット（一発実行用）
PROFILES = {
    # dept.html を単体で院内配信: Plotly同梱・他ページナビ非表示・日付入り和名
    # 一手（moves）は最新スナップショットで上書き（オーバーライド反映の核・§8-S2）
    "dept-standalone": dict(
        pages=["dept"],
        inline_plotly=True,
        hide_nav=True,
        refresh_moves=True,
        out_name="部門ダッシュボード_{date}.html",
    ),
    # portal.html を単体で院内配信: Plotly不使用・他ページへのリンクは無効化（§8.1/8.2）
    "portal-standalone": dict(
        pages=["portal"],
        inline_plotly=False,
        hide_nav=True,
        neutralize_links=True,
        out_name="診療KPIポータル_{date}.html",
    ),
}


def main() -> int:
    ap = argparse.ArgumentParser(description="自己完結HTML生成（既存非干渉の後処理）")
    ap.add_argument(
        "--profile",
        choices=sorted(PROFILES),
        help="配信構成プリセット（指定時は他フラグの既定を上書き）。例: dept-standalone",
    )
    ap.add_argument(
        "--pages",
        nargs="+",
        help="対象ページ（既定: portal）。例: --pages portal detail dept",
    )
    ap.add_argument(
        "--inline-plotly",
        action="store_true",
        help="Plotly CDN をインライン同梱（detail/dept をオフライン化する場合に指定）",
    )
    ap.add_argument(
        "--hide-nav",
        action="store_true",
        help="他ページへのナビ(#backLink/#pageNav)を非表示（単一ページ配信でデッドリンク化を防ぐ）",
    )
    ap.add_argument(
        "--out-name",
        help="出力ファイル名テンプレ（{page}/{date}置換可）。例: 部門ダッシュボード_{date}.html",
    )
    ap.add_argument(
        "--neutralize-links",
        action="store_true",
        help="dept.html/detail.html への直リンクを無効化（portal単体配布用・§8.2）",
    )
    ap.add_argument(
        "--refresh-moves",
        action="store_true",
        help="最新 moves スナップショットで DATA.drill[unit].move を上書き（§8-S2）",
    )
    args = ap.parse_args()

    prof = PROFILES.get(args.profile, {})
    pages = args.pages or prof.get("pages", ["portal"])
    inline_plotly_flag = args.inline_plotly or prof.get("inline_plotly", False)
    hide_nav_flag = args.hide_nav or prof.get("hide_nav", False)
    neutralize_links_flag = args.neutralize_links or prof.get("neutralize_links", False)
    refresh_moves_flag = args.refresh_moves or prof.get("refresh_moves", False)
    out_name = args.out_name or prof.get("out_name")

    print(f"自己完結HTML生成 → {OUT_DIR.relative_to(REPO)}/")
    rc = 0
    for name in pages:
        rc |= build_page(
            name,
            do_inline_plotly=inline_plotly_flag,
            do_hide_nav=hide_nav_flag,
            do_neutralize_links=neutralize_links_flag,
            do_refresh_moves=refresh_moves_flag,
            out_name=out_name,
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
