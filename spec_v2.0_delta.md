# 診療ダッシュボード システム仕様書 v2.0 差分更新

> **本文書は v1.8 からの差分です。v1.8 仕様書の該当箇所に追記・上書きしてください。**
> **★v2.0更新 = 既存セクションの変更、★v2.0新規 = 新規セクション追加**

---

## 改訂履歴（末尾に追加）

| Ver | 更新日 | 主な変更内容 |
|---|---|---|
| 2.0 | 2026-04-03 | 提案B「2層ハブ＆スポーク型」への全面再構築。7画面→2画面（portal.html + detail.html）に集約。デザインシステム統合（色覚多様性対応カラー・三重エンコーディング）。目標値改定（在院600/550/583、新入院380）。ステータス閾値90%化。稼働率→利用率用語統一。術者別軸廃止。手術KPIの二重集計基準（病院全体=営業平日/診療科=全日）。グラフ仕様刷新（棒グラフ廃止→線グラフ中心、ゾーン色分け、平日/休日チェックボックス）。ランキング折りたたみ。ドリルダウンにミニグラフ3本+コメント自動生成。要注視カードを絶対差ベースに改定。ヘッドライン複数KPI列挙対応。Makefile v2.1対応。|

---

## 第1章 システム概要 ★v2.0更新

### 変更内容

v2.0で「提案B: 2層ハブ＆スポーク型」を採用し、画面構成を全面再構築した。

| 項目 | v1.8（旧） | v2.0（新） |
|---|---|---|
| 画面構成 | 7画面・4層 | **2画面・2層** |
| Layer-1 | portal.html（ポータルハブ） | portal.html（**信号機ポータル**に簡略化） |
| Layer-2 | doctor.html / nurse.html / admission / inpatient / reports | **detail.html 1画面に統合** |
| 役割別表示 | 医師/看護師で専用ページ | **1画面内でプリセットボタン切替** |
| 診療科詳細 | reports/dept_*.html | **detail.html内ドリルダウン展開** |
| 旧URL互換 | — | **自動リダイレクトHTML 5件** |

#### 廃止されたファイル

- `doctor.html`（→ `detail.html#admission?axis=dept` にリダイレクト）
- `nurse.html`（→ `detail.html#inpatient?axis=ward` にリダイレクト）
- `admission/index.html`（→ `detail.html#admission` にリダイレクト）
- `inpatient/index.html`（→ `detail.html#inpatient` にリダイレクト）
- `operation/index.html`（→ `detail.html#operation` にリダイレクト）
- `reports/index.html`, `reports/dept_*.html`（→ detail.htmlドリルダウンに統合）
- `doctor_summary.json`, `nurse_summary.json`（不要）
- `admission_app.py`, `inpatient_app.py`（不要）

---

## 第2章 ファイル構成 ★v2.0更新

### 2.1 出力HTMLファイル（v2.0 確定構成）

| ファイル | 役割 | v2.0変更 |
|---|---|---|
| portal.html | 信号機ポータル。ヘッドライン+KPIカード3枚+要注視3件+改善1件 | ★大幅簡略化（ナビカード6枚→KPIカードがリンクに） |
| detail.html | 統合詳細ダッシュボード。KPIタブ×軸切替×期間切替×ランキング×ドリルダウン | ★新規 |
| doctor.html | リダイレクト → detail.html#admission?axis=dept | ★リダイレクトのみ |
| nurse.html | リダイレクト → detail.html#inpatient?axis=ward | ★リダイレクトのみ |
| admission/index.html | リダイレクト → detail.html#admission | ★リダイレクトのみ |
| inpatient/index.html | リダイレクト → detail.html#inpatient | ★リダイレクトのみ |
| operation/index.html | リダイレクト → detail.html#operation | ★リダイレクトのみ |

### 2.2 ソースファイル（v2.0 確定構成）

