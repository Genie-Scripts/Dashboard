#!/usr/bin/env bash
# Cloudflare Pages デプロイの疎通検証。
#
# 使い方（TTY のある通常のターミナルで実行すること）:
#   bash scripts/verify_cf_deploy.sh
#
# パスワードは read -rs で受け取るため、画面にもシェル履歴にも残らない。
# 検証対象の URL は publish/ の実ファイルから生成するので、
# 配信物が増減しても自動で追随する（日本語ファイル名は percent-encode する）。

set -uo pipefail
cd "$(dirname "$0")/.."

BASE="${CF_BASE_URL:-https://hospital-dashboard-6ow.pages.dev}"
USER_NAME="${BASIC_AUTH_USER:-dashboard}"
PUBLISH_DIR="publish"

if [ ! -d "$PUBLISH_DIR" ]; then
  echo "❌ ${PUBLISH_DIR}/ がありません。先に 'make publish' を実行してください。" >&2
  exit 1
fi

if [ ! -t 0 ]; then
  echo "❌ 標準入力が TTY ではありません。" >&2
  echo "   Claude Code の '!' 経由ではなく、ターミナル.app から直接実行してください。" >&2
  echo "   （TTY が無いと空パスワードが読み込まれてしまいます）" >&2
  exit 1
fi

printf 'ユーザー名 [%s] のパスワード: ' "$USER_NAME"
read -rs PASSWORD
echo

if [ -z "$PASSWORD" ]; then
  echo "❌ パスワードが空です。中止します。" >&2
  exit 1
fi

# ── publish/ の実ファイルから検証パスを生成（percent-encode 済み） ──
# _headers は Cloudflare が設定として消費するため配信されない → 除外する。
PATHS=$(
  find "$PUBLISH_DIR" -type f ! -name '_headers' -print0 \
  | python3 -c '
import sys, urllib.parse
raw = sys.stdin.buffer.read().split(b"\0")
out = []
for item in raw:
    if not item:
        continue
    rel = item.decode("utf-8").split("/", 1)[1]
    out.append("/" + urllib.parse.quote(rel))
out.append("/")  # ルート
for p in sorted(set(out)):
    print(p)
'
)

TOTAL=0
OK=0
REDIRECTED=0
FAILED=""

# Cloudflare Pages は /foo.html → /foo、/dir/index.html → /dir/ の 308 正規化を行う。
# これは標準挙動でブラウザは透過的に追従するため、-L で追従した最終ステータスで判定する。
# 併せて、リダイレクトを跨いでも Basic 認証が維持されることの確認になる。
echo "=== 認証あり（追従後 200 が正） ==="
while IFS= read -r p; do
  [ -z "$p" ] && continue
  TOTAL=$((TOTAL + 1))
  out=$(curl -sSL -o /dev/null -w '%{http_code} %{num_redirects}' -u "${USER_NAME}:${PASSWORD}" "${BASE}${p}")
  code=${out%% *}
  hops=${out##* }
  if [ "$code" = "200" ]; then
    OK=$((OK + 1))
    [ "$hops" != "0" ] && REDIRECTED=$((REDIRECTED + 1))
  else
    FAILED="${FAILED}\n  ${code}  ${p}"
    printf '  ❌ %s  %s\n' "$code" "$p"
  fi
done <<< "$PATHS"

echo "  → ${OK}/${TOTAL} が最終的に 200（うち ${REDIRECTED} 件は 308 正規化を経由）"

echo
echo "=== セキュリティヘッダ（認証後の応答に付いているか） ==="
curl -sSI -u "${USER_NAME}:${PASSWORD}" "${BASE}/portal.html" \
  | grep -iE '^(HTTP|x-robots-tag|referrer-policy|x-frame-options|cache-control):' \
  | sed 's/^/  /'

echo
echo "=== 未認証は遮断されるか（401 が正） ==="
for p in / /portal.html /detail.html /robots.txt; do
  printf '  %-16s %s\n' "$p" "$(curl -sS -o /dev/null -w '%{http_code}' "${BASE}${p}")"
done

echo
echo "=== 中身が portal であることの確認 ==="
body=$(curl -sSL -u "${USER_NAME}:${PASSWORD}" "${BASE}/")
title=$(printf '%s' "$body" | tr -d '\n' | sed -n 's/.*<title>\(.*\)<\/title>.*/\1/p' | head -c 120)
echo "  ルート / の <title>: ${title:-（取得できず）}"

echo
echo "=== アクセス解析タグ ==="
# Google Analytics は移行時に除去済み。残っていたら除去漏れ。
if printf '%s' "$body" | grep -qiE 'googletagmanager|gtag\('; then
  echo "  ❌ Google Analytics が残存している（除去漏れ）"
else
  echo "  ✅ Google Analytics なし"
fi
# Cloudflare Web Analytics はエッジで自動注入される（コード側にタグは持たない）。
if printf '%s' "$body" | grep -qi 'cloudflareinsights.com'; then
  echo "  ✅ Cloudflare Web Analytics の beacon が注入されている"
else
  echo "  ⚠️  Cloudflare Web Analytics の beacon が見当たらない"
  echo "     → Workers & Pages → hospital-dashboard → Metrics タブで有効化し、再デプロイが必要"
fi

echo
echo "=== 短縮URLの転送先（Comedix週報に載る公開入口） ==="
# build_comedix_card.py の WEB_URL は PUBLIC_BASE_URL を参照しない独立した定数で、
# 実体は tinyurl 側の転送先設定にある。配信先を変えたときに取り残されやすいので
# ここで機械的に検出する（2026-09-03 の Cloudflare 移行で実際に取り残された）。
SHORT_URL="${COMEDIX_WEB_URL:-https://tinyurl.com/daily-dashboard-G}"
EXPECTED_TARGET="${BASE}/portal.html"
ACTUAL_TARGET=$(curl -sSI -o /dev/null -w '%{redirect_url}' "$SHORT_URL" 2>/dev/null)
echo "  ${SHORT_URL}"
if [ -z "$ACTUAL_TARGET" ]; then
  echo "  ⚠️  転送先を取得できませんでした（ネットワークかURLを確認してください）"
elif [ "$ACTUAL_TARGET" = "$EXPECTED_TARGET" ]; then
  echo "  ✅ → ${ACTUAL_TARGET}"
else
  echo "  ❌ → ${ACTUAL_TARGET}"
  echo "     期待値: ${EXPECTED_TARGET}"
  echo "     tinyurl の管理画面で転送先を変更してください（コード側の修正は不要）"
  FAILED="${FAILED}\n  短縮URLの転送先が古い: ${ACTUAL_TARGET}"
fi

echo
if [ -z "$FAILED" ]; then
  echo "✅ 全 ${TOTAL} パスが 200。疎通に問題なし。"
  exit 0
else
  echo "❌ 200 にならなかったパス:"
  printf '%b\n' "$FAILED"
  exit 1
fi
