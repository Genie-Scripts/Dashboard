#!/bin/bash
# deploy.sh — 作業終了時: ビルド → Cloudflare Pages へ配信
set -euo pipefail

# Homebrew（Apple Silicon）のパスを明示的に追加
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

# ログ出力先（ホームディレクトリの隠しフォルダなどに変えるとより管理しやすいです）
LOG="/tmp/dashboard_deploy.log"
echo "=== $(date '+%Y/%m/%d %H:%M:%S') deploy 開始 ===" >> "$LOG"

# 通知関数（GENIE_HEADLESS=1＝業務ハブからの実行時は osascript を使わずログのみ＝背景実行で固まらない）
notify() {
  if [ "${GENIE_HEADLESS:-}" = "1" ]; then echo "🔔 $1 / $2" >> "$LOG"; return; fi
  osascript -e "display notification \"$1\" with title \"診療ダッシュボード\" subtitle \"$2\"" 2>/dev/null || true
}

# エラーダイアログ関数（ヘッドレス時はブロッキングする display dialog を出さずログのみ）
error_dialog() {
  echo "❌ $1" >> "$LOG"
  if [ "${GENIE_HEADLESS:-}" = "1" ]; then return; fi
  osascript -e "display dialog \"$1\" buttons {\"OK\"} with title \"エラー\" with icon caution" 2>/dev/null || true
}

# 予期せぬエラー時に実行
trap 'error_dialog "予期せぬエラーで停止しました。詳細は $LOG を確認してください。"' ERR

# ── 0a. oMLX（要約LLM・OpenAI互換）の起動確認 ──
# 要約LLMは oMLX(127.0.0.1:8000) に統一（旧 Ollama から移行）。非Docker・ホスト実行なので localhost。
# export して python(generate_html.py / app/lib/llm.py)へ確実に渡す。
# 案1 P2: 既定を LLMブローカー(:8936) 経由にする（透過プロキシなので応答は同一・直列化/入場制御の恩恵を受ける）。
# 明示指定すればそちらが優先＝切り戻しは OMLX_BASE_URL=http://localhost:8000/v1 で可能。
export OMLX_BASE_URL="${OMLX_BASE_URL:-http://127.0.0.1:8936/v1}"
export OMLX_MODEL="${OMLX_MODEL:-gemma-4-26B-A4B-it-MLX-8bit}"  # 2026-08-13 ハブoverride実効値へ同期（app/lib/llm.py と同値にすること）
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

# ── 1c. 部門ダッシュボード 自己完結HTML（院内LAN配信用・git には載せない）──
# Comedix資料室へ手動アップする単一ファイル: Plotly同梱・他ページナビ非表示・日付入り和名
# （部門ダッシュボード_YYYY-MM-DD.html）。出力先 output/selfcontained/ は .gitignore 済みのため
# git add されず公開リポには出ない。失敗してもデプロイ（Cloudflare Pages配信）は継続。
echo "🏥 部門ダッシュボード(自己完結)生成中..." >> "$LOG"
if python scripts/build_selfcontained.py --profile dept-standalone >> "$LOG" 2>&1; then
  echo "✅ 部門ダッシュボード(自己完結)生成完了 → output/selfcontained/" >> "$LOG"
else
  echo "⚠️  部門ダッシュボード(自己完結)生成に失敗（デプロイは継続）" >> "$LOG"
fi

echo "📋 診療KPIポータル(自己完結)生成中..." >> "$LOG"
if python scripts/build_selfcontained.py --profile portal-standalone >> "$LOG" 2>&1; then
  echo "✅ 診療KPIポータル(自己完結)生成完了 → output/selfcontained/" >> "$LOG"
else
  echo "⚠️  診療KPIポータル(自己完結)生成に失敗（デプロイは継続）" >> "$LOG"
fi

# ── 2. Cloudflare Pages 配信用ステージング（publish/ を再構築）──
# 公開先は GitHub Pages から Cloudflare Pages へ移行済み（旧 GitHub Pages 側は
# 案内ページ専用ブランチ pages-notice に切替済み・main の生成HTMLはもう配信されない）。
# git へのコミット/プッシュはやめ、wrangler で Cloudflare へ直接配信する。
CF_PAGES_PROJECT="${CF_PAGES_PROJECT:-hospital-dashboard}"

echo "📦 配信用ステージング(publish/)構築中..." >> "$LOG"
if ! bash scripts/build_publish.sh >> "$LOG" 2>&1; then
  error_dialog "配信用ステージング(publish/)の構築に失敗しました。$LOG を確認してください。"
  exit 1
fi
echo "✅ publish/ 構築完了" >> "$LOG"

# ── 3. 変更検知はしない・毎回デプロイ ──
# git diff ベースの「変更がなければスキップ」は GitHub push 前提の最適化だったが、
# Cloudflare Pages では wrangler 側がハッシュ差分アップロード（実体が同一なら実質no-op）
# するため、こちらで事前にスキップ判定する意味がない。常に deploy する。

# ── 4. Cloudflare Pages へデプロイ（wrangler）──
# 認証は `wrangler login` の OAuth トークン（~/.wrangler）に依存する。自動実行中に
# トークンが失効するとサイレントに失敗し得るため、失敗時は必ず error_dialog で気付ける
# ようにしている。CLOUDFLARE_API_TOKEN 環境変数が設定されていればそちらが優先して使われる
# （OAuthトークンより優先。CIやヘッドレス実行での失効対策として設定を推奨）。
echo "☁️  Cloudflareへデプロイ中..." >> "$LOG"
if ! npx wrangler pages deploy publish --project-name="$CF_PAGES_PROJECT" --branch=main --commit-dirty=true >> "$LOG" 2>&1; then
  error_dialog "Cloudflareへのデプロイに失敗しました。'wrangler login' の認証切れの可能性があります。$LOG を確認してください。"
  exit 1
fi

echo "✅ デプロイ完了" >> "$LOG"
notify "Cloudflareへの配信が完了しました。" "✅ deploy 完了"