| ファイル | 役割 | v2.0変更 |
|---|---|---|
| generate_html.py | 2ファイル生成CLI（portal.html + detail.html） | ★全面書換 |
| app/lib/config.py | 定数・目標値・閾値・デザイントークン・ヘッドライン生成 | ★大幅更新 |
| app/lib/metrics.py | KPI算出・二重集計基準・ウォッチスコア | ★大幅更新 |
| app/lib/charts.py | グラフデータ生成（線グラフ中心・ゾーン色分け） | ★全面書換 |
| app/lib/html_builder.py | portal/detailコンテキスト生成・要注視カード選出・JSON一括生成 | ★全面書換 |
| app/templates/portal.html | 信号機ポータルJinja2テンプレート | ★新規 |
| app/templates/detail.html | 統合詳細Jinja2テンプレート（Plotly.js + JS制御込み） | ★新規 |
| app/lib/data_loader.py | データ読込・マージ | 変更なし |
| app/lib/preprocess.py | 前処理・目標ルックアップ構築 | 変更なし |
| app/lib/profit.py | 粗利KPI算出 | 変更なし |
| app/lib/validate.py | データ検証 | 変更なし |
| Makefile | ビルド・デプロイ（v2.1対応） | ★更新 |

### 2.3 データフォルダ構成

変更なし（v1.8と同一）。

---

## 第8章 ビルド・デプロイ仕様（Makefile v2.1）★v2.0更新

| コマンド | 動作 | v2.0変更 |
|---|---|---|
| make build | portal.html + detail.html + 旧URLリダイレクト生成 | ★出力ファイル変更 |
| make build-fast | --no-redirect（旧URLリダイレクトスキップ） | ★変更 |
| make deploy | make build + git add portal.html detail.html + git push | ★対象ファイル変更 |
| make serve | ビルド後にローカルサーバー起動。ロール別URL案内表示 | ★案内URL変更 |
| make serve-only | サーバーのみ起動 | 変更なし |
| make clean | portal.html / detail.html / 旧ファイル全削除 | ★対象追加 |
| make clean-legacy | 旧バージョン固有ファイル削除（reports/、*_summary.json等） | ★新規 |
| make setup | データフォルダ初期化 | 変更なし |
| make check | データ検証のみ | 変更なし |
| make help | コマンド一覧+ロール別URL表示 | ★表示内容更新 |
| make lint | Python構文チェック | 変更なし |
| make install | 依存ライブラリインストール | 変更なし |

#### 廃止されたMakefileターゲット

| 旧コマンド | 理由 |
|---|---|
| make dept DEPT=○○ | detail.html内ドリルダウンに統合 |
| make streamlit-admission | admission_app.py廃止 |
| make streamlit-inpatient | inpatient_app.py廃止 |

---

## 第9章 KPI算出仕様 ★v2.0更新

### 9.1 病院全体目標値 ★v2.0改定

| KPI | v1.8目標値 | v2.0目標値 | 集計単位 |
|---|---|---|---|
| 在院患者数（平日） | 580人 | **600人** | 日次 |
| 在院患者数（休日） | 540人 | **550人** | 日次 |
| 在院患者数（全日） | 567人 | **583人** | 年間加重平均（平日約245日×600+休日約120日×550）/365 |
| 新入院患者数（直近7日） | 385人 | **380人** | 直近7日ローリング累計 |
| 新入院患者数（1日・平日） | 80人 | 80人 | 変更なし |
| 新入院患者数（1日・休日） | 40人 | 40人 | 変更なし |
| 全身麻酔手術 | 21件 | 21件 | 営業平日1日あたり（変更なし） |

### 9.2 ステータス色分け基準 ★v2.0改定

