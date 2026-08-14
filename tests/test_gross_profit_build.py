"""粗利ダッシュボード配信用HTML自動生成（scripts/lib_gross_profit.py）のユニットテスト。

密閉（実データ・実サーバ不要）: openpyxlでtmp_pathに合成した最小xlsxフィクスチャのみを使う。

実行:
    cd /Users/genie/dev/ai-apps/Dashboard && python3 -m pytest tests/test_gross_profit_build.py -q
"""
import json
import pathlib
import sys

import openpyxl
import pytest

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent.parent / 'scripts'
sys.path.insert(0, str(SCRIPTS_DIR))

import lib_gross_profit as L  # noqa: E402


# ============ フィクスチャ用ヘルパ ============
def _write_workbook(path, sheets: dict) -> None:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append(list(row))
    wb.save(path)


# ============ 1) getTable: ヘッダ検出・列役割判定 ============
GT_A_HEADER = ['診療科コード', '診療科名称', 'R8.4', 'R8.5', 'R8.6', 'R8.7', 'R8.8', 'R8.9']  # 月列6（境界）
GT_B_HEADER = ['入院主科', '入院主科名称', 'R8.4', 'R8.5', 'R8.6', 'R8.7', 'R8.8', 'R8.9']
GT_C_HEADER = ['診療科コード', '診療科名称', 'R8.4', 'R8.5', 'R8.6', 'R8.7', 'R8.8']  # 月列5（閾値未満）


@pytest.fixture
def gt_fixture_rows(tmp_path):
    path = tmp_path / 'gt_fixture.xlsx'
    _write_workbook(path, {
        'gt_a': [GT_A_HEADER, [1, '内科', 100, 200, 300, 400, 500, 600]],
        'gt_b': [GT_B_HEADER, [2, '外科病棟', 10, 20, 30, 40, 50, 60]],
        'gt_c': [GT_C_HEADER, [3, '皮膚科', 1, 2, 3, 4, 5]],
    })
    return L.read_workbook_rows(path)


def test_get_table_header_detection_and_name_code_cols(gt_fixture_rows):
    t = L.get_table(gt_fixture_rows['gt_a'])
    assert t is not None
    assert t['hdr'] == 0
    assert t['nameCol'] == 1  # 診療科名称
    assert t['codeCol'] == 0  # 診療科コード
    assert len(t['monthCols']) == 6  # 6列以上の境界
    assert [m['ord'] for m in t['monthCols']] == [1, 2, 3, 4, 5, 6]


def test_get_table_inpatient_naming_variant(gt_fixture_rows):
    t = L.get_table(gt_fixture_rows['gt_b'])
    assert t is not None
    assert t['nameCol'] == 1  # 入院主科名称
    assert t['codeCol'] == 0  # 入院主科


def test_get_table_returns_none_when_below_month_col_threshold(gt_fixture_rows):
    t = L.get_table(gt_fixture_rows['gt_c'])
    assert t is None  # 月列5 < 6 は不成立


# ============ 2) extract: ord割当・code整形・終端ガード・4月ずれ・左詰め総計・賞与3型 ============
EXT_MISC_HEADER = ['診療科コード', '診療科名称'] + [f'R8.{m}' for m in range(4, 10)] + \
    [f'R8.{m}' for m in (10, 11, 12)] + ['R9.1', 'R9.2', 'R9.3']

EXT_SHIFT_HEADER = ['診療科コード', '診療科名称', None] + \
    [f'R8.{m}' for m in range(4, 10)] + [f'R8.{m}' for m in (10, 11, 12)] + ['R9.1', 'R9.2', 'R9.3']

EXT_BONUS_COLS_HEADER = EXT_MISC_HEADER + ['R8.6（賞与）', 'R8.12（賞与）', 'R9.3（賞与）']
EXT_WBTOTAL_HEADER = EXT_MISC_HEADER + ['総計（賞与込み）']


