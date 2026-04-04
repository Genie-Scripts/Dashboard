# 提案B 2層ハブ＆スポーク型 診療ダッシュボード 統合仕様書

| 項目 | 内容 |
|---|---|
| バージョン | 2.1 |
| 作成日 | 2026-04-03 |
| 前版 | v1.0（2026-04-02） |
| 対象リポジトリ | Genie-Scripts/Dashboard |
| 対象構造 | portal.html（Layer-1）+ detail.html（Layer-2） |
| 参照ワイヤーフレーム | v2_portal_pc/sp.html, v2_detail_pc/sp.html |
| 参照デザインシステム | design_system.html |

### 変更履歴

| 版 | 日付 | 変更内容 |
|---|---|---|
| 1.0 | 2026-04-02 | 初版。データ基盤・KPI算出・画面表示仕様 |
| 2.0 | 2026-04-03 | デザインシステム統合、v2ワイヤーフレームUI要素マッピング、レスポンシブ・ロール最適化 |
| 2.1 | 2026-04-03 | 目標値改定(在院600/550/583、新入院380)、ステータス閾値90%化、稼働率→利用率統一、術者別軸廃止、手術集計の営業平日/全日二重基準明文化 |

---

# 第I部 デザインシステム

## 第1章 カラーパレット

### 1.1 設計原則

- **色覚多様性対応**: 日本人男性の約5%（P型/D型色覚）が赤-緑の判別困難。赤系は青み寄りに、緑系は青緑寄りに補正し、さらに形状（▼▲●）と文言で冗長表現する
- **WCAG 2.2 AA準拠**: 通常テキスト 4.5:1以上、大きい文字 3:1以上のコントラスト比を確保
- **医療施設の落ち着き**: ベースカラーは寒色系（ネイビー）で信頼感を演出。背景は薄いグレーブルーで目の疲労を低減

### 1.2 ベースカラー

| トークン名 | CSS変数 | カラーコード | 用途 |
|---|---|---|---|
| Background | `--bg` | `#f6f8fb` | ページ背景 |
| Surface | `--surface` | `#ffffff` | カード・パネル背景 |
| Ink | `--ink` | `#1a2332` | 本文テキスト |
| Sub | `--sub` | `#5f7084` | 補足テキスト |
| Muted | `--muted` | `#9daab8` | 注記・フッター |
| Line | `--line` | `#dfe5ed` | 罫線・ボーダー |
| Hover | `--hover` | `#f0f4f9` | ホバー背景 |

### 1.3 ブランドカラー

| トークン名 | CSS変数 | カラーコード | 用途 |
|---|---|---|---|
| Brand | `--brand` | `#0e4da4` | ヘッダー、選択中タブ、主要ボタン |
| Brand Light | `--brand-light` | `#e8f0fe` | 選択中ボタン背景、ホバー |
| Brand Dark | `--brand-dark` | `#0a3671` | ヘッダー背景 |

### 1.4 ステータスカラー（色覚多様性対応版）

| 状態 | CSS変数(前景) | コード | CSS変数(背景) | 背景色 | CSS変数(テキスト) | テキスト色 | コントラスト比 |
|---|---|---|---|---|---|---|---|
| 未達(Danger) | `--st-danger` | `#c4314b` | `--st-danger-bg` | `#fdf0f2` | `--st-danger-text` | `#8c1d35` | 5.8:1 |
| 接近(Warning) | `--st-warn` | `#b45309` | `--st-warn-bg` | `#fef7ee` | `--st-warn-text` | `#7c3a06` | 5.2:1 |
| 達成(OK) | `--st-ok` | `#0e7a54` | `--st-ok-bg` | `#ecfdf5` | `--st-ok-text` | `#065f42` | 5.4:1 |
| 情報(Info) | `--st-info` | `#2563eb` | `--st-info-bg` | `#eff6ff` | — | — | — |

### 1.5 三重エンコーディング規則

色＋形状＋文言を常にセットで表示する。

| 状態 | 色 | 形状 | 文言 | 表示例 |
|---|---|---|---|---|
| 未達（達成率<90%） | `#c4314b` | ▼ 下向き三角 | 「▼ 未達」 | KPIカードバッジ、ランキング行 |
| 接近（90%≤達成率<100%） | `#b45309` | ― 横線 | 「― 接近」 | ランキング行 |
| 達成（達成率≥100%） | `#0e7a54` | ▲ 上向き三角 | 「▲ 達成」 | ランキング行 |
| 情報 | `#2563eb` | → 矢印 | 「参考」 | 前年比較値 |

### 1.6 グラフカラートークン

| 用途 | カラーコード | 線種 |
|---|---|---|
| 実績（棒/線） | `#3A6EA5` | 実線 |
| 移動平均 | `#0D9488` | 実線 |
| 目標ライン | `#C0293B` | 破線 |
| 前年度 | `#94A3B8` | 点線 |
| 棒グラフ塗り | `rgba(58,110,165,0.6)` | — |
| 全麻棒グラフ塗り | `rgba(13,148,136,0.6)` | — |

### 1.7 シャドウ・角丸

| トークン | CSS変数 | 値 | 適用先 |
|---|---|---|---|
| Shadow SM | `--shadow-sm` | `0 1px 3px rgba(10,22,42,.06)` | ボタン、ランキング行 |
| Shadow MD | `--shadow-md` | `0 4px 14px rgba(10,22,42,.08)` | KPIカード、ドリルダウン |
| Shadow LG | `--shadow-lg` | `0 8px 28px rgba(10,22,42,.10)` | カードhover |
| Radius SM | `--r-sm` | `10px` | ボタン |
| Radius MD | `--r-md` | `14px` | ランキング行 |
| Radius LG | `--r-lg` | `18px` | KPIカード、タブバー、パネル |

### 1.8 フォント

```css
--font: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Noto Sans JP", "Segoe UI", sans-serif;
```

