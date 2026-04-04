import streamlit as st
import pandas as pd
import numpy as np
import altair as alt
import jpholiday
import datetime
from dateutil.relativedelta import relativedelta
import unicodedata
import json
import pathlib
import os

# generate_html.py から importlib でロードされた場合は Streamlit UI を実行しない
# streamlit.runtime.exists() は Streamlit コンテキスト内でのみ True を返す
try:
    from streamlit.runtime import exists as _st_runtime_exists
    _IS_MAIN = _st_runtime_exists()
except Exception:
    _IS_MAIN = False

# --- 設定 ---
if _IS_MAIN:
    st.set_page_config(page_title="手術経営管理ダッシュボード v7.4", layout="wide")

# --- 自動読み込みフォルダ設定 ---
_BASE_DIR   = pathlib.Path(__file__).parent
DATA_DIR    = _BASE_DIR / "data"    # データCSVを置くフォルダ
TARGET_DIR  = _BASE_DIR / "target"  # 目標CSVを置くフォルダ

def _auto_load_data_files():
    """data/ フォルダの CSV を更新日時付きで読み込む（キャッシュ無効化に使用）"""
    if not DATA_DIR.exists():
        return []
    result = []
    for path in sorted(DATA_DIR.glob("*.csv")):
        mtime = path.stat().st_mtime
        content = path.read_bytes()
        result.append((path.name, f"auto:{path}:{mtime}", content))
    return result

def _auto_load_target_file():
    """target/ フォルダの最初の CSV を開いて返す（なければ None）"""
    if not TARGET_DIR.exists():
        return None
    files = sorted(TARGET_DIR.glob("*.csv"))
    return files[0].open("rb") if files else None
TARGET_DAILY_GA = 21
CORE_MINUTES = 525

GA_ROOM_LIST = [f"OP-{i}" for i in [1,2,3,4,5,6,7,8,9,10,12]] 
ALL_ROOM_LIST = GA_ROOM_LIST + ["OP-11A", "OP-11B"]

# --- 1. ヘルパー関数 ---

def safe_date_range(date_input_value):
    """date_inputの戻り値を安全にタプルに変換"""
    if isinstance(date_input_value, tuple):
        return date_input_value if len(date_input_value) == 2 else None
    elif isinstance(date_input_value, (datetime.date, datetime.datetime)):
        return None  # 単一の日付の場合は範囲として扱わない
    else:
        return None

def is_biz_day(date):
    if not isinstance(date, (datetime.date, datetime.datetime)): return False
    if date.weekday() >= 5: return False
    if jpholiday.is_holiday(date): return False
    if (date.month == 12 and date.day >= 28) or (date.month == 1 and date.day <= 3): return False
    return True

def count_remaining_biz_days(start_date, end_date):
    if not isinstance(start_date, (datetime.date, datetime.datetime)) or \
       not isinstance(end_date, (datetime.date, datetime.datetime)): return 0
    if start_date >= end_date: return 0
    dates = pd.date_range(start=start_date + datetime.timedelta(days=1), end=end_date)
    return sum(1 for d in dates if is_biz_day(d))

def normalize_str(s):
    if pd.isna(s): return ""
    s = unicodedata.normalize('NFKC', str(s))
    for char in ['－', 'ー', '−', '―', '‐', '—']: s = s.replace(char, '-')
    return s.strip().upper()

# --- 2. データ読み込みと前処理（順序並べ替え対応版） ---

@st.cache_data(show_spinner="データを読み込み中...")
def load_and_preprocess(file_data_list):
    """複数ファイルを読み込んで結合し、表記ゆれ（順序・改行・空白）を統一して重複削除"""
    
    # 1. データの読み込み
    raw_dfs = []
    file_info = []
    
    for file_name, file_id, file_content in file_data_list:
        import io
        try:
            # 型推論による不一致を防ぐため、まずは全て文字列として読み込む
            df = pd.read_csv(io.BytesIO(file_content), encoding='cp932', dtype=str)
        except:
            df = pd.read_csv(io.BytesIO(file_content), encoding='utf-8', dtype=str)
        
        # カラム名の空白除去
        df.columns = [c.strip() for c in df.columns]
        raw_dfs.append(df)
        file_info.append({'name': file_name, 'rows': len(df)})
    
    # 結合
    combined_raw = pd.concat(raw_dfs, ignore_index=True)
    before_count = len(combined_raw)
    
    # 2. 重複判定のための厳密なデータ正規化
    # 判定に使用するカラム
    dedup_cols = [
        '手術実施日', '実施診療科', '実施手術室', '麻酔科関与', 
        '入外区分', '申込区分', '実施術者', '麻酔種別', 
        '入室時刻', '退室時刻', '予定手術時間', '予定手術時間(OR)'
    ]
    # 存在するカラムのみに絞る
    target_cols = [c for c in dedup_cols if c in combined_raw.columns]

    # (A) 日付の統一
    if '手術実施日' in combined_raw.columns:
        combined_raw['手術実施日'] = pd.to_datetime(combined_raw['手術実施日'], errors='coerce')

    # (B) 数値の統一
    num_cols = ['予定手術時間', '予定手術時間(OR)']
    for c in num_cols:
        if c in combined_raw.columns:
            combined_raw[c] = pd.to_numeric(combined_raw[c], errors='coerce')

    # (C) 文字列の統一ヘルパー関数（改行削除 ＋ 並び替え）
    def normalize_and_sort_lines(s):
        if pd.isna(s): return None
        s = str(s)
        # 全角→半角正規化
        s = unicodedata.normalize('NFKC', s)
        
        # 改行やカンマで分割してリスト化
        # セル内で改行(\n)またはカンマ(,)で区切られている要素を分解
        separators = ['\r\n', '\n', '\r', ',']
        for sep in separators:
            s = s.replace(sep, '\n') # 一旦すべて改行コードに置換
            
        parts = [p.strip() for p in s.split('\n') if p.strip()]
        
        # 【重要】リストの中身をソート（あいうえお順）して再結合
        parts.sort()
        return ' '.join(parts).upper()

    # (D) 時刻の統一ヘルパー関数
    def normalize_time_strict(t):
        if pd.isna(t): return None
        t = str(t).strip()
        t = t.replace(':', '')
        if not t.isdigit(): return None
        if len(t) == 3: t = '0' + t
        return t

    # 文字列カラム（順序が変わる可能性のあるもの）の正規化
    # 実施術者、実施手術室などは順序をソートして統一
    sortable_cols = ['実施手術室', '実施術者', '麻酔種別']
    for c in sortable_cols:
        if c in combined_raw.columns:
            combined_raw[c] = combined_raw[c].apply(normalize_and_sort_lines)
            
    # その他の文字列カラム（単純正規化）
    other_str_cols = ['実施診療科', '麻酔科関与', '入外区分', '申込区分']
    for c in other_str_cols:
        if c in combined_raw.columns:
            combined_raw[c] = combined_raw[c].apply(lambda x: unicodedata.normalize('NFKC', str(x)).strip().upper() if pd.notna(x) else None)

    # 時刻カラムの正規化
    time_target_cols = ['入室時刻', '退室時刻']
    for c in time_target_cols:
        if c in combined_raw.columns:
            combined_raw[c] = combined_raw[c].apply(normalize_time_strict)

    # 3. 重複削除の実行
    # ここで「順序を統一したデータ」同士を比較するため、隠れ重複も削除されます
    combined_raw = combined_raw.drop_duplicates(subset=target_cols, keep='first')
    
    after_dedup_count = len(combined_raw)
    removed_count = before_count - after_dedup_count
    
    # 4. 分析用データフレームの作成
    df = combined_raw.copy()
    
    # --- 以降は既存のロジック（日付型は変換済みなのでそのまま使用） ---
    
    # 営業日判定
    def is_biz_day(date):
        if not isinstance(date, (pd.Timestamp, datetime.date, datetime.datetime)): 
            return False
        if date.weekday() >= 5: 
            return False
        import jpholiday
        if jpholiday.is_holiday(date): 
            return False
        if (date.month == 12 and date.day >= 28) or (date.month == 1 and date.day <= 3): 
            return False
        return True
    
    df['is_biz'] = df['手術実施日'].apply(is_biz_day)
    
    # 麻酔判定（正規化データを使うので条件文を調整）
    # normalize_and_sort_lines で大文字・半角・スペース区切りになっている
    if '麻酔種別' in df.columns:
        df['麻酔種別_norm'] = df['麻酔種別'].fillna("")
        df['is_ga'] = df['麻酔種別_norm'].str.contains("全身麻酔", na=False) & \
                      df['麻酔種別_norm'].str.contains("20分以上", na=False)
    else:
        df['is_ga'] = False

    # 手術室リスト化（正規化済みデータはスペース区切りになっている）
    if '実施手術室' in df.columns:
        df['rooms_list'] = df['実施手術室'].apply(lambda x: str(x).split(' ') if pd.notna(x) else [])
    else:
        df['rooms_list'] = []

    # 時間計算
    def parse_time_from_normalized(s):
        if not s or not isinstance(s, str): return np.nan
        try:
            if len(s) == 4:
                return int(s[:2]) * 60 + int(s[2:4])
            return np.nan
        except:
            return np.nan

    if '入室時刻' in df.columns and '退室時刻' in df.columns:
        in_m = df['入室時刻'].apply(parse_time_from_normalized)
        out_m = df['退室時刻'].apply(parse_time_from_normalized)
        out_m = np.where(out_m < in_m, out_m + 1440, out_m)
        overlap_s = np.maximum(in_m, 510)
        overlap_e = np.minimum(out_m, 1035)
        df['core_m'] = np.maximum(0, overlap_e - overlap_s)
        df['core_m'] = df['core_m'].fillna(0)
        df['over_m'] = np.maximum(0, out_m - np.maximum(in_m, 1035))
        df['over_m'] = df['over_m'].fillna(0)
        df['total_m'] = df['core_m'] + df['over_m']
    else:
        df['core_m'] = 0
        df['over_m'] = 0
        df['total_m'] = 0
    
    # サイドバー表示
    if len(file_data_list) > 1:
        st.sidebar.markdown("---")
        st.sidebar.markdown("### 📋 データ読み込み")
        for info in file_info:
            st.sidebar.text(f"• {info['name']}: {info['rows']:,}行")
        st.sidebar.text(f"合計: {before_count:,}行")
        if removed_count > 0:
            st.sidebar.warning(f"🔄 重複削除: {removed_count:,}行")
            st.sidebar.success(f"✅ 最終: {after_dedup_count:,}行")
        else:
            st.sidebar.success(f"✨ 重複なし: {after_dedup_count:,}行")
    
    return df

# --- 3. 診療科目標CSV読み込み ---

def load_target_csv(file):
    """診療科別週次目標CSVを読み込んでdictで返す（エンコーディング自動判別）"""
    raw_bytes = file.read()  # 一度バイト列として読み込む

    df = None
    for enc in ('utf-8-sig', 'utf-8', 'cp932', 'shift_jis'):
        try:
            import io
            df = pd.read_csv(io.BytesIO(raw_bytes), encoding=enc)
            df.columns = [c.strip() for c in df.columns]
            if len(df.columns) >= 2:
                break
        except Exception:
            continue

    if df is None or df.empty:
        st.error("目標CSVの読み込みに失敗しました。文字コードを確認してください。")
        return {}

    cols = list(df.columns)
    col_dept_candidates   = [c for c in cols if '診療科' in c]
    col_target_candidates = [c for c in cols if '目標' in c or 'target' in c.lower()]

    # 部分一致で見つからない場合は先頭列・2列目を使う
    col_dept   = col_dept_candidates[0]   if col_dept_candidates   else cols[0]
    col_target = col_target_candidates[0] if col_target_candidates else cols[1]

    df[col_dept]   = df[col_dept].astype(str).str.strip()
    df[col_target] = pd.to_numeric(df[col_target], errors='coerce')
    df = df.dropna(subset=[col_target])

    return dict(zip(df[col_dept], df[col_target]))


# --- 3b. 診療科パフォーマンス集計 ---

def calc_dept_performance(df_raw, target_dict, window_days, fy_start=None, fy_end=None):
    """
    診療科別の全身麻酔パフォーマンスを集計する。
    window_days : 集計日数（7 or 28）。fy_start 指定時は無視される。
    fy_start    : 今年度開始日（datetime.date）。指定時は4/1〜最新日の今年度集計。
                  目標は週次を日割り（÷7×実日数）で換算。
    fy_end      : 集計終了日（datetime.date）。指定時はその日を上限とする（昨年度等に使用）。
    """
    if fy_end is not None:
        max_date = pd.Timestamp(fy_end)
    else:
        max_date = df_raw['手術実施日'].max()

    if fy_start is not None:
        start_dt    = pd.Timestamp(fy_start)
        actual_days = (max_date.date() - fy_start).days + 1
    else:
        start_dt    = max_date - pd.Timedelta(days=window_days - 1)
        actual_days = window_days

    df_ga  = df_raw[df_raw['is_ga']].copy()
    df_win = df_ga[
        (df_ga['手術実施日'] >= start_dt) &
        (df_ga['手術実施日'] <= max_date) &
        (df_ga['実施診療科'].isin(target_dict.keys()))
    ]

    actual_series = df_win.groupby('実施診療科').size()

    rows = []
    week_factor = actual_days / 7   # 日割り換算（方法A）
    for dept, weekly_tgt in target_dict.items():
        actual = int(actual_series.get(dept, 0))
        target = weekly_tgt * week_factor
        rate   = actual / target if target > 0 else 0.0
        rows.append({
            'dept':          dept,
            'actual':        actual,
            'target':        target,
            'weekly_target': weekly_tgt,
            'actual_days':   actual_days,
            'rate':          rate,
        })

    rows.sort(key=lambda x: x['rate'], reverse=True)
    return rows


# --- 3c. 診療科パフォーマンス Streamlit描画 ---

def render_dept_performance(df_raw, target_dict, window_days, section_title, fy_start=None):
    """
    バーゲージ（達成率バー） + 実績/目標を併記するパネルを描画する。
    """
    if not target_dict:
        st.info("診療科目標CSVをアップロードすると診療科別パフォーマンスが表示されます。")
        return None

    rows = calc_dept_performance(df_raw, target_dict, window_days, fy_start=fy_start)
    
    # --- 修正箇所: week_factor の定義を追加 ---
    if fy_start is not None:
        max_date = df_raw['手術実施日'].max().date()
        actual_days = (max_date - fy_start).days + 1
    else:
        actual_days = window_days
    
    current_week_factor = actual_days / 7
    # ---------------------------------------

    st.markdown(
        f"### 🏥 {section_title}"
        f"<span style='font-size:0.82rem;color:#6B7280;margin-left:10px;'>"
        f"</span>",
        unsafe_allow_html=True
    )

    # Altair用DataFrame作成
    df_bar = pd.DataFrame(rows)
    df_bar['期間目標'] = df_bar['weekly_target'] * current_week_factor
    df_bar['達成率(%)'] = (df_bar['rate'] * 100).round(1)
    df_bar['ラベル'] = df_bar.apply(
        lambda r: f"{r['dept']}  {r['actual']}件 / 目標{r['target']:.1f}件", axis=1
    )
    # 色区分
    def color_class(rate):
        if rate >= 1.0:  return "達成 ✅"
        if rate >= 0.8:  return "概達 🟡"
        return "未達 🔴"
    df_bar['状態'] = df_bar['rate'].apply(color_class)

    color_scale = alt.Scale(
        domain=["達成 ✅", "概達 🟡", "未達 🔴"],
        range=["#10B981", "#F59E0B", "#EF4444"]
    )

    # バーチャート（実績）
    bar_actual = alt.Chart(df_bar).mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4).encode(
        x=alt.X('actual:Q',
                title=f"実績件数（直近{window_days}日）",
                scale=alt.Scale(domain=[0, max(df_bar['期間目標'].max() * 1.15, 1)])),
        y=alt.Y('ラベル:N', sort='-x', title=None,
                axis=alt.Axis(labelFontSize=12, labelLimit=280)),
        color=alt.Color('状態:N', scale=color_scale, legend=alt.Legend(title="達成状況")),
        tooltip=[
            alt.Tooltip('dept:N',       title='診療科'),
            alt.Tooltip('actual:Q',     title='実績件数'),
            alt.Tooltip('期間目標:Q',   title=f'期間目標({window_days}日)', format='.1f'),
            alt.Tooltip('達成率(%):Q',  title='達成率(%)', format='.1f'),
        ]
    )

    # 目標ラインを棒の上にオーバーレイ
    rule_target = alt.Chart(df_bar).mark_tick(
        color='#1D4ED8', thickness=2, size=14
    ).encode(
        x=alt.X('期間目標:Q'),
        y=alt.Y('ラベル:N', sort='-x'),
        tooltip=[alt.Tooltip('期間目標:Q', title='目標', format='.1f')]
    )

    chart = (bar_actual + rule_target).properties(
        height=max(260, len(rows) * 36)
    ).configure_axis(
        grid=False
    ).configure_view(
        strokeWidth=0
    )

    st.altair_chart(chart, use_container_width=True)

    # 凡例補足
    st.caption("🔵 縦線 = 期間目標  ✅ ≥100%  🟡 80〜99%  🔴 <80%")

    return rows  # HTMLレポート用に返す


# --- 4. HTMLレポート生成（全面改善版：差分表示、印刷対応、モバイルUX、データ信頼性） ---

