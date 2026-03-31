# 📊 診療ダッシュボード

病院の経営KPI（在院患者数・新入院患者数・全身麻酔手術件数・粗利）を  
医師・看護師向けに可視化する静的HTMLダッシュボード。

**公開先**: https://genie-scripts.github.io/Streamlit-Dashboard/

---

## 目次

1. [システム概要](#1-システム概要)
2. [セットアップ](#2-セットアップ)
3. [データ準備](#3-データ準備)
4. [HTML生成（日次更新）](#4-html生成日次更新)
5. [ローカル確認](#5-ローカル確認)
6. [GitHub Pagesデプロイ](#6-github-pagesデプロイ)
7. [CLI オプション詳細](#7-cli-オプション詳細)
8. [ファイル構成](#8-ファイル構成)
9. [ビジネスルール](#9-ビジネスルール)
10. [トラブルシューティング](#10-トラブルシューティング)

---

## 1. システム概要

```
データファイル (xlsx/csv)
        ↓
  generate_html.py    ← Python でKPI算出・HTML生成
        ↓
  index.html          ← ダッシュボードトップ（Plotly.js でグラフ描画）
  reports/            ← 診療科別詳細ページ群
        ↓
  GitHub Pages        ← 静的ファイルとしてホスティング
```

- **生データをGitHubに上げない** — 集計済みJSONのみHTMLに埋め込み
- Plotly.js（CDN）でクライアントサイドグラフ描画
- 全てVanilla JS — フレームワーク不要

---

## 2. セットアップ

### 必要環境

- Python 3.10 以上
- macOS / Linux / Windows (WSL推奨)

### 手順

```bash
# リポジトリをクローン
git clone https://github.com/genie-scripts/Streamlit-Dashboard.git
cd Streamlit-Dashboard

# 依存ライブラリをインストール
pip install pandas openpyxl jinja2 plotly streamlit numpy
```

### コマンドの使い分け

| やりたいこと | コマンド |
|---|---|
| データフォルダの初期化（初回のみ） | `python generate_html.py --setup` |
| HTML生成（日次更新） | `python generate_html.py` |
| ローカルで動作確認（インタラクティブ） | `streamlit run streamlit_app.py` |
| ブラウザで静的HTMLを確認 | `python -m http.server 8080` |

> ⚠️ `streamlit run generate_html.py` は**誤り**です。  
> `generate_html.py` は Streamlit アプリではなく、コマンドラインスクリプトです。

---

## 3. データ準備

### フォルダ構成（初回セットアップ）

まず以下のコマンドでデータフォルダを自動作成します：

```bash
# ターミナル（python コマンド）で実行
python generate_html.py --setup

# Makefile がある場合
make setup

# Streamlit アプリ上から行う場合
# → サイドバーの「📁 データフォルダを初期化」ボタンを押す
```

> ⚠️ `streamlit run generate_html.py --setup` は**動作しません**。  
> `generate_html.py` は `python` コマンドで実行するスクリプトです。  
> Streamlit アプリは `streamlit run streamlit_app.py` で起動します。

以下のフォルダが作成されます：

```
data/
  patient_data/      ← 入院日報（xlsx/csv）を複数置いてOK → 自動マージ
  patient_target/    ← 在院・新入院目標（csv）
  op_data/           ← 手術データ（csv）を複数置いてOK → 自動マージ
  op_target/         ← 手術目標（csv）
  profit_data/       ← 粗利データ（xlsx）
  profit_target/     ← 粗利目標（xlsx）
```

### 各フォルダへのファイル配置

| フォルダ | 置くファイル | 形式 | 複数ファイル |
|---|---|---|---|
| `patient_data/` | 入院日報 | .xlsx / .csv | ✅ 自動マージ |
| `patient_target/` | 在院・新入院目標 | .csv (UTF-8 BOM) | 最新ファイルを使用 |
| `op_data/` | 手術実施データ | .csv (CP932) / .xlsx | ✅ 自動マージ |
| `op_target/` | 手術目標 | .csv (UTF-8 BOM) | 最新ファイルを使用 |
| `profit_data/` | 粗利データ（横持ち） | .xlsx | 最新ファイルを使用 |
| `profit_target/` | 粗利目標 | .xlsx | 最新ファイルを使用 |

### 複数ファイルのマージ動作

`patient_data/` と `op_data/` は**フォルダ内の全ファイルを自動的に結合**します。

```
patient_data/
  base_data.xlsx     ← 2024-01〜2026-01（過去半年分）
  add_data.xlsx      ← 2026-01〜2026-03（直近更新分）
```

**重複日付の扱い**: 同一（日付・病棟・診療科）が複数ファイルに存在する場合、  
**ファイル更新日時が新しい方**のデータを採用します（`add_data.xlsx` 優先）。

### データ更新の流れ（日次）

```bash
# 1. 新しいデータファイルをフォルダに追加
cp /path/to/新入退院クロス0401.xlsx data/patient_data/
cp /path/to/手術データ0401.csv       data/op_data/

# 2. HTML生成 & デプロイ
make deploy
```

古いファイルを残しつつ新しいファイルを追加するだけでOKです。  
フォルダ内のファイルが増えると読込に時間がかかるため、  
不要になった古いファイルは定期的に削除するか、`base_data` にまとめることを推奨します。

---

## 4. HTML生成（日次更新）

### 基本コマンド

```bash
# データを data/ に入れて実行
make

# または直接
python generate_html.py
```

### よく使うパターン

```bash
# 特定の基準日で生成
make build-date DATE=2026-03-26

# 高速生成（詳細ページスキップ）
make build-fast

# データ検証のみ（HTML出力なし）
make check

# 1科のみ詳細ページ再生成
make dept DEPT=整形外科

# 詳細ログを抑制
python generate_html.py --quiet
```

---

## 5. ローカル確認

### ブラウザで確認（推奨）

```bash
make serve
# → http://localhost:8080 でダッシュボードが開く
```

### Streamlitアプリで確認

```bash
make streamlit
# → http://localhost:8501 でインタラクティブに確認可能
```

Streamlitアプリの機能:
- サイドバーで基準日・期間・診療科を動的に切替
- 医師タブ / 看護師タブ / 粗利レポートタブ
- サイドバーの「HTML生成」ボタンで `index.html` を出力

---

## 6. GitHub Pagesデプロイ

```bash
# ビルド → コミット → プッシュを1コマンドで
make deploy

# または手動
git add index.html reports/
git commit -m "Dashboard update: $(date '+%Y/%m/%d')"
git push origin main
```

> **注意**: `data/` ディレクトリは `.gitignore` に追加して生データをプッシュしないでください。

### .gitignore の設定例

```
data/
__pycache__/
*.pyc
.DS_Store
```

---

## 7. CLI オプション詳細

```
python generate_html.py [オプション]
```

| オプション | 省略値 | 説明 |
|---|---|---|
| `--data-dir DIR` | `data` | データディレクトリのパス |
| `--output FILE` | `index.html` | トップページの出力先 |
| `--base-date YYYY-MM-DD` | 最新日付 | 基準日の指定 |
| `--sort-by achievement\|actual` | `achievement` | ランキングの並び順 |
| `--dept 診療科名` | なし | 1科のみ詳細ページ再生成 |
| `--skip-reports` | false | 診療科別詳細ページをスキップ |
| `--dry-run` | false | 検証のみ実行（HTML出力なし） |
| `--no-validate` | false | データ検証スキップ（高速化） |
| `--quiet` / `-q` | false | 進捗ログを抑制 |

---

## 8. ファイル構成

```
.
├── generate_html.py      # メイン生成スクリプト
├── streamlit_app.py      # ローカル確認用Streamlitアプリ
├── Makefile              # コマンドショートカット
├── requirements.txt      # 依存ライブラリ
├── README.md             # このファイル
│
├── app/
│   ├── lib/
│   │   ├── config.py         # 定数・マッピング（病棟名・診療科合算など）
│   │   ├── data_loader.py    # データ読込（Excel/CSV）
│   │   ├── preprocess.py     # 前処理（科名合算・全麻フラグなど）
│   │   ├── metrics.py        # KPI算出（日次・週次・ランキング）
│   │   ├── charts.py         # Plotlyグラフデータ生成
│   │   ├── html_builder.py   # Jinja2コンテキスト構築
│   │   ├── profit.py         # 粗利KPI算出
│   │   └── validate.py       # データ整合性検証
│   │
│   └── templates/
│       ├── dashboard.html    # ダッシュボードトップ（医師・看護師タブ）
│       └── dept_report.html  # 診療科別詳細ページ
│
├── data/                 # ← ここにデータを配置（.gitignore推奨）
│   ├── 新入退院クロス*.xlsx
│   ├── サンプル.csv
│   ├── 手術目標.csv
│   ├── 新入院患者_目標値.csv
│   ├── 入院患者ダッシュボード_目標値.csv
│   ├── 粗利データ.xlsx
│   └── 粗利目標.xlsx
│
├── index.html            # ← 生成される（GitHub Pagesで公開）
└── reports/              # ← 生成される（診療科別詳細ページ）
    ├── dept_整形外科.html
    ├── dept_総合内科.html
    └── ...
```

---

## 9. ビジネスルール

### 入院データ

| ルール | 内容 |
|---|---|
| 診療科合算 | 「感染症」「内科」→「総合内科」に合算 |
| 非表示科 | 健診センター・麻酔科・放射線診断科・空白 → 表示しない（集計には含む） |
| 非表示病棟 | 03B病棟 → 目標未設定のため表示除外 |
| 例外病棟名 | 04B → ICU、04D → HCU |
| 新入院患者数 | 入院患者数 + 緊急入院患者数 |
| 在院患者数 | 実態は在院数（慣習上「入院患者数」と呼ぶ） |

### 手術データ

| ルール | 内容 |
|---|---|
| エンコーディング | CP932（セル内改行あり） |
| 手術室名正規化 | 全角`ＯＰ－１` → 半角`OP-1` |
| 全身麻酔判定 | 麻酔種別に「全身麻酔(20分以上：吸入もしくは静脈麻酔薬)」を含む |
| 稼働対象室 | OP-1〜OP-10・OP-12（11室） |
| 稼働時間帯 | 平日 8:45〜17:15（510分/室） |
| 表示対象12科 | 皮膚科・整形外科・産婦人科・歯科口腔外科・耳鼻咽喉科・泌尿器科・一般消化器外科・呼吸器外科・心臓血管外科・乳腺外科・形成外科・脳神経外科 |

### 病院全体目標

| KPI | 目標値 |
|---|---|
| 在院患者数 | 平日 580 / 休日 540 / 全日 567 人 |
| 新入院患者数 | 385 人/週 |
| 全身麻酔手術 | 21 件/日 |

---

## 10. トラブルシューティング

### ファイルが見つからないエラー

```
❌ ファイルが見つかりません: [Errno 2] No such file or directory
```

→ `--data-dir` でデータフォルダのパスを指定してください:
```bash
python generate_html.py --data-dir /Users/yourname/hospital/data
```

### 文字化けする

→ 手術CSVのエンコーディングを確認。CP932（Shift-JIS）が必要です。  
　 `config.py` の `load_surgery_data()` の `encoding="cp932"` を確認してください。

### グラフが表示されない

→ ブラウザのコンソール（F12）でエラーを確認。CDNにアクセスできない場合は  
　 Plotly.jsをダウンロードしてローカル参照に変更してください。

### 検証エラーが出る

```bash
# 検証のみ実行して詳細を確認
make check
# または
python generate_html.py --dry-run
```

### 粗利データが読み込めない

→ `粗利データ.xlsx` の1列目が診療科名、2列目以降が月次データ（横持ち）になっているか確認してください。  
　 ヘッダー行の日付形式は `2024-04-01` 形式または Excel のシリアル日付に対応しています。

### Streamlitアプリのエラー

```bash
# エラーログを確認
streamlit run streamlit_app.py --logger.level debug
```

---

## 開発メモ

### 目標値の更新方法

目標値は `data/` 内のCSV/Excelファイルで管理しています（半年ごとに更新）:
- 在院・新入院目標: `入院患者ダッシュボード_目標値.csv`
- 手術目標: `手術目標.csv`
- 粗利目標: `粗利目標.xlsx`

ファイルを更新後、`make` で再生成してください。

### 新しい診療科の追加

`app/lib/config.py` の `DEPT_MERGE`・`DEPT_HIDDEN`・`SURGERY_DISPLAY_DEPTS` を確認・更新してください。

### デザイン変更

`app/templates/dashboard.html` の `:root { ... }` CSS変数を変更することでカラーテーマを調整できます。

---

*最終更新: 2026/03/27*
