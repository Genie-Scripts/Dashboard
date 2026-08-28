#!/usr/bin/env python3
"""report_edit_telemetry.py — AIコメントへの人手添削の実態を可視化する（P5-f・オフライン集計・読み取り専用）。

dept_reports/_state/edits_*.jsonl（app.lib.report_feedback.load_edits）から基準日ごとに
  (i)   対象ユニット数（ai/tpl レコードのあるユニット）・添削ユニット数（ai→manual ペアあり）・添削率
  (ii)  平均編集距離（body / action 別。文字3-gram Jaccard 類似度の 1-sim。
        同一 base_date 内に AI 原文の記録が無いペア＝had_ai=False は編集距離の計算対象外）
  (iii) topic 別・axis（dept/ward）別の内訳
を出す。末尾に添削率の推移（基準日昇順）と「直近N基準日 vs それ以前」の比較を要約する。
ペアリングは report_feedback.pair_corrections をそのまま使う（自作しない）。

これは「学習ループ（P5）が効いているか」の温度計。添削率・編集距離が下がっていれば
few-shot/ノート化などの学習ループが効いている、下がらなければ効いていないと読む。

  python scripts/report_edit_telemetry.py
  python scripts/report_edit_telemetry.py --output-dir dept_reports --csv out.csv
  python scripts/report_edit_telemetry.py --recent-n 4    # トレンド比較の直近基準日数（既定4）
"""
import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.report_feedback import load_edits, pair_corrections
# 編集距離＝1-類似度。私的関数だが自前で再実装せず fewshot.py の実装をそのまま再利用する。
from app.lib.fewshot import _trigram_jaccard


def _edit_distance(a: Optional[str], b: Optional[str]) -> float:
    return 1.0 - _trigram_jaccard(a, b)


def _dist_stats(pairs: list) -> dict:
    """had_ai=True のペアのみを対象に body/action の平均編集距離を出す
    （AI原文が同一run内に記録されていないペアは「何から添削したか」が不明なため対象外）。"""
    body = [_edit_distance(p["ai_body"], p["human_body"]) for p in pairs
            if p.get("had_ai") and p.get("ai_body") is not None and p.get("human_body") is not None]
    action = [_edit_distance(p["ai_action"], p["human_action"]) for p in pairs
              if p.get("had_ai") and p.get("ai_action") is not None and p.get("human_action") is not None]
    return {
        "body_mean": round(sum(body) / len(body), 3) if body else None, "body_n": len(body),
        "action_mean": round(sum(action) / len(action), 3) if action else None, "action_n": len(action),
    }


def compute_by_date(records: list, pairs: list) -> dict:
    """基準日ごとに「対象ユニット集合」と「添削ペア一覧」を集める（全体・axis別・topic別）。"""
    by_date = defaultdict(lambda: {
        "target": set(), "target_by_axis": defaultdict(set), "target_by_topic": defaultdict(set),
        "pairs": [], "pairs_by_axis": defaultdict(list), "pairs_by_topic": defaultdict(list),
    })
    for r in records:
        if r.get("src") not in ("ai", "tpl"):
            continue
        axis, unit, topic = r.get("axis"), r.get("unit"), r.get("topic")
        if not (axis and unit):
            continue
        d = by_date[r.get("base_date")]
        key = (axis, unit)
        d["target"].add(key)
        d["target_by_axis"][axis].add(key)
        if topic:
            d["target_by_topic"][topic].add(key)
    for p in pairs:
        d = by_date[p.get("date")]
        d["pairs"].append(p)
        if p.get("axis"):
            d["pairs_by_axis"][p["axis"]].append(p)
        if p.get("topic"):
            d["pairs_by_topic"][p["topic"]].append(p)
    return by_date


