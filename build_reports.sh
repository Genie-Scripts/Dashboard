#!/bin/bash
# build_reports.sh — 部門別レポートPDF を1コマンドで生成（AppleScript/Automator から実行可）
#
#   AppleScript 例:
#     do shell script "/Users/genie/dev/ai-apps/Dashboard/build_reports.sh"
#
#   既定: 軸ごとに連結した1ファイル（診療科版/病棟版_{基準日}.pdf）＋ 一手AI。
#   引数はそのまま scripts/build_dept_reports.py へ渡す:
#     ./build_reports.sh --no-ai   # 一手を定型文のみ（oMLX不要・高速）
#     ./build_reports.sh --split   # 部門ごとの個別PDFに分割
#     ./build_reports.sh --base-date 2026-05-31
set -euo pipefail

# Homebrew（Apple Silicon）のパスを明示的に追加（AppleScript からは PATH が最小のため）
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

LOG="/tmp/dept_reports.log"
echo "=== $(date '+%Y/%m/%d %H:%M:%S') 部門レポート生成 開始 ===" >> "$LOG"

notify() {
  osascript -e "display notification \"$1\" with title \"部門別レポートPDF\" subtitle \"$2\"" 2>/dev/null || true
}
error_dialog() {
  osascript -e "display dialog \"$1\" buttons {\"OK\"} with title \"エラー\" with icon caution" 2>/dev/null || true
  echo "❌ $1" >> "$LOG"
}
trap 'error_dialog "予期せぬエラーで停止しました。詳細は $LOG を確認してください。"' ERR

# スクリプトのある Dashboard フォルダへ移動
cd "$(dirname "$0")"

# ── oMLX（一手AIの要約LLM・OpenAI互換）の起動確認・環境設定 ──
# 未起動でも build_dept_reports.py が定型文へ自動フォールバックするので致命ではない。
export OMLX_BASE_URL="${OMLX_BASE_URL:-http://localhost:8000/v1}"
export OMLX_MODEL="${OMLX_MODEL:-Llama-3.1-Swallow-8B-Instruct-v0.5}"
_omlx_key="$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.omlx/settings.json')))['auth']['api_key'])" 2>/dev/null || true)"
[ -n "$_omlx_key" ] && export OMLX_API_KEY="$_omlx_key"
if ! curl -s -o /dev/null --max-time 3 -H "Authorization: Bearer ${OMLX_API_KEY:-x}" "$OMLX_BASE_URL/models"; then
  echo "🧠 oMLX を起動中..." >> "$LOG"
  open -a oMLX >/dev/null 2>&1 || true
  for _ in $(seq 1 30); do
    curl -s -o /dev/null --max-time 3 -H "Authorization: Bearer ${OMLX_API_KEY:-x}" "$OMLX_BASE_URL/models" && break
    sleep 1
  done
fi

# ── 仮想環境（uv）を有効化 ──
if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
else
  error_dialog "仮想環境(.venv)が見つかりません。uv venv を実行してください。"
  exit 1
fi

# ── 生成（引数はそのまま渡す。既定=軸ごと連結＋AI）──
echo "🖨  レポート生成中..." && notify "生成中…" "入退院バランス"
if ! python scripts/build_dept_reports.py "$@" >> "$LOG" 2>&1; then
  error_dialog "レポート生成に失敗しました。詳細は $LOG を確認してください。"
  exit 1
fi
echo "✅ 生成完了" >> "$LOG"

# ── 最新の出力フォルダを開いて通知 ──
LATEST="$(ls -dt dept_reports/*/ 2>/dev/null | head -1 || true)"
[ -n "$LATEST" ] && open "$LATEST" 2>/dev/null || true
notify "PDF生成が完了しました。" "✅ ${LATEST:-dept_reports/}"
echo "✅ 完了: ${LATEST:-dept_reports/}" >> "$LOG"
