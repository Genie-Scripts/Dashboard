#!/usr/bin/env python3
"""配信用HTML（粗利ダッシュボード_配信用.html）から管理者パスワードでencMultiを復号し、
今後の再ビルドに必要な種データ一式（seed_store.json / template.html）を作る。

実行例:
    python3 scripts/extract_gross_profit_seed.py --password 'xxxxxxxx'
    python3 scripts/extract_gross_profit_seed.py --password 'xxxxxxxx' --html /path/to/other.html

出力:
    data/gross_profit/seed_store.json                              … 復号したstore（version/updated/sheets/patients）
    data/gross_profit/粗利ダッシュボード_配信用_backup_YYYYMMDD.html … 入力HTMLのバックアップコピー
    data/gross_profit/template.html                                 … head内のsnapData/encMulti/distBootを除去したテンプレート
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from lib_gross_profit import decrypt_encmulti, strip_head_data_scripts  # noqa: E402

REPO_DIR = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_HTML = REPO_DIR / '粗利ダッシュボード_配信用.html'


def main() -> int:
    parser = argparse.ArgumentParser(
        description='配信用HTMLのencMultiを管理者パスワードで復号し、seed_store.json / template.html を作る。'
    )
    parser.add_argument('--password', required=True, help='管理者用パスワード（コード中に埋め込まない）')
    parser.add_argument('--html', default=str(DEFAULT_HTML), help='入力HTML（既定=リポ直下の配信用HTML）')
    args = parser.parse_args()

    html_path = pathlib.Path(args.html)
    if not html_path.exists():
        print(f'入力HTMLが見つかりません: {html_path}', file=sys.stderr)
        return 1

    html = html_path.read_text(encoding='utf-8')
    store = decrypt_encmulti(html, args.password)

    out_dir = REPO_DIR / 'data' / 'gross_profit'
    out_dir.mkdir(parents=True, exist_ok=True)

    seed_path = out_dir / 'seed_store.json'
    seed_path.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding='utf-8')

    ymd = datetime.date.today().strftime('%Y%m%d')
    backup_path = out_dir / f'粗利ダッシュボード_配信用_backup_{ymd}.html'
    shutil.copyfile(html_path, backup_path)

    template = strip_head_data_scripts(html)
    template_path = out_dir / 'template.html'
    template_path.write_text(template, encoding='utf-8')

    print('抽出完了:')
    print(f'  seed_store.json: {seed_path}')
    print(f'  バックアップHTML: {backup_path}')
    print(f'  テンプレートHTML: {template_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
