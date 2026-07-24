#!/bin/bash
# build_reports.sh — 部門別レポートPDF を1コマンドで生成（AppleScript/Automator から実行可）
#
#   AppleScript 例:
#     do shell script "/Users/genie/dev/ai-apps/Dashboard/build_reports.sh"
#
#   既定＝**レビュー運用**: ビルド完了後にレビュー画面（レビュー_{基準日}.html）が
#   ブラウザで自動的に開く。一手を直したら「保存」→「PDF再作成」で確定し、
#   画面内の「できあがりPDF」リンクから印刷する。
#   レビューサーバはバックグラウンドで常駐し、無操作2時間で自動終了する
#   （このスクリプト自体はレビュー画面が開いた時点で終了＝AppleScriptは固まらない）。
#
#   従来どおり生成だけして終わる場合:
#     ./build_reports.sh --no-review
#   その他の引数はそのまま scripts/build_dept_reports.py へ渡す:
#     ./build_reports.sh --no-ai              # 一手を定型文のみ（oMLX不要・高速）
#     ./build_reports.sh --fast               # 意味整合検査(judge)を切り初回生成を高速化
#     ./build_reports.sh --no-review --split  # 部門ごとの個別PDFに分割
#     ./build_reports.sh --base-date 2026-05-31
#   ※ --only/--limit（部分ビルド）はレビュー対象外のため自動で従来動作になる。
#   ※ 「PDF再作成」は生成キャッシュで数秒（同一データの再ビルドはLLMを呼び直さない）。
set -euo pipefail

# Homebrew（Apple Silicon）のパスを明示的に追加（AppleScript からは PATH が最小のため）
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

LOG="/tmp/dept_reports.log"
echo "=== $(date '+%Y/%m/%d %H:%M:%S') 部門レポート生成 開始 ===" >> "$LOG"

# GENIE_HEADLESS=1＝業務ハブ（orchestrator ジョブ）からの背景実行時は osascript を使わず
# ログのみにする（display dialog はブロッキングのためハブ実行が固まる。deploy.sh と同じ対処）。
notify() {
  if [ "${GENIE_HEADLESS:-}" = "1" ]; then echo "🔔 $1 / $2" >> "$LOG"; return; fi
  osascript -e "display notification \"$1\" with title \"部門別レポートPDF\" subtitle \"$2\"" 2>/dev/null || true
}
error_dialog() {
  echo "❌ $1" >> "$LOG"
  if [ "${GENIE_HEADLESS:-}" = "1" ]; then return; fi
  osascript -e "display dialog \"$1\" buttons {\"OK\"} with title \"エラー\" with icon caution" 2>/dev/null || true
}
trap 'error_dialog "予期せぬエラーで停止しました。詳細は $LOG を確認してください。"' ERR

# スクリプトのある Dashboard フォルダへ移動
cd "$(dirname "$0")"

# ── 引数の仕分け: --no-review はこのスクリプト用。--only/--limit はレビュー不可 ──
REVIEW=1
PORT=8768          # 既定。業務ハブ系(8502-04/8910-12/8930-31)・oMLX(8000)と重複しない番号
PORT_EXPLICIT=0
ARGS=()
prev=""
for a in "$@"; do
  case "$a" in
    --no-review) REVIEW=0; continue ;;
    # --fast: 意味整合の第2パス検査(judge)を切って初回生成を速くする（一手の生成LLM
    # 呼び出しが約半分）。PDF再作成は生成キャッシュで元々速いので judge は既定ON。
    --fast) export AI_NARRATIVE_JUDGE=0; continue ;;
    --only|--limit) REVIEW=0 ;;
    --only=*|--limit=*) REVIEW=0 ;;
    --port=*) PORT="${a#--port=}"; PORT_EXPLICIT=1 ;;
  esac
  [ "$prev" = "--port" ] && { PORT="$a"; PORT_EXPLICIT=1; }
  prev="$a"
  ARGS+=("$a")
done

# 「自分のレビューサーバか」の識別（/rebuild/status が {"state":...} を返すのは本サーバだけ。
# 業務ハブ等の他プロセスに /shutdown を送ったり、他プロセスの応答を準備完了と誤認しない）
is_review_server() {
  curl -s --max-time 2 "http://127.0.0.1:$1/rebuild/status" 2>/dev/null | grep -q '"state"'
}

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

