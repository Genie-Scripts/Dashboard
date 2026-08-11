# -*- coding: utf-8 -*-
"""report_overrides — 部門レポート「一手」の人手オーバーライド（overrides.md）。

レビューHTML（またはテキストエディタ）で書かれた dept_reports/overrides.md を読み、
該当部門の move（body/action）をビルド時に差し替える。バックログ §6-1。

ファイル形式（素朴なテキスト・YAML不採用＝日本語本文のコロン/インデント事故を排除）:

    # コメント行（#始まり）
    [診療科:整形外科] base:2026-08-11 expires:2026-08-25
    body: 差し替え本文（1行・省略可）
    action: 差し替え一手（1行・省略可）

    [病棟:9階B病棟] base:2026-08-11 expires:2026-08-25
    action: 片方だけの差し替えも可

属性（`[軸:ユニット]` の後ろに空白区切り、順序は不問）:
- `base`  = この添削が書かれたレポートの基準日。**base が今回ビルドの基準日と一致する
  ブロックだけ「有効(active)」として適用される**。ビルドをまたぐと（新しい基準日になると）
  古い base のブロックは自動的に非適用になる＝毎ビルドはAI文が既定になる。
- `expires` = 「前回の添削を再利用候補（carry）として保持する期限」（既定 base+14日）。
  expires を過ぎたブロックは active・carry いずれにも入らない（自動で無視）。
- `base` の無いブロック（旧形式）は適用しない。carry（前回の添削）として保持するだけ。
- 未知の属性は警告して無視する（fail-soft）。

ルール:
- 壊れたブロックはそのブロックだけスキップして警告（fail-soft・ビルドは止めない）。
- base が今回のビルドと一致するブロックが同一部門で複数回現れたら後勝ち（手編集で末尾に
  追記した修正を優先）＋警告。
- carry 候補（base 不一致・base 無し・base 不正のいずれか）が同一部門に複数あれば base が
  新しい方を残す（base 無しは最古扱い、同着はファイル後方が勝ち）。active があるキーは
  carry から除外する（世代交代でファイルを有界に保つ）。
- expires 未指定は受理するが「無期限」警告（レビューUIは必ず expires を書く）。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

# ファイル上の軸ラベル（人間が書く）→ 内部軸キー
# 「病院全体」は病院全体サマリシートの一手（unit＝病院名 or "病院全体"）に対応。
AXIS_LABELS = {"診療科": "dept", "病棟": "ward", "病院全体": "hospital"}
AXIS_JP = {v: k for k, v in AXIS_LABELS.items()}

# 既定の有効期限 = 基準日 + 14日（レビューUI・save_overrides_header と共有）
DEFAULT_EXPIRES_DAYS = 14

_HEADER_RE = re.compile(
    r"^\[(?P<axis>診療科|病棟|病院全体):(?P<unit>[^\]]+)\]"
    r"(?P<attrs>(?:\s+[A-Za-z_]+:\S+)*)\s*$")
_FIELD_RE = re.compile(r"^(?P<kind>body|action):\s*(?P<text>.*)$")

_KNOWN_ATTRS = {"base", "expires"}


def parse_overrides(path, base_date) -> tuple[dict, dict, list]:
    """overrides.md を読み、(active, carry, notes) を返す。

    - active: {(axis, unit) -> {"body","action","expires","base"}}。base が今回の
      base_date と一致し、未失効で、body/action のいずれかがあるブロックのみ。
    - carry: 同形式。未失効だが active でないブロック（base 不一致／無し／不正）。
      同一キーが複数あれば base が新しい方を残す（base 無しは最古扱い、同着はファイル
      後方が勝ち）。active があるキーは carry から除外する。
    - notes は [(level, msg)] のリスト。level は "info"（期限切れ・旧形式等の正常動作）
      / "warn"（壊れたブロック等・fail-soft でスキップ／保持した事実）。
    - ファイルが無ければ ({}, {}, [])。例外は投げない（ビルドを止めない）。
    """
    path = Path(path)
    if not path.is_file():
        return {}, {}, []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as e:  # 読めない場合も fail-soft
        return {}, {}, [("warn", f"overrides.md を読めません（無視して続行）: {e}")]

    base_ts = pd.Timestamp(base_date).normalize()
    active: dict = {}
    carry_candidates: dict = {}
    notes: list = []
    cur_key = None          # (axis, unit)
    cur: Optional[dict] = None
    cur_line = 0            # ヘッダ行番号（警告表示用）
    cur_base_present = False   # base: 属性がそもそも書かれていたか（旧形式判定用）

    def _carry_rank(blk: dict):
        b = blk.get("base")
        return b if b is not None else pd.Timestamp.min

    def _flush():
        nonlocal cur_key, cur, cur_base_present
        if cur_key is None:
            return
        if not (cur.get("body") or cur.get("action")):
            notes.append(("warn", f"{cur_line}行目 [{_label(cur_key)}]: "
                          "body/action がありません（スキップ）"))
            cur_key, cur = None, None
            return
        exp = cur.get("expires")
        if exp is not None and exp < base_ts:
            notes.append(("info", f"[{_label(cur_key)}] expires:"
                          f"{exp.strftime('%Y-%m-%d')} は期限切れ（無視）"))
            cur_key, cur = None, None
            return
        if exp is None:
            notes.append(("warn", f"{cur_line}行目 [{_label(cur_key)}]: "
                          "expires 未指定（無期限扱い）"))
        blk_base = cur.get("base")
        if blk_base is None and not cur_base_present:
            notes.append(("info", f"[{_label(cur_key)}] は旧形式（base 無し）＝"
                          "前回の添削として保持（今回は適用しません）"))
        if blk_base is not None and blk_base == base_ts:
            if cur_key in active:
                notes.append(("warn", f"[{_label(cur_key)}] が複数あります（後勝ち）"))
            active[cur_key] = cur
        else:
            prev = carry_candidates.get(cur_key)
            if prev is None or _carry_rank(cur) >= _carry_rank(prev):
                carry_candidates[cur_key] = cur
        cur_key, cur = None, None

    for i, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _HEADER_RE.match(line)
        if m:
            _flush()
            attrs_str = (m.group("attrs") or "").strip()
            exp = None
            blk_base = None
            base_present = False
            skip_block = False
            for tok in attrs_str.split():
                k, _sep, v = tok.partition(":")
                if k == "expires":
                    try:
                        exp = pd.Timestamp(datetime.strptime(v, "%Y-%m-%d"))
                    except ValueError:
                        notes.append(("warn", f"{i}行目: expires:{v} が不正な日付"
                                      "（ブロックをスキップ）"))
                        skip_block = True
                elif k == "base":
                    base_present = True
                    try:
                        blk_base = pd.Timestamp(
                            datetime.strptime(v, "%Y-%m-%d")).normalize()
                    except ValueError:
                        notes.append(("warn", f"{i}行目: base:{v} が不正な日付"
                                      "（前回の添削として保持）"))
                        blk_base = None
                elif k in _KNOWN_ATTRS:
                    pass
                else:
                    notes.append(("warn", f"{i}行目: 未知の属性 {tok} を無視"))
            if skip_block:
                cur_key = None
                continue
            cur_key = (AXIS_LABELS[m.group("axis")], m.group("unit").strip())
            cur = {"body": None, "action": None, "expires": exp, "base": blk_base}
            cur_base_present = base_present
            cur_line = i
            continue
        f = _FIELD_RE.match(line)
        if f:
            if cur_key is None:
                notes.append(("warn", f"{i}行目: ヘッダ行 [診療科:◯◯] の無い "
                              f"{f.group('kind')}: 行（無視）"))
                continue
            text = f.group("text").strip()
            if text:
                cur[f.group("kind")] = text
            continue
        notes.append(("warn", f"{i}行目: 解釈できない行（無視）: {line[:30]}"))
    _flush()
    carry = {k: v for k, v in carry_candidates.items() if k not in active}
    return active, carry, notes


def carry_payload(carry: dict) -> dict:
    """carry（parse_overrides の戻り値）をレビューUI埋め込み用の dict に変換する。

    キーは "診療科:整形外科" 形式（AXIS_JP[axis] + ":" + unit）。値は
    {"base","expires","body","action"} で、日付は "YYYY-MM-DD" 文字列（無ければ
    None）にする（そのまま JSON へ）。body/action の片側だけの添削は None のまま残す。
    """
    out: dict = {}
    for (axis, unit), blk in carry.items():
        label = f"{AXIS_JP.get(axis, axis)}:{unit}"
        base = blk.get("base")
        exp = blk.get("expires")
        out[label] = {
            "base": base.strftime("%Y-%m-%d") if base is not None else None,
            "expires": exp.strftime("%Y-%m-%d") if exp is not None else None,
            "body": blk.get("body"),
            "action": blk.get("action"),
        }
    return out


def _label(key: tuple) -> str:
    return f"{AXIS_JP.get(key[0], key[0])}:{key[1]}"


def is_full_override(ov: Optional[dict]) -> bool:
    """body・action の両方を差し替えるか（→該当部門のAI生成をスキップできる）。

    片方だけの差し替えでは AI 生成を止めない: 決定論seed（§0-e）で再ビルドは
    レビュー時と同じAI文を再現するため、「見て承認した文＋自分の修正」が成立する。
    スキップして定型文に落とすと、直していない側がレビューで見た文と変わってしまう。

    現在は生成スキップに使っていない（履歴的API・テスト互換のため残置）。
    """
    return bool(ov and ov.get("body") and ov.get("action"))


def apply_override(move: dict, ov: dict) -> dict:
    """move 確定直後の1箇所で呼ぶ。body/action を差し替え src="manual" 刻印。

    数値行（surg_line/util_line/nadm_line）・topic 等はデータ由来のため保持。
    ov_fields はレビューHTMLが「どちらが手動か」を表示・再保存するのに使う。
    """
    out = {**move, "src": "manual",
           "ov_fields": [k for k in ("body", "action") if ov.get(k)]}
    for k in ("body", "action"):
        if ov.get(k):
            out[k] = ov[k]
    return out


def default_expires(base_date) -> str:
    """レビューUIに埋め込む既定 expires（基準日+14日）を YYYY-MM-DD で返す。"""
    return (pd.Timestamp(base_date) + timedelta(days=DEFAULT_EXPIRES_DAYS)).strftime("%Y-%m-%d")
