"""
config.py — 定数・マッピング定義
診療ダッシュボード共通設定

v2.1 変更点:
  - 在院目標: 580/540/567 → 600/550/583
  - 新入院目標: 385 → 380
  - ステータス閾値: 80% → 90%
  - 「稼働率」→「利用率」(病棟)
  - 術者別軸廃止
  - デザイントークン追加
"""

# ──────────────────────────────
# 病棟コード → 正式名称
# ──────────────────────────────
WARD_NAMES = {
    "02A": "2階A病棟", "02B": "2階B病棟", "03A": "3階A病棟",
    "03B": "3階B病棟",  # データのみ（目標なし → 表示除外）
    "04A": "4階A病棟", "04B": "ICU",
    "04C": "4階C病棟", "04D": "HCU",
    "05A": "5階A病棟", "05B": "5階B病棟",
    "06A": "6階A病棟", "06B": "6階B病棟",
    "07A": "7階A病棟", "07B": "7階B病棟",
    "08A": "8階A病棟", "08B": "8階B病棟",
    "09A": "9階A病棟", "09B": "9階B病棟",
}

# 表示除外病棟（目標未設定）
WARD_HIDDEN = {"03B"}

# 救命救急センター系病棟（4A/4C）。緊急入院・院内転棟が中心で、他病棟のような
# 「予定入院の曜日調整」「地域医療連携（紹介元）への働きかけ」という業務前提が成り立たない。
# 最終目標は病床稼働率を高く保つこと（救急受け入れの空床確保はそのための手段）。
# ICU/HCU との連携による病床管理が要。部門レポートの「この期間の一手」は専用文言にする。
EMERGENCY_WARDS = {"04A", "04C"}

# 重症ケア病棟（ICU=04B / HCU=04D）。新入院患者数よりも在院患者数・病床稼働率の増が最終目標。
# 院内急変・緊急手術後の受け入れが中心で、予定入院・紹介という業務前提が無い（4A/4C とは
# 「救急受け入れ」でなく「重症管理」が主軸である点が異なる）。「この期間の一手」は専用文言にする。
CRITICAL_CARE_WARDS = {"04B", "04D"}

# 救急科（診療科）。外科系・内科系いずれにも属さない特例。北極星は救急受け入れ＝
# 2次・3次救急の受け入れ増、救急車の応需台数増、救急外来（ER）滞在時間の短縮。
# 予定入院・紹介・地域連携という業務前提が無い。「この期間の一手」は専用文言にする。
ER_DEPTS = {"救急科"}

# ──────────────────────────────
# 診療科名の合算ルール
# ──────────────────────────────
DEPT_MERGE = {
    "感染症": "総合内科",
    "内科": "総合内科",
    # 粗利データ側の括弧付き名称を素の科名に正規化（targets / adm / surg と整合）
    "呼吸器内科（アレ含む）": "呼吸器内科",
    "腎内科（糖尿含む）": "腎内科",
}

# ダッシュボード非表示科（病棟集計には含むが表示しない）
DEPT_HIDDEN = {"健診センター", "麻酔科", "放射線診断科", None, ""}

# 新入院ダッシュボード 表示対象科（23科）
NADM_DISPLAY_DEPTS = {
    "リウマチ膠原病内科", "一般消化器外科", "眼科", "救急科", "形成外科",
    "血液内科", "呼吸器外科", "呼吸器内科", "産婦人科", "歯科口腔外科",
    "耳鼻咽喉科", "循環器内科", "小児科", "消化器内科", "心臓血管外科",
    "腎内科", "整形外科", "総合内科", "乳腺外科", "脳神経外科",
    "脳神経内科", "泌尿器科", "皮膚科",
}

# ──────────────────────────────
# 手術関連
# ──────────────────────────────

# 手術ダッシュボード表示対象12科
SURGERY_DISPLAY_DEPTS = {
    "皮膚科", "整形外科", "産婦人科", "歯科口腔外科",
    "耳鼻咽喉科", "泌尿器科", "一般消化器外科", "呼吸器外科",
    "心臓血管外科", "乳腺外科", "形成外科", "脳神経外科",
}

# 手術KPIの評価軸を「全手術（入院+外来、ORフィード内=手術データ全行）」に切替える科。
# 眼科は全麻がほぼ無いが術式ボリュームがあり、全手術件数で評価できる。
ALLSURG_NORTH_STAR_DEPTS = {"眼科"}

