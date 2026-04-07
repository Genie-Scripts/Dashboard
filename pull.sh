#!/bin/bash
# pull.sh — 作業開始時: 最新コードを取得
set -euo pipefail

notify() {
  osascript -e "display notification \"$1\" with title \"診療ダッシュボード\" subtitle \"$2\"" 2>/dev/null || true
}

cd "$(dirname "$0")"

echo "📥 GitHubから最新を取得中..."
if git pull origin main; then
  echo "✅ pull 完了"
  notify "最新コードを取得しました。" "✅ pull 完了"
else
  osascript -e "display dialog \"pullに失敗しました。\" buttons {\"OK\"} with title \"エラー\" with icon caution" 2>/dev/null || true
  exit 1
fi
