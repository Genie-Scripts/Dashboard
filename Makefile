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
#   make install  — 依存ライブラリインストール
#   make help     — コマンド一覧表示
# ============================================================

# ── 設定変数（必要に応じて変更） ────────────────────────────
DATA_DIR       ?= data
OUTPUT_DIR     ?= .
SORT_BY        ?= achievement
PORT           ?= 8080
CF_PAGES_PROJECT ?= hospital-dashboard

# AIナラティブ生成先。broker(:8936)経由が既定（deploy.sh/build_reports.sh と同値・
# 直列化/入場制御の恩恵を受ける）。切り戻しは: OMLX_BASE_URL=http://localhost:8000/v1 make
export OMLX_BASE_URL ?= http://127.0.0.1:8936/v1

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

## 指標関数のユニットテスト
.PHONY: test
test:
	@echo "$(CYAN)🧪 ユニットテスト実行中...$(RESET)"
	$(PYTHON) -m unittest discover -s tests -v

## P2 判定閾値用 ユニット別σ 測定（読み取り専用・stdoutのみ。app/lib/unit_sigma.py への
## 貼り付けは手動。build系には含めない＝毎ビルド再計算するとσが揺れて安定化の目的を壊すため）
.PHONY: unit-sigma
unit-sigma:
	@echo "$(CYAN)📐 ユニット別σ 測定中...$(RESET)"
	$(PYTHON) scripts/measure_unit_sigma.py --data-dir $(DATA_DIR)

## 特定の基準日で生成
## 使用例: make build-date DATE=2026-03-26
.PHONY: build-date
build-date:
	@if [ -z "$(DATE)" ]; then echo "$(YELLOW)使い方: make build-date DATE=2026-03-26$(RESET)"; exit 1; fi
	$(PYTHON) generate_html.py \
		--data-dir   $(DATA_DIR) \
		--output-dir $(OUTPUT_DIR) \
		--base-date  $(DATE)

## 医業収支 推計レポート生成（ローカル閲覧専用）
## 出力: output/pl_projection.html（portal からはリンクしない）
.PHONY: pl
pl:
	@echo "$(CYAN)📊 医業収支 推計レポート生成中...$(RESET)"
	$(PYTHON) -m scripts.build_pl_projection \
		--data-dir $(DATA_DIR) \
		--output   $(OUTPUT_DIR)/output/pl_projection.html
	@echo "$(GREEN)✅ 完了: $(OUTPUT_DIR)/output/pl_projection.html$(RESET)"

## 部門別レポートPDF 一括生成（ローカル印刷・配布専用）
## 出力: dept_reports/{基準日}/診療科版_{基準日}.pdf・病棟版_{基準日}.pdf（軸ごと連結＝一括印刷用）
##       ＋ レビュー_{基準日}.html（一手をその場で直して overrides.md へ保存→再実行で差し替え）
## 個別PDFに分割: make reports SPLIT=on ／ 一手を定型文のみ: make reports AI=off
.PHONY: reports
reports:
	@echo "$(CYAN)🖨  部門別レポートPDF 生成中...$(RESET)"
	$(PYTHON) scripts/build_dept_reports.py \
		--data-dir $(DATA_DIR) \
		$(if $(filter off,$(AI)),--no-ai,) \
		$(if $(filter on,$(SPLIT)),--split,) \
		$(if $(filter on,$(SERVE)),--serve,) \
		$(if $(DATE),--base-date $(DATE),)
	@echo "$(GREEN)✅ 完了: dept_reports/$(RESET)"

## 一手レビュー（ビルド→レビューHTMLをブラウザで自動オープン・「PDF再作成」ボタン有効）
## 終了は Ctrl+C。オプションは reports と同じ（AI=off / DATE=YYYY-MM-DD）
.PHONY: review
review:
	@$(MAKE) reports SERVE=on