# 手術KPI評価対象（SURGERY_DISPLAY_DEPTS=全麻評価 ＋ ALLSURG_NORTH_STAR_DEPTS=全手術評価）。
SURGERY_EVAL_DEPTS = SURGERY_DISPLAY_DEPTS | ALLSURG_NORTH_STAR_DEPTS


def surgery_metric_label(dept: str, short: bool = False) -> str:
    """診療科の手術KPIラベル。眼科=全手術、他外科系=全身麻酔。"""
    if dept in ALLSURG_NORTH_STAR_DEPTS:
        return "手術" if short else "全手術"
    return "全麻" if short else "全身麻酔手術"


# 粗利の「手術モデル」評価対象科（allowlist）。
# = 全麻手術目標を持つ外科系(SURGERY_DISPLAY_DEPTS) ＋ 眼科。
#   眼科は全麻はほぼ無いが白内障など術式ボリュームがあり、術式NNLSで粗利を説明できる。
# これ以外の科（内科系）は在院ベース(ratio_fallback)に固定し、手術件数モデルを使わない。
# 理由: 内科系に手術件数モデル(OLS)が付くと、わずかな手術にモデルが張り付き、
#   当月に手術が無いと月末見込みが0へ崩落する（例: 血液内科が1件の全麻手術に
#   OLSが張り付き6月見込みが76→21へ潰れた事象。腎内科も同型の潜在ケース）。
PROFIT_SURGERY_DEPTS = SURGERY_DISPLAY_DEPTS | {"眼科"}

# ──────────────────────────────
# 2026-06-01 診療報酬改定 補正（粗利 学習パイプライン専用）
# ──────────────────────────────
# 2026年度改定（急性期一般入院料の引上げ・急性期総合体制加算3・物価対応料 入院/外来）で
# 粗利単価が区分別に一段上がった。粗利「学習」パイプライン（profit_estimate.py）では、
# 改定前(月 < FEE_REVISION_DATE)の確定粗利に下記係数を乗じて改定後スケールへ換算して
# 学習する（改定換算）。表示(profit.py)・δ恒等式(pl_projection.py)は生実績のまま。
#
# 係数の出所: 2026-07-17 測定（scripts/estimate_revision_uplift.py）。
#   改定後確定月 = 2026-06 の1か月のみの leakage-free バックキャスト
#   （改定前 2026-03〜05 の actual/proj 比 median で正規化）。
#   ※確定月が2〜3か月たまったら同スクリプトで再測定し、係数を更新すること。
FEE_REVISION_DATE = "2026-06-01"
FEE_REVISION_PROFIT_UPLIFT = {
    "外来": 1.053,
    "入院": 1.144,
}

# ──────────────────────────────
# 部門トリアージ 北極星KPI分類
# ──────────────────────────────
# 経営目標に直結する単一KPI（北極星）で科をランクするための分類。
#   外科系 → 全身麻酔手術件数（op）
#   内科系 → 在院患者数（inp）
# 外科系は手術目標を持つ SURGERY_DISPLAY_DEPTS と一致。内科系はそれ以外の表示科。
#
# 【眼科】全手術モードで手術評価対象へ移行済み（ALLSURG_NORTH_STAR_DEPTS）。
#   北極星KPIは全手術件数（週目標あり）で外科系として評価する。
SURGERY_NORTH_STAR_DEPTS = set(SURGERY_DISPLAY_DEPTS) | ALLSURG_NORTH_STAR_DEPTS
INTERNAL_NORTH_STAR_DEPTS = (
    (NADM_DISPLAY_DEPTS | SURGERY_DISPLAY_DEPTS) - SURGERY_NORTH_STAR_DEPTS
)

# 粗利のみ存在し、入院・手術の患者データに出ない科。
# dept.html に「粗利ページのみ」を表示するための表示集合。
# NADM/SURGERY_DISPLAY_DEPTS には入れない（alerts/triage の新入院・手術
# 判定に巻き込むと、患者データの無い科で誤判定/空処理になるため）。
PROFIT_ONLY_DISPLAY_DEPTS = {
    "放射線治療科", "メンタルケア科",
}

# 手術室稼働対象（正規化後の名称）
OR_ROOMS_ACTIVE = {f"OP-{i}" for i in range(1, 11)} | {"OP-12"}

# 全身麻酔判定キーワード
GA_KEYWORD = "全身麻酔(20分以上：吸入もしくは静脈麻酔薬)"

