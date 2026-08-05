"""narrative_ab.py — AIコメント生成モデルのA/Bハーネス（人手判断なしで測れる指標のみ）。

2026-08-04 に Dashboard の既定モデルが Llama-3.1-Swallow-8B-Instruct-v0.5 →
gemma-4-26B-A4B-it-qat-OptiQ-4bit（data/model_override.json）へ切り替わった。この切替が
品質を落としていないかを、**本番の生成コードパスをそのまま使い**、モデルだけ差し替えて測る
（プロンプトは一切再実装しない・Masking/tools/adversarial_ab.py・Slides/tools/model_ab.py と
同じ「実LLMを叩くハーネス」の作法）。

対象は「この期間の一手」（部門別レポート・診療科版/病棟版＋病院全体サマリ）の body/action
生成: app.lib.dept_report.build_dept_report_contexts / build_hospital_overview_context
→ app.lib.ai_narrative の narrate_* 群。これは scripts/report_comment_diversity.py が
Jaccard 均質化を実測してきた経路と同一（2026-07-09/2026-08-04「外科系11/11科が同一レバーに
収束→是正」の実測もこの経路・Jaccard 0.425→0.142）。narrate_alerts（headline/body/action・
ポータルのアラートカード）は「部門をまたいだ均質化」という本ハーネスの主眼と異なる別経路の
ため対象外（詳細は委譲元への報告を参照）。

同一の入力（同一 base_date の adm/surg/targets 等スナップショット・generate_html の既存
ローダーで作る）を全モデルへ与える。モデル切替は app.lib.ai_narrative.chat_json をラップし、
実際のHTTP呼び出し直前で model= を強制上書きする方式（ModelSwap）。各 narrate_* 関数の
model= 引数やプロンプト構築コードは一切変更しない。

測る指標（すべて人手判断なし）:
  - 数値の捏造: app.lib.ai_narrative._DIGIT_RE / _MAX_TEXT_LEN と同じ判定ロジックを再利用し、
    最終文（AI採択後）に許容フレーズ・ユニット名以外の数字が残っていないかを検査する。
    ※ app/lib/proofread.py にも「数値」を扱うガードがあるが、あれは既存文の校正前後で
    数字列が一致するかを見るもの（校正対象＝当ハーネス外）。実際にこの生成経路をゲート
    しているのは ai_narrative 側の digit ガードなので、そちらを再利用する
    （判断の詳細は委譲元への報告を参照）。
  - 書式遵守: JSON envelope の妥当性（body/action が揃っているか）＋各トピックの
    SYSTEM_PROMPT の【出力スキーマ】に書かれた文字数レンジ（例:「50〜80字」）を正規表現で
    読み取り、実際の文字数がレンジ内かを検査する（レンジの数値は書き写さず、本番プロンプト
    文字列から都度パースする＝プロンプトが変わっても追随する）。
  - 禁止語: ai_narrative.py の各トピック用 _*_BANNED 定数を直接 import して再利用する
    （新規定義しない）。文脈依存の連動緩和（前年/祝日等、当日の事実が与えられたかで禁止語が
    変わる）の一部は本ハーネスからは再現できないため、常に「安全側（渡していない前提）」で
    判定する（_build_topic_tables() 直上のコメント参照。両モデルに同一基準を適用するため、
    相対比較の公平性は保たれる）。
  - 均質化(Jaccard): scripts/report_comment_diversity.py の文字3-gram Jaccard 実装
    （_ngrams/_jaccard/_pairwise/_dup_groups）をそのまま import して再利用する（実測の連続性
    を保つため、独自の定義を作らない）。診療科・病棟をまたいだ全ペアで算出する。
  - 生成失敗: app.lib.ai_narrative.REJECT_STATS（parse/digit/banned/length/judge/error/ok/
    ok@retry）をモデルごとにリセットして集計する。chat_json 呼び出し自体の例外は別途集計する。
  - 所要時間: 参考値。モデルごとの壁時計・chat_json 呼び出し秒数（中央値/合計）。

使い方:
    /Users/genie/dev/ai-apps/.venv/bin/python tools/narrative_ab.py \\
        --models Llama-3.1-Swallow-8B-Instruct-v0.5,gemma-4-26B-A4B-it-qat-OptiQ-4bit \\
        --axes dept,ward --out /tmp/narrative_ab_result.json

    # 引数なしなら「切替前の既定モデル」と「data/model_override.json が指す現行モデル」を比較する。
    /Users/genie/dev/ai-apps/.venv/bin/python tools/narrative_ab.py

★このハーネスは実LLMを叩く（oMLX起動が必要）。pytestからは実行されない
（tools/ 配下・import時にLLM副作用なし）。
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parents[1]  # Dashboard/
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.lib.config import DEFAULT_DATA_DIR  # noqa: E402
from app.lib.llm import DEFAULT_MODEL as CURRENT_MODEL  # noqa: E402

# 2026-08-04 切替前の既定モデル（app/lib/llm.py:_resolve_default_model の最終フォールバックと
# 同値）。data/model_override.json が無かった/OMLX_MODEL未設定だった頃の既定モデル。
OLD_MODEL = "Llama-3.1-Swallow-8B-Instruct-v0.5"
# 重複除去（override 未設定環境では OLD_MODEL == CURRENT_MODEL になり得る）・順序保持。
DEFAULT_MODELS = list(dict.fromkeys([OLD_MODEL, CURRENT_MODEL]))


# ────────────────────────────────────────────────────────────────
# トピック→禁止語／SYSTEM_PROMPT の対応表
# ────────────────────────────────────────────────────────────────
# app.lib.ai_narrative の各 narrate_* が使う禁止語タプルは、いくつか「当日その事実を渡したか」
# で内容が変わる（例: 新入院/全麻トピックは前年同期比較(yoy)を渡した日だけ「前年」の禁止が
# 外れる／delta・holiday も同様）。本ハーネスは delta_anchor を渡さない（report_comment_
# diversity.py と同じ・継続性state不要）ため「前回」は常に禁止語（決定論的に正しい）。
# 一方 yoy・holiday は当日のデータに依存し、ここから静的には再現できないため「前年」「祝日」
# 「連休」は超過検出の対象に含めない（含めると、正当に yoy 事実を渡された大半の科で
# 「前年同期を上回っている」等の正しい記述まで誤検出してしまうため）。全麻の「達成(met)版」
# でのみ追加される禁止語（件数増/積み増し/増やし）も同じ理由で対象外にしている
# （未達版では逆にこれらは推奨表現のため、常時禁止に含めると誤検出になる）。
# → 本ハーネスの禁止語チェックは「常に安全側（渡していない前提）」の静的近似であり、完全な
#   本番一致ではない。ただし両モデルに同一基準を適用するため、モデル間の相対比較としては公平。
def _build_topic_tables():
    """app.lib.ai_narrative の内部定数（禁止語タプル・SYSTEM_PROMPT）を直接参照する
    対応表を作る（値を書き写さず、モジュールの定数オブジェクトをそのまま束ねるだけ）。"""
    from app.lib import ai_narrative as an  # 重い import はここまで遅延

    ward_lever = an.WARD_BANNED_LEVER_TERMS
    banned_by_key = {
        ("dept", "leveling"): an._LEVELING_BANNED + ("前回",),
        ("ward", "leveling"): an._LEVELING_BANNED + ("前回",) + ward_lever,
        ("dept", "admission"): an._ADMISSION_BANNED + ("前回",),
        ("ward", "admission"): an._WARD_ADMISSION_BANNED + ("前回",),
        ("dept", "surgery"): an._SURGERY_BANNED + ("前回",),
        ("dept", "emergency-leveling"): an._EMERGENCY_LEVELING_BANNED,
        ("ward", "emergency-leveling"): an._EMERGENCY_LEVELING_BANNED,
        ("dept", "emergency-admission"): an._EMERGENCY_ADMISSION_BANNED,
        ("ward", "emergency-admission"): an._EMERGENCY_ADMISSION_BANNED,
        ("dept", "critical_care-leveling"): an._CRITICAL_CARE_LEVELING_BANNED,
        ("ward", "critical_care-leveling"): an._CRITICAL_CARE_LEVELING_BANNED,
        ("dept", "critical_care-admission"): an._CRITICAL_CARE_ADMISSION_BANNED,
        ("ward", "critical_care-admission"): an._CRITICAL_CARE_ADMISSION_BANNED,
        ("dept", "er_dept-leveling"): an._ER_LEVELING_BANNED,
        ("dept", "er_dept-admission"): an._ER_ADMISSION_BANNED,
    }
    prompt_by_key = {
        ("dept", "leveling"): an.LEVELING_ACTION_SYSTEM_PROMPT,
        ("ward", "leveling"): an.LEVELING_ACTION_SYSTEM_PROMPT,
        ("dept", "admission"): an.ADMISSION_ACTION_SYSTEM_PROMPT,
        ("ward", "admission"): an.WARD_ADMISSION_ACTION_SYSTEM_PROMPT,
        ("dept", "surgery"): an.SURGERY_ACTION_SYSTEM_PROMPT,
        ("dept", "emergency-leveling"): an.EMERGENCY_LEVELING_SYSTEM_PROMPT,
        ("ward", "emergency-leveling"): an.EMERGENCY_LEVELING_SYSTEM_PROMPT,
        ("dept", "emergency-admission"): an.EMERGENCY_ADMISSION_SYSTEM_PROMPT,
        ("ward", "emergency-admission"): an.EMERGENCY_ADMISSION_SYSTEM_PROMPT,
        ("dept", "critical_care-leveling"): an.CRITICAL_CARE_LEVELING_SYSTEM_PROMPT,
        ("ward", "critical_care-leveling"): an.CRITICAL_CARE_LEVELING_SYSTEM_PROMPT,
        ("dept", "critical_care-admission"): an.CRITICAL_CARE_ADMISSION_SYSTEM_PROMPT,
        ("ward", "critical_care-admission"): an.CRITICAL_CARE_ADMISSION_SYSTEM_PROMPT,
        ("dept", "er_dept-leveling"): an.ER_LEVELING_SYSTEM_PROMPT,
        ("dept", "er_dept-admission"): an.ER_ADMISSION_SYSTEM_PROMPT,
    }
    # axis="hospital" は topic(leveling/admission/surgery) によらず専用の禁止語/プロンプト。
    hospital_banned = an._HOSPITAL_SUMMARY_BANNED + ("前回",)
    hospital_prompt = an.HOSPITAL_SUMMARY_SYSTEM_PROMPT
    return an, banned_by_key, prompt_by_key, hospital_banned, hospital_prompt


# ────────────────────────────────────────────────────────────────
# 書式遵守: SYSTEM_PROMPT の【出力スキーマ】に書かれた文字数レンジをパースする
# ────────────────────────────────────────────────────────────────
_SCHEMA_LEN_RE = re.compile(r'"(headline|body|action)"\s*:\s*"[^"]*?(\d+)(?:[〜~](\d+))?字')


def _extract_schema_ranges(system_prompt: str) -> dict:
    """SYSTEM_PROMPT内の【出力スキーマ】ブロックから body/action の文字数レンジを読み取る。
    レンジの数値はここで定義せず、本番プロンプト文字列から都度パースする
    （例:「50〜80字」→(50,80)、「20字以内」→(0,20)）。"""
    block = system_prompt
    if "【出力スキーマ】" in system_prompt:
        block = system_prompt.split("【出力スキーマ】", 1)[1].split("【", 1)[0]
    ranges = {}
    for m in _SCHEMA_LEN_RE.finditer(block):
        key, lo = m.group(1), int(m.group(2))
        hi = int(m.group(3)) if m.group(3) else None
        ranges[key] = (lo, hi) if hi is not None else (0, lo)
    return ranges


# ────────────────────────────────────────────────────────────────
# 数値の捏造・禁止語・長さ の検査（app.lib.ai_narrative._rejection_reason と同じロジック）
# ────────────────────────────────────────────────────────────────
def _scan_text_violations(an, text: str, banned: tuple, allow: tuple) -> dict:
    """ai_narrative._rejection_reason と同じアルゴリズム（allowフレーズ除去→数字検査→
    禁止語検査→長さ検査）を適用する。_rejection_reason は最初に一致した理由だけを返すため、
    3種類を独立に判定できるようここでばらす（薄いラッパー・判定式自体は _DIGIT_RE /
    _MAX_TEXT_LEN という同じオブジェクト/定数を再利用する）。"""
    scan = text
    for a in sorted((a for a in allow if a), key=len, reverse=True):
        scan = scan.replace(a, "")
    digit = bool(an._DIGIT_RE.search(scan))
    hit_banned = [p for p in banned if p in text]
    over_len = len(text) > an._MAX_TEXT_LEN
    return {"digit": digit, "banned": hit_banned, "over_len": over_len}


# ────────────────────────────────────────────────────────────────
# モデル差し替え（ai_narrative.chat_json をラップし、発呼直前で model= を強制上書きする）
# ────────────────────────────────────────────────────────────────
class ModelSwap:
    """narrate_* 各関数はそれぞれ model=DEFAULT_MODEL を既定値に持つ（呼び出し元
    dept_report.py は model= を明示しないため常にこの既定値が使われる）。この既定値を
    呼び出し側ごとに書き換える代わりに、実際にHTTPを叩く chat_json をここで丸ごと差し替え、
    受け取った model 引数を無視して target_model を強制する。narrate_* のロジック・
    プロンプト構築コードは一切変更しない（「モデルだけ差し替える」を実現する最小の介入点）。
    """

    def __init__(self, an_module, target_model: str):
        self._an = an_module
        self._orig = an_module.chat_json
        self._target = target_model
        self._lock = threading.Lock()
        self.calls: list[dict] = []  # {"ok": bool, "elapsed": float, "error": str|None}

    def _patched(self, system, user, model, temperature=0.2, max_tokens=256, seed=None):
        t0 = time.time()
        try:
            content = self._orig(system, user, self._target, temperature=temperature,
                                 max_tokens=max_tokens, seed=seed)
            with self._lock:
                self.calls.append({"ok": True, "elapsed": time.time() - t0})
            return content
        except Exception as e:  # noqa: BLE001 — 呼び出し元(narrate_*)がインフラ例外として処理する
            with self._lock:
                self.calls.append({"ok": False, "elapsed": time.time() - t0, "error": str(e)[:200]})
            raise

    def __enter__(self):
        self._an.chat_json = self._patched
        return self

    def __exit__(self, *exc):
        self._an.chat_json = self._orig
        return False


# ────────────────────────────────────────────────────────────────
# 入力スナップショット（実データ・既存ローダーをそのまま使う）
# ────────────────────────────────────────────────────────────────
def load_snapshot(data_dir: str, base_date: Optional[str]) -> dict:
    """generate_html.load_and_preprocess（build_dept_reports.py / report_comment_diversity.py
    と同じ本番ローダー）でスナップショットを作る。全モデルへ同一のオブジェクトを渡す。"""
    from generate_html import load_and_preprocess
    adm, surg, targets, surg_targets, profit_monthly, base_date_ts, profit_breakdown = \
        load_and_preprocess(data_dir, base_date, no_validate=False)
    return {
        "adm": adm, "surg": surg, "targets": targets, "surg_targets": surg_targets,
        "profit_monthly": profit_monthly, "base_date": base_date_ts,
        "profit_breakdown": profit_breakdown,
    }


# ────────────────────────────────────────────────────────────────
# 1モデル分の生成・採点
# ────────────────────────────────────────────────────────────────
def run_model(model: str, snapshot: dict, axes: tuple, tables: tuple, verbose: bool) -> dict:
    """同一スナップショットに対し、本番の生成コードパス
    （build_dept_report_contexts + build_hospital_overview_context）をそのまま呼ぶ。
    model だけ ModelSwap で強制する。"""
    from datetime import datetime
    from app.lib.dept_report import build_dept_report_contexts, build_hospital_overview_context

    an = tables[0]
    an.reset_reject_stats()
    generated_at = datetime.now()
    wall_t0 = time.time()
    swap = ModelSwap(an, model)
    try:
        with swap:
            contexts = build_dept_report_contexts(
                snapshot["adm"], snapshot["surg"], snapshot["targets"], snapshot["surg_targets"],
                snapshot["profit_monthly"], snapshot["base_date"], generated_at,
                with_ai=True, axes=axes, quiet=not verbose,
                profit_breakdown=snapshot["profit_breakdown"])
            hosp = build_hospital_overview_context(
                snapshot["adm"], snapshot["surg"], snapshot["targets"], snapshot["surg_targets"],
                snapshot["profit_monthly"], snapshot["base_date"], generated_at,
                profit_breakdown=snapshot["profit_breakdown"], with_ai=True, quiet=not verbose)
    except Exception as e:  # noqa: BLE001 — 1モデルの失敗で他モデルの計測を止めない
        return {"model": model, "error": str(e)[:400]}
    wall_secs = time.time() - wall_t0

    records = [{"axis": c["axis"], "unit": c["unit"], "topic": (c.get("move") or {}).get("topic"),
                "src": (c.get("move") or {}).get("src"),
                "body": (c.get("move") or {}).get("body") or "",
                "action": (c.get("move") or {}).get("action") or ""}
               for c in (contexts + [hosp]) if c.get("move")]

    return _score_records(model, records, dict(an.REJECT_STATS), swap.calls, wall_secs, tables)


def _jaccard_summary(records: list) -> dict:
    """scripts/report_comment_diversity.py の Jaccard 実装をそのまま再利用する
    （実測の連続性を保つため独自定義を作らない）。"""
    import scripts.report_comment_diversity as rcd

    sim_all = rcd._pairwise(records, "body")
    sim_axis = {ax: rcd._pairwise([r for r in records if r["axis"] == ax], "body")
                for ax in ("dept", "ward")}
    dup_body = rcd._dup_groups(records, "body")
    dup_action = rcd._dup_groups(records, "action")
    return {
        "jaccard_body_all": sim_all,
        "jaccard_body_by_axis": sim_axis,
        "dup_body_groups": len(dup_body),
        "dup_body_max_group": len(dup_body[0][1]) if dup_body else 0,
        "dup_body_examples": [[t[:40], us] for t, us in dup_body[:5]],
        "dup_action_groups": len(dup_action),
        "dup_action_max_group": len(dup_action[0][1]) if dup_action else 0,
    }


def _score_records(model: str, records: list, reject_stats: dict, calls: list,
                   wall_secs: float, tables: tuple) -> dict:
    an, banned_by_key, prompt_by_key, hosp_banned, hosp_prompt = tables

    fabrication_hits = 0
    banned_hits = 0
    banned_hit_words: Counter = Counter()
    over_len_hits = 0
    envelope_missing = 0
    range_checked = 0
    range_violations = 0
    range_abs_dev = []
    unmapped = 0
    detail = []

    for r in records:
        axis, topic = r["axis"], r["topic"]
        body, action = r["body"], r["action"]
        if not body or not action:
            envelope_missing += 1
        if axis == "hospital":
            banned, prompt = hosp_banned, hosp_prompt
        else:
            banned, prompt = banned_by_key.get((axis, topic)), prompt_by_key.get((axis, topic))
        if banned is None or prompt is None:
            unmapped += 1
            detail.append({**r, "checked": False})
            continue

        allow = an._unit_allow(r["unit"]) + an._ALLOW_FACT_PHRASES
        text = body + action
        v = _scan_text_violations(an, text, banned, allow)
        if v["digit"]:
            fabrication_hits += 1
        if v["banned"]:
            banned_hits += 1
            banned_hit_words.update(v["banned"])
        if v["over_len"]:
            over_len_hits += 1

        ranges = _extract_schema_ranges(prompt)
        rec_dev = {}
        for key, txt in (("body", body), ("action", action)):
            lohi = ranges.get(key)
            if lohi is None or not txt:
                continue
            lo, hi = lohi
            range_checked += 1
            n = len(txt)
            if n < lo or n > hi:
                range_violations += 1
                dev = (lo - n) if n < lo else (n - hi)
                range_abs_dev.append(dev)
                rec_dev[key] = {"len": n, "range": [lo, hi]}
        detail.append({**r, "checked": True, "digit": v["digit"], "banned": v["banned"],
                       "over_len": v["over_len"], "range_dev": rec_dev})

    jac = _jaccard_summary(records)
    axis_counts = dict(Counter(r["axis"] for r in records))

    n = len(records)
    n_ai = sum(1 for r in records if r["src"] == "ai")
    call_err = [c for c in calls if not c["ok"]]
    elapsed = [c["elapsed"] for c in calls]
    n_ok = reject_stats.get("ok", 0) + reject_stats.get("ok@retry", 0)

    return {
        "model": model,
        "n_records": n, "n_ai": n_ai, "n_tpl": n - n_ai,
        "ai_rate": round(n_ai / n, 3) if n else None,
        "axis_counts": axis_counts,
        "reject_stats": reject_stats,
        "unmapped_topic": unmapped,
        "fabrication": {"final_digit_violations": fabrication_hits,
                        "reject_digit_attempts": reject_stats.get("digit", 0)},
        "banned": {"final_violations": banned_hits, "words": dict(banned_hit_words),
                   "reject_banned_attempts": reject_stats.get("banned", 0)},
        "format": {
            "envelope_missing_fields": envelope_missing,
            "reject_parse_attempts": reject_stats.get("parse", 0),
            "over_400_final": over_len_hits,
            "schema_range_checked": range_checked,
            "schema_range_violations": range_violations,
            "schema_range_violation_rate": (round(range_violations / range_checked, 3)
                                            if range_checked else None),
            "schema_range_mean_abs_dev": (round(statistics.mean(range_abs_dev), 1)
                                          if range_abs_dev else 0.0),
        },
        "homogenization": jac,
        "failures": {
            "call_errors": len(call_err),
            "call_error_samples": [c["error"] for c in call_err[:5]],
            "retry_rate": round(reject_stats.get("ok@retry", 0) / n_ok, 3) if n_ok else None,
        },
        "timing": {
            "wall_secs": round(wall_secs, 1),
            "n_calls": len(calls),
            "median_call_secs": round(statistics.median(elapsed), 2) if elapsed else None,
            "sum_call_secs": round(sum(elapsed), 1) if elapsed else None,
        },
        "records": detail,
    }


# ────────────────────────────────────────────────────────────────
# 表示
# ────────────────────────────────────────────────────────────────
def print_model_report(res: dict) -> None:
    W = 78
    print("\n" + "=" * W)
    if "error" in res:
        print(f"  {res['model']}: 生成失敗 — {res['error']}")
        print("=" * W)
        return
    print(f"  {res['model']}")
    print("=" * W)
    print(f"■ 生成数: {res['n_records']} 件（axis別: {res['axis_counts']}）")
    print(f"  AI採択 {res['n_ai']} / 定型文フォールバック {res['n_tpl']}"
          f"　AI率 {(res['ai_rate'] or 0) * 100:.0f}%")
    print(f"■ 棄却理由内訳（試行単位）: {res['reject_stats']}")
    if res["unmapped_topic"]:
        print(f"  ⚠ 禁止語/書式テーブル未対応の topic: {res['unmapped_topic']} 件（当該レコードは"
              f"数値/禁止語/書式チェックをskip・Jaccardには含む）")

    fab, ban, fmt = res["fabrication"], res["banned"], res["format"]
    print(f"\n■ 数値の捏造: 最終文（AI採択後）に許容外の数字が残存 {fab['final_digit_violations']} 件"
          f"（0が正常＝本番の digit ガード通過後の値）"
          f"　参考: 生成中に digit で棄却された試行 {fab['reject_digit_attempts']} 件")
    print(f"■ 禁止語: 最終文に残存 {ban['final_violations']} 件"
          + (f"　内訳: {ban['words']}" if ban["words"] else "")
          + f"　参考: 生成中に banned で棄却された試行 {ban['reject_banned_attempts']} 件")
    print(f"■ 書式: JSON envelope 不備(body/action欠落) {fmt['envelope_missing_fields']} 件"
          f"　400字超 {fmt['over_400_final']} 件"
          f"　参考: parse失敗の試行 {fmt['reject_parse_attempts']} 件")
    rate = fmt["schema_range_violation_rate"]
    print(f"  スキーマ文字数レンジ逸脱: {fmt['schema_range_violations']}/{fmt['schema_range_checked']}"
          f"（{(rate or 0) * 100:.0f}%・平均逸脱幅 {fmt['schema_range_mean_abs_dev']}字）")

    jac = res["homogenization"]
    sim = jac["jaccard_body_all"]
    print(f"\n■ 均質化（body 3-gram Jaccard・低いほど良い）: 平均 {sim['mean']}"
          f"　高類似(≥0.6)ペア率 {sim['hi_share']}　({sim['pairs']} ペア)")
    for ax, s in jac["jaccard_body_by_axis"].items():
        if s["pairs"]:
            print(f"    {ax:6s} 平均 {s['mean']}　高類似ペア率 {s['hi_share']}　({s['pairs']} ペア)")
    print(f"  一字一句同一の body: {jac['dup_body_groups']} グループ"
          f"（最大 {jac['dup_body_max_group']} 部門）")
    print(f"  一字一句同一の action: {jac['dup_action_groups']} グループ"
          f"（最大 {jac['dup_action_max_group']} 部門）")

    fail, tim = res["failures"], res["timing"]
    print(f"\n■ 生成失敗: chat_json例外 {fail['call_errors']} 件"
          + (f"（例: {fail['call_error_samples'][0]}）" if fail["call_error_samples"] else "")
          + f"　再試行率 {(fail['retry_rate'] or 0) * 100:.0f}%")
    print(f"■ 所要時間（参考・判断基準ではない）: 総壁時計 {tim['wall_secs']}s"
          f"　呼び出し {tim['n_calls']} 回"
          f"　中央値 {tim['median_call_secs']}s　合計 {tim['sum_call_secs']}s")
    print("=" * W)


def print_comparison_table(results: list) -> None:
    print("\n=== まとめ（narrative_ab: モデル比較） ===")
    header = (f"{'model':44} {'AI率':>6} {'捏造':>5} {'禁止語':>6} {'書式逸脱':>8} "
              f"{'Jaccard平均':>10} {'高類似率':>8} {'重複最大':>8} {'失敗':>5} {'中央値/秒':>9}")
    print(header)
    for r in results:
        if "error" in r:
            print(f"{r['model']:44} エラー: {r['error']}")
            continue
        jac = r["homogenization"]["jaccard_body_all"]
        mean = jac["mean"] if jac["mean"] is not None else 0.0
        hi = jac["hi_share"] if jac["hi_share"] is not None else 0.0
        med = r["timing"]["median_call_secs"] or 0.0
        print(f"{r['model']:44} "
              f"{(r['ai_rate'] or 0) * 100:5.0f}% "
              f"{r['fabrication']['final_digit_violations']:>5} "
              f"{r['banned']['final_violations']:>6} "
              f"{r['format']['schema_range_violations']:>8} "
              f"{mean:>10.3f} "
              f"{hi:>8.3f} "
              f"{r['homogenization']['dup_body_max_group']:>8} "
              f"{r['failures']['call_errors']:>5} "
              f"{med:>8.2f}s")
    print("\n※ 捏造/禁止語/書式逸脱/重複最大 は低いほど良い。Jaccard平均・高類似率 は低いほど"
          "「各部門固有の文になっている」＝良い（過去実測の実例: 0.425→0.142 が改善）。"
          "\n※ 所要時間は参考値（速度は今回の判断基準ではない）。")


# ────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────
def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--models", default=",".join(DEFAULT_MODELS),
                   help=f"カンマ区切りのモデルID一覧（既定: 切替前={OLD_MODEL} / "
                        f"現行(model_override.json)={CURRENT_MODEL}）")
    p.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    p.add_argument("--base-date", default=None, help="基準日 YYYY-MM-DD（既定: データの最新日）")
    p.add_argument("--axes", default="dept,ward", help="dept,ward のいずれか/両方（既定: 両方）")
    p.add_argument("--out", default=None, help="結果JSONの保存先（前回結果との差分比較用）")
    p.add_argument("--verbose", action="store_true", help="生成中の [AI] ✓/— ログも表示する")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    axes = tuple(a.strip() for a in args.axes.split(",") if a.strip() in ("dept", "ward"))
    if not axes:
        print("axes は dept / ward を指定してください", file=sys.stderr)
        return 1

    tables = _build_topic_tables()

    print(f"スナップショット読込中… data_dir={args.data_dir} base_date={args.base_date or '(最新)'}")
    snapshot = load_snapshot(args.data_dir, args.base_date)
    print(f"基準日: {snapshot['base_date']:%Y-%m-%d}　axes={axes}　モデル {len(models)} 種を計測します"
          "（同一スナップショットを全モデルへ使用）。")

    results = []
    for m in models:
        print(f"\n### {m} を生成中…", flush=True)
        res = run_model(m, snapshot, axes, tables, args.verbose)
        results.append(res)
        print_model_report(res)

    print_comparison_table(results)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "base_date": f"{snapshot['base_date']:%Y-%m-%d}",
            "axes": list(axes),
            "models": models,
            "results": results,
        }
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                            encoding="utf-8")
        print(f"\n結果を保存: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
