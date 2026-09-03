// Cloudflare Pages Functions middleware — 全パスに HTTP Basic 認証を強制する。
// ユーザー名: 環境変数 BASIC_AUTH_USER（未設定時は "dashboard"）
// パスワード: 環境変数 BASIC_AUTH_PASS（未設定時はフェイルオープンさせず 503 を返す）

/**
 * UTF-8 文字列を定数時間（長さ既知の全バイトXOR蓄積）で比較する。
 * crypto.subtle.timingSafeEqual は Cloudflare Workers に存在しないため自前実装。
 */
function timingSafeEqual(a, b) {
  const encoder = new TextEncoder();
  const bytesA = encoder.encode(a);
  const bytesB = encoder.encode(b);

  // 長さが異なる場合も早期returnせず、片方をダミーとして走査してタイミングを均す
  const len = Math.max(bytesA.length, bytesB.length);
  let diff = bytesA.length ^ bytesB.length;
  for (let i = 0; i < len; i++) {
    const byteA = i < bytesA.length ? bytesA[i] : 0;
    const byteB = i < bytesB.length ? bytesB[i] : 0;
    diff |= byteA ^ byteB;
  }
  return diff === 0;
}

/**
 * "Basic base64(user:pass)" 形式の Authorization ヘッダを {user, pass} にデコードする。
 * atob の結果はバイト列(Latin1解釈)なので、Uint8Array経由でTextDecoderにかけてUTF-8として復元する。
 * パスワードにコロンが含まれてもよいよう、最初のコロンのみで分割する。
 */
function decodeBasicAuth(header) {
  const base64 = header.slice("Basic ".length).trim();
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  const decoded = new TextDecoder("utf-8").decode(bytes);
  const sepIndex = decoded.indexOf(":");
  if (sepIndex === -1) {
    return null;
  }
  return {
    user: decoded.slice(0, sepIndex),
    pass: decoded.slice(sepIndex + 1),
  };
}

function unauthorizedResponse() {
  return new Response("Unauthorized", {
    status: 401,
    headers: {
      "WWW-Authenticate": 'Basic realm="Dashboard", charset="UTF-8"',
    },
  });
}

export async function onRequest(context) {
  const { request, next, env } = context;

  try {
    const expectedUser = env.BASIC_AUTH_USER || "dashboard";
    const expectedPass = env.BASIC_AUTH_PASS;

    // パスワード未設定は認証をすり抜けさせず、明示的にサービス停止として扱う
    if (!expectedPass) {
      return new Response("Service Unavailable: BASIC_AUTH_PASS is not configured", {
        status: 503,
      });
    }

    const authHeader = request.headers.get("Authorization");
    if (!authHeader || !authHeader.startsWith("Basic ")) {
      return unauthorizedResponse();
    }

    const credentials = decodeBasicAuth(authHeader);
    if (!credentials) {
      return unauthorizedResponse();
    }

    const userOk = timingSafeEqual(credentials.user, expectedUser);
    const passOk = timingSafeEqual(credentials.pass, expectedPass);
    if (!userOk || !passOk) {
      return unauthorizedResponse();
    }

    return next();
  } catch (err) {
    // 例外の中身は漏らさず401として扱う（500で内部情報を露出させない）
    return unauthorizedResponse();
  }
}
