# -*- coding: utf-8 -*-
"""report_overrides — 部門レポート「一手」の人手オーバーライド（overrides.md）。

レビューHTML（またはテキストエディタ）で書かれた dept_reports/overrides.md を読み、
該当部門の move（body/action）をビルド時に差し替える。バックログ §6-1。

ファイル形式（素朴なテキスト・YAML不採用＝日本語本文のコロン/インデント事故を排除）:

    # コメント行（#始まり）
    [診療科:整形外科] expires:2026-07-16
    body: 差し替え本文（1行・省略可）
    action: 差し替え一手（1行・省略可）

    [病棟:9階B病棟] expires:2026-07-16
    action: 片方だけの差し替えも可

ルール:
- expires を過ぎたブロックは自動で無視（古い文の残留防止）。
- 壊れたブロックはそのブロックだけスキップして警告（fail-soft・ビルドは止めない）。
- 同一部門が複数回現れたら後勝ち（手編集で末尾に追記した修正を優先）＋警告。
- expires 未指定は受理するが「無期限」警告（レビューUIは必ず expires を書く）。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

# ファイル上の軸ラベル（人間が書く）→ 内部軸キー
AXIS_LABELS = {"診療科": "dept", "病棟": "ward"}
AXIS_JP = {v: k for k, v in AXIS_LABELS.items()}

# 既定の有効期限 = 基準日 + 14日（レビューUI・save_overrides_header と共有）
DEFAULT_EXPIRES_DAYS = 14

_HEADER_RE = re.compile(
    r"^\[(?P<axis>診療科|病棟):(?P<unit>[^\]]+)\]\s*(?:expires:(?P<exp>\S+))?\s*$")
_FIELD_RE = re.compile(r"^(?P<kind>body|action):\s*(?P<text>.*)$")


def parse_overrides(path, base_date) -> tuple[dict, list]:
    """overrides.md を読み ((axis, unit) -> {"body","action","expires"}) と注記を返す。

    - 戻り値の dict は有効（期限内・本文あり）なブロックのみ。
    - notes は [(level, msg)] のリスト。level は "info"（期限切れ等の正常動作）
      / "warn"（壊れたブロック等・fail-soft でスキップした事実）。
    - ファイルが無ければ ({}, [])。例外は投げない（ビルドを止めない）。
    """
    path = Path(path)
    if not path.is_file():
        return {}, []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as e:  # 読めない場合も fail-soft
        return {}, [("warn", f"overrides.md を読めません（無視して続行）: {e}")]

    base = pd.Timestamp(base_date).normalize()
    out: dict = {}
    notes: list = []
    cur_key = None          # (axis, unit)
    cur: Optional[dict] = None
    cur_line = 0            # ヘッダ行番号（警告表示用）

    def _flush():
        nonlocal cur_key, cur
        if cur_key is None:
            return
        if not (cur.get("body") or cur.get("action")):
            notes.append(("warn", f"{cur_line}行目 [{_label(cur_key)}]: "
                          "body/action がありません（スキップ）"))
        else:
            exp = cur.get("expires")
            if exp is not None and exp < base:
                notes.append(("info", f"[{_label(cur_key)}] expires:"
                              f"{exp.strftime('%Y-%m-%d')} は期限切れ（無視）"))
            else:
                if exp is None:
                    notes.append(("warn", f"{cur_line}行目 [{_label(cur_key)}]: "
                                  "expires 未指定（無期限扱い）"))
                if cur_key in out:
                    notes.append(("warn", f"[{_label(cur_key)}] が複数あります（後勝ち）"))
                out[cur_key] = cur
        cur_key, cur = None, None

    for i, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _HEADER_RE.match(line)
        if m:
            _flush()
            exp_s = m.group("exp")
            exp = None
            if exp_s:
                try:
                    exp = pd.Timestamp(datetime.strptime(exp_s, "%Y-%m-%d"))
                except ValueError:
                    notes.append(("warn", f"{i}行目: expires:{exp_s} が不正な日付"
                                  "（ブロックをスキップ）"))
                    continue
            cur_key = (AXIS_LABELS[m.group("axis")], m.group("unit").strip())
            cur = {"body": None, "action": None, "expires": exp}
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
    return out, notes


def _label(key: tuple) -> str:
    return f"{AXIS_JP.get(key[0], key[0])}:{key[1]}"


def is_full_override(ov: Optional[dict]) -> bool:
    """body・action の両方を差し替えるか（→該当部門のAI生成をスキップできる）。

    片方だけの差し替えでは AI 生成を止めない: 決定論seed（§0-e）で再ビルドは
    レビュー時と同じAI文を再現するため、「見て承認した文＋自分の修正」が成立する。
    スキップして定型文に落とすと、直していない側がレビューで見た文と変わってしまう。
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
