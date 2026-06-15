#!/usr/bin/env python3
"""build_comedix_card.py — Comedix「お知らせ・回覧板」用 週報サマリーPNG（軽量・レバーB）。

Comedix は <img src="library_refer.php?…u=…png"> でライブラリ画像を画面内インライン表示できる
（page.html のQRが実証）。動的内容を1枚のPNGに焼き込み、資料室の固定文書へ毎週**中身だけ上書き**。
→ お知らせ本文HTMLは初回に貼るだけ。

軽量構成（詳細は別途「実績まとめPDF」＝build_hospital_report.py）:
  ① 今週の一手（ヒーロー／下書きファイル編集可）
  ② portal準拠KPI3（build_kpi_summary）
  ③ 在院トレンド（12週・7日移動平均＋前年同期線・小）
  ④ 詳細PDF/WEB版への導線

「今週の一手」は output/comedix/今週の一手.md を編集→再実行で反映。--refresh で自動下書きに戻す。
レンダリングは headless Chrome --screenshot ＋ Pillow 余白トリミング。

  python scripts/build_comedix_card.py [--width 760] [--refresh] [--keep-html]
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
from app.lib.config import DEFAULT_DATA_DIR, TARGET_INPATIENT_ALLDAY
try:
    from app.lib.config import REPORT_HOSPITAL_NAME
except Exception:
    REPORT_HOSPITAL_NAME = ""
from app.lib import hospital_summary as hs

OUT_DIR = ROOT / "output" / "comedix"
WEB_URL = "https://tinyurl.com/daily-dashboard-G"
COMMENT_FILE = OUT_DIR / "今週の一手.md"
INK, SUB = hs.INK, hs.SUB

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


# ════════════════════════════════════════════════════════════
# 今週の一手 下書きファイル（編集ステップ）
# ════════════════════════════════════════════════════════════
def _draft_text(headline: str, body: str) -> str:
    return (
        "# このファイルを編集して `make comedix` を再実行すると、編集後の文が週報サマリーに反映されます。\n"
        "# 形式: #で始まらない最初の行＝見出し／空行をはさんで以降＝本文。\n"
        "# 自動下書きに戻すには: make comedix REFRESH=1 （または --refresh）。維持率/のびしろは常にデータから自動表示。\n\n"
        f"{headline}\n\n{body}\n"
    )


def resolve_comment(auto: dict, refresh: bool) -> tuple[str, str]:
    """今週の一手.md を読み（無ければ自動下書きを作成）、(見出し, 本文) を返す。"""
    if refresh or not COMMENT_FILE.exists():
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        COMMENT_FILE.write_text(_draft_text(auto["headline"], auto["body"]), encoding="utf-8")
        log(f"今週の一手 下書きを生成: {COMMENT_FILE}", "ok")
        return auto["headline"], auto["body"]
    lines = [l for l in COMMENT_FILE.read_text(encoding="utf-8").splitlines()
             if not l.lstrip().startswith("#")]
    content = [l for l in lines]
    # 先頭の空行を除去
    while content and not content[0].strip():
        content.pop(0)
    if not content:
        return auto["headline"], auto["body"]
    headline = content[0].strip()
    body = " ".join(l.strip() for l in content[1:] if l.strip()) or auto["body"]
    return headline, body


# ════════════════════════════════════════════════════════════
# ページ組立 → PNG
# ════════════════════════════════════════════════════════════
def build_html(ctx, headline, body, width):
    bd = ctx["base_date"]
    period = f"{bd - timedelta(days=6):%Y年%-m月%-d日}〜{bd:%-m月%-d日}（直近7日）"
    title = (REPORT_HOSPITAL_NAME + "　") if REPORT_HOSPITAL_NAME else ""
    hero = hs.render_hero(headline, body, ctx["hero"]["chips"])
    kpis = hs.render_kpi_cards(ctx["kpi"])
    trend = hs.render_trend_svg(ctx["trends"]["inpatient"], ref=TARGET_INPATIENT_ALLDAY,
                                ref_label=f"目標{TARGET_INPATIENT_ALLDAY:.0f}", unit="人",
                                window_label="在院患者数の推移（12週・7日移動平均）")
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><style>
{hs.BASE_CSS}
  .wrap{{width:{width}px;padding:22px 22px 26px;}}
</style></head><body><div class="wrap">
  <div style="border-bottom:2px solid #2b5797;padding-bottom:8px;margin-bottom:14px">
    <div style="font-size:12px;color:{SUB};letter-spacing:1px">{title}全職員向け 週報</div>
    <div style="font-size:19px;font-weight:800">病院全体KPI　<span style="font-size:13px;color:{SUB};font-weight:600">{period}</span></div>
  </div>
  {hero}
  <div style="margin:14px 0 0">{kpis}</div>
  <div style="margin-top:10px">{trend}</div>
  <div style="margin-top:14px;padding:9px 13px;background:#f0f6ff;border-left:5px solid #2b6cb0;border-radius:6px;font-size:12.5px;color:#2b3a4a">
    📄 病棟別・診療科別の詳細、12週の新入院/手術トレンドは <b>実績まとめPDF</b>（資料室）へ。
  </div>
  <div style="margin-top:12px;text-align:right;font-size:11px;color:{SUB}">
    フル機能のWEB版週報 → {WEB_URL}　／　基準日 {bd:%Y-%m-%d}
  </div>
</div></body></html>"""


