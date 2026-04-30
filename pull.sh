#!/bin/bash
# pull.sh — 作業開始時: 最新コードを取得 + ライブラリ更新
set -euo pipefail

notify() {
  osascript -e "display notification \"$1\" with title \"診療ダッシュボード\" subtitle \"$2\"" 2>/dev/null || true
}

# スクリプトの場所（Dashboardフォルダ）へ移動
cd "$(dirname "$0")"

echo "📥 GitHubから最新を取得中..."
if git pull origin main; then
  echo "✅ pull 完了"
  
  # --- 追加ポイント：ライブラリの更新を自動反映 ---
  if [ -f "requirements.txt" ]; then
    echo "📦 ライブラリの更新を確認中..."
    # uvを使って一瞬で同期（変更がなければスキップされます）
    # 仮想環境が有効でなくても .venv を探して実行してくれます
    ./.venv/bin/uv pip install -r requirements.txt > /dev/null 2>&1 || true
  fi
  # -------------------------------------------

  notify "最新コードとライブラリを同期しました。" "✅ 準備完了"
else
  osascript -e "display dialog \"pullに失敗しました。競合が発生している可能性があります。\" buttons {\"OK\"} with title \"エラー\" with icon caution" 2>/dev/null || true
  exit 1
fi