def summarize_date(d: dict) -> dict:
    """1基準日ぶんの対象/添削集合から、全体・axis(dept/ward)別・topic別の集計を作る。"""
    target_n, corrected_n = len(d["target"]), len(d["pairs"])
    rate = round(corrected_n / target_n * 100, 1) if target_n else None

    by_axis = {}
    for axis in ("dept", "ward"):
        t = len(d["target_by_axis"].get(axis, set()))
        c = len(d["pairs_by_axis"].get(axis, []))
        by_axis[axis] = {"target": t, "corrected": c,
                         "rate": round(c / t * 100, 1) if t else None,
                         **_dist_stats(d["pairs_by_axis"].get(axis, []))}

    by_topic = {}
    for topic in sorted(set(d["target_by_topic"]) | set(d["pairs_by_topic"])):
        t = len(d["target_by_topic"].get(topic, set()))
        c = len(d["pairs_by_topic"].get(topic, []))
        by_topic[topic] = {"target": t, "corrected": c,
                           "rate": round(c / t * 100, 1) if t else None,
                           **_dist_stats(d["pairs_by_topic"].get(topic, []))}

    return {"target": target_n, "corrected": corrected_n, "rate": rate,
            **_dist_stats(d["pairs"]), "by_axis": by_axis, "by_topic": by_topic}


def _pool(by_date: dict, summaries: dict, dates_subset: list) -> Optional[dict]:
    """複数基準日をまとめて（日別平均でなく全体プールで）添削率・編集距離を出す。"""
    if not dates_subset:
        return None
    target = sum(summaries[dt]["target"] for dt in dates_subset)
    corrected = sum(summaries[dt]["corrected"] for dt in dates_subset)
    pairs = [p for dt in dates_subset for p in by_date[dt]["pairs"]]
    rate = round(corrected / target * 100, 1) if target else None
    return {"target": target, "corrected": corrected, "rate": rate, **_dist_stats(pairs)}


def analyze(state_dir, recent_n: int = 4) -> dict:
    """edits_*.jsonl を読み、基準日別サマリー＋直近N基準日 vs それ以前のトレンドを返す。"""
    records = load_edits(state_dir)
    pairs = pair_corrections(records)
    by_date = compute_by_date(records, pairs)
    dates = sorted(by_date.keys())
    summaries = {dt: summarize_date(by_date[dt]) for dt in dates}

    if len(dates) > recent_n:
        recent_dates, prior_dates = dates[-recent_n:], dates[:-recent_n]
    else:
        recent_dates, prior_dates = dates, []

    return {
        "n_records": len(records), "dates": dates, "by_date": by_date, "summaries": summaries,
        "recent_dates": recent_dates, "prior_dates": prior_dates,
        "recent_pool": _pool(by_date, summaries, recent_dates),
        "prior_pool": _pool(by_date, summaries, prior_dates) if prior_dates else None,
    }


# ════════════════════════════════════════════════════════════
# 出力
# ════════════════════════════════════════════════════════════
def _fmt_rate(rate) -> str:
    return f"{rate:.1f}%" if rate is not None else "N/A"


def _fmt_dist(mean, n) -> str:
    return f"{mean}(n={n})" if mean is not None else "—"


def _fmt_pool(pool: Optional[dict]) -> str:
    if pool is None:
        return "（データ不足）"
    return (f"添削率 {_fmt_rate(pool['rate'])}（{pool['corrected']}/{pool['target']}）"
           f"　距離[body {_fmt_dist(pool['body_mean'], pool['body_n'])}"
           f" / action {_fmt_dist(pool['action_mean'], pool['action_n'])}]")


