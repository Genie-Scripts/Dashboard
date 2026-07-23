#!/usr/bin/env python3
"""mine_report_edits.py — 部門レポート「一手」の人手添削を突き合わせる（P1・薄い CLI）。

report_feedback.py が貯めた dept_reports/_state/edits_*.jsonl を読み、ai→manual 遷移＝人の添削ペア
（AI原文／人の最終文／変更フィールド）を復元して digest を出力する。ロジックは app.lib.report_feedback
に置き（テスト可能）、本スクリプトは読み込み→digest 生成→書き出しのラッパー。

  python scripts/mine_report_edits.py [--output-dir dept_reports] [--md PATH]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.report_feedback import load_edits, pair_corrections, build_digest_md


def main() -> int:
    ap = argparse.ArgumentParser(description="部門レポート 一手の人手添削 突き合わせ（P1）")
    ap.add_argument("--output-dir", default="dept_reports",
                    help="部門レポートの出力ディレクトリ（_state/ を含む親）")
    ap.add_argument("--md", default=None,
                    help="digest markdown の出力先（既定: <output-dir>/_state/edits_digest.md）")
    args = ap.parse_args()

    state_dir = Path(args.output_dir) / "_state"
    recs = load_edits(state_dir)
    pairs = pair_corrections(recs)
    md = build_digest_md(pairs)

    md_path = Path(args.md) if args.md else (state_dir / "edits_digest.md")
    try:
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(md, encoding="utf-8")
    except OSError as e:
        print(f"digest の書き出しに失敗: {e}", file=sys.stderr)

    action_edits = sum(1 for p in pairs if "action" in p["changed"])
    print(f"レコード {len(recs)} 件 → 添削ペア {len(pairs)} 件"
          f"（うち action 添削 {action_edits} 件）")
    print(f"digest: {md_path}")
    if not pairs:
        print("（まだ添削信号なし。レビューUIで override を保存→ビルドで蓄積されます）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