# 手術室稼働時間帯
OR_START_HOUR = 8
OR_START_MIN  = 45
OR_END_HOUR   = 17
OR_END_MIN    = 15
OR_MINUTES_PER_ROOM = 510  # 8.5h = 510分
OR_ROOM_COUNT = 11

# ──────────────────────────────
# 全角→半角 変換用（手術室名正規化）
# ──────────────────────────────
ZEN2HAN = str.maketrans(
    "０１２３４５６７８９−－",
    "0123456789--",
)

# ──────────────────────────────
# 営業平日判定
# ──────────────────────────────
import jpholiday as _jpholiday


def is_operational_day(dt) -> bool:
    """
    病院の営業平日かどうかを返す。

    除外条件:
      - 土曜・日曜（weekday >= 5）
      - 国民の祝日・振替休日（jpholiday で動的判定）
      - 年末年始（12/29〜12/31、1/1〜1/3）
    """
    import pandas as _pd
    ts = _pd.Timestamp(dt)
    if ts.weekday() >= 5:
        return False
    if ts.month == 12 and ts.day >= 29:
        return False
    if ts.month == 1 and ts.day <= 3:
        return False
    if _jpholiday.is_holiday(ts.date()):
        return False
    return True


# ──────────────────────────────
# 粗利 営業日換算評価
# ──────────────────────────────
# 月次目標を「1営業日あたり目標」に分解する際の標準営業日数（固定）。
# 月ごとの営業日数のばらつき（GW・お盆・年末年始）で達成率が歪まないよう、
# 達成率を 日次粗利 / 日次目標 で評価するための分母として使う。
STD_BIZ_DAYS_PER_MONTH = 20

# 入院粗利は土日祝も患者がゼロにならないため暦日基準で補正する。
# 年平均（365/12 ≒ 30.4167）を標準月日数として使う。
STD_CAL_DAYS_PER_MONTH = 365.0 / 12

_BIZ_DAYS_CACHE: dict = {}


def biz_days_in_month(month) -> int:
    """
    対象月（Timestampや日付）に含まれる営業平日数を返す。
    結果は (year, month) でキャッシュする。
    """
    import pandas as _pd
    ts = _pd.Timestamp(month)
    key = (ts.year, ts.month)
    if key in _BIZ_DAYS_CACHE:
        return _BIZ_DAYS_CACHE[key]
    start = ts.replace(day=1)
    end = (start + _pd.offsets.MonthEnd(0))
    count = sum(1 for d in _pd.date_range(start, end, freq="D") if is_operational_day(d))
    _BIZ_DAYS_CACHE[key] = count
    return count


def calendar_days_in_month(month) -> int:
    """対象月の暦日数（28〜31）を返す。"""
    import pandas as _pd
    ts = _pd.Timestamp(month)
    return (ts.replace(day=1) + _pd.offsets.MonthEnd(0)).day


# ──────────────────────────────
# Google Analytics 設定
# ──────────────────────────────
# 計測IDを設定すると全ページに GA タグが自動埋め込まれます（例: "G-XXXXXXXXXX"）
# 空文字のままの場合は埋め込みをスキップします
GA_MEASUREMENT_ID = "G-R843H5Z3R8"

# ──────────────────────────────
# 病院全体 KPI 目標値  ★v2.1 改定
# ──────────────────────────────
TARGET_INPATIENT_WEEKDAY = 600   # 平日目標（人）
TARGET_INPATIENT_HOLIDAY = 550   # 休日目標（人）
TARGET_INPATIENT_ALLDAY  = 582.8   # 全日目標（年間加重平均）
TARGET_ADMISSION_WEEKLY  = 379.2   # 新入院 週目標（人/週）
TARGET_GA_DAILY          = 21    # 全身麻酔 営業平日目標（件/営業平日）
TARGET_WEEKEND_RETENTION = 93    # 週末在院維持率 目標（%＝土日平均在院÷平日平均在院）

# ──────────────────────────────
# ステータス閾値  ★v2.1 改定 (80% → 90%)
# ──────────────────────────────
THRESHOLD_DANGER = 90   # 達成率 < 90% → 未達
THRESHOLD_OK     = 100  # 達成率 ≥ 100% → 達成
# 90% ≤ 達成率 < 100% → 接近


def status_label(achievement: float) -> str:
    """達成率からステータスを返す"""
    if achievement is None:
        return "neutral"
    if achievement < THRESHOLD_DANGER:
        return "danger"   # 未達
    if achievement < THRESHOLD_OK:
        return "warn"     # 接近
    return "ok"           # 達成


