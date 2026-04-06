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

# ──────────────────────────────
# 診療科名の合算ルール
# ──────────────────────────────
DEPT_MERGE = {"感染症": "総合内科", "内科": "総合内科"}

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
# 病院全体 KPI 目標値  ★v2.1 改定
# ──────────────────────────────
TARGET_INPATIENT_WEEKDAY = 600   # 平日目標（人）
TARGET_INPATIENT_HOLIDAY = 550   # 休日目標（人）
TARGET_INPATIENT_ALLDAY  = 583   # 全日目標（年間加重平均）
TARGET_ADMISSION_WEEKLY  = 380   # 新入院 週目標（人/週）
TARGET_GA_DAILY          = 21    # 全身麻酔 営業平日目標（件/営業平日）

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

DATA_FOLDERS = {
    "patient_data":   "patient_data",
    "patient_target": "patient_target",
    "op_data":        "op_data",
    "op_target":      "op_target",
    "profit_data":    "profit_data",
    "profit_target":  "profit_target",
}

MERGE_STRATEGY = "newer_wins"

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

    # 補足行
    inp = kpi_summary
    detail_parts = []
    if inp.get("inpatient_actual") is not None:
        tgt = inp.get("inpatient_target", TARGET_INPATIENT_ALLDAY)
        gap = inp["inpatient_actual"] - tgt
        detail_parts.append(f"在院 {inp['inpatient_actual']}人/目標{tgt}人（{gap:+.0f}人）")
    if inp.get("admission_actual_7d") is not None:
        gap = inp["admission_actual_7d"] - TARGET_ADMISSION_WEEKLY
        detail_parts.append(f"新入院7日 {inp['admission_actual_7d']}人/目標{TARGET_ADMISSION_WEEKLY}人（{gap:+.0f}人）")
    if inp.get("operation_daily_avg") is not None:
        gap = inp["operation_daily_avg"] - TARGET_GA_DAILY
        detail_parts.append(f"全麻 {inp['operation_daily_avg']:.1f}件/目標{TARGET_GA_DAILY}件（{gap:+.1f}件）")

    return {
        "level": level,
        "icon": icon,
        "text": text,
        "detail": "、".join(detail_parts),
    }
