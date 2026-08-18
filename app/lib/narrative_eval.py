"""narrative_eval.py — C1 ナラティブ品質測定ハーネス（純ロジック・I/Oなし・LLM呼び出しなし）。

dept_reports/_state/edits_*.jsonl（report_feedback.load_edits の戻り値＝人手添削台帳）を
入力に取り、既存の品質ガード判定（ai_narrative._rejection_reason 等）をそのまま再利用して
「AIの一手がどれだけ添削されたか／どれだけ距離があるか／どれだけ機械ガードを通るか」を
集計する。判定ロジックの二重実装はしない（本モジュールは import して通すだけ）。

設計上の注意（申し送り）:
  - banned は本番では trend/delta の有無で "傾向"/"前回" が動的追加されるが、台帳の
    レコードから facts の有無（trend を渡したか等）は復元できない。そのため本モジュールは
    常に BASE 定数（動的追加分を含まない）で採点する＝本番より寛容な側に倒れる
    （フェイルクローズで過検出するより、判定基盤が事実と食い違う方が害が大きいため）。
  - 偽ペア（2種類・report_feedback.pair_corrections には無い区別）: pair_corrections の
    before 選定（「最初の ai/tpl」を無条件採用）は時系列を見ていないため、
    (a) AI/定型文の記録が無いユニット（手編集直で override）、
    (b) 「最後の manual より後にしか ai/tpl が無い」ユニット（人が書いた後にAIが
        再生成された＝時系列が逆転した偽ペア）
    の両方を距離・添削率に混入させる危険がある。本モジュールは pair_corrections を
    使わず、記録列（時系列順＝jsonl追記順）を自前で時系列制約付きに分類する
    （_classify_unit_pairing/_classified_pairs）: 「最後の manual より前」に ai/tpl が
    あるものだけを有効ペア（kind="valid"）とし、AI原文は「最後の manual より前にある
    最初の ai/tpl」（report_feedback の「最初のAI/定型」の思想を時系列制約付きで踏襲）。
    (a) は kind="manual_only"（manual_only_units）、(b) は kind="inverted"
    （inverted_units）として日付別に件数だけ別掲し、距離・true_edit_rate等の
    分子分母からは除外する。
  - regime タグ: 開発プラン_添削フィードバックループ.md §6-1 の反転（2026-08-04 = 新規
    base_date は全ユニットAI文が既定）と override 基準化（2026-08-12）を境に台帳の性質が
    変わる。日付を跨いだ単純平均は regime 混在を招くため、集計は常に base_date 別に返す
    （呼び出し側が必要なら自分で束ねる）。
  - churn は同一 base_date 内の記録列に「連続する」src=ai→src=ai の変化だけを数える
    （ai→manual→ai は挟まれた manual が人の承認なので数えない）。moves_*.json は
    ビルドのたびに上書きされ証拠が残らないため使わない＝edits_*.jsonl のみが証拠。
  - is_taigen_dome はゲート（採否）には使わない。計測専用のヒューリスティックで、
    誤判定は _TAIGEN_PREDICATE_SUFFIXES 等の辞書へ追記して運用で直す。
"""
from __future__ import annotations

import difflib
import re
import statistics
from collections import Counter, defaultdict
from typing import Optional

from .ai_narrative import (
    _alert_reject_reason, _headline_echoes_fact, _norm_ja,  # noqa: F401  (再輸出・現状未使用)
    _rejection_reason, _unit_allow, _ALLOW_FACT_PHRASES, _HEADLINE_MAX, _DIGIT_RE,
    _LEVELING_BANNED, _ADMISSION_BANNED_BASE, _WARD_ADMISSION_BANNED_BASE,
    _SURGERY_BANNED_BASE, _EMERGENCY_LEVELING_BANNED, _EMERGENCY_ADMISSION_BANNED_BASE,
    _CRITICAL_CARE_LEVELING_BANNED, _CRITICAL_CARE_ADMISSION_BANNED_BASE,
    _ER_LEVELING_BANNED, _ER_ADMISSION_BANNED_BASE, _HOSPITAL_SUMMARY_BANNED,
)
from .config import WARD_BANNED_LEVER_TERMS
from .fewshot import _trigram_jaccard as trigram_jaccard
from .report_feedback import load_edits   # noqa: F401  (CLI側で使用)