@pytest.fixture
def extract_fixture_rows(tmp_path):
    path = tmp_path / 'extract_fixture.xlsx'
    _write_workbook(path, {
        'ext_misc': [
            EXT_MISC_HEADER,
            ['1.0', '内科', 1000000, 1100000, 1200000, None, None, None, None, None, None, None, None, None],
            ['2', '外科', 500000, 500000, 1000000, None, None, None, None, None, None, None, None, None],
            ['9', '＊注記：以下略', None, None, None, None, None, None, None, None, None, None, None, None],
            ['4', 'ダミー科', 999999, 999999, 999999, None, None, None, None, None, None, None, None, None],
        ],
        'ext_shift': [
            EXT_SHIFT_HEADER,
            ['3', '産婦人科', 2000000, None, 2100000, 2200000, None, None, None, None, None, None, None, None, None],
        ],
        'ext_bonus_cols': [
            EXT_BONUS_COLS_HEADER,
            ['10', '科A', 1000000, 1000000, 1000000, 1000000, None, None, None, None, None, None, None, None,
             500000, None, None],
            ['11', '科B', 900000, 950000, None, None, None, None, None, None, None, None, None, None,
             300000, None, None],
            ['12', '科E', 800000, 800000, 800000, None, None, None, None, None, None, None, None, None,
             2400000, None, None],
        ],
        'ext_wbtotal': [
            EXT_WBTOTAL_HEADER,
            ['20', '科C', 700000, 700000, 700000, None, None, None, None, None, None, None, None, None, 2150000],
        ],
    })
    return L.read_workbook_rows(path)


def test_extract_ord_code_and_terminator_guard(extract_fixture_rows):
    rows = L.extract(extract_fixture_rows['ext_misc'])
    # 終端ガード：＊注記行で打ち切り→ダミー科は含まれない
    assert len(rows) == 2
    assert rows[0]['name'] == '内科'
    assert rows[0]['code'] == '1'  # '1.0' → '1'
    assert rows[0]['months']['1'] == 1000000  # 先頭月列=ord1
    assert rows[0]['months']['2'] == 1100000
    assert rows[0]['months']['3'] == 1200000
    assert rows[0]['bonus'] is None


def test_extract_left_padded_running_total_removed(extract_fixture_rows):
    rows = L.extract(extract_fixture_rows['ext_misc'])
    dept2 = rows[1]
    assert dept2['code'] == '2'
    assert dept2['months']['1'] == 500000
    assert dept2['months']['2'] == 500000
    assert dept2['months']['3'] is None  # 500000+500000=1000000と一致→総計とみなし除去


def test_extract_four_month_shift_correction(extract_fixture_rows):
    rows = L.extract(extract_fixture_rows['ext_shift'])
    assert len(rows) == 1
    assert rows[0]['months']['1'] == 2000000  # ヘッダ位置は空欄・1列左の値を採用
    assert rows[0]['months']['2'] == 2100000
    assert rows[0]['months']['3'] == 2200000


def test_extract_bonus_sum_of_bonus_columns(extract_fixture_rows):
    rows = L.extract(extract_fixture_rows['ext_bonus_cols'])
    dept_a = next(r for r in rows if r['code'] == '10')
    assert dept_a['bonus'] == 500000  # 賞与列合計


def test_extract_bonus_null_when_input_months_below_three(extract_fixture_rows):
    rows = L.extract(extract_fixture_rows['ext_bonus_cols'])
    dept_b = next(r for r in rows if r['code'] == '11')
    assert dept_b['bonus'] is None  # 入力月数2 < 3 → null


def test_extract_bonus_null_when_equals_month_sum(extract_fixture_rows):
    rows = L.extract(extract_fixture_rows['ext_bonus_cols'])
    dept_e = next(r for r in rows if r['code'] == '12')
    assert dept_e['bonus'] is None  # 賞与列の値が月合計と一致→左詰め総計の誤読とみなしnull


def test_extract_bonus_from_wb_total_minus_month_sum(extract_fixture_rows):
    rows = L.extract(extract_fixture_rows['ext_wbtotal'])
    dept_c = rows[0]
    assert dept_c['months']['1'] == 700000
    assert dept_c['bonus'] == 50000  # 賞与込み総計(2150000) - 月合計(2100000)


# ============ 3) fy検出（境界） ============
def test_fy_from_month_label_reiwa_boundary():
    assert L.fy_from_month_label('R8.4') == 2026   # 4月以降→当年度
    assert L.fy_from_month_label('R6.1') == 2023    # 1〜3月→前年度側の境界


