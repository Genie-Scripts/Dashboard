#!/usr/bin/env python3
"""build_weekly_digest.py — 週次ダイジェスト自動生成（掲示A4＋メール貼付テキスト）。

pull型（毎日更新の portal/detail/dept）に加え、push型の周知チャネルとして
直近7日 vs 前週の確定差分・当月着地見込み・要注視・改善を1枚のA4／プレーン
テキストにまとめる。ローカル運用専用（output/ はgitignore・非公開）。

  python scripts/build_weekly_digest.py                  # AI要約あり（oMLX起動が前提）
  python scripts/build_weekly_digest.py --no-ai           # 確定差分の箇条書きのみ（oMLX不要・高速）
  python scripts/build_weekly_digest.py --base-date YYYY-MM-DD

出力: output/weekly_digest/{基準日}/週次ダイジェスト_{基準日}.{html,pdf,txt}
     ／週次掲示_{基準日}.{html,pdf}（B13: 壁掲示用・大きな数字3つ＋1文＋QRのみ）
PDF化は headless Chrome（scripts.build_dept_reports の find_chrome/html_to_pdf を再利用）。

--base-date 未指定時はデータ最終日を直近日曜（完全週=月〜日の終端）へ丸める
（B12: PDF・掲示の完全週固定。明示指定時は丸めない）。
"""
import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.lib.config import (DEFAULT_DATA_DIR, PUBLIC_BASE_URL, TARGET_GA_DAILY,
                            status_display, fmt_jp_date, fmt_jp_range)
try:
    from app.lib.config import REPORT_HOSPITAL_NAME
except ImportError:
    REPORT_HOSPITAL_NAME = ""
from app.lib.calendar_preview import complete_week_end
from app.lib.metrics import build_kpi_summary, achievement_rate
from app.lib.weekly_story import build_kpi_snapshot, compute_wow_diffs, narrate_weekly_story
from app.lib.month_projection import build_month_projection_payload
from app.lib.triage import score_departments, score_wards, pick_targets, adjusted_weekly_target
from app.lib.qr import qr_svg_inline
from scripts.build_dept_reports import find_chrome, html_to_pdf


def log(msg, lv="info"):
    p = {"info": "ℹ️ ", "ok": "✅", "warn": "⚠️ ", "err": "❌"}.get(lv, "")
    print(f"  {p} [{datetime.now():%H:%M:%S}] {msg}")


# ════════════════════════════════════════
# 純関数（テキスト整形・WoW再計算）— tests/test_weekly_digest.py の対象
# ════════════════════════════════════════

def resolve_base_date(explicit_base_date, base_date):
    """B12: --base-date 未指定時は直近日曜（完全週の終端）へ丸める。

    explicit_base_date は argparse の生値（未指定なら None）。base_date は
    load_and_preprocess が解決済みの値（未指定時はデータ最終日）。明示指定時は
    ユーザーが特定の日を指定しているのでそのまま返す（丸めない）。
    """
    if explicit_base_date is not None:
        return base_date
    return complete_week_end(base_date)


def build_period_heading(week_start, week_end) -> str:
    """B12: 「対象週 8/24(月)〜8/30(日)｜比較: その前の週 8/17〜8/23」。
    「直近7日」「直近7日 vs 前週」の語は使わない（正本§3・§7裁定）。"""
    prior_start = week_start - timedelta(days=7)
    prior_end = week_end - timedelta(days=7)
    return (f"対象週 {fmt_jp_date(week_start)}〜{fmt_jp_date(week_end)}"
            f"｜比較: その前の週 {fmt_jp_range(prior_start, prior_end)}")


def relabel_diffs_for_complete_week(diffs: list) -> list:
    """B12: weekly_story.compute_wow_diffs()（本バッチの編集対象外）が生成する差分文言
    には「（直近7日）」「（直近7日累計）」という部分週前提の語が残っている。B12丸め後は
    その窓が丁度「対象週」（月〜日の完全週）と一致するため、weekly_story.py 側のロジックは
    変更せず、表示直前でラベルだけ「対象週」に置き換える（値は不変・文言のみ）。
    """
    return [d.replace("直近7日", "対象週") for d in diffs]