def create_full_html_report(p1_data, p2_data, mode_label, portal_url, dept_rows_7=None, dept_rows_28=None, dept_rows_fy=None, fy_label="今年度", annual_p1=None, annual_p2=None, rolling_p1=None, rolling_p2=None, annual_dept_fy=None, rolling_dept_fy=None, annual_fy_label="今年度", rolling_fy_label="12ヶ月累計", current_mode="年度比較", prev_year_dept_fy=None, prev_year_fy_label="昨年度", dept_chart_json=None):
    def get_chart_json(df):
        if df is None: return "[]"
        temp = df.copy()
        temp['date_label'] = temp['手術実施日'].dt.strftime('%m/%d')
        return temp[['date_label', '件数', 'MA']].to_json(orient='records')

    p1_json = get_chart_json(p1_data.get('daily'))
    p2_json = get_chart_json(p2_data.get('daily'))
    target_val = TARGET_DAILY_GA if mode_label == "全身麻酔のみ" else "null"

    # ── 両モードデータ（JS切替用） ──
    has_dual = (annual_p1 is not None and rolling_p1 is not None)
    import json as _json

    def _metrics_dict(d):
        if d is None: return {}
        return {
            'title':   d.get('title', ''),
            'avg_v':   round(float(d.get('avg_v', 0)), 1),
            'achieve': round(float(d.get('achieve', 0)) * 100, 1),
            'month':   int(d.get('month', 1)),
            'm_val':   int(d.get('m_val', 0)),
            'fy_val':  int(d.get('fy_val', 0)),
            'util':    round(float(d.get('util', 0)) * 100, 1),
            'over':    round(float(d.get('over', 0)) * 100, 1),
            'rolling': bool(d.get('rolling_mode', False)),
            'num_days': int(len(d['daily']) if d.get('daily') is not None else 0),
        }

    ann_p1_json = get_chart_json(annual_p1.get('daily'))  if annual_p1  else '[]'
    ann_p2_json = get_chart_json(annual_p2.get('daily'))  if annual_p2  else '[]'
    rol_p1_json = get_chart_json(rolling_p1.get('daily')) if rolling_p1 else '[]'
    rol_p2_json = get_chart_json(rolling_p2.get('daily')) if rolling_p2 else '[]'

    ann_p1_m = _json.dumps(_metrics_dict(annual_p1),  ensure_ascii=False)
    ann_p2_m = _json.dumps(_metrics_dict(annual_p2),  ensure_ascii=False)
    rol_p1_m = _json.dumps(_metrics_dict(rolling_p1), ensure_ascii=False)
    rol_p2_m = _json.dumps(_metrics_dict(rolling_p2), ensure_ascii=False)

    # 診療科 fy HTML（両モード）
    # ※ build_dept_rows_html は後で定義されるが、Pythonは関数実行時に解決されるため問題なし
    _has_dual_dept = (annual_dept_fy is not None and rolling_dept_fy is not None)

    # 初期表示モードに応じた p2/p1 タイトル
    _init_is_rolling = (current_mode == "直近365日")

    # 差分計算
    def calc_diff(p2_val, p1_val):
        if p1_val == 0: return 0, "0%"
        diff = p2_val - p1_val
        pct = (diff / p1_val) * 100
        return diff, f"{pct:+.1f}%"
    
    avg_diff, avg_pct = calc_diff(p2_data.get('avg_v', 0), p1_data.get('avg_v', 0))
    m_diff, m_pct = calc_diff(p2_data.get('m_val', 0), p1_data.get('m_val', 0))
    fy_diff, fy_pct = calc_diff(p2_data.get('fy_val', 0), p1_data.get('fy_val', 0))
    util_diff, util_pct = calc_diff(p2_data.get('util', 0), p1_data.get('util', 0))
    over_diff, over_pct = calc_diff(p2_data.get('over', 0), p1_data.get('over', 0))
    
    # データ件数
    p1_count = len(p1_data.get('daily', [])) if p1_data.get('daily') is not None else 0
    p2_count = len(p2_data.get('daily', [])) if p2_data.get('daily') is not None else 0
    
    # トレンド判定（直近5日の移動平均の傾向）
    def get_trend(daily_df):
        """
        トレンド判定: 直近14日 vs その前14日の平均件数を比較（2週間ブロック比較法）
        条件: 変化率±8% かつ 絶対差±1.5件 の両方を満たす場合のみ上昇/下降と判定
        - 14日ブロックにより週次サイクル（曜日効果）をキャンセル
        - 絶対値条件により低件数診療科のノイズ誤判定を防止
        """
        if daily_df is None or len(daily_df) < 28: return "横ばい", "→"
        recent_14 = daily_df.tail(14)['件数'].mean()
        before_14 = daily_df.iloc[-28:-14]['件数'].mean()
        if before_14 == 0: return "横ばい", "→"
        diff_pct = (recent_14 - before_14) / before_14
        diff_abs = recent_14 - before_14
        if diff_pct > 0.08 and diff_abs > 1.5:
            return "上昇傾向", "↗"
        elif diff_pct < -0.08 and diff_abs < -1.5:
            return "下降傾向", "↘"
        return "横ばい", "→"
    
    p1_trend, p1_arrow = get_trend(p1_data.get('daily'))
    p2_trend, p2_arrow = get_trend(p2_data.get('daily'))
    
    # 現在時刻・最新データ日
    now_str = datetime.datetime.now().strftime('%Y年%m月%d日 %H:%M')
    _p2d = p2_data.get('daily')
    latest_data_date_str = (
        _p2d['手術実施日'].max().strftime('%Y年%m月%d日')
        if _p2d is not None and not _p2d.empty else "不明"
    )

    # ── 診療科パフォーマンス HTML生成ヘルパー ──
    def build_dept_rows_html(rows, window_days=None, weekly_mode=False):
        """
        rows       : calc_dept_performance の戻り値
        window_days: 7 or 28（短期タブ用）
        weekly_mode: True のとき「週平均件数/週 vs 週目標」表示
                     False のとき「累計件数 vs 日割り目標累計」表示
        """
        if not rows:
            return '<p style="color:#9CA3AF;text-align:center;">目標CSVが未設定です</p>'

        if weekly_mode:
            # 週平均換算: actual_days は rows[0] から取得
            actual_days = rows[0].get('actual_days', 7)
            week_factor = actual_days / 7.0
            # グラフの最大幅基準: 週換算値で統一
            max_val = max(
                max(r['actual'] / week_factor for r in rows),
                max(r['weekly_target']         for r in rows), 1
            )
        else:
            # 累計件数モード（7日・28日タブ）
            max_val = max(max(r['actual'] for r in rows),
                          max(r['target']  for r in rows), 1)

        html_rows = []
        for r in rows:
            rate = r['rate']
            if weekly_mode:
                actual_days = r.get('actual_days', 7)
                wf          = actual_days / 7.0
                weekly_act  = r['actual'] / wf          # 週平均実績
                weekly_tgt  = r['weekly_target']         # 週目標
                pct_fill    = min(weekly_act / max_val * 100, 100)
                tgt_pct     = min(weekly_tgt / max_val * 100, 100)
                act_tgt_label = f"{weekly_act:.1f}件/週 / 目標{weekly_tgt:.1f}件"
            else:
                pct_fill      = min(r['actual'] / max_val * 100, 100)
                tgt_pct       = min(r['target']  / max_val * 100, 100)
                act_tgt_label = f"{r['actual']}件 / 目標{r['target']:.1f}件"

            if rate >= 1.0:
                css_cls = "achieved"; badge = "✅"
            elif rate >= 0.8:
                css_cls = "near";     badge = "🟡"
            else:
                css_cls = "below";    badge = "🔴"
            rate_label = f"{rate*100:.0f}%"
            html_rows.append(f"""
            <div class="dept-row">
              <div class="dept-name {css_cls}" title="{r['dept']}">{r['dept']}</div>
              <div class="dept-act-tgt">{act_tgt_label}</div>
              <div class="dept-bar-wrap">
                <div class="dept-bar-fill {css_cls}" style="width:{pct_fill:.1f}%"></div>
                <div class="dept-target-line" style="left:{tgt_pct:.1f}%"></div>
              </div>
              <div class="dept-rate">{rate_label}</div>
              <div class="dept-badge {css_cls}"></div>
            </div>""")
        return "\n".join(html_rows)

    # 7日・28日タブ: 累計件数表示
    dept7_html   = build_dept_rows_html(dept_rows_7  or [], window_days=7,  weekly_mode=False)
    dept28_html  = build_dept_rows_html(dept_rows_28 or [], window_days=28, weekly_mode=False)
    # 今年度・ローリングタブ: 週平均件数/週 表示（app_department.py と統一）
    dept_fy_html     = build_dept_rows_html(dept_rows_fy    or [], weekly_mode=True)
    ann_dept_fy_html = build_dept_rows_html(annual_dept_fy  or [], weekly_mode=True) if annual_dept_fy  is not None else dept_fy_html
    rol_dept_fy_html = build_dept_rows_html(rolling_dept_fy or [], weekly_mode=True) if rolling_dept_fy is not None else ''
    # 昨年度タブ
    py_dept_fy_html  = build_dept_rows_html(prev_year_dept_fy or [], weekly_mode=True) if prev_year_dept_fy is not None else ''
    _has_prev_year   = bool(prev_year_dept_fy)

    # 集計期間ラベル（最新日付ベース）
    def get_period_label(window_days, p2_daily):
        if p2_daily is not None and not p2_daily.empty:
            end_d   = p2_daily['手術実施日'].max().date()
            start_d = end_d - datetime.timedelta(days=window_days - 1)
            return f"{start_d} 〜 {end_d}"
        return f"直近{window_days}日"

    period7_label  = get_period_label(7,  p2_data.get('daily'))
    period28_label = get_period_label(28, p2_data.get('daily'))

    # 今年度ラベル
    if dept_rows_fy:
        _p2daily = p2_data.get('daily')
        _max_d   = _p2daily['手術実施日'].max().date() if _p2daily is not None else datetime.date.today()
        _days    = dept_rows_fy[0].get('actual_days', 1)
        _start   = _max_d - datetime.timedelta(days=_days - 1)
        period_fy_label = f"{_start} 〜 {_max_d}"
    else:
        period_fy_label = fy_label

    # 昨年度タブボタンHTML（f-string内でバックスラッシュ不可のため事前生成）
    if _has_prev_year:
        _py_btn_html = (
            '<button class="dept-tab-btn" id="dept-btn-py"'
            ' onclick="switchDeptTab(\'py\')">昨年度<br>'
            f'<small>{prev_year_fy_label}</small></button>'
        )
    else:
        _py_btn_html = ''

    # 診療科別推移グラフ用データ
    import json as _json2
    _dept_chart_json = dept_chart_json if dept_chart_json else '{}'
    try:
        _dept_list = list(_json2.loads(_dept_chart_json).keys())
    except Exception:
        _dept_list = []
    _dept_initial = _dept_list[0] if _dept_list else ''
    _dept_initial_escaped = _dept_initial.replace("'", "\\'")
    _dept_initial_json = f"'{_dept_initial_escaped}'"
    # 診療科選択ボタンHTML（f-string外で生成）
    _dept_btn_rows = ''
    for _d in _dept_list:
        _active = ' active' if _d == _dept_initial else ''
        # シングルクォートで囲み、HTML属性のダブルクォートと衝突させない
        _dept_escaped = _d.replace("'", "\\'")
        _dept_btn_rows += f"<button class=\"dc-btn{_active}\" onclick=\"selectDept(this,'{_dept_escaped}')\">{_d}</button>"

    html_template = f"""
    <!DOCTYPE html>
    <html lang="ja">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=yes">
        <title>🏥 全身麻酔手術レポート年度版</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/hammer.js/2.0.8/hammer.min.js"></script>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/chartjs-plugin-zoom/2.0.1/chartjs-plugin-zoom.min.js"></script>
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@300;400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
        <style>
            /* ══ DESIGN TOKENS (app_department準拠) ══ */
            :root {{
                --bg-page:      #F0F2F5;
                --bg-card:      #FFFFFF;
                --bg-elevated:  #F7F9FC;
                --bg-header:    #1D2B3A;
                --border:       #DCE1E9;
                --border-mid:   #C4CCD8;
                --accent:       #3A6EA5;
                --accent-light: #EEF3FA;
                --accent-hover: #2F5A8A;
                --text-primary:   #1A2535;
                --text-secondary: #5A6A82;
                --text-muted:     #94A3B8;
                --success:      #1A9E6A;
                --success-bg:   #EDFAF4;
                --warning:      #C87A00;
                --warning-bg:   #FEF6E4;
                --danger:       #C0293B;
                --danger-bg:    #FEF0F2;
                --bar-achieved: #1A9E6A;
                --bar-near:     #E09B00;
                --bar-below:    #D93650;
                --font-ui:   'Noto Sans JP', sans-serif;
                --font-mono: 'IBM Plex Mono', monospace;
                --r-sm: 6px; --r-md: 10px; --r-lg: 14px;
            }}
            * {{ box-sizing: border-box; margin: 0; padding: 0; }}
            body {{
                font-family: var(--font-ui);
                background: var(--bg-page);
                color: var(--text-primary);
                padding: 0;
                margin: 0;
                line-height: 1.6;
                touch-action: pan-y;
            }}
            ::-webkit-scrollbar {{ width: 6px; }}
            ::-webkit-scrollbar-track {{ background: var(--bg-page); }}
            ::-webkit-scrollbar-thumb {{ background: var(--border-mid); border-radius: 3px; }}

            /* ══ SITE HEADER ══ */
            .site-header {{
                background: var(--bg-header);
                color: #E8EEF5;
                padding: 0 40px;
                height: 64px;
                display: flex;
                align-items: center;
                justify-content: space-between;
                position: sticky;
                top: 0;
                z-index: 200;
                border-bottom: 2px solid var(--accent);
                margin-bottom: 0;
            }}
            .header-brand {{ display: flex; align-items: center; gap: 14px; }}
            .header-badge {{
                background: var(--accent); color: #fff;
                font-size: 0.62rem; font-weight: 700;
                letter-spacing: 0.14em; text-transform: uppercase;
                padding: 3px 9px; border-radius: 4px;
            }}
            .header-title {{ font-size: 1.05rem; font-weight: 600; color: #E8EEF5; }}
            .header-meta-row {{
                display: flex; align-items: center; gap: 20px;
                font-size: 0.75rem; color: #7A90A8;
                font-family: var(--font-mono); margin-left: 20px;
            }}
            .header-meta-row span {{ display: flex; align-items: center; gap: 5px; }}
            .header-actions {{ display: flex; gap: 8px; }}

            /* ══ CONTAINER ══ */
            .page-container {{ max-width: 1280px; margin: 0 auto; padding: 28px 32px; }}

            /* レガシー header クラス（非表示にして site-header に置き換え） */
            .header {{ display: none !important; }}

            /* ══ BUTTONS ══ */
            .btn-header {{
                display: inline-flex; align-items: center; gap: 6px;
                padding: 7px 16px; border-radius: var(--r-sm);
                font-size: 0.8rem; font-weight: 500; cursor: pointer;
                transition: all 0.15s; text-decoration: none;
                border: 1px solid transparent; font-family: var(--font-ui); line-height: 1;
            }}
            .btn-outline {{
                background: transparent; color: #AAB8C8; border-color: #3A4E62;
            }}
            .btn-outline:hover {{ background: rgba(255,255,255,0.06); color: #E8EEF5; border-color: #6A8098; }}
            .btn-solid {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
            .btn-solid:hover {{ background: var(--accent-hover); }}
            /* 旧ボタンクラスも新スタイルに合わせる */
            .portal-btn {{
                display: inline-flex; align-items: center; gap: 6px;
                padding: 7px 16px; border-radius: var(--r-sm);
                font-size: 0.8rem; font-weight: 500; cursor: pointer;
                transition: all 0.15s; text-decoration: none;
                border: 1px solid var(--accent);
                background: var(--accent); color: #fff;
            }}
            .portal-btn:hover {{ background: var(--accent-hover); }}
            .print-btn {{
                display: inline-flex; align-items: center; gap: 6px;
                padding: 7px 16px; border-radius: var(--r-sm);
                font-size: 0.8rem; font-weight: 500; cursor: pointer;
                transition: all 0.15s; text-decoration: none;
                border: 1px solid #3A4E62;
                background: transparent; color: #AAB8C8;
            }}
            .print-btn:hover {{ background: rgba(255,255,255,0.06); color: #E8EEF5; border-color: #6A8098; }}

            /* ══ TAB BAR (モード切替) ══ */
            .tab-bar-wrap {{
                padding: 20px 32px 0;
                max-width: 1280px;
                margin: 0 auto;
            }}
            .tab-bar {{
                display: flex; gap: 2px;
                background: var(--bg-card); border: 1px solid var(--border);
                border-radius: var(--r-md); padding: 4px; margin-bottom: 20px;
                width: fit-content; box-shadow: 0 1px 3px rgba(0,0,0,0.06);
            }}
            .tab-btn {{
                padding: 8px 20px; border-radius: var(--r-sm);
                font-size: 0.82rem; font-weight: 500; cursor: pointer;
                border: 1px solid transparent; background: transparent;
                color: var(--text-secondary); transition: all 0.15s;
                font-family: var(--font-ui); display: flex; align-items: center; gap: 7px;
            }}
            .tab-btn.active {{
                background: var(--accent); color: #fff; border-color: var(--accent);
                box-shadow: 0 1px 4px rgba(58,110,165,0.25);
            }}
            .tab-btn:hover:not(.active) {{ background: var(--accent-light); color: var(--accent); }}

            /* スマホタブナビ */
            .tab-navigation {{
                display: flex; gap: 8px;
                margin: 0 32px 16px;
                max-width: 1280px;
            }}
            .tab-button {{
                flex: 1; padding: 10px; background: var(--bg-card);
                border: 1px solid var(--border); border-radius: var(--r-md);
                font-weight: 600; font-size: clamp(0.82rem, 2vw, 0.9rem);
                cursor: pointer; transition: all 0.2s;
                color: var(--text-secondary); font-family: var(--font-ui);
            }}
            .tab-button.active {{
                background: var(--accent); color: #fff; border-color: var(--accent);
            }}
            @media (min-width: 768px) {{ .tab-navigation {{ display: none; }} }}

            /* ══ MAIN LAYOUT ══ */
            .container {{
                display: flex;
                flex-direction: column;
                gap: 20px;
                max-width: 1280px;
                margin: 0 auto;
                padding: 0 32px 32px;
            }}
            @media (min-width: 768px) {{ .container {{ flex-direction: row; }} }}

            /* ══ COLUMN CARDS ══ */
            .column {{
                flex: 1; min-width: 0;
                border-radius: var(--r-lg);
                page-break-inside: avoid;
                overflow: hidden;
                border: 1px solid var(--border);
                background: var(--bg-card);
                box-shadow: 0 1px 4px rgba(0,0,0,0.06);
            }}
            @media (max-width: 767px) {{
                .column {{ display: none; }}
                .column.active {{ display: block; }}
            }}
            @media (min-width: 768px) {{
                .column.period-2 {{ order: 1; }}
                .column.period-1 {{ order: 2; }}
            }}

            /* 期間1（昨年度）: 控えめスタイル */
            .column.period-1 {{
                opacity: 0.75;
                filter: grayscale(15%);
                transition: opacity 0.3s, filter 0.3s;
            }}
            .column.period-1:hover {{ opacity: 0.92; filter: grayscale(5%); }}

            /* 期間2（今年度）: アクセントスタイル */
            .column.period-2 {{
                border-color: var(--accent);
                border-width: 2px;
                box-shadow: 0 4px 16px rgba(58,110,165,0.14);
                position: relative;
            }}
            .column.period-2::before {{
                content: "✦ 最新期間";
                position: absolute;
                top: -11px; left: 14px;
                background: var(--accent);
                color: #fff;
                font-size: 0.68rem; font-weight: 700;
                padding: 2px 10px; border-radius: 4px;
                letter-spacing: 0.08em;
            }}

            .col-title {{
                background: var(--bg-header);
                color: #E8EEF5;
                padding: 12px 16px;
                text-align: center;
                font-weight: 600;
                font-size: clamp(0.88rem, 2.5vw, 1rem);
            }}
            .column.period-2 .col-title {{
                background: var(--accent);
            }}

            .col-subtitle {{
                text-align: center;
                font-size: clamp(0.72rem, 1.8vw, 0.82rem);
                color: var(--text-secondary);
                padding: 6px 16px;
                background: var(--bg-elevated);
                border-bottom: 1px solid var(--border);
            }}
            .trend-badge {{
                display: inline-block; padding: 2px 10px;
                border-radius: 4px; font-size: 0.78rem;
                font-weight: 600; margin-left: 5px;
            }}
            .trend-badge.up {{ background: var(--success-bg); color: var(--success); }}
            .trend-badge.down {{ background: var(--danger-bg); color: var(--danger); }}
            .trend-badge.flat {{ background: var(--warning-bg); color: var(--warning); }}

            /* ══ KPI GRID ══ */
            .grid {{
                display: grid;
                grid-template-columns: repeat(2, 1fr);
                background: var(--border);
                gap: 1px;
                border-bottom: 1px solid var(--border);
            }}
            .metric {{
                background: var(--bg-card);
                padding: 14px 16px;
                text-align: center;
                cursor: pointer;
                transition: background 0.15s;
            }}
            .column.period-2 .metric {{ background: var(--bg-elevated); }}
            .metric:hover {{ background: var(--accent-light); }}
            .metric.alert {{ background: var(--danger-bg); }}
            .metric-label {{
                font-size: 0.68rem; font-weight: 700;
                letter-spacing: 0.08em; text-transform: uppercase;
                color: var(--text-muted); margin-bottom: 5px;
            }}
            .metric-value {{
                font-size: clamp(1.05rem, 2.8vw, 1.5rem);
                font-weight: 700;
                font-family: var(--font-mono);
                color: var(--text-primary); line-height: 1.1;
            }}
            .metric-value.success {{ color: var(--success); }}
            .metric-value.danger  {{ color: var(--danger); }}

            .data-info {{
                background: var(--accent-light);
                padding: 8px 16px;
                font-size: clamp(0.72rem, 1.8vw, 0.82rem);
                color: var(--accent);
                text-align: center;
                border-bottom: 1px solid var(--border);
                font-weight: 500;
            }}

            /* ══ CHART BOX ══ */
            .chart-box {{
                background: var(--bg-card);
                padding: 16px;
                margin: 0;
                height: 320px;
                position: relative;
                page-break-inside: avoid;
                border-top: 1px solid var(--border);
            }}
            @media (min-width: 768px) {{ .chart-box {{ height: 360px; }} }}
            .chart-box canvas {{
                position: absolute; top: 44px; left: 16px; right: 16px; bottom: 16px;
                width: calc(100% - 32px) !important;
                height: calc(100% - 60px) !important;
                touch-action: pan-y pinch-zoom;
            }}
            .chart-title {{
                font-weight: 600;
                margin-bottom: 8px;
                font-size: clamp(0.82rem, 2vw, 0.92rem);
                color: var(--text-primary);
                position: relative; z-index: 1;
            }}
            .chart-hint {{
                font-size: clamp(0.62rem, 1.5vw, 0.72rem);
                color: var(--text-muted); margin-top: 3px;
            }}

            /* ══ 比較サマリー ══ */
            .comparison-summary {{
                max-width: 1280px; margin: 0 auto 20px;
                background: var(--bg-card);
                padding: 20px 24px;
                border-radius: var(--r-lg);
                border: 1px solid var(--border);
                box-shadow: 0 1px 4px rgba(0,0,0,0.06);
            }}
            .comparison-title {{
                font-size: clamp(0.9rem, 2.5vw, 1.1rem);
                font-weight: 700;
                margin-bottom: 14px;
                color: var(--text-primary);
            }}
            .comparison-grid {{
                display: grid; grid-template-columns: 1fr; gap: 10px;
            }}
            @media (min-width: 640px) {{ .comparison-grid {{ grid-template-columns: repeat(2, 1fr); }} }}
            @media (min-width: 1024px) {{ .comparison-grid {{ grid-template-columns: repeat(3, 1fr); }} }}
            .comparison-item {{
                background: var(--bg-elevated); padding: 12px 14px;
                border-radius: var(--r-sm);
                border-left: 3px solid var(--accent);
            }}
            .comparison-item.positive {{ border-left-color: var(--success); }}
            .comparison-item.negative {{ border-left-color: var(--danger); }}
            .comparison-item-label {{
                font-size: clamp(0.7rem, 1.8vw, 0.8rem);
                color: var(--text-secondary); margin-bottom: 4px;
                font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em;
            }}
            .comparison-item-value {{
                font-size: clamp(0.95rem, 2.5vw, 1.1rem);
                font-weight: 700; font-family: var(--font-mono);
                display: flex; align-items: center; gap: 8px;
                color: var(--text-primary);
            }}
            .diff-badge {{
                font-size: clamp(0.68rem, 1.5vw, 0.78rem);
                padding: 2px 8px; border-radius: 4px;
                background: var(--border); color: var(--text-secondary);
                font-family: var(--font-ui);
            }}
            .diff-badge.positive {{ background: var(--success-bg); color: var(--success); }}
            .diff-badge.negative {{ background: var(--danger-bg); color: var(--danger); }}

            /* ══ モード切替バー ══ */
            .mode-switch-bar {{
                max-width: 1280px; margin: 0 auto 0;
                display: flex; align-items: center; gap: 8px;
                padding: 16px 32px 16px;
            }}
            .mode-label {{
                font-size: 0.78rem; color: var(--text-secondary);
                font-weight: 700; white-space: nowrap;
            }}
            .mode-btn {{
                padding: 7px 18px; border-radius: var(--r-sm);
                border: 1px solid var(--border); background: var(--bg-card);
                color: var(--text-secondary);
                font-weight: 500; font-size: 0.82rem; cursor: pointer;
                transition: all 0.15s; font-family: var(--font-ui);
            }}
            .mode-btn.active {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
            .mode-btn:hover:not(.active) {{ background: var(--accent-light); color: var(--accent); border-color: var(--accent); }}
            @media print {{ .mode-switch-bar {{ display:none; }} }}

            /* ══ 診療科パフォーマンスセクション ══ */
            .dept-section {{
                max-width: 1280px; margin: 0 auto 24px;
                background: var(--bg-card);
                padding: 20px 24px;
                border-radius: var(--r-lg);
                border: 1px solid var(--border);
                box-shadow: 0 1px 4px rgba(0,0,0,0.06);
            }}

            /* ══ 診療科別推移グラフセクション ══ */
            .dc-chart-section {{
                max-width: 1280px; margin: 0 auto 24px;
                background: var(--bg-card);
                padding: 20px 24px;
                border-radius: var(--r-lg);
                border: 1px solid var(--border);
                box-shadow: 0 1px 4px rgba(0,0,0,0.06);
            }}
            .dc-chart-title {{
                font-size: clamp(0.9rem, 2.5vw, 1.05rem);
                font-weight: 700; color: var(--text-primary); margin-bottom: 4px;
            }}
            .dc-chart-subtitle {{
                font-size: clamp(0.7rem, 1.6vw, 0.78rem);
                color: var(--text-muted); margin-bottom: 14px;
            }}
            .dc-btn-wrap {{
                display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 16px;
            }}
            .dc-btn {{
                padding: 5px 14px; border-radius: var(--r-sm);
                border: 1px solid var(--border); background: var(--bg-elevated);
                color: var(--text-secondary); font-weight: 500; font-size: 0.78rem;
                cursor: pointer; transition: all 0.15s; font-family: var(--font-ui);
            }}
            .dc-btn.active {{
                background: var(--accent); color: #fff; border-color: var(--accent);
            }}
            .dc-btn:hover:not(.active) {{
                background: var(--accent-light); color: var(--accent);
            }}
            .dc-canvas-wrap {{
                position: relative; height: 280px;
            }}
            .dept-section-title {{
                font-size: clamp(0.9rem, 2.5vw, 1.05rem);
                font-weight: 700;
                color: var(--text-primary); margin-bottom: 4px;
            }}
            .dept-section-subtitle {{
                font-size: clamp(0.7rem, 1.6vw, 0.78rem);
                color: var(--text-muted); margin-bottom: 14px;
            }}
            .dept-tabs {{
                display: flex; gap: 4px; margin-bottom: 16px;
            }}
            .dept-tab-btn {{
                padding: 6px 16px; border-radius: var(--r-sm);
                border: 1px solid var(--border); background: var(--bg-elevated);
                color: var(--text-secondary);
                font-weight: 500; font-size: 0.8rem; cursor: pointer;
                transition: all 0.15s; font-family: var(--font-ui);
            }}
            .dept-tab-btn.active {{
                background: var(--accent); color: #fff; border-color: var(--accent);
            }}
            .dept-tab-btn:hover:not(.active) {{
                background: var(--accent-light); color: var(--accent);
            }}
            .dept-panel {{ display: none; }}
            .dept-panel.active {{ display: block; }}

            .dept-row {{
                display: grid;
                grid-template-columns: 130px 180px 1fr 56px 28px;
                align-items: center;
                gap: 14px;
                padding: 8px 0;
                border-bottom: 1px solid var(--border);
            }}
            .dept-row:last-child {{ border-bottom: none; }}
            .dept-row:hover {{ background: var(--bg-elevated); border-radius: var(--r-sm); }}
            .dept-name {{
                font-size: 0.76rem; font-weight: 600;
                padding: 3px 10px; border-radius: 4px; text-align: center;
                white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
            }}
            .dept-name.achieved {{ background: var(--success-bg); color: var(--success); }}
            .dept-name.near     {{ background: var(--warning-bg); color: var(--warning); }}
            .dept-name.below    {{ background: var(--danger-bg);  color: var(--danger);  }}
            .dept-act-tgt {{
                font-family: var(--font-mono); font-size: 0.76rem;
                color: var(--text-secondary); white-space: nowrap;
            }}
            .dept-bar-wrap {{
                position: relative; height: 7px;
                background: var(--bg-elevated); border-radius: 4px;
                border: 1px solid var(--border); overflow: visible;
            }}
            .dept-bar-fill {{
                position: absolute; left: 0; top: -1px;
                height: calc(100% + 2px); border-radius: 3px;
                transition: width 0.6s cubic-bezier(.22,.68,0,1.1);
                min-width: 2px;
            }}
            .dept-bar-fill.achieved {{ background: var(--bar-achieved); }}
            .dept-bar-fill.near     {{ background: var(--bar-near); }}
            .dept-bar-fill.below    {{ background: var(--bar-below); }}
            .dept-target-line {{
                position: absolute; top: -5px; width: 2px; height: 17px;
                background: var(--accent); border-radius: 1px; opacity: 0.7;
                z-index: 2; pointer-events: none;
            }}
            .dept-rate {{
                font-family: var(--font-mono); font-size: 0.86rem;
                font-weight: 700; text-align: right; color: var(--text-primary);
                white-space: nowrap;
            }}
            .dept-badge {{ width: 8px; height: 8px; border-radius: 50%; margin: auto; }}
            .dept-badge.achieved {{ background: var(--success); }}
            .dept-badge.near     {{ background: var(--warning); }}
            .dept-badge.below    {{ background: var(--danger); }}
            .dept-legend {{
                display: flex; gap: 16px; margin-top: 10px;
                font-size: 0.72rem; color: var(--text-muted); flex-wrap: wrap;
            }}
            .legend-item {{ display: flex; align-items: center; gap: 6px; }}
            .legend-dot {{ width: 7px; height: 7px; border-radius: 50%; }}

            /* ══ ローディング ══ */
            .loading {{
                position: fixed; top: 50%; left: 50%;
                transform: translate(-50%, -50%);
                background: rgba(255,255,255,0.95);
                padding: 20px 40px; border-radius: var(--r-lg);
                box-shadow: 0 4px 20px rgba(0,0,0,0.15);
                z-index: 1000; display: none;
                font-family: var(--font-ui); color: var(--text-primary);
            }}
            .loading.show {{ display: block; }}

            /* ══ 印刷 ══ */
            @media print {{
                .site-header {{ position: static; }}
                .column.period-1 {{ opacity: 1 !important; filter: none !important; }}
                .column.period-2 {{ border: 2px solid var(--accent) !important; box-shadow: none !important; }}
                .comparison-summary, .dept-section {{ box-shadow: none; break-inside: avoid; }}
                .portal-btn, .print-btn, .tab-navigation {{ display: none; }}
                .container {{ flex-direction: row !important; }}
                .chart-box {{ height: 300px; }}
                .dept-tabs {{ display: none; }}
                .dept-panel {{ display: block !important; }}
                @page {{ margin: 15mm; }}
            }}

            /* ══ レスポンシブ ══ */
            @media (max-width: 768px) {{
                .site-header {{ padding: 0 16px; height: auto; min-height: 56px; flex-wrap: wrap; gap: 8px; padding: 8px 16px; }}
                .header-meta-row {{ display: none; }}
                .container {{ padding: 0 16px 24px; }}
                .page-container {{ padding: 16px; }}
                .mode-switch-bar {{ padding: 12px 16px; }}
                .comparison-summary {{ margin: 0 16px 16px; }}
                .dept-section {{ margin: 0 16px 16px; }}
                .dept-row {{ grid-template-columns: 100px 1fr 46px 20px; }}
                .dept-act-tgt {{ display: none; }}
            }}
        </style>
    </head>
    <body>
        <div class="loading" id="loading">読み込み中...</div>

        <!-- ══ SITE HEADER ══ -->
        <header class="site-header">
            <div class="header-brand">
                <div class="header-badge">全身麻酔手術</div>
                <div class="header-title">🏥 全身麻酔手術レポート年度版</div>
                <div class="header-meta-row">
                    <span>&#128202; {mode_label}</span>
                    <span>&#128197; 最新: {latest_data_date_str}</span>
                    <span>&#128336; 出力: {now_str}</span>
                </div>
            </div>
            <div class="header-actions">
                <button onclick="window.print()" class="print-btn btn-header btn-outline">&#128424; 印刷</button>
                <a href="{portal_url}" class="portal-btn btn-header btn-solid">&#8593; ポータルに戻る</a>
            </div>
        </header>

        <!-- 旧ヘッダー（CSSで非表示） -->
        <div class="header" style="display:none">
            <h1>🏥 全身麻酔手術レポート年度版</h1>
            <p>分析対象: <strong>{mode_label}</strong></p>
            <div class="latest-data-date">📊 最新データ取得日: {latest_data_date_str}</div>
            <div class="update-time">🕐 レポート作成日時: {now_str}</div>
        </div>

        <!-- モード切替バー -->
        <div class="mode-switch-bar" id="mode-switch-bar" style="{'display:flex' if has_dual else 'display:none'}">
            <span class="mode-label">&#128197; 比較モード:</span>
            <button class="mode-btn {'active' if not _init_is_rolling else ''}" id="btn-annual"  onclick="switchMode('annual')">&#128197; 年度比較</button>
            <button class="mode-btn {'active' if     _init_is_rolling else ''}" id="btn-rolling" onclick="switchMode('rolling')">&#128260; 直近365日</button>
        </div>

        <!-- タブナビゲーション（スマホのみ表示） -->
        <div class="tab-navigation">
            <button class="tab-button active" onclick="switchTab('period2')">
                📅 今年度（最新）
            </button>
            <button class="tab-button" onclick="switchTab('period1')">
                📅 昨年度（比較）
            </button>
        </div>

        <div class="container">
            <!-- 今年度（PCでは左、スマホではデフォルト表示） -->
            <div class="column period-2 active">
                <div class="col-title">📅 今年度: {p2_data.get('title', '')}</div>
                <div class="col-subtitle">
                    トレンド: <span class="trend-badge {'up' if '上昇' in p2_trend else 'down' if '下降' in p2_trend else 'flat'}">{p2_arrow} {p2_trend}</span>
                </div>
                <div class="data-info">
                    📋 分析対象: {p2_count}平日のデータ
                </div>
                <div class="grid">
                    <div class="metric {'alert' if mode_label == '全身麻酔のみ' and p2_data.get('achieve', 0) < 1.0 else ''}">
                        <div class="metric-label">平日平均件数</div>
                        <div class="metric-value {('danger' if p2_data.get('achieve', 0) < 1.0 else '') if mode_label == '全身麻酔のみ' else ''}">{p2_data.get('avg_v', 0):.1f} 件</div>
                    </div>
                    <div class="metric {'alert' if mode_label == '全身麻酔のみ' and p2_data.get('achieve', 0) < 1.0 else ''}">
                        <div class="metric-label">{'目標達成率' if mode_label == '全身麻酔のみ' else '実施件数'}</div>
                        <div class="metric-value {('success' if p2_data.get('achieve', 0) >= 1.0 else 'danger') if mode_label == '全身麻酔のみ' else ''}">
                            {'%.1f%%' % (p2_data.get('achieve', 0)*100) if mode_label == '全身麻酔のみ' else '%.0f 件' % p2_data.get('avg_v', 0)}
                        </div>
                    </div>
                    <div class="metric">
                        <div class="metric-label">{p2_data.get('month', 1)}月 予測</div>
                        <div class="metric-value">{int(p2_data.get('m_val', 0))} 件</div>
                    </div>
                    <div class="metric">
                        <div class="metric-label">年度 予測</div>
                        <div class="metric-value">{int(p2_data.get('fy_val', 0))} 件</div>
                    </div>
                    <div class="metric">
                        <div class="metric-label">時間内稼働率</div>
                        <div class="metric-value">{p2_data.get('util', 0):.1%}</div>
                    </div>
                    <div class="metric">
                        <div class="metric-label">時間外比率</div>
                        <div class="metric-value">{p2_data.get('over', 0):.1%}</div>
                    </div>
                </div>
                <div class="chart-box">
                    <div class="chart-title">📈 推移グラフ (移動平均)
                        <div class="chart-hint">💡 スマホ: ピンチで拡大、ダブルタップでリセット</div>
                    </div>
                    <canvas id="chart2"></canvas>
                </div>
            </div>
            
            <!-- 昨年度（PCでは右、スマホでは非表示） -->
            <div class="column period-1">
                <div class="col-title">📅 昨年度: {p1_data.get('title', '')}</div>
                <div class="col-subtitle">
                    トレンド: <span class="trend-badge {'up' if '上昇' in p1_trend else 'down' if '下降' in p1_trend else 'flat'}">{p1_arrow} {p1_trend}</span>
                </div>
                <div class="data-info">
                    📋 分析対象: {p1_count}平日のデータ
                </div>
                <div class="grid">
                    <div class="metric">
                        <div class="metric-label">平日平均件数</div>
                        <div class="metric-value">{p1_data.get('avg_v', 0):.1f} 件</div>
                    </div>
                    <div class="metric {'alert' if mode_label == '全身麻酔のみ' and p1_data.get('achieve', 0) < 1.0 else ''}">
                        <div class="metric-label">{'目標達成率' if mode_label == '全身麻酔のみ' else '実施件数'}</div>
                        <div class="metric-value {('success' if p1_data.get('achieve', 0) >= 1.0 else 'danger') if mode_label == '全身麻酔のみ' else ''}">
                            {'%.1f%%' % (p1_data.get('achieve', 0)*100) if mode_label == '全身麻酔のみ' else '%.0f 件' % p1_data.get('avg_v', 0)}
                        </div>
                    </div>
                    <div class="metric">
                        <div class="metric-label">{p1_data.get('month', 1)}月 実績</div>
                        <div class="metric-value">{int(p1_data.get('m_val', 0))} 件</div>
                    </div>
                    <div class="metric">
                        <div class="metric-label">年度 実績</div>
                        <div class="metric-value">{int(p1_data.get('fy_val', 0))} 件</div>
                    </div>
                    <div class="metric">
                        <div class="metric-label">時間内稼働率</div>
                        <div class="metric-value">{p1_data.get('util', 0):.1%}</div>
                    </div>
                    <div class="metric">
                        <div class="metric-label">時間外比率</div>
                        <div class="metric-value">{p1_data.get('over', 0):.1%}</div>
                    </div>
                </div>
                <div class="chart-box">
                    <div class="chart-title">📈 推移グラフ (移動平均)
                        <div class="chart-hint">💡 スマホ: ピンチで拡大、ダブルタップでリセット</div>
                    </div>
                    <canvas id="chart1"></canvas>
                </div>
            </div>
        </div>
        
        <!-- 期間比較サマリー（今年度・昨年度データの下） -->
        <div class="comparison-summary">
            <div class="comparison-title">📊 期間比較サマリー（今年度 vs 昨年度）</div>
            <div class="comparison-grid">
                <div class="comparison-item {'positive' if avg_diff > 0 else 'negative' if avg_diff < 0 else ''}">
                    <div class="comparison-item-label">平日平均件数の変化</div>
                    <div class="comparison-item-value">
                        {avg_diff:+.1f} 件
                        <span class="diff-badge {'positive' if avg_diff > 0 else 'negative' if avg_diff < 0 else ''}">{avg_pct}</span>
                    </div>
                </div>
                <div class="comparison-item {'positive' if m_diff > 0 else 'negative' if m_diff < 0 else ''}">
                    <div class="comparison-item-label">月次実績の変化</div>
                    <div class="comparison-item-value">
                        {int(m_diff):+d} 件
                        <span class="diff-badge {'positive' if m_diff > 0 else 'negative' if m_diff < 0 else ''}">{m_pct}</span>
                    </div>
                </div>
                <div class="comparison-item {'positive' if fy_diff > 0 else 'negative' if fy_diff < 0 else ''}">
                    <div class="comparison-item-label">年度予測の変化</div>
                    <div class="comparison-item-value">
                        {int(fy_diff):+d} 件
                        <span class="diff-badge {'positive' if fy_diff > 0 else 'negative' if fy_diff < 0 else ''}">{fy_pct}</span>
                    </div>
                </div>
                <div class="comparison-item {'positive' if util_diff > 0 else 'negative' if util_diff < 0 else ''}">
                    <div class="comparison-item-label">稼働率の変化</div>
                    <div class="comparison-item-value">
                        {util_diff:+.1%}
                        <span class="diff-badge {'positive' if util_diff > 0 else 'negative' if util_diff < 0 else ''}">{util_pct}</span>
                    </div>
                </div>
                <div class="comparison-item {'negative' if over_diff > 0 else 'positive' if over_diff < 0 else ''}">
                    <div class="comparison-item-label">時間外比率の変化</div>
                    <div class="comparison-item-value">
                        {over_diff:+.1%}
                        <span class="diff-badge {'negative' if over_diff > 0 else 'positive' if over_diff < 0 else ''}">{over_pct}</span>
                    </div>
                </div>
                <div class="comparison-item">
                    <div class="comparison-item-label">分析対象</div>
                    <div class="comparison-item-value">
                        {mode_label}
                    </div>
                </div>
            </div>
        </div>
        
        <script>
            // ── 両モードデータ埋め込み ──
            const DUAL_MODE = {'true' if has_dual else 'false'};
            const modeData = {{
                annual: {{
                    p2_chart: {ann_p2_json}, p1_chart: {ann_p1_json},
                    p2: {ann_p2_m}, p1: {ann_p1_m},
                    fy_label: {annual_fy_label!r},
                }},
                rolling: {{
                    p2_chart: {rol_p2_json}, p1_chart: {rol_p1_json},
                    p2: {rol_p2_m}, p1: {rol_p1_m},
                    fy_label: {rolling_fy_label!r},
                }},
            }};
            let currentMode = '{'rolling' if _init_is_rolling else 'annual'}';
            let chart1Inst = null, chart2Inst = null;

            function switchMode(mode) {{
                if (!DUAL_MODE) return;
                currentMode = mode;
                const d = modeData[mode];
                const isRolling = (mode === 'rolling');

                // ボタン状態
                document.getElementById('btn-annual') .classList.toggle('active', !isRolling);
                document.getElementById('btn-rolling').classList.toggle('active',  isRolling);

                // 期間タイトル
                const t2 = document.querySelector('.period-2 .col-title');
                const t1 = document.querySelector('.period-1 .col-title');
                if (t2) t2.textContent = '📅 ' + (isRolling?'期間2':'今年度') + ': ' + d.p2.title;
                if (t1) t1.textContent = '📅 ' + (isRolling?'期間1':'昨年度') + ': ' + d.p1.title;

                // 平日データ数
                const di2 = document.querySelector('.period-2 .data-info');
                const di1 = document.querySelector('.period-1 .data-info');
                if (di2) di2.textContent = '📋 分析対象: ' + d.p2.num_days + '平日のデータ';
                if (di1) di1.textContent = '📋 分析対象: ' + d.p1.num_days + '平日のデータ';

                // 指標更新
                function updateMetrics(colSel, m, isPeriod2) {{
                    const vals  = document.querySelectorAll(colSel + ' .metric-value');
                    const lbls  = document.querySelectorAll(colSel + ' .metric-label');
                    if (!vals.length) return;
                    vals[0].textContent = m.avg_v.toFixed(1) + ' 件';
                    vals[1].textContent = m.achieve.toFixed(1) + '%';
                    vals[2].textContent = m.m_val + ' 件';
                    vals[3].textContent = m.fy_val + ' 件';
                    vals[4].textContent = m.util.toFixed(1) + '%';
                    vals[5].textContent = m.over.toFixed(1) + '%';
                    if (lbls[2]) lbls[2].textContent = m.month + '月 ' + (isPeriod2 ? '予測着地' : '確定実績');
                    if (lbls[3]) lbls[3].textContent = isRolling ? '12ヶ月累計（実績）' : (isPeriod2 ? '年度 予測' : '年度 実績');
                }}
                updateMetrics('.period-2', d.p2, true);
                updateMetrics('.period-1', d.p1, false);

                // グラフ再描画
                if (chart1Inst) {{ chart1Inst.destroy(); chart1Inst = null; }}
                if (chart2Inst) {{ chart2Inst.destroy(); chart2Inst = null; }}
                chart1Inst = drawChart('chart1', d.p1_chart);
                chart2Inst = drawChart('chart2', d.p2_chart);

                // 診療科 fy パネル切替
                const fyAnn = document.getElementById('dept-panel-fy-annual');
                const fyRol = document.getElementById('dept-panel-fy-rolling');
                const fyDef = document.getElementById('dept-panel-fy');
                // fyタブが現在アクティブな場合のみ表示を切り替え
                const fyTabActive = document.getElementById('dept-btn-fy')?.classList.contains('active');
                if (fyAnn && fyRol) {{
                    fyAnn.style.display = (!isRolling && fyTabActive) ? 'block' : 'none';
                    fyRol.style.display  = ( isRolling && fyTabActive) ? 'block' : 'none';
                    if (fyDef) fyDef.style.display = 'none';
                }}
                // ボタンラベル変更
                const fyBtnLbl = document.getElementById('dept-fy-btn-label');
                const fyPrdLbl = document.getElementById('dept-fy-period-label');
                if (fyBtnLbl) fyBtnLbl.textContent = isRolling ? '直近365日' : '今年度';
                if (fyPrdLbl) fyPrdLbl.textContent = d.fy_label;

                // 比較サマリー更新
                updateSummary(d.p2, d.p1);
            }}

            function updateSummary(p2, p1) {{
                function diffCalc(a, b) {{
                    const v = a - b; const p = b===0 ? 0 : v/b*100;
                    return {{v, pct: (p>=0?'+':'')+p.toFixed(1)+'%'}};
                }}
                const diffs = [
                    diffCalc(p2.avg_v,  p1.avg_v),
                    diffCalc(p2.m_val,  p1.m_val),
                    diffCalc(p2.fy_val, p1.fy_val),
                    diffCalc(p2.util,   p1.util),
                    diffCalc(p2.over,   p1.over),
                ];
                const positiveGood = [true, true, true, true, false];
                const items = document.querySelectorAll('.comparison-item');
                diffs.forEach((d, i) => {{
                    if (i >= items.length - 1) return; // 最後のアイテム(分析対象)はスキップ
                    const el = items[i];
                    const vEl = el.querySelector('.comparison-item-value');
                    const bEl = el.querySelector('.diff-badge');
                    if (!vEl || !bEl) return;
                    const sign = d.v >= 0 ? '+' : '';
                    const dispVal = Number.isInteger(d.v) ? d.v : d.v.toFixed(2);
                    // テキストノードのみ更新（diffバッジは保持）
                    vEl.firstChild.textContent = sign + dispVal + '\xa0';
                    bEl.textContent = d.pct;
                    const pos = positiveGood[i] ? d.v > 0 : d.v < 0;
                    const neg = positiveGood[i] ? d.v < 0 : d.v > 0;
                    el.className = 'comparison-item' + (pos?' positive':neg?' negative':'');
                    bEl.className = 'diff-badge' + (pos?' positive':neg?' negative':'');
                }});
            }}

            // ローディング表示
            document.getElementById('loading').classList.add('show');
            
            // 「コ」型目標フレーム: ::before/::afterの幅をCSS変数から動的設定
            function applyKoFrame() {{
                document.querySelectorAll('.dept-target-line').forEach(el => {{
                    const pct = parseFloat(getComputedStyle(el).getPropertyValue('--tgt-pct'));
                    if (!isNaN(pct)) {{
                        const wrapW = el.parentElement.offsetWidth;
                        const lineX = wrapW * pct / 100;
                        el.style.setProperty('--ko-width', lineX + 'px');
                    }}
                }});
            }}
            // ::before/::after は CSS では動的幅をとれないため
            // dept-target-line の左辺から左端まで border-top/bottom を使う
            
            // 診療科タブ切り替え
            function switchDeptTab(w) {{
                document.querySelectorAll('.dept-tab-btn').forEach((b,i) => {{
                    b.classList.toggle('active',
                        (i===0 && w==='7') || (i===1 && w==='28') || (i===2 && w==='fy') || (i===3 && w==='py'));
                }});
                document.getElementById('dept-panel-7').classList.toggle('active',  w==='7');
                document.getElementById('dept-panel-28').classList.toggle('active', w==='28');
                // fyタブ: annual/rolling 両パネルの表示を currentMode で制御
                const isFy = (w === 'fy');
                const isPy = (w === 'py');
                const isRollingNow = (typeof currentMode !== 'undefined' && currentMode === 'rolling');
                const annPanel = document.getElementById('dept-panel-fy-annual');
                const rolPanel = document.getElementById('dept-panel-fy-rolling');
                const defPanel = document.getElementById('dept-panel-fy');
                const pyPanel  = document.getElementById('dept-panel-py');
                if (annPanel && rolPanel) {{
                    annPanel.style.display = (isFy && !isRollingNow) ? 'block' : 'none';
                    rolPanel.style.display = (isFy &&  isRollingNow) ? 'block' : 'none';
                    if (defPanel) defPanel.style.display = 'none';
                }} else {{
                    // フォールバック（dual モードなし）
                    if (defPanel) defPanel.style.display = isFy ? 'block' : 'none';
                }}
                if (pyPanel) pyPanel.style.display = isPy ? 'block' : 'none';
            }}
            
            // タブ切り替え機能
            function switchTab(period) {{
                const buttons = document.querySelectorAll('.tab-button');
                const columns = document.querySelectorAll('.column');
                
                buttons.forEach(btn => btn.classList.remove('active'));
                columns.forEach(col => col.classList.remove('active'));
                
                if (period === 'period2') {{
                    buttons[0].classList.add('active');
                    document.querySelector('.period-2').classList.add('active');
                }} else {{
                    buttons[1].classList.add('active');
                    document.querySelector('.period-1').classList.add('active');
                }}
            }}
            
            const targetVal = {target_val};
            
            function drawChart(ctxId, data) {{
                if (!data || data.length === 0) return;
                
                const datasets = [
                    {{
                        label: '実績',
                        data: data.map(d => d['件数']),
                        borderColor: 'rgba(200,200,200,0.5)',
                        backgroundColor: 'rgba(200,200,200,0.1)',
                        borderWidth: 1,
                        pointRadius: 2,
                        fill: false
                    }},
                    {{
                        label: '移動平均',
                        data: data.map(d => d.MA),
                        borderColor: '#1f77b4',
                        backgroundColor: 'rgba(31,119,180,0.1)',
                        borderWidth: 3,
                        pointRadius: 0,
                        fill: false,
                        tension: 0.3
                    }}
                ];
                
                if (targetVal !== null) {{
                    datasets.push({{
                        label: '目標(21件)', 
                        data: Array(data.length).fill(targetVal),
                        borderColor: 'red', 
                        borderDash: [5, 5], 
                        borderWidth: 2, 
                        pointRadius: 0, 
                        fill: false
                    }});
                }}
                
                const ctx = document.getElementById(ctxId);
                const chart = new Chart(ctx, {{
                    type: 'line',
                    data: {{ labels: data.map(d => d.date_label), datasets: datasets }},
                    options: {{ 
                        responsive: true, 
                        maintainAspectRatio: false,
                        interaction: {{
                            mode: 'index',
                            intersect: false
                        }},
                        plugins: {{ 
                            legend: {{ 
                                display: window.innerWidth > 480,
                                position: 'top',
                                labels: {{
                                    font: {{
                                        size: window.innerWidth > 480 ? 12 : 10
                                    }},
                                    usePointStyle: true,
                                    padding: 15
                                }}
                            }},
                            tooltip: {{
                                enabled: true,
                                mode: 'index',
                                intersect: false,
                                backgroundColor: 'rgba(0,0,0,0.8)',
                                titleFont: {{
                                    size: 14,
                                    weight: 'bold'
                                }},
                                bodyFont: {{
                                    size: 13
                                }},
                                padding: 12,
                                displayColors: true
                            }},
                            zoom: {{
                                pan: {{
                                    enabled: true,
                                    mode: 'x'
                                }},
                                zoom: {{
                                    wheel: {{
                                        enabled: false
                                    }},
                                    pinch: {{
                                        enabled: true
                                    }},
                                    mode: 'x'
                                }}
                            }}
                        }},
                        scales: {{
                            x: {{
                                ticks: {{
                                    font: {{
                                        size: window.innerWidth > 480 ? 11 : 9
                                    }},
                                    maxRotation: 45,
                                    minRotation: 45
                                }},
                                grid: {{
                                    display: false
                                }}
                            }},
                            y: {{
                                ticks: {{
                                    font: {{
                                        size: window.innerWidth > 480 ? 11 : 9
                                    }}
                                }},
                                grid: {{
                                    color: 'rgba(0,0,0,0.05)'
                                }}
                            }}
                        }}
                    }}
                }});
                
                // ダブルタップでズームリセット
                const canvas = document.getElementById(ctxId);
                let lastTap = 0;
                canvas.addEventListener('touchend', function(e) {{
                    const currentTime = new Date().getTime();
                    const tapLength = currentTime - lastTap;
                    if (tapLength < 500 && tapLength > 0) {{
                        chart.resetZoom();
                    }}
                    lastTap = currentTime;
                }});
                return chart;
            }}
            
            // チャートを描画
            setTimeout(() => {{
                chart1Inst = drawChart('chart1', {p1_json});
                chart2Inst = drawChart('chart2', {p2_json});
                document.getElementById('loading').classList.remove('show');
                // 診療科グラフ初期描画
                const _initDept = {_dept_initial_json};
                if (_initDept && typeof renderDeptChart === 'function') renderDeptChart(_initDept);
            }}, 100);

            // ── 診療科別推移グラフ（グローバルスコープ: onclick から呼ばれるため setTimeout 外） ──
            const DEPT_DATA = {_dept_chart_json};
            let dcChartInst = null;

            function selectDept(btn, dept) {{
                document.querySelectorAll('.dc-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                renderDeptChart(dept);
            }}

            function renderDeptChart(dept) {{
                const d = DEPT_DATA[dept];
                if (!d) return;

                const lbl = document.getElementById('dc-selected-label');
                if (lbl) lbl.textContent =
                    '【' + dept + '】　週次目標: ' + d.weekly_target + '件/週　｜　' +
                    d.cy_label + '（実線）  vs  ' + d.py_label + '（破線）　窓: ' + d.win_label;

                if (dcChartInst) {{ dcChartInst.destroy(); dcChartInst = null; }}
                const ctx = document.getElementById('dc-chart');
                if (!ctx) return;

                const cyLabels = d.cy.map(r => r.date_label);
                const pyLabels = d.py.map(r => r.date_label);
                const allLabels = cyLabels.length >= pyLabels.length ? cyLabels : pyLabels;
                const targetLine = Array(allLabels.length).fill(d.weekly_target);

                dcChartInst = new Chart(ctx, {{
                    type: 'line',
                    data: {{
                        labels: allLabels,
                        datasets: [
                            {{
                                label: '直近365日（' + d.win_label + '）',
                                data: d.cy.map(r => r.weekly_avg),
                                borderColor: '#3A6EA5',
                                backgroundColor: 'rgba(58,110,165,0.07)',
                                borderWidth: 2.5, pointRadius: 0, fill: false, tension: 0.3,
                            }},
                            {{
                                label: '前年同時期（' + d.win_label + '）',
                                data: d.py.map(r => r.weekly_avg),
                                borderColor: '#94A3B8',
                                backgroundColor: 'transparent',
                                borderWidth: 2, borderDash: [5, 4],
                                pointRadius: 0, fill: false, tension: 0.3,
                            }},
                            {{
                                label: '週次目標（' + d.weekly_target + '件）',
                                data: targetLine,
                                borderColor: 'rgba(220,38,38,0.8)',
                                backgroundColor: 'transparent',
                                borderWidth: 1.5, borderDash: [6, 3],
                                pointRadius: 0, fill: false,
                            }},
                        ]
                    }},
                    options: {{
                        responsive: true, maintainAspectRatio: false,
                        interaction: {{ mode: 'index', intersect: false }},
                        plugins: {{
                            legend: {{
                                display: true, position: 'top',
                                labels: {{ usePointStyle: true, padding: 16, font: {{ size: 11 }} }}
                            }},
                            tooltip: {{
                                enabled: true, mode: 'index', intersect: false,
                                backgroundColor: 'rgba(0,0,0,0.82)', padding: 10,
                                titleFont: {{ size: 12 }}, bodyFont: {{ size: 12 }},
                                callbacks: {{
                                    label: function(ctx) {{
                                        const v = ctx.parsed.y;
                                        if (v === null || v === undefined) return null;
                                        return ctx.dataset.label + ': ' + v.toFixed(1) + '件';
                                    }}
                                }}
                            }},
                        }},
                        scales: {{
                            x: {{
                                ticks: {{ font: {{ size: 10 }}, maxRotation: 45,
                                         minRotation: 45, maxTicksLimit: 26 }},
                                grid: {{ display: false }}
                            }},
                            y: {{
                                beginAtZero: true,
                                ticks: {{ font: {{ size: 11 }} }},
                                grid: {{ color: 'rgba(0,0,0,0.05)' }},
                                title: {{ display: true, text: '件数/週（移動平均）',
                                          font: {{ size: 11 }}, color: 'var(--text-muted)' }}
                            }},
                        }}
                    }}
                }});
            }}

            // メトリックにツールチップ機能（タップで詳細表示）
            document.querySelectorAll('.metric').forEach(metric => {{
                metric.addEventListener('click', function() {{
                    const label = this.querySelector('.metric-label').textContent;
                    const value = this.querySelector('.metric-value').textContent;
                    alert(`${{label}}\\n${{value}}`);
                }});
            }});
        </script>
        
        <!-- 診療科別推移グラフ -->
        {'<div class="dc-chart-section">' if _dept_list else ''}
        {'''
            <div class="dc-chart-title">📈 診療科別 全身麻酔件数推移</div>
            <div class="dc-chart-subtitle">直近365日 vs 前年同時期の7日累計推移（週次目標との比較）｜ 診療科ボタンで切り替え</div>
            <div class="dc-btn-wrap">''' + _dept_btn_rows + '''</div>
            <div id="dc-selected-label" style="font-size:0.8rem;color:var(--text-secondary);margin-bottom:8px;"></div>
            <div class="dc-canvas-wrap"><canvas id="dc-chart"></canvas></div>
        ''' if _dept_list else ''}
        {'</div>' if _dept_list else ''}

        <!-- 診療科別パフォーマンス（最下部） -->
        <div class="dept-section">
            <div class="dept-section-title">📊 診療科別 全身麻酔パフォーマンス</div>
            <div class="dept-section-subtitle">目標ファイル登録診療科のみ ｜ 🔵フレーム = 期間目標  ✅≥100%  🟡80〜99%  🔴&lt;80%</div>
            <div class="dept-tabs">
                <button class="dept-tab-btn active" onclick="switchDeptTab('7')">直近7日<br><small>{period7_label}</small></button>
                <button class="dept-tab-btn"        onclick="switchDeptTab('28')">直近28日<br><small>{period28_label}</small></button>
                <button class="dept-tab-btn"        id="dept-btn-fy" onclick="switchDeptTab('fy')">
                    <span id="dept-fy-btn-label">{'直近365日' if _init_is_rolling else '今年度'}</span><br>
                    <small id="dept-fy-period-label">{period_fy_label}</small>
                </button>
                {_py_btn_html}
            </div>
            <div class="dept-panel active" id="dept-panel-7">{dept7_html}</div>
            <div class="dept-panel" id="dept-panel-28">{dept28_html}</div>
            <!-- 今年度タブ: annual/rolling の両方がある場合は各パネルを使い分け -->
            <!-- annual パネル: 年度比較モード時に表示（初期は非表示、タブクリックで表示） -->
            <div class="dept-panel" id="dept-panel-fy-annual"
                 style="display:none">{ann_dept_fy_html}</div>
            <!-- rolling パネル: 直近365日モード時に表示（初期は非表示、タブクリックで表示） -->
            <div class="dept-panel" id="dept-panel-fy-rolling"
                 style="display:none">{rol_dept_fy_html}</div>
            <!-- dept-panel-fy: annual/rolling がない場合のフォールバック -->
            <div class="dept-panel" id="dept-panel-fy"
                 style="{'display:none' if _has_dual_dept else 'display:none'}">{dept_fy_html}</div>
            <!-- 昨年度パネル -->
            <div class="dept-panel" id="dept-panel-py" style="display:none">{py_dept_fy_html}</div>
        </div>
    </body>
    </html>
    """
    return html_template