__all__ = [
    "keep_ratio", "edit_strength", "is_taigen_dome",
    "score_alert_narrative", "aggregate_alert_scores", "banned_for",
    "group_by_unit", "unit_timeline", "edit_stats", "distance_stats",
    "churn_stats", "style_stats", "build_eval_report", "build_eval_md",
    "compare_reports", "trigram_jaccard",
]

# regime タグの境界日（開発プラン_添削フィードバックループ.md §6-1）。
AI_ALWAYS_SINCE = "2026-08-04"     # 新規base_dateの初回ビルドは全ユニットAI文が既定に
OVERRIDE_BASE_SINCE = "2026-08-12"  # overrideは同一base_dateの再ビルドで毎回自動適用に


# ════════════════════════════════════════════════════════════
# 距離（keep/edit_strength）
# ════════════════════════════════════════════════════════════
def keep_ratio(a: Optional[str], b: Optional[str]) -> float:
    """difflib.SequenceMatcher の一致率（1.0=完全一致・0.0=語彙が重ならない）。"""
    return difflib.SequenceMatcher(None, a or "", b or "").ratio()


def edit_strength(a: Optional[str], b: Optional[str]) -> float:
    """1 - keep_ratio（0.0=無変更・1.0=総入れ替え）。"""
    return 1.0 - keep_ratio(a, b)


# ════════════════════════════════════════════════════════════
# 体言止め判定（計測専用・ゲートには使わない）
# ════════════════════════════════════════════════════════════
_TAIGEN_PARTICLES = "をにはがへとでもやかねよ"
# 長い順一致（"しています"を先に見ないと"います"止まりで誤判定する語がある）。
_TAIGEN_PREDICATE_SUFFIXES = tuple(sorted((
    "している", "していない", "しています", "されている", "されました", "しました",
    "します", "できない", "できる", "ている", "ていた", "ました", "ません", "である",
    "でした", "られる", "なった", "です", "ます", "ない", "いる", "ある", "なる",
    "った", "れる", "する", "した", "して", "たい", "だ", "ください",
    # i形容詞（明示列挙・ゲートに使わないため辞書追記で運用）
    "高い", "低い", "多い", "少ない", "大きい", "小さい", "強い", "弱い", "悪い",
    "良い", "厳しい", "著しい", "乏しい", "鈍い", "早い", "遅い", "長い", "短い", "無い",
), key=len, reverse=True))

# 末尾括弧注記を1回だけ剥がす（「〜（なお未達）」等）。ネスト括弧は非対応（実データで未観測）。
_TAIGEN_PAREN_RE = re.compile(r"[（(〔\[][^（(〔\[]*[）)〕\]]$")
# 末尾の句読点・記号・空白を除去。
_TAIGEN_TRAIL_RE = re.compile(r"[。．.!！?？…、，,・\s]+$")


def is_taigen_dome(headline: str) -> bool:
    """見出しが体言止めか（ヒューリスティック・計測専用）。

    手順: ①末尾括弧注記を1回剥がす → ②末尾の句読点/記号/空白を除去 →
    ③末尾1字が助詞なら False → ④末尾が述語接尾辞（長い順一致）なら False →
    ⑤それ以外 True。誤判定はゲートに影響しない（本関数の辞書追記で運用改善する）。
    """
    s = (headline or "").strip()
    s = _TAIGEN_PAREN_RE.sub("", s)
    s = _TAIGEN_TRAIL_RE.sub("", s)
    if not s:
        return False
    if s[-1] in _TAIGEN_PARTICLES:
        return False
    for suf in _TAIGEN_PREDICATE_SUFFIXES:
        if s.endswith(suf):
            return False
    return True


