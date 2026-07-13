"""moves_store.py — 一手スナップショット(moves_{date}.json)の読み出し（stdlibのみ）

build_dept_reports.py がフルビルド時に保存する dept_reports/_state/moves_{基準日}.json
（部門別レポートの「この期間の一手」の確定値・オーバーライド適用後）を、公開HTML側
（html_builder.py / build_selfcontained.py）から共通の選定ロジックで読み出す。

stdlib のみに依存（pandas/plotly非依存）。理由: 軽量後処理の build_selfcontained.py
（§8-S2）と選定ロジックを単一の実装で共有するため。
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional

MOVES_MAX_AGE_DAYS = 45   # これより古い一手は載せない（古い助言の残留防止）
MOVE_PUBLIC_KEYS = ("body", "action", "surg_line", "util_line", "nadm_line")  # 公開してよいキー

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_STATE_DIR = _REPO_ROOT / "dept_reports" / "_state"

_FILE_RE = re.compile(r"^moves_(\d{4}-\d{2}-\d{2})\.json$")


def _as_date(v) -> date:
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if hasattr(v, "strftime"):
        v = v.strftime("%Y-%m-%d")
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def load_latest_moves(base_date, state_dir=None, max_age_days=MOVES_MAX_AGE_DAYS) -> Optional[dict]:
    """dept_reports/_state/moves_*.json の最新（base_date以下・45日以内）を読む。無ければNone。

    ファイル名から日付を取り base_date 以下の最大を選ぶ → 年齢チェック → json.loads。
    壊れたJSONはスキップ。例外は外に投げない（呼び出し側は None 縮退のみ）。
    """
    try:
        bd = _as_date(base_date)
        p = Path(state_dir) if state_dir is not None else _DEFAULT_STATE_DIR
        if not p.is_dir():
            return None
        best_date = None
        best_path = None
        for f in p.glob("moves_*.json"):
            m = _FILE_RE.match(f.name)
            if not m:
                continue
            try:
                d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            except ValueError:
                continue
            if d > bd or (bd - d).days > max_age_days:
                continue
            if best_date is None or d > best_date:
                best_date, best_path = d, f
        if best_path is None:
            return None
        return json.loads(best_path.read_text(encoding="utf-8"))
    except Exception:
        return None