def _fmt_num(v) -> str:
    """None→「—」。整数値は小数なし、それ以外は小数1桁。"""
    if v is None:
        return "—"
    v = float(v)
    return f"{v:.0f}" if v.is_integer() else f"{v:.1f}"


def _fmt_diff(v) -> str:
    """符号付き整形（±0対応）。None→「—」。"""
    if v is None:
        return "—"
    v = float(v)
    nd = 0 if v.is_integer() else 1
    return f"{v:+.{nd}f}"


def build_kpi_rows(kpi_now: dict, kpi_prev: dict,
                   snap_now: dict, snap_prev: dict) -> list:
    """KPI表の5行（在院7日平均／新入院7日累計／全麻（7平日平均）／手術室稼働率／緊急入院）を
    今週・先週・差・目標・達成率で組み立てる。

    build_kpi_summary(base_date) / build_kpi_summary(base_date-7日) の戻り値と、
    weekly_story.build_kpi_snapshot の戻り値だけから組み立てる純関数（データ読込に依存しない）。
    達成率の配色は config.status_display（▲=達成／―=接近／▼=未達）に準拠。
    """
    def _row(label, now, prev, unit, target, rate):
        diff = (now - prev) if (now is not None and prev is not None) else None
        st = status_display(rate)
        diff_css = ("ok" if (diff is not None and diff > 0)
                    else ("dr" if (diff is not None and diff < 0) else "mu"))
        return {
            "label": label, "unit": unit,
            "now": now, "prev": prev, "diff": diff, "target": target, "rate": rate,
            "now_s": _fmt_num(now), "prev_s": _fmt_num(prev),
            "diff_s": _fmt_diff(diff), "target_s": _fmt_num(target),
            "rate_display": f"{st['shape']} {rate:.1f}%" if rate is not None else "—",
            "status": st, "diff_css": diff_css,
        }

    snap_now = snap_now or {}
    snap_prev = snap_prev or {}
    op, op_p = snap_now.get("operation", {}) or {}, snap_prev.get("operation", {}) or {}
    ad, ad_p = snap_now.get("admission", {}) or {}, snap_prev.get("admission", {}) or {}

    _inp_now, _inp_tgt = kpi_now.get("inpatient_avg_7d"), kpi_now.get("inpatient_target_allday")
    _inp_rate = achievement_rate(_inp_now, _inp_tgt) if _inp_now is not None else None

    return [
        _row("在院7日平均", _inp_now, kpi_prev.get("inpatient_avg_7d"),
             "人", _inp_tgt, _inp_rate),
        _row("新入院7日累計", kpi_now.get("admission_actual_7d"), kpi_prev.get("admission_actual_7d"),
             "人", kpi_now.get("admission_target_weekly"), kpi_now.get("admission_rate_7d")),
        _row("全麻（1週・営業日平均）", kpi_now.get("operation_daily_avg"), kpi_prev.get("operation_daily_avg"),
             "件/日", kpi_now.get("operation_target"), kpi_now.get("operation_rate")),
        _row("手術室稼働率", op.get("or_util_7d"), op_p.get("or_util_7d"), "%", None, None),
        _row("緊急入院", ad.get("emergency_7d"), ad_p.get("emergency_7d"), "人", None, None),
    ]