def status_display(achievement: float) -> dict:
    """達成率から表示用の色・形状・文言を返す（三重エンコーディング）"""
    st = status_label(achievement)
    return {
        "danger":  {"color": "#c4314b", "shape": "▼", "text": "未達", "bg": "#fdf0f2", "css": "dr"},
        "warn":    {"color": "#b45309", "shape": "―", "text": "接近", "bg": "#fef7ee", "css": "wr"},
        "ok":      {"color": "#0e7a54", "shape": "▲", "text": "達成", "bg": "#ecfdf5", "css": "ok"},
        "neutral": {"color": "#9daab8", "shape": "—", "text": "—",   "bg": "#f6f8fb", "css": "mu"},
    }[st]


# ──────────────────────────────
# 病棟利用率ヒートマップ色スケール ★v2.1
# ──────────────────────────────
# 利用率は高いほうが望ましい（経営目標）
HEATMAP_SCALE = [
    # (閾値上限, 色名, 意味)
    (85,  "danger",  "赤系: 利用率不足"),
    (95,  "warn",    "オレンジ系: もう少し"),
    (999, "ok",      "緑系: 良好"),
]

# ──────────────────────────────
# データフォルダ
# ──────────────────────────────
DEFAULT_DATA_DIR = "data"

# ──────────────────────────────
# 部門別レポートPDF（印刷ハンドアウト）
# ──────────────────────────────
# レポート用紙のレターヘッド（病院名）。空文字なら部門名のみ表示。
# 例: REPORT_HOSPITAL_NAME = "○○総合病院"
REPORT_HOSPITAL_NAME = ""

# 公開ダッシュボードのベースURL（部門レポートPDFのQRコード等で使用）
PUBLIC_BASE_URL = "https://genie-scripts.github.io/Dashboard/"

DATA_FOLDERS = {
    "patient_data":   "patient_data",
    "patient_target": "patient_target",
    "op_data":        "op_data",
    "op_target":      "op_target",
    "profit_data":    "profit_data",
    "profit_target":  "profit_target",
    # 外来件数（粗利推計の特徴量）。将来の日次フィード用 base+追記フォルダ。
    # 現状は隣リポ Outpatient-Dashboard の集計CSVを既定ソースにする（下記参照）。
    "outpatient_data": "outpatient_data",
}

MERGE_STRATEGY = "newer_wins"

# ──────────────────────────────
# 外来データ連携（粗利推計の特徴量用 / 表示統合はしない）
# ──────────────────────────────
import os as _os

# 外来集計CSV (data/aggregated/YYYY-MM/02_dept_monthly.csv) のルート。
# 別運用の隣リポ Outpatient-Dashboard を既定参照（集計CSVはgit管理済み・非個人情報）。
# 環境変数 OUTPATIENT_AGG_DIR で上書き可。
OUTPATIENT_AGG_DIR = _os.environ.get(
    "OUTPATIENT_AGG_DIR",
    _os.path.expanduser("~/dev/ai-apps/Outpatient-Dashboard/data/aggregated"),
)

# 外来側の診療科名 → 粗利側の診療科名 への畳み込み。
# DEPT_MERGE（感染症/内科→総合内科）に加え、粗利データが括弧付きで内包している
# 科を外来側でも同じ親科に寄せる:
#   - 粗利「呼吸器内科（アレ含む）」→「呼吸器内科」なので 外来 アレルギー科 を呼吸器内科へ
#   - 粗利「腎内科（糖尿含む）」→「腎内科」なので 外来 糖尿病内分泌内科 を腎内科へ
# これにより外来件数の合算範囲を粗利の科定義に一致させる。
OUTPATIENT_DEPT_MERGE = {
    **DEPT_MERGE,
    "アレルギー科": "呼吸器内科",
    "糖尿病内分泌内科": "腎内科",
}

# ──────────────────────────────
# グラフ用デザイントークン  ★v2.1 更新
# ──────────────────────────────
CHART_COLORS = {
    "actual":      "#3A6EA5",
    "moving_avg":  "#0D9488",
    "target":      "#C0293B",
    "yoy":         "#94A3B8",
    "bar_fill":    "rgba(58,110,165,0.6)",
    "bar_fill_ga": "rgba(13,148,136,0.6)",
}