def render_png(html, out_png, width, keep_html):
    chrome = find_chrome()
    if not chrome:
        log("Chrome/Chromium が見つかりません。", "err")
        return False
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="comedix_"))
    card, raw = tmp / "card.html", tmp / "raw.png"
    card.write_text(html, encoding="utf-8")
    cmd = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
           "--force-device-scale-factor=2", f"--window-size={width+40},2400",
           "--default-background-color=FFFFFFFF", f"--screenshot={raw}", card.as_uri()]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if not raw.exists():
        log(f"Chrome screenshot 失敗: {r.stderr[:300]}", "err")
        return False
    from PIL import Image, ImageChops
    im = Image.open(raw).convert("RGB")
    bbox = ImageChops.difference(im, Image.new("RGB", im.size, (255, 255, 255))).getbbox()
    if bbox:
        m = 24
        x0, y0, x1, y1 = bbox
        im = im.crop((max(0, x0 - m), max(0, y0 - m), min(im.width, x1 + m), min(im.height, y1 + m)))
    im.save(out_png)
    if keep_html:
        shutil.copy(card, OUT_DIR / "_card_debug.html")
    shutil.rmtree(tmp, ignore_errors=True)
    log(f"PNG 生成: {out_png}  ({im.width}×{im.height}px)", "ok")
    return True


def write_post_body():
    body = f"""<!-- ▼▼ Comedixお知らせへHTMLソースモードで貼る（初回のみ）▼▼ -->
<!-- ① 週報サマリー.png を資料室にアップ → ② library_refer.php?... のURLを <img src> に貼り替え -->
<!-- ③ 以後は資料室で同じ文書の中身を週報サマリー.pngで上書きするだけ。このHTMLは触らない -->
<h2 style="text-align:center;border-bottom:2px solid #2b5797;padding-bottom:8px;color:#1f2d3d">
  <span style="font-size:medium">【<span style="background-color:#ffff00">全職員向け 週報</span>】病院全体KPI（毎週更新）</span></h2>
<div style="text-align:center;margin:14px 0">
  <img style="max-width:100%;border:1px solid #e0e6ee;border-radius:8px"
       src="library_refer.php?s=XXXXXXXX&amp;i=NNNNN&amp;u=docArchive%2Fall%2Fcate103%2FdocumentNNNNN.png"
       alt="今週の週報サマリー" /></div>
<h3 style="margin-top:22px;border-bottom:2px solid #ccc;padding-bottom:6px">
  <span style="font-size:small">🌐 <span style="background-color:#cce5ff">WEB版週報（フル機能）</span></span></h3>
<div style="display:flex;align-items:center;justify-content:center;padding:14px;background-color:#f9f9f9;border-radius:8px">
  <div style="flex:1;padding-right:18px;font-size:14px;line-height:1.6;color:#333">
    スマホ・PCから全診療科／全病棟を確認できます。<br />画面を回転させるとグラフを拡大できます。<br />
    <span style="font-size:12px;font-weight:bold;color:#0066cc">{WEB_URL}</span></div>
  <!-- ↓ page.html で動作実績のある実物QRの <img> をここに貼る -->
  <div style="flex-shrink:0;text-align:center"><div style="width:80px;height:80px;border:4px solid #fff;
       box-shadow:0 0 6px rgba(0,0,0,.2);background:#ddd;display:flex;align-items:center;justify-content:center;font-size:10px;color:#888">QR</div></div>
</div>
<!-- ▲▲ ここまで ▲▲ -->
"""
    (OUT_DIR / "お知らせ本文.html").write_text(body, encoding="utf-8")
    log(f"貼付用本文（初回のみ）: {OUT_DIR / 'お知らせ本文.html'}", "ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--width", type=int, default=760)
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--base-date", default=None)
    ap.add_argument("--refresh", action="store_true", help="今週の一手.md を自動下書きで作り直す")
    ap.add_argument("--keep-html", action="store_true")
    args = ap.parse_args()

    from generate_html import load_and_preprocess
    log("データ読込・前処理中（load_and_preprocess）...")
    adm, surg, targets, surg_targets, _pm, base_date, _ = \
        load_and_preprocess(args.data_dir, args.base_date, no_validate=False)
    ctx = hs.build_summary_context(adm, surg, targets, surg_targets, base_date)
    headline, body = resolve_comment(ctx["hero"], args.refresh)

    html = build_html(ctx, headline, body, args.width)
    out_png = OUT_DIR / "週報サマリー.png"
    if not render_png(html, out_png, args.width, args.keep_html):
        sys.exit(1)
    write_post_body()
    print()
    log("運用: 週報サマリー.png を資料室の同じ文書へ上書き／一手は 今週の一手.md を編集して再実行", "info")


if __name__ == "__main__":
    main()
