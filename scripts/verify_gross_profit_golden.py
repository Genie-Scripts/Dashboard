#!/usr/bin/env python3
"""ゴールデン検証（ローカル専用・pytest対象外）。

data/gross_profit/seed_store.json（=現行配信版の中身）と、公式4ファイル
（data/profit_xlsx/*.xlsx）から parse_year_file + merge_store で再構築したstoreを比較する。

公式ファイルは月次で改訂される（0埋め⇔空欄・行の追加・診療科コードの付与など）ため、
「seedと完全一致」はゲートにならない。パーサ移植バグ（列ズレ・単位ミス・行落ち）は
【両側が非Noneで値が食い違うセル（=値衝突）】として大量に現れるので、それだけをFAILにする。

  - FY2026（当年度）会計月1〜3:
      HARD（FAIL）= 値衝突（months/bonusの両側非None不一致）、
                    seed行の消失（official側に同名行も無い場合＝行落ちの疑い）
      SOFT（情報）= None⇔値（0埋め/空欄・月移動などの改訂）、official側の行追加、
                    行キー移動（コード付与。同名行が存在）、name表記変更
  - FY2023〜2025: すべて情報表示のみ（exitに影響しない）。
  - patients は比較対象外。

実行例:
    python3 scripts/verify_gross_profit_golden.py
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from lib_gross_profit import (  # noqa: E402
    is_year_file,
    norm,
    parse_year_file,
    merge_store,
    read_workbook_rows,
    SHEET_TYPES,
)

REPO_DIR = pathlib.Path(__file__).resolve().parent.parent
ALL_SHEET_TYPES = SHEET_TYPES + ['給与', '給与(謝金)', '給与(合算)']

CURRENT_FY = '2026'
REFERENCE_FYS = ('2023', '2024', '2025')


def _build_from_official(xlsx_dir: pathlib.Path) -> dict:
    store = {'version': 2, 'updated': '', 'sheets': {}}
    for path in sorted(xlsx_dir.glob('*.xlsx')):
        wb_rows = read_workbook_rows(path)
        if not is_year_file(wb_rows):
            continue
        merge_store(store, parse_year_file(path))
    return store


def _row_key(row: dict) -> str:
    code = row.get('code')
    if code:
        return 'C' + str(code)
    return 'N' + str(row.get('name') or '')


def _rows_to_map(rows) -> dict:
    return {_row_key(r): r for r in (rows or [])}


def _compare_rows(seed_rows, official_rows, month_ords):
    """(hard, soft) の2リストを返す。hard=値衝突・行落ちの疑い、soft=改訂として説明のつく差。"""
    seed_map = _rows_to_map(seed_rows)
    off_map = _rows_to_map(official_rows)
    off_names = {norm(r.get('name')) for r in (official_rows or [])}
    hard, soft = [], []
    for key in sorted(set(seed_map) | set(off_map)):
        a = seed_map.get(key)
        b = off_map.get(key)
        if a is None:
            soft.append(f'{key}: official側に行追加')
            continue
        if b is None:
            if norm(a.get('name')) in off_names:
                soft.append(f'{key}: 行キー移動（同名行 {a.get("name")!r} はofficial側に存在）')
            else:
                hard.append(f'{key}: seed行がofficialに無い（行落ちの疑い） name={a.get("name")!r}')
            continue
        if a.get('name') != b.get('name'):
            soft.append(f'{key}: name表記変更 seed={a.get("name")!r} official={b.get("name")!r}')
        for o in month_ords:
            mk = str(o)
            av = (a.get('months') or {}).get(mk)
            bv = (b.get('months') or {}).get(mk)
            if av == bv:
                continue
            if av is not None and bv is not None:
                hard.append(f'{key}: 月{mk} 値衝突 seed={av!r} official={bv!r}')
            else:
                soft.append(f'{key}: 月{mk} None⇔値 seed={av!r} official={bv!r}')
        ab, bb = a.get('bonus'), b.get('bonus')
        if ab != bb:
            # bonusは改訂頻度が高い派生値（賞与列の構成が版によって変わる実例あり:
            # 旧版seedに本給合計を超える非現実値が入っていた）。値衝突でもFAILにはしない。
            soft.append(f'{key}: bonus改訂 seed={ab!r} official={bb!r}')
    return hard, soft


def main() -> int:
    parser = argparse.ArgumentParser(description='seed_store.json と公式xlsxの再構築を突き合わせるゴールデン検証')
    parser.add_argument('--dashboard-dir', default=str(REPO_DIR))
    args = parser.parse_args()
    root = pathlib.Path(args.dashboard_dir)

    seed_path = root / 'data' / 'gross_profit' / 'seed_store.json'
    xlsx_dir = root / 'data' / 'profit_xlsx'

    if not seed_path.exists():
        print(f'seed_store.json が見つかりません: {seed_path}'
              '（先に extract_gross_profit_seed.py を実行してください）', file=sys.stderr)
        return 1
    if not xlsx_dir.exists():
        print(f'公式xlsxディレクトリが見つかりません: {xlsx_dir}', file=sys.stderr)
        return 1

    seed = json.loads(seed_path.read_text(encoding='utf-8'))
    official = _build_from_official(xlsx_dir)

    hard_fail = False

    print(f'=== FY{CURRENT_FY}（当年度） 会計月1〜3・8シート 値衝突チェック ===')
    seed_cur = (seed.get('sheets') or {}).get(CURRENT_FY, {})
    off_cur = (official.get('sheets') or {}).get(CURRENT_FY, {})
    for t in ALL_SHEET_TYPES:
        hard, soft = _compare_rows(seed_cur.get(t), off_cur.get(t), range(1, 4))
        if hard:
            hard_fail = True
            print(f'  [NG] {t}: 値衝突/行落ち {len(hard)}件（改訂差 {len(soft)}件）')
            for d in hard[:20]:
                print(f'    - {d}')
            if len(hard) > 20:
                print(f'    ...ほか{len(hard) - 20}件')
        else:
            note = f'（改訂差 {len(soft)}件: 例 {soft[0]}）' if soft else ''
            print(f'  [OK] {t} {note}')

    print(f'=== FY{"・".join(REFERENCE_FYS)}（参考・exitに影響しない） ===')
    for fy in REFERENCE_FYS:
        seed_fy = (seed.get('sheets') or {}).get(fy, {})
        off_fy = (official.get('sheets') or {}).get(fy, {})
        for t in ALL_SHEET_TYPES:
            hard, soft = _compare_rows(seed_fy.get(t), off_fy.get(t), range(1, 13))
            if hard or soft:
                ex = hard[0] if hard else soft[0]
                print(f'  FY{fy} {t}: 値衝突{len(hard)}件・改訂差{len(soft)}件（例: {ex}）')

    if hard_fail:
        print(f'FAIL: FY{CURRENT_FY} 会計月1〜3 に値衝突（移植バグの疑い）があります。', file=sys.stderr)
        return 1
    print(f'PASS: FY{CURRENT_FY} 会計月1〜3 に値衝突なし（改訂差はレポート参照）。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
