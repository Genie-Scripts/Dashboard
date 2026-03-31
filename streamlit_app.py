"""
streamlit_app.py — 診療ダッシュボード Streamlitアプリ
ローカル確認用: streamlit run streamlit_app.py

【用途】
- 開発中のKPI・グラフをブラウザで素早く確認
- フッターの「HTML生成」ボタンで index.html を出力
"""

import sys
import os
from pathlib import Path
from datetime import datetime, timedelta

import streamlit as st
import pandas as pd
import plotly.graph_objects as go

# ── パス設定 ──────────────────────────────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from app.lib.config import DEFAULT_DATA_DIR, CHART_COLORS
from app.lib.data_loader import load_all
from app.lib.preprocess import (
    preprocess_admission, preprocess_surgery,
    build_target_lookup, build_surgery_target_lookup,
)
from app.lib.metrics import (
    build_kpi_summary, build_dept_ranking, build_surgery_ranking,
    build_daily_series, build_surgery_daily_series, add_moving_average,
    build_weekly_agg, achievement_rate,
    build_doctor_watch_ranking, build_doctor_gap_ranking,
    build_nurse_watch_ranking, build_nurse_load_ranking,
)


# ═══════════════════════════════════════════════════════
# 定数・スタイル
# ═══════════════════════════════════════════════════════

STATUS_EMOJI = {"ok": "🟢", "warn": "🟡", "ng": "🔴"}
STATUS_LABEL = {"ok": "達成", "warn": "注意", "ng": "未達", "neutral": "—"}