| 要素 | PC (size/weight) | SP (size/weight) |
|---|---|---|
| KPIメイン数値 | 40px / 900 | 30px / 900 |
| セクション見出し | 18px / 800 | 16px / 800 |
| カード名・ランキング名 | 14px / 700 | 13px / 700 |
| 補足説明 | 13px / 400 | 12px / 400 |
| 注記・フッター | 11px / 400 | 11px / 400 |

---

## 第2章 アイコン設計

絵文字ベース。KPIごとに固有アイコンを割り当て全画面で統一。

### 2.1 KPIアイコン

| KPI | アイコン | 意味 |
|---|---|---|
| 在院患者数 | 🛏️ | ベッド＝「今いる患者」 |
| 新入院患者数 | 🚪 | ドア＝「入ってくる患者」 |
| 全身麻酔手術 | 💉 | 注射器＝手術・麻酔 |

### 2.2 ナビゲーション・軸切替アイコン

| 軸 | アイコン | 意味 |
|---|---|---|
| 診療科別 | 🩺 | 聴診器＝医師視点 |
| 病棟別 | 🏥 | 病院＝看護師視点 |
| 術者別 | 👨‍⚕️ | ※本版では公開ダッシュボード上非表示（個人名のため） |

### 2.3 ステータス・アクションアイコン

| 用途 | アイコン | 使用箇所 |
|---|---|---|
| 要注視 | 🔴 | ヘッドラインアラート |
| 改善 | 📈 | 改善トピック |
| 推移グラフ | 📊 | グラフセクション見出し |
| ランキング | 🏆 | 達成状況セクション見出し |
| 詳細展開 | 📋 | ドリルダウン見出し |
| 印刷 | 🖨️ | ヘッダー右端アクション |

---

## 第3章 ボタン文言・文言トーン

### 3.1 ツールバーボタン文言

| 用途 | 推奨文言 | ボタンスタイル |
|---|---|---|
| 軸: 診療科 | `🩺 診療科別` | Primary（選択中軸） |
| 軸: 病棟 | `🏥 病棟別` | Active（選択中軸） |
| ~~軸: 術者~~ | ~~`👨‍⚕️ 術者別`~~ | v2.1で廃止（個人名が公開不可） |
| 期間: 7日 | `直近7日` | Ghost/Active |
| 期間: 28日 | `直近28日` | Ghost/Active |
| 期間: 年度 | `今年度` | Ghost/Active |
| 比較 | `🔄 前年度と比較` | Ghost |
| 印刷 | `🖨️ 印刷` | Ghost |

ボタンスタイル定義:
- **Primary**: `background:--brand; color:#fff; border-color:--brand`
- **Active**: `background:--brand-light; color:--brand; border-color:#93b4e4`
- **Ghost**: `background:transparent; color:--sub`

### 3.2 KPIカード内文言

| 要素 | 推奨 | 禁止 | 理由 |
|---|---|---|---|
| ステータスバッジ | `▼ 未達` / `― 接近` / `▲ 達成` | 「赤」「黄」「緑」 | 色名でなく状態名。色覚対応 |
| 差分表示 | `目標まで −35.0人` | 「ギャップ -35」 | 英語排除、単位明示 |
| 期間表示 | `4/1 時点` / `直近7日累計` | 「7d」「as of」 | 英語略語排除 |
| 導線ヒント | `詳細を見る ›` | 「Click here」 | 日本語・行動誘導 |

### 3.3 文言トーン5原則

1. **事実ベース**: 「悪い」「良い」の価値判断を避け、数値差分で客観的に伝える
2. **アクション示唆**: 「何に注目すべきか」を暗示する
3. **短く、敬語不使用**: ダッシュボードはUI。体言止めで最短に
4. **責任追及しない**: 「○○科が悪い」→「○○科 新入院 目標差 −14人」（数値のみ）
5. **改善も伝える**: 未達だけでなく改善トピック1件を必ず表示

### 3.4 ヘッドライン自動生成ロジック

| 条件 | ヘッドライン | アイコン |
|---|---|---|
| 3 KPI すべて達成 | 全指標が目標を達成しています | 🟢 |
| 2 達成/1 未達 | 〔未達KPI名〕が目標をやや下回っています | 🟡 |
| 1 達成/2 未達 | 〔主要未達KPI名〕が目標を下回っています | 🔴 |
| 3 KPI すべて未達 | 〔最大乖離KPI名〕が目標を大きく下回っています | 🔴 |

### 3.5 印刷対応

- ヘッダー・ツールバー: `@media print` で非表示
- KPIタブ: 全タブ展開して縦並べ
- グラフ: Plotly の `toImage()` で静的画像出力

---

# 第II部 データ基盤

## 第4章 データソース

### 4.1 入力データ一覧

| # | フォルダ | 内容 | 形式 | エンコーディング | 複数ファイル |
|---|---|---|---|---|---|
| 1 | patient_data/ | 入院データ | .xlsx/.csv | utf-8-sig | ✅自動マージ |
| 2 | patient_target/ | 在院・新入院目標 | .csv | utf-8-sig | — |
| 3 | op_data/ | 手術データ | .csv/.xlsx | cp932 | ✅自動マージ |
| 4 | op_target/ | 手術目標 | .csv | utf-8-sig | — |
| 5 | profit_data/ | 粗利データ | .xlsx | — | — |
| 6 | profit_target/ | 粗利目標 | .xlsx | — | — |

### 4.2 必須カラム定義

#### patient_data（入院データ）

| カラム名 | 型 | 説明 |
|---|---|---|
| 日付 | date | 集計日 |
| 病棟コード | str | 病棟識別子（02A, 05B 等） |
| 診療科名 | str | 診療科名（合算前） |
| 在院患者数 | int | 当日在院数 |
| 入院患者数 | int | 予定入院数 |
| 緊急入院患者数 | int | 緊急入院数 |
| 転入患者数 | int | 他病棟からの転入 |
| 退院患者数 | int | 退院数 |
| 転出患者数 | int | 他病棟への転出 |
| 死亡患者数 | int | 死亡退院数 |

#### patient_target（目標データ）

