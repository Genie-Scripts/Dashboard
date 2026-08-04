"""report_feedback — 部門レポート「一手」の人手添削(override)を学習信号として捕捉/突き合わせる。

overrides.md は単一の現行ファイルで保存のたびに上書き＝人の添削という品質信号を毎回失っていた。
本モジュールは:
  - P0 capture_edits(): ビルド時に各ユニットの一手(move)を _state/edits_<date>.jsonl へ
    「状態が変わったときだけ」追記する(append-only・非破壊・fail-soft)。
  - P1 load_edits()/pair_corrections()/build_digest_md(): 貯めた edits から
    ai→manual 遷移＝人の添削ペア(AI原文/人の最終文/変更フィールド)を復元して digest 化する。

override 適用ユニットも AI 生成は走る(レビューUIのAI文/修正文トグル用に move.ai_body/ai_action へ
併載)が、capture_edits は override 適用後の最終 body/action しか記録しないため、AI原文(src=ai の
record)と人の最終文(src=manual の record)は従来どおり**別 run に跨って**記録され、
pair_corrections が (base_date, axis, unit) で対応づける。

置き場: dept_reports/_state/（.gitignore の dept_reports/ 配下＝公開リポに載らない）。
再利用の原則: **スタイル・action(打ち手)は蒸留してよいが、body の事実は verbatim 流用しない**（月依存の陳腐化・幻覚防止）。
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _edits_path(state_dir, base_date_str: str) -> Path:
    return Path(state_dir) / f"edits_{base_date_str}.jsonl"


def _iter_jsonl(path: Path):
    """jsonl を1レコードずつ（壊れ行はスキップ）。"""
    if not path.exists():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


# ════════════════════════════════════════════════════════════
# P0: 捕捉
# ════════════════════════════════════════════════════════════
def capture_edits(state_dir, base_date, contexts, hosp_ctx=None) -> Optional[Path]:
    """各ユニットの一手(move)を状態遷移ログとして edits_<date>.jsonl へ追記する。

    記録: {ts, base_date, axis, unit, src, topic, body, action, facts}。
    src=ai(AI採択)/manual(人手override)/tpl(定型文)。(src,body,action) が同一 (axis,unit) の直近と
    一致すれば追記しない(dedup)＝変化点だけの状態遷移ログになる。追記があれば path、無ければ None を返す。
    全て fail-soft: 例外は握って None（生成本体を壊さない）。"""
    try:
        state_dir = Path(state_dir)
        base_date_str = (base_date.strftime("%Y-%m-%d")
                         if hasattr(base_date, "strftime") else str(base_date))
        path = _edits_path(state_dir, base_date_str)
        # 既存の (axis,unit) ごと直近状態を読む（dedup 用）
        last = {}
        for r in _iter_jsonl(path):
            last[(r.get("axis"), r.get("unit"))] = (r.get("src"), r.get("body"), r.get("action"))

        ts = datetime.now().isoformat(timespec="seconds")
        rows = ([hosp_ctx] if hosp_ctx else []) + list(contexts or [])
        out_lines = []
        for c in rows:
            if not isinstance(c, dict):
                continue
            axis, unit = c.get("axis"), c.get("unit")
            move = c.get("move") or {}
            if not (axis and unit and move):
                continue
            state = (move.get("src"), move.get("body"), move.get("action"))
            if last.get((axis, unit)) == state:
                continue                      # 変化なし＝追記しない
            out_lines.append(json.dumps({
                "ts": ts, "base_date": base_date_str, "axis": axis, "unit": unit,
                "src": move.get("src"), "topic": move.get("topic"),
                "body": move.get("body"), "action": move.get("action"),
                "facts": c.get("_state") or {},
            }, ensure_ascii=False))
            last[(axis, unit)] = state

        if not out_lines:
            return None
        state_dir.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write("\n".join(out_lines) + "\n")
        return path
    except Exception as e:  # noqa: BLE001
        logger.warning(f"添削フィードバック捕捉に失敗（無視して続行）: {e}")
        return None


# ════════════════════════════════════════════════════════════
# P1: 突き合わせ
# ════════════════════════════════════════════════════════════
def load_edits(state_dir) -> list[dict]:
    """_state/edits_*.jsonl を全て読み、記録順を保ったレコード列を返す。"""
    recs: list[dict] = []
    p = Path(state_dir)
    if not p.is_dir():
        return recs
    for f in sorted(p.glob("edits_*.jsonl")):
        recs.extend(_iter_jsonl(f))
    return recs


def pair_corrections(records) -> list[dict]:
    """(base_date, axis, unit) ごとに「最初の AI/定型状態 → 最後の manual 状態」を添削ペアにする。

    manual が無いユニット（AI 文をそのまま採用）はペアにしない＝信号なし。
    返り: [{date, axis, unit, topic, changed:[body|action], ai_*, human_*, had_ai}]。"""
    groups: dict = defaultdict(list)
    for r in records:
        groups[(r.get("base_date"), r.get("axis"), r.get("unit"))].append(r)

    pairs = []
    for (date, axis, unit), rs in groups.items():
        before = next((r for r in rs if r.get("src") in ("ai", "tpl")), None)
        manual = None
        for r in rs:                          # 最後の manual を最終稿とする
            if r.get("src") == "manual":
                manual = r
        if manual is None:
            continue
        b = before or {}
        changed = []
        if (b.get("body") or "") != (manual.get("body") or ""):
            changed.append("body")
        if (b.get("action") or "") != (manual.get("action") or ""):
            changed.append("action")
        pairs.append({
            "date": date, "axis": axis, "unit": unit, "topic": manual.get("topic"),
            "changed": changed, "had_ai": before is not None,
            "ai_body": b.get("body"), "ai_action": b.get("action"),
            "human_body": manual.get("body"), "human_action": manual.get("action"),
        })
    pairs.sort(key=lambda p: (p["date"] or "", p["axis"] or "", p["unit"] or ""))
    return pairs


def build_digest_md(pairs: list[dict]) -> str:
    """添削ペアを目視用 markdown digest に。action 添削は末尾に levers 候補として集約する。"""
    if not pairs:
        return ("# 部門レポート 添削 digest\n\n"
                "まだ添削信号がありません（レビューUIで override を保存し、ビルドすると蓄積されます）。\n")

    n = len(pairs)
    by_axis: dict = defaultdict(int)
    body_edits = sum(1 for p in pairs if "body" in p["changed"])
    action_edits = sum(1 for p in pairs if "action" in p["changed"])
    for p in pairs:
        by_axis[p["axis"]] += 1

    lines = ["# 部門レポート 添削 digest（人手 override の突き合わせ・P1）", ""]
    lines.append(f"- 添削ペア: **{n}件**（軸別: "
                 + " / ".join(f"{a}={c}" for a, c in sorted(by_axis.items())) + "）")
    lines.append(f"- 変更フィールド: body={body_edits} / action={action_edits}")
    lines.append("- ⚠ 再利用は**スタイル・action の蒸留のみ**。body の事実は verbatim 流用しない（陳腐化・幻覚防止）。")
    lines.append("")

    lines.append("## 添削の詳細（AI原文 → 人の最終文）")
    for p in pairs:
        tag = "＋".join(p["changed"]) or "（差分なし／manualのみ）"
        ai_flag = "" if p["had_ai"] else "  ※同一dateにAI原文の記録なし（別runで生成）"
        lines.append(f"\n### [{p['axis']}:{p['unit']}] {p['date']}（変更: {tag}・topic={p['topic']}）{ai_flag}")
        if "body" in p["changed"]:
            lines.append(f"- body  AI : {p['ai_body']}")
            lines.append(f"- body  人 : {p['human_body']}")
        if "action" in p["changed"]:
            lines.append(f"- action AI: {p['ai_action']}")
            lines.append(f"- action 人: {p['human_action']}")

    action_pairs = [p for p in pairs if "action" in p["changed"] and p["human_action"]]
    if action_pairs:
        lines.append("\n## action 添削 → levers 候補（P2 の入口）")
        lines.append("人が直した打ち手。診療科群の `evaluation_rules.yaml: levers:` へ一般化して昇格する候補。")
        for p in action_pairs:
            lines.append(f"- [{p['axis']}:{p['unit']}] {p['human_action']}")
    return "\n".join(lines) + "\n"
