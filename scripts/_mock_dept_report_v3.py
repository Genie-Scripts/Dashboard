#!/usr/bin/env python3
"""_mock_dept_report_v3.py — 部門別レポートPDF 構成見直し版モック（種別ごと優先順・仕上げ版）。

グラフパーツ A.在院 B.新入院 C.全麻手術 D.粗利 E.曜日プロファイル を、ユニット種別ごとの
優先順で配置。優先1位=全幅ヒーロー、以降は読み順(=優先順)で半幅2列、⭐一手が端を埋める。
  外科系診療科: C, D, E, B, A   内科系診療科: A, D, B, E   病棟: A(病床利用率), B, E
本物のレンダラー(render_trend_svg 相当＝高さ可変版 / _render_dow_svg)＋ダミーデータ。合意形成用。
出力: spec/dept_report_v3_mock.html / .pdf
"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.lib.dept_report import _render_dow_svg

INK, SUB, PREV = "#1f2d3d", "#5a6b7b", "#b9c2cd"
COL = {"A": "#2a9d8f", "B": "#3d5a80", "C": "#2b6cb0", "D": "#bf8a2e"}
ICON = {"A": "🛏", "B": "➕", "C": "🔪", "D": "💴", "E": "📊"}
TITLE = {"A": "在院患者数", "B": "新入院患者数", "C": "全身麻酔手術", "D": "粗利", "E": "曜日プロファイル"}
WIN = {"A": "12週・7日移動平均", "B": "12週・週次", "C": "12週・30営業平日MA",
       "D": "12か月・月次", "E": "曜日別 日平均（直近8週）"}
UNITLBL = {"A": "人", "B": "件/週", "C": "件/日", "D": "百万円"}

WK12 = ["04/02", "04/09", "04/16", "04/23", "04/30", "05/07",
        "05/14", "05/21", "05/28", "06/04", "06/11", "06/18"]
MON12 = ["7月", "8月", "9月", "10月", "11月", "12月", "1月", "2月", "3月", "4月", "5月", "6月"]


# ── トレンドSVG（hospital_summary.render_trend_svg の高さ可変コピー）──
def trend_svg(data, ref, ref_label, unit, window_label, color, height=210):
    dates, cur, prev = data["dates"], data["cur"], data["prev"]
    pts = [v for v in cur if v is not None] + [v for v in prev if v is not None] + [ref]
    if not pts:
        return ""
    W, H, L, R, T, B = 760, height, 50, 700, 24, height - 42
    lo, hi = min(pts), max(pts)
    pad = max((hi - lo) * 0.18, 2)
    y0, y1 = lo - pad, hi + pad
    n = len(cur)
    X = lambda i: L + (R - L) * (i / (n - 1)) if n > 1 else (L + R) / 2
    Y = lambda v: B - (B - T) * ((v - y0) / (y1 - y0))
    el = [f'<text x="{L}" y="14" font-size="12" font-weight="700" fill="{SUB}">{window_label}'
          f'<tspan font-size="10.5" fill="#9aa7b4">（{unit}）</tspan></text>']
    for g in range(3):
        v = y0 + (y1 - y0) * (g + 0.5) / 3
        yy = Y(v)
        el.append(f'<line x1="{L}" y1="{yy:.1f}" x2="{R}" y2="{yy:.1f}" stroke="#eef2f7"/>')
        el.append(f'<text x="{L-7:.1f}" y="{yy+3.5:.1f}" font-size="10.5" fill="#9aa7b4" text-anchor="end">{v:.0f}</text>')
    yr = Y(ref)
    el.append(f'<line x1="{L}" y1="{yr:.1f}" x2="{R}" y2="{yr:.1f}" stroke="#9aa7b4" stroke-width="1.2" stroke-dasharray="5 4"/>')
    el.append(f'<text x="{R+4:.1f}" y="{yr+3.5:.1f}" font-size="10.5" fill="#9aa7b4" font-weight="700">{ref_label}</text>')

    def path(vals, stroke, w, dash=False):
        seg = []
        for i, v in enumerate(vals):
            if v is None:
                continue
            seg.append(f'{"M" if not seg else "L"}{X(i):.1f} {Y(v):.1f}')
        if not seg:
            return ""
        d = ' stroke-dasharray="4 3"' if dash else ""
        return f'<path d="{" ".join(seg)}" fill="none" stroke="{stroke}" stroke-width="{w}" stroke-linejoin="round"{d}/>'

    el.append(path(prev, PREV, 1.8, dash=True))
    el.append(path(cur, color, 2.6))
    el.append(f'<text x="{X(0):.1f}" y="{B+15:.1f}" font-size="10" fill="{SUB}" text-anchor="middle">{dates[0]}</text>')
    el.append(f'<text x="{X(n-1):.1f}" y="{B+15:.1f}" font-size="10" fill="{SUB}" text-anchor="middle">{dates[-1]}</text>')
    last = next((v for v in reversed(cur) if v is not None), None)
    if last is not None:
        yy = Y(last)
        el.append(f'<circle cx="{X(n-1):.1f}" cy="{yy:.1f}" r="4" fill="{color}"/>')
        el.append(f'<text x="{X(n-1):.1f}" y="{yy-9:.1f}" font-size="12.5" fill="{color}" text-anchor="end" font-weight="900">{last:g}</text>')
    el.append(f'<text x="{R}" y="14" font-size="10" fill="{PREV}" text-anchor="end">― 前年同期</text>')
    return f'<svg viewBox="0 0 {W} {H}" width="100%" style="display:block">' + "".join(el) + "</svg>"


# ── チャート仕様（描画はレイアウト時に高さ確定）──────────────────
def spec(kind, **kw):
    return {"kind": kind, **kw}


def render_chart_svg(sp, hero):
    k = sp["kind"]
    if k == "E":
        return _render_dow_svg(sp["dis"], sp["adm"], sp["cen"])
    h = 256 if hero else 232
    return trend_svg({"dates": sp.get("dates", WK12), "cur": sp["cur"], "prev": sp["prev"]},
                     sp["ref"], sp["ref_label"], UNITLBL[k], WIN[k], COL[k], height=h)


def A(cur, prev, ref, badge, num="人", title=None):
    return spec("A", cur=cur, prev=prev, ref=ref,
                ref_label=("定員100%" if num == "%" else f"目標{ref:g}"),
                badge=badge, title=title, _num=num)


def B(cur, prev, ref, badge):
    return spec("B", cur=cur, prev=prev, ref=ref, ref_label=f"目標{ref:g}", badge=badge)


def C(cur, prev, ref, badge):
    return spec("C", cur=cur, prev=prev, ref=ref, ref_label=f"目標{ref:g}", badge=badge)


def D(cur, prev, ref, badge):
    return spec("D", cur=cur, prev=prev, ref=ref, ref_label=f"目標{ref:g}", dates=MON12,
                badge=badge, note="※当月(6月)は月末見込み・確報は20日頃")


def E(dis, adm, cen):
    return spec("E", dis=dis, adm=adm, cen=cen)


def kpi(label, sub, val, unit, *, lead=False, badge=None):
    return {"label": label, "sub": sub, "val": val, "unit": unit, "lead": lead, "badge": badge}


# ── 3バリアント（ダミー）───────────────────────────────────────
def surgical_sheet():
    return {"unit": "整形外科", "type_label": "外科系・診療科版",
            "subtitle": "診療科パフォーマンスレポート",
            "kpis": [kpi("手術（全麻・週）", "直近7日", "25", "件", lead=True, badge=("105%", "ok")),
                     kpi("粗利 達成率", "当月見込み", "98", "%", badge=("見込", "wr")),
                     kpi("在院患者数", "直近7日平均", "55", "人", badge=("102%", "ok")),
                     kpi("新入院", "週", "25", "件", badge=("104%", "ok"))],
            "charts": [
                C([3.1, 3.3, 3.0, 3.6, 3.4, 3.7, 3.6, 3.9, 3.8, 4.0, 4.1, 4.2],
                  [3.4, 3.3, 3.5, 3.2, 3.1, 3.3, 3.4, 3.2, 3.5, 3.4, 3.3, 3.6], 4.0, ("達成率 105%", "ok")),
                D([128, 134, 121, 139, 132, 145, 138, 142, 150, 136, 148, 152],
                  [132, 129, 135, 126, 130, 138, 134, 131, 142, 135, 140, 138], 140, ("達成率 98%", "wr")),
                E([2.6, 2.3, 2.5, 2.8, 4.2, 1.1, 0.7], [3.4, 3.2, 3.3, 3.0, 2.4, 0.5, 0.3], [53, 54, 54, 53, 52, 49, 48]),
                B([20, 22, 19, 23, 21, 24, 22, 25, 23, 24, 26, 25],
                  [22, 21, 23, 20, 21, 23, 22, 21, 24, 22, 23, 24], 24, ("達成率 104%", "ok")),
                A([50, 51, 50, 52, 51, 53, 52, 54, 53, 54, 55, 55],
                  [52, 51, 53, 50, 51, 53, 52, 51, 54, 52, 53, 54], 54, ("達成率 102%", "ok"))],
            "move_body": "手術件数・粗利は目標を概ね達成。一方 <b>退院が金曜に集中</b>し週末の在院がやや落ちます。",
            "move_act": "手術枠は維持しつつ、金曜に寄った退院を木曜までへ一部前倒し。週末の空床を予定入院で補充。"}


def internal_sheet():
    return {"unit": "消化器内科", "type_label": "内科系・診療科版",
            "subtitle": "診療科パフォーマンスレポート",
            "kpis": [kpi("在院患者数", "直近7日平均", "44.6", "人", lead=True, badge=("99%", "ok")),
                     kpi("粗利 達成率", "当月見込み", "106", "%", badge=("見込", "ok")),
                     kpi("新入院", "週", "24", "件", badge=("104%", "ok")),
                     kpi("週末 在院維持率", "土日/平日", "91.8", "%")],
            "charts": [
                A([38.5, 39.2, 38.8, 40.1, 41.5, 40.8, 42.2, 43.0, 42.5, 43.8, 44.2, 44.6],
                  [40.1, 39.8, 40.5, 39.2, 38.8, 39.5, 40.2, 39.8, 40.5, 41.0, 40.2, 39.8], 45, ("達成率 99%", "ok")),
                D([95, 99, 91, 104, 98, 108, 102, 106, 112, 101, 110, 114],
                  [98, 96, 100, 94, 97, 103, 100, 98, 106, 100, 104, 106], 108, ("達成率 106%", "ok")),
                B([19, 21, 18, 22, 20, 23, 21, 24, 22, 23, 25, 24],
                  [21, 20, 22, 19, 20, 22, 21, 20, 23, 21, 22, 23], 23, ("達成率 104%", "ok")),
                E([3.2, 2.8, 3.0, 3.4, 5.1, 1.2, 0.8], [4.1, 3.8, 4.0, 3.5, 2.9, 0.6, 0.4], [44, 45, 45, 44, 43, 40, 39])],
            "move_body": "在院・粗利とも上向きで目標圏。<b>退院が金曜に集中</b>し土日の入院補充が乏しく、週末に在院が落ちます。",
            "move_act": "金曜の退院を月〜木へ少し分散し、予定入院を週後半へ寄せて空床を補充。在院日数は延ばさず回転で。"}


def ward_sheet():
    return {"unit": "7階B病棟", "type_label": "病棟版",
            "subtitle": "病棟パフォーマンスレポート",
            "kpis": [kpi("病床利用率", "直近7日平均", "92", "%", lead=True, badge=("稼働50床", "muted")),
                     kpi("在院患者数", "直近7日平均", "47.8", "人"),
                     kpi("新入院", "週", "16", "件", badge=("100%", "ok")),
                     kpi("週末 在院維持率", "土日/平日", "91.0", "%")],
            "charts": [
                A([82, 84, 83, 86, 88, 87, 89, 90, 88, 91, 90, 92],
                  [85, 84, 86, 83, 82, 84, 85, 83, 86, 85, 84, 86], 100, ("対定員 92%", "wr"),
                  num="%", title="病床利用率"),
                B([12, 14, 11, 15, 13, 16, 14, 15, 16, 15, 17, 16],
                  [13, 12, 14, 11, 12, 14, 13, 12, 15, 13, 14, 15], 16, ("達成率 100%", "ok")),
                E([2.4, 2.1, 2.3, 2.6, 3.8, 1.0, 0.7], [3.0, 2.8, 2.9, 2.6, 2.2, 0.5, 0.3], [48, 49, 49, 48, 47, 44, 43])],
            "move_body": "平日の利用率は高い一方、<b>週末の入院受け入れが手薄</b>で空床が週末に残りやすい状態です。",
            "move_act": "週末の入院受け入れを強化して空床を補充。相乗り科の金曜退院を平日へ少し分散。"}


# ── テンプレート ─────────────────────────────────────────────────
CSS = """
:root{--ink:#1a2332;--sub:#5f7084;--mu:#9daab8;--ln:#dfe5ed;--bg:#f6f8fb;--s:#fff;
  --ok:#0e7a54;--ok-bg:#ecfdf5;--wr:#b45309;--wr-bg:#fef7ee;--dr:#c4314b;--cen:#2a9d8f;--in:#3d5a80}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",sans-serif;
  background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased;line-height:1.5}