def build_poster_kpis(kpi_now: dict, kpi_rows: list, base_date) -> list:
    """B13: 壁掲示用の大きな数字3枚（在院・新入院・全麻）。

    在院・新入院はkpi_rows（在院7日平均／新入院7日累計）をそのまま使う（B12丸め後は
    真の直近7暦日＝対象週と一致・単位=人・目標/達成率は既存ロジックを流用）。
    全麻はkpi_rowsの行が件/日の営業日平均のため、掲示は§3共通規約（全麻=件）に
    合わせてoperation_7d_total（対象週の実件数）を、F3と同じ営業日期待値の割引
    （triage.adjusted_weekly_target・triage.pyは変更せず再利用のみ）で判定し直す。
    """
    by_label = {r["label"]: r for r in kpi_rows}
    tiles = []
    for label, poster_label in (("在院7日平均", "在院"), ("新入院7日累計", "新入院")):
        row = by_label.get(label)
        if row is None:
            continue
        tiles.append({
            "label": poster_label, "unit": row["unit"],
            "now_s": row["now_s"], "target_s": row["target_s"],
            "status_css": row["status"]["css"], "status_shape": row["status"]["shape"],
        })

    op_total = kpi_now.get("operation_7d_total")
    op_target = adjusted_weekly_target(TARGET_GA_DAILY * 5, base_date)
    op_rate = achievement_rate(op_total, op_target) if op_total is not None else None
    op_status = status_display(op_rate)
    tiles.append({
        "label": "全麻", "unit": "件",
        "now_s": _fmt_num(op_total), "target_s": _fmt_num(op_target),
        "status_css": op_status["css"], "status_shape": op_status["shape"],
    })
    return tiles


def _fmt_improvement_txt(imp: dict) -> str:
    groups = [("内科系", (imp or {}).get("dept_internal") or []),
              ("外科系", (imp or {}).get("dept_surgery") or []),
              ("病棟",   (imp or {}).get("ward") or [])]
    parts = []
    for label, items in groups:
        if not items:
            continue
        items_txt = "、".join(
            f"{it['name']} {it['metric_label']}{it['delta']:+d}{it['unit']}（{it['compare']}）"
            for it in items)
        parts.append(f"{label}: {items_txt}")
    return "／".join(parts) if parts else "該当なし"


def render_txt(ctx: dict) -> str:
    """院内メール／Comedix掲示板 貼付用プレーンテキスト（§6.5 体裁準拠・純関数）。"""
    ws, we, bd = ctx["week_start"], ctx["week_end"], ctx["base_date"]
    lines = [
        f"【週次ダイジェスト】{ws:%Y/%m/%d}〜{we:%m/%d}（基準日 {bd:%m/%d}）",
        "■ 今週のまとめ",
        ctx.get("story") or "（自動要約なし）",
        "■ KPI（今週 / 先週 / 目標）",
    ]
    for row in ctx["kpi_rows"]:
        extra = []
        if row["prev"] is not None:
            extra.append(f"先週 {row['prev_s']}")
        if row["target"] is not None:
            extra.append(f"目標 {row['target_s']}")
        paren = f"（{' / '.join(extra)}）" if extra else ""
        rate = f"{row['rate']:.1f}%" if row["rate"] is not None else ""
        lines.append(f"・{row['label']} {row['now_s']}{row['unit']}{paren}{rate}")

    att = ctx["attention"]
    worst_txt = "、".join(f"{it['name']}({it['primary_rate']:.0f}%)" for it in att["worst3"])
    att_line = f"■ 要注視: 病棟{att['ward_count']}・診療科{att['dept_count']}"
    if worst_txt:
        att_line += f" ─ ワースト: {worst_txt}"
    lines.append(att_line)

    cp = ctx.get("calendar_preview")
    if cp:
        cal_texts = [cp[k]["text"] for k in ("early", "week", "month") if cp.get(k)]
        if cal_texts:
            lines.append(f"■ 暦プレビュー: {'／'.join(cal_texts)}")

    lines.append(f"■ 改善: {_fmt_improvement_txt(ctx['improvement'])}")
    lines.append(f"▶ 詳細（毎日更新）: {ctx['public_base_url']}portal.html")
    return "\n".join(lines)


# ════════════════════════════════════════
# 要注視（score_departments/score_wards → pick_targets を LLMなしで使用）
# ════════════════════════════════════════

