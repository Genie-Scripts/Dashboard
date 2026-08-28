#!/usr/bin/env python3
"""build_comedix_html.py — Comedix「お知らせ・回覧板」用 単一HTML週報（直貼り）。

PNG版（build_comedix_card.py）と違い、ダッシュボードを **インラインstyleのみのHTML断片** として
出力する。Comedix は <style>/class/<script> を許さないが inline style 付きの div/table/flex は
描画できる（tests/page.html・tests/comedix_sample.html で実証）。
→ お知らせ本文に **このHTMLをそのまま貼るだけ**。テーブル/KPIは画像アップロード不要。

【グラフだけは img 方式】2026-06-19 実機テストで Comedix のサニタイザが **インライン<svg>を除去**
することが判明。テーブル・KPI（div/table）は残る。よってトレンドだけ QR と同じ **library_refer.php
の <img>** で配信する。グラフは標準PNG（output/comedix/週報グラフ.png）として出力 → 資料室へ一度アップ
→ その library_refer.php URL を output/comedix/グラフ画像URL.txt に保存（または --chart-img-url）。
以後は毎週、同じ資料室文書へPNGを上書き＋このHTMLを貼り替えるだけで自動反映。URL未設定なら手順入りの
プレースホルダを表示する。

構成（詳細込み・ユーザ選択）:
  ① 今週の一手（ヒーロー／output/comedix/今週の一手.md を共有・編集可）
  ② portal準拠KPI3（在院=平日/休日デュアル）
  ③ 在院12週トレンド（library_refer.php の <img>・前年同期線／別出力の週報グラフ.png）
  ④ 病棟別テーブル（在院・病床利用率・入退院フロー・週末在院維持率）
  ⑤ 診療科別テーブル（在院・新入院・フロー・退院再配分率・全麻）
  ⑥ 凡例＋WEB版導線

数値は build_summary_context（= portal/WEB と同じ metrics）を再利用＝**完全一致**。
render_hero は元から class 不使用なのでそのまま流用。KPI/テーブル/凡例は inline-only 版をここで描画、
グラフは hs.render_trend_svg を Chrome screenshot で PNG 化（hs の class 版は PNG/PDF 用に温存）。

  python scripts/build_comedix_html.py [--base-date YYYY-MM-DD] [--refresh] [--with-all-trends] \
      [--chart-img-url 'library_refer.php?s=...&i=...&u=...png']
出力: output/comedix/単一HTML週報.html（貼付）＋ output/comedix/週報グラフ.png（資料室へアップ）
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/ を import 可能に

from app.lib import hospital_summary as hs
from app.lib import metrics
from app.lib.config import (
    DEFAULT_DATA_DIR, status_display, KPI_ICONS,
    TARGET_INPATIENT_ALLDAY, TARGET_INPATIENT_WEEKDAY, TARGET_INPATIENT_HOLIDAY,
    TARGET_ADMISSION_WEEKLY, TARGET_GA_DAILY,
)
try:
    from app.lib.config import REPORT_HOSPITAL_NAME
except Exception:
    REPORT_HOSPITAL_NAME = ""
from build_comedix_card import resolve_comment, log, WEB_URL, find_chrome  # 今週の一手.md/Chrome を共有

OUT_DIR = ROOT / "output" / "comedix"
CHART_PNG = OUT_DIR / "週報グラフ.png"
CHART_URL_FILE = OUT_DIR / "グラフ画像URL.txt"
SUB, INK = hs.SUB, hs.INK

# ── inline style 断片（hs.BASE_CSS のクラスを直書きに展開）──
S_TD = "border:1px solid #e6ebf1;padding:2px 6px;text-align:center;line-height:1.25"
S_TH = ("border:1px solid #e6ebf1;padding:3px 6px;text-align:center;background:#f2f5f9;"
        "font-weight:700;font-size:10.5px;color:#3a4656")
S_NM = ("border:1px solid #e6ebf1;padding:2px 6px;text-align:left;white-space:nowrap;font-weight:600")
S_SUB = "font-size:9px;color:#7a899a;font-weight:400"
S_TABLE = "border-collapse:collapse;width:100%;font-size:11px;margin:2px 0 4px"
S_SEC = "font-size:14px;font-weight:800;margin:16px 0 4px;color:#1f2d3d"


# ════════════════════════════════════════════════════════════
# inline-only セル/テーブル（hs の class 版を直書きに移植）
# ════════════════════════════════════════════════════════════
def _cell(sd: dict, html: str, extra: str = "") -> str:
    return f'<td style="{S_TD};background:{sd["bg"]};color:{sd["color"]}{extra}">{html}</td>'


def _ach(actual, target, rate, sd, emphasize=False) -> str:
    if actual is None:
        return _cell(status_display(None), "—")
    a = f"{actual:.0f}" if float(actual).is_integer() else f"{actual:.1f}"
    t = "—" if target is None else (f"{target:.0f}" if float(target).is_integer() else f"{target:.1f}")
    pct = f"{rate:.0f}%" if rate is not None else "—"
    wt = "900" if emphasize else "700"
    return (f'<td style="{S_TD};background:{sd["bg"]};color:{sd["color"]}">'
            f'<span style="font-weight:{wt}">{pct}</span><br>'
            f'<span style="{S_SUB}">{a}/{t}</span></td>')


def _flow_cell(fl: dict, bg: str) -> str:
    net = fl["net"]
    return (f'<td style="{S_TD};background:{bg}"><span style="{S_SUB}">入{fl["in"]:.0f} / 退{fl["out"]:.0f}</span><br>'
            f'<b style="color:{hs._flow_color(net)}">純{net:+.0f}</b></td>')


def render_kpi_cards_inline(kpi: dict) -> str:
    """portal準拠KPI3（在院=平日/休日デュアル）を inline-only で。"""
    wd_v, hd_v = kpi["inpatient_avg_7d_wd"], kpi["inpatient_avg_7d_hd"]
    wd_sd = status_display(metrics.achievement_rate(wd_v, TARGET_INPATIENT_WEEKDAY))
    hd_sd = status_display(metrics.achievement_rate(hd_v, TARGET_INPATIENT_HOLIDAY))
    card = ("flex:1 1 200px;box-sizing:border-box;border:1px solid #e0e6ee;border-radius:10px;"
            "padding:11px 13px;box-shadow:0 1px 3px rgba(0,0,0,.06)")
    lab = f"font-size:12px;color:{SUB}"

    def bar(rate, color):
        w = max(2.0, min(100.0, rate if rate is not None else 0))
        return (f'<div style="background:#e9edf3;border-radius:5px;height:7px;width:100%;margin-top:6px">'
                f'<div style="background:{color};border-radius:5px;height:7px;width:{w:.1f}%"></div></div>')

    def dual_row(name, v, tgt, sd):
        vtxt = "—" if v is None else f"{v:.1f}"
        rate = metrics.achievement_rate(v, tgt) if v is not None else None
        return (f'<div style="display:flex;align-items:baseline;gap:8px;margin:4px 0 0">'
                f'<span style="font-size:12px;font-weight:600;color:{SUB};min-width:30px">{name}</span>'
                f'<span style="font-size:22px;font-weight:900;line-height:1">{vtxt}'
                f'<span style="font-size:12px;font-weight:600;color:{SUB}">人</span></span>'
                f'<span style="font-size:11px;font-weight:600;color:{sd["color"]}">{sd["shape"]}{sd["text"]}</span>'
                f'<span style="font-size:11px;color:#9aa7b4;margin-left:auto">目標{tgt:g}</span></div>'
                + bar(rate, sd["color"]))

    inp_card = (
        f'<div style="{card};border-left:5px solid {wd_sd["color"]}">'
        f'<div style="{lab}">{KPI_ICONS.get("inpatient","")} 在院患者数 '
        f'<span style="{S_SUB}">直近7日平均（平日／休日）</span></div>'
        + dual_row("平日", wd_v, TARGET_INPATIENT_WEEKDAY, wd_sd)
        + dual_row("休日", hd_v, TARGET_INPATIENT_HOLIDAY, hd_sd) + '</div>')

    def single(kid, label, period, val, unit, gap, gu, sd, tgt, rate):
        vtxt = f"{val:.1f}" if unit == "件/日" else f"{val:.0f}"
        return (f'<div style="{card};border-left:5px solid {sd["color"]}">'
                f'<div style="{lab}">{KPI_ICONS.get(kid,"")} {label} <span style="{S_SUB}">{period}</span></div>'
                f'<div style="font-size:27px;font-weight:800;line-height:1.1;margin-top:3px">{vtxt}'
                f'<span style="font-size:13px;font-weight:400;color:{SUB}"> {unit}</span> '
                f'<span style="font-size:12px;color:{sd["color"]};font-weight:700">{sd["shape"]}{sd["text"]}</span></div>'
                f'<div style="margin-top:2px"><span style="color:{SUB};font-size:12px">目標差 {gap:+.1f}{gu}'
                f'<span style="color:#9aa7b4">（目標{tgt:g}）</span></span></div>'
                + bar(rate, sd["color"]) + '</div>')

    cards = [
        inp_card,
        single("admission", "新入院患者数", "直近7日累計", kpi["admission_actual_7d"], "人",
               kpi["admission_gap"], "人", kpi["admission_status"], TARGET_ADMISSION_WEEKLY,
               kpi["admission_rate_7d"]),
        single("operation", "全身麻酔手術", "直近1週・営業日平均", kpi["operation_daily_avg"], "件/日",
               kpi["operation_gap"], "件/日", kpi["operation_status"], TARGET_GA_DAILY,
               kpi["operation_rate"]),
    ]
    return f'<div style="display:flex;flex-wrap:wrap;gap:9px">{"".join(cards)}</div>'


def render_ward_table_inline(rows: list) -> str:
    head = (f'<tr><th style="{S_TH}">病棟</th>'
            f'<th style="{S_TH}">在院 <span style="{S_SUB}">実/目</span></th>'
            f'<th style="{S_TH}">病床利用率</th>'
            f'<th style="{S_TH}">入退院フロー <span style="{S_SUB}">直近7日</span></th>'
            f'<th style="{S_TH}">週末在院維持率</th></tr>')
    body = []
    for i, r in enumerate(rows):
        zb = "#fcfdfe" if i % 2 else "#ffffff"
        ex = r["exempt"]
        sd = hs._sd(r["inp_rate"], ex)
        util = "—" if r["util"] is None else f'{r["util"]:.1f}%'
        util_sub = f'<br><span style="{S_SUB}">{r["beds"]:g}床</span>' if r.get("beds") else ""
        rsd = hs._ret_sd(r["retention"], ex)
        ret = "—" if r["retention"] is None else f'{r["retention"]*100:.0f}%'
        tag = ' <span style="' + S_SUB + '">※</span>' if ex else ""
        body.append(
            f'<tr><td style="{S_NM};background:{zb}">{r["name"]}{tag}</td>'
            + _ach(r["inp_actual"], r["inp_target"], r["inp_rate"], sd)
            + _cell(sd, f'{util}{util_sub}')
            + _flow_cell(r["flow"], zb)
            + _cell(rsd, ret) + '</tr>')
    return f'<table style="{S_TABLE}"><thead>{head}</thead><tbody>{"".join(body)}</tbody></table>'


def render_dept_table_inline(rows: list) -> str:
    head = (f'<tr><th style="{S_TH}">診療科</th>'
            f'<th style="{S_TH}">在院 <span style="{S_SUB}">実/目</span></th>'
            f'<th style="{S_TH}">新入院 <span style="{S_SUB}">実/目</span></th>'
            f'<th style="{S_TH}">入退院フロー <span style="{S_SUB}">直近7日</span></th>'
            f'<th style="{S_TH}">退院再配分率</th>'
            f'<th style="{S_TH}">全麻 <span style="{S_SUB}">実/目</span></th></tr>')
    body, cur_type, i = [], None, 0
    grp = "font-size:11px;color:#41618a;font-weight:600;background:#eef3fb;text-align:left"
    for r in rows:
        if r["type"] != cur_type:
            cur_type = r["type"]
            i = 0
            body.append(f'<tr><td style="{S_TD};{grp}" colspan="6">{cur_type}系</td></tr>')
        zb = "#fcfdfe" if i % 2 else "#ffffff"
        i += 1
        ex = r["exempt"]
        med = (r["type"] == "内科")
        dsd = hs._redist_sd(r["redist"], ex)
        rd = "—" if r["redist"] is None else f'{r["redist"]:.0f}%'
        body.append(
            f'<tr><td style="{S_NM};background:{zb}">{r["name"]}{" ※" if ex else ""}</td>'
            + _ach(r["inp_actual"], r["inp_target"], r["inp_rate"], hs._sd(r["inp_rate"], ex), emphasize=med)
            + _ach(r["nadm_actual"], r["nadm_target"], r["nadm_rate"], hs._sd(r["nadm_rate"], ex), emphasize=med)
            + _flow_cell(r["flow"], zb)
            + _cell(dsd, rd)
            + _ach(r["surg_actual"], r["surg_target"], r["surg_rate"], hs._sd(r["surg_rate"], ex), emphasize=not med)
            + '</tr>')
    return f'<table style="{S_TABLE}"><thead>{head}</thead><tbody>{"".join(body)}</tbody></table>'


def render_legend_inline() -> str:
    so, sw, sd = status_display(100), status_display(95), status_display(80)

    def chip(s, t):
        return (f'<i style="display:inline-block;width:11px;height:11px;border-radius:3px;'
                f'vertical-align:-1px;margin:0 3px 0 10px;background:{s["bg"]};border:1px solid {s["color"]}"></i>{t}')
    return (f'<div style="font-size:11px;color:{SUB};margin:6px 0 2px">指標の見方：達成率%＋小さく実績/目標。'
            + chip(so, "達成") + chip(sw, "接近") + chip(sd, "未達")
            + '　／　純＝直近7日の入−退（＋増/−減）　／　※＝色評価の対象外（業務実態が異なる）</div>')


# ════════════════════════════════════════════════════════════
# トレンドグラフ（Comedixは<svg>を除去→PNG化してlibrary_refer.phpの<img>で配信）
# ════════════════════════════════════════════════════════════
def build_trend_svgs(ctx, with_all_trends: bool) -> list[str]:
    svgs = [hs.render_trend_svg(
        ctx["trends"]["inpatient"], ref=TARGET_INPATIENT_ALLDAY,
        ref_label=f"目標{TARGET_INPATIENT_ALLDAY:.0f}", unit="人",
        window_label="在院患者数の推移（12週・7日移動平均）")]
    if with_all_trends:
        svgs.append(hs.render_trend_svg(
            ctx["trends"]["admission"], ref=TARGET_ADMISSION_WEEKLY,
            ref_label=f"目標{TARGET_ADMISSION_WEEKLY:.0f}", unit="人/週",
            window_label="新入院患者数の推移（12週・週次ラン）", color="#0e7a54"))
        svgs.append(hs.render_trend_svg(
            ctx["trends"]["operation"], ref=TARGET_GA_DAILY,
            ref_label=f"目標{TARGET_GA_DAILY:.0f}", unit="件/日",
            window_label="全身麻酔手術の推移（12週・30営業平日移動平均）", color="#b45309"))
    return svgs


def render_chart_png(svgs: list[str], out_png: Path, width: int = 760) -> bool:
    """SVG断片群を白背景HTMLに包んで Chrome screenshot→余白トリム→PNG保存。"""
    chrome = find_chrome()
    if not chrome:
        log("Chrome/Chromium が見つかりません（グラフPNGをスキップ）。", "warn")
        return False
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    blocks = "".join(f'<div style="margin:6px 0">{s}</div>' for s in svgs)
    doc = (f'<!doctype html><meta charset="utf-8"><body style="margin:0;background:#fff">'
           f'<div style="width:{width}px;padding:10px">{blocks}</div></body>')
    tmp = Path(tempfile.mkdtemp(prefix="comedix_chart_"))
    card, raw = tmp / "chart.html", tmp / "raw.png"
    card.write_text(doc, encoding="utf-8")
    cmd = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
           "--force-device-scale-factor=2", f"--window-size={width+40},{220*len(svgs)+120}",
           "--default-background-color=FFFFFFFF", f"--screenshot={raw}", card.as_uri()]
    subprocess.run(cmd, capture_output=True, text=True)
    if not raw.exists():
        log("グラフPNGの screenshot に失敗。", "warn")
        shutil.rmtree(tmp, ignore_errors=True)
        return False
    from PIL import Image, ImageChops
    im = Image.open(raw).convert("RGB")
    bbox = ImageChops.difference(im, Image.new("RGB", im.size, (255, 255, 255))).getbbox()
    if bbox:
        m = 16
        x0, y0, x1, y1 = bbox
        im = im.crop((max(0, x0 - m), max(0, y0 - m), min(im.width, x1 + m), min(im.height, y1 + m)))
    im.save(out_png)
    shutil.rmtree(tmp, ignore_errors=True)
    log(f"グラフPNG 生成: {out_png}  ({im.width}×{im.height}px) → 資料室へアップ", "ok")
    return True


def resolve_chart_url(arg_url: str | None) -> str | None:
    """--chart-img-url 指定時はファイルへ保存して採用。無ければ グラフ画像URL.txt の最初の非#行。"""
    if arg_url and arg_url.strip():
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        CHART_URL_FILE.write_text(arg_url.strip() + "\n", encoding="utf-8")
        return arg_url.strip()
    if CHART_URL_FILE.exists():
        for ln in CHART_URL_FILE.read_text(encoding="utf-8").splitlines():
            if ln.strip() and not ln.lstrip().startswith("#"):
                return ln.strip()
    return None