def print_report(result: dict, recent_n: int) -> None:
    W = 72
    dates, summaries, by_date = result["dates"], result["summaries"], result["by_date"]
    print("=" * W)
    print(f"  AIコメント添削テレメトリ（学習ループの温度計・全 {len(dates)} 基準日）")
    print("=" * W)

    for dt in dates:
        s = summaries[dt]
        print(f"\n{dt}  対象{s['target']:3d}  添削{s['corrected']:3d}件 ({_fmt_rate(s['rate'])})"
              f"  距離[body {_fmt_dist(s['body_mean'], s['body_n'])}"
              f" / action {_fmt_dist(s['action_mean'], s['action_n'])}]")
        axis_bits = [f"{axis}:{a['corrected']}/{a['target']}({_fmt_rate(a['rate'])})"
                    for axis, a in s["by_axis"].items() if a["target"] or a["corrected"]]
        if axis_bits:
            print("    axis   " + "  ".join(axis_bits))
        topic_bits = [f"{topic}:{t['corrected']}/{t['target']}({_fmt_rate(t['rate'])}"
                     f",body={t['body_mean'] if t['body_mean'] is not None else '—'})"
                     for topic, t in s["by_topic"].items() if t["target"] or t["corrected"]]
        if topic_bits:
            print("    topic  " + "  ".join(topic_bits))

    print("\n" + "=" * W)
    print("  トレンド要約")
    print("=" * W)
    print("\n■ 添削率の推移（基準日昇順）:")
    for dt in dates:
        s = summaries[dt]
        print(f"    {dt}: {_fmt_rate(s['rate'])}（{s['corrected']}/{s['target']}）")

    print(f"\n■ 直近{recent_n}基準日 vs それ以前:")
    print(f"    直近{len(result['recent_dates'])}基準日: {_fmt_pool(result['recent_pool'])}")
    print(f"    それ以前{len(result['prior_dates'])}基準日: {_fmt_pool(result['prior_pool'])}")
    print("=" * W)


def write_csv(path, result: dict, recent_n: int) -> None:
    dates, summaries = result["dates"], result["summaries"]
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["base_date", "scope", "key", "target_units", "corrected_units",
                    "correction_rate_pct", "avg_dist_body", "n_dist_body",
                    "avg_dist_action", "n_dist_action"])
        for dt in dates:
            s = summaries[dt]
            w.writerow([dt, "overall", "", s["target"], s["corrected"], s["rate"],
                        s["body_mean"], s["body_n"], s["action_mean"], s["action_n"]])
            for axis, a in s["by_axis"].items():
                w.writerow([dt, "axis", axis, a["target"], a["corrected"], a["rate"],
                            a["body_mean"], a["body_n"], a["action_mean"], a["action_n"]])
            for topic, t in s["by_topic"].items():
                w.writerow([dt, "topic", topic, t["target"], t["corrected"], t["rate"],
                            t["body_mean"], t["body_n"], t["action_mean"], t["action_n"]])
        for label, pool in (("recent", result["recent_pool"]), ("prior", result["prior_pool"])):
            if pool is None:
                continue
            w.writerow([f"{label}_{recent_n}" if label == "recent" else label, "trend_bucket", label,
                        pool["target"], pool["corrected"], pool["rate"],
                        pool["body_mean"], pool["body_n"], pool["action_mean"], pool["action_n"]])


def main() -> int:
    p = argparse.ArgumentParser(description="AIコメント添削の実態可視化（添削率・編集距離・トレンド）")
    p.add_argument("--output-dir", default="dept_reports",
                   help="部門レポートの出力ディレクトリ（_state/ を含む親、既定: dept_reports）")
    p.add_argument("--csv", default=None, help="集計結果のCSV出力先（同内容をlong形式で出力）")
    p.add_argument("--recent-n", type=int, default=4, help="トレンド比較の直近基準日数（既定4）")
    args = p.parse_args()

    state_dir = Path(args.output_dir) / "_state"
    result = analyze(state_dir, recent_n=args.recent_n)
    if not result["dates"]:
        print(f"添削信号が見つかりません（{state_dir} に edits_*.jsonl がありません）")
        return 0

    print_report(result, args.recent_n)

    if args.csv:
        write_csv(args.csv, result, args.recent_n)
        print(f"\nCSV出力: {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
