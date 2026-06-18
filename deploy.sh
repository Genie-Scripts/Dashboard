#!/bin/bash
# deploy.sh — 作業終了時: ビルド → コミット → プッシュ
set -euo pipefail

# Homebrew（Apple Silicon）のパスを明示的に追加
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

# ログ出力先（ホームディレクトリの隠しフォルダなどに変えるとより管理しやすいです）
LOG="/tmp/dashboard_deploy.log"
echo "=== $(date '+%Y/%m/%d %H:%M:%S') deploy 開始 ===" >> "$LOG"

# 通知関数
notify() {
  osascript -e "display notification \"$1\" with title \"診療ダッシュボード\" subtitle \"$2\"" 2>/dev/null || true
}

# エラーダイアログ関数
error_dialog() {
  osascript -e "display dialog \"$1\" buttons {\"OK\"} with title \"エラー\" with icon caution" 2>/dev/null || true
  echo "❌ $1" >> "$LOG"
}

# 予期せぬエラー時に実行
trap 'error_dialog "予期せぬエラーで停止しました。詳細は $LOG を確認してください。"' ERR

# ── 0a. oMLX（要約LLM・OpenAI互換）の起動確認 ──
# 要約LLMは oMLX(127.0.0.1:8000) に統一（旧 Ollama から移行）。非Docker・ホスト実行なので localhost。
# export して python(generate_html.py / app/lib/llm.py)へ確実に渡す。
export OMLX_BASE_URL="${OMLX_BASE_URL:-http://localhost:8000/v1}"
export OMLX_MODEL="${OMLX_MODEL:-Llama-3.1-Swallow-8B-Instruct-v0.5}"
# APIキーは ~/.omlx/settings.json から取得（取れなければ llm.py の既定にフォールバック）
_omlx_key="$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.omlx/settings.json')))['auth']['api_key'])" 2>/dev/null || true)"
[ -n "$_omlx_key" ] && export OMLX_API_KEY="$_omlx_key"

if ! curl -s -o /dev/null --max-time 3 -H "Authorization: Bearer ${OMLX_API_KEY:-x}" "$OMLX_BASE_URL/models"; then
  echo "🧠 oMLX を起動中..." >> "$LOG"
  open -a oMLX >/dev/null 2>&1 || true
  for i in $(seq 1 30); do
    curl -s -o /dev/null --max-time 3 -H "Authorization: Bearer ${OMLX_API_KEY:-x}" "$OMLX_BASE_URL/models" && break
    sleep 1
  done
fi

# ── 0b. モデル存在確認（oMLX はローカルのモデルファイル前提＝自動pull は無し）──
if curl -s --max-time 5 -H "Authorization: Bearer ${OMLX_API_KEY:-x}" "$OMLX_BASE_URL/models" | grep -q "\"$OMLX_MODEL\""; then
  echo "✅ oMLX 準備済: $OMLX_MODEL" >> "$LOG"
else
  echo "⚠️  oMLX 未起動 or モデル '$OMLX_MODEL' 未取得 — AI生成はスキップされます" >> "$LOG"
  notify "oMLX/モデル未確認: AI生成はスキップ" "$OMLX_MODEL"
fi

# ── 0. ディレクトリ移動と環境有効化 ──
# スクリプトがある場所（Dashboardフォルダ）へ移動
cd "$(dirname "$0")"

# 新しい環境（uv）の仮想環境を有効化
if [ -f ".venv/bin/activate" ]; then
  source .venv/bin/activate
else
  error_dialog "仮想環境(.venv)が見つかりません。uv venvを実行してください。"
  exit 1
fi

# ── 1. HTML ビルド ──
echo "🔨 ビルド中..." && notify "ビルド中..." "HTML生成"
# uv環境では 'python' コマンドで 3.11 が動きます
if ! python generate_html.py --data-dir data --sort-by achievement >> "$LOG" 2>&1; then
  error_dialog "HTMLのビルドに失敗しました。Pythonの実行エラーを確認してください。"
  exit 1
fi
echo "✅ ビルド完了" >> "$LOG"

# ── 1b. 医業収支 推計レポート（ローカル閲覧専用・git には載せない）──
echo "📊 医業収支 推計レポート生成中..." >> "$LOG"
if python -m scripts.build_pl_projection \
      --data-dir data \
      --output   output/pl_projection.html >> "$LOG" 2>&1; then
  echo "✅ pl_projection.html 生成完了" >> "$LOG"
else
  echo "⚠️  pl_projection.html 生成に失敗（デプロイは継続）" >> "$LOG"
fi

# ── 2. ソースコード + 生成HTML をステージ ──
# 注: output/pl_projection.html は公開しない方針のため git add しない
# さきほど設定した .gitignore により .venv や data/ は自動で除外されます
git add .gitignore robots.txt generate_html.py portal.html detail.html dept.html \
        app/templates/ app/lib/ 2>/dev/null || true

# ── 3. 変更がなければスキップ ──
if git diff --cached --quiet; then
  echo "⚠️  変更なし。スキップ。" >> "$LOG"
  notify "変更なし。スキップしました。" "deploy"
  exit 0
fi

# ── 4. コミット ──
MSG="Dashboard update: $(date '+%Y/%m/%d %H:%M') [M5-Pro]"
git commit -m "$MSG" >> "$LOG" 2>&1
echo "✅ コミット: $MSG" >> "$LOG"

# ── 5. プッシュ（non-fast-forward 時は rebase で1回リトライ）──
# SSH通信設定済みなので、パスワードなしで通るはずです
push_with_retry() {
  if git push origin main >> "$LOG" 2>&1; then
    return 0
  fi
  echo "⚠️  push拒否 — リモートの変更を取り込んでリトライします" >> "$LOG"
  if ! git pull --rebase origin main >> "$LOG" 2>&1; then
    echo "❌ rebase 失敗（コンフリクトの可能性）" >> "$LOG"
    return 1
  fi
  git push origin main >> "$LOG" 2>&1
}

if ! push_with_retry; then
  error_dialog "GitHubへのpushに失敗しました。手動で 'git pull --rebase origin main' 後に再実行してください。"
  exit 1
fi

echo "✅ push 完了" >> "$LOG"
notify "GitHubへの保存が完了しました。" "✅ deploy 完了"