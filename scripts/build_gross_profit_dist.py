#!/usr/bin/env python3
"""公式年度ファイル（data/profit_xlsx/*.xlsx）・患者数月報（data/patient_monthly/*）を取り込み、
蓄積store（data/gross_profit/store.json）を更新した上で、パスワード無し配信用HTML
（粗利ダッシュボード_配信用.html / output/粗利ダッシュボード_配信用_YYYYMMDD.html）を再生成する。

前提: 先に extract_gross_profit_seed.py を実行し、
      data/gross_profit/seed_store.json と data/gross_profit/template.html を作成しておくこと。

実行例:
    python3 scripts/build_gross_profit_dist.py
    python3 scripts/build_gross_profit_dist.py --dashboard-dir /path/to/Dashboard
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from lib_gross_profit import (  # noqa: E402
    is_year_file,
    parse_year_file,
    merge_store,
    read_workbook_rows,
    parse_patient_csv,
    parse_patient_xlsx,
    inject_snapshot,
)

REPO_DIR = pathlib.Path(__file__).resolve().parent.parent


def _file_sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _freshness_baseline(store: dict) -> datetime.datetime:
    """このstoreに最後にデータを取り込んだ時刻。初回（seed直後）はseedの updated 日付を使う。

    背景: ディスク上の公式年度ファイルは、ブラウザ管理ツールへ取り込み済みの版（=seed）より
    古い断面のことがある（実例: 2023ファイルは2024年2月断面で、月11-12に明示的0が入っており、
    月マージするとseedの実データを0で潰す）。ストアより古いファイルはマージしない。
    """
    meta = store.get('_meta') or {}
    ts = meta.get('built_at') or ((store.get('updated') or '1970-01-01') + 'T00:00:00')
    try:
        return datetime.datetime.fromisoformat(ts)
    except ValueError:
        return datetime.datetime(1970, 1, 1)


def decide_merge(path: pathlib.Path, store: dict) -> tuple[bool, str]:
    """(取り込むか, 理由) を返す。①内容ハッシュ一致→スキップ ②storeより古いmtime→スキップ。"""
    meta = store.setdefault('_meta', {})
    hashes = meta.setdefault('file_sha256', {})
    digest = _file_sha256(path)
    if hashes.get(path.name) == digest:
        return False, '内容変更なし（取込済み）'
    mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime)
    if mtime <= _freshness_baseline(store):
        return False, f'ストアより古い断面（{mtime.date()}）のためスキップ'
    hashes[path.name] = digest
    return True, ''


def _merge_patients(store: dict, pat: dict) -> int:
    """1件のファイルから読んだ患者数(pat: {code:{ym:{...}}})を月単位でstore.patientsへマージする。
    2277〜2283行のロジックに相当（store全体のmergeStoreは使わない＝JSでも別経路）。
    """
    if not pat:
        return 0
    store.setdefault('patients', {})
    n = 0
    for code, months in pat.items():
        dst = store['patients'].setdefault(code, {})
        for ym, rec in months.items():
            merged = dict(dst.get(ym, {}))
            merged.update(rec)
            dst[ym] = merged
            n += 1
    return n


def main() -> int:
    parser = argparse.ArgumentParser(description='公式年度ファイル・患者数月報を取り込み、配信用HTMLを再生成する。')
    parser.add_argument('--dashboard-dir', default=str(REPO_DIR), help='Dashboardリポのルート（既定=このスクリプトの親のリポ直下）')
    args = parser.parse_args()

    root = pathlib.Path(args.dashboard_dir)
    data_dir = root / 'data' / 'gross_profit'
    seed_path = data_dir / 'seed_store.json'
    store_path = data_dir / 'store.json'
    template_path = data_dir / 'template.html'

    if not template_path.exists() or not (store_path.exists() or seed_path.exists()):
        print(
            '先に extract_gross_profit_seed.py を実行してください'
            '（data/gross_profit/seed_store.json と template.html が必要です）。',
            file=sys.stderr,
        )
        return 1

    base_path = store_path if store_path.exists() else seed_path
    store = json.loads(base_path.read_text(encoding='utf-8'))

    # --- 1) 公式年度ファイルの取り込み ---
    xlsx_dir = root / 'data' / 'profit_xlsx'
    file_summary = []  # [(filename, fy|None, {type:rows}|note)]
    if xlsx_dir.exists():
        for path in sorted(xlsx_dir.glob('*.xlsx')):
            ok, reason = decide_merge(path, store)
            if not ok:
                file_summary.append((path.name, None, reason))
                continue
            wb_rows = read_workbook_rows(path)
            if not is_year_file(wb_rows):
                file_summary.append((path.name, None, '公式年度ファイルではないためスキップ'))
                continue
            partial = parse_year_file(path)
            fy_keys = sorted(partial['sheets'].keys())
            merge_store(store, partial)
            for fy in fy_keys:
                rows_by_type = {t: len(rows) for t, rows in partial['sheets'][fy].items()}
                file_summary.append((path.name, fy, rows_by_type))

    # --- 2) 患者数月報の取り込み（存在すれば） ---
    patient_dir = root / 'data' / 'patient_monthly'
    patient_summary = []  # [(filename, updated_count)]
    if patient_dir.exists():
        patient_files = sorted(list(patient_dir.glob('*.xlsx')) + list(patient_dir.glob('*.csv')))
        for path in patient_files:
            ok, reason = decide_merge(path, store)
            if not ok:
                patient_summary.append((path.name, reason))
                continue
            if path.suffix.lower() == '.xlsx':
                pat = parse_patient_xlsx(path)
            else:
                pat = parse_patient_csv(path.read_text(encoding='utf-8-sig'))
            n = _merge_patients(store, pat)
            patient_summary.append((path.name, f'{n}件（診療科×年月）更新'))

    store['updated'] = datetime.date.today().isoformat()
    store.setdefault('_meta', {})['built_at'] = datetime.datetime.now().isoformat(timespec='seconds')
    data_dir.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps(store, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')

    # --- 3) 配信用HTMLの再生成（_meta等の内部キーはスナップショットに含めない） ---
    template = template_path.read_text(encoding='utf-8')
    snap_store = {k: v for k, v in store.items() if not k.startswith('_')}
    dist_html = inject_snapshot(template, snap_store)

    out_html_path = root / '粗利ダッシュボード_配信用.html'
    out_html_path.write_text(dist_html, encoding='utf-8')

    output_dir = root / 'output'
    output_dir.mkdir(parents=True, exist_ok=True)
    ymd = datetime.date.today().strftime('%Y%m%d')
    dated_out_path = output_dir / f'粗利ダッシュボード_配信用_{ymd}.html'
    dated_out_path.write_text(dist_html, encoding='utf-8')

    # --- サマリ出力 ---
    print('=== 取込サマリ（ファイル別） ===')
    for name, fy, info in file_summary:
        if fy is None:
            print(f'  {name}: {info}')
            continue
        print(f'  {name}: FY{fy}')
        for t, cnt in info.items():
            print(f'    - {t}: {cnt}行')

    if patient_summary:
        print('=== 患者数月報 取込サマリ ===')
        for name, note in patient_summary:
            print(f'  {name}: {note}')

    # 最新入力月: 外来総計の入力行数がピーク月の50%以上ある月だけを「入力済み」とみなす
    # （ダッシュボード側 validMonths と同じ考え方。迷い込みの単発値や未来月の0埋めを月として数えない）
    print('=== 年度別 最新入力月（会計月・R表記） ===')
    for fy in sorted(store.get('sheets', {}).keys(), key=int):
        rows = store['sheets'][fy].get('外来総計') or []
        cnt = {m: 0 for m in range(1, 13)}
        for row in rows:
            for m, v in (row.get('months') or {}).items():
                if v is not None:
                    cnt[int(m)] += 1
        peak = max(cnt.values()) if cnt else 0
        latest = None
        for m in range(1, 13):
            if peak > 0 and cnt[m] >= 0.5 * peak:
                latest = m
        if latest is None:
            print(f'  FY{fy}: -')
        else:
            gy, cm = (int(fy), latest + 3) if latest <= 9 else (int(fy) + 1, latest - 9)
            print(f'  FY{fy}: 会計月{latest}（R{gy - 2018}.{cm}）')

    print('=== 出力パス ===')
    print(f'  store.json: {store_path}')
    print(f'  配信用HTML: {out_html_path}')
    print(f'  日付付きコピー: {dated_out_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
