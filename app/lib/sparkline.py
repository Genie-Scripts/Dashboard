"""sparkline.py — 訴求力強化 Phase 2 B7: portal KPIカード用の豆粒トレンドSVG。

`app/lib/hospital_summary.py` の `render_trend_svg` は軸ラベル・凡例・当月見込み等が多く
豆粒表示には過剰なため、本モジュールへ最小構成（折れ線1本＋目標破線1本＋終点ドット1つ・
軸/凡例なし）で自前実装する（hospital_summary.py は別ワーカーがPDF用に編集中のため触らない
＝訴求力強化 Phase 2 委譲仕様）。

詳細: spec/設計_訴求力強化_B_表示系.md B7、spec/改修プラン_訴求力強化.md §7 B9裁定
（線色は #2b6cb0 に統一済み。既定色はその値をそのまま使う）。
"""
from __future__ import annotations

SPARK_COLOR = "#2b6cb0"


def render_sparkline_svg(values: list, ref: float | None = None,
                         color: str = SPARK_COLOR,
                         width: int = 120, height: int = 40) -> str:
    """軸・凡例なしの豆粒トレンドSVGを返す。

    values: 末尾が最新の数値列（None混在可）。空、または全件Noneなら空文字列を返す。
    ref: 目標値。指定があれば薄い破線を1本引く。
    color: 折れ線・終点ドットの色（既定はB9で統一した当年線色）。
    """
    if not values:
        return ""
    pts = [v for v in values if v is not None]
    if not pts:
        return ""

    all_pts = pts + ([ref] if ref is not None else [])
    lo, hi = min(all_pts), max(all_pts)
    span = (hi - lo) or (abs(hi) * 0.1) or 1.0
    pad_x, pad_y = 2.0, 4.0
    n = len(values)

    def X(i):
        return pad_x + (width - 2 * pad_x) * (i / (n - 1)) if n > 1 else width / 2.0

    def Y(v):
        return height - pad_y - (height - 2 * pad_y) * ((v - lo) / span)

    el = []
    if ref is not None:
        yr = Y(ref)
        el.append(
            f'<line x1="{pad_x:.1f}" y1="{yr:.1f}" x2="{width - pad_x:.1f}" y2="{yr:.1f}" '
            f'stroke="#9aa7b4" stroke-width="1" stroke-dasharray="3 2"/>'
        )

    seg, started = [], False
    last_i, last_v = None, None
    for i, v in enumerate(values):
        if v is None:
            started = False
            continue
        seg.append(f'{"M" if not started else "L"}{X(i):.1f} {Y(v):.1f}')
        started = True
        last_i, last_v = i, v
    if seg:
        el.append(
            f'<path d="{" ".join(seg)}" fill="none" stroke="{color}" '
            f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
        )
    if last_i is not None:
        el.append(f'<circle cx="{X(last_i):.1f}" cy="{Y(last_v):.1f}" r="2.6" fill="{color}"/>')

    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'style="display:block" role="img" aria-label="トレンド">' + "".join(el) + "</svg>"
    )
