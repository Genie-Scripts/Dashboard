#!/usr/bin/env bash
# ============================================================
# Cloudflare Pages 配信用ステージングビルド
# ============================================================
# publish/ を毎回まっさらに作り直し、配信対象ファイルだけをコピーする。
# publish/ 自体は .gitignore 対象（ビルド成果物のため）。
#
# 使い方: bash scripts/build_publish.sh
# ============================================================

set -euo pipefail

# リポジトリルートを基準に動作する
cd "$(dirname "$0")/.."

PUBLISH_DIR="publish"

echo "🧹 ${PUBLISH_DIR}/ を再作成中..."
rm -rf "${PUBLISH_DIR}"
mkdir -p "${PUBLISH_DIR}"

# ── ヘルパー: 存在すればコピー、無ければ警告してスキップ ─────────
copy_if_exists() {
  local src="$1"
  local dest="$2"
  if [ -e "$src" ]; then
    mkdir -p "$(dirname "$dest")"
    cp -R "$src" "$dest"
  else
    echo "⚠️  スキップ: ${src} が見つかりません"
  fi
}

# portal.html だけは配信の起点（index.html の実体）なので必須
if [ ! -f "portal.html" ]; then
  echo "❌ portal.html が見つかりません。先に \`make build\` を実行してください。" >&2
  exit 1
fi

# ── 配信するファイル ──────────────────────────────────────────
copy_if_exists "portal.html" "${PUBLISH_DIR}/portal.html"
copy_if_exists "detail.html" "${PUBLISH_DIR}/detail.html"
copy_if_exists "dept.html" "${PUBLISH_DIR}/dept.html"
copy_if_exists "doctor.html" "${PUBLISH_DIR}/doctor.html"
copy_if_exists "nurse.html" "${PUBLISH_DIR}/nurse.html"
copy_if_exists "admission/index.html" "${PUBLISH_DIR}/admission/index.html"
copy_if_exists "inpatient/index.html" "${PUBLISH_DIR}/inpatient/index.html"
copy_if_exists "operation/index.html" "${PUBLISH_DIR}/operation/index.html"
copy_if_exists "docs/dept_reports_manual.html" "${PUBLISH_DIR}/docs/dept_reports_manual.html"
copy_if_exists "運用マニュアル.html" "${PUBLISH_DIR}/運用マニュアル.html"
copy_if_exists "robots.txt" "${PUBLISH_DIR}/robots.txt"

# ルート index.html は portal.html の実体コピー（リダイレクトではない）
cp "portal.html" "${PUBLISH_DIR}/index.html"

# ── 配信しないもの（意図的に除外・理由をコメントで明記） ─────────
# admission_index.html / inpatient_index.html … 孤立レガシーファイル（現行導線から未参照）
# reports/ (index.html + dept_*.html 33件)       … 2026-06-19に一度だけ生成された旧世代の成果物。
#                                                  内包データは2026-04-02までで現行値ではなく、
#                                                  portal/detail/dept のどこからもリンクされていない。
#                                                  現行の部門レポートは dept_reports/ 系（別系統）で運用。
#                                                  古い数値が現行レポートと誤認されるため配信しない。
# app/templates/                                 … Jinja2テンプレート（ソース。生成物のみ配信）
# hospital_analysis_report.html                  … 別系統の作業成果物・配信対象外
# 粗利ダッシュボード_配信用.html                 … 財務データ含む院内限定配布物（.gitignore対象）
# *.py / *.md / Makefile                         … ソース・ドキュメント（配信物ではない）
# data/ / output/ / tests/ / .venv/ / .git/       … ローカル専用・非配信ディレクトリ

# ── _headers 生成（noindex・no-cache・クリックジャッキング対策） ─
cat > "${PUBLISH_DIR}/_headers" <<'EOF'
/*
  X-Robots-Tag: noindex, nofollow
  Referrer-Policy: no-referrer
  X-Frame-Options: DENY
  Cache-Control: private, no-cache
EOF

# ── サマリ出力 ────────────────────────────────────────────────
echo ""
echo "📦 ${PUBLISH_DIR}/ ビルド完了"

FILE_COUNT=$(find "${PUBLISH_DIR}" -type f | wc -l | tr -d ' ')
TOTAL_SIZE=$(du -sh "${PUBLISH_DIR}" | cut -f1)
echo "   ファイル数: ${FILE_COUNT}"
echo "   総サイズ:   ${TOTAL_SIZE}"

# Cloudflare Pages のファイルサイズ上限（25MiB）チェック
OVERSIZED=$(find "${PUBLISH_DIR}" -type f -size +25M)
if [ -n "$OVERSIZED" ]; then
  echo "❌ 25MiBを超えるファイルがあります:"
  echo "$OVERSIZED"
  exit 1
else
  echo "✅ 25MiB超のファイルなし"
fi