def chart_block(url: str | None) -> str:
    """グラフ枠：library URL があれば <img>、無ければ手順入りプレースホルダ。"""
    cap = ('<div style="font-size:11px;color:#7a899a;text-align:center;margin-top:3px">'
           '在院患者数の推移（12週・7日移動平均／点線＝前年同期・目標）</div>')
    if url:
        safe = url.replace("&", "&amp;") if "&amp;" not in url else url
        return (f'<div style="text-align:center;margin:6px 0">'
                f'<img src="{safe}" alt="在院患者数トレンド" '
                f'style="max-width:100%;border:1px solid #e0e6ee;border-radius:8px" />{cap}</div>')
    return (
        '<div style="margin:6px 0;padding:14px;border:2px dashed #b9c6d6;border-radius:8px;'
        'background:#f7faff;color:#41618a;font-size:12.5px;line-height:1.7">'
        '<b>📈 トレンドグラフ（要1回設定）</b><br>'
        'Comedixは &lt;svg&gt; を消すため、グラフは画像配信します：'
        '① <b>output/comedix/週報グラフ.png</b> を資料室へアップ → '
        '② 表示された <b>library_refer.php?...</b> のURLを <b>output/comedix/グラフ画像URL.txt</b> に保存'
        '（または <code>--chart-img-url</code>）→ ③ 次回からこの枠に自動で &lt;img&gt; が入ります。'
        '<br><span style="color:#7a899a">※ 以後は同じ資料室文書へPNGを上書きするだけでURL不変・自動更新。</span></div>')