# ──────────────────────────────
# UIデザイントークン（CSS変数と対応）★v2.1 新規
# ──────────────────────────────
UI_TOKENS = {
    # Base
    "bg":          "#f6f8fb",
    "surface":     "#ffffff",
    "ink":         "#1a2332",
    "sub":         "#5f7084",
    "muted":       "#9daab8",
    "line":        "#dfe5ed",
    "hover":       "#f0f4f9",
    # Brand
    "brand":       "#0e4da4",
    "brand_light": "#e8f0fe",
    "brand_dark":  "#0a3671",
    # Status
    "st_danger":      "#c4314b",
    "st_danger_bg":   "#fdf0f2",
    "st_danger_text": "#8c1d35",
    "st_warn":        "#b45309",
    "st_warn_bg":     "#fef7ee",
    "st_warn_text":   "#7c3a06",
    "st_ok":          "#0e7a54",
    "st_ok_bg":       "#ecfdf5",
    "st_ok_text":     "#065f42",
    "st_info":        "#2563eb",
    "st_info_bg":     "#eff6ff",
}

# KPIアイコン
KPI_ICONS = {
    "inpatient": "🛏️",
    "admission": "🚪",
    "operation": "💉",
}

# 軸アイコン
AXIS_ICONS = {
    "dept": "🩺",
    "ward": "🏥",
}

# ──────────────────────────────
# ヘッドライン自動生成ルール ★v2.1 新規
# ──────────────────────────────
def build_headline(kpi_summary: dict) -> dict:
    """
    3 KPIの達成率からヘッドラインメッセージを自動生成する。

    Returns:
        {"level": "danger|warn|ok", "icon": "🔴|🟡|🟢",
         "text": "...", "detail": "..."}
    """
    rates = {
        "在院患者数":   kpi_summary.get("inpatient_rate"),
        "新入院患者数": kpi_summary.get("admission_rate"),
        "全身麻酔手術": kpi_summary.get("operation_rate"),
    }

    # KPI名短縮
    SHORT = {"在院患者数": "在院", "新入院患者数": "新入院", "全身麻酔手術": "全麻"}

    # 未達(<90%)と接近(90-100%)を分離
    danger = {k: v for k, v in rates.items() if v is not None and v < THRESHOLD_DANGER}
    warn   = {k: v for k, v in rates.items() if v is not None and THRESHOLD_DANGER <= v < THRESHOLD_OK}
    n_danger = len(danger)
    n_warn = len(warn)

    if n_danger == 0 and n_warn == 0:
        level, icon, text = "ok", "🟢", "全指標が目標を達成しています"
    elif n_danger == 0 and n_warn > 0:
        names = sorted(warn.keys(), key=lambda k: warn[k])
        joined = "と".join(SHORT.get(n, n) for n in names)
        level, icon = "warn", "🟡"
        text = f"{joined}が目標をやや下回っています"
    else:
        # 未達KPIを「と」で列挙
        names = sorted(danger.keys(), key=lambda k: danger[k])
        joined = "と".join(SHORT.get(n, n) for n in names)
        severity = "大きく下回って" if n_danger >= 2 else "下回って"
        level, icon = "danger", "🔴"
        text = f"{joined}が目標を{severity}います"

    # 補足行（トレンド矢印付き）
    inp = kpi_summary
    detail_parts = []
    if inp.get("inpatient_actual") is not None:
        tgt = inp.get("inpatient_target", TARGET_INPATIENT_ALLDAY)
        gap = inp["inpatient_actual"] - tgt
        td = inp.get("trend_inp", {})
        trend_str = f" {td.get('label','')}" if td.get("label") else ""
        detail_parts.append(f"在院 {inp['inpatient_actual']}人/目標{tgt}人（{gap:+.0f}人）{trend_str}")
    if inp.get("admission_actual_7d") is not None:
        gap = inp["admission_actual_7d"] - TARGET_ADMISSION_WEEKLY
        td = inp.get("trend_adm", {})
        trend_str = f" {td.get('label','')}" if td.get("label") else ""
        detail_parts.append(f"新入院7日 {inp['admission_actual_7d']}人/目標{TARGET_ADMISSION_WEEKLY}人（{gap:+.0f}人）{trend_str}")
    if inp.get("operation_daily_avg") is not None:
        gap = inp["operation_daily_avg"] - TARGET_GA_DAILY
        td = inp.get("trend_op", {})
        trend_str = f" {td.get('label','')}" if td.get("label") else ""
        detail_parts.append(f"全麻 {inp['operation_daily_avg']:.1f}件/目標{TARGET_GA_DAILY}件（{gap:+.1f}件）{trend_str}")

    return {
        "level": level,
        "icon": icon,
        "text": text,
        "detail": "、".join(detail_parts),
    }