if [ "$REVIEW" = "1" ]; then
  # ── レビュー運用（既定）: サーバ付きでバックグラウンド起動し、開いたら終了 ──
  # ポートの先客を確認:
  #  - 自分の旧レビューサーバ → 先に止める（残っているとポーリングが旧サーバの応答を
  #    拾い、新ビルドの完了前に「準備完了」を誤通知してしまう）
  #  - 他プロセス（業務ハブ等） → 手を出さず、空きポートへ自動でずらす
  if nc -z 127.0.0.1 "$PORT" 2>/dev/null; then
    if is_review_server "$PORT"; then
      curl -s -X POST -o /dev/null --max-time 3 "http://127.0.0.1:$PORT/shutdown" || true
      for _ in $(seq 1 5); do
        nc -z 127.0.0.1 "$PORT" 2>/dev/null || break
        sleep 1
      done
      echo "ℹ️  旧レビューサーバを停止しました" >> "$LOG"
    elif [ "$PORT_EXPLICIT" = "0" ]; then
      FOUND=0
      for p in $(seq $((PORT + 1)) $((PORT + 10))); do
        if ! nc -z 127.0.0.1 "$p" 2>/dev/null; then PORT="$p"; FOUND=1; break; fi
      done
      if [ "$FOUND" = "0" ]; then
        error_dialog "レビューサーバ用の空きポートが見つかりません。"
        exit 1
      fi
      echo "ℹ️  既定ポートは他プロセスが使用中のため $PORT を使用します" >> "$LOG"
    else
      error_dialog "指定ポート $PORT は別のプロセスが使用中です。"
      exit 1
    fi
  fi
  echo "🖨  レポート生成中...（完了後にレビュー画面が開きます）"
  notify "生成中…" "完了後にレビュー画面が開きます"
  nohup python scripts/build_dept_reports.py --serve "${ARGS[@]+"${ARGS[@]}"}" \
    --port "$PORT" >> "$LOG" 2>&1 &
  PID=$!

  SERVER_UP=0
  for _ in $(seq 1 900); do   # 最大30分待つ（AIありのフルビルドは数分かかる）
    if ! kill -0 "$PID" 2>/dev/null; then break; fi
    if is_review_server "$PORT"; then
      SERVER_UP=1; break
    fi
    sleep 2
  done

  if [ "$SERVER_UP" = "1" ]; then
    echo "✅ レビュー準備完了（サーバ常駐・無操作2時間で自動終了）" >> "$LOG"
    notify "レビュー画面を開きました" "修正→保存→「PDF再作成」で確定"
    exit 0
  fi
  # 30分経ってもビルド中: 待ち続けず打ち切る（異常な長時間化はエラー扱い）
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    error_dialog "30分以内に完了しなかったため中断しました。詳細は $LOG を確認してください。"
    exit 1
  fi
  # サーバが立たずにプロセスが終わった: 0=serveなし完了（--only等）/ 非0=ビルド失敗
  if wait "$PID"; then
    LATEST="$(ls -dt dept_reports/*/ 2>/dev/null | head -1 || true)"
    [ -n "$LATEST" ] && open "$LATEST" 2>/dev/null || true
    notify "PDF生成が完了しました。" "✅ ${LATEST:-dept_reports/}"
    echo "✅ 完了: ${LATEST:-dept_reports/}" >> "$LOG"
    exit 0
  fi
  kill "$PID" 2>/dev/null || true
  error_dialog "レポート生成に失敗しました。詳細は $LOG を確認してください。"
  exit 1
fi

# ── 従来運用（--no-review）: 同期で生成して出力フォルダを開く ──
echo "🖨  レポート生成中..." && notify "生成中…" "入退院バランス"
if ! python scripts/build_dept_reports.py "${ARGS[@]+"${ARGS[@]}"}" >> "$LOG" 2>&1; then
  error_dialog "レポート生成に失敗しました。詳細は $LOG を確認してください。"
  exit 1
fi
echo "✅ 生成完了" >> "$LOG"

# ── 最新の出力フォルダを開いて通知 ──
LATEST="$(ls -dt dept_reports/*/ 2>/dev/null | head -1 || true)"
[ -n "$LATEST" ] && open "$LATEST" 2>/dev/null || true
notify "PDF生成が完了しました。" "✅ ${LATEST:-dept_reports/}"
echo "✅ 完了: ${LATEST:-dept_reports/}" >> "$LOG"