# ════════════════════════════════════════════════════════════
# アラート見出し（narrate_alerts経路）の採点
# ════════════════════════════════════════════════════════════
# banned_hits は "参考"（gating無し）。alerts.py の narrate_alerts / _alert_reject_reason は
# トピック別 banned を持たない（parse/empty/headline_long/headline_echoの4理由のみが正式な
# 棄却理由）ため、代わりに全トピックのBASE禁止語の和集合に触れていないかを診断的に見る。
_ALL_BANNED_REFERENCE = tuple(sorted(set(
    _LEVELING_BANNED + _ADMISSION_BANNED_BASE + _WARD_ADMISSION_BANNED_BASE +
    _SURGERY_BANNED_BASE + _EMERGENCY_LEVELING_BANNED + _EMERGENCY_ADMISSION_BANNED_BASE +
    _CRITICAL_CARE_LEVELING_BANNED + _CRITICAL_CARE_ADMISSION_BANNED_BASE +
    _ER_LEVELING_BANNED + _ER_ADMISSION_BANNED_BASE + _HOSPITAL_SUMMARY_BANNED +
    WARD_BANNED_LEVER_TERMS
)))


def score_alert_narrative(alert: dict, obj: Optional[dict]) -> dict:
    """narrate_alerts経路の1件を採点する（本番ガード _alert_reject_reason をそのまま再利用）。"""
    reason = _alert_reject_reason(obj, alert)
    headline = ((obj or {}).get("headline") or "") if obj else ""
    body = ((obj or {}).get("body") or "") if obj else ""
    action = ((obj or {}).get("action") or "") if obj else ""
    hlen = len(headline)
    return {
        "reject_reason": reason,
        "echo": _headline_echoes_fact(headline, alert.get("facts")),
        "len_ok_hard": hlen <= _HEADLINE_MAX,
        "len_ok_spec": hlen <= 20,
        "len_on_target": 12 <= hlen <= 18,
        "taigen": is_taigen_dome(headline),
        "body_digit": bool(_DIGIT_RE.search(body)),           # 参考（gatingしない）
        "banned_hits": tuple(t for t in _ALL_BANNED_REFERENCE if t in body or t in action),  # 参考
        "headline_len": hlen,
    }