# --- 4b. 術者別分析 HTML生成関数 ---

def build_surgeon_data(df_raw, start_date, end_date, analysis_target, dept_filter=None):
    """
    指定期間・分析対象・診療科フィルターで術者別データを集計。
    dept_filter: None=全科, list=指定科のみ
    戻り値: [{'name':..,'dept':..,'count':..,'hours':..}, ...]
    """
    df = df_raw.copy()
    if analysis_target == "全身麻酔のみ":
        df = df[df['is_ga']]
    df = df[df['is_biz']]
    df = df[(df['手術実施日'].dt.date >= start_date) & (df['手術実施日'].dt.date <= end_date)]
    if dept_filter:
        df = df[df['実施診療科'].isin(dept_filter)]

    if '実施術者' not in df.columns or df.empty:
        return []

    df_ex = df.dropna(subset=['実施術者']).copy()
    df_ex['names'] = df_ex['実施術者'].str.split(r'\r\n|\n')
    df_ex = df_ex.explode('names')
    df_ex['術者名'] = df_ex['names'].apply(normalize_str)
    df_ex = df_ex[df_ex['術者名'] != ""].drop_duplicates(['手術実施日', '術者名', '入室時刻'])
    if df_ex.empty:
        return []

    # 術者ごとの最頻診療科（フィルタ前の全データから計算して安定化）
    d_count = df_ex.groupby(['術者名', '実施診療科']).size().reset_index(name='c')
    d_map = dict(
        d_count.sort_values(['術者名', 'c'], ascending=[True, False])
        .drop_duplicates('術者名')[['術者名', '実施診療科']].values
    )

    op_sum = df_ex.groupby('術者名').agg(
        参加件数=('手術実施日', 'count'),
        total_m=('total_m', 'sum')
    ).reset_index()
    op_sum['総手術時間'] = (op_sum['total_m'] / 60).round(1)
    op_sum['診療科'] = op_sum['術者名'].map(d_map).fillna('不明')

    return [
        {'name': r['術者名'], 'dept': r['診療科'],
         'count': int(r['参加件数']), 'hours': float(r['総手術時間'])}
        for _, r in op_sum.iterrows()
    ]