def build_attention_summary(adm, surg, targets, surg_targets, profit_monthly, base_date) -> dict:
    """要注視: 対象件数＋ワースト3。triage.build_triage_section はLLMナラティブ生成込みの
    ため使わず、score_departments/score_wards → pick_targets を直接呼び kpi_summary
    文字列だけを再利用する（§6.3）。"""
    scored = (score_departments(adm, surg, targets, surg_targets, profit_monthly, base_date)
              + score_wards(adm, targets, base_date))
    items = pick_targets(scored, adm, base_date)
    worst3 = sorted(items, key=lambda x: x["primary_rate"])[:3]
    return {
        "dept_count": sum(1 for it in items if it.get("entity_type") == "dept"),
        "ward_count": sum(1 for it in items if it.get("entity_type") == "ward"),
        "worst3": worst3,
    }


def main():
    ap = argparse.ArgumentParser(description="週次ダイジェスト自動生成（掲示A4＋メール貼付テキスト）")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--output-dir", default="output/weekly_digest")
    ap.add_argument("--base-date", default=None, help="基準日 YYYY-MM-DD")
    ap.add_argument("--no-ai", action="store_true",
                    help="今週のまとめをAI生成せず確定差分の箇条書きのみ（oMLX不要・高速）")
    args = ap.parse_args()

    from generate_html import load_and_preprocess
    log("データ読込・前処理中（load_and_preprocess）...")
    adm, surg, targets, surg_targets, profit_monthly, base_date, profit_breakdown = \
        load_and_preprocess(args.data_dir, args.base_date, no_validate=False)
    resolved_base_date = resolve_base_date(args.base_date, base_date)
    if resolved_base_date != base_date:
        log(f"--base-date 未指定のため直近日曜（{resolved_base_date:%Y-%m-%d}）へ丸めました")
    base_date = resolved_base_date
    generated_at = datetime.now()

    prior_date = base_date - timedelta(days=7)
    week_start = base_date - timedelta(days=6)
    period_heading = build_period_heading(week_start, base_date)

    log(f"KPIサマリー構築中（今週={base_date:%Y-%m-%d} / 先週={prior_date:%Y-%m-%d}）...")
    kpi_now = build_kpi_summary(adm, surg, base_date, targets, surg_targets)
    kpi_prev = build_kpi_summary(adm, surg, prior_date, targets, surg_targets)
    snap_now = build_kpi_snapshot(adm, surg, kpi_now, profit_monthly, base_date)
    snap_prev = build_kpi_snapshot(adm, surg, kpi_prev, profit_monthly, prior_date)
    diffs = relabel_diffs_for_complete_week(compute_wow_diffs(snap_now, snap_prev))
    kpi_rows = build_kpi_rows(kpi_now, kpi_prev, snap_now, snap_prev)

    story = None
    if not args.no_ai and diffs:
        log("AI要約（今週のまとめ）生成中...")
        story = narrate_weekly_story(diffs, base_date.strftime("%Y-%m-%d"),
                                     prior_date.strftime("%Y-%m-%d"))
        log(f"今週のまとめ: {'✓ ' + story if story else '— (未生成・箇条書きのみで構成)'}",
            "ok" if story else "warn")
    elif args.no_ai:
        log("今週のまとめ: --no-ai のため確定差分の箇条書きのみ")
    else:
        log("今週のまとめ: 確定差分なし（AI要約はスキップ）")

    try:
        mp = build_month_projection_payload(adm, surg, profit_monthly, None, None, base_date)
        month_projection = [mp[k] for k in ("inpatient", "admission", "operation") if mp.get(k)]
    except Exception as e:
        log(f"当月着地見込みスキップ: {e}", "warn")
        month_projection = []

    try:
        attention = build_attention_summary(adm, surg, targets, surg_targets, profit_monthly, base_date)
    except Exception as e:
        log(f"要注視スキップ: {e}", "warn")
        attention = {"dept_count": 0, "ward_count": 0, "worst3": []}

    try:
        from app.lib.html_builder import _build_improvement
        improvement = _build_improvement(adm, surg, base_date)
    except Exception as e:
        log(f"改善トピックスキップ: {e}", "warn")
        improvement = {"dept_internal": [], "dept_surgery": [], "ward": []}

    try:
        from app.lib.calendar_preview import build_calendar_preview
        calendar_preview = build_calendar_preview(base_date)
    except Exception as e:
        log(f"暦プレビュースキップ: {e}", "warn")
        calendar_preview = None

    qr_svg = qr_svg_inline(f"{PUBLIC_BASE_URL}portal.html", size_mm=18)

    poster_kpis = build_poster_kpis(kpi_now, kpi_rows, base_date)
    poster_story = story or (diffs[0] if diffs else
                              "今週も引き続き、日々の目標達成をよろしくお願いします。")

    date_str = base_date.strftime("%Y-%m-%d")
    html_ctx = {
        "hospital_name": REPORT_HOSPITAL_NAME,
        "base_date": date_str,
        "week_start": week_start.strftime("%Y-%m-%d"),
        "week_end": date_str,
        "period_heading": period_heading,
        "generated_at": generated_at.strftime("%Y/%m/%d %H:%M"),
        "story": story,
        "diffs": diffs,
        "kpi_rows": kpi_rows,
        "month_projection": month_projection,
        "attention": attention,
        "improvement": improvement,
        "calendar_preview": calendar_preview,
        "qr_svg": qr_svg,
        "public_base_url": PUBLIC_BASE_URL,
    }
    txt_ctx = dict(html_ctx)
    txt_ctx["base_date"] = base_date
    txt_ctx["week_start"] = week_start
    txt_ctx["week_end"] = base_date

    poster_ctx = dict(html_ctx)
    poster_ctx["poster_kpis"] = poster_kpis
    poster_ctx["story"] = poster_story

    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(str(ROOT / "app" / "templates")), autoescape=False)
    tmpl = env.get_template("weekly_digest.html")
    html = tmpl.render(**html_ctx)
    poster_tmpl = env.get_template("weekly_poster.html")
    poster_html = poster_tmpl.render(**poster_ctx)

    out_root = Path(args.output_dir) / date_str
    out_root.mkdir(parents=True, exist_ok=True)
    base_name = f"週次ダイジェスト_{date_str}"
    poster_name = f"週次掲示_{date_str}"

    html_path = out_root / f"{base_name}.html"
    html_path.write_text(html, encoding="utf-8")
    log(f"{html_path.name}", "ok")

    txt_path = out_root / f"{base_name}.txt"
    txt_path.write_text(render_txt(txt_ctx), encoding="utf-8")
    log(f"{txt_path.name}", "ok")

    poster_path = out_root / f"{poster_name}.html"
    poster_path.write_text(poster_html, encoding="utf-8")
    log(f"{poster_path.name}", "ok")

    chrome = find_chrome()
    if chrome:
        pdf_path = out_root / f"{base_name}.pdf"
        if html_to_pdf(chrome, html_path, pdf_path):
            log(f"{pdf_path.name}", "ok")
        else:
            log("PDF生成に失敗しました", "warn")

        poster_pdf_path = out_root / f"{poster_name}.pdf"
        if html_to_pdf(chrome, poster_path, poster_pdf_path):
            log(f"{poster_pdf_path.name}", "ok")
        else:
            log("掲示PDF生成に失敗しました", "warn")
    else:
        log("Chrome/Chromium が見つからないため PDF はスキップします", "warn")

    print(f"\n{'='*52}")
    print(f"  週次ダイジェスト生成完了 — {generated_at.strftime('%Y/%m/%d %H:%M')}")
    print(f"  対象週: {week_start:%Y-%m-%d} 〜 {date_str}")
    print(f"  出力先: {out_root.resolve()}")
    print(f"{'='*52}\n")


if __name__ == "__main__":
    main()