st.set_page_config(
    page_title="診療ダッシュボード",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap');
  html, body, [class*="css"] { font-family: 'Noto Sans JP', sans-serif; }

  /* ── CSS変数（仕様書 §9-2） ── */
  :root {
    --status-good: #16a34a;
    --status-warn: #d97706;
    --status-bad:  #dc2626;
    --status-info: #2563eb;
    --text-main:   #0f172a;
    --text-sub:    #64748b;
    --border-soft: #e2e8f0;
  }

  /* ── 判断カード（仕様書 §7、§14） ── */
  .kpi-card {
    background: #fff;
    border: 1px solid var(--border-soft);
    border-radius: 16px;
    padding: 20px;
    box-shadow: 0 2px 12px rgba(15,23,42,0.04);
    border-top: 3px solid #94a3b8;
    height: 100%;
  }
  .kpi-card.is-good { border-top-color: var(--status-good); }
  .kpi-card.is-warn { border-top-color: var(--status-warn); }
  .kpi-card.is-bad  { border-top-color: var(--status-bad);  }

  .kpi-card__head {
    display: flex; justify-content: space-between; align-items: flex-start;
    margin-bottom: 12px;
  }
  .kpi-card__title  { font-size: 14px; font-weight: 600; color: var(--text-main); }
  .kpi-card__period { font-size: 12px; color: var(--text-sub); margin-top: 2px; }

  .kpi-card__badge {
    padding: 4px 10px; border-radius: 999px;
    font-size: 12px; font-weight: 700; white-space: nowrap;
  }
  .is-good .kpi-card__badge { background: #dcfce7; color: #166534; }
  .is-warn .kpi-card__badge { background: #fef3c7; color: #92400e; }
  .is-bad  .kpi-card__badge { background: #fee2e2; color: #991b1b; }
  .is-neutral .kpi-card__badge { background: #f1f5f9; color: #475569; }

  .kpi-card__main { margin-bottom: 14px; }
  .kpi-card__value {
    font-size: 2.2rem; font-weight: 700; font-family: 'IBM Plex Mono', monospace;
    color: var(--text-main); line-height: 1.1;
  }
  .kpi-card__value .unit { font-size: 1rem; font-weight: 500; color: var(--text-sub); margin-left: 2px; }
  .kpi-card__gap { font-size: 14px; font-weight: 600; margin-top: 4px; }
  .gap-neg { color: var(--status-bad); }
  .gap-pos { color: var(--status-good); }
  .gap-neu { color: var(--text-sub); }

  .kpi-card__meta {
    display: grid; grid-template-columns: 1fr 1fr;
    gap: 6px 12px;
    padding-top: 12px;
    border-top: 1px solid var(--border-soft);
    font-size: 12px;
  }
  .meta-item { display: flex; justify-content: space-between; }
  .meta-item span { color: var(--text-sub); }
  .meta-item strong { color: var(--text-main); font-weight: 600; }

  /* ── Role Brief ── */
  .role-brief {
    display: flex; gap: 12px; flex-wrap: wrap;
    background: #f8fafc; border-radius: 10px;
    padding: 12px 16px; margin-bottom: 16px;
  }
  .role-brief__item {
    display: flex; align-items: center; gap: 6px;
    font-size: 13px; color: var(--text-main);
  }
  .rb-icon { font-size: 14px; }

  /* ── 旧バッジ（ランキング等で継続使用） ── */
  .badge-ok   { background:#dcfce7; color:#166534; padding:2px 7px; border-radius:4px; font-size:.72rem; font-weight:700; }
  .badge-ng   { background:#fee2e2; color:#991b1b; padding:2px 7px; border-radius:4px; font-size:.72rem; font-weight:700; }
  .badge-warn { background:#fef3c7; color:#92400e; padding:2px 7px; border-radius:4px; font-size:.72rem; font-weight:700; }
</style>
""", unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════
# データ読込（キャッシュ）
# ═══════════════════════════════════════════════════════

@st.cache_data(ttl=300, show_spinner="データ読込中...")
def load_data(data_dir: str):
    data  = load_all(data_dir)
    adm   = preprocess_admission(data["admission"])
    surg  = preprocess_surgery(data["surgery"])
    tgts  = build_target_lookup(data["inpatient_targets"])
    stgts = build_surgery_target_lookup(data["surgery_targets"])
    return adm, surg, tgts, stgts


# ═══════════════════════════════════════════════════════
# ステータス判定（仕様書 §9、95/105% 基準）
# ═══════════════════════════════════════════════════════

def get_status(ach):
    """達成率 → ステータス文字列。仕様書 §9 の色ルールに合わせ 95/105% 基準。"""
    if ach is None: return "neutral"
    if ach >= 105:  return "ok"
    if ach >= 95:   return "warn"
    return "ng"


# ═══════════════════════════════════════════════════════
# 判断カード表示（仕様書 §7）
# ═══════════════════════════════════════════════════════

def _gap_text(actual, target, unit: str) -> tuple[str, str]:
    """
    目標差分テキストと CSS クラスを返す。
    Returns: (表示テキスト, cssクラス)
    """
    if target is None or actual is None:
        return "目標未設定", "gap-neu"
    diff = actual - target
    if diff >= 0:
        return f"目標 +{diff:,.1f}{unit}", "gap-pos"
    else:
        return f"目標まで {diff:,.1f}{unit}", "gap-neg"


def kpi_card(label: str, period: str, value: str, unit: str,
             actual_num, target_num, achievement,
             meta_items: list, status: str = None):
    """
    判断カードを描画する。

    Args:
        label       : カードタイトル（例: "在院患者数"）
        period      : 時間軸ラベル（例: "昨日時点"）
        value       : 表示メイン値の文字列（例: "524"）
        unit        : 単位（例: "人"）
        actual_num  : 実値 float（gap計算用）
        target_num  : 目標値 float | None
        achievement : 達成率 float | None
        meta_items  : [{"lbl": str, "val": str}, ...] 参考値リスト（4件推奨）
        status      : 強制指定する場合に渡す。省略時は achievement から自動判定
    """
    if status is None:
        status = get_status(achievement)

    css_cls = {"ok": "is-good", "warn": "is-warn", "ng": "is-bad"}.get(status, "is-neutral")
    badge_label = STATUS_LABEL.get(status, "—")

    # 目標差分
    gap_text, gap_css = _gap_text(actual_num, target_num, unit)

    # 参考値グリッド（最大4件）
    meta_html = ""
    for item in meta_items[:4]:
        meta_html += (
            f'<div class="meta-item">'
            f'<span>{item["lbl"]}</span>'
            f'<strong>{item["val"]}</strong>'
            f'</div>'
        )

    st.markdown(f"""
    <div class="kpi-card {css_cls}">
      <div class="kpi-card__head">
        <div>
          <div class="kpi-card__title">{label}</div>
          <div class="kpi-card__period">{period}</div>
        </div>
        <div class="kpi-card__badge">{badge_label}</div>
      </div>
      <div class="kpi-card__main">
        <div class="kpi-card__value">{value}<span class="unit">{unit}</span></div>
        <div class="kpi-card__gap {gap_css}">{gap_text}</div>
      </div>
      <div class="kpi-card__meta">{meta_html}</div>
    </div>
    """, unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════
# グラフ生成（Plotly）
# ═══════════════════════════════════════════════════════

def make_trend_chart(series: pd.DataFrame, title: str, target: float,
                      unit: str = "人", ma_win: int = 7,
                      period: str = "12w", base_date=None) -> go.Figure:
    """移動平均メインのトレンドグラフ"""
    df = series.copy()
    if base_date is not None:
        period_days = {
            "7d": 7, "12w": 84, "24w": 168, "365d": 365
        }
        if period in period_days:
            cutoff = base_date - timedelta(days=period_days[period])
            df = df[df["日付"] >= cutoff]
        elif period == "fy":
            fy_y = base_date.year if base_date.month >= 4 else base_date.year - 1
            cutoff = pd.Timestamp(f"{fy_y}-04-01")
            df = df[df["日付"] >= cutoff]

    # 週次集約（12w以上）
    if period != "7d" and len(df) > 0:
        df["週開始"] = df["日付"] - pd.to_timedelta(df["日付"].dt.weekday, unit="D")
        df = df.groupby("週開始")["値"].mean().reset_index()
        df.columns = ["日付", "値"]

    df = add_moving_average(df, window=min(ma_win, len(df)))
    ma_col = f"MA{ma_win}"

    fig = go.Figure()

    import numpy as np
    x_dates = np.array(df["日付"].values)  # FutureWarning 回避

    # 棒グラフ（実績）
    fig.add_trace(go.Bar(
        x=x_dates, y=df["値"],
        name="実績",
        marker_color=CHART_COLORS["bar_fill"],
        opacity=0.5,
        hovertemplate=f"%{{y:.0f}}{unit}<extra>実績</extra>",
    ))

    # 移動平均（メイン）
    fig.add_trace(go.Scatter(
        x=x_dates, y=df[ma_col],
        mode="lines",
        name=f"{ma_win}日移動平均",
        line=dict(color=CHART_COLORS["moving_avg"], width=2.5),
        hovertemplate=f"%{{y:.1f}}{unit}<extra>MA</extra>",
    ))

    # 目標線
    if target and len(df) > 0:
        fig.add_hline(y=target, line_dash="dash",
                       line_color=CHART_COLORS["target"], line_width=1.5,
                       annotation_text=f"目標 {target:.0f}",
                       annotation_position="bottom right",
                       annotation_font_size=10)

    fig.update_layout(
        title=dict(text=title, font=dict(size=13)),
        height=260,
        margin=dict(t=36, b=36, l=48, r=16),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(gridcolor="#DCE1E9", linecolor="#DCE1E9"),
        yaxis=dict(gridcolor="#DCE1E9", linecolor="#DCE1E9", rangemode="tozero"),
        legend=dict(orientation="h", x=0, y=-0.22, font_size=10,
                    bgcolor="rgba(0,0,0,0)"),
        hovermode="x unified",
        font=dict(family="Noto Sans JP, sans-serif", size=11, color="#5A6A82"),
    )
    return fig


# ═══════════════════════════════════════════════════════
# サイドバー
# ═══════════════════════════════════════════════════════

def render_sidebar(adm, base_date_max):
    st.sidebar.header("⚙️ 設定")

    data_dir = st.sidebar.text_input("データディレクトリ", value=DEFAULT_DATA_DIR)

    # データフォルダ初期化（初回セットアップ）
    if st.sidebar.button("📁 データフォルダを初期化", use_container_width=True,
                          help="patient_data/ op_data/ などのサブフォルダを作成します"):
        try:
            from app.lib.data_loader import setup_data_dir
            setup_data_dir(data_dir)
            st.sidebar.success(
                f"✅ {data_dir}/ を初期化しました。\n\n"
                "各フォルダにデータファイルを配置してページを再読み込みしてください。"
            )
        except Exception as e:
            st.sidebar.error(f"❌ {e}")

    st.sidebar.markdown("---")
    st.sidebar.subheader("基準日")
    base_date = st.sidebar.date_input(
        "基準日",
        value=base_date_max.date(),
        min_value=(base_date_max - timedelta(days=365)).date(),
        max_value=base_date_max.date(),
    )

    st.sidebar.markdown("---")
    st.sidebar.subheader("表示期間")
    period = st.sidebar.radio(
        "期間",
        options=["24w", "365d", "fy"],
        format_func=lambda x: {"24w":"直近24週","365d":"直近1年","fy":"今年度"}[x],
        index=0,
    )

    st.sidebar.markdown("---")
    st.sidebar.subheader("診療科フィルター")
    depts = ["全体（全診療科）"] + sorted(
        adm[adm["科_表示"]]["診療科名"].dropna().unique().tolist())
    selected_dept = st.sidebar.selectbox("診療科", depts)
    dept_val = None if selected_dept == "全体（全診療科）" else selected_dept

    st.sidebar.markdown("---")
    sort_by = st.sidebar.radio(
        "ランキング並び順",
        ["achievement", "actual"],
        format_func=lambda x: "達成率順" if x == "achievement" else "実績数順",
    )

    st.sidebar.markdown("---")
    st.sidebar.subheader("🖨 HTML生成")
    st.sidebar.caption(
        "※ HTMLの生成は `python generate_html.py` でも実行できます"
    )
    out_file = st.sidebar.text_input("出力ファイル", value="doctor.html")

    if st.sidebar.button("📄 doctor.html / nurse.html を生成", use_container_width=True):
        with st.spinner("HTML生成中..."):
            try:
                from generate_html import generate
                out = generate(
                    data_dir=data_dir,
                    output=out_file,
                    verbose=False,
                )
                st.sidebar.success(
                    f"✅ 生成完了\n🩺 doctor.html\n👩‍⚕️ nurse.html"
                )
            except Exception as e:
                st.sidebar.error(f"❌ エラー: {e}")

    return pd.Timestamp(base_date), period, dept_val, sort_by, data_dir


# ═══════════════════════════════════════════════════════
# メイン
# ═══════════════════════════════════════════════════════

def main():
    # ── ヘッダー ──────────────────────────────────────
    st.markdown(
        '<div style="background:#1D2B3A;padding:14px 20px;border-radius:10px;'
        'border-bottom:2px solid #3A6EA5;margin-bottom:20px">'
        '<span style="background:#3A6EA5;color:#fff;font-size:.6rem;font-weight:700;'
        'letter-spacing:.14em;text-transform:uppercase;padding:3px 9px;border-radius:4px">'
        'KPI</span> '
        '<span style="color:#E8EEF5;font-size:1rem;font-weight:600;margin-left:10px">'
        '📊 診療ダッシュボード</span>'
        f'<span style="color:#7A90A8;font-size:.72rem;float:right;font-family:monospace">'
        f'📅 {datetime.now().strftime("%Y/%m/%d %H:%M")} 更新</span>'
        '</div>',
        unsafe_allow_html=True,
    )

    # 静的HTMLページへのリンクバナー
    st.info(
        "📄 静的HTMLページ: `make build` で **doctor.html**（医師版）と "
        "**nurse.html**（看護師版）を生成できます。　`make serve` でローカルサーバー起動。",
        icon="🔗",
    )

    # ── データ読込 ────────────────────────────────────
    try:
        adm, surg, targets, surg_targets = load_data(DEFAULT_DATA_DIR)
    except FileNotFoundError as e:
        st.error("❌ データが見つかりません")
        st.markdown(f"""
**エラー内容:**
```
{e}
```
**解決方法:**
1. サイドバーの「データディレクトリ」でパスを確認してください
2. フォルダ未作成の場合、サイドバーの **「📁 データフォルダを初期化」** を押してください
3. コマンドラインから初期化する場合: `python generate_html.py --setup`
""")
        with st.sidebar:
            st.header("⚙️ 設定")
            _dd = st.text_input("データディレクトリ", value=DEFAULT_DATA_DIR)
            if st.button("📁 データフォルダを初期化", use_container_width=True):
                try:
                    from app.lib.data_loader import setup_data_dir
                    setup_data_dir(_dd)
                    st.success("✅ 初期化完了。データを配置後にページを再読み込みしてください。")
                    st.balloons()
                except Exception as _e:
                    st.error(f"❌ {_e}")
        return
    except Exception as e:
        st.error(f"❌ データ読込エラー: {e}")
        st.exception(e)
        return

    base_date_max = adm["日付"].max()

    # ── サイドバー ────────────────────────────────────
    base_date, period, dept_val, sort_by, data_dir = render_sidebar(adm, base_date_max)

    # ── KPI算出 ───────────────────────────────────────
    kpi = build_kpi_summary(adm, surg, base_date, targets, surg_targets)
    inp   = kpi["inpatient"]
    nadm  = kpi["new_admission"]
    surg_ = kpi["surgery"]
    dis   = kpi["discharge"]
    or_util = kpi.get("or_utilization", 0)

    # ── タブ ─────────────────────────────────────────
    tab_doc, tab_nur, tab_profit = st.tabs(["🩺 医師向け", "👩‍⚕️ 看護師向け", "💰 粗利レポート"])

    # ─────────────────────────────────────────────────
    with tab_doc:

        # ── Role Brief（仕様書 §6）─────────────────────
        inp_ach   = inp["achievement"] or 0
        nadm_prog = nadm.get("weekly_progress") or 0
        ga_avg    = surg_["ga_rolling_avg"]
        ga_ach    = surg_["ga_achievement"] or 0
        ga_tgt    = surg_["ga_target"]

        inp_st  = get_status(inp_ach)
        nadm_st = get_status(nadm_prog)
        ga_st   = get_status(ga_ach)

        rb_icon = {"ok": "🟢", "warn": "🟡", "ng": "🔴", "neutral": "⚪"}
        inp_diff  = inp["value"] - inp["target"]
        wk_total  = nadm["weekly_total"]
        wk_target = int(nadm.get("weekly_target") or 385)

        if inp_st == "ok":
            inp_msg = f"在院患者数は目標を達成（{inp_diff:+.0f}人）"
        elif inp_st == "warn":
            inp_msg = f"在院患者数は目標に対しやや不足（{inp_diff:+.0f}人）"
        else:
            inp_msg = f"在院患者数が目標未達（{inp_diff:+.0f}人）"

        if nadm_st == "ok":
            nadm_msg = f"新入院は週目標達成ペース（週累計{wk_total}人）"
        elif nadm_st == "warn":
            nadm_msg = f"新入院の進捗はやや遅れ（週{wk_total}/{wk_target}人）"
        else:
            nadm_msg = f"新入院が週目標に対し遅れている（週{wk_total}/{wk_target}人）"

        if ga_avg is None:
            ga_msg = "全麻: データなし"
        elif ga_st == "ok":
            ga_msg = f"全麻は平日目標{ga_tgt}件を達成（直近7平日平均{ga_avg:.1f}件）"
        elif ga_st == "warn":
            ga_msg = f"全麻はやや不足（直近7平日平均{ga_avg:.1f}件 / 目標{ga_tgt}件）"
        else:
            ga_msg = f"全麻が平日目標{ga_tgt}件に未達（直近7平日平均{ga_avg:.1f}件）"

        st.markdown(
            '<div class="role-brief">'
            f'<div class="role-brief__item"><span class="rb-icon">{rb_icon[inp_st]}</span>{inp_msg}</div>'
            f'<div class="role-brief__item"><span class="rb-icon">{rb_icon[nadm_st]}</span>{nadm_msg}</div>'
            f'<div class="role-brief__item"><span class="rb-icon">{rb_icon[ga_st]}</span>{ga_msg}</div>'
            '</div>',
            unsafe_allow_html=True,
        )

        # ── KPIカード 4枚（判断カード構造）───────────────
        c1, c2, c3, c4 = st.columns(4)

        with c1:
            kpi_card(
                label="在院患者数", period="昨日時点",
                value=f"{inp['value']:,}", unit="人",
                actual_num=inp["value"], target_num=inp["target"],
                achievement=inp["achievement"],
                meta_items=[
                    {"lbl": "目標",   "val": f"{inp['target']:,}人"},
                    {"lbl": "7日MA",  "val": f"{inp['ma7']:.1f}人" if inp["ma7"] else "—"},
                    {"lbl": "前週比", "val": f"{inp['wow']:+.0f}人" if inp.get("wow") is not None else "—"},
                    {"lbl": "達成率", "val": f"{inp_ach:.1f}%"},
                ],
            )

        with c2:
            wk_tgt = nadm.get("weekly_target")
            kpi_card(
                label="新入院患者数", period="今週累計",
                value=f"{nadm['weekly_total']:,}", unit="人",
                actual_num=nadm["weekly_total"], target_num=wk_tgt,
                achievement=nadm_prog,
                meta_items=[
                    {"lbl": "週目標",   "val": f"{int(wk_tgt):,}人" if wk_tgt else "—"},
                    {"lbl": "昨日",     "val": f"{nadm['value']:,}人"},
                    {"lbl": "うち緊急", "val": f"{nadm['emergency']:,}人"},
                    {"lbl": "前週比",   "val": f"{nadm['wow']:+.0f}人" if nadm.get("wow") is not None else "—"},
                ],
            )

        with c3:
            last_cnt = surg_["ga_last_biz_count"]
            yr_avg   = surg_["ga_fy_biz_avg"]
            kpi_card(
                label="全身麻酔手術", period="直近7平日平均",
                value=f"{ga_avg:.1f}" if ga_avg is not None else "—", unit="件/日",
                actual_num=ga_avg, target_num=ga_tgt,
                achievement=ga_ach,
                meta_items=[
                    {"lbl": "平日目標",     "val": f"{ga_tgt}件/日"},
                    {"lbl": "直近平日実績", "val": f"{last_cnt}件" if last_cnt is not None else "—"},
                    {"lbl": "年度平均",     "val": f"{yr_avg:.1f}件/日" if yr_avg else "—"},
                    {"lbl": "今週累計",     "val": f"{surg_['weekly_ga']:,}件"},
                ],
            )

        with c4:
            kpi_card(
                label="緊急入院", period="昨日",
                value=f"{nadm['emergency']:,}", unit="人",
                actual_num=None, target_num=None,
                achievement=None,
                status="neutral",
                meta_items=[
                    {"lbl": "退院", "val": f"{dis['value']:,}人"},
                    {"lbl": "転入", "val": f"{dis['transfer_in']:,}人"},
                    {"lbl": "転出", "val": f"{dis['transfer_out']:,}人"},
                    {"lbl": "前週比", "val": f"{nadm['wow']:+.0f}人" if nadm.get("wow") is not None else "—"},
                ],
            )

        st.markdown("<div style='margin-top:20px'></div>", unsafe_allow_html=True)

        # ── ランキング ───────────────────────────────
        col_rank, col_insight = st.columns([3, 2])

        with col_rank:
            st.subheader("📋 診療科別ランキング")
            rank_tab = st.radio("指標", ["在院", "新入院", "全麻", "🔴 要注視"],
                                 horizontal=True, label_visibility="collapsed")

            if rank_tab == "在院":
                rdf = build_dept_ranking(adm, base_date, targets, "inpatient", sort_by)
                unit_str = "人"
            elif rank_tab == "新入院":
                rdf = build_dept_ranking(adm, base_date, targets, "new_admission", sort_by)
                unit_str = "人/週"
            elif rank_tab == "全麻":
                rdf = build_surgery_ranking(surg, base_date, surg_targets, sort_by)
                unit_str = "件/週"
            else:
                rdf = None
                unit_str = ""

            if rdf is None:
                # 要注視ランキング
                watch = build_doctor_watch_ranking(adm, surg, base_date, targets, surg_targets)
                status_colors = {"ok": "#1A9E6A", "warn": "#C87A00", "ng": "#C0293B", "neutral": "#94A3B8"}
                for i, r in enumerate(watch):
                    clr = status_colors.get(r["status"], "#94A3B8")
                    badge_nums = ["🥇", "🥈", "🥉"]
                    rank_icon = badge_nums[i] if i < 3 else f"**{i+1}**"
                    st.markdown(
                        f'{rank_icon} **{r["department_name"]}**  '
                        f'<span style="color:{clr};font-weight:700">'
                        f'スコア {r["watch_score"]:.1f}pt</span>  \n'
                        f'<small style="color:#5A6A82">{r["note"]}</small>',
                        unsafe_allow_html=True,
                    )
            elif len(rdf) == 0:
                st.info("データなし")
            else:
                name_col = "診療科" if "診療科" in rdf.columns else "病棟名"
                tgt_col  = "週目標" if rank_tab == "全麻" else "目標"

                for i, row in rdf.iterrows():
                    ach = row.get("達成率")
                    st_  = get_status(ach)
                    bar_color = {"ok":"#1A9E6A","warn":"#C87A00","ng":"#C0293B"}.get(st_, "#94A3B8")
                    with st.container():
                        cc1, cc2, cc3, cc4 = st.columns([1, 3, 2, 3])
                        with cc1:
                            badges = ["🥇","🥈","🥉"]
                            idx = row.get("順位", i+1) - 1
                            st.markdown(badges[idx] if idx < 3 else f"**{idx+1}**")
                        with cc2:
                            st.markdown(f"**{row[name_col]}**")
                        with cc3:
                            st.markdown(
                                f'<span style="font-family:monospace">{row["実績"]}{unit_str}</span>',
                                unsafe_allow_html=True)
                        with cc4:
                            tgt_val = row.get(tgt_col)
                            tgt_str = f"目標{tgt_val:.1f}" if tgt_val and pd.notna(tgt_val) else ""
                            ach_str = f"{ach:.1f}%" if ach is not None and pd.notna(ach) else "—"
                            st.markdown(
                                f'<span style="color:{bar_color};font-weight:700">'
                                f'{ach_str}</span> '
                                f'<span style="font-size:.72rem;color:#94A3B8">{tgt_str}</span>',
                                unsafe_allow_html=True)

        with col_insight:
            st.subheader("💡 注目診療科")
            inp_rdf = build_dept_ranking(adm, base_date, targets, "inpatient", "achievement")
            nadm_rdf = build_dept_ranking(adm, base_date, targets, "new_admission", "achievement")
            surg_rdf = build_surgery_ranking(surg, base_date, surg_targets, "achievement")

            if len(inp_rdf) > 0:
                st.markdown("**↑ 達成率上位**")
                for _, row in inp_rdf.head(3).iterrows():
                    ach = row.get("達成率")
                    st_ = get_status(ach)
                    icon = "🏆" if ach and ach >= 110 else "📈"
                    n = nadm_rdf[nadm_rdf["診療科"] == row["診療科"]]["実績"].values
                    s = surg_rdf[surg_rdf["診療科"] == row["診療科"]]["実績"].values if "診療科" in surg_rdf.columns else []
                    sub = []
                    if len(n): sub.append(f"新入院{n[0]}")
                    if len(s): sub.append(f"全麻{s[0]}件")
                    st.markdown(
                        f'{icon} **{row["診療科"]}** '
                        f'<span class="badge-{st_}">{ach:.1f}%</span>  \n'
                        f'<small>{"  /  ".join(sub)}</small>',
                        unsafe_allow_html=True,
                    )
                st.markdown("---")
                st.markdown("**↓ 達成率下位**")
                for _, row in inp_rdf[inp_rdf["達成率"].notna()].tail(2).iterrows():
                    ach = row.get("達成率")
                    st_ = get_status(ach)
                    icon = "⚠️" if ach and ach >= 70 else "🔴"
                    st.markdown(
                        f'{icon} **{row["診療科"]}** '
                        f'<span class="badge-{st_}">{ach:.1f}%</span>',
                        unsafe_allow_html=True,
                    )

        # ── グラフ ────────────────────────────────────
        st.markdown("<div style='margin-top:20px'></div>", unsafe_allow_html=True)

        g1, g2 = st.columns(2)
        with g1:
            dept_filter = dept_val
            s_inp = build_daily_series(adm, "在院患者数",
                                        group_col="診療科名" if dept_filter else None,
                                        group_val=dept_filter)
            inp_tgt = (targets.get("inpatient",{}).get("dept",{}).get(dept_filter)
                       if dept_filter
                       else targets.get("inpatient",{}).get("hospital",{}).get("全日", 567))
            st.plotly_chart(
                make_trend_chart(s_inp, f"在院患者数{'（'+dept_filter+'）' if dept_filter else ''}推移（MA）",
                                  inp_tgt, "人", period=period, base_date=base_date),
                use_container_width=True)

        with g2:
            s_nadm = build_daily_series(adm, "新入院患者数",
                                         group_col="診療科名" if dept_filter else None,
                                         group_val=dept_filter)
            nadm_tgt_weekly = (targets.get("new_admission",{}).get("dept",{}).get(dept_filter, 0) * 7
                                if dept_filter
                                else targets.get("new_admission",{}).get("hospital",{}).get("全日", 385))
            nadm_tgt_daily = nadm_tgt_weekly / 7 if nadm_tgt_weekly else 55
            st.plotly_chart(
                make_trend_chart(s_nadm, f"新入院患者数{'（'+dept_filter+'）' if dept_filter else ''}推移（MA）",
                                  nadm_tgt_daily, "人/日", period=period, base_date=base_date),
                use_container_width=True)

        g3, g4 = st.columns(2)
        with g3:
            s_ga = build_surgery_daily_series(surg, ga_only=True,
                                               dept=dept_filter if dept_filter else None)
            if dept_filter:
                # ── 診療科フィルター時：週次集計棒グラフ ──
                ga_weekly_tgt = surg_targets.get(dept_filter)
                # 週次集計
                if len(s_ga) > 0:
                    _wdf = s_ga.copy()
                    _wdf["週開始"] = _wdf["日付"] - pd.to_timedelta(_wdf["日付"].dt.weekday, unit="D")
                    _wk = _wdf.groupby("週開始")["値"].sum().reset_index()
                    _wk.columns = ["日付", "値"]
                else:
                    _wk = pd.DataFrame(columns=["日付", "値"])

                # 期間フィルター
                _period_map = {"7d": 7, "12w": 84, "24w": 168, "365d": 365, "fy": None}
                _days = _period_map.get(period, 84)
                if _days:
                    _wk = _wk[_wk["日付"] >= (base_date - pd.Timedelta(days=_days))]

                _bar_colors = []
                for _v in _wk["値"]:
                    if ga_weekly_tgt is None:
                        _bar_colors.append("rgba(13,148,136,0.6)")
                    elif _v >= ga_weekly_tgt:
                        _bar_colors.append("rgba(22,163,74,0.65)")
                    elif _v >= ga_weekly_tgt * 0.95:
                        _bar_colors.append("rgba(217,119,6,0.65)")
                    else:
                        _bar_colors.append("rgba(220,38,38,0.55)")

                _fig_ga = go.Figure()
                _fig_ga.add_trace(go.Bar(
                    x=[d.strftime("%m/%d") for d in _wk["日付"]],
                    y=_wk["値"].tolist(),
                    marker_color=_bar_colors,
                    text=[str(int(v)) for v in _wk["値"]],
                    textposition="outside",
                    textfont_size=9,
                    name="週合計件数",
                    hovertemplate="週合計: %{y}件<extra></extra>",
                ))
                if ga_weekly_tgt:
                    _fig_ga.add_hline(y=ga_weekly_tgt, line_dash="dash",
                                      line_color="#C0293B", line_width=1.5,
                                      annotation_text=f"週目標 {ga_weekly_tgt:.0f}件",
                                      annotation_position="bottom right",
                                      annotation_font_size=9)
                _fig_ga.update_layout(
                    title=dict(text=f"全身麻酔件数（{dept_filter}）週次", font_size=13),
                    height=300, margin=dict(t=36, b=60, l=48, r=14),
                    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                    xaxis=dict(type="category", tickangle=-30,
                               gridcolor="#DCE1E9", linecolor="#DCE1E9"),
                    yaxis=dict(gridcolor="#DCE1E9", linecolor="#DCE1E9",
                               title_text="件/週", rangemode="tozero"),
                    showlegend=False,
                    font=dict(family="Noto Sans JP, sans-serif", size=11, color="#5A6A82"),
                )
                st.plotly_chart(_fig_ga, use_container_width=True)
            else:
                # ── 病院全体：日次グラフ（目標21件/日）──
                ga_tgt = 21
                st.plotly_chart(
                    make_trend_chart(s_ga, "全身麻酔件数（全体）推移",
                                      ga_tgt, "件", ma_win=7, period=period, base_date=base_date),
                    use_container_width=True)

        with g4:
            if dept_filter:
                # ── 診療科フィルター時：手術室稼働率は不要、週次実績サマリーを表示 ──
                st.markdown("#### 📊 週次実績サマリー（手術）")
                _wk_surg = surg[(surg["手術実施日"] <= base_date) & surg["全麻"] &
                                (surg["実施診療科"] == dept_filter)].copy()
                if len(_wk_surg) > 0:
                    _wk_surg["週開始"] = _wk_surg["手術実施日"] - pd.to_timedelta(
                        _wk_surg["手術実施日"].dt.weekday, unit="D")
                    _wk_sum = _wk_surg.groupby("週開始").size().reset_index(name="件数")
                    _wk_sum = _wk_sum.sort_values("週開始", ascending=False).head(8)
                    _wk_sum["週"] = _wk_sum["週開始"].dt.strftime("%m/%d〜")
                    _tgt = surg_targets.get(dept_filter)
                    _wk_sum["達成率"] = (_wk_sum["件数"] / _tgt * 100).round(1) if _tgt else None
                    st.dataframe(
                        _wk_sum[["週", "件数"] + (["達成率"] if _tgt else [])].reset_index(drop=True),
                        use_container_width=True, hide_index=True)
                    if _tgt:
                        st.caption(f"週目標: {_tgt:.0f}件")
                else:
                    st.info("手術データなし")
            else:
                # ── 病院全体：手術室稼働率表示 ──
                from app.lib.config import OR_MINUTES_PER_ROOM, OR_ROOM_COUNT
                wk = surg[surg["稼働対象室"] & surg["平日"]].copy()
                or_daily = (wk.groupby("手術実施日")["稼働分"].sum().reset_index())
                or_daily.columns = ["日付", "値"]
                or_daily["値"] = (or_daily["値"] / (OR_MINUTES_PER_ROOM * OR_ROOM_COUNT) * 100).round(1)
                st.plotly_chart(
                    make_trend_chart(or_daily, "手術室稼働率（平日）",
                                      80, "%", ma_win=7, period=period, base_date=base_date),
                    use_container_width=True)

        # ── 指標定義 ─────────────────────────────────
        with st.expander("📖 指標定義・注意事項"):
            st.markdown("""
| 指標 | 定義 |
|---|---|
| 在院患者数 | 各日の在院患者総数。慣習上「入院患者数」とも呼ぶが実態は在院数。 |
| 新入院患者数 | 入院患者数 + 緊急入院患者数 |
| 全身麻酔（全麻） | 麻酔種別に「全身麻酔(20分以上：吸入もしくは静脈麻酔薬)」を含む手術件数 |
| 手術室稼働率 | 平日8:45〜17:15のOP-1〜10・OP-12（11室）占有時間 ÷ 510分×11室 |
| 移動平均 | 直近7日の単純移動平均（週次表示は週平均） |
| 達成率 | 実績÷目標×100。緑≥100% / 橙≥85% / 赤<85% |
| 目標 | 平日在院580・休日540・全日567人 / 新入院385人/週 / 全麻21件/日 |
""")

    # ─────────────────────────────────────────────────
    with tab_nur:

        # ── Role Brief（看護師版：仕様書 §6-1）──────────
        load_val    = (nadm["value"] + dis["transfer_in"]
                       + dis["value"] + dis["transfer_out"])
        in_out_diff = nadm["value"] - dis["value"]
        in_out_sign = "+" if in_out_diff >= 0 else ""

        nur_inp_st  = get_status(inp["achievement"] or 0)
        nur_nadm_st = get_status(nadm.get("weekly_progress") or 0)
        nur_load_st = "warn" if abs(in_out_diff) > 10 else "neutral"

        if nur_inp_st == "ok":
            nur_inp_msg = f"病棟稼働は目標を達成（在院{inp['value']}人）"
        elif nur_inp_st == "warn":
            nur_inp_msg = f"病棟稼働はやや不足（在院{inp['value']}人 / 目標{inp['target']}人）"
        else:
            nur_inp_msg = f"病棟稼働が目標を下回っている（在院{inp['value']}人 / 目標{inp['target']}人）"

        if abs(in_out_diff) <= 5:
            nur_load_msg = f"入退院バランスは均衡（入退差{in_out_sign}{in_out_diff}人）"
        elif in_out_diff > 5:
            nur_load_msg = f"入院超過により在院増加傾向（入退差{in_out_sign}{in_out_diff}人）"
        else:
            nur_load_msg = f"退院超過により在院減少傾向（入退差{in_out_sign}{in_out_diff}人）"

        nur_wk_total  = nadm["weekly_total"]
        nur_wk_target = int(nadm.get("weekly_target") or 385)
        if nur_nadm_st == "ok":
            nur_nadm_msg = f"新入院は週目標達成ペース（週累計{nur_wk_total}人）"
        elif nur_nadm_st == "warn":
            nur_nadm_msg = f"新入院の進捗はやや遅れ（週{nur_wk_total}/{nur_wk_target}人）"
        else:
            nur_nadm_msg = f"新入院が週目標に対し遅れている（週{nur_wk_total}/{nur_wk_target}人）"

        rb_icon = {"ok": "🟢", "warn": "🟡", "ng": "🔴", "neutral": "⚪"}
        st.markdown(
            '<div class="role-brief">'
            f'<div class="role-brief__item"><span class="rb-icon">{rb_icon[nur_inp_st]}</span>{nur_inp_msg}</div>'
            f'<div class="role-brief__item"><span class="rb-icon">{rb_icon[nur_load_st]}</span>{nur_load_msg}</div>'
            f'<div class="role-brief__item"><span class="rb-icon">{rb_icon[nur_nadm_st]}</span>{nur_nadm_msg}</div>'
            '</div>',
            unsafe_allow_html=True,
        )

        # ── KPIカード 4枚（看護師版：仕様書 §8-2）──────
        nc1, nc2, nc3, nc4 = st.columns(4)

        with nc1:
            kpi_card(
                label="在院患者数", period="昨日時点",
                value=f"{inp['value']:,}", unit="人",
                actual_num=inp["value"], target_num=inp["target"],
                achievement=inp["achievement"],
                meta_items=[
                    {"lbl": "目標",   "val": f"{inp['target']:,}人"},
                    {"lbl": "7日MA",  "val": f"{inp['ma7']:.1f}人" if inp["ma7"] else "—"},
                    {"lbl": "前週比", "val": f"{inp['wow']:+.0f}人" if inp.get("wow") is not None else "—"},
                    {"lbl": "達成率", "val": f"{inp['achievement']:.1f}%" if inp["achievement"] else "—"},
                ],
            )

        with nc2:
            nur_wk_tgt = nadm.get("weekly_target")
            kpi_card(
                label="新入院患者数", period="今週累計",
                value=f"{nadm['weekly_total']:,}", unit="人",
                actual_num=nadm["weekly_total"], target_num=nur_wk_tgt,
                achievement=nadm.get("weekly_progress"),
                meta_items=[
                    {"lbl": "週目標",   "val": f"{int(nur_wk_tgt):,}人" if nur_wk_tgt else "—"},
                    {"lbl": "昨日",     "val": f"{nadm['value']:,}人"},
                    {"lbl": "うち緊急", "val": f"{nadm['emergency']:,}人"},
                    {"lbl": "前週比",   "val": f"{nadm['wow']:+.0f}人" if nadm.get("wow") is not None else "—"},
                ],
            )

        with nc3:
            kpi_card(
                label="退院関連件数", period="昨日",
                value=f"{dis['value']:,}", unit="人",
                actual_num=None, target_num=None,
                achievement=None,
                status="neutral",
                meta_items=[
                    {"lbl": "転入",   "val": f"{dis['transfer_in']:,}人"},
                    {"lbl": "転出",   "val": f"{dis['transfer_out']:,}人"},
                    {"lbl": "入退差", "val": f"{in_out_sign}{in_out_diff}人"},
                    {"lbl": "—",      "val": "—"},
                ],
            )

        with nc4:
            kpi_card(
                label="出入り負荷", period="昨日",
                value=f"{load_val:,}", unit="件",
                actual_num=None, target_num=None,
                achievement=None,
                status="neutral",
                meta_items=[
                    {"lbl": "新入院", "val": f"{nadm['value']:,}人"},
                    {"lbl": "退院",   "val": f"{dis['value']:,}人"},
                    {"lbl": "転入",   "val": f"{dis['transfer_in']:,}人"},
                    {"lbl": "転出",   "val": f"{dis['transfer_out']:,}人"},
                ],
            )

        st.markdown("<div style='margin-top:16px'></div>", unsafe_allow_html=True)

        # 病棟フィルター
        from app.lib.config import WARD_NAMES, WARD_HIDDEN
        ward_options = ["全体（全病棟）"] + [
            f"{v}（{k}）" for k, v in sorted(WARD_NAMES.items())
            if k not in WARD_HIDDEN
        ]
        ward_sel_str = st.selectbox("🏥 病棟フィルター", ward_options)
        if ward_sel_str == "全体（全病棟）":
            ward_code_sel = None
        else:
            ward_code_sel = ward_sel_str.split("（")[-1].rstrip("）")

        # ランキング + インサイト
        wr_col, wi_col = st.columns([3, 2])

        with wr_col:
            st.subheader("🏥 病棟別ランキング")
            ward_rank_tab = st.radio(
                "指標", ["在院", "新入院", "出入り負荷"],
                horizontal=True, label_visibility="collapsed")

            from app.lib.metrics import build_ward_ranking
            if ward_rank_tab == "在院":
                wrdf = build_ward_ranking(adm, base_date, targets, "inpatient", sort_by)
                w_unit = "人"
                w_tgt_col = "目標"
            elif ward_rank_tab == "新入院":
                wrdf = build_ward_ranking(adm, base_date, targets, "new_admission", sort_by)
                w_unit = "人/週"
                w_tgt_col = "目標"
            else:
                # 出入り負荷：日次合計で集計
                day_df = adm[(adm["日付"] == base_date) & adm["病棟_表示"]]
                load_by_ward = (day_df.groupby(["病棟コード"])["出入り負荷"].sum()
                                .reset_index())
                load_by_ward.columns = ["病棟コード", "実績"]
                load_by_ward["病棟名"] = load_by_ward["病棟コード"].map(WARD_NAMES)
                load_by_ward["目標"] = None
                load_by_ward["達成率"] = None
                load_by_ward = load_by_ward.sort_values("実績", ascending=False)
                load_by_ward["順位"] = range(1, len(load_by_ward)+1)
                wrdf = load_by_ward
                w_unit = "件/日"
                w_tgt_col = "目標"

            if len(wrdf) == 0:
                st.info("データなし")
            else:
                name_col = "病棟名" if "病棟名" in wrdf.columns else "病棟コード"
                for i, row in wrdf.iterrows():
                    ach  = row.get("達成率")
                    st_  = get_status(ach)
                    icon = STATUS_EMOJI.get(st_, "⚪")
                    badges = ["🥇","🥈","🥉"]
                    idx = int(row.get("順位", i+1)) - 1
                    rank_str = badges[idx] if idx < 3 else f"**{idx+1}**"
                    tgt_val = row.get(w_tgt_col)
                    tgt_str = f" 目標{tgt_val:.1f}" if tgt_val and pd.notna(tgt_val) else ""
                    ach_str = f"{ach:.1f}%" if ach and pd.notna(ach) else "—"
                    bar_color = {"ok":"#1A9E6A","warn":"#C87A00","ng":"#C0293B"}.get(st_, "#94A3B8")
                    wc1, wc2, wc3, wc4 = st.columns([1, 3, 2, 3])
                    with wc1: st.markdown(rank_str)
                    with wc2: st.markdown(f"**{row[name_col]}**")
                    with wc3: st.markdown(
                        f'<span style="font-family:monospace">{row["実績"]}{w_unit}</span>',
                        unsafe_allow_html=True)
                    with wc4: st.markdown(
                        f'<span style="color:{bar_color};font-weight:700">{ach_str}</span>'
                        f'<span style="font-size:.72rem;color:#94A3B8">{tgt_str}</span>',
                        unsafe_allow_html=True)

        with wi_col:
            st.subheader("💡 注目病棟")
            ward_inp_rdf  = build_ward_ranking(adm, base_date, targets, "inpatient", "achievement")
            ward_nadm_rdf = build_ward_ranking(adm, base_date, targets, "new_admission", "achievement")

            if len(ward_inp_rdf) > 0:
                st.markdown("**↑ 達成率上位**")
                for _, row in ward_inp_rdf.head(3).iterrows():
                    ach = row.get("達成率")
                    st_  = get_status(ach)
                    icon = "🏆" if ach and ach >= 110 else "📈"
                    n = ward_nadm_rdf[ward_nadm_rdf["病棟名"] == row["病棟名"]]["実績"].values
                    sub = f"新入院{n[0]}人" if len(n) else ""
                    st.markdown(
                        f'{icon} **{row["病棟名"]}** '
                        f'<span class="badge-{st_}">{ach:.1f}%</span>'
                        f'{"  " + sub if sub else ""}',
                        unsafe_allow_html=True)
                st.markdown("---")
                st.markdown("**↓ 達成率下位**")
                for _, row in ward_inp_rdf[ward_inp_rdf["達成率"].notna()].tail(2).iterrows():
                    ach = row.get("達成率")
                    st_  = get_status(ach)
                    icon = "⚠️" if ach and ach >= 70 else "🔴"
                    st.markdown(
                        f'{icon} **{row["病棟名"]}** '
                        f'<span class="badge-{st_}">{ach:.1f}%</span>',
                        unsafe_allow_html=True)

        st.markdown("<div style='margin-top:20px'></div>", unsafe_allow_html=True)

        # グラフ
        from app.lib.metrics import build_daily_series as bds
        g1n, g2n = st.columns(2)

        def _ward_series(col, wcode=None):
            if wcode:
                return bds(adm, col, group_col="病棟コード",
                            group_val=wcode, display_filter=False)
            return bds(adm, col, display_filter=False)

        with g1n:
            s = _ward_series("在院患者数", ward_code_sel)
            tgt_v = (targets.get("inpatient",{}).get("ward",{}).get(ward_code_sel)
                     if ward_code_sel
                     else targets.get("inpatient",{}).get("hospital",{}).get("全日", 567))
            st.plotly_chart(
                make_trend_chart(s, f"在院患者数{'（'+WARD_NAMES.get(ward_code_sel,ward_code_sel)+'）' if ward_code_sel else ''}推移",
                                  tgt_v, "人", period=period, base_date=base_date),
                use_container_width=True)

        with g2n:
            s = _ward_series("新入院患者数", ward_code_sel)
            nadm_tgt_w = (targets.get("new_admission",{}).get("ward",{}).get(ward_code_sel)
                          if ward_code_sel else None)
            nadm_tgt_d = (nadm_tgt_w / 7) if nadm_tgt_w else (385 / 7)
            st.plotly_chart(
                make_trend_chart(s, f"新入院患者数{'（'+WARD_NAMES.get(ward_code_sel,ward_code_sel)+'）' if ward_code_sel else ''}推移",
                                  nadm_tgt_d, "人/日", period=period, base_date=base_date),
                use_container_width=True)

        g3n, g4n = st.columns(2)
        with g3n:
            s = _ward_series("退院合計", ward_code_sel)
            st.plotly_chart(
                make_trend_chart(s, f"退院患者数{'（'+WARD_NAMES.get(ward_code_sel,ward_code_sel)+'）' if ward_code_sel else ''}推移",
                                  None, "人/日", period=period, base_date=base_date),
                use_container_width=True)

        with g4n:
            s = _ward_series("出入り負荷", ward_code_sel)
            st.plotly_chart(
                make_trend_chart(s, f"出入り負荷{'（'+WARD_NAMES.get(ward_code_sel,ward_code_sel)+'）' if ward_code_sel else ''}推移",
                                  None, "件/日", period=period, base_date=base_date),
                use_container_width=True)

        with st.expander("📖 指標定義・注意事項（看護師向け）"):
            st.markdown("""
| 指標 | 定義 |
|---|---|
| 在院患者数 | 各日の在院患者総数 |
| 新入院患者数 | 入院患者数 + 緊急入院患者数の合計 |
| 退院患者数 | 退院 + 死亡退院の合計 |
| 出入り負荷 | 新入院 + 転入 + 退院 + 転出 の合計。病棟の業務量の目安 |
| 移動平均 | 直近7日の単純移動平均（週次表示は週平均） |
| 03B病棟 | 目標未設定のため達成率非表示。データは集計に含む |
""")

    # ─────────────────────────────────────────────────
    with tab_profit:
        st.subheader("💰 粗利レポート")

        # 粗利データ読込
        try:
            from app.lib.data_loader import load_profit_data, load_profit_targets
            from app.lib.profit import build_profit_monthly, build_profit_kpi, build_profit_chart_data
            profit_raw  = load_profit_data(data_dir)
            profit_tgts = load_profit_targets(data_dir)
            profit_mth  = build_profit_monthly(profit_raw, profit_tgts)
        except Exception as e:
            st.error(f"粗利データ読込エラー: {e}")
            st.stop()

        base_month = profit_mth["月"].max()
        profit_kpi = build_profit_kpi(profit_mth, base_month)

        # KPI カード
        pk1, pk2, pk3, pk4 = st.columns(4)
        ach_total = profit_kpi["hospital_achievement"]
        p_st = get_status(ach_total)
        with pk1:
            kpi_card(
                label="全科合計（直近月）",
                period="直近月",
                value=f"{profit_kpi['hospital_total']:.1f}",
                unit="百万円",
                actual_num=profit_kpi['hospital_total'],
                target_num=profit_kpi['hospital_target'],
                achievement=ach_total,
                meta_items=[
                    {"lbl": "月次目標", "val": f"{profit_kpi['hospital_target']:.0f}M"},
                    {"lbl": "達成率",   "val": f"{ach_total:.1f}%" if ach_total else "—"},
                ],
                status=p_st,
            )
        with pk2:
            ytd_ach = profit_kpi.get("hospital_ytd_achievement")
            kpi_card(
                label="年度累計粗利",
                period="年度累計",
                value=f"{profit_kpi['hospital_ytd']:.2f}",
                unit="十億円",
                actual_num=profit_kpi['hospital_ytd'],
                target_num=profit_kpi.get('hospital_ytd_target'),
                achievement=ytd_ach,
                meta_items=[
                    {"lbl": "年度目標", "val": f"{profit_kpi['hospital_ytd_target']:.2f}十億円"},
                    {"lbl": "達成率",   "val": f"{ytd_ach:.1f}%" if ytd_ach else "—"},
                ],
                status="neutral",
            )
        with pk3:
            st.markdown("**🏆 達成率上位3科**")
            for item in profit_kpi["top3"]:
                ach = item["achievement"]
                st_ = get_status(ach)
                st.markdown(
                    f'📈 **{item["name"]}** <span class="badge-{st_}">{ach:.1f}%</span>  '
                    f'<small>{item["actual"]:.1f}M / {item["target"] or "—"}M</small>',
                    unsafe_allow_html=True)
        with pk4:
            st.markdown("**⚠️ 達成率下位3科**")
            for item in profit_kpi["bottom3"]:
                ach = item["achievement"]
                st_ = get_status(ach)
                st.markdown(
                    f'📉 **{item["name"]}** <span class="badge-{st_}">{ach:.1f}%</span>  '
                    f'<small>{item["actual"]:.1f}M / {item["target"] or "—"}M</small>',
                    unsafe_allow_html=True)

        st.markdown("---")

        # 診療科フィルター
        all_profit_depts = sorted(profit_mth["診療科名"].dropna().unique().tolist())
        dept_sel = st.selectbox("診療科フィルター", ["全体（全診療科合計）"] + all_profit_depts)
        is_all = dept_sel == "全体（全診療科合計）"

        chart_data_p = build_profit_chart_data(profit_mth)

        if is_all:
            months = chart_data_p["global"]["months"]
            values = chart_data_p["global"]["values"]
            target_val = chart_data_p["global"]["targets"][-1] if chart_data_p["global"]["targets"] else None
            achs = None
        else:
            pd_dept = chart_data_p["by_dept"].get(dept_sel, {})
            months  = pd_dept.get("months", [])
            values  = pd_dept.get("values", [])
            target_val = pd_dept.get("target")
            achs    = pd_dept.get("achievements")

        # グラフ
        gc1, gc2 = st.columns(2)
        with gc1:
            # 月次棒グラフ
            import plotly.graph_objects as go
            bar_colors = []
            if achs:
                for a in achs:
                    bar_colors.append(
                        "rgba(22,163,74,0.65)"  if a and a >= 105 else
                        "rgba(217,119,6,0.65)"  if a and a >= 95  else
                        "rgba(220,38,38,0.55)"
                    )
            else:
                bar_colors = ["rgba(58,110,165,0.55)"] * len(values)

            fig = go.Figure()
            fig.add_trace(go.Bar(
                x=months, y=values,
                marker_color=bar_colors,
                text=[f"{v:.0f}" for v in values],
                textposition="outside",
                textfont_size=9,
                name="粗利",
                hovertemplate="%{x}: %{y:.1f}百万円<extra></extra>",
            ))
            if target_val:
                fig.add_hline(y=target_val, line_dash="dash",
                               line_color="#C0293B", line_width=1.5,
                               annotation_text=f"目標 {target_val:.0f}M",
                               annotation_position="bottom right",
                               annotation_font_size=10)
            fig.update_layout(
                title=dict(text="月次粗利推移（百万円）", font_size=13),
                height=300,
                margin=dict(t=36,b=60,l=48,r=14),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                xaxis=dict(type="category", tickangle=-30,
                            gridcolor="#DCE1E9", linecolor="#DCE1E9"),
                yaxis=dict(gridcolor="#DCE1E9", linecolor="#DCE1E9",
                            title_text="百万円", rangemode="tozero"),
                showlegend=False,
                font=dict(family="Noto Sans JP, sans-serif", size=11, color="#5A6A82"),
            )
            st.plotly_chart(fig, use_container_width=True)

        with gc2:
            # 達成率折れ線
            if achs and months:
                fig2 = go.Figure()
                fig2.add_trace(go.Scatter(
                    x=months, y=achs,
                    mode="lines+markers",
                    line=dict(color="#3A6EA5", width=2),
                    marker=dict(size=6,
                                color=["#16a34a" if a and a>=105 else "#d97706" if a and a>=95 else "#dc2626" for a in achs]),
                    hovertemplate="%{x}: %{y:.1f}%<extra>達成率</extra>",
                    name="達成率",
                ))
                fig2.add_hline(y=105, line_dash="dash", line_color="#16a34a",
                                line_width=1, annotation_text="105%",
                                annotation_position="bottom right",
                                annotation_font_size=9)
                fig2.add_hline(y=95, line_dash="dot", line_color="#d97706",
                                line_width=1, annotation_text="95%",
                                annotation_position="bottom right",
                                annotation_font_size=9)
                fig2.update_layout(
                    title=dict(text="月次達成率推移", font_size=13),
                    height=300,
                    margin=dict(t=36,b=60,l=48,r=14),
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    xaxis=dict(type="category", tickangle=-30,
                                gridcolor="#DCE1E9", linecolor="#DCE1E9"),
                    yaxis=dict(gridcolor="#DCE1E9", linecolor="#DCE1E9",
                                title_text="%", range=[50,130]),
                    showlegend=False,
                    font=dict(family="Noto Sans JP, sans-serif", size=11, color="#5A6A82"),
                )
                st.plotly_chart(fig2, use_container_width=True)
            else:
                st.info("全体集計では達成率グラフは非表示（診療科を選択してください）")

        # 月次明細テーブル
        st.markdown("#### 📅 月次明細（直近12ヶ月）")
        if is_all:
            show_df = (profit_mth.groupby("月")
                       .agg(粗利=("粗利","sum"), 月次目標=("月次目標","sum"))
                       .reset_index()
                       .sort_values("月", ascending=False)
                       .head(12))
            show_df["達成率"] = (show_df["粗利"] / show_df["月次目標"] * 100).round(1)
        else:
            show_df = (profit_mth[profit_mth["診療科名"] == dept_sel]
                       .sort_values("月", ascending=False)
                       .head(12)[["月","粗利","月次目標","達成率","前月比"]])

        # 表示用整形
        show_df = show_df.copy()
        show_df["月"] = show_df["月"].dt.strftime("%Y-%m")
        for col in ["粗利","月次目標"]:
            if col in show_df.columns:
                show_df[col] = (show_df[col] / 1000).round(1)
        if "前月比" in show_df.columns:
            show_df["前月比"] = (show_df["前月比"] / 1000).round(1)
        show_df = show_df.rename(columns={"粗利":"粗利(百万)", "月次目標":"目標(百万)",
                                            "前月比":"前月比(百万)"})

        def highlight_ach(val):
            if pd.isna(val): return ""
            try:
                v = float(val)
                if v >= 105: return "color: #16a34a; font-weight: bold"
                elif v >= 95: return "color: #d97706; font-weight: bold"
                else: return "color: #dc2626; font-weight: bold"
            except: return ""

        st.dataframe(
            show_df.style.applymap(highlight_ach, subset=["達成率"]),
            use_container_width=True,
            hide_index=True,
        )

        with st.expander("📖 粗利指標定義"):
            st.markdown("""
| 指標 | 定義 |
|---|---|
| 粗利 | 単位: 千円（元データ）/ 百万円（グラフ・KPI）|
| 月次目標 | 診療科別月次粗利目標（千円） |
| 達成率 | 実績 ÷ 月次目標 × 100。緑≥100% / 橙≥85% / 赤<85% |
| 年度進捗率 | 年度累計 ÷ (月次目標 × 経過月数) × 100 |
""")


if __name__ == "__main__":
    main()