| カラム名 | 型 | 説明 |
|---|---|---|
| 部門コード | str | 病棟コードまたは診療科コード |
| 部門名 | str | 表示名 |
| 部門種別 | str | "病棟"/"診療科" |
| 指標タイプ | str | "在院"/"新入院" |
| 期間区分 | str | "平日"/"休日"/"全日"/"週" |
| 単位 | str | "人"/"人/週" |
| 目標値 | float | 目標数値 |
| 病床数 | int | 許可病床数 |

#### op_data（手術データ）

| カラム名 | 型 | 説明 |
|---|---|---|
| 手術実施日 | date | 手術日 |
| 実施診療科 | str | 執刀科 |
| 実施手術室 | str | 手術室名（正規化前） |
| 麻酔科関与 | str | 麻酔科の関与有無 |
| 入外区分 | str | 入院/外来 |
| 申込区分 | str | 予定/緊急 |
| 実施術者 | str | 術者名 |
| 麻酔種別 | str | 全身麻酔判定に使用 |
| 入室時刻 | datetime | 手術室入室時刻 |
| 退室時刻 | datetime | 手術室退室時刻 |
| 予定手術時間 | float | 予定時間（分） |
| 予定手術時間(OR) | float | 手術室ベースの予定時間 |

#### op_target / profit_data / profit_target

| ファイル | カラム | 型 | 説明 |
|---|---|---|---|
| op_target | 実施診療科, 週目標 | str, float | 科別全麻週目標 |
| profit_data | 診療科名, 月, 粗利（千円） | str, date, float | 月次粗利実績 |
| profit_target | 診療科名, 月次目標（千円） | str, float | 月次粗利目標 |

### 4.3 前処理ルール

| 処理 | 内容 |
|---|---|
| 診療科合算 | `DEPT_MERGE = {"感染症":"総合内科","内科":"総合内科"}` |
| 非表示科 | `DEPT_HIDDEN = {"健診センター","麻酔科","放射線診断科",None,""}` |
| 病棟変換 | `WARD_NAMES` 辞書（02A→2階A病棟 等、全17棟） |
| 除外病棟 | `WARD_HIDDEN = {"03B"}` |
| 手術室正規化 | 全角→半角変換（`ZEN2HAN`）、OP-1〜OP-10,OP-12の11室が稼働対象 |
| 全身麻酔判定 | 麻酔種別に「全身麻酔(20分以上：吸入もしくは静脈麻酔薬)」を含む |
| 派生: 新入院患者数 | 入院患者数 + 緊急入院患者数 |
| 派生: 退院合計 | 退院患者数 + 死亡患者数 |
| 派生: 出入り負荷 | 新入院 + 転入 + 退院合計 + 転出 |
| マージ戦略 | `newer_wins`（同一日付は更新日時が新しいファイルで上書き） |

---

## 第5章 期間定義

### 5.1 営業平日

```python
def is_operational_day(dt) -> bool:
    # 除外1: 土曜・日曜（weekday >= 5）
    # 除外2: 国民の祝日（jpholiday で動的判定、振替休日も対応）
    # 除外3: 年末年始（12/29〜12/31、1/1〜1/3）
```

### 5.2 評価期間一覧

| 期間名 | 定義 | 主な用途 |
|---|---|---|
| 直近7日 | base_date−6 〜 base_date（暦日7日間） | KPIカード、ランキング |
| 直近28日 | base_date−27 〜 base_date | 期間タブ |
| 今年度 | 4/1〜base_date（月<4なら前年4/1起算） | 年度集計 |
| 直近365日 | base_date−364 〜 base_date | 長期平均 |
| 直近7平日 | base_dateから遡り営業平日7日分 | 全身麻酔手術の日平均 |
| 週（月曜始まり） | 当該日の月曜〜日曜 | 週単位集計 |

### 5.3 年度計算

```python
fy_start_year = date.year if date.month >= 4 else date.year - 1
fy_start = pd.Timestamp(f"{fy_start_year}-04-01")
```

---

## 第6章 KPI算出ロジック

### 6.1 在院患者数

| 項目 | 計算式 | 単位 |
|---|---|---|
| 当日在院 | `SUM(在院患者数) WHERE 日付=base_date` | 人 |
| 目標（平日/休日/全日） | 600 / 550 / 583 | 人 |
| 7日移動平均 | `rolling(7, min_periods=1).mean()` | 人 |
| 28日移動平均 | `rolling(28, min_periods=1).mean()` | 人 |
| 達成率 | `actual/target×100` (round 1) | % |
| 年度平均 | FY開始〜base_dateの日次在院平均 | 人 |
| 前年同期差 | 当年度値−前年度同日値 | 人 |

### 6.2 新入院患者数

| 項目 | 計算式 | 単位 |
|---|---|---|
| 日次新入院 | `SUM(入院+緊急入院) WHERE 日付=base_date` | 人 |
| 直近7日累計 | `SUM(新入院) WHERE base_date−6〜base_date` | 人 |
| 週目標 | 385（目標ファイルから動的読込） | 人/週 |
| 直近7日達成率 | `7日累計/385×100` | % |
| 365日週平均 | `過去364日合計/52` | 人/週 |
| 年度平均 | `FY合計/経過週数` | 人/週 |
| 年度達成率 | `年度平均/385×100` | % |

### 6.3 全身麻酔手術

#### 6.3.1 二重集計基準（重要）

手術KPIは、**病院全体**と**診療科別**で集計基準が異なる。

| 集計対象 | 日数基準 | 目標 | 移動平均の計算 | 使用箇所 |
|---|---|---|---|---|
| **病院全体** | 営業平日（土日祝日年末年始除外） | 21件/営業平日 | 営業平日7日のrolling | Portal KPIカード、Detailサマリーバー、病院全体推移グラフ |
| **診療科別** | 全日（暦日7日） | 週目標（op_targetの週目標） | 暦日7日のrolling | 診療科別ランキング、診療科別推移グラフ、ドリルダウン |

**背景**: 病院重点目標「全麻21件/日」は営業平日基準。一方、診療科の目標は「週○件」という全日（暦日7日）基準で設定されているため。

