"""fewshot — 人手添削(override)の蒸留を「今週の一手」プロンプトへ few-shot 注入する(P3)。

設計原則:
    - override＝人の承認そのもの。既に人が最終稿として確定させた文なので、
      別途の採否ループ（LLM審査等）は設けない。report_feedback.pair_corrections が
      復元した ai→manual ペアをそのまま学習素材として使う。
    - 事実は絶対に流用しない（スタイル・言い回し・視点のみを真似る）。本文の具体的な
      事実（数値・祝日/連休・診療科名）は月次で陳腐化し、他ユニットへの取り違えは
      ハルシネーションになるため、注入前に診療科名をマスキングし、呼び出し側が渡した
      banned（未提供事実の語）を含む例は除外する。
    - 無効化時・コーパス無し・該当例無しは必ず空文字列を返す（呼び出し側の
      プロンプトをバイト不変に保つ＝キャッシュキー・決定論seedへの影響ゼロ）。

コーパスは report_feedback.load_edits() / pair_corrections() が復元したペアを
1行1件で state_dir/fewshot_corpus.jsonl へ再構築する（毎回全再構築・冪等）。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from .report_feedback import load_edits, pair_corrections

logger = logging.getLogger(__name__)

CORPUS_NAME = "fewshot_corpus.jsonl"

# state_dir 省略時の既定（build 側の呼び出し規模を抑えるため、ai_narrative.py からは
# state_dir を渡さずこの既定へフォールバックする）。app/lib/fewshot.py → Dashboard/。
DEFAULT_STATE_DIR = Path(__file__).resolve().parents[2] / "dept_reports" / "_state"

# トピック→facts の水準トークンキー（capture_edits の _state 語彙は na/surg/ret/… で、
# topic 名(admission/surgery/leveling)とは一致しない。leveling は token 常に None なので
# 未使用だが対応関係として明記しておく）。
_TOPIC_FACT_KEY = {"admission": "na", "surgery": "surg", "leveling": "ret"}

# _q_target_gap の5段階（上＝良い順）。隣接階級ほどスコアを近づける。
_TIER_ORDER = ("exceed", "met", "close", "mild", "poor")
_TIER_IDX = {t: i for i, t in enumerate(_TIER_ORDER)}

# 「状態文 → facts語彙トークン」の包含判定表。_q_target_gap_trend が付ける傾向節
# （「〜が、直近は…」等）を含む文にも部分一致で当たる。
_STATE_TOKEN_PATTERNS = (
    ("大きく上回", "exceed"),
    ("達成", "met"),
    ("わずか", "close"),
    ("やや", "mild"),
    ("明確に下回", "poor"),
)


def _enabled() -> bool:
    return os.environ.get("GENIE_FEWSHOT", "1") != "0"


def _default_k() -> int:
    try:
        return int(os.environ.get("GENIE_FEWSHOT_K", "2"))
    except (TypeError, ValueError):
        return 2


def _iter_jsonl(path: Path):
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
# コーパス構築（report_feedback のペアリングをそのまま再利用）
# ════════════════════════════════════════════════════════════
def rebuild_corpus(state_dir) -> int:
    """_state/edits_*.jsonl から ai→manual 添削ペアを集め、state_dir/fewshot_corpus.jsonl
    へ全再構築する（毎回上書き・冪等）。ペアリングは report_feedback.pair_corrections を
    そのまま使う（自作しない）。書いた件数を返す。fail-soft（例外時は0返し・既存コーパス温存）。
    """
    try:
        state_dir = Path(state_dir)
        records = load_edits(state_dir)
        pairs = pair_corrections(records)
        if not pairs:
            return 0

        # facts は pair_corrections の戻り値に含まれない（body/action の突き合わせのみ）ため、
        # 同じ (base_date,axis,unit) グルーピングで「最初の ai/tpl レコード」の facts を
        # 別途引く（pair_corrections の before 選定＝最初の ai/tpl と同じ規則）。
        groups: dict = {}
        for r in records:
            key = (r.get("base_date"), r.get("axis"), r.get("unit"))
            groups.setdefault(key, []).append(r)
        facts_by_key: dict = {}
        for key, rs in groups.items():
            before = next((r for r in rs if r.get("src") in ("ai", "tpl")), None)
            manual_last = None
            for r in rs:
                if r.get("src") == "manual":
                    manual_last = r
            src_r = before or manual_last
            facts_by_key[key] = (src_r or {}).get("facts") or {}

        rows_by_key: dict = {}
        for p in pairs:
            key = (p["date"], p["axis"], p["unit"], p["topic"])
            rows_by_key[key] = {
                "base_date": p["date"], "axis": p["axis"], "unit": p["unit"],
                "topic": p["topic"],
                "facts": facts_by_key.get((p["date"], p["axis"], p["unit"]), {}),
                "ai_body": p["ai_body"], "ai_action": p["ai_action"],
                "human_body": p["human_body"], "human_action": p["human_action"],
                "fields": p["changed"],
            }   # 同キー再出現時は上書き（最後勝ち）
        rows = list(rows_by_key.values())

        # 同一トピック内で human_action が完全一致する行が複数あれば、base_date が
        # 最新の1件だけ残す（同着はファイル後方＝後勝ち）。人手オーバーライドは
        # expires=基準日+14で運用されるため、同一の添削文が最大14日分 manual として
        # 再捕捉され、実質は少数ユニットの定型文がコーパスを水増しする自己強化ループ
        # になっていた。human_action が空文字の行は判定不能として重複排除の対象外
        # とする（空文字同士を同一視して間引かない）。
        dedup: dict = {}
        empties = []
        for row in rows:
            action = row.get("human_action") or ""
            if not action:
                empties.append(row)
                continue
            key = (row.get("topic"), action)
            prev = dedup.get(key)
            if prev is None or (row.get("base_date") or "") >= (prev.get("base_date") or ""):
                dedup[key] = row
        rows = list(dedup.values()) + empties

        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / CORPUS_NAME
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return len(rows)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"few-shot コーパス再構築に失敗（無視して既存コーパスを維持）: {e}")
        return 0


_CORPUS_CACHE: dict = {}   # path文字列 → (mtime, rows)


def load_corpus(state_dir) -> list:
    """state_dir/fewshot_corpus.jsonl を読む。無ければ []。
    プロセス内キャッシュ（path+mtime）で毎ユニット読み直しを避ける。"""
    path = Path(state_dir) / CORPUS_NAME
    if not path.exists():
        return []
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    key = str(path)
    cached = _CORPUS_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    rows = list(_iter_jsonl(path))
    _CORPUS_CACHE[key] = (mtime, rows)
    return rows


# ════════════════════════════════════════════════════════════
# 状態文 → facts語彙トークン
# ════════════════════════════════════════════════════════════
def state_token(state_sentence: str) -> Optional[str]:
    """_q_target_gap_trend系の日本語文（傾向節を含みうる）を facts語彙トークン
    （exceed/met/close/mild/poor）へ写す。どれにも当たらなければ None。"""
    if not state_sentence:
        return None
    for pat, tok in _STATE_TOKEN_PATTERNS:
        if pat in state_sentence:
            return tok
    return None


# ════════════════════════════════════════════════════════════
# 診療科名マスキング（evaluation_rules.yaml の dept_group_rules 配下 depts を結合）
# ════════════════════════════════════════════════════════════
_RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "evaluation_rules.yaml"
_rules_cache: Optional[dict] = None


def _load_eval_rules_yaml() -> dict:
    global _rules_cache
    if _rules_cache is not None:
        return _rules_cache
    if not _RULES_PATH.exists():
        _rules_cache = {}
        return _rules_cache
    try:
        import yaml
        raw = yaml.safe_load(_RULES_PATH.read_text(encoding="utf-8"))
        _rules_cache = raw if isinstance(raw, dict) else {}
    except Exception as e:  # fail-soft
        logger.warning(f"evaluation_rules.yaml 読込失敗（マスキングはコーパス由来名のみ）: {e}")
        _rules_cache = {}
    return _rules_cache


def _known_dept_names(corpus: list) -> set:
    names = {r.get("unit") for r in corpus if r.get("unit")}
    try:
        cfg = _load_eval_rules_yaml()
        for group in (cfg.get("dept_group_rules") or {}).values():
            if isinstance(group, dict):
                for d in (group.get("depts") or []):
                    if d:
                        names.add(d)
    except Exception:
        pass
    return names


def _mask(text: Optional[str], self_name: str, known_names: set) -> str:
    """例文中の診療科名を置換する（固有情報の二次防御）。自分自身→「当科」、
    それ以外の既知科名→「他科」。"""
    result = text or ""
    if not result:
        return result
    if self_name and self_name in result:
        result = result.replace(self_name, "当科")
    for name in sorted((n for n in known_names if n and n != self_name), key=len, reverse=True):
        if name in result:
            result = result.replace(name, "他科")
    return result


def _char_trigrams(s: Optional[str]) -> set:
    """文字3-gram集合。3文字未満は文字列全体を1要素として扱う（空文字なら空集合）。"""
    s = s or ""
    if len(s) < 3:
        return {s} if s else set()
    return {s[i:i + 3] for i in range(len(s) - 2)}


def _trigram_jaccard(a: Optional[str], b: Optional[str]) -> float:
    """文字3-gram Jaccard類似度（0=語彙が重ならない〜1=完全一致）。examples_blockの
    2件目選択（MMR的多様性選択）用の小さなヘルパ。どちらかが空/計算不能なら0.0。"""
    sa, sb = _char_trigrams(a), _char_trigrams(b)
    if not sa or not sb:
        return 0.0
    union = len(sa | sb)
    if not union:
        return 0.0
    return len(sa & sb) / union


# ════════════════════════════════════════════════════════════
# few-shot ブロック整形
# ════════════════════════════════════════════════════════════
# ヘッダに禁止語（祝日/連休/前年/前回）の literal を書かないこと。
# _generate_checked が検査するのは出力側だが、プロンプトに当該語があるとモデルを誘発し
# 棄却→リトライ→定型文フォールバックを招く（実測で確認したため「時期の事情」と抽象化した）。
_BLOCK_HEADER = ("【過去に人が添削した例（言い回しと視点だけを参考にする。"
                "例に出てくる数値・時期の事情・固有名詞は一切流用せず、"
                "必ず上記の事実のみに基づいて書く）】")


def examples_block(topic: str, token: Optional[str], banned: tuple, unit_name: str,
                   state_dir=None, k: Optional[int] = None) -> str:
    """topic/token に合う添削例を最大k件、プロンプト追記用ブロックとして整形する。

    無効化時・コーパス無し・該当例無しは必ず ""（呼び出し側プロンプトをバイト不変に保つ）。
    fail-soft: 途中で何が起きても "" を返す（生成本体を壊さない）。
    """
    if not _enabled():
        return ""
    try:
        sdir = Path(state_dir) if state_dir is not None else DEFAULT_STATE_DIR
        corpus = load_corpus(sdir)
        if not corpus:
            return ""
        k = k if k is not None else _default_k()

        # ① dept軸・同topicのみ（自ユニット自身の過去添削は今週の一手には使わない＝
        # 同時期の実事実が近く残留リークの懸念があるうえ自己参照は例として不自然）
        cands = [r for r in corpus if r.get("axis") == "dept" and r.get("topic") == topic
                and r.get("unit") != unit_name]
        if not cands:
            return ""

        # ② banned フィルタ（祝日リーク等の一次防御）。
        # 例を丸ごと捨てず**フィールド単位**で判定する: 対象ユニットに渡していない事実の語
        # （祝日・前年・前回）が body 側にだけ在るなら、action ペアだけを例として使う。
        # 祝日週に採取したコーパスは大半の body が「祝日」を含むため、丸ごと除外すると
        # 非祝日週に一切効かなくなる（実測: admission/surgery が選択0件になった）。
        # 安全性は落ちない（プロンプトに載る文字列そのものを検査しているため）。
        banned = tuple(b for b in (banned or ()) if b)

        def _clean(*vals) -> bool:
            t = "".join(str(v or "") for v in vals)
            return not any(b in t for b in banned)

        scored = []
        for r in cands:
            usable = []
            if _clean(r.get("ai_body"), r.get("human_body")):
                usable.append("body")
            if _clean(r.get("ai_action"), r.get("human_action")):
                usable.append("action")
            if not usable:
                continue          # 両フィールドとも禁止語を含む＝この例は使わない
            # 「人が実際に直した箇所」かつ「禁止語を含まない」フィールドだけを載せる。
            # 積集合が空なら例ごと捨てる（フォールバックで無変更ペアを載せてはいけない:
            # AI案と添削後が同一の例は何も教えず、既存の文体を強化してしまう）。
            orig = r.get("fields") or ["body", "action"]
            keep = [f for f in usable if f in orig]
            if not keep:
                continue
            scored.append({**r, "_usable_fields": keep})
        cands = scored
        if not cands:
            return ""

        # ③ スコア: topicトークン一致を最優先、次いで隣接階級。token=None(leveling)は
        # topic一致のみでk=1に制限。
        if token is None:
            k = 1
        fact_key = _TOPIC_FACT_KEY.get(topic, topic)

        def _score(r):
            if token is None:
                return 0
            cand_tok = (r.get("facts") or {}).get(fact_key)
            if cand_tok == token:
                return 0
            if cand_tok in _TIER_IDX and token in _TIER_IDX:
                return abs(_TIER_IDX[cand_tok] - _TIER_IDX[token])
            return len(_TIER_ORDER)

        # 同点は base_date 降順（先に日付降順で並べ、スコアで安定ソート）
        cands.sort(key=lambda r: r.get("base_date") or "", reverse=True)
        cands.sort(key=_score)

        # 同ユニットは1件まで（多様性）。1件目は現行のまま（token一致→隣接階級→
        # base_date降順の先頭）。2件目は候補プール（1件目のユニットを除く）の中から
        # 「1件目のhuman_actionと文字3-gram Jaccard類似度が最小」のものを選ぶ
        # （MMR的多様性選択）。人手添削コーパスは同型の定型文が多く、単純に上位2件を
        # 採ると同型の例が並びやすいため。候補が1件しかない/類似度が計算不能（同率）
        # なら remaining の先頭が選ばれ、現行どおりの並び順選択にフォールバックする。
        # 3件目以降（現状は呼ばれない）は元の並び順選択にフォールバックする。
        seen_units, selected = set(), []
        for r in cands:
            u = r.get("unit")
            if u in seen_units:
                continue
            seen_units.add(u)
            selected.append(r)
            break

        if selected and k >= 2:
            base_action = selected[0].get("human_action") or ""
            remaining = [r for r in cands if r.get("unit") not in seen_units]
            if remaining:
                pick = min(remaining, key=lambda r: _trigram_jaccard(
                    base_action, r.get("human_action") or ""))
                seen_units.add(pick.get("unit"))
                selected.append(pick)

        for r in cands:
            if len(selected) >= k:
                break
            u = r.get("unit")
            if u in seen_units:
                continue
            seen_units.add(u)
            selected.append(r)

        if not selected:
            return ""

        known_names = _known_dept_names(corpus)
        lines = []
        for i, r in enumerate(selected, start=1):
            self_name = r.get("unit") or ""
            # ②で禁止語を含まないと確認できたフィールドのみを載せる
            fields = r.get("_usable_fields") or r.get("fields") or ["body", "action"]
            ai_parts, human_parts = [], []
            if "body" in fields:
                ai_parts.append(_mask(r.get("ai_body"), self_name, known_names))
                human_parts.append(_mask(r.get("human_body"), self_name, known_names))
            if "action" in fields:
                ai_parts.append(_mask(r.get("ai_action"), self_name, known_names))
                human_parts.append(_mask(r.get("human_action"), self_name, known_names))
            if not ai_parts:
                ai_parts = [_mask(r.get("ai_body"), self_name, known_names),
                           _mask(r.get("ai_action"), self_name, known_names)]
                human_parts = [_mask(r.get("human_body"), self_name, known_names),
                              _mask(r.get("human_action"), self_name, known_names)]
            ai_line = " / ".join(p for p in ai_parts if p)
            human_line = " / ".join(p for p in human_parts if p)
            lines.append(f"例{i} AI案: {ai_line}")
            lines.append(f"例{i} 添削後: {human_line}")
        if not lines:
            return ""
        return "\n\n" + _BLOCK_HEADER + "\n" + "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"few-shot 例文組み立てに失敗（無視して例文なしで続行）: {e}")
        return ""