## 編集テレメトリ（AIコメントへの人手添削の可視化・読み取り専用・P5-f）
## 出力: 標準出力（基準日ごとの添削率・編集距離・topic/axis内訳・トレンド）。CSV=path で追加出力
.PHONY: edit-telemetry
edit-telemetry:
	@echo "$(CYAN)📊 編集テレメトリ集計中...$(RESET)"
	$(PYTHON) scripts/report_edit_telemetry.py \
		$(if $(CSV),--csv $(CSV),)
	@echo "$(GREEN)✅ 完了$(RESET)"

## Comedix「お知らせ・回覧板」用 週報サマリーPNG 生成（院内LAN配信・レバーB）
## 出力: output/comedix/週報サマリー.png（資料室の同じ文書へ毎週上書き）＋ お知らせ本文.html（初回のみ貼付）
.PHONY: comedix
comedix:
	@echo "$(CYAN)🖼  Comedix週報サマリーPNG 生成中...$(RESET)"
	$(PYTHON) scripts/build_comedix_card.py \
		--data-dir $(DATA_DIR) \
		$(if $(DATE),--base-date $(DATE),) \
		$(if $(REFRESH),--refresh,)
	@echo "$(GREEN)✅ 完了: output/comedix/週報サマリー.png（一手: output/comedix/今週の一手.md）$(RESET)"

## Comedix「お知らせ・回覧板」用 単一HTML週報 生成（直貼り・画像不要・インラインstyleのみ）
## 出力: output/comedix/単一HTML週報.html（お知らせ HTMLソースモードへ毎週貼付）。一手は 今週の一手.md を共有
.PHONY: comedix-html
comedix-html:
	@echo "$(CYAN)📰 Comedix単一HTML週報 生成中...$(RESET)"
	$(PYTHON) scripts/build_comedix_html.py \
		--data-dir $(DATA_DIR) \
		$(if $(DATE),--base-date $(DATE),) \
		$(if $(REFRESH),--refresh,) \
		$(if $(ALL_TRENDS),--with-all-trends,)
	@echo "$(GREEN)✅ 完了: output/comedix/単一HTML週報.html（一手: output/comedix/今週の一手.md）$(RESET)"

## 全病院 実績まとめPDF（詳細・印刷/資料室リンク）: KPI3＋12週トレンド3枚＋病棟別/診療科別テーブル
.PHONY: report-hospital
report-hospital:
	@echo "$(CYAN)📑 全病院 実績まとめPDF 生成中...$(RESET)"
	$(PYTHON) scripts/build_hospital_report.py \
		--data-dir $(DATA_DIR) \
		$(if $(DATE),--base-date $(DATE),)
	@echo "$(GREEN)✅ 完了: output/comedix/実績まとめ_<基準日>.pdf$(RESET)"

## 週次ダイジェスト自動生成（掲示A4＋メール貼付テキスト・push型周知）
## 出力: output/weekly_digest/{基準日}/週次ダイジェスト_{基準日}.{html,pdf,txt}
.PHONY: digest
digest:
	@echo "$(CYAN)📰 週次ダイジェスト 生成中...$(RESET)"
	$(PYTHON) scripts/build_weekly_digest.py \
		--data-dir $(DATA_DIR) \
		$(if $(DATE),--base-date $(DATE),)
	@echo "$(GREEN)✅ 完了: output/weekly_digest/$(RESET)"

## 週次ダイジェスト（一手同様、確定差分の箇条書きのみ・oMLX不要・高速）
.PHONY: digest-fast
digest-fast:
	@echo "$(CYAN)⚡ 週次ダイジェスト（高速・AI要約なし）生成中...$(RESET)"
	$(PYTHON) scripts/build_weekly_digest.py \
		--data-dir $(DATA_DIR) \
		--no-ai \
		$(if $(DATE),--base-date $(DATE),)
	@echo "$(GREEN)✅ 完了: output/weekly_digest/$(RESET)"