#### 6.3.2 病院全体KPI（営業平日基準）

| 項目 | 計算式 | 単位 |
|---|---|---|
| 日次件数 | `COUNT WHERE 全麻=True AND 日付=base_date` | 件 |
| 直近7平日平均 | `SUM(直近営業平日7日の全麻)/7` | 件/営業平日 |
| 目標 | 21 | 件/営業平日 |
| 達成率 | `7平日平均/21×100` | % |
| 週間合計 | `月曜〜base_dateの全麻SUM`（全日） | 件 |
| 年度平均（平日） | `FY全麻合計/FY営業平日数` | 件/営業平日 |
| 月次予測 | `当月営業平日数×直近7平日平均` | 件 |
| 年度予測 | `累積+残営業平日×直近平均` | 件 |
| 時間内稼働率 | `SUM(稼働分)/(510×11)×100` | % |
| 時間外比率 | `17:15以降稼働分/全稼働分×100` | % |

手術室パラメータ: 8:45〜17:15（510分/室）、11室（OP-1〜OP-10,OP-12）

#### 6.3.3 診療科別KPI（全日基準）

| 項目 | 計算式 | 単位 |
|---|---|---|
| 直近7日合計 | `SUM(全麻) WHERE base_date−6〜base_date`（暦日） | 件/週 |
| 週目標 | `op_targetの週目標`（科別） | 件/週 |
| 達成率 | `7日合計/週目標×100` | % |
| 直近28日平均 | `28暦日のSUM/4`（過去4週平均） | 件/週 |
| 年度平均 | `FY全麻合計/FY経過週数` | 件/週 |
| 年度達成率 | `年度平均/週目標×100` | % |

### 6.4 退院・病棟負荷

| 項目 | 計算式 |
|---|---|
| 退院関連件数 | 退院患者数 + 死亡患者数 |
| 出入り負荷 | 新入院 + 転入 + 退院合計 + 転出 |
| フローバランス | (新入院+転入) − (退院合計+転出) |

### 6.5 粗利

| 項目 | 計算式 |
|---|---|
| 月次達成率 | `粗利/月次目標×100` |
| 前月比 | `当月粗利−前月粗利` |
| 年度累計 | `FY開始月〜当月のSUM` |
| 年度達成率 | `累計/(月次目標×経過月数)×100` |

### 6.6 達成率共通ルール

```python
def achievement_rate(actual, target) -> float:
    if target is None or target == 0 or pd.isna(target):
        return None
    return round(actual / target * 100, 1)
```

---

## 第7章 ランキング・スコアリング

### 7.1 達成率ランキング

ソート: 達成率降順（NaN末尾）。代替: 実績降順。

### 7.2 ステータス区分

| 区分 | 条件 | 色 | 形状 | 文言 |
|---|---|---|---|---|
| 未達 | <90% | `#c4314b` | ▼ | 未達 |
| 接近 | 90-99% | `#b45309` | ― | 接近 |
| 達成 | ≥100% | `#0e7a54` | ▲ | 達成 |

### 7.3 医師ウォッチスコア

```
score = 新入院未達分 × 0.5 + 7日MA乖離 × 0.3 + 手術未達分 × 0.2
```

### 7.4 看護師ウォッチスコア

```
score = 利用率超過(95%超) × 1.5 + 入退院負荷スコア
```

### 7.5 差分ランキング

`gap = actual − target`、負の大きい順にソート。


---

# 第III部 画面仕様（v2ワイヤーフレーム準拠）

## 第8章 Layer-1 Portal（v2_portal_pc/sp準拠）

### 8.1 ヘッダー（CSSクラス: `.hdr`）

