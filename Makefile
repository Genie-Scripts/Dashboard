# ============================================================
# 診療ダッシュボード Makefile
# ============================================================
# 使い方:
#   make          — HTML生成（デフォルト）
#   make check    — データ検証のみ
#   make serve    — ローカルサーバーで確認
#   make streamlit — Streamlitアプリ起動
#   make install  — 依存ライブラリインストール
#   make help     — コマンド一覧表示
# ============================================================

# ── 設定変数（必要に応じて変更） ────────────────────────────
DATA_DIR   ?= data
OUTPUT     ?= doctor.html
SORT_BY    ?= achievement
PORT       ?= 8080
STREAMLIT_PORT ?= 8501

PYTHON     := python3
PIP        := pip3

.DEFAULT_GOAL := build

# ── カラー定義 ───────────────────────────────────────────────
GREEN  := \033[0;32m
YELLOW := \033[0;33m
CYAN   := \033[0;36m
RESET  := \033[0m

# ============================================================
# 主要ターゲット
# ============================================================

## HTML生成（全ページ）
.PHONY: build
build:
	@echo "$(CYAN)🏥 ダッシュボード HTML生成中...$(RESET)"
	$(PYTHON) generate_html.py \
		--data-dir $(DATA_DIR) \
		--output   $(OUTPUT) \
		--sort-by  $(SORT_BY)
	@echo "$(GREEN)✅ 完了$(RESET)"

## HTML生成（詳細ページスキップ・高速）
.PHONY: build-fast
build-fast:
	@echo "$(CYAN)⚡ 高速ビルド（詳細ページスキップ）...$(RESET)"
	$(PYTHON) generate_html.py \
		--data-dir    $(DATA_DIR) \
		--output      $(OUTPUT) \
		--skip-reports

## データ検証のみ（HTML出力なし）
.PHONY: check
check:
	@echo "$(CYAN)🔍 データ検証中...$(RESET)"
	$(PYTHON) generate_html.py \
		--data-dir $(DATA_DIR) \
		--dry-run

## 特定の診療科の詳細ページのみ再生成
## 使用例: make dept DEPT=整形外科
.PHONY: dept
dept:
	@if [ -z "$(DEPT)" ]; then echo "$(YELLOW)使い方: make dept DEPT=整形外科$(RESET)"; exit 1; fi
	@echo "$(CYAN)📋 $(DEPT) レポート再生成中...$(RESET)"
	$(PYTHON) generate_html.py \
		--data-dir $(DATA_DIR) \
		--dept     "$(DEPT)" \
		--skip-reports  # index.htmlは再生成せずレポートのみ

## 特定の基準日で生成
## 使用例: make build-date DATE=2026-03-26
.PHONY: build-date
build-date:
	@if [ -z "$(DATE)" ]; then echo "$(YELLOW)使い方: make build-date DATE=2026-03-26$(RESET)"; exit 1; fi
	$(PYTHON) generate_html.py \
		--data-dir  $(DATA_DIR) \
		--output    $(OUTPUT) \
		--base-date $(DATE)

# ============================================================
# ローカル確認
# ============================================================

## ローカルWebサーバー起動（http://localhost:8080）
.PHONY: serve
serve: build
	@echo "$(GREEN)🌐 http://localhost:$(PORT)/portal.html でサーバー起動中 (Ctrl+C で停止)$(RESET)"
	@echo "$(GREEN)   http://localhost:$(PORT)/doctor.html$(RESET)"
	@echo "$(GREEN)   http://localhost:$(PORT)/nurse.html$(RESET)"
	@echo "$(GREEN)   http://localhost:$(PORT)/admission/index.html$(RESET)"
	@$(PYTHON) -m http.server $(PORT) --directory .

## サーバーのみ起動（ビルドなし）
.PHONY: serve-only
serve-only:
	@echo "$(GREEN)🌐 http://localhost:$(PORT) でサーバー起動中 (Ctrl+C で停止)$(RESET)"
	$(PYTHON) -m http.server $(PORT) --directory .

## Streamlitアプリ起動（メイン）
.PHONY: streamlit
streamlit:
	@echo "$(GREEN)🚀 Streamlit起動中 → http://localhost:$(STREAMLIT_PORT)$(RESET)"
	streamlit run streamlit_app.py \
		--server.port $(STREAMLIT_PORT) \
		--server.headless false

## 新入院ダッシュボード生成ツール（Streamlit版）
.PHONY: streamlit-admission
streamlit-admission:
	@echo "$(GREEN)🚀 新入院ダッシュボード生成ツール起動中 → http://localhost:$(STREAMLIT_PORT)$(RESET)"
	streamlit run admission_app.py \
		--server.port $(STREAMLIT_PORT) \
		--server.headless false

# ============================================================
# 環境セットアップ
# ============================================================

## 依存ライブラリのインストール
.PHONY: install
install:
	@echo "$(CYAN)📦 依存ライブラリをインストール中...$(RESET)"
	$(PIP) install -r requirements.txt
	@echo "$(GREEN)✅ インストール完了$(RESET)"

## データフォルダを初期化（初回セットアップ）
.PHONY: setup
setup:
	@echo "$(CYAN)📁 データフォルダを初期化中...$(RESET)"
	$(PYTHON) generate_html.py --data-dir $(DATA_DIR) --setup
freeze:
	$(PIP) freeze | grep -E "pandas|openpyxl|jinja2|plotly|streamlit|numpy" > requirements.txt
	@echo "requirements.txt を更新しました:"
	@cat requirements.txt

# ============================================================
# GitHub Pages デプロイ
# ============================================================

## GitHub Pages へデプロイ（git push）
## 注意: 事前に git remote origin を設定してください
.PHONY: deploy
deploy: build
	@echo "$(CYAN)🚀 GitHub Pages へデプロイ中...$(RESET)"
	@if ! git diff --quiet HEAD -- portal.html doctor.html nurse.html doctor_summary.json nurse_summary.json admission/ reports/; then \
		git add portal.html doctor.html nurse.html doctor_summary.json nurse_summary.json admission/ reports/ ; \
		git commit -m "Dashboard update: $(shell date '+%Y/%m/%d %H:%M')"; \
		git push origin main; \
		echo "$(GREEN)✅ デプロイ完了$(RESET)"; \
	else \
		echo "$(YELLOW)⚠️  変更なし（スキップ）$(RESET)"; \
	fi

## 更新のみデプロイ（ビルドスキップ）
.PHONY: push
push:
	@git add portal.html doctor.html nurse.html doctor_summary.json nurse_summary.json admission/ reports/ && \
	git commit -m "Dashboard update: $(shell date '+%Y/%m/%d %H:%M')" && \
	git push origin main && \
	echo "$(GREEN)✅ プッシュ完了$(RESET)"

# ============================================================
# ユーティリティ
# ============================================================

## Python構文チェック
.PHONY: lint
lint:
	@echo "$(CYAN)🔎 構文チェック中...$(RESET)"
	$(PYTHON) -m py_compile generate_html.py streamlit_app.py \
		app/lib/config.py app/lib/data_loader.py app/lib/preprocess.py \
		app/lib/metrics.py app/lib/charts.py app/lib/html_builder.py \
		app/lib/profit.py app/lib/validate.py
	@echo "$(GREEN)✅ 構文チェックOK$(RESET)"

## 生成ファイルのクリーンアップ
.PHONY: clean
clean:
	@echo "$(YELLOW)🗑  生成ファイルを削除中...$(RESET)"
	rm -f portal.html doctor.html nurse.html doctor_summary.json nurse_summary.json
	rm -rf admission/ reports/
	@echo "$(GREEN)✅ クリーン完了$(RESET)"

## ヘルプ表示
.PHONY: help
help:
	@echo ""
	@echo "$(CYAN)診療ダッシュボード — 利用可能なコマンド$(RESET)"
	@echo "================================================="
	@echo "  $(GREEN)make$(RESET)             HTML生成（全ページ）"
	@echo "  $(GREEN)make build-fast$(RESET)  高速ビルド（詳細ページスキップ）"
	@echo "  $(GREEN)make setup$(RESET)       データフォルダを初期化（初回のみ）"
	@echo "  $(GREEN)make check$(RESET)       データ検証のみ（HTML出力なし）"
	@echo "  $(GREEN)make serve$(RESET)       ビルド後にローカルサーバー起動"
	@echo "  $(GREEN)make streamlit$(RESET)          Streamlitアプリ起動"
	@echo "  $(GREEN)make streamlit-admission$(RESET) 新入院ダッシュボード生成ツール起動"
	@echo "  $(GREEN)make deploy$(RESET)      GitHub Pagesへデプロイ"
	@echo "  $(GREEN)make dept DEPT=科名$(RESET)  特定科の詳細ページのみ再生成"
	@echo "  $(GREEN)make build-date DATE=YYYY-MM-DD$(RESET)  日付指定でビルド"
	@echo "  $(GREEN)make install$(RESET)     依存ライブラリのインストール"
	@echo "  $(GREEN)make lint$(RESET)        Python構文チェック"
	@echo "  $(GREEN)make clean$(RESET)       生成ファイルの削除"
	@echo ""
	@echo "$(CYAN)出力ファイル:$(RESET)"
	@echo "  portal.html — ポータルページ（起点）"
	@echo "  doctor.html / nurse.html — ダッシュボード本体"
	@echo "  admission/index.html — 新入院患者ダッシュボード"
	@echo "  doctor_summary.json / nurse_summary.json — チャートデータ"
	@echo "  reports/dept_*.html — 診療科別詳細ページ"
	@echo ""
	@echo "$(CYAN)設定変数（環境変数で上書き可）:$(RESET)"
	@echo "  DATA_DIR=$(DATA_DIR)  OUTPUT=$(OUTPUT)"
	@echo "  SORT_BY=$(SORT_BY)    PORT=$(PORT)"
	@echo ""
