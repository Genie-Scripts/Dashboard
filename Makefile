# ============================================================
# 診療ダッシュボード Makefile（v2.1）
# ============================================================
# 提案B: 2層ハブ＆スポーク型
#   Layer-1: portal.html  — 信号機ポータル
#   Layer-2: detail.html  — 統合詳細ダッシュボード
#
# 使い方:
#   make          — HTML生成（portal.html + detail.html）
#   make check    — データ検証のみ
#   make serve    — ローカルサーバーで確認
#   make streamlit — Streamlitアプリ起動
#   make install  — 依存ライブラリインストール
#   make help     — コマンド一覧表示
# ============================================================

# ── 設定変数（必要に応じて変更） ────────────────────────────
DATA_DIR       ?= data
OUTPUT_DIR     ?= .
SORT_BY        ?= achievement
PORT           ?= 8080
STREAMLIT_PORT ?= 8501

PYTHON  := python3
PIP     := pip3

.DEFAULT_GOAL := build

# ── 出力ファイル定義（v2.1: 2ファイル体制） ─────────────────
PORTAL  := $(OUTPUT_DIR)/portal.html
DETAIL  := $(OUTPUT_DIR)/detail.html
# 旧URLリダイレクト
REDIRECTS := $(OUTPUT_DIR)/doctor.html \
             $(OUTPUT_DIR)/nurse.html \
             $(OUTPUT_DIR)/admission/index.html \
             $(OUTPUT_DIR)/inpatient/index.html \
             $(OUTPUT_DIR)/operation/index.html