## Comedix配布用 自己完結HTML（部門＋ポータル）を再出力
## 出力: output/selfcontained/部門ダッシュボード_{基準日}.html ＋ 診療KPIポータル_{基準日}.html
.PHONY: selfcontained
selfcontained:
	@echo "$(CYAN)📦 Comedix配布用 自己完結HTML 生成中...$(RESET)"
	$(PYTHON) scripts/build_selfcontained.py --profile dept-standalone
	$(PYTHON) scripts/build_selfcontained.py --profile portal-standalone
	@echo "$(GREEN)✅ 完了: output/selfcontained/$(RESET)"

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
	$(PIP) freeze | grep -iE "pandas|openpyxl|jinja2|plotly|numpy|jpholiday" > requirements.txt
	@echo "requirements.txt を更新しました:"
	@cat requirements.txt

# ============================================================
# Cloudflare Pages デプロイ（Direct Upload方式）
# ============================================================

## 配信用ステージング publish/ を作り直す（Basic認証は functions/_middleware.js で別途適用）
.PHONY: publish
publish:
	@bash scripts/build_publish.sh

## Cloudflare Pages へデプロイ（ビルド → publish/ 再構築 → wrangler pages deploy）
.PHONY: deploy-cf
deploy-cf: build
	@bash scripts/build_publish.sh
	npx wrangler pages deploy publish --project-name="$(CF_PAGES_PROJECT)" --branch=main --commit-dirty=true

## Cloudflare Pages へデプロイ（deploy-cf の別名。公開先は Cloudflare へ移行済み）
.PHONY: deploy
deploy: deploy-cf

# ============================================================
# 旧 GitHub Pages 向け（公開には反映されない）
# ============================================================
# 公開先は Cloudflare Pages へ移行済みで、旧 GitHub Pages 側は案内ページ専用ブランチ
# pages-notice に切替済み。以下の2ターゲットは main へ生成HTMLをコミット＆push するだけで、
# 公開サイトには一切反映されない。履歴を残す等の明示的な目的がある時だけ使うこと。

## [旧] 生成HTMLを git にコミットして push（公開には反映されない）
.PHONY: deploy-git
deploy-git: build
	@echo "$(YELLOW)⚠️  これは旧 GitHub Pages 向けです。公開サイト（Cloudflare Pages）には反映されません。$(RESET)"
	@echo "$(YELLOW)   公開したい場合は make deploy（= deploy-cf）を使ってください。$(RESET)"
	@if ! git diff --quiet HEAD -- portal.html detail.html dept.html doctor.html nurse.html admission/ inpatient/ operation/ 2>/dev/null; then \
		git add portal.html detail.html dept.html; \
		git add -f doctor.html nurse.html admission/ inpatient/ operation/ 2>/dev/null || true; \
		git commit -m "Dashboard update: $$(date '+%Y/%m/%d %H:%M') [v2.1]"; \
		git push origin main; \
		echo "$(GREEN)✅ push 完了（公開サイトには反映されません）$(RESET)"; \
	else \
		echo "$(YELLOW)⚠️  変更なし（スキップ）$(RESET)"; \
	fi

## [旧] ビルドなしで git push のみ（公開には反映されない）
.PHONY: push-git
push-git:
	@echo "$(YELLOW)⚠️  これは旧 GitHub Pages 向けです。公開サイト（Cloudflare Pages）には反映されません。$(RESET)"
	@git add portal.html detail.html && \
	git add -f doctor.html nurse.html admission/ inpatient/ operation/ 2>/dev/null || true && \
	git commit -m "Dashboard update: $$(date '+%Y/%m/%d %H:%M') [v2.1]" && \
	git push origin main && \
	echo "$(GREEN)✅ プッシュ完了（公開サイトには反映されません）$(RESET)"

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
	@echo "  $(GREEN)make deploy$(RESET)         Cloudflare Pagesへデプロイ（= deploy-cf）"
	@echo "  $(GREEN)make deploy-cf$(RESET)      Cloudflare Pagesへデプロイ（Direct Upload）"
	@echo "  $(GREEN)make publish$(RESET)        Cloudflare Pages配信用 publish/ を再構築"
	@echo "  $(GREEN)make deploy-git$(RESET)     [旧] gitにコミットしてpush（公開には反映されません）"
	@echo "  $(GREEN)make push-git$(RESET)       [旧] ビルドなしでpushのみ（公開には反映されません）"
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