| 要素 | PC版 | SP版 |
|---|---|---|
| 配置 | `position:sticky; top:0` | 同左 |
| 背景 | `--brand-dark` (#0a3671) | 同左 |
| 左側 | badge「KPI」(`--brand`) + タイトル「🏥 診療ダッシュボード」(15px/900) | 同左 |
| 右側 | 📅基準日 + 🕐更新時刻(12px) | 同左(11px) |

### 8.2 ヘッドラインアラート（`.alert`）

| 属性 | 値 | データソース |
|---|---|---|
| 背景 | `--st-danger-bg` (#fdf0f2) | — |
| 左ボーダー | `4px solid --st-danger` (#c4314b) | — |
| h2テキスト | 第3章3.4のロジックで自動生成 | `headline.text` |
| pテキスト | 3KPIの「実績/目標(差分)」を1行 | `headline.detail` |
| 角丸 | `--r-lg` (18px) | — |

### 8.3 KPIカード×3（`.kpi > .kc`）

各カードは `<a>` リンクで detail.html へ遷移。

| 要素 | CSSクラス | データソース | 表示ルール |
|---|---|---|---|
| アイコン+ラベル | `.lb` | 固定文言 | 🛏️在院患者数 / 🚪新入院患者数 / 💉全身麻酔手術 |
| 期間説明 | `.sb` | 固定文言 | 「{base_date}時点」/「直近7日累計」/「直近7平日平均」 |
| 主値 | `.vl` | `kpi.{id}.actual` / `actual_7d` / `daily_avg` | 30px(SP)/42px(PC)、font-weight:900 |
| 差分 | `.gp` | `kpi.{id}.gap` | 「目標まで −{gap}{単位}」、色: `--st-danger-text` |
| ステータスバッジ | `.st` | `kpi.{id}.rate` | rate<90: `▼未達`/90-99: `―接近`/≥100: `▲達成` |
| border-top | — | ステータスに連動 | 未達: `--st-danger` / 接近: `--st-warn` / 達成: `--st-ok` |
| リンク先 | `href` | — | `detail.html#inpatient` / `#admission` / `#operation` |
| ヒントテキスト | `.ht` | 固定 | 「詳細を見る ›」 |

**PC版**: `grid 3列`、hover時 `shadow-lg + translateY(-2px)`
**SP版**: `1列`、タップ導線

### 8.4 要注視カード×3（`.sec .at`）

| 属性 | データソース | 表示 |
|---|---|---|
| 対象名 | `attention[i].name` | `.nm` (13px/900) |
| KPI種別 | `attention[i].kpi` + アイコン | `.mt` (🚪新入院 等) |
| 差分値 | `attention[i].gap` | `.dl` (22px/900, `--st-danger-text`) |
| 実績/目標 | `attention[i].actual/target` | `.dt` (11px) |
| border-left | — | `4px solid --st-danger` |

選出ロジック: 医師ウォッチスコア上位2件 + 看護師ウォッチスコア上位1件 = **計3件**

**PC版**: 2カラム（左: 要注視3件、右: 改善トピック）
**SP版**: 1カラム縦積み

### 8.5 改善トピック×1（`.at.imp`）

| 属性 | データソース |
|---|---|
| 対象名 | `improvement.name` |
| 差分値 | `improvement.delta` |
| 比較軸 | `improvement.compare` |
| border-left | `4px solid --st-info` (#2563eb) |

選出: 前週同曜日比で最大改善1件

### 8.6 SP専用固定ナビ（`.nav`）

| ボタン | アイコン | リンク先 |
|---|---|---|
| 新入院 | 🚪 | `detail_sp.html#admission` |
| 在院 | 🛏️ | `detail_sp.html#inpatient` |
| 手術 | 💉 | `detail_sp.html#operation` |

`position:fixed; bottom:0`、backdrop-filter付き。PC版では非表示。

---

## 第9章 Layer-2 Detail（v2_detail_pc/sp準拠）

### 9.1 ヘッダー（`.hdr`）

| 要素 | PC配置 | SP配置 | 動作 |
|---|---|---|---|
| ‹ポータル | `.back` 左端 | 同左 | `portal.html` へ戻る |
| badge | `DETAIL` | 同左 | — |
| タイトル | 📊 統合詳細ダッシュボード | 短縮: 統合詳細 | — |
| ロールセレクタ | `.role` 右側 | `.row2` 2行目 | 下記参照 |
| 印刷 | `.act` 右端 | 非表示 | `window.print()` |
| 基準日 | `.meta` 右端 | `.meta` 2行目 | — |

**ロールセレクタ動作**:
- `🩺 医師向け` → `switchTab('admission')` + `setAxis('dept')` + `setPeriod('7d')`
- `🏥 看護師向け` → `switchTab('inpatient')` + `setAxis('ward')` + `setPeriod('7d')`
- ページ遷移なし、URL hash+query書換

### 9.2 KPIタブバー（`.tabs`）

| タブ | data-tab | 表示テキスト | 概況値 | バッジ |
|---|---|---|---|---|
| 在院患者 | `inpatient` | 🛏️ 在院患者 | `{kpi.inpatient.actual}人/目標{target}` | ステータス |
| 新入院 | `admission` | 🚪 新入院 | `{kpi.admission.actual_7d}人/目標{target}` | ステータス |
| 手術 | `operation` | 💉 全身麻酔手術 | `{kpi.operation.daily_avg}件/日/目標{target}` | ステータス |

**PC**: `grid 3列固定`、active → `background:--brand; color:#fff`
**SP**: `flex横スクロール`、`min-width:125px/タブ`、`scrollbar-width:none`

### 9.3 サマリーバー（`.sum`）

#### 在院患者タブ

| PC列位置 | ラベル(`.k`) | 値(`.v`) | データソース | 値が未達時 |
|---|---|---|---|---|
| 1 (230px) | 🛏️ 在院患者数 | — (h2) | — | — |
| 2 | 目標日平均 | 567人 | 固定 | — |
| 3 | 今年度平均 | {fy_avg}人 | `kpi.inpatient.fy_avg` | — |
| 4 | 直近7日 | {avg_7d}人 | `kpi.inpatient.avg_7d` | `.v.dr` (--st-danger) |
| 5 | 前年平均 | {prev_avg}人 | `kpi.inpatient.prev_avg` | — |

#### 新入院タブ

| PC列位置 | ラベル | 値 | データソース | 未達時色 |
|---|---|---|---|---|
| 1 | 🚪 新入院患者数 | — | — | — |
| 2 | 週目標 | 380人 | 固定 | — |
| 3 | 年度平均 | {fy_avg}人 | `kpi.admission.fy_avg` | — |
| 4 | 直近7日 | {actual_7d}人({rate_7d}%) | `kpi.admission.actual_7d` | `.v.dr` |
| 5 | 前年平均 | {prev_avg}人 | `kpi.admission.prev_avg` | — |

#### 手術タブ

| PC列位置 | ラベル | 値 | データソース | 未達時色 |
|---|---|---|---|---|
| 1 | 💉 全身麻酔手術 | — | — | — |
| 2 | 目標 | 21件/日 | 固定 | — |
| 3 | 平日平均 | {daily_avg}件 | `kpi.operation.daily_avg` | `.v.dr` |
| 4 | 時間内稼働率 | {in_hours_rate}% | `kpi.operation.in_hours_rate` | — |
| 5 | 時間外比率 | {overtime_rate}% | `kpi.operation.overtime_rate` | — |

**PC**: `grid: 230px repeat(4,1fr)`、gap:12px
**SP**: `grid: 1fr 1fr` × 2行 + h2を独立行

### 9.4 ツールバー（`.tb`）

#### 軸切替グループ（`.grp`）

| KPIタブ | 軸1 (医師デフォルト) | 軸2 (看護師デフォルト) | 軸3 |
|---|---|---|---|
| 在院患者 | `🩺 診療科別` | `🏥 病棟別` | — |
| 新入院 | `🩺 診療科別` | `🏥 病棟別` | — |
| 手術 | `🩺 診療科別` | — | — |

#### 期間切替グループ

| KPIタブ | 期間1 | 期間2 | 期間3 |
|---|---|---|---|
| 在院患者 | `直近7日` | `直近28日` | `今年度` |
| 新入院 | `直近7日` | `直近28日` | `今年度` |
| 手術 | `年度比較` | `直近365日` | `今年度` |

#### オプショングループ

`🔄 前年度と比較` / `🖨️ 印刷`

**PC**: `flex 1行`、gap:6px、グループ間にセパレータ
**SP**: `grid 2段`（軸セグメント + 期間セグメント）

### 9.5 メインコンテンツグリッド（`.main`）

**PC**: `grid: 1.15fr 0.85fr`（左グラフ + 右ランキング）、gap:16px
**SP**: `1列`（グラフ → ランキング の縦積み）

### 9.6 グラフ仕様（左ペイン `.pnl > .chart`）

#### 共通仕様

| 項目 | 値 |
|---|---|
| ライブラリ | Plotly.js（CDN） |
| フォント | Noto Sans JP, IBM Plex Mono |
| テキスト色 | `#5A6A82` |
| 軸グリッド | `#DCE1E9` |
| Y軸 | `rangemode:"tozero"` |
| 凡例 | 下部水平（`orientation:"h", y:-0.18`） |
| ホバー | `hovermode:"x unified"`, 背景 `#1D2B3A`, テキスト `#E8EEF5` |
| PC高さ | 360px |
| SP高さ | 200px |

#### 在院患者タブ × 診療科別

| 系列名 | Plotly type | データソース | 色 | 線種 |
|---|---|---|---|---|
| 7日移動平均 | `scatter (mode:"lines")` | `trend.inpatient.ma7` | `#0D9488` | 実線 |
| 28日移動平均 | `scatter` | `trend.inpatient.ma28` | `#0D9488` | `dash:"dash"` |
| 目標ライン | `scatter (hline)` | 583 (固定) | `#C0293B` | `dash:"dash"` |
| 前年度実績 | `scatter` | `trend.inpatient.yoy` | `#94A3B8` | `dash:"dot"` |

X軸: `trend.inpatient.dates`、type: date

#### 在院患者タブ × 病棟別 → ヒートマップに切替

推移グラフに代えて **病棟別利用率ヒートマップ** を表示。

| 軸 | 内容 | データ |
|---|---|---|
| X軸 | 日付（直近8週） | `charts.occupancy_heatmap.dates` |
| Y軸 | 病棟名 | `charts.occupancy_heatmap.wards` |
| 値 | 利用率%（在院/病床数×100） | `charts.occupancy_heatmap.values` |
| 色スケール | 85%以下=赤系、85-95%=オレンジ系、95%超=緑系 | — |
| PC高さ | 280px | — |
| SP高さ | 180px | — |

#### 新入院タブ × 診療科別

| 系列名 | type | データ | 色 | 線種 |
|---|---|---|---|---|
| 7日移動平均 | `scatter` | `trend.admission.ma7` | `#0D9488` | 実線 |
| 28日移動平均 | `scatter` | `trend.admission.ma28` | `#0D9488` | `dash` |
| 目標(日割り) | `scatter` | 380/7≈54 | `#C0293B` | `dash` |
| 前年度実績 | `scatter` | `trend.admission.yoy` | `#94A3B8` | `dot` |

表示範囲切替: 直近24週(デフォルト) / 今年度 / 直近365日

#### 新入院タブ × 病棟別

全体推移と同じ線グラフ。選択病棟のデータのみ抽出。

#### 手術タブ × 診療科別

† 手術タブは「病院全体表示」と「診療科別表示」で集計基準が異なる（第6章6.3.1参照）。

**病院全体表示時**（デフォルト）:

| 系列名 | type | データ | 色 | 線種 | 備考 |
|---|---|---|---|---|---|
| 営業平日移動平均 | `scatter` | `trend.operation.ma7` | `#0D9488` | 実線 | 営業平日7日のrolling |
| 目標ライン | `scatter` | 21 | `#C0293B` | `dash` | 営業平日基準 |
| 前年度実績 | `scatter` | `trend.operation.yoy` | `#94A3B8` | `dot` | 営業平日ベース |

**診療科選択時**（ランキングで科名を選択した場合）:

| 系列名 | type | データ | 色 | 線種 | 備考 |
|---|---|---|---|---|---|
| 暦日7日移動平均 | `scatter` | `trend.operation.dept[name].ma7` | `#0D9488` | 実線 | **暦日**7日のrolling |
| 週目標ライン | `scatter` | `op_target[科名].週目標/7` | `#C0293B` | `dash` | 週目標の日割り |
| 前年度実績 | `scatter` | `trend.operation.dept[name].yoy` | `#94A3B8` | `dot` | 暦日ベース |

#### 手術タブ × 年度比較モード

PC: 1カラムで今年度(`compare.period2`)をlineで、前年度(`compare.period1`)をscatterで併記。
KPIグリッドは今年度の値。このモードは営業平日基準（病院全体KPI）。

### 9.7 ランキング仕様（右ペイン `.pnl > .rk`）

#### 共通構造（各行 `.ri`）

| PC列 | CSSクラス | 内容 | サイズ |
|---|---|---|---|
| 1 (28px) | `.num` | 順位（丸数字） | 28×28px丸 |
| 2 (1fr) | `.nm` + `.mt2` | 名前 + 実績/目標 | 13px/900 + 11px |
| 3 (130px) | `.bar > .fill` | 達成率バー | 高さ8px |
| 4 (60px) | `.rt` | 達成率% + ▼▲― | 13px/900 |
| 5 (70px) | `.stl` | ステータスバッジ | 10px/800 |
| 6 (18px) | `.arrow` | `›` 遷移矢印 | 16px |

**SP列**: `26px 1fr auto`（バー非表示、ステータスをauto幅に）

**順位丸の色分け**:
- `.n-ok`: `background:#d1fae5; color:#065f42`
- `.n-wr`: `background:#fef3c7; color:#92400e`
- `.n-dr`: `background:#fce4ec; color:#8c1d35`

#### 凡例（`.legend`）
```
▲達成(≥100%) #0e7a54 / ―接近(80-99%) #b45309 / ▼未達(<80%) #c4314b
```

#### タブ×軸別の対象

| タブ | 診療科軸 対象 | 病棟軸 対象 |
|---|---|---|
| 在院患者 | 23科 (NADM_DISPLAY_DEPTS) | 17棟 (WARD_HIDDEN除く) |
| 新入院 | 23科 | 17棟 |
| 手術 | 12科 (SURGERY_DISPLAY_DEPTS) | 12科 (SURGERY_DISPLAY_DEPTS) |

データソース: `perf.{kpi}.{axis}.{period}`（例: `perf.admission.dept.7`）

期間切替でデータが `perf.*.*.7` → `perf.*.*.28` → `perf.*.*.fy` に連動。

### 9.8 ドリルダウン（`.drill`）

#### トリガー: ランキング行（`.ri`）クリック

#### PC版（`.dr-head` + `.dr-body`）

| 要素 | CSSクラス | レイアウト | 内容 |
|---|---|---|---|
| ヘッダー | `.dr-head` | 全幅 | 📋 {科名/病棟名} の詳細 + ▼ |
| KPIグリッド | `.dgrid` | `grid 4列` | 4ミニカード(下表) |
| 下段 | `.d2` | `grid: 1fr 280px` | 左:推移グラフ(180px) + 右:注視理由+アクション |

**ミニカード4枚（`.mc`）**:

| # | ラベル(`.l`) | 値(`.v`) | 補足(`.s`) | データソース |
|---|---|---|---|---|
| 1 | 🚪 新入院（直近7日） | {actual_7d}人 | 目標{target}({rate}%) | `drill[name].admission` |
| 2 | 🛏️ 在院患者数 | {actual}人 | 目標{target}({rate}%) | `drill[name].inpatient` |
| 3 | 💉 全身麻酔（直近7日） | {actual}件 | 目標{target}({rate}%) | `drill[name].operation` |
| 4 | 💰 粗利（月次） | ¥{monthly/1000}M | 前月比{mom} | `drill[name].profit` |

未達時: `.v` に `.dr` クラス付与 → color: `--st-danger`

推移グラフ: `drill[name].trend.dates` / `drill[name].trend.values`

#### SP版（`<details>` アコーディオン）

| 要素 | 内容 |
|---|---|
| `<summary>` | 📋 {科名} の詳細 |
| `.mgrid` | `grid 3列`: 新入院/在院/手術のミニカード |
| `.chart` | 推移グラフ(130px) |

### 9.9 手術タブ専用: 期間比較サマリー（`.compare`）

PC版の手術タブ下段、`grid 3列`。

| カード(`.c`) | ラベル(`.t`) | 値(`.v`) | デルタ(`.d`) | データ |
|---|---|---|---|---|
| 1 | 平日平均件数の変化 | {delta.daily_avg}件 | ▼/▲ {%} | `compare.delta.daily_avg` |
| 2 | 月次実績の変化 | {delta.month}件 | ▼/▲ {%} | `compare.delta.month` |
| 3 | 年度予測の変化 | {delta.fy}件 | ▼/▲ {%} | `compare.delta.fy` |

負の値: `.neg` (color: `--st-danger`)、正の値: `.pos` (color: `--st-ok`)

---

# 第IV部 レスポンシブ・ロール・URL設計

## 第10章 レスポンシブ差分総覧

| 要素 | PC (≥768px) | SP (<768px) |
|---|---|---|
| Portal KPIカード | `grid 3列` | `1列` |
| Portal 注視+改善 | `2カラム` | `1列縦積み` |
| Portal 下部ナビ | なし | `固定ナビ (.nav)` |
| Detail KPIタブ | `grid 3列固定` | `flex横スクロール` |
| Detail サマリー | `grid: 230px+4列` | `grid 2列×2行` |
| Detail ツールバー | `flex 1行` | `grid 2段` |
| Detail メイン | `grid: 1.15fr+0.85fr` | `1列縦積み` |
| Detail ランキング列 | `28px 1fr 130px 60px 70px 18px` | `26px 1fr auto` |
| Detail ドリルダウン | `下段固定パネル` | `<details>アコーディオン` |
| Detail ロールセレクタ | `ヘッダー右 (.role)` | `ヘッダー2行目 (.row2)` |

### グラフサイズ

| 種類 | PC | SP |
|---|---|---|
| 推移グラフ | 360px | 200px |
| ドリルダウングラフ | 180px | 130px |
| ヒートマップ | 280px | 180px |

---

## 第11章 ロール別初期表示

### 11.1 ブックマークURL

| ロール | URL | 初期タブ | 初期軸 | 初期期間 |
|---|---|---|---|---|
| 医師 | `detail.html#admission?axis=dept` | 新入院 | 🩺診療科別 | 直近7日 |
| 看護師 | `detail.html#inpatient?axis=ward` | 在院患者 | 🏥病棟別 | 直近7日 |
| 経営層 | `portal.html` | — | — | — |
| 手術室 | `detail.html#operation?axis=dept` | 手術 | 🩺診療科別 | 年度比較 |

### 11.2 プリセットJS動作

```javascript
function presetDoctor(e) {
  switchTab('admission');
  setAxis('dept');
  setPeriod('7d');
  updateURL();
}
function presetNurse(e) {
  switchTab('inpatient');
  setAxis('ward');
  setPeriod('7d');
  updateURL();
}
```

### 11.3 医師が開いた瞬間（v2_detail_pc参照）

- タブ: 🚪新入院(active、`--brand`背景)
- 軸: 🩺診療科別(active、`--brand-light`背景)
- 期間: 直近7日(active)
- 左ペイン: 新入院推移グラフ(直近24週+目標ライン)
- 右ペイン: 23科の新入院達成率ランキング
- 想定アクション: 自科の未達確認 → 紹介受入・入院判断の改善検討

### 11.4 看護師が開いた瞬間（v2_detail_pc参照）

- タブ: 🛏️在院患者(active)
- 軸: 🏥病棟別(active)
- 期間: 直近7日(active)
- 左ペイン: 病棟別稼働ヒートマップ(直近8週)
- 右ペイン: 17棟の在院達成率ランキング（目標差分降順）
- 想定アクション: 自病棟の空床確認 → 入退院調整優先度判断

---

# 第V部 付録

## 付録A 目標値一覧

| KPI | 目標値 | 備考 |
|---|---|---|
| 在院患者数（平日） | 600人 | |
| 在院患者数（休日） | 550人 | |
| 在院患者数（全日） | 583人 | 年間加重平均（平日約245日×600＋休日約120日×550）/365 |
| 新入院患者数 | 380人/週 | 目標ファイルから動的読込 |
| 全身麻酔手術 | 21件/日 | 営業平日ベース |

## 付録B 対象一覧

### 新入院ダッシュボード表示対象（23科）

リウマチ膠原病内科、一般消化器外科、眼科、救急科、形成外科、血液内科、呼吸器外科、呼吸器内科、産婦人科、歯科口腔外科、耳鼻咽喉科、循環器内科、小児科、消化器内科、心臓血管外科、腎内科、整形外科、総合内科、乳腺外科、脳神経外科、脳神経内科、泌尿器科、皮膚科

### 手術ダッシュボード表示対象（12科）

皮膚科、整形外科、産婦人科、歯科口腔外科、耳鼻咽喉科、泌尿器科、一般消化器外科、呼吸器外科、心臓血管外科、乳腺外科、形成外科、脳神経外科

### 病棟（17棟、除外1棟）

2階A、2階B、3階A、4階A、ICU(04B)、4階C、HCU(04D)、5階A、5階B、6階A、6階B、7階A、7階B、8階A、8階B、9階A、9階B
※除外: 03B（目標未設定）

### 手術室（11室）

OP-1〜OP-10、OP-12 ※除外: OP-11

## 付録C 現行→提案B画面マッピング

| 現行画面 | 提案Bでの配置 |
|---|---|
| portal.html | portal.html（信号機版に簡略化） |
| doctor.html | detail.html 診療科別軸 |
| nurse.html | detail.html 病棟別軸 |
| admission/index.html | detail.html 新入院タブ |
| inpatient/index.html | detail.html 在院患者タブ |
| operation/index.html | detail.html 手術タブ |
| reports/dept_*.html | detail.html ドリルダウン展開 |

## 付録D データJSON全体スキーマ

```json
{
  "meta": {"base_date":"2026-04-01","generated":"2026-04-02T15:23:00"},
  "headline": {"level":"danger|warn|ok","text":"...","detail":"..."},
  "kpi": {
    "inpatient": {"actual":545,"target":567,"rate":96.1,"avg_7d":545.0,"avg_28d":544.2,"fy_avg":545.0,"prev_avg":550.0,"gap":-22.0},
    "admission": {"actual_7d":329,"target_weekly":385,"rate_7d":85.5,"fy_avg":378.0,"fy_rate":98.2,"prev_avg":355.8,"daily_actual":54,"daily_target":55},
    "operation": {"daily_avg":18.0,"target":21,"rate":85.7,"week_total":54,"fy_avg":18.0,"month_forecast":378,"fy_forecast":4320,"in_hours_rate":56.6,"overtime_rate":8.6}
  },
  "attention": [{"name":"泌尿器科","kpi":"admission","icon":"🚪","gap":-14,"actual":17,"target":30.5,"score":8.2}],
  "improvement": {"name":"乳腺外科","kpi":"admission","delta":5,"compare":"前週同曜日比"},
  "perf": {
    "admission": {"dept":{"7":[{"name":"皮膚科","actual":7,"target":3.5,"rate":200.0,"status":"ok"}],"28":[],"fy":[]},"ward":{"7":[],"28":[],"fy":[]}},
    "inpatient": {"dept":{},"ward":{}},
    "operation": {"dept":{},"surgeon":{}}
  },
  "trend": {
    "admission": {"dates":[],"values":[],"ma7":[],"ma28":[],"target_line":[],"yoy":[]},
    "inpatient": {"dates":[],"values":[],"ma7":[],"ma28":[],"target_line":[],"yoy":[]},
    "operation": {"dates":[],"values":[],"ma7":[],"target_line":[],"yoy":[]}
  },
  "drill": {
    "泌尿器科": {
      "admission":{"actual_7d":17,"target":30.5,"rate":55.7},
      "inpatient":{"actual":28,"target":32.0,"rate":87.5},
      "operation":{"actual":2,"target":10.2,"rate":19.6},
      "profit":{"monthly":12400,"target":15000,"mom":500},
      "trend":{"dates":[],"values":[]}
    }
  },
  "charts": {
    "occupancy_heatmap":{"wards":[],"dates":[],"values":[[]]},
    "load_stack":{"wards":[],"new_adm":[],"transfer_in":[],"discharge":[],"transfer_out":[]},
    "flow_balance":{"dates":[],"values":[]},
    "surgery_bubble":{"depts":[],"x":[],"y":[],"size":[]},
    "waterfall":{"depts":[],"values":[]}
  },
  "compare": {
    "period2":{"label":"今年度","daily_avg":18.0,"rate":85.7,"month":378,"fy":4320,"in_hours":56.6,"overtime":8.6},
    "period1":{"label":"昨年度","daily_avg":19.6,"rate":93.4,"month":373,"fy":4818,"in_hours":57.7,"overtime":9.1},
    "delta":{"daily_avg":-1.6,"month":5,"fy":-498,"in_hours":-1.1,"overtime":-0.5}
  }
}
```

## 付録E 参照ワイヤーフレーム

| ファイル | 内容 |
|---|---|
| design_system.html | デザインシステム総合資料（色・アイコン・文言・トーン） |
| v2_portal_pc.html | PC版ポータル ワイヤーフレーム |
| v2_portal_sp.html | SP版ポータル ワイヤーフレーム |
| v2_detail_pc.html | PC版統合詳細 ワイヤーフレーム |
| v2_detail_sp.html | SP版統合詳細 ワイヤーフレーム |

---

以上