def _percentile(values: list[float], pct: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * (pct / 100)
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return float(s[f])
    return float(s[f] + (s[c] - s[f]) * (k - f))


def aggregate_alert_scores(rows: list[dict]) -> dict:
    """score_alert_narrative の戻り値のリストを集計する。分母は常に全件（rows全体）。"""
    n = len(rows)
    if n == 0:
        return {"n": 0}

    def _rate(key: str) -> float:
        return sum(1 for r in rows if r.get(key)) / n

    lens = [r["headline_len"] for r in rows]
    return {
        "n": n,
        "accept_rate": sum(1 for r in rows if r.get("reject_reason") is None) / n,
        "echo_rate": _rate("echo"),
        "len_ok_hard_rate": _rate("len_ok_hard"),
        "len_ok_spec_rate": _rate("len_ok_spec"),
        "len_on_target_rate": _rate("len_on_target"),
        "taigen_rate": _rate("taigen"),
        "headline_len_median": statistics.median(lens),
        "headline_len_p90": _percentile(lens, 90),
        "reject_reasons": dict(Counter(r.get("reject_reason") or "ok" for r in rows)),
    }


# ════════════════════════════════════════════════════════════
# banned_for: (axis, topic) → 採点に使う禁止語BASE集合
# ════════════════════════════════════════════════════════════
def banned_for(axis: Optional[str], topic: Optional[str]) -> tuple:
    """topic/axis から ai_narrative.py の禁止語BASE定数を引く写像。

    動的追加分（"傾向"=trend未提供時、"前回"=delta未提供時、"祝日"/"連休"等）は
    含めない（BASEのみで採点＝モジュールdocstring参照）。未知の (axis, topic) は
    KeyErrorにせず空タプルを返す。

    topic は "leveling"/"admission"/"surgery"（axis=dept/ward/hospitalに共通）、または
    特例ユニット用の "emergency-"/"critical_care-"/"er_dept-" プレフィクス＋
    "leveling"/"admission"（dept_report._special_narration_kind が付与する形式）。
    """
    if not topic:
        return ()
    for prefix, leveling_c, admission_base_c in (
        ("emergency-", _EMERGENCY_LEVELING_BANNED, _EMERGENCY_ADMISSION_BANNED_BASE),
        ("critical_care-", _CRITICAL_CARE_LEVELING_BANNED, _CRITICAL_CARE_ADMISSION_BANNED_BASE),
        ("er_dept-", _ER_LEVELING_BANNED, _ER_ADMISSION_BANNED_BASE),
    ):
        if topic.startswith(prefix):
            sub = topic[len(prefix):]
            return leveling_c if sub == "leveling" else admission_base_c
    # 病院全体サマリは topic=leveling/admission/surgery のいずれでも単一の禁止語集合を使う
    # （narrate_hospital_summary が h_topic によらず _HOSPITAL_SUMMARY_BANNED を使うのと同じ）。
    if axis == "hospital":
        return _HOSPITAL_SUMMARY_BANNED
    if topic == "leveling":
        return (_LEVELING_BANNED + WARD_BANNED_LEVER_TERMS) if axis == "ward" else _LEVELING_BANNED
    if topic == "admission":
        return _WARD_ADMISSION_BANNED_BASE if axis == "ward" else _ADMISSION_BANNED_BASE
    if topic == "surgery":
        return _SURGERY_BANNED_BASE
    return ()


# ════════════════════════════════════════════════════════════
# ユニット単位のグルーピング／タイムライン
# ════════════════════════════════════════════════════════════
def group_by_unit(records: list[dict]) -> dict:
    """(base_date, axis, unit) → [記録順のレコード…] へグルーピングする。"""
    groups: dict = defaultdict(list)
    for r in records:
        groups[(r.get("base_date"), r.get("axis"), r.get("unit"))].append(r)
    return dict(groups)


def unit_timeline(recs: list[dict]) -> dict:
    """1ユニット分のレコード列（記録順）から要約情報を作る。"""
    if not recs:
        return {"first_src": None, "final_src": None, "n_builds": 0,
                "ai_seq": [], "n_records": 0}
    ts_set = {r.get("ts") for r in recs if r.get("ts") is not None}
    return {
        "first_src": recs[0].get("src"),
        "final_src": recs[-1].get("src"),
        "n_builds": len(ts_set),
        "ai_seq": [r.get("src") for r in recs],
        "n_records": len(recs),
    }


def _units_total_by_date(records: list[dict]) -> dict:
    """base_date → その日の distinct (axis, unit) 数。"""
    seen: dict = defaultdict(set)
    for r in records:
        seen[r.get("base_date")].add((r.get("axis"), r.get("unit")))
    return {d: len(u) for d, u in seen.items()}


def _classify_unit_pairing(recs: list[dict]) -> Optional[dict]:
    """1ユニット分の記録列（時系列順＝jsonl追記順）を分類する。

    manual を1件も持たなければ None（人手添削の対象外＝集計に含めない）。
    manual を持つ場合、"最後の manual" を基準に3分類する:
      - "valid":        最後の manual より**前**に src∈(ai,tpl) の記録がある＝有効ペア。
                        before はその中の**最初**の ai/tpl（report_feedback の「最初の
                        AI/定型」の思想を時系列制約付きで踏襲）。
      - "inverted":     最後の manual より前には無いが、記録列のどこか（＝後）に
                        ai/tpl がある＝時系列逆転（人が書いた後にAIが再生成された）。
      - "manual_only":  記録列のどこにも ai/tpl が無い＝AI記録なし（手編集直で override）。
    戻り値: {"kind", "final_manual", "before"（validのときのみ ai/tpl レコード、他は None）}。
    """
    manual_idxs = [i for i, r in enumerate(recs) if r.get("src") == "manual"]
    if not manual_idxs:
        return None
    last_idx = manual_idxs[-1]
    final_manual = recs[last_idx]
    before = next((r for r in recs[:last_idx] if r.get("src") in ("ai", "tpl")), None)
    if before is not None:
        return {"kind": "valid", "final_manual": final_manual, "before": before}
    any_ai_tpl = next((r for r in recs if r.get("src") in ("ai", "tpl")), None)
    if any_ai_tpl is not None:
        return {"kind": "inverted", "final_manual": final_manual, "before": None}
    return {"kind": "manual_only", "final_manual": final_manual, "before": None}


def _classified_pairs(records: list[dict]) -> list[dict]:
    """(base_date, axis, unit) ごとに _classify_unit_pairing した結果を平坦なリストで返す
    （manual を1件も持たないユニットは含まない）。kind="valid" のときだけ ai_body/ai_action/
    changed/before_src が意味を持つ（他 kind では None/[]）。"""
    out = []
    for (date, axis, unit), recs in group_by_unit(records).items():
        c = _classify_unit_pairing(recs)
        if c is None:
            continue
        final_manual, before = c["final_manual"], c["before"]
        changed = []
        if before is not None:
            if (before.get("body") or "") != (final_manual.get("body") or ""):
                changed.append("body")
            if (before.get("action") or "") != (final_manual.get("action") or ""):
                changed.append("action")
        out.append({
            "date": date, "axis": axis, "unit": unit, "kind": c["kind"],
            "topic": final_manual.get("topic"), "changed": changed,
            "before_src": before.get("src") if before else None,
            "ai_body": before.get("body") if before else None,
            "ai_action": before.get("action") if before else None,
            "human_body": final_manual.get("body"),
            "human_action": final_manual.get("action"),
        })
    return out


# ════════════════════════════════════════════════════════════
# 添削率（edit_stats）
# ════════════════════════════════════════════════════════════
def edit_stats(records: list[dict]) -> dict:
    """base_date別の添削率。母数は常に units_total（=その日のdistinct(axis,unit)数）。

    - manual_rate: 人手添削されたユニットの割合（kind問わず manual を持つ全ユニット）。
    - true_edit_rate: kind="valid" かつ body/actionが実際に変化したユニットの割合。
    - reapply_rate: kind="valid" かつ ai/tpl文とmanual文が完全一致（変化なしの再確定）の割合。
    - body_edit_rate_paired/action_edit_rate_paired: 分母=paired_units（kind="valid"件数）。
    - body_edit_rate_total/action_edit_rate_total: 分母=units_total（併記）。
    - manual_only_units: kind="manual_only"（AI記録が全く無い）件数（距離計算からは除外）。
    - inverted_units: kind="inverted"（時系列逆転の偽ペア）件数（距離計算からは除外）。
    """
    units_total = _units_total_by_date(records)
    by_date: dict = defaultdict(list)
    for p in _classified_pairs(records):
        by_date[p["date"]].append(p)

    out = {}
    for date, tot in units_total.items():
        dpairs = by_date.get(date, [])
        manual_units = len(dpairs)
        valid = [p for p in dpairs if p["kind"] == "valid"]
        inverted = [p for p in dpairs if p["kind"] == "inverted"]
        manual_only = [p for p in dpairs if p["kind"] == "manual_only"]
        paired_units = len(valid)
        true_edit = [p for p in valid if p["changed"]]
        reapply = [p for p in valid if not p["changed"]]
        body_edit = [p for p in valid if "body" in p["changed"]]
        action_edit = [p for p in valid if "action" in p["changed"]]
        out[date] = {
            "units_total": tot,
            "manual_units": manual_units,
            "manual_rate": (manual_units / tot) if tot else 0.0,
            "manual_only_units": len(manual_only),
            "inverted_units": len(inverted),
            "paired_units": paired_units,
            "true_edit_units": len(true_edit),
            "true_edit_rate": (len(true_edit) / tot) if tot else 0.0,
            "reapply_units": len(reapply),
            "reapply_rate": (len(reapply) / tot) if tot else 0.0,
            "body_edit_rate_paired": (len(body_edit) / paired_units) if paired_units else None,
            "body_edit_rate_total": (len(body_edit) / tot) if tot else 0.0,
            "action_edit_rate_paired": (len(action_edit) / paired_units) if paired_units else None,
            "action_edit_rate_total": (len(action_edit) / tot) if tot else 0.0,
        }
    return out


# ════════════════════════════════════════════════════════════
# 距離（distance_stats）
# ════════════════════════════════════════════════════════════
def _round_stat(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean": None, "median": None, "p90": None}
    p90 = _percentile(values, 90)
    return {
        "n": len(values),
        "mean": round(statistics.mean(values), 3),
        "median": round(statistics.median(values), 3),
        "p90": round(p90, 3) if p90 is not None else None,
    }


def _summarize_distance(vals: list[dict]) -> dict:
    keep_vals = [v["keep"] for v in vals]
    micro = sum(1 for k in keep_vals if k >= 0.95)
    partial = sum(1 for k in keep_vals if 0.6 <= k < 0.95)
    rewrite = sum(1 for k in keep_vals if k < 0.6)
    by_src: dict = defaultdict(list)
    for v in vals:
        by_src[v["before_src"] or "unknown"].append(v)
    return {
        "n": len(vals),
        "keep": _round_stat(keep_vals),
        "edit_strength": _round_stat([v["edit_strength"] for v in vals]),
        "trigram_jaccard": _round_stat([v["trigram_jaccard"] for v in vals]),
        "len_delta": _round_stat([float(v["len_delta"]) for v in vals]),
        "buckets": {"micro": micro, "partial": partial, "rewrite": rewrite},
        "by_before_src": {
            src: {"n": len(sv), "keep": _round_stat([v["keep"] for v in sv])}
            for src, sv in by_src.items()
        },
    }


def distance_stats(records: list[dict]) -> dict:
    """kind="valid"（時系列制約を満たす有効ペア）かつ当該fieldがchangedのものだけを対象に、
    body/actionそれぞれの AI原文→人の最終文の距離を集計する。kind="manual_only"（AI記録
    なし）・kind="inverted"（時系列逆転の偽ペア）は除外する（モジュールdocstring参照）。
    before_src（ai/tpl）別にも分けて返す（tplからの添削は「AIの品質」を測っていないため）。"""
    pairs = [p for p in _classified_pairs(records) if p["kind"] == "valid"]
    out = {}
    for field in ("body", "action"):
        vals = []
        for p in pairs:
            if field not in p["changed"]:
                continue
            a = p.get(f"ai_{field}") or ""
            b = p.get(f"human_{field}") or ""
            vals.append({
                "keep": keep_ratio(a, b),
                "edit_strength": edit_strength(a, b),
                "trigram_jaccard": trigram_jaccard(a, b),
                "len_delta": len(b) - len(a),
                "before_src": p["before_src"],
            })
        out[field] = _summarize_distance(vals)
    return out


# ════════════════════════════════════════════════════════════
# churn（同一base_date内でのAI→AIの揺れ）
# ════════════════════════════════════════════════════════════
def churn_stats(records: list[dict]) -> dict:
    """base_date別のchurn（AI再生成のブレ）。記録列に連続するsrc=ai→src=aiで
    (body,action)が変化した箇所を1ユニット1回とカウントする（ai→manual→aiは対象外）。"""
    units_total = _units_total_by_date(records)
    ts_by_date: dict = defaultdict(set)
    churn_units_by_date: dict = defaultdict(set)

    for (date, axis, unit), recs in group_by_unit(records).items():
        for r in recs:
            if r.get("ts") is not None:
                ts_by_date[date].add(r["ts"])
        for i in range(len(recs) - 1):
            r1, r2 = recs[i], recs[i + 1]
            if (r1.get("src") == "ai" and r2.get("src") == "ai"
                    and (r1.get("body"), r1.get("action")) != (r2.get("body"), r2.get("action"))):
                churn_units_by_date[date].add((axis, unit))

    out = {}
    for date, tot in units_total.items():
        units = len(churn_units_by_date.get(date, ()))
        builds = len(ts_by_date.get(date, ()))
        out[date] = {
            "units_total": tot,
            "ai_churn_units": units,
            "ai_churn_rate": (units / tot) if tot else 0.0,
            "builds": builds,
            "churn_per_extra_build": units / max(builds - 1, 1),
        }
    return out


# ════════════════════════════════════════════════════════════
# スタイル（機械ガード通過率の再検査）
# ════════════════════════════════════════════════════════════
def style_stats(records: list[dict], src: str = "ai") -> dict:
    """src（既定 "ai"）のレコードを _rejection_reason に通し、理由別に集計する。
    src="manual" にも通せる（人の文がガードで落ちる率＝ガードの厳しさの検算に使う）。"""
    rows = [r for r in records if r.get("src") == src]
    reasons: Counter = Counter()
    for r in rows:
        obj = {"body": r.get("body") or "", "action": r.get("action") or ""}
        banned = banned_for(r.get("axis"), r.get("topic"))
        allow = _unit_allow(r.get("unit") or "") + _ALLOW_FACT_PHRASES
        reason = _rejection_reason(obj, banned=banned, allow=allow)
        reasons[reason or "ok"] += 1
    n = len(rows)
    return {"n": n, "ok_rate": (reasons.get("ok", 0) / n) if n else None,
            "reasons": dict(reasons)}


# ════════════════════════════════════════════════════════════
# レポート組み立て
# ════════════════════════════════════════════════════════════
def _regime_tag(date: str, paired_units: int, manual_units: int) -> str:
    """日付とその日の paired/manual 比から regime タグを付ける。
    paired/manual < 0.5 は「AI原文の記録がほとんど無いのにmanualだけ多い」＝
    §6-1反転前の旧形式データの自己診断シグナルとして legacy を優先する。"""
    if manual_units and (paired_units / manual_units) < 0.5:
        return "legacy"
    if date >= OVERRIDE_BASE_SINCE:
        return "override_base"
    if date >= AI_ALWAYS_SINCE:
        return "ai_always"
    return "pre_ai_always"


def build_eval_report(records: list[dict], alert_rows: Optional[list[dict]] = None) -> dict:
    """人手添削台帳(report_feedback.load_edits の戻り値)から品質レポートを組み立てる。

    alert_rows: [{"alert": {...,"facts":[...]}, "narrative": {"headline","body","action"} or None}]
    （--alerts-recorded で読んだJSONL）。渡さなければ alert_scores は含めない。
    日付を跨いだ単純平均は出さない（regime混在を避けるため base_date 別のまま返す）。
    """
    edits = edit_stats(records)
    churn = churn_stats(records)
    dist = distance_stats(records)
    style_ai = style_stats(records, src="ai")
    style_manual = style_stats(records, src="manual")

    regimes = {date: _regime_tag(date, e["paired_units"], e["manual_units"])
               for date, e in edits.items()}

    report = {
        "n_records": len(records),
        "dates": sorted(edits.keys()),
        "regimes": regimes,
        "edit_stats": edits,
        "churn_stats": churn,
        "distance_stats": dist,
        "style_ai": style_ai,
        "style_manual": style_manual,
    }
    if alert_rows:
        scored = [score_alert_narrative(row.get("alert") or {}, row.get("narrative"))
                 for row in alert_rows]
        report["alert_scores"] = aggregate_alert_scores(scored)
    return report


def build_eval_md(report: dict) -> str:
    """build_eval_report の戻り値を目視用 markdown へ整形する。"""
    lines = ["# ナラティブ品質評価（C1）", ""]
    lines.append(f"- レコード数: {report.get('n_records', 0)}")
    dates = report.get("dates") or []
    if not dates:
        lines.append("")
        lines.append("まだ台帳がありません（ビルドを重ねると _state/edits_*.jsonl に蓄積されます）。")
        return "\n".join(lines) + "\n"
    lines.append(f"- 対象日付: {dates[0]} 〜 {dates[-1]}（{len(dates)}日）")
    lines.append("")

    lines.append("## 日付別 添削率（母数=units_total）")
    for d in dates:
        e = report.get("edit_stats", {}).get(d, {})
        tag = report.get("regimes", {}).get(d, "")
        lines.append(
            f"- {d}（{tag}）: units={e.get('units_total')} "
            f"manual_rate={e.get('manual_rate', 0):.2f} "
            f"true_edit_rate={e.get('true_edit_rate', 0):.2f} "
            f"reapply_rate={e.get('reapply_rate', 0):.2f} "
            f"manual_only_units={e.get('manual_only_units', 0)} "
            f"inverted_units={e.get('inverted_units', 0)}")
    lines.append("")

    lines.append("## 添削の距離（有効ペア=kind:validのみ）")
    for field in ("body", "action"):
        d = report.get("distance_stats", {}).get(field, {})
        if d.get("n"):
            lines.append(
                f"- {field}: n={d['n']} keep_mean={d['keep']['mean']} "
                f"edit_strength_mean={d['edit_strength']['mean']} "
                f"buckets(微修正/部分添削/書き直し)={d['buckets']}")
    lines.append("")

    lines.append("## churn（同一日内のAI→AIの揺れ）")
    any_churn = False
    for d in dates:
        c = report.get("churn_stats", {}).get(d, {})
        if c.get("ai_churn_units"):
            any_churn = True
            lines.append(
                f"- {d}: ai_churn_units={c['ai_churn_units']} "
                f"rate={c['ai_churn_rate']:.2f} builds={c['builds']}")
    if not any_churn:
        lines.append("- churnなし")
    lines.append("")

    lines.append("## スタイル（機械ガード通過率）")
    for label, s in (("AI採択文", report.get("style_ai", {})),
                     ("人手添削文", report.get("style_manual", {}))):
        if s.get("n"):
            lines.append(f"- {label}: n={s['n']} ok_rate={s.get('ok_rate', 0):.2f} "
                         f"reasons={s.get('reasons')}")

    if "alert_scores" in report:
        a = report["alert_scores"]
        lines.append("")
        lines.append("## アラート見出し品質")
        lines.append(
            f"- n={a['n']} accept_rate={a['accept_rate']:.2f} "
            f"echo_rate={a['echo_rate']:.2f} taigen_rate={a['taigen_rate']:.2f} "
            f"headline_len_median={a['headline_len_median']} "
            f"headline_len_p90={a['headline_len_p90']}")
    return "\n".join(lines) + "\n"


def compare_reports(a: dict, b: dict) -> str:
    """2つの build_eval_report スナップショットを比較する（markdown）。"""
    lines = ["# ナラティブ品質評価 比較（A → B）", ""]
    lines.append(f"- レコード数: A={a.get('n_records', 0)} → B={b.get('n_records', 0)}")
    common = sorted(set(a.get("dates") or []) & set(b.get("dates") or []))
    if not common:
        lines.append("- 共通の日付がありません（比較できる重なりなし）")
        return "\n".join(lines) + "\n"
    lines.append("")
    lines.append("## 日付別 添削率の変化")
    for d in common:
        ea = a.get("edit_stats", {}).get(d, {})
        eb = b.get("edit_stats", {}).get(d, {})
        lines.append(
            f"- {d}: manual_rate {ea.get('manual_rate', 0):.2f} → {eb.get('manual_rate', 0):.2f}"
            f" / true_edit_rate {ea.get('true_edit_rate', 0):.2f} → {eb.get('true_edit_rate', 0):.2f}"
            f" / reapply_rate {ea.get('reapply_rate', 0):.2f} → {eb.get('reapply_rate', 0):.2f}")
    return "\n".join(lines) + "\n"
