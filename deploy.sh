#!/bin/bash
# deploy.sh — 作業終了時: ビルド → コミット → プッシュ
set -euo pipefail

LOG="/tmp/dashboard_deploy.log"
echo "=== $(date '+%Y/%m/%d %H:%M:%S') deploy 開始 ===" >> "$LOG"

notify() {
  osascript -e "display notification \"$1\" with title \"診療ダッシュボード\" subtitle \"$2\"" 2>/dev/null || true
}
error_dialog() {
  osascript -e "display dialog \"$1\" buttons {\"OK\"} with title \"エラー\" with icon caution" 2>/dev/null || true
  echo "❌ $1" >> "$LOG"
}
trap 'error_dialog "予期せぬエラーで停止しました。"' ERR

cd "$(dirname "$0")"
source /Users/Genie/streamlit_project/streamlit_env/bin/activate

# ── 1. HTML ビルド ──
echo "🔨 ビルド中..." && notify "ビルド中..." "HTML生成"
if ! python3 generate_html.py --data-dir data --sort-by achievement >> "$LOG" 2>&1; then
  error_dialog "HTMLのビルドに失敗しました。"
  exit 1
fi
echo "✅ ビルド完了" >> "$LOG"

# ── 2. ソースコード + 生成HTML をステージ ──
git add app/templates/ app/lib/config.py app/lib/html_builder.py \
        app/lib/metrics.py app/lib/profit.py app/lib/charts.py \
        app/lib/profit.py generate_html.py \
        portal.html detail.html dept.html \
        .gitignore 2>/dev/null || true

# ── 3. 変更がなければスキップ ──
if git diff --cached --quiet; then
  echo "⚠️  変更なし。スキップ。" >> "$LOG"
  notify "変更なし。スキップしました。" "deploy"
  exit 0
fi

# ── 4. コミット ──
MSG="Dashboard update: $(date '+%Y/%m/%d %H:%M') [v2.1]"
git commit -m "$MSG" >> "$LOG" 2>&1
echo "✅ コミット: $MSG" >> "$LOG"

# ── 5. プッシュ ──
if ! git push origin main >> "$LOG" 2>&1; then
  error_dialog "GitHubへのpushに失敗しました。"
  exit 1
fi

echo "✅ push 完了" >> "$LOG"
notify "GitHubへの保存が完了しました。" "✅ deploy 完了"