.sheet{width:190mm;min-height:277mm;margin:0 auto;background:var(--s);padding:11mm 11mm 8mm;
  display:flex;flex-direction:column}
.sheet+.sheet{break-before:page;page-break-before:always}
.letterhead{font-size:11px;font-weight:800;color:var(--sub);text-align:right;margin-bottom:3px}
.hd{display:flex;align-items:flex-end;justify-content:space-between;
  border-bottom:2.5px solid var(--ink);padding-bottom:7px;margin-bottom:9px}
.hd-l{display:flex;align-items:baseline;gap:10px}
.hd-dept{font-size:23px;font-weight:900;letter-spacing:.5px}
.hd-axis{font-size:10.5px;font-weight:800;color:var(--in);background:#eef2f9;border:1px solid #d3deef;
  border-radius:6px;padding:2px 8px;position:relative;top:-3px}
.hd-title{font-size:12.5px;font-weight:800;color:var(--sub)}
.hd-r{text-align:right;font-size:10px;color:var(--sub);line-height:1.55}
.hd-r b{color:var(--ink);font-weight:800}
.pill-conf{display:inline-block;font-size:9.5px;font-weight:900;color:var(--dr);border:1px solid #e9c2cb;
  background:#fdf0f2;border-radius:999px;padding:1px 8px;margin-top:2px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:8px}
.kpi{border:1px solid var(--ln);border-radius:10px;padding:9px 11px 10px;display:flex;flex-direction:column;gap:1px;position:relative}
.kpi.lead{border-color:#bfe3d6;background:#f4fdf9}
.kpi .k-lbl{font-size:9.5px;font-weight:800;color:var(--sub)}
.kpi .k-lbl span{font-weight:700;color:var(--mu);font-size:8.5px}
.kpi .k-val{font-size:24px;font-weight:900;line-height:1.05;letter-spacing:-.5px}
.kpi .k-val small{font-size:11px;font-weight:800;color:var(--sub);margin-left:1px}
.kpi.lead .k-val{color:var(--cen)}
.kbadge{position:absolute;top:8px;right:9px;font-size:8.5px;font-weight:900;border-radius:999px;padding:1px 6px}
.kbadge.ok{background:var(--ok-bg);color:#065f42}
.kbadge.wr{background:var(--wr-bg);color:#7c3a06}
.kbadge.muted{background:#f1f4f8;color:var(--mu)}
.prio{display:flex;gap:6px;align-items:center;margin-bottom:8px;font-size:9.5px;color:var(--sub)}
.prio b{font-weight:800;color:var(--ink)}
.prio .ord{font-size:9px;font-weight:900;color:var(--in);background:#eef2f9;border:1px solid #d3deef;
  border-radius:5px;padding:1px 6px}
.grid{display:flex;flex-direction:column;gap:9px;flex:1}
.row{display:grid;grid-template-columns:1fr 1fr;gap:9px;flex:1}
.card{border:1px solid var(--ln);border-radius:11px;padding:9px 11px 6px;background:var(--s);
  display:flex;flex-direction:column}
.card.hero{border-color:#cdd8e6}
.card-h{display:flex;align-items:center;gap:6px;margin-bottom:2px}
.card-h .ttl{font-size:11.5px;font-weight:900}
.badge{font-size:9px;font-weight:900;border-radius:999px;padding:1px 7px;margin-left:auto}
.badge.ok{background:var(--ok-bg);color:#065f42;border:1px solid #bfe3d6}
.badge.wr{background:var(--wr-bg);color:#7c3a06;border:1px solid #f1d9bd}
.badge.muted{background:#f1f4f8;color:var(--mu);border:1px solid var(--ln)}
.card .pr{font-size:8px;font-weight:900;color:#fff;background:var(--in);border-radius:4px;padding:1px 5px}
.cnote{font-size:8.5px;color:var(--mu);font-weight:700;margin:1px 0 0 2px}
.svgwrap{flex:1;display:flex;align-items:center}
.svgwrap svg{width:100%}
.dow-legend{display:flex;gap:9px;font-size:9px;font-weight:800;color:var(--sub);margin:1px 0 2px 2px}
.dow-legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:3px;vertical-align:-1px}
.dow-legend .ln{width:13px;height:0;border-top:2.5px solid var(--cen);margin-right:4px;vertical-align:3px}
.move{border:1px solid #cfe0d8;border-left:5px solid var(--ok);border-radius:11px;
  background:linear-gradient(180deg,#f4fdf9,#fff);padding:10px 13px 11px;display:flex;flex-direction:column;justify-content:center}
.move-h{display:flex;align-items:center;gap:6px;margin-bottom:5px}
.move-h .star{font-size:15px}
.move-h .ttl{font-size:12.5px;font-weight:900;color:#0b5e41}
.move-h .ai{font-size:8px;font-weight:800;color:var(--mu);border:1px solid var(--ln);border-radius:999px;
  padding:1px 6px;margin-left:auto}
.move-body{font-size:10.5px;line-height:1.6;color:var(--ink)}
.move-body b{font-weight:900}
.move-act{display:flex;align-items:flex-start;gap:7px;margin-top:8px;padding:8px 10px;background:#fff;
  border:1px dashed #b6d6c8;border-radius:8px}
.move-act .arw{color:var(--ok);font-weight:900;font-size:12px}
.move-act .txt{font-size:10.5px;font-weight:800;color:#0b5e41;line-height:1.55}
.ft{display:flex;justify-content:space-between;align-items:center;margin-top:9px;padding-top:6px;
  border-top:1px solid var(--ln);font-size:8.5px;color:var(--mu)}
.ft b{color:var(--sub);font-weight:800}
@page{size:A4 portrait;margin:0}
@media print{body{background:#fff}.sheet{width:auto;min-height:auto;margin:0}}
"""

PRIO_TXT = {
    "外科系・診療科版": "C 手術 → D 粗利 → E 曜日 → B 新入院 → A 在院",
    "内科系・診療科版": "A 在院 → D 粗利 → B 新入院 → E 曜日",
    "病棟版": "A 病床利用率 → B 新入院 → E 曜日",
}


def badge_html(b):
    return f'<span class="badge {b[1]}">{b[0]}</span>' if b else ""


def chart_card(sp, order, hero=False):
    name = sp.get("title") or TITLE[sp["kind"]]
    legend = ""
    if sp["kind"] == "E":
        legend = ('<div class="dow-legend"><span><i style="background:#e07a5f"></i>退院/退出</span>'
                  '<span><i style="background:#3d5a80"></i>入院/流入</span>'
                  '<span><i class="ln"></i>在院指数(平日=100)</span></div>')
    note = f'<div class="cnote">{sp["note"]}</div>' if sp.get("note") else ""
    svg = render_chart_svg(sp, hero)
    return (f'<div class="card{" hero" if hero else ""}">'
            f'<div class="card-h"><span class="pr">優先{order}</span>'
            f'<span class="ttl">{ICON[sp["kind"]]} {name}</span>{badge_html(sp.get("badge"))}</div>'
            f'{legend}<div class="svgwrap">{svg}</div>{note}</div>')


def move_card(s):
    return (f'<div class="move"><div class="move-h"><span class="star">⭐️</span>'
            f'<span class="ttl">この期間の一手</span><span class="ai">AI要約／院内データ</span></div>'
            f'<div class="move-body">{s["move_body"]}</div>'
            f'<div class="move-act"><span class="arw">▶</span><span class="txt">{s["move_act"]}</span></div></div>')


def render_sheet(s):
    cs = s["charts"]
    body = [f'<div style="flex:1.25;display:flex;flex-direction:column">{chart_card(cs[0], 1, hero=True)}</div>']
    rest, i, order = cs[1:], 0, 2
    while i < len(rest):
        a = chart_card(rest[i], order)
        if i + 1 < len(rest):
            body.append(f'<div class="row">{a}{chart_card(rest[i+1], order+1)}</div>')
            i += 2; order += 2
        else:
            body.append(f'<div class="row">{a}{move_card(s)}</div>')
            break
    else:
        body.append(f'<div style="flex:.7;display:flex;flex-direction:column">{move_card(s)}</div>')

    kpi_html = ""
    for k in s["kpis"]:
        kb = f'<span class="kbadge {k["badge"][1]}">{k["badge"][0]}</span>' if k["badge"] else ""
        kpi_html += (f'<div class="kpi{" lead" if k["lead"] else ""}">{kb}'
                     f'<div class="k-lbl">{k["label"]} <span>{k["sub"]}</span></div>'
                     f'<div class="k-val">{k["val"]}<small>{k["unit"]}</small></div></div>')

    return f"""
<div class="sheet">
  <div class="letterhead">○○総合病院</div>
  <div class="hd">
    <div class="hd-l"><div class="hd-dept">{s['unit']}</div>
      <span class="hd-axis">{s['type_label']}</span>
      <div class="hd-title">{s['subtitle']}</div></div>
    <div class="hd-r">集計 <b>直近12週/12か月</b><br>基準日 <b>2026/06/18</b>　発行 2026/06/20<br>
      <span class="pill-conf">院内限り</span></div>
  </div>
  <div class="kpis">{kpi_html}</div>
  <div class="prio"><span class="ord">表示優先順</span><b>{PRIO_TXT.get(s['type_label'],'')}</b>
    <span style="margin-left:auto">＝種別で構成・序列が変わる（上＝主役）</span></div>
  <div class="grid">{''.join(body)}</div>
  <div class="ft"><span>出典：入退院クロス（電カル）／手術台帳／月次粗利 ｜ <b>※モック・ダミーデータ</b></span>
    <span><b>診療ダッシュボード</b>　自動生成</span></div>
</div>"""


def main():
    sheets = [surgical_sheet(), internal_sheet(), ward_sheet()]
    html = (f'<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">'
            f'<meta name="robots" content="noindex,nofollow"><title>部門レポート 構成見直し版モック</title>'
            f'<style>{CSS}</style></head><body>'
            + "".join(render_sheet(s) for s in sheets) + "</body></html>")
    out = Path("spec/dept_report_v3_mock.html")
    out.write_text(html, encoding="utf-8")
    print(f"HTML: {out.resolve()}")

    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as tf:
        tf.write(html); tmp = Path(tf.name)
    pdf = Path("spec/dept_report_v3_mock.pdf")
    subprocess.run([chrome, "--headless", "--disable-gpu", "--no-sandbox", "--no-pdf-header-footer",
                    f"--print-to-pdf={pdf}", tmp.resolve().as_uri()],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"PDF : {pdf.resolve()}" if pdf.exists() else "PDF 失敗")
    tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