def create_surgeon_html_report(df_raw,
                               ann_p2_start, ann_p2_end,
                               ann_p1_start, ann_p1_end,
                               rol_p2_start, rol_p2_end,
                               rol_p1_start, rol_p1_end,
                               portal_url,
                               initial_mode="年度比較",
                               max_surgeons=30):
    """
    術者別分析HTMLレポートを生成する。
    ・年度比較 / 直近365日 を HTML内で切替
    ・全身麻酔のみ / 全手術 を HTML内で切替
    ・診療科フィルター（全科 or 個別科）を HTML内で選択
    ・4パターン全データをPython側で集計してJSONとして埋め込む
    """
    import json as _json

    now_str = datetime.datetime.now().strftime('%Y年%m月%d日 %H:%M')
    latest_date_str = df_raw['手術実施日'].max().strftime('%Y年%m月%d日')

    # 全診療科リスト（ソート済み）
    all_depts = sorted(df_raw['実施診療科'].dropna().unique().tolist())
    all_depts_json = _json.dumps(all_depts, ensure_ascii=False)

    # 期間ラベル（シンプル形式）
    ann_p2_label = f"{ann_p2_start}〜{ann_p2_end}（今年度）"
    ann_p1_label = f"{ann_p1_start}〜{ann_p1_end}（昨年度）"
    rol_p2_label = f"{rol_p2_start}〜{rol_p2_end}（直近365日）"
    rol_p1_label = f"{rol_p1_start}〜{rol_p1_end}（前期365日）"

    # ── 4パターン × 2指標分のデータを集計 ──
    # 全科・科別は HTML 側の JS でフィルタするため、
    # ここでは「診療科情報付き全術者データ」を渡す
    def _build(p2s, p2e, p1s, p1e, analysis_target):
        r2 = build_surgeon_data(df_raw, p2s, p2e, analysis_target)
        r1 = build_surgeon_data(df_raw, p1s, p1e, analysis_target)
        return {'p2': r2, 'p1': r1}

    data_ann_ga  = _build(ann_p2_start, ann_p2_end, ann_p1_start, ann_p1_end, "全身麻酔のみ")
    data_ann_all = _build(ann_p2_start, ann_p2_end, ann_p1_start, ann_p1_end, "全手術")
    data_rol_ga  = _build(rol_p2_start, rol_p2_end, rol_p1_start, rol_p1_end, "全身麻酔のみ")
    data_rol_all = _build(rol_p2_start, rol_p2_end, rol_p1_start, rol_p1_end, "全手術")

    master_json = _json.dumps({
        'annual': {
            'ga':  data_ann_ga,
            'all': data_ann_all,
            'p2_label': ann_p2_label,
            'p1_label': ann_p1_label,
        },
        'rolling': {
            'ga':  data_rol_ga,
            'all': data_rol_all,
            'p2_label': rol_p2_label,
            'p1_label': rol_p1_label,
        },
    }, ensure_ascii=False)

    init_mode_js = 'rolling' if initial_mode == "直近365日" else 'annual'

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>👨‍⚕️ 術者別分析レポート</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@300;400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg-page:      #F0F2F5;
      --bg-card:      #FFFFFF;
      --bg-elevated:  #F7F9FC;
      --bg-header:    #1D2B3A;
      --border:       #DCE1E9;
      --border-mid:   #C4CCD8;
      --accent:       #3A6EA5;
      --accent-light: #EEF3FA;
      --accent-hover: #2F5A8A;
      --text-primary:   #1A2535;
      --text-secondary: #5A6A82;
      --text-muted:     #94A3B8;
      --success:      #1A9E6A;
      --warning:      #C87A00;
      --danger:       #C0293B;
      --bar2: var(--accent);
      --bar1: #94A3B8;
      --font-ui:   'Noto Sans JP', sans-serif;
      --font-mono: 'IBM Plex Mono', monospace;
      --r-sm: 6px; --r-md: 10px; --r-lg: 14px;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: var(--font-ui); background: var(--bg-page); color: var(--text-primary); padding: 0; line-height: 1.6; }}

    /* ─── ヘッダー ─── */
    .hdr {{
      background: var(--bg-header); color: #E8EEF5;
      padding: 0 40px; height: 64px;
      display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px;
      position: sticky; top: 0; z-index: 200;
      border-bottom: 2px solid var(--accent);
    }}
    .hdr h1 {{ font-size: clamp(0.95rem, 3vw, 1.15rem); font-weight: 600; color: #E8EEF5; }}
    .hdr-meta {{ font-size: 0.72rem; color: #7A90A8; font-family: var(--font-mono); margin-left: 16px; }}
    .hdr-badge {{ display: inline-block; background: var(--accent); color: #fff; font-weight: 700; font-size: 0.62rem; padding: 3px 9px; border-radius: 4px; letter-spacing: 0.12em; text-transform: uppercase; margin-right: 6px; }}
    .btn {{ display: inline-flex; align-items: center; gap: 6px; padding: 7px 14px; border-radius: var(--r-sm); font-weight: 500; font-size: 0.8rem; cursor: pointer; text-decoration: none; transition: all 0.15s; border: 1px solid #3A4E62; background: transparent; color: #AAB8C8; font-family: var(--font-ui); }}
    .btn:hover {{ background: rgba(255,255,255,0.06); color: #E8EEF5; border-color: #6A8098; }}
    .btn.print {{ border-color: #3A4E62; color: #AAB8C8; }}

    /* ─── コントロールバー ─── */
    .ctrl-bar {{ max-width: 1320px; margin: 0 auto 12px; background: var(--bg-card); border-radius: var(--r-lg); border: 1px solid var(--border); box-shadow: 0 1px 4px rgba(0,0,0,0.06); padding: 12px 18px; display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }}
    .ctrl-label {{ font-size: 0.76rem; color: var(--text-muted); font-weight: 700; white-space: nowrap; letter-spacing: 0.08em; text-transform: uppercase; }}
    .ctrl-sep {{ width: 1px; height: 24px; background: var(--border); }}
    .seg-btn {{ padding: 6px 16px; border-radius: var(--r-sm); border: 1px solid var(--border); background: var(--bg-elevated); color: var(--text-secondary); font-weight: 500; font-size: 0.8rem; cursor: pointer; transition: all 0.15s; font-family: var(--font-ui); }}
    .seg-btn.active {{ background: var(--accent); color: white; border-color: var(--accent); }}
    .seg-btn:hover:not(.active) {{ background: var(--accent-light); color: var(--accent); border-color: var(--accent); }}
    .mode-btn {{ padding: 6px 16px; border-radius: var(--r-sm); border: 1px solid var(--border); background: var(--bg-elevated); color: var(--text-secondary); font-weight: 500; font-size: 0.8rem; cursor: pointer; transition: all 0.15s; font-family: var(--font-ui); }}
    .mode-btn.active {{ background: var(--accent); color: white; border-color: var(--accent); }}
    .mode-btn:hover:not(.active) {{ background: var(--accent-light); color: var(--accent); border-color: var(--accent); }}

    /* ─── 診療科フィルター ─── */
    .dept-filter {{ max-width: 1320px; margin: 0 auto 12px; background: var(--bg-card); border-radius: var(--r-lg); border: 1px solid var(--border); box-shadow: 0 1px 4px rgba(0,0,0,0.06); padding: 12px 18px; display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }}
    .dept-btn {{ padding: 4px 12px; border-radius: 4px; border: 1px solid var(--border); background: var(--bg-elevated); color: var(--text-secondary); font-size: 0.76rem; font-weight: 500; cursor: pointer; transition: all 0.15s; font-family: var(--font-ui); }}
    .dept-btn.active {{ background: var(--accent); color: white; border-color: var(--accent); }}
    .dept-btn.all-btn {{ background: var(--bg-elevated); font-weight: 700; }}
    .dept-btn.all-btn.active {{ background: var(--text-primary); color: white; border-color: var(--text-primary); }}
    .dept-btn:hover:not(.active) {{ background: var(--accent-light); border-color: var(--accent); color: var(--accent); }}

    /* ─── 2カラム ─── */
    .grid-2 {{ max-width: 1320px; margin: 0 auto; display: grid; grid-template-columns: 1fr 1fr; gap: 16px; padding: 0 16px 24px; }}
    @media (max-width: 880px) {{ .grid-2 {{ grid-template-columns: 1fr; }} }}

    .col {{ background: var(--bg-card); border-radius: var(--r-lg); border: 1px solid var(--border); overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,0.06); }}
    .col.period-2 {{ border-color: var(--accent); border-width: 2px; box-shadow: 0 4px 16px rgba(58,110,165,0.14); }}
    .col.period-1 {{ opacity: 0.78; filter: grayscale(12%); transition: opacity 0.3s, filter 0.3s; }}
    .col.period-1:hover {{ opacity: 0.96; filter: grayscale(0%); }}

    .col-hdr {{ padding: 12px 16px 10px; border-bottom: 1px solid var(--border); background: var(--bg-elevated); }}
    .col-title {{ font-size: clamp(0.84rem, 2vw, 0.96rem); font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--text-primary); }}
    .col.period-2 .col-title {{ color: var(--accent); }}
    .col-summary {{ display: flex; gap: 18px; margin-top: 8px; flex-wrap: wrap; }}
    .sum-item {{ font-size: 0.74rem; color: var(--text-muted); }}
    .sum-item strong {{ font-size: 0.96rem; font-family: var(--font-mono); color: var(--text-primary); }}
    .col.period-2 .sum-item strong {{ color: var(--accent); }}

    /* ─── 術者リスト ─── */
    .surgeon-list {{ padding: 10px 14px 14px; }}
    .surgeon-row {{ display: grid; grid-template-columns: 26px minmax(90px,1.5fr) 1fr 64px; align-items: center; gap: 6px; padding: 5px 3px; border-bottom: 1px solid var(--border); transition: background 0.15s; }}
    .surgeon-row:hover {{ background: var(--bg-elevated); border-radius: var(--r-sm); }}
    .surgeon-rank {{ font-family: var(--font-mono); font-size: 0.74rem; color: var(--text-muted); text-align: center; font-weight: 600; }}
    .surgeon-rank.gold {{ color: #B45309; }}
    .surgeon-info {{ min-width: 0; }}
    .surgeon-name {{ font-size: 0.86rem; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--text-primary); }}
    .surgeon-dept {{ font-size: 0.68rem; color: var(--text-muted); }}
    .surgeon-bar-wrap {{ position: relative; height: 6px; background: var(--bg-elevated); border-radius: 3px; border: 1px solid var(--border); }}
    .col.period-2 .surgeon-bar-fill {{ height: 100%; border-radius: 3px; background: var(--bar2); transition: width 0.5s ease; }}
    .col.period-1 .surgeon-bar-fill {{ height: 100%; border-radius: 3px; background: var(--bar1); transition: width 0.5s ease; }}
    .surgeon-val {{ font-family: var(--font-mono); font-size: 0.82rem; font-weight: 600; text-align: right; white-space: nowrap; color: var(--text-primary); }}
    .col.period-2 .surgeon-val {{ color: var(--accent); }}
    .no-data {{ color: var(--text-muted); text-align: center; padding: 24px; font-size: 0.86rem; }}

    /* ─── 補助 ─── */
    .ctrl-bar, .dept-filter {{ margin-top: 16px; }}

    /* ─── 印刷 ─── */
    @media print {{
      body {{ background: white; }}
      .ctrl-bar, .dept-filter, .btn {{ display: none !important; }}
      .col.period-1 {{ opacity: 1 !important; filter: none !important; }}
      .grid-2 {{ grid-template-columns: 1fr 1fr !important; padding: 0; }}
      .hdr {{ position: static; }}
    }}
  </style>
</head>
<body>

<!-- ─── ヘッダー ─── -->
<div class="hdr">
  <div style="display:flex;align-items:center;gap:14px;">
    <div class="hdr-badge" style="white-space:nowrap;">術者別分析</div>
    <h1>👨‍⚕️ 術者別分析レポート</h1>
    <div class="hdr-meta">
      <span id="badge-mode" style="margin-right:8px;"></span>
      <span id="badge-target"></span>
      ｜ 最新: {latest_date_str} ｜ 出力: {now_str}
    </div>
  </div>
  <div>
    <button class="btn print" onclick="window.print()">🖨 印刷</button>
  </div>
</div>

<!-- ─── コントロールバー ─── -->
<div class="ctrl-bar">
  <span class="ctrl-label">📅 比較モード</span>
  <button class="mode-btn active" id="mode-annual" onclick="switchMode('annual')">年度比較</button>
  <button class="mode-btn"        id="mode-rolling" onclick="switchMode('rolling')">直近365日</button>
  <div class="ctrl-sep"></div>
  <span class="ctrl-label">🫀 分析対象</span>
  <button class="seg-btn active" id="tgt-ga"  onclick="switchTarget('ga')">全身麻酔のみ</button>
  <button class="seg-btn"        id="tgt-all" onclick="switchTarget('all')">全手術</button>
  <div class="ctrl-sep"></div>
  <span class="ctrl-label">📊 表示指標</span>
  <button class="seg-btn active" id="met-count" onclick="switchMetric('count')">参加件数</button>
  <button class="seg-btn"        id="met-hours" onclick="switchMetric('hours')">総手術時間 (h)</button>
</div>

<!-- ─── 診療科フィルター ─── -->
<div class="dept-filter">
  <span class="ctrl-label">🏥 診療科</span>
  <button class="dept-btn all-btn active" id="dept-all" onclick="selectDept('__all__')">全科</button>

</div>

<!-- ─── 2カラム ─── -->
<div class="grid-2">
  <div class="col period-2">
    <div class="col-hdr">
      <div class="col-title" id="p2-period"></div>
      <div class="col-summary" id="p2-summary"></div>
    </div>
    <div class="surgeon-list" id="list-p2"></div>
  </div>
  <div class="col period-1">
    <div class="col-hdr">
      <div class="col-title" id="p1-period"></div>
      <div class="col-summary" id="p1-summary"></div>
    </div>
    <div class="surgeon-list" id="list-p1"></div>
  </div>
</div>

<script>
// ─── マスターデータ ───
const MASTER = {master_json};
const ALL_DEPTS = {all_depts_json};
const MAX_N = {max_surgeons};

// ─── 診療科ボタン動的生成 ───
(function() {{
  const wrap = document.querySelector('.dept-filter');
  ALL_DEPTS.forEach(dept => {{
    const btn = document.createElement('button');
    btn.className = 'dept-btn';
    btn.textContent = dept;
    btn.dataset.dept = dept;
    btn.onclick = () => selectDept(dept);
    wrap.appendChild(btn);
  }});
}})();
// プレースホルダーのテキストを削除
document.querySelector('.dept-filter').childNodes.forEach(n => {{
  if (n.nodeType === 3 && n.textContent.includes('DEPT_BUTTONS_PLACEHOLDER')) n.remove();
}});

// ─── 状態 ───
let curMode   = '{init_mode_js}';
let curTarget = 'ga';
let curMetric = 'count';
let curDept   = '__all__';

// ─── フィルタ関数 ───
function filterRows(rows) {{
  if (curDept === '__all__') return rows;
  return rows.filter(r => r.dept === curDept);
}}

function getTopRows(rows) {{
  const key = curMetric === 'count' ? 'count' : 'hours';
  return [...filterRows(rows)].sort((a,b) => b[key]-a[key]).slice(0, MAX_N);
}}

// ─── リスト描画 ───
function buildList(containerId, rows, isPeriod2) {{
  const el = document.getElementById(containerId);
  if (!rows || rows.length === 0) {{
    el.innerHTML = '<div class="no-data">表示データなし</div>';
    return {{persons:0, total:0}};
  }}
  const key = curMetric === 'count' ? 'count' : 'hours';
  const maxVal = Math.max(...rows.map(r=>r[key]), 1);
  const medals = ['🥇','🥈','🥉'];
  let html = '';
  rows.forEach((r,i) => {{
    const rank = i+1;
    const pct  = (r[key]/maxVal*100).toFixed(1);
    const rankDisp = rank <= 3 ? medals[rank-1] : rank;
    const rankCls  = rank <= 3 ? 'surgeon-rank gold' : 'surgeon-rank';
    const valStr   = curMetric === 'count' ? r[key]+'件' : r[key].toFixed(1)+'h';
    html += `<div class="surgeon-row">
      <div class="${{rankCls}}">${{rankDisp}}</div>
      <div class="surgeon-info">
        <div class="surgeon-name" title="${{r.name}}">${{r.name}}</div>
        <div class="surgeon-dept">${{r.dept}}</div>
      </div>
      <div class="surgeon-bar-wrap"><div class="surgeon-bar-fill" style="width:${{pct}}%"></div></div>
      <div class="surgeon-val">${{valStr}}</div>
    </div>`;
  }});
  el.innerHTML = html;
  const total = rows.reduce((s,r)=>s+r[key], 0);
  return {{persons: rows.length, total}};
}}

function buildSummary(elId, s) {{
  const unit = curMetric === 'count' ? '件' : 'h';
  const totalStr = curMetric === 'count' ? s.total+unit : s.total.toFixed(1)+unit;
  document.getElementById(elId).innerHTML =
    `<div class="sum-item">対象術者 <strong>${{s.persons}}名</strong></div>
     <div class="sum-item">合計 <strong>${{totalStr}}</strong></div>`;
}}

// ─── 全体再描画 ───
function render() {{
  const md = MASTER[curMode];
  const tkey = curTarget; // 'ga' or 'all'
  const r2 = getTopRows(md[tkey].p2);
  const r1 = getTopRows(md[tkey].p1);
  const s2 = buildList('list-p2', r2, true);
  const s1 = buildList('list-p1', r1, false);
  buildSummary('p2-summary', s2);
  buildSummary('p1-summary', s1);
  document.getElementById('p2-period').textContent = md.p2_label;
  document.getElementById('p1-period').textContent = md.p1_label;
  // バッジ更新
  document.getElementById('badge-mode').textContent   = curMode==='annual' ? '年度比較' : '直近365日';
  document.getElementById('badge-target').textContent = curTarget==='ga'   ? '全身麻酔のみ' : '全手術';
}}

// ─── スイッチ関数 ───
function switchMode(m) {{
  curMode = m;
  document.getElementById('mode-annual').classList.toggle('active', m==='annual');
  document.getElementById('mode-rolling').classList.toggle('active', m==='rolling');
  render();
}}
function switchTarget(t) {{
  curTarget = t;
  document.getElementById('tgt-ga').classList.toggle('active', t==='ga');
  document.getElementById('tgt-all').classList.toggle('active', t==='all');
  render();
}}
function switchMetric(m) {{
  curMetric = m;
  document.getElementById('met-count').classList.toggle('active', m==='count');
  document.getElementById('met-hours').classList.toggle('active', m==='hours');
  render();
}}
function selectDept(dept) {{
  curDept = dept;
  document.getElementById('dept-all').classList.toggle('active', dept==='__all__');
  document.querySelectorAll('.dept-btn:not(.all-btn)').forEach(btn => {{
    btn.classList.toggle('active', btn.dataset.dept === dept);
  }});
  render();
}}

// ─── 初期描画 ───
// 初期モードを設定
switchMode('{init_mode_js}');
</script>
</body>
</html>"""
    return html


# --- 4. 集計・描画メイン関数 ---

def render_dashboard(df_all, df_source, title_label, analysis_target, ma_window, metric_type, is_prediction_mode=False, reference_date=None, rolling_mode=False):
    st.subheader(f"{title_label}")
    df_local = df_source[df_source['is_ga']].copy() if analysis_target == "全身麻酔のみ" else df_source.copy()
    df_biz = df_local[df_local['is_biz']]
    if df_biz.empty: return st.warning("データなし"), None

    daily = df_biz.groupby('手術実施日').size().reset_index(name='件数')
    daily['MA'] = daily['件数'].rolling(window=ma_window, min_periods=1).mean()
    avg_v = daily['件数'].mean()

    target_rooms = GA_ROOM_LIST if analysis_target == "全身麻酔のみ" else ALL_ROOM_LIST
    room_denom = 11 if analysis_target == "全身麻酔のみ" else 12
    df_eff = df_biz[df_biz['rooms_list'].apply(lambda rl: any(r in target_rooms for r in rl))]
    num_days = len(daily)
    util_rate = df_eff['core_m'].sum() / (room_denom * CORE_MINUTES * num_days) if num_days > 0 else 0
    over_rate = df_eff['over_m'].sum() / (df_eff['core_m'].sum() + df_eff['over_m'].sum()) if (df_eff['core_m'].sum() + df_eff['over_m'].sum()) > 0 else 0

    st.write("📊 **経営指標 ＆ 効率分析**")
    r1, r2, r3 = st.columns(2), st.columns(2), st.columns(2)
    r1[0].metric("平日平均件数", f"{avg_v:.1f} 件")
    r1[1].metric("目標達成率" if analysis_target == "全身麻酔のみ" else "稼働日数", f"{avg_v/TARGET_DAILY_GA:.1%}" if analysis_target == "全身麻酔のみ" else f"{num_days} 日")

    ref_d = reference_date if isinstance(reference_date, (datetime.date, datetime.datetime)) else daily['手術実施日'].max().date()
    fy_year = ref_d.year if ref_d.month >= 4 else ref_d.year - 1
    df_base = df_all[df_all['is_ga']] if analysis_target == "全身麻酔のみ" else df_all
    last_d = daily['手術実施日'].max().date()

    try: rem_m = count_remaining_biz_days(last_d, (ref_d + relativedelta(day=31))) if is_prediction_mode else 0
    except: rem_m = 0

    # 月実績は df_base（全期間）から取得 → ローリング期間で月が途中切れでも正確に取得できる
    m_curr = len(df_base[
        (df_base['手術実施日'].dt.year  == ref_d.year) &
        (df_base['手術実施日'].dt.month == ref_d.month)
    ])
    m_val = m_curr + (daily.tail(20)['件数'].mean() * rem_m) if is_prediction_mode else m_curr
    
    fy_now = len(df_base[(df_base['手術実施日'] >= pd.Timestamp(fy_year, 4, 1)) & (df_base['手術実施日'] <= pd.Timestamp(last_d))])
    fy_val = fy_now + (daily.tail(60)['件数'].mean() if len(daily)>=60 else avg_v) * count_remaining_biz_days(last_d, datetime.date(fy_year + 1, 3, 31)) if is_prediction_mode else \
             len(df_base[(df_base['手術実施日'] >= pd.Timestamp(fy_year, 4, 1)) & (df_base['手術実施日'] <= pd.Timestamp(fy_year + 1, 3, 31))])

    def _rt():
        if analysis_target == "全身麻酔のみ":
            return len(df_source[df_source['is_biz'] & df_source['is_ga']])
        return len(df_source[df_source['is_biz']])

    if is_prediction_mode and not rolling_mode:   # 年度比較 期間2
        r2[0].metric(f"{ref_d.month}月 予測着地", f"{int(m_val)} 件", f"残平日:{rem_m}日")
        r2[1].metric("今年度 累計予測", f"{int(fy_val)} 件")
    elif is_prediction_mode and rolling_mode:     # ローリング 期間2
        r2[0].metric(f"{ref_d.month}月 予測着地", f"{int(m_val)} 件", f"残平日:{rem_m}日")
        r2[1].metric("12ヶ月累計（実績）", f"{int(_rt())} 件")
    elif rolling_mode:                            # ローリング 期間1
        r2[0].metric(f"{ref_d.month}月 確定実績", f"{int(m_val)} 件")
        r2[1].metric("12ヶ月累計（実績）", f"{int(_rt())} 件")
    else:                                         # 年度比較 期間1
        r2[0].metric(f"{ref_d.month}月 確定実績", f"{int(m_val)} 件")
        r2[1].metric(f"{fy_year}年度 通期実績", f"{int(fy_val)} 件")

    r3[0].metric("平日時間内 稼動率", f"{util_rate:.1%}"); r3[1].metric("時間外比率", f"{over_rate:.1%}")

    # 推移グラフ
    st.write("---")
    y_limit = alt.Scale(domain=[float(daily['件数'].min()*0.8), float(daily['件数'].max()*1.2)] if not daily.empty else [0,30])
    base = alt.Chart(daily).encode(x=alt.X('手術実施日:T', title=None, axis=alt.Axis(format='%m/%d')))
    lines = base.mark_line(opacity=0.3, color='gray').encode(y=alt.Y('件数', scale=y_limit)) + \
            base.mark_line(strokeWidth=3, color='#1f77b4').encode(y=alt.Y('MA:Q'))
    if analysis_target == "全身麻酔のみ":
        lines = alt.layer(lines, alt.Chart(pd.DataFrame({'y': [TARGET_DAILY_GA]})).mark_rule(color='red', strokeDash=[5,5]).encode(y='y:Q'))
    st.altair_chart(lines.interactive(), use_container_width=True)

    # 【復活】診療科名付き術者分析
    if '実施術者' in df_local.columns:
        st.write(f"👨‍⚕️ 術者別分析 ({metric_type})")
        df_ex = df_local.dropna(subset=['実施術者']).copy()
        df_ex['names'] = df_ex['実施術者'].str.split(r'\r\n|\n')
        df_ex = df_ex.explode('names')
        df_ex['術者名'] = df_ex['names'].apply(normalize_str)
        df_ex = df_ex[df_ex['術者名'] != ""].drop_duplicates(['手術実施日','術者名','入室時刻'])
        
        # 術者ごとの最頻診療科を特定
        d_count = df_ex.groupby(['術者名', '実施診療科']).size().reset_index(name='c')
        d_map = dict(d_count.sort_values(['術者名','c'], ascending=[True,False]).drop_duplicates('術者名')[['術者名','実施診療科']].values)
        df_ex['表示名'] = df_ex['術者名'].apply(lambda x: f"[{d_map.get(x, '不明')[:3]}] {x}")
        
        op_sum = df_ex.groupby('表示名').agg({'手術実施日': 'count', 'total_m': 'sum'}).reset_index()
        op_sum['総手術時間 (時間)'] = (op_sum['total_m'] / 60).round(1)
        
        sort_col = '手術実施日' if metric_type == "参加件数" else "総手術時間 (時間)"
        st.altair_chart(alt.Chart(op_sum.sort_values(sort_col, ascending=False).head(10)).mark_bar(color='#1f77b4').encode(
            x=alt.X(f"{sort_col}:Q", title=sort_col),
            y=alt.Y('表示名:N', sort='-x')
        ).properties(height=350), use_container_width=True)

    # ローリング用集計（rolling_mode時のmy_val上書き）
    if rolling_mode:
        if analysis_target == "全身麻酔のみ":
            rolling_total = len(df_source[df_source['is_biz'] & df_source['is_ga']])
        else:
            rolling_total = len(df_source[df_source['is_biz']])
        fy_val = rolling_total  # HTML出力用に12ヶ月累計をfy_valとして渡す

    metrics = {'title': title_label, 'avg_v': avg_v, 'achieve': avg_v/TARGET_DAILY_GA if analysis_target=="全身麻酔のみ" else 1.0, 'month': ref_d.month, 'm_val': m_val, 'fy_val': fy_val, 'util': util_rate, 'over': over_rate, 'daily': daily, 'rolling_mode': rolling_mode}
    return None, metrics


# --- 6. メイン (Streamlit UI) ---
if _IS_MAIN:

    st.title("🏥 手術経営管理ダッシュボード v7.4")

    # ── ファイル読み込み: 自動(data/フォルダ) → フォールバック(手動アップロード) ──
    auto_file_list = _auto_load_data_files()
    _using_auto = bool(auto_file_list)

    if _using_auto:
        st.sidebar.success(f"📂 data/ から {len(auto_file_list)} ファイルを自動読み込み中")
        with st.sidebar.expander("自動読み込みファイル一覧"):
            for name, _, _ in auto_file_list:
                st.write(f"• {name}")
        st.sidebar.caption(f"📁 フォルダ: `{DATA_DIR}`")
        uploaded_files = []  # 手動アップロードは非表示
    else:
        st.sidebar.info(f"💡 `data/` フォルダにCSVを置くと自動読み込みされます")
        uploaded_files = st.sidebar.file_uploader(
            "CSVアップロード（複数可）", type="csv", accept_multiple_files=True
        )

    # 診療科目標CSV: 自動(target/フォルダ) → フォールバック(手動アップロード)
    _auto_target = _auto_load_target_file()
    if _auto_target:
        st.sidebar.success(f"🎯 target/ から目標CSVを自動読み込み")
        st.sidebar.caption(f"📁 フォルダ: `{TARGET_DIR}`")
        target_file = None
        target_dict = load_target_csv(_auto_target)
    else:
        target_file = st.sidebar.file_uploader("診療科目標CSV（任意）", type="csv", key="target_csv")
        target_dict = load_target_csv(target_file) if target_file else {}

    # ── ファイルデータリストを確定 ──
    if _using_auto:
        file_data_list = auto_file_list
    elif uploaded_files:
        file_data_list = []
        for f in uploaded_files:
            file_content = f.read()
            f.seek(0)
            file_data_list.append((f.name, f.file_id, file_content))
        if len(uploaded_files) > 1:
            st.sidebar.success(f"✅ {len(uploaded_files)}個のファイルを読み込みました")
            with st.sidebar.expander("読み込みファイル一覧"):
                for i, f in enumerate(uploaded_files, 1):
                    st.write(f"{i}. {f.name}")
    else:
        file_data_list = []

    if file_data_list:
        df_raw = load_and_preprocess(file_data_list)

        # CSVの最新日付を取得
        csv_max_date = df_raw['手術実施日'].max().date()

        # ── 比較モード選択 ──
        compare_mode = st.sidebar.radio(
            "📅 比較モード",
            ["年度比較", "直近365日"],
            key="compare_mode",
            help="年度比較: 4/1〜3/31の年度区切り\nローリング: 最新日から遡る365日比較"
        )

        # ── デフォルト期間の設定 ──
        if compare_mode == "直近365日":
            # ローリングモード: 基準日 = CSVの最大日付
            # 期間2（直近365日）: 基準日-364日 〜 基準日
            # 期間1（その前365日）: 基準日-729日 〜 基準日-365日
            rolling_base = csv_max_date
            default_p2_end   = rolling_base
            default_p2_start = rolling_base - datetime.timedelta(days=364)
            default_p1_end   = rolling_base - datetime.timedelta(days=365)
            default_p1_start = rolling_base - datetime.timedelta(days=729)
            period2_label = "期間2 直近365日 (左)"
            period1_label = "期間1 その前365日 (右)"
        else:
            # 年度比較モード（従来通り）
            fy_now  = csv_max_date.year if csv_max_date.month >= 4 else csv_max_date.year - 1
            default_p2_start = datetime.date(fy_now, 4, 1)
            default_p2_end   = csv_max_date
            default_p1_start = datetime.date(fy_now - 1, 4, 1)
            default_p1_end   = datetime.date(fy_now, 3, 31)
            period2_label = "期間2 今年度直近 (左)"
            period1_label = "期間1 昨年度 (右)"

        # モード切替を検知して session_state を強制上書き
        if st.session_state.get('prev_compare_mode') != compare_mode:
            st.session_state['d2'] = (default_p2_start, default_p2_end)
            st.session_state['d1'] = (default_p1_start, default_p1_end)
            st.session_state['prev_compare_mode'] = compare_mode

        # ── サイドバー入力 ──
        d2 = st.sidebar.date_input(period2_label, key='d2')
        d1 = st.sidebar.date_input(period1_label, key='d1')
        depts = st.sidebar.multiselect("診療科", sorted(df_raw['実施診療科'].dropna().unique()))
        mode = st.sidebar.radio("分析対象", ["全身麻酔のみ", "全手術"])
        met = st.sidebar.radio("術者指標", ["参加件数", "総手術時間 (時間)"])
        portal_url = st.sidebar.text_input("ポータルトップURL", "../index.html")

        df_f = df_raw.copy()
        if depts: df_f = df_f[df_f['実施診療科'].isin(depts)]

        # ── 診療科別パフォーマンスパネル（全体上部・横幅フル） ──
        st.write("---")
        _dw_opts = ["直近7日", "直近28日", "直近365日（期間2）"] if compare_mode == "直近365日" else ["直近7日", "直近28日", "今年度"]
        dept_window = st.radio("診療科パフォーマンス 集計期間", _dw_opts, horizontal=True, key="dept_window")

        _csv_max_d = df_raw['手術実施日'].max().date()
        _fy_yr     = _csv_max_d.year if _csv_max_d.month >= 4 else _csv_max_d.year - 1
        fy_start_d = datetime.date(_fy_yr, 4, 1)

        if dept_window == "直近7日":
            w_days, fy_s = 7, None
        elif dept_window == "直近28日":
            w_days, fy_s = 28, None
        elif dept_window == "直近365日（期間2）":
            d2_range = safe_date_range(d2)
            _rs = d2_range[0] if d2_range else (_csv_max_d - datetime.timedelta(days=364))
            w_days, fy_s = None, _rs
        else:  # 今年度
            w_days, fy_s = None, fy_start_d

        dept_rows_disp = render_dept_performance(
            df_raw, target_dict, w_days,
            f"診療科別 全身麻酔パフォーマンス（{dept_window}）",
            fy_start=fy_s
        )
        st.write("---")

        # ── 比較モードのモード情報をページ上部に表示 ──
        d2_range = safe_date_range(d2)
        d1_range = safe_date_range(d1)

        if compare_mode == "直近365日" and d2_range and d1_range:
            st.info(
                f"📅 **ローリングモード**: 基準日 {csv_max_date}  "
                f"｜ 期間2: {d2_range[0]} 〜 {d2_range[1]}（{(d2_range[1]-d2_range[0]).days+1}日）  "
                f"｜ 期間1: {d1_range[0]} 〜 {d1_range[1]}（{(d1_range[1]-d1_range[0]).days+1}日）"
            )

        # ── 期間比較ダッシュボード（左右2列） ──
        cl, cr = st.columns(2)
        p1_rep, p2_rep = None, None

        # 左列: 期間2（常に予測モード: 月末着地を推計）
        with cl:
            if d2_range:
                df_p2 = df_f[(df_f['手術実施日'].dt.date >= d2_range[0]) & (df_f['手術実施日'].dt.date <= d2_range[1])]
                _, p2_rep = render_dashboard(df_raw, df_p2, f"{d2_range[0]}〜{d2_range[1]}", mode, 30, met, True, d2_range[1], rolling_mode=(compare_mode=="直近365日"))

        # 右列: 期間1（常に確定実績）
        with cr:
            if d1_range:
                df_p1 = df_f[(df_f['手術実施日'].dt.date >= d1_range[0]) & (df_f['手術実施日'].dt.date <= d1_range[1])]
                if compare_mode == "直近365日":
                    ref_d1 = d1_range[1]  # 期間1末尾をそのまま使う（年またぎ誤作動を防止）
                else:
                    # 昨年度の「同月（今年度最新データの月）」末尾日を reference_date として渡す
                    _target_month = csv_max_date.month
                    _same_month = df_p1[df_p1['手術実施日'].dt.month == _target_month]
                    if not _same_month.empty:
                        ref_d1 = _same_month['手術実施日'].max().date()
                    else:
                        # 同月データがない場合は最新日を使用
                        ref_d1 = df_p1['手術実施日'].max().date()
                _, p1_rep = render_dashboard(df_raw, df_p1, f"{d1_range[0]}〜{d1_range[1]}", mode, 30, met, False, ref_d1, rolling_mode=(compare_mode=="直近365日"))

        if p1_rep and p2_rep:
            st.sidebar.write("---")
            _cmax  = df_raw['手術実施日'].max().date()
            _fy_y  = _cmax.year if _cmax.month >= 4 else _cmax.year - 1
            _fy_st = datetime.date(_fy_y, 4, 1)

            # ── 共通: 直近7日・28日 ──
            dept_rows_7  = calc_dept_performance(df_raw, target_dict, 7)  if target_dict else []
            dept_rows_28 = calc_dept_performance(df_raw, target_dict, 28) if target_dict else []

            # ── 年度比較モード用 fy データ ──
            ann_dept_fy  = calc_dept_performance(df_raw, target_dict, None, fy_start=_fy_st) if target_dict else []
            ann_fy_lbl   = f"{_fy_y}年度（{_fy_st}〜{_cmax}・{(_cmax-_fy_st).days+1}日間）"

            # ── ローリングモード用 fy データ（常にCSV最新日ベースで計算）──
            _rol_base    = _cmax
            _rol_p2_start = _rol_base - datetime.timedelta(days=364)
            _rol_p1_end   = _rol_base - datetime.timedelta(days=365)
            _rol_p1_start = _rol_base - datetime.timedelta(days=729)
            rol_dept_fy  = calc_dept_performance(df_raw, target_dict, None, fy_start=_rol_p2_start) if target_dict else []
            rol_fy_lbl   = f"12ヶ月累計（{_rol_p2_start}〜{_rol_base}・365日間）"

            # ── 現在のモードに応じた表示用 fy ──
            if compare_mode == "直近365日":
                dept_rows_fy = rol_dept_fy
                fy_lbl = rol_fy_lbl
            else:
                dept_rows_fy = ann_dept_fy
                fy_lbl = ann_fy_lbl

            # ── 両モード分のデータを計算してHTMLに埋め込む ──
            _ann_p2_df = df_f[(df_f['手術実施日'].dt.date >= _fy_st) & (df_f['手術実施日'].dt.date <= _cmax)]
            _ann_p1_df = df_f[(df_f['手術実施日'].dt.date >= datetime.date(_fy_y-1,4,1)) & (df_f['手術実施日'].dt.date <= datetime.date(_fy_y,3,31))]
            _, _ann_p2 = render_dashboard(df_raw, _ann_p2_df, f'{_fy_st}〜{_cmax}', mode, 30, met, True, _cmax, rolling_mode=False)
            # 年度比較の昨年度 reference_date: csv最新日と同じ月の昨年度末尾日
            _ann_p1_same_month = _ann_p1_df[pd.to_datetime(_ann_p1_df['手術実施日']).dt.month == _cmax.month]
            _ann_p1_ref = _ann_p1_same_month['手術実施日'].max().date() if not _ann_p1_same_month.empty else _ann_p1_df['手術実施日'].max().date()
            _, _ann_p1 = render_dashboard(df_raw, _ann_p1_df, f'{datetime.date(_fy_y-1,4,1)}〜{datetime.date(_fy_y,3,31)}', mode, 30, met, False, _ann_p1_ref, rolling_mode=False)

            _rol_p2_df = df_f[(df_f['手術実施日'].dt.date >= _rol_p2_start) & (df_f['手術実施日'].dt.date <= _rol_base)]
            _rol_p1_df = df_f[(df_f['手術実施日'].dt.date >= _rol_p1_start) & (df_f['手術実施日'].dt.date <= _rol_p1_end)]
            _, _rol_p2 = render_dashboard(df_raw, _rol_p2_df, f'{_rol_p2_start}〜{_rol_base}', mode, 30, met, True, _rol_base, rolling_mode=True)
            _, _rol_p1 = render_dashboard(df_raw, _rol_p1_df, f'{_rol_p1_start}〜{_rol_p1_end}', mode, 30, met, False, _rol_p1_end, rolling_mode=True)

            import os
            _docs_op = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs_operation")

            # ── フルレポート生成ボタン ──
            if st.sidebar.button("📄 フルレポートを生成", type="primary"):
                # 昨年度診療科データ
                _py_st  = datetime.date(_fy_y - 1, 4, 1)
                _py_end = datetime.date(_fy_y,     3, 31)
                _py_dept_fy = calc_dept_performance(df_raw, target_dict, None, fy_start=_py_st, fy_end=_py_end) if target_dict else []
                _py_fy_lbl  = f"{_fy_y - 1}年度（{_py_st}〜{_py_end}）"

                # 診療科別推移グラフ用データ（直近365日 vs 前年同時期、週累計、全日）
                import json as _json_st
                _st_dept_chart = {}
                if target_dict:
                    _df_ga = df_raw[df_raw['is_ga']].copy()
                    _rol_cy_st  = _cmax - datetime.timedelta(days=364)
                    _rol_py_end = _cmax - datetime.timedelta(days=365)
                    _rol_py_st  = _cmax - datetime.timedelta(days=729)
                    for _dept, _weekly_tgt in target_dict.items():
                        _df_d = _df_ga[_df_ga['実施診療科'] == _dept]
                        def _ws(df_p, s, e):
                            import pandas as _pd2
                            all_d = _pd2.date_range(start=s, end=e, freq='D')
                            dly = df_p.groupby('手術実施日').size().reset_index(name='件数')
                            dly = dly.set_index('手術実施日').reindex(all_d, fill_value=0).reset_index()
                            dly.columns = ['手術実施日', '件数']
                            dly['weekly_avg'] = dly['件数'].rolling(window=28, min_periods=14).sum() / 4
                            dly['date_label'] = dly['手術実施日'].dt.strftime('%m/%d')
                            return dly
                        _cy_d = _ws(_df_d, _rol_cy_st, _cmax)
                        _py_d = _ws(_df_d, _rol_py_st, _rol_py_end)
                        _st_dept_chart[_dept] = {
                            'cy': _cy_d[['date_label','weekly_avg']].round({'weekly_avg':1}).to_dict(orient='records'),
                            'py': _py_d[['date_label','weekly_avg']].round({'weekly_avg':1}).to_dict(orient='records'),
                            'cy_label': f"直近365日（{_rol_cy_st}〜{_cmax}）",
                            'py_label': f"前年同時期（{_rol_py_st}〜{_rol_py_end}）",
                            'weekly_target': _weekly_tgt,
                            'win_label': '28日移動平均（週換算）',
                        }

                report_html = create_full_html_report(
                    p1_rep, p2_rep, mode, portal_url,
                    dept_rows_7=dept_rows_7,
                    dept_rows_28=dept_rows_28,
                    dept_rows_fy=dept_rows_fy,
                    fy_label=fy_lbl,
                    annual_p1=_ann_p1,  annual_p2=_ann_p2,
                    rolling_p1=_rol_p1, rolling_p2=_rol_p2,
                    annual_dept_fy=ann_dept_fy,  rolling_dept_fy=rol_dept_fy,
                    annual_fy_label=ann_fy_lbl,  rolling_fy_label=rol_fy_lbl,
                    current_mode=compare_mode,
                    prev_year_dept_fy=_py_dept_fy,
                    prev_year_fy_label=_py_fy_lbl,
                    dept_chart_json=_json_st.dumps(_st_dept_chart, ensure_ascii=False),
                )
                os.makedirs(_docs_op, exist_ok=True)
                with open(os.path.join(_docs_op, "index.html"), "w", encoding="utf-8") as _f:
                    _f.write(report_html)
                st.session_state['full_report_html'] = report_html
                st.sidebar.success("📁 docs_operation/index.html に保存しました")

            if 'full_report_html' in st.session_state:
                st.sidebar.download_button(
                    label="⬇️ フルレポート(HTML)をダウンロード",
                    data=st.session_state['full_report_html'],
                    file_name="index.html",
                    mime="text/html"
                )

            # ── 術者別分析HTML出力ボタン ──
            st.sidebar.write("---")

            # 年度比較用の期間（4パターンすべてを渡す）
            _ann_p1_start = datetime.date(_fy_y - 1, 4, 1)
            _ann_p1_end   = datetime.date(_fy_y, 3, 31)

            if st.sidebar.button("👨‍⚕️ 術者別分析を生成", type="primary"):
                surgeon_html = create_surgeon_html_report(
                    df_raw=df_raw,
                    ann_p2_start=_fy_st,           ann_p2_end=_cmax,
                    ann_p1_start=_ann_p1_start,    ann_p1_end=_ann_p1_end,
                    rol_p2_start=_rol_p2_start,    rol_p2_end=_rol_base,
                    rol_p1_start=_rol_p1_start,    rol_p1_end=_rol_p1_end,
                    portal_url=portal_url,
                    initial_mode=compare_mode,
                    max_surgeons=30,
                )
                os.makedirs(_docs_op, exist_ok=True)
                with open(os.path.join(_docs_op, "surgeon_analysis.html"), "w", encoding="utf-8") as _f:
                    _f.write(surgeon_html)
                st.session_state['surgeon_report_html'] = surgeon_html
                st.sidebar.success("📁 docs_operation/surgeon_analysis.html に保存しました")

            if 'surgeon_report_html' in st.session_state:
                st.sidebar.download_button(
                    label="⬇️ 術者別分析(HTML)をダウンロード",
                    data=st.session_state['surgeon_report_html'],
                    file_name="surgeon_analysis.html",
                    mime="text/html"
                )
    else:
        st.info("CSVファイルをアップロードするか、`data/` フォルダにCSVを配置してください。")
        st.markdown(
            f"""
            **自動読み込みの使い方:**
            1. `{DATA_DIR}` フォルダを作成する
            2. 手術データCSV（複数可）を置く
            3. `{TARGET_DIR}` フォルダを作成し、目標CSVを1ファイル置く（任意）
            4. ページをリロードすると自動で読み込まれます
            """
        )




# ============================================================
# generate_html.py 統合用インターフェース
# ============================================================

def _load_op_csv_from_dir(data_dir: str):
    """
    data_dir 配下の op_data/ (CSVファイル群) と target/ (目標CSV) を読み込んで
    前処理済み DataFrame を返す。
    """
    import io, pathlib, unicodedata, datetime
    import pandas as pd
    import jpholiday

    _root    = pathlib.Path(data_dir)
    data_sub = _root / "op_data"      # 手術データCSV置き場
    tgt_sub  = _root / "op_target"    # 目標CSV置き場

    # ── 手術データ収集 ──
    csv_paths = sorted((data_sub if data_sub.exists() else _root).glob("*.csv"))
    if not csv_paths:
        raise FileNotFoundError(
            f"手術データCSVが見つかりません: {data_sub} または {_root}"
        )

    raw_dfs = []
    for p in csv_paths:
        content = p.read_bytes()
        mtime   = p.stat().st_mtime
        raw_dfs.append((p.name, f"auto:{p}:{mtime}", content))

    # ── 前処理（load_and_preprocess と同じロジック） ──
    # load_and_preprocess は Streamlit キャッシュデコレータ付きなので
    # 内部実装を直接呼び出す（デコレータなし版）
    df = _load_and_preprocess_nodeco(raw_dfs)
    if df is None or df.empty:
        raise ValueError("手術データの読み込みに失敗しました")

    # ── 目標CSV ──
    tgt_dict = {}
    tgt_paths = sorted((tgt_sub if tgt_sub.exists() else _root / "target").glob("*.csv"))
    if not tgt_paths:
        # data_dir 直下の 目標.csv も探す
        fallback = _root / "目標.csv"
        if fallback.exists():
            tgt_paths = [fallback]
    if tgt_paths:
        with open(tgt_paths[0], "rb") as f:
            tgt_dict = load_target_csv(f)

    return df, tgt_dict


def _load_and_preprocess_nodeco(file_data_list):
    """load_and_preprocess の Streamlit デコレータなし版（generate_html.py 統合用）"""
    import io, unicodedata, datetime
    import pandas as pd
    import numpy as np

    raw_dfs = []
    for file_name, file_id, file_content in file_data_list:
        try:
            df = pd.read_csv(io.BytesIO(file_content), encoding='cp932', dtype=str)
        except Exception:
            df = pd.read_csv(io.BytesIO(file_content), encoding='utf-8', dtype=str)
        df.columns = [c.strip() for c in df.columns]
        raw_dfs.append(df)

    combined_raw = pd.concat(raw_dfs, ignore_index=True)

    dedup_cols = [
        '手術実施日', '実施診療科', '実施手術室', '麻酔科関与',
        '入外区分', '申込区分', '実施術者', '麻酔種別',
        '入室時刻', '退室時刻', '予定手術時間', '予定手術時間(OR)'
    ]
    target_cols = [c for c in dedup_cols if c in combined_raw.columns]

    if '手術実施日' in combined_raw.columns:
        combined_raw['手術実施日'] = pd.to_datetime(combined_raw['手術実施日'], errors='coerce')

    for c in ['予定手術時間', '予定手術時間(OR)']:
        if c in combined_raw.columns:
            combined_raw[c] = pd.to_numeric(combined_raw[c], errors='coerce')

    def normalize_and_sort_lines(s):
        if pd.isna(s): return None
        s = unicodedata.normalize('NFKC', str(s))
        for sep in ['\r\n', '\n', '\r', ',']:
            s = s.replace(sep, '\n')
        parts = [p.strip() for p in s.split('\n') if p.strip()]
        parts.sort()
        return ' '.join(parts).upper()

    def normalize_time_strict(t):
        if pd.isna(t): return None
        t = str(t).strip().replace(':', '')
        if not t.isdigit(): return None
        if len(t) == 3: t = '0' + t
        return t

    for c in ['実施手術室', '実施術者', '麻酔種別']:
        if c in combined_raw.columns:
            combined_raw[c] = combined_raw[c].apply(normalize_and_sort_lines)

    for c in ['実施診療科', '麻酔科関与', '入外区分', '申込区分']:
        if c in combined_raw.columns:
            combined_raw[c] = combined_raw[c].apply(
                lambda x: unicodedata.normalize('NFKC', str(x)).strip().upper() if pd.notna(x) else None
            )

    for c in ['入室時刻', '退室時刻']:
        if c in combined_raw.columns:
            combined_raw[c] = combined_raw[c].apply(normalize_time_strict)

    combined_raw = combined_raw.drop_duplicates(subset=target_cols, keep='first')

    df = combined_raw.copy()

    import jpholiday as _jph
    def _is_biz(date):
        if not isinstance(date, (pd.Timestamp, datetime.date, datetime.datetime)):
            return False
        if date.weekday() >= 5: return False
        if _jph.is_holiday(date): return False
        if (date.month == 12 and date.day >= 28) or (date.month == 1 and date.day <= 3):
            return False
        return True

    df['is_biz'] = df['手術実施日'].apply(_is_biz)

    if '麻酔種別' in df.columns:
        df['麻酔種別_norm'] = df['麻酔種別'].fillna("")
        df['is_ga'] = (
            df['麻酔種別_norm'].str.contains("全身麻酔", na=False) &
            df['麻酔種別_norm'].str.contains("20分以上", na=False)
        )
    else:
        df['is_ga'] = False

    if '実施手術室' in df.columns:
        df['rooms_list'] = df['実施手術室'].apply(
            lambda x: str(x).split(' ') if pd.notna(x) else []
        )
    else:
        df['rooms_list'] = [[] for _ in range(len(df))]

    def parse_time_from_normalized(s):
        if not s or not isinstance(s, str): return np.nan
        try:
            if len(s) == 4: return int(s[:2]) * 60 + int(s[2:4])
        except Exception:
            pass
        return np.nan

    if '入室時刻' in df.columns and '退室時刻' in df.columns:
        in_m  = df['入室時刻'].apply(parse_time_from_normalized)
        out_m = df['退室時刻'].apply(parse_time_from_normalized)
        out_m = np.where(out_m < in_m, out_m + 1440, out_m)
        overlap_s = np.maximum(in_m, 510)
        overlap_e = np.minimum(out_m, 1035)
        df['core_m'] = np.maximum(0, overlap_e - overlap_s)
        df['core_m'] = df['core_m'].fillna(0)
        df['over_m'] = np.maximum(0, out_m - np.maximum(in_m, 1035))
        df['over_m'] = df['over_m'].fillna(0)
        df['total_m'] = df['core_m'] + df['over_m']
    else:
        df['core_m'] = 0; df['over_m'] = 0; df['total_m'] = 0

    return df


def _build_report_data(df_raw, target_dict, mode="全身麻酔のみ"):
    """
    create_full_html_report に渡す p1_rep / p2_rep 相当のデータを
    Streamlit UIなしで組み立てる。
    期間2: 今年度 (4/1 〜 最新日)
    期間1: 昨年度 (前年4/1 〜 前年3/31)
    """
    import datetime, pandas as pd
    from dateutil.relativedelta import relativedelta

    max_d  = df_raw['手術実施日'].max().date()
    fy_yr  = max_d.year if max_d.month >= 4 else max_d.year - 1
    fy_st  = datetime.date(fy_yr, 4, 1)
    fy_end = datetime.date(fy_yr + 1, 3, 31)
    py_st  = datetime.date(fy_yr - 1, 4, 1)
    py_end = datetime.date(fy_yr, 3, 31)

    def _metrics(df_all, df_src, is_pred, ref_d, rolling=False):
        df_f = df_src[df_src['is_ga']].copy() if mode == "全身麻酔のみ" else df_src.copy()
        df_biz = df_f[df_f['is_biz']]
        if df_biz.empty:
            return None
        daily = df_biz.groupby('手術実施日').size().reset_index(name='件数')
        daily['MA'] = daily['件数'].rolling(window=30, min_periods=1).mean()
        avg_v = daily['件数'].mean()

        df_base = df_all[df_all['is_ga']] if mode == "全身麻酔のみ" else df_all
        last_d  = daily['手術実施日'].max().date()
        fy_year = ref_d.year if ref_d.month >= 4 else ref_d.year - 1

        try:
            rem_m = count_remaining_biz_days(
                last_d, ref_d + relativedelta(day=31)
            ) if is_pred else 0
        except Exception:
            rem_m = 0

        m_curr = len(df_base[
            (df_base['手術実施日'].dt.year  == ref_d.year) &
            (df_base['手術実施日'].dt.month == ref_d.month)
        ])
        m_val = m_curr + (daily.tail(20)['件数'].mean() * rem_m) if is_pred else m_curr

        if rolling:
            fy_val = len(df_src[df_src['is_biz'] & (df_src['is_ga'] if mode=="全身麻酔のみ" else pd.Series(True, index=df_src.index))])
        elif is_pred:
            fy_now = len(df_base[
                (df_base['手術実施日'] >= pd.Timestamp(fy_year, 4, 1)) &
                (df_base['手術実施日'] <= pd.Timestamp(last_d))
            ])
            fy_val = fy_now + (
                daily.tail(60)['件数'].mean() if len(daily) >= 60 else avg_v
            ) * count_remaining_biz_days(last_d, datetime.date(fy_year + 1, 3, 31))
        else:
            fy_val = len(df_base[
                (df_base['手術実施日'] >= pd.Timestamp(fy_year, 4, 1)) &
                (df_base['手術実施日'] <= pd.Timestamp(fy_year + 1, 3, 31))
            ])

        target_rooms = GA_ROOM_LIST if mode == "全身麻酔のみ" else ALL_ROOM_LIST
        room_denom   = 11 if mode == "全身麻酔のみ" else 12
        df_eff = df_biz[df_biz['rooms_list'].apply(
            lambda rl: any(r in target_rooms for r in rl)
        )]
        nd = len(daily)
        util = df_eff['core_m'].sum() / (room_denom * CORE_MINUTES * nd) if nd > 0 else 0
        denom_over = df_eff['core_m'].sum() + df_eff['over_m'].sum()
        over = df_eff['over_m'].sum() / denom_over if denom_over > 0 else 0

        return {
            'title': f"{fy_st}〜{max_d}",
            'avg_v': avg_v,
            'achieve': avg_v / TARGET_DAILY_GA if mode == "全身麻酔のみ" else 1.0,
            'month': ref_d.month,
            'm_val': m_val,
            'fy_val': fy_val,
            'util': util,
            'over': over,
            'daily': daily,
            'rolling_mode': rolling,
        }

    # ── 年度比較 ──
    df_p2 = df_raw[(df_raw['手術実施日'].dt.date >= fy_st) &
                   (df_raw['手術実施日'].dt.date <= max_d)]
    df_p1 = df_raw[(df_raw['手術実施日'].dt.date >= py_st) &
                   (df_raw['手術実施日'].dt.date <= py_end)]

    ann_p2 = _metrics(df_raw, df_p2, True,  max_d, rolling=False)
    _p1_same = df_p1[df_p1['手術実施日'].dt.month == max_d.month]
    ann_ref1 = _p1_same['手術実施日'].max().date() if not _p1_same.empty else \
               df_p1['手術実施日'].max().date() if not df_p1.empty else max_d
    ann_p1 = _metrics(df_raw, df_p1, False, ann_ref1, rolling=False)

    # ── ローリング12ヶ月 ──
    rol_p2_start = max_d - datetime.timedelta(days=364)
    rol_p1_end   = max_d - datetime.timedelta(days=365)
    rol_p1_start = max_d - datetime.timedelta(days=729)

    df_rp2 = df_raw[(df_raw['手術実施日'].dt.date >= rol_p2_start) &
                    (df_raw['手術実施日'].dt.date <= max_d)]
    df_rp1 = df_raw[(df_raw['手術実施日'].dt.date >= rol_p1_start) &
                    (df_raw['手術実施日'].dt.date <= rol_p1_end)]

    rol_p2 = _metrics(df_raw, df_rp2, True,  max_d,      rolling=True)
    rol_p1 = _metrics(df_raw, df_rp1, False, rol_p1_end, rolling=True)

    # ── 診療科別パフォーマンス ──
    dept7   = calc_dept_performance(df_raw, target_dict, 7)   if target_dict else []
    dept28  = calc_dept_performance(df_raw, target_dict, 28)  if target_dict else []
    ann_fy  = calc_dept_performance(df_raw, target_dict, None, fy_start=fy_st) if target_dict else []
    rol_fy  = calc_dept_performance(df_raw, target_dict, None, fy_start=rol_p2_start) if target_dict else []

    # 昨年度診療科別パフォーマンス（4/1〜3/31 の丸1年）
    py_fy   = calc_dept_performance(df_raw, target_dict, None, fy_start=py_st, fy_end=py_end) if target_dict else []

    # ── 診療科別推移グラフ用JSON（直近365日 vs 前年同時期365日、全日対象） ──
    import json as _json
    dept_chart_data = {}
    if target_dict:
        # 全身麻酔のみ（is_biz フィルターなし＝休日含む）
        df_ga = df_raw[df_raw['is_ga']].copy()
        # 直近365日
        rol_cy_st = max_d - datetime.timedelta(days=364)
        # 前年同時期365日
        rol_py_end = max_d - datetime.timedelta(days=365)
        rol_py_st  = max_d - datetime.timedelta(days=729)

        for dept, weekly_tgt in target_dict.items():
            df_d = df_ga[df_ga['実施診療科'] == dept]

            def _build_weekly_series(df_period, start_d, end_d):
                """
                指定期間の日次件数を連続日付で0埋めし、
                28日ローリング合計÷4 = 週平均換算値を返す。
                週次目標と同じスケールで直接比較できる。
                """
                import pandas as _pd
                all_dates = _pd.date_range(start=start_d, end=end_d, freq='D')
                daily = df_period.groupby('手術実施日').size().reset_index(name='件数')
                daily = daily.set_index('手術実施日').reindex(all_dates, fill_value=0).reset_index()
                daily.columns = ['手術実施日', '件数']
                daily['weekly_avg'] = daily['件数'].rolling(window=28, min_periods=14).sum() / 4
                daily['date_label'] = daily['手術実施日'].dt.strftime('%m/%d')
                return daily

            cy_daily = _build_weekly_series(df_d, rol_cy_st, max_d)
            py_daily = _build_weekly_series(df_d, rol_py_st, rol_py_end)

            dept_chart_data[dept] = {
                'cy': cy_daily[['date_label','weekly_avg']].round({'weekly_avg': 1}).to_dict(orient='records'),
                'py': py_daily[['date_label','weekly_avg']].round({'weekly_avg': 1}).to_dict(orient='records'),
                'cy_label': f"直近365日（{rol_cy_st}〜{max_d}）",
                'py_label': f"前年同時期（{rol_py_st}〜{rol_py_end}）",
                'weekly_target': weekly_tgt,
                'win_label': '28日移動平均（週換算）',
            }
    dept_chart_json = _json.dumps(dept_chart_data, ensure_ascii=False)

    ann_fy_lbl = f"{fy_yr}年度（{fy_st}〜{max_d}）"
    rol_fy_lbl = f"12ヶ月累計（{rol_p2_start}〜{max_d}）"
    py_fy_lbl  = f"{fy_yr - 1}年度（{py_st}〜{py_end}）"

    return dict(
        ann_p2=ann_p2, ann_p1=ann_p1,
        rol_p2=rol_p2, rol_p1=rol_p1,
        dept7=dept7, dept28=dept28,
        ann_fy=ann_fy,   ann_fy_lbl=ann_fy_lbl,
        rol_fy=rol_fy,   rol_fy_lbl=rol_fy_lbl,
        py_fy=py_fy,     py_fy_lbl=py_fy_lbl,
        dept_chart_json=dept_chart_json,
        max_d=max_d,
    )


def load_and_process_from_dir(data_dir: str):
    """
    generate_html.py の _generate_operation() から呼ばれるインターフェース。
    Returns:
        (df_raw, target_dict, report_data)
    """
    df_raw, target_dict = _load_op_csv_from_dir(data_dir)
    report_data = _build_report_data(df_raw, target_dict, mode="全身麻酔のみ")
    return df_raw, target_dict, report_data


def generate_html(df_raw, target_dict, report_data, portal_url="../portal.html"):
    """
    generate_html.py の _generate_operation() から呼ばれるインターフェース。
    create_full_html_report を呼び出して HTML 文字列を返す。
    """
    d = report_data
    # p2 は年度比較の今年度を初期値として使用
    p2 = d['ann_p2'] or d['rol_p2'] or {}
    p1 = d['ann_p1'] or d['rol_p1'] or {}

    html = create_full_html_report(
        p1_data=p1,
        p2_data=p2,
        mode_label="全身麻酔のみ",
        portal_url=portal_url,
        dept_rows_7=d['dept7'],
        dept_rows_28=d['dept28'],
        dept_rows_fy=d['ann_fy'],
        fy_label=d['ann_fy_lbl'],
        annual_p1=d['ann_p1'],
        annual_p2=d['ann_p2'],
        rolling_p1=d['rol_p1'],
        rolling_p2=d['rol_p2'],
        annual_dept_fy=d['ann_fy'],
        rolling_dept_fy=d['rol_fy'],
        annual_fy_label=d['ann_fy_lbl'],
        rolling_fy_label=d['rol_fy_lbl'],
        current_mode="年度比較",
        prev_year_dept_fy=d.get('py_fy'),
        prev_year_fy_label=d.get('py_fy_lbl', '昨年度'),
        dept_chart_json=d.get('dept_chart_json'),
    )
    return html
