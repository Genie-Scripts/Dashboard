#!/usr/bin/env python3
"""report_comment_diversity.py — 「この期間の一手」単調さの定量計測（バックログ3-1）。

部門別レポートの move（body/action）を全部門ぶん生成して、
  (i)   一字一句同一のコメントグループ（最重要: 「6科同一文」の検知）
  (ii)  文字3-gram Jaccard の平均ペア類似度・高類似ペア率（body）
  (iii) fallback率（axis×topic 別・src=ai/tpl）
  (iv)  AI棄却理由の内訳（ok/parse/digit/banned/length/error）
を1画面で出す。改善施策の前後で同じ数字を取り、効果を数値で確認する（回帰ガード）。

  python scripts/report_comment_diversity.py                # AI ON（oMLX必要・本番相当）
  python scripts/report_comment_diversity.py --no-ai        # 定型文のみ（fallback語彙の計測）
  python scripts/report_comment_diversity.py --json out.json  # スナップショット保存（前後比較用）
  python scripts/report_comment_diversity.py --compare A.json B.json
      # 生成せず、2つのスナップショットを同一ユニットで突き合わせ（月次安定性の運用・3-3）。
      # 同じ基準日同士なら再現性チェック（seed導入後は完全一致が期待値）、
      # 基準日違いなら「事実が変わった科だけ文が変わっているか」を見る。

病院全体サマリも1ユニットとして含める（粗利の当月見込み推計は計測目的では省略し、
確報ベースの事実で生成する＝本番と粗利factだけ僅差になり得る）。
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.lib.config import DEFAULT_DATA_DIR


def _ngrams(s: str, n: int = 3) -> set:
    s = re.sub(r"\s", "", s or "")
    return {s[i:i + n] for i in range(len(s) - n + 1)} if len(s) >= n else ({s} if s else set())


def _jaccard(a: set, b: set) -> float:
    u = a | b
    return len(a & b) / len(u) if u else 0.0


def _dup_groups(records: list, key: str) -> list:
    """一字一句同一テキストのグループ（サイズ降順）。"""
    groups = defaultdict(list)
    for r in records:
        t = (r.get(key) or "").strip()
        if t:
            groups[t].append(f"{r['axis']}:{r['unit']}")
    return sorted(([t, us] for t, us in groups.items() if len(us) >= 2),
                  key=lambda g: len(g[1]), reverse=True)


def _pairwise(records: list, key: str) -> dict:
    """文字3-gram Jaccard の平均・高類似(≥0.6)ペア率。"""
    grams = [_ngrams(r.get(key) or "") for r in records]
    sims = [_jaccard(a, b) for a, b in combinations(grams, 2) if a and b]
    if not sims:
        return {"pairs": 0, "mean": None, "hi_share": None}
    hi = sum(1 for s in sims if s >= 0.6)
    return {"pairs": len(sims), "mean": round(sum(sims) / len(sims), 3),
            "hi_share": round(hi / len(sims), 3)}


def compare_snapshots(path_a: str, path_b: str) -> None:
    """2スナップショットを (axis, unit) で突き合わせ、同一ユニットの文の変化を報告する。"""
    a = json.loads(Path(path_a).read_text(encoding="utf-8"))
    b = json.loads(Path(path_b).read_text(encoding="utf-8"))
    ra = {(r["axis"], r["unit"]): r for r in a["records"]}
    rb = {(r["axis"], r["unit"]): r for r in b["records"]}
    common = sorted(set(ra) & set(rb))
    changed, sims = [], []
    for k in common:
        x, y = ra[k], rb[k]
        same = (x.get("body") == y.get("body") and x.get("action") == y.get("action"))
        sims.append(_jaccard(_ngrams(x.get("body") or ""), _ngrams(y.get("body") or "")))
        if not same:
            note = []
            if x.get("topic") != y.get("topic"):
                note.append(f"topic {x.get('topic')}→{y.get('topic')}")
            if x.get("src") != y.get("src"):
                note.append(f"src {x.get('src')}→{y.get('src')}")
            changed.append((k, ", ".join(note)))
    W = 72
    print("=" * W)
    print(f"  スナップショット比較  A={a['base_date']}({Path(path_a).name})"
          f"  B={b['base_date']}({Path(path_b).name})")
    print("=" * W)
    print(f"■ 共通ユニット {len(common)} 件中、文（body/action）が変化: {len(changed)} 件"
          f"（{len(changed) / len(common) * 100:.0f}%）" if common else "共通ユニットなし")
    if sims:
        print(f"■ 同一ユニットの body 類似度平均: {sum(sims) / len(sims):.3f}"
              f"（同基準日の再現性チェックなら 1.0 が期待値）")
    for (ax, unit), note in changed[:15]:
        print(f"    {ax}:{unit}" + (f"（{note}）" if note else ""))
    print("=" * W)


def main():
    p = argparse.ArgumentParser(description="「この期間の一手」単調さの定量計測")
    p.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    p.add_argument("--base-date", default=None, help="基準日 YYYY-MM-DD")
    p.add_argument("--axes", default="dept,ward")
    p.add_argument("--no-ai", action="store_true", help="定型文のみ（oMLX不要）")
    p.add_argument("--json", default=None, help="スナップショットJSONの保存先")
    p.add_argument("--compare", nargs=2, metavar=("A.json", "B.json"),
                   help="生成せず2スナップショットを比較（月次安定性・再現性チェック）")
    p.add_argument("--quiet", "-q", action="store_true")
    args = p.parse_args()

    if args.compare:
        compare_snapshots(*args.compare)
        return

    axes = tuple(a.strip() for a in args.axes.split(",") if a.strip() in ("dept", "ward"))

    from generate_html import load_and_preprocess
    from app.lib import ai_narrative
    from app.lib.dept_report import (build_dept_report_contexts,
                                     build_hospital_overview_context)

    adm, surg, targets, surg_targets, profit_monthly, base_date, profit_breakdown = \
        load_and_preprocess(args.data_dir, args.base_date, no_validate=False)
    generated_at = datetime.now()

    ai_narrative.reset_reject_stats()
    with_ai = not args.no_ai
    print(f"move 生成中… axes={axes} AI={'ON' if with_ai else 'OFF(定型文のみ)'} "
          f"基準日={base_date:%Y-%m-%d}")
    contexts = build_dept_report_contexts(
        adm, surg, targets, surg_targets, profit_monthly, base_date, generated_at,
        with_ai=with_ai, axes=axes, quiet=args.quiet,
        profit_breakdown=profit_breakdown)
    hosp = build_hospital_overview_context(
        adm, surg, targets, surg_targets, profit_monthly, base_date, generated_at,
        profit_breakdown=profit_breakdown, with_ai=with_ai, quiet=args.quiet)
    contexts = contexts + [hosp]

    records = [{"axis": c["axis"], "type_key": c["type_key"], "unit": c["unit"],
                "topic": (c.get("move") or {}).get("topic"),
                "src": (c.get("move") or {}).get("src"),
                "body": (c.get("move") or {}).get("body"),
                "action": (c.get("move") or {}).get("action")}
               for c in contexts if c.get("move")]

    # ── (iii) fallback率（axis×topic）──
    by_at = defaultdict(lambda: {"n": 0, "ai": 0})
    for r in records:
        k = (r["axis"], r["topic"] or "?")
        by_at[k]["n"] += 1
        by_at[k]["ai"] += (r["src"] == "ai")
    n_all = len(records)
    n_ai = sum(1 for r in records if r["src"] == "ai")

    dup_body = _dup_groups(records, "body")
    dup_action = _dup_groups(records, "action")
    sim_all = _pairwise(records, "body")
    sim_axis = {ax: _pairwise([r for r in records if r["axis"] == ax], "body")
                for ax in ("dept", "ward")}

    W = 72
    print("\n" + "=" * W)
    print(f"  「この期間の一手」多様性レポート  基準日 {base_date:%Y-%m-%d}"
          f"  AI={'ON' if with_ai else 'OFF'}")
    print("=" * W)

    print(f"\n■ 生成数: {n_all} 件（AI採択 {n_ai} / 定型文 {n_all - n_ai}"
          f"　AI率 {n_ai / n_all * 100:.0f}%）")
    print("  axis×topic 別（AI採択/件数）:")
    for (ax, tp), v in sorted(by_at.items()):
        print(f"    {ax:8s} {tp:20s} {v['ai']}/{v['n']}")

    print(f"\n■ AI棄却理由の内訳: {dict(ai_narrative.REJECT_STATS) or '（AI OFF）'}")

    print(f"\n■ 一字一句同一の body: {len(dup_body)} グループ"
          f"（最大 {len(dup_body[0][1]) if dup_body else 0} 部門）")
    for t, us in dup_body[:5]:
        print(f"    ×{len(us)}: {t[:46]}…")
        print(f"        → {', '.join(us)}")
    print(f"■ 一字一句同一の action: {len(dup_action)} グループ"
          f"（最大 {len(dup_action[0][1]) if dup_action else 0} 部門）")
    for t, us in dup_action[:3]:
        print(f"    ×{len(us)}: {t[:46]}…")
        print(f"        → {', '.join(us)}")

    print(f"\n■ body 3-gram Jaccard 類似度:")
    print(f"    全体   平均 {sim_all['mean']}　高類似(≥0.6)ペア率 {sim_all['hi_share']}"
          f"　({sim_all['pairs']} ペア)")
    for ax, s in sim_axis.items():
        if s["pairs"]:
            print(f"    {ax:6s} 平均 {s['mean']}　高類似ペア率 {s['hi_share']}　({s['pairs']} ペア)")
    print("=" * W)

    if args.json:
        snap = {"base_date": f"{base_date:%Y-%m-%d}", "generated_at": generated_at.isoformat(),
                "with_ai": with_ai, "n": n_all, "n_ai": n_ai,
                "reject_stats": dict(ai_narrative.REJECT_STATS),
                "dup_body_groups": [[t, us] for t, us in dup_body],
                "dup_action_groups": [[t, us] for t, us in dup_action],
                "similarity": {"all": sim_all, **{f"axis_{k}": v for k, v in sim_axis.items()}},
                "records": records}
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"スナップショット保存: {out}")


if __name__ == "__main__":
    main()