# ============ 4) findSheetExact相当（parse_year_file経由） ============
@pytest.fixture
def year_file_path(tmp_path):
    header = ['診療科コード', '診療科名称'] + [f'R8.{m}' for m in range(4, 10)] + \
        [f'R8.{m}' for m in (10, 11, 12)] + ['R9.1', 'R9.2', 'R9.3']
    salary_header = ['診療科コード', '診療科名', '人数'] + header[2:]
    path = tmp_path / 'year_file.xlsx'
    _write_workbook(path, {
        '外来総計': [header, [1, '内科', 6000000, 8000000, 8600000, None, None, None, None, None, None, None, None, None]],
        '外来薬剤材料': [header, [1, '内科', 160000, 300000, 210000, None, None, None, None, None, None, None, None, None]],
        '入院総計': [header, [1, '内科', 10000000, 7700000, 15000000, None, None, None, None, None, None, None, None, None]],
        '入院薬剤材料': [header, [1, '内科', 1400000, 120000, 440000, None, None, None, None, None, None, None, None, None]],
        '入院包括分薬剤材料': [header, [1, '内科', 1000000, 1100000, 1500000, None, None, None, None, None, None, None, None, None]],
        '給与': [salary_header, [None, '臨床研修科', 45, 21000000, 22000000, 23000000, None, None, None, None, None, None, None, None, None]],
        # 半角スペース有り：norm一致でfindSheetExactにマッチさせる
        '給与 (謝金)': [salary_header, [None, '臨床研修科', None, None, None, None, None, None, None, None, None, None, None, None, None]],
        '給与 (合算)': [salary_header, [2, '救命救急センター', None, 19000000, 19000000, 20900000, None, None, None, None, None, None, None, None, None]],
    })
    return path


def test_find_sheet_exact_matches_spaced_salary_sheet_names(year_file_path):
    store = L.parse_year_file(year_file_path)
    assert set(store['sheets'].keys()) == {'2026'}
    year_sheets = store['sheets']['2026']
    # 実際のシート名は半角スペース入り「給与 (合算)」だが、格納キーは正規化済みラベル
    assert '給与(合算)' in year_sheets
    assert year_sheets['給与(合算)'][0]['name'] == '救命救急センター'
    assert '給与(謝金)' in year_sheets
    assert year_sheets['外来総計'][0]['months']['1'] == 6000000


def test_is_year_file_and_detect_fy(year_file_path):
    wb_rows = L.read_workbook_rows(year_file_path)
    assert L.is_year_file(wb_rows) is True
    assert L.detect_fy(wb_rows) == 2026


# ============ 5) merge_store: 同月上書き・None非上書き・strキー維持 ============
def test_merge_store_overwrite_none_preserved_and_str_keys():
    store = {
        'version': 2, 'updated': '', 'sheets': {
            '2025': {'外来総計': [
                {'name': '内科', 'code': '1', 'months': {'1': 100, '2': 300}, 'bonus': None},
            ]},
        },
    }
    partial = {
        'sheets': {
            '2025': {'外来総計': [
                {'name': '内科', 'code': '1', 'months': {'1': 200, '2': None}, 'bonus': 5000},
            ]},
        },
    }
    L.merge_store(store, partial)
    rows = store['sheets']['2025']['外来総計']
    assert len(rows) == 1  # 同一行キーで統合される（新規追加されない）
    row = rows[0]
    assert row['months']['1'] == 200  # 同月上書き
    assert row['months']['2'] == 300  # None非上書き（既存値保持）
    assert row['bonus'] == 5000
    assert all(isinstance(k, str) for k in row['months'].keys())  # strキー維持


def test_merge_store_new_row_added_by_name_when_code_empty():
    store = {'version': 2, 'updated': '', 'sheets': {}}
    partial = {'sheets': {'2026': {'給与': [
        {'name': '未コード科', 'code': '', 'months': {'1': 999}, 'bonus': None},
    ]}}}
    L.merge_store(store, partial)
    rows = store['sheets']['2026']['給与']
    assert len(rows) == 1
    assert rows[0]['months']['1'] == 999