# ── カラー定義 ───────────────────────────────────────────────
GREEN  := \033[0;32m
YELLOW := \033[0;33m
CYAN   := \033[0;36m
RESET  := \033[0m

# ============================================================
# 主要ターゲット
# ============================================================

## HTML生成（portal.html + detail.html + 旧URLリダイレクト）
.PHONY: build
build:
	@echo "$(CYAN)🏥 ダッシュボード HTML生成中（v2.1: 2層構造）...$(RESET)"
	$(PYTHON) generate_html.py \
		--data-dir   $(DATA_DIR) \
		--output-dir $(OUTPUT_DIR) \
		--sort-by    $(SORT_BY)
	@echo "$(GREEN)✅ 完了: $(PORTAL) / $(DETAIL)$(RESET)"

## HTML生成（旧URLリダイレクトなし）
.PHONY: build-fast
build-fast:
	@echo "$(CYAN)⚡ 高速ビルド（リダイレクトスキップ）...$(RESET)"
	$(PYTHON) generate_html.py \
		--data-dir    $(DATA_DIR) \
		--output-dir  $(OUTPUT_DIR) \
		--no-redirect
	@echo "$(GREEN)✅ 完了$(RESET)"

## データ検証のみ（HTML出力なし）
.PHONY: check
check:
	@echo "$(CYAN)🔍 データ検証中...$(RESET)"
	$(PYTHON) -c "\
		import sys; sys.path.insert(0,'.'); \
		from generate_html import load_and_preprocess; \
		load_and_preprocess('$(DATA_DIR)'); \
		print('✅ データ検証OK')"

## 特定の基準日で生成
## 使用例: make build-date DATE=2026-03-26
.PHONY: build-date
build-date:
	@if [ -z "$(DATE)" ]; then echo "$(YELLOW)使い方: make build-date DATE=2026-03-26$(RESET)"; exit 1; fi
	$(PYTHON) generate_html.py \
		--data-dir   $(DATA_DIR) \
		--output-dir $(OUTPUT_DIR) \
		--base-date  $(DATE)

# ============================================================
# ローカル確認
# ============================================================

## ビルド後にローカルWebサーバー起動
.PHONY: serve
serve: build
	@echo ""
	@echo "$(GREEN)🌐 ローカルサーバー起動中 (Ctrl+C で停止)$(RESET)"
	@echo "$(GREEN)   ポータル:  http://localhost:$(PORT)/portal.html$(RESET)"
	@echo "$(GREEN)   統合詳細:  http://localhost:$(PORT)/detail.html$(RESET)"
	@echo "$(GREEN)   医師向け:  http://localhost:$(PORT)/detail.html#admission?axis=dept$(RESET)"
	@echo "$(GREEN)   看護師向け: http://localhost:$(PORT)/detail.html#inpatient?axis=ward$(RESET)"
	@echo ""
	@$(PYTHON) -m http.server $(PORT) --directory $(OUTPUT_DIR)

## サーバーのみ起動（ビルドなし）
.PHONY: serve-only
serve-only:
	@echo "$(GREEN)🌐 http://localhost:$(PORT) でサーバー起動中 (Ctrl+C で停止)$(RESET)"
	$(PYTHON) -m http.server $(PORT) --directory $(OUTPUT_DIR)

## Streamlitアプリ起動
.PHONY: streamlit
streamlit:
	@echo "$(GREEN)🚀 Streamlit起動中 → http://localhost:$(STREAMLIT_PORT)$(RESET)"
	streamlit run streamlit_app.py \
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
	@echo "$(GREEN)✅ セットアップ完了$(RESET)"

## requirements.txt 更新
.PHONY: freeze
freeze:
	$(PIP) freeze | grep -iE "pandas|openpyxl|jinja2|plotly|streamlit|numpy|jpholiday" > requirements.txt
	@echo "requirements.txt を更新しました:"
	@cat requirements.txt

# ============================================================
# GitHub Pages デプロイ
# ============================================================

## GitHub Pages へデプロイ（ビルド → git add → commit → push）
.PHONY: deploy
deploy: build
	@echo "$(CYAN)🚀 GitHub Pages へデプロイ中...$(RESET)"
	@if ! git diff --quiet HEAD -- portal.html detail.html dept.html doctor.html nurse.html admission/ inpatient/ operation/ 2>/dev/null; then \
		git add portal.html detail.html dept.html; \
		git add -f doctor.html nurse.html admission/ inpatient/ operation/ 2>/dev/null || true; \
		git commit -m "Dashboard update: $$(date '+%Y/%m/%d %H:%M') [v2.1]"; \
		git push origin main; \
		echo "$(GREEN)✅ デプロイ完了$(RESET)"; \
	else \
		echo "$(YELLOW)⚠️  変更なし（スキップ）$(RESET)"; \
	fi

## 更新のみデプロイ（ビルドスキップ）
.PHONY: push
push:
	@git add portal.html detail.html && \
	git add -f doctor.html nurse.html admission/ inpatient/ operation/ 2>/dev/null || true && \
	git commit -m "Dashboard update: $$(date '+%Y/%m/%d %H:%M') [v2.1]" && \
	git push origin main && \
	echo "$(GREEN)✅ プッシュ完了$(RESET)"

# ============================================================
# ユーティリティ
# ============================================================

## Python構文チェック
.PHONY: lint
lint:
	@echo "$(CYAN)🔎 構文チェック中...$(RESET)"
	$(PYTHON) -m py_compile generate_html.py
	$(PYTHON) -m py_compile app/lib/config.py
	$(PYTHON) -m py_compile app/lib/data_loader.py
	$(PYTHON) -m py_compile app/lib/preprocess.py
	$(PYTHON) -m py_compile app/lib/metrics.py
	$(PYTHON) -m py_compile app/lib/charts.py
	$(PYTHON) -m py_compile app/lib/html_builder.py
	$(PYTHON) -m py_compile app/lib/profit.py
	$(PYTHON) -m py_compile app/lib/validate.py
	@echo "$(GREEN)✅ 構文チェックOK$(RESET)"

## 生成ファイルのクリーンアップ
.PHONY: clean
clean:
	@echo "$(YELLOW)🗑  生成ファイルを削除中...$(RESET)"
	rm -f portal.html detail.html
	rm -f doctor.html nurse.html doctor_summary.json nurse_summary.json
	rm -rf admission/ inpatient/ operation/ reports/
	@echo "$(GREEN)✅ クリーン完了$(RESET)"

## 旧ファイルのみ削除（v2.1移行後に一度だけ実行）
.PHONY: clean-legacy
clean-legacy:
	@echo "$(YELLOW)🗑  旧バージョンのファイルを削除中...$(RESET)"
	rm -f doctor_summary.json nurse_summary.json
	rm -rf reports/
	rm -f index.html
	@echo "$(GREEN)✅ 旧ファイル削除完了$(RESET)"
	@echo "$(YELLOW)   ※ doctor.html / nurse.html / admission/ / inpatient/ / operation/ は"
	@echo "     リダイレクト用に残しています$(RESET)"

## ヘルプ表示
.PHONY: help
help:
	@echo ""
	@echo "$(CYAN)診療ダッシュボード v2.1 — 利用可能なコマンド$(RESET)"
	@echo "================================================================="
	@echo ""
	@echo "  $(GREEN)make$(RESET)               HTML生成（portal.html + detail.html）"
	@echo "  $(GREEN)make build-fast$(RESET)     高速ビルド（旧URLリダイレクトスキップ）"
	@echo "  $(GREEN)make build-date DATE=YYYY-MM-DD$(RESET)  日付指定でビルド"
	@echo "  $(GREEN)make setup$(RESET)          データフォルダを初期化（初回のみ）"
	@echo "  $(GREEN)make check$(RESET)          データ検証のみ（HTML出力なし）"
	@echo "  $(GREEN)make serve$(RESET)          ビルド後にローカルサーバー起動"
	@echo "  $(GREEN)make serve-only$(RESET)     サーバーのみ起動（ビルドなし）"
	@echo "  $(GREEN)make streamlit$(RESET)      Streamlitアプリ起動"
	@echo "  $(GREEN)make deploy$(RESET)         GitHub Pagesへデプロイ"
	@echo "  $(GREEN)make push$(RESET)           ビルドなしでpushのみ"
	@echo "  $(GREEN)make install$(RESET)        依存ライブラリのインストール"
	@echo "  $(GREEN)make lint$(RESET)           Python構文チェック"
	@echo "  $(GREEN)make clean$(RESET)          生成ファイルの削除"
	@echo "  $(GREEN)make clean-legacy$(RESET)   旧バージョン固有ファイルの削除"
	@echo ""
	@echo "$(CYAN)出力ファイル（v2.1: 2層構造）:$(RESET)"
	@echo "  portal.html  — Layer-1 信号機ポータル（入口）"
	@echo "  detail.html  — Layer-2 統合詳細ダッシュボード"
	@echo ""
	@echo "$(CYAN)旧URLリダイレクト（自動生成）:$(RESET)"
	@echo "  doctor.html          → detail.html#admission?axis=dept"
	@echo "  nurse.html           → detail.html#inpatient?axis=ward"
	@echo "  admission/index.html → detail.html#admission"
	@echo "  inpatient/index.html → detail.html#inpatient"
	@echo "  operation/index.html → detail.html#operation"
	@echo ""
	@echo "$(CYAN)ロール別ブックマークURL:$(RESET)"
	@echo "  経営層:     portal.html"
	@echo "  医師:       detail.html#admission?axis=dept"
	@echo "  看護師:     detail.html#inpatient?axis=ward"
	@echo "  手術室:     detail.html#operation?axis=dept"
	@echo ""
	@echo "$(CYAN)設定変数（環境変数で上書き可）:$(RESET)"
	@echo "  DATA_DIR=$(DATA_DIR)  OUTPUT_DIR=$(OUTPUT_DIR)"
	@echo "  SORT_BY=$(SORT_BY)    PORT=$(PORT)"
	@echo ""
