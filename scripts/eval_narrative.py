#!/usr/bin/env python3
"""eval_narrative.py — C1 ナラティブ品質測定ハーネス（薄い CLI）。

dept_reports/_state/edits_*.jsonl（人手添削台帳）を読み、app.lib.narrative_eval で
採点・集計してテキストサマリを stdout に出し、スナップショット(JSON)とmarkdownを書く。
ロジックは app.lib.narrative_eval に置く（テスト可能）。LLM は一切呼ばない。

  python scripts/eval_narrative.py [--output-dir dept_reports] [--since D] [--until D]
      [--json PATH] [--md PATH] [--quiet] [--alerts-recorded PATH.jsonl] [--compare A.json B.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date as _date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.report_feedback import load_edits
from app.lib.narrative_eval import build_eval_report, build_eval_md, compare_reports


def _load_jsonl(path: Path) -> list:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="C1 ナラティブ品質測定ハーネス")
    ap.add_argument("--output-dir", default="dept_reports",
                    help="部門レポートの出力ディレクトリ（_state/ を含む親）")
    ap.add_argument("--since", default=None, help="この日付以降を対象（YYYY-MM-DD・含む）")
    ap.add_argument("--until", default=None, help="この日付までを対象（YYYY-MM-DD・含む）")
    ap.add_argument("--json", default=None, help="スナップショットJSONの出力先（既定: 自動）")
    ap.add_argument("--md", default=None, help="markdownサマリの出力先（既定: 自動・上書き）")
    ap.add_argument("--quiet", action="store_true", help="stdoutへのテキストサマリ出力を抑止")
    ap.add_argument("--alerts-recorded", default=None,
                    help='{"alert":{...,"facts":[...]},"narrative":{"headline","body","action"}}'
                        ' 形式のJSONL。score_alert_narrative へ通してレポートに含める')
    ap.add_argument("--compare", nargs=2, default=None, metavar=("A_JSON", "B_JSON"),
                    help="2つのスナップショットJSONを比較して stdout に出すだけ（他の処理はしない）")
    args = ap.parse_args()

    if args.compare:
        a = json.loads(Path(args.compare[0]).read_text(encoding="utf-8"))
        b = json.loads(Path(args.compare[1]).read_text(encoding="utf-8"))
        print(compare_reports(a, b))
        return 0

    state_dir = Path(args.output_dir) / "_state"
    records = load_edits(state_dir)
    if args.since:
        records = [r for r in records if (r.get("base_date") or "") >= args.since]
    if args.until:
        records = [r for r in records if (r.get("base_date") or "") <= args.until]

    alert_rows = None
    if args.alerts_recorded:
        alert_rows = _load_jsonl(Path(args.alerts_recorded))

    report = build_eval_report(records, alert_rows=alert_rows)
    md = build_eval_md(report)

    if not args.quiet:
        print(md)

    eval_dir = state_dir / "eval"
    json_path = (Path(args.json) if args.json
                else eval_dir / f"narrative_eval_{_date.today().isoformat()}.json")
    md_path = Path(args.md) if args.md else (eval_dir / "narrative_eval.md")
    try:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(md, encoding="utf-8")
    except OSError as e:
        print(f"書き出しに失敗: {e}", file=sys.stderr)

    out = sys.stderr if args.quiet else sys.stdout
    print(f"レコード {len(records)} 件 → JSON: {json_path} / MD: {md_path}", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