# ============ 6) parse_patient_csv / parse_patient_xlsx ============
def test_parse_patient_csv_basic_and_invalid_rows_skipped():
    text = '\n'.join([
        '日付,診療科コード,診療科名,年月,入院患者数,新入院患者数,外来患者数',
        '2026-04-01,10,内科,2026-04,123.5,12,45',
        '2026-04-01,,不明科,2026-04,10,1,2',       # コード不正→スキップ
        '2026-04-01,11,外科,bad-ym,5,1,1',           # 年月不正→スキップ
    ])
    pat = L.parse_patient_csv(text)
    assert pat == {'10': {'2026-04': {'in': 123.5, 'new': 12.0, 'gairai': 45.0}}}


def test_parse_patient_csv_returns_none_when_no_valid_rows():
    text = '日付,診療科コード,診療科名,年月,入院患者数,新入院患者数,外来患者数\nx,x,x,x,x,x,x'
    assert L.parse_patient_csv(text) is None


@pytest.fixture
def patient_xlsx_path(tmp_path):
    ws_rows = [
        [None] * 14,
        ['入院患者数', None] + [None] * 12,
        [10, '内科', 100, 105, 110, 115, 120, 125, 130, 135, 140, 145, 150, 155],
        [20, '外科', 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100, 105],
        [None, '合計', None, None, None, None, None, None, None, None, None, None, None, None],
        [None] * 14,
        ['新入院患者数', None] + [None] * 12,
        [10, '内科', 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21],
        [20, '外科', 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16],
    ]
    path = tmp_path / 'patient_monthly.xlsx'
    _write_workbook(path, {
        '入力及び月設定': ws_rows,
        'メモ【2026年度】': [['fy tag sheet']],
    })
    return path


def test_parse_patient_xlsx_reads_in_and_new_by_fiscal_month(patient_xlsx_path):
    pat = L.parse_patient_xlsx(patient_xlsx_path)
    assert pat is not None
    assert pat['10']['2026-04'] == {'in': 100.0, 'new': 10.0}
    assert pat['10']['2027-03'] == {'in': 155.0, 'new': 21.0}   # 1〜3月は翌暦年
    assert pat['20']['2026-04'] == {'in': 50.0, 'new': 5.0}
    assert '合計' not in json.dumps(pat, ensure_ascii=False)  # 合計行は取り込まれない


# ============ 7) strip_head_data_scripts / inject_snapshot ============
FAKE_TEMPLATE = (
    '<!DOCTYPE html>\n'
    '<html><head>\n'
    '<meta charset="utf-8">\n'
    '<title>粗利ダッシュボード</title>\n'
    '<script id="snapData">window.__SNAPSHOT__={"old":true};</script>\n'
    '<script id="encMulti">window.__ENCMULTI__={"s":"AAAA","b":[]};</script>\n'
    '<script id="distBoot">window.tabTo=function(){};</script>\n'
    '</head>\n'
    '<body class="ops">\n'
    '<div id="opsCard">OPS</div>\n'
    '<div id="cardListWrap">LIST</div>\n'
    '<script>\n'
    'function exportMultiDistHtml(){\n'
    "  const inj=boot+'<script id=\"encMulti\">window.__ENCMULTI__='+esc2(JSON.stringify(pack))+';<\\/script>\\n';\n"
    '}\n'
    '</script>\n'
    '</body></html>'
)


def test_strip_head_data_scripts_head_only_and_body_untouched():
    stripped = L.strip_head_data_scripts(FAKE_TEMPLATE)
    head_part, sep, body_part = stripped.partition('</head>')
    assert sep == '</head>'
    assert 'snapData' not in head_part
    assert 'encMulti' not in head_part
    assert 'distBoot' not in head_part
    assert '<title>粗利ダッシュボード</title>' in head_part  # 他head内容は保持

    orig_body = FAKE_TEMPLATE.partition('</head>')[2]
    assert body_part == orig_body  # 本体は完全に無傷
    assert '<script id="encMulti">window.__ENCMULTI__=' in body_part  # 本体JS文字列リテラルは残存
    assert '<\\/script>' in body_part  # 実物の閉じタグではない（バックスラッシュ付き）ため誤爆しない