| ステータス | v1.8条件 | v2.0条件 | v1.8表示色 | v2.0表示色 |
|---|---|---|---|---|
| 達成 | ≥105% | **≥100%** | 緑(#1A9E6A) | **緑(#0e7a54)** |
| 接近 | 95-104% | **90-99%** | 橙(#C87A00) | **橙(#b45309)** |
| 未達 | <95% | **<90%** | 赤(#C0293B) | **赤(#c4314b)** |
| neutral | 目標なし | 目標なし | 灰(#94A3B8) | 灰(#9daab8) |

### 9.3 色覚多様性対応カラー ★v2.0新規

P型/D型色覚（日本人男性の約5%）で赤-緑の混同を避けるため、赤を青み寄り、緑を青緑寄りにシフト。WCAG 2.2 AA準拠。

| 状態 | 前景色 | 背景色 | テキスト色 | コントラスト比(白背景) |
|---|---|---|---|---|
| 未達 | #c4314b | #fdf0f2 | #8c1d35 | 5.8:1 |
| 接近 | #b45309 | #fef7ee | #7c3a06 | 5.2:1 |
| 達成 | #0e7a54 | #ecfdf5 | #065f42 | 5.4:1 |

### 9.4 三重エンコーディング規則 ★v2.0新規

色だけに頼らず、色＋形状＋文言を常にセットで表示する。

| 状態 | 色 | 形状 | 文言 |
|---|---|---|---|
| 未達(<90%) | #c4314b | ▼ | 「▼ 未達」 |
| 接近(90-99%) | #b45309 | ― | 「― 接近」 |
| 達成(≥100%) | #0e7a54 | ▲ | 「▲ 達成」 |

---

## 第10章 設定定数一覧（config.py）★v2.0更新

| 定数名 | 内容 | v2.0変更 |
|---|---|---|
| TARGET_INPATIENT_WEEKDAY | 在院平日目標: 600 | ★新規（旧は目標ファイル参照） |
| TARGET_INPATIENT_HOLIDAY | 在院休日目標: 550 | ★新規 |
| TARGET_INPATIENT_ALLDAY | 在院全日目標: 583 | ★新規 |
| TARGET_ADMISSION_WEEKLY | 新入院週目標: 380 | ★新規 |
| TARGET_GA_DAILY | 全麻日目標: 21 | ★新規 |
| THRESHOLD_DANGER | ステータス未達閾値: 90 | ★新規 |
| THRESHOLD_OK | ステータス達成閾値: 100 | ★新規 |
| status_label() | 達成率→ステータス文字列 | ★新規関数 |
| status_display() | 達成率→色・形状・文言の辞書 | ★新規関数 |
| build_headline() | 3KPI達成率→ヘッドラインメッセージ自動生成（複数KPI列挙対応） | ★新規関数（v1.8のhtml_builder版を移動・拡張） |
| UI_TOKENS | CSSデザイントークン辞書（ベース/ブランド/ステータス色） | ★新規 |
| KPI_ICONS | KPIアイコン辞書（🛏️在院/🚪新入院/💉手術） | ★新規 |
| AXIS_ICONS | 軸アイコン辞書（🩺診療科/🏥病棟） | ★新規 |
| HEATMAP_SCALE | 利用率ヒートマップ色スケール（85%以下=赤、85-95%=オレンジ、95%超=緑） | ★新規 |
| DEPT_HIDDEN | 変更なし | |
| DEPT_MERGE | 変更なし | |
| WARD_HIDDEN | 変更なし | |
| SURGERY_DISPLAY_DEPTS | 変更なし | |
| NADM_DISPLAY_DEPTS | 変更なし | |
| OR_ROOMS_ACTIVE | 変更なし | |
| GA_KEYWORD | 変更なし | |
| DOD_THRESHOLDS | v1.8新規（変更なし） | |

---

## 第11章 バグ修正ログ

### 11.5 v2.0 変更ファイル一覧 ★v2.0新規

| ファイル | 変更内容 |
|---|---|
| generate_html.py | 7ファイル生成→2ファイル生成に全面書換。load_all()→build_target_lookup()フロー。旧URLリダイレクト自動生成。 |
| app/lib/config.py | 目標値定数化、閾値90%化、status_label/status_display/build_headline関数追加、デザイントークン追加 |
| app/lib/metrics.py | 二重集計基準（ga_rolling_calendar_dept新設）、rolling28_surgery_dept新設、build_kpi_summary v2.1化 |
| app/lib/charts.py | 全面書換。棒グラフ廃止→線グラフ化。build_surgery_chart_hospital/dept分離。年度比較1カラム化。利用率ヒートマップ色反転。 |
| app/lib/html_builder.py | 全面書換。build_portal_context/build_detail_json新設。_build_attention_cards（絶対差ベース）新設。旧build_doctor/nurse_context廃止。病棟ドリルダウンデータ・コメント自動生成追加。平日フラグ(is_weekday)追加。 |
| app/templates/portal.html | 新規。信号機ポータルJinja2テンプレート。要注視に選出理由・期間ラベル表示。SP固定ナビ。 |
| app/templates/detail.html | 新規。統合詳細Jinja2テンプレート。Plotly.js描画。タブ/軸/期間切替。平日休日・描画線チェックボックス。ランキング折りたたみ。ドリルダウン（ミニグラフ3本+コメント）。全データMA→期間切り取りアルゴリズム。 |
| Makefile | v2.1対応。2ファイル出力。clean-legacy新設。 |

---

## 第13章 ヘッドライン自動生成 ★v2.0更新

### 13.1 build_headline() 仕様 ★v2.0変更

v2.0で未達(<90%)と接近(90-100%)を分離し、未達KPIを「と」で列挙する方式に変更。

| 条件 | ヘッドライン | アイコン |
|---|---|---|
| 3 KPI すべて達成(≥100%) | 全指標が目標を達成しています | 🟢 |
| 未達なし・接近のみ | 〔接近KPI名〕が目標をやや下回っています | 🟡 |
| 未達1件 | 〔未達KPI名〕が目標を下回っています | 🔴 |
| 未達2件以上 | 〔未達KPI名〕と〔未達KPI名〕が目標を大きく下回っています | 🔴 |

KPI名は短縮表示: 在院患者数→「在院」、新入院患者数→「新入院」、全身麻酔手術→「全麻」

例: `🔴 新入院と全麻が目標を大きく下回っています`

---

## 第16章 提案B 2層構造設計 ★v2.0新規

### 16.1 設計思想

7画面4層構造を2画面2層に集約。Progressive Disclosure（重要情報を先に、詳細は操作で出す）の原則に基づく。

- **Layer-1 portal.html**: 10秒で状況把握。気になるKPIカードをクリックしてLayer-2へ。
- **Layer-2 detail.html**: KPIタブ×軸切替×期間切替で全組合せをカバー。医師/看護師の視点差は「軸切替」で吸収。

### 16.2 Layer-1 portal.html

#### ヘッダー
- sticky、背景 `--brand-dark` (#0a3671)
- 左: badge「KPI」+ タイトル「🏥 診療ダッシュボード」
- 右: 基準日 + 更新時刻

#### ヘッドラインアラート
- 背景: `--st-danger-bg`、左ボーダー4px `--st-danger`
- テキスト: build_headline()による自動生成
- 補足行: 3KPIの「実績/目標(差分)」を1行表示

#### KPIカード×3
各カードは`<a>`リンクでdetail.htmlへ遷移。

| カード | アイコン | 主値 | 期間ラベル | リンク先 |
|---|---|---|---|---|
| 在院患者数 | 🛏️ | 当日在院数（28px太字） | 「{base_date}時点の在院数」 | detail.html#inpatient |
| 新入院患者数 | 🚪 | 直近7日累計（28px太字） | 「直近7日累計」 | detail.html#admission |
| 全身麻酔手術 | 💉 | 直近7平日平均（28px太字） | 「直近7平日平均」 | detail.html#operation |

#### 要注視カード×3
**選出ロジック**: 目標との絶対差（人・件）が大きい順。目標以上（gap≥0）は除外。
- 診療科（新入院直近7日）: 絶対差が大きい順に上位2件
- 病棟（在院当日時点）: 絶対差が大きい順に上位1件

各カードに表示: 対象名 + KPIアイコン + 期間ラベル + 差分値 + 実績/目標 + 選出理由

#### 改善トピック×1
前週同曜日比で最大改善1件。

#### SP専用固定ナビ
画面下部固定、3ボタン（🚪新入院/🛏️在院/💉手術）。

### 16.3 Layer-2 detail.html

#### KPIタブバー
3タブ（在院/新入院/手術）。各タブにKPI値を大きく表示（28px太字）+ 期間ラベル + ステータスバッジ。

#### サマリーバー
タブ切替に連動。5列grid（KPI名 + 4指標）。

#### ツールバー

| グループ | 内容 |
|---|---|
| 軸 | 🩺診療科別 / 🏥病棟別（手術タブは診療科のみ） |
| 期間 | 直近12週 / 直近24週 / 直近365日 |
| 日種 | ☑平日 ☑休日（チェックボックス） |
| 描画 | ☑日次実績 ☑7日MA ☑28日MA（チェックボックス） |

#### ロールプリセット
ヘッダー右側に「🩺医師向け」「🏥看護師向け」ボタン。ページ遷移なし。

| プリセット | KPIタブ | 軸 | 期間 |
|---|---|---|---|
| 🩺 医師向け | 新入院 | 診療科別 | 直近12週 |
| 🏥 看護師向け | 在院患者 | 病棟別 | 直近12週 |

### 16.4 グラフ仕様

#### メイングラフ系列

| 系列 | スタイル | 色 | チェックボックス |
|---|---|---|---|
| 日次実績 | 細い点線 | グレー #94A3B8 | ☑日次実績 |
| 7日移動平均 | 太実線 | 緑 #0D9488 | ☑7日MA |
| 28日移動平均 | 実線 | オレンジ #e67e22 | ☑28日MA |
| 目標ライン | 細かい点線（太め） | 赤 #C0293B | 常時表示 |
| 達成ゾーン | 背景塗り（目標以上） | 薄緑 rgba(22,163,74,0.08) | 常時表示 |
| 注意ゾーン | 背景塗り（目標-5%〜目標） | 薄オレンジ rgba(245,158,11,0.10) | 常時表示 |

#### 集計アルゴリズム

```
正しい順序:
① 全データで平日/休日フィルタ
② フィルタ後の全データでMA7/MA28を計算
③ 期間（12週/24週/365日）で切り取り
→ 切り取り時点で既にMAが完全に計算済みなので左端が欠けない
```

#### Y軸
MAの最大/最小に±8%マージンの自動レンジ。rangemode:'tozero'は使用しない。

### 16.5 ランキング

- 初期表示10件。「もっと見る（+N件）▾」ボタンで展開/折りたたみ。
- 達成率降順。NaN末尾。
- 各行: 順位 + 名前 + 達成率バー + %値 + ステータスバッジ + 遷移矢印
- クリックでドリルダウン展開。

### 16.6 ドリルダウン（診療科）

- KPIミニカード3枚: 新入院(直近7日)/在院(当日)/手術(直近7日)
- コメント自動生成: 達成率<90%のKPIを列挙（例:「新入院が目標の56%（17/30.5）」）
- ミニグラフ3本: 新入院推移/在院推移/手術推移（週間件数）
  - 手術ミニグラフは7日ローリング合計で表示、目標は週目標そのまま
- メイン期間・描画チェックボックスと連動
- 展開時アニメーション + スムーズスクロール

### 16.7 ドリルダウン（病棟）

- KPIミニカード4枚: 新入院/在院/退院関連/利用率
- 手術グラフなし（「病棟単位の手術データなし」表示）
- 目標以上の病棟は要注視から自動除外

---

## 第17章 ロール別初期表示 ★v2.0新規

### 17.1 ブックマークURL

| ロール | URL | 初期タブ | 初期軸 |
|---|---|---|---|
| 医師 | detail.html#admission?axis=dept | 新入院 | 診療科別 |
| 看護師 | detail.html#inpatient?axis=ward | 在院患者 | 病棟別 |
| 経営層 | portal.html | — | — |
| 手術室 | detail.html#operation?axis=dept | 手術 | 診療科別 |

### 17.2 プリセットボタン動作

```javascript
function presetDoctor(e) {
  switchTab('admission'); setAxis('dept'); setPeriod('12w');
  // ページ遷移なし、URL hash+query書換
}
function presetNurse(e) {
  switchTab('inpatient'); setAxis('ward'); setPeriod('12w');
}
```

---

## 第18章 レスポンシブ設計 ★v2.0新規

| 要素 | PC (≥768px) | SP (<768px) |
|---|---|---|
| KPIタブ | grid 3列固定 | flex横スクロール |
| サマリー | grid: 200px+4列 | grid 2列×2行 |
| ツールバー | flex 1行 | flex wrap |
| メインコンテンツ | grid: 1.15fr+0.85fr | 1列（グラフ→ランキング縦積み） |
| ランキング列 | 6列 | 3列（バー・ステータス非表示） |
| ドリルダウン | 下段固定パネル | アニメーション展開 |
| ロールセレクタ | ヘッダー右 | 非表示 |
| ポータル下部ナビ | なし | 固定ナビ |
| 推移グラフ高さ | 380px | 200px |
| ミニグラフ高さ | 225px | 160px |

---

## 第19章 手術KPIの二重集計基準 ★v2.0新規

### 19.1 病院全体（営業平日基準）

| 項目 | 計算式 | 用途 |
|---|---|---|
| 直近7平日平均 | SUM(直近営業平日7日の全麻)/7 | Portal KPIカード、Detailサマリー |
| 目標 | 21件/営業平日 | 目標ライン |
| 移動平均 | 営業平日7日のrolling | 病院全体推移グラフ |

### 19.2 診療科別（全日基準）

| 項目 | 計算式 | 用途 |
|---|---|---|
| 直近7日合計 | SUM(全麻) WHERE 暦日7日 | 診療科ランキング |
| 週目標 | op_targetの週目標 | 達成率計算 |
| 移動平均 | 暦日7日のrolling | 診療科別推移グラフ |

### 19.3 ミニグラフ（手術専用）

- 7日ローリング合計（週間件数）で表示
- 目標は週目標そのまま（日割りしない）
- KPIカードの達成率とグラフが一致

---

## 第20章 デザインシステム ★v2.0新規

### 20.1 カラーパレット

#### ベースカラー

| トークン | CSS変数 | カラーコード | 用途 |
|---|---|---|---|
| Background | --bg | #f6f8fb | ページ背景 |
| Surface | --surface | #ffffff | カード背景 |
| Ink | --ink | #1a2332 | 本文テキスト |
| Sub | --sub | #5f7084 | 補足テキスト |
| Muted | --muted | #9daab8 | 注記 |
| Line | --line | #dfe5ed | 罫線 |
| Brand | --brand | #0e4da4 | ヘッダー・選択中タブ |
| Brand Light | --brand-light | #e8f0fe | 選択中ボタン背景 |
| Brand Dark | --brand-dark | #0a3671 | ヘッダー背景 |

#### グラフカラー

| 用途 | カラーコード | 線種 |
|---|---|---|
| 日次実績 | #94A3B8 | 細い点線 |
| 7日MA | #0D9488 | 太実線 |
| 28日MA | #e67e22 | 実線 |
| 目標ライン | #C0293B | 細かい点線（太め） |

### 20.2 アイコン

| 用途 | アイコン |
|---|---|
| 在院患者数 | 🛏️ |
| 新入院患者数 | 🚪 |
| 全身麻酔手術 | 💉 |
| 診療科別 | 🩺 |
| 病棟別 | 🏥 |
| 要注視 | 🔴 |
| 改善 | 📈 |

### 20.3 文言トーン5原則

1. 事実ベース（価値判断を避ける）
2. アクション示唆
3. 短く、敬語不使用（体言止め）
4. 責任追及しない（数値のみ）
5. 改善も伝える

### 20.4 フォントサイズ

| 要素 | PC | SP |
|---|---|---|
| KPIメイン数値 | 28px/900 | 22px/900 |
| セクション見出し | 18px/800 | 16px/800 |
| カード名 | 14px/700 | 13px/700 |
| 補足 | 13px/400 | 12px/400 |
| 注記 | 11px/400 | 11px/400 |

---

## 第21章 データJSON構造 ★v2.0新規

detail.htmlに埋め込むJSONの全体スキーマ:

```json
{
  "meta": {"base_date": "2026-04-01", "generated": "2026-04-02T15:23:00"},
  "headline": {"level": "danger|warn|ok", "icon": "🔴|🟡|🟢", "text": "...", "detail": "..."},
  "kpi": {
    "inpatient": {"actual", "target", "rate", "avg_7d", "avg_28d", "fy_avg", "prev_avg", "gap", "status"},
    "admission": {"actual_7d", "target_weekly", "rate_7d", "fy_avg", "fy_rate", "prev_avg", "gap", "status"},
    "operation": {"daily_avg", "target", "rate", "week_total", "fy_avg", "gap", "in_hours_rate", "status"}
  },
  "attention": [{"name", "kpi", "icon", "gap", "actual", "target", "period_label", "reason"}],
  "improvement": {"name", "kpi", "delta", "compare"},
  "perf": {
    "admission": {"dept_7": [...], "dept_28": [...], "dept_fy": [...], "ward_7": [...], ...},
    "inpatient": {...},
    "operation": {...}
  },
  "trend": {
    "admission": {"dates": [...], "values": [...], "ma7": [...], "ma28": [...], "is_weekday": [...]},
    "inpatient": {...},
    "operation": {...}
  },
  "drill": {
    "泌尿器科": {
      "admission": {"actual_7d", "target", "rate"},
      "inpatient": {"actual", "target", "rate"},
      "operation": {"actual", "target", "rate"},
      "trend": {"admission": {...}, "inpatient": {...}, "operation": {...}},
      "comment": "..."
    },
    "5階A病棟": {
      "admission": {...}, "inpatient": {...}, "operation": {"label": "退院関連"},
      "ward_extra": {"beds", "util_rate", "load"},
      "trend": {...}, "comment": "..."
    }
  },
  "charts": {"occupancy_heatmap": {...}}
}
```

---

以上