# ════════════════════════════════════════════════════════════
# ページ組立（インラインstyleのみの断片）
# ════════════════════════════════════════════════════════════
def build_fragment(ctx, headline, body, trend_html: str) -> str:
    bd = ctx["base_date"]
    period = f"{bd - timedelta(days=6):%Y年%-m月%-d日}〜{bd:%-m月%-d日}（直近7日）"
    title = (REPORT_HOSPITAL_NAME + "　") if REPORT_HOSPITAL_NAME else ""
    hero = hs.render_hero(headline, body, ctx["hero"]["chips"])
    kpis = render_kpi_cards_inline(ctx["kpi"])

    ward = render_ward_table_inline(ctx["ward_rows"])
    dept = render_dept_table_inline(ctx["dept_rows"])
    legend = render_legend_inline()

    return f"""<!-- ▼▼ Comedixお知らせ「HTMLソースモード」へ そのまま貼付（テーブル/KPIは画像不要・毎週貼り替え）▼▼ -->
<!-- 注: トレンドだけ library_refer.php の <img>（Comedixは<svg>を除去するため）。週報グラフ.png を資料室へアップしURLを設定 -->
<div style="font-family:'Hiragino Kaku Gothic ProN','Hiragino Sans','Noto Sans JP',sans-serif;color:{INK};max-width:880px;margin:0 auto">

  <h2 style="text-align:center;border-bottom:2px solid #2b5797;padding-bottom:8px;color:{INK};margin:0 0 6px">
    <span style="font-size:medium">【<span style="background-color:#ffff00">全職員向け 週報</span>】{title}病院全体KPI</span></h2>
  <p style="text-align:center;font-size:13px;color:{SUB};margin:0 0 12px">{period}　|　数字を覚える必要はありません—各指標の
    <b style="color:#0e7a54">緑</b>＝目標到達、<b style="color:#c4314b">赤</b>＝未達の度合いです。</p>

  {hero}

  <div style="{S_SEC}">📣 今週のKPI</div>
  {kpis}

  <div style="{S_SEC}">📈 トレンド</div>
  {trend_html}

  <div style="{S_SEC}">🏥 病棟別の状況</div>
  {ward}

  <div style="{S_SEC}">🩺 診療科別の状況</div>
  {dept}

  {legend}

  <h3 style="margin:22px 0 6px;border-bottom:2px solid #ccc;padding-bottom:6px">
    <span style="font-size:small">🌐 <span style="background-color:#cce5ff">WEB版週報（フル機能・グラフ操作可）</span></span></h3>
  <div style="display:flex;align-items:center;justify-content:center;padding:14px;background-color:#f9f9f9;border-radius:8px">
    <div style="flex:1;padding-right:18px;font-size:14px;line-height:1.6;color:#333">
      スマホ・PCから全診療科／全病棟の強み弱みを確認できます。<br />画面を回転させるとグラフを横向きで拡大できます。<br />
      <span style="font-size:12px;font-weight:bold;color:#0066cc">{WEB_URL}</span></div>
    <!-- ↓ page.html で動作実績のある実物QRの <img src="library_refer.php?..."> をここに貼ると画面内表示されます -->
    <div style="flex-shrink:0;text-align:center"><div style="width:80px;height:80px;border:4px solid #fff;
         box-shadow:0 0 6px rgba(0,0,0,.2);background:#ddd;display:flex;align-items:center;justify-content:center;font-size:10px;color:#888">QR</div></div>
  </div>

  <p style="text-align:right;font-size:11px;color:{SUB};margin-top:10px">基準日 {bd:%Y-%m-%d}　／　数値はWEB版・portalと一致</p>
</div>
<!-- ▲▲ ここまで貼付 ▲▲ -->
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--base-date", default=None)
    ap.add_argument("--refresh", action="store_true", help="今週の一手.md を自動下書きで作り直す")
    ap.add_argument("--with-all-trends", action="store_true",
                    help="在院に加え 新入院/全麻 のトレンドもグラフPNGに含める")
    ap.add_argument("--chart-img-url", default=None,
                    help="グラフ画像の library_refer.php URL（保存して以後自動採用）")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    from generate_html import load_and_preprocess
    log("データ読込・前処理中（load_and_preprocess）...")
    adm, surg, targets, surg_targets, _pm, base_date, _ = \
        load_and_preprocess(args.data_dir, args.base_date, no_validate=False)
    ctx = hs.build_summary_context(adm, surg, targets, surg_targets, base_date)
    headline, body = resolve_comment(ctx["hero"], args.refresh)

    # トレンドは PNG 化（資料室へアップ）→ HTMLは library_refer.php の <img> で参照
    render_chart_png(build_trend_svgs(ctx, args.with_all_trends), CHART_PNG)
    url = resolve_chart_url(args.chart_img_url)
    frag = build_fragment(ctx, headline, body, chart_block(url))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else (OUT_DIR / "単一HTML週報.html")
    out.write_text(frag, encoding="utf-8")
    log(f"単一HTML週報 生成: {out}  ({len(frag):,} bytes)", "ok")
    if not url:
        log("グラフURL未設定：本文にプレースホルダ表示中。週報グラフ.png を資料室へアップ→ "
            "--chart-img-url か グラフ画像URL.txt にURLを保存すると次回から <img> 表示。", "warn")
    print()
    log("運用: このHTMLを Comedixお知らせ『HTMLソースモード』へ貼付（毎週）。グラフは資料室の同じ文書へPNG上書き", "info")
    log("今週の一手: output/comedix/今週の一手.md を編集して再実行（REFRESH=1で自動下書きへ）", "info")


if __name__ == "__main__":
    main()