def test_strip_head_data_scripts_idempotent():
    once = L.strip_head_data_scripts(FAKE_TEMPLATE)
    twice = L.strip_head_data_scripts(once)
    assert once == twice


def test_inject_snapshot_replaces_head_and_preserves_body():
    store = {'version': 2, 'updated': '2026-08-14', 'sheets': {}}
    out = L.inject_snapshot(FAKE_TEMPLATE, store)
    head, sep, body = out.partition('</head>')
    assert sep == '</head>'
    assert head.count('id="snapData"') == 1
    assert head.count('id="distBoot"') == 1
    assert 'id="encMulti"' not in head  # 旧encMultiは除去され、再注入はしない

    snap_json = json.dumps(store, ensure_ascii=False, separators=(',', ':'))
    assert ('window.__SNAPSHOT__=' + snap_json + ';') in head

    orig_body = FAKE_TEMPLATE.partition('</head>')[2]
    assert body == orig_body
    assert '<script id="encMulti">window.__ENCMULTI__=' in body  # 本体JS文字列リテラルは無傷


def test_inject_snapshot_idempotent_when_applied_twice():
    store = {'version': 2, 'updated': '2026-08-14', 'sheets': {}}
    out1 = L.inject_snapshot(FAKE_TEMPLATE, store)
    out2 = L.inject_snapshot(out1, store)
    assert out1 == out2  # 2回適用しても差分なし（冪等）


def test_inject_snapshot_escapes_less_than_in_json():
    store = {
        'version': 2, 'updated': '', 'sheets': {
            '2026': {'外来総計': [{'name': 'A<B', 'code': '1', 'months': {}, 'bonus': None}]},
        },
    }
    out = L.inject_snapshot(FAKE_TEMPLATE, store)
    head = out.partition('</head>')[0]
    assert 'A<B' not in head
    assert 'A\\u003cB' in head


# ============ build: 鮮度ガード（decide_merge） ============
def test_decide_merge_freshness_guard(tmp_path):
    """ストアより古い断面のファイルはマージしない／内容不変はスキップ／新しい内容はマージ。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'build_gross_profit_dist', str(SCRIPTS_DIR / 'build_gross_profit_dist.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    import datetime as _dt
    import os

    f = tmp_path / 'y2023.xlsx'
    f.write_bytes(b'dummy-content-1')

    # 1) storeのbuilt_atよりmtimeが古い → スキップ
    store = {'updated': '2026-08-10', '_meta': {}}
    old = _dt.datetime(2024, 2, 28, 12, 0).timestamp()
    os.utime(f, (old, old))
    ok, reason = mod.decide_merge(f, store)
    assert not ok and '古い断面' in reason

    # 2) mtimeが新しい → マージ（ハッシュが記録される）
    new = _dt.datetime(2026, 8, 14, 7, 0).timestamp()
    os.utime(f, (new, new))
    ok, reason = mod.decide_merge(f, store)
    assert ok
    assert store['_meta']['file_sha256']['y2023.xlsx']

    # 3) 同一内容の再実行 → 内容変更なしでスキップ（mtimeを更新しても）
    newer = _dt.datetime(2026, 8, 15, 7, 0).timestamp()
    os.utime(f, (newer, newer))
    ok, reason = mod.decide_merge(f, store)
    assert not ok and '内容変更なし' in reason

    # 4) 内容が変わりmtimeも新しい → マージ
    f.write_bytes(b'dummy-content-2')
    os.utime(f, (newer, newer))
    ok, reason = mod.decide_merge(f, store)
    assert ok


def test_decide_merge_seed_fallback_baseline(tmp_path):
    """_meta.built_at が無い初回は seed の updated 日付が基準になる。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'build_gross_profit_dist', str(SCRIPTS_DIR / 'build_gross_profit_dist.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    import datetime as _dt
    import os

    f = tmp_path / 'y2026.xlsx'
    f.write_bytes(b'x')
    after_seed = _dt.datetime(2026, 8, 14, 6, 58).timestamp()
    os.utime(f, (after_seed, after_seed))
    store = {'updated': '2026-08-10'}
    ok, _ = mod.decide_merge(f, store)
    assert ok
