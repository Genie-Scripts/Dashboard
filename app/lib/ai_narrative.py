"""
ai_narrative.py — oMLX(OpenAI互換) 経由でアラートを自然言語化

alerts.py が返した「確定事実」を受け取り、ローカルLLMで
{headline, body, action} の JSON を生成してアラートに添える。

設計原則:
    - LLMには「計算」させない。与えた事実のみを翻訳する
    - 数値を文中で再引用させない（ハルシネーション封じ）
    - 出力は JSON 強制、temperature 低め
    - oMLX 未起動・モデル未取得時は無害に失敗（narrative=None）

環境:
    依存: `pip install openai`
    モデル: 環境変数 OMLX_MODEL（既定 Llama-3.1-Swallow-8B-Instruct-v0.5）
"""

from __future__ import annotations
import concurrent.futures
import fcntl
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
import zlib
from collections import Counter
from pathlib import Path
from typing import Optional

from .llm import DEFAULT_MODEL, chat_json
from .config import surgery_metric_label, UNIT_ROLE_LEVERS, WARD_BANNED_LEVER_TERMS

try:
    from app.lib import fewshot  # P3: 添削フィードバックの few-shot 注入（失敗時は完全無効）
except Exception:  # noqa: BLE001
    fewshot = None

# プロンプト一括管理 P0（開発プラン_プロンプト管理.md）: ai-apps root の prompt_kit を
# .llm の genie_llm 取り込みと同じ流儀で読む。Dashboard は単体/公開でも動く独立repoのため、
# 未配置（root 非同居）でも fail-open で従来の SYSTEM_PROMPT 定数のまま動作する。
try:
    _ROOT = Path(__file__).resolve().parents[3]  # Dashboard/app/lib → ai-apps root
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    import prompt_kit
except Exception:  # noqa: BLE001
    prompt_kit = None

_PROMPTS_TOML = Path(__file__).resolve().parents[2] / "config" / "prompts.toml"  # Dashboard/config/


def _resolve_system(key: str, fallback: str) -> str:
    """レジストリ（override→正本TOML→定数）で system プロンプトを解決する。"""
    return (prompt_kit.resolve(_PROMPTS_TOML, key, fallback=fallback)
            if prompt_kit else fallback)


logger = logging.getLogger(__name__)


# ────────────────────────────────────
# 設定（必要に応じて上書き）
# ────────────────────────────────────
# 使用モデルは llm.DEFAULT_MODEL（環境変数 OMLX_MODEL で deploy.sh と一元管理）
DEFAULT_TEMPERATURE = 0.35   # 表現の固定化を戻す（数値・禁止語ガードで安全側は担保）
DEFAULT_NUM_PREDICT = 220          # 出力トークン上限

# oMLX の同時実行数（leveling バッチの並列化・dept_report.py 側も参照）。27B/35B など
# 大型モデルを使う場合は 2 に落とす（2モデル同時ロードや prefill メモリガードを避けるため）。
NARRATE_WORKERS = int(os.environ.get("OMLX_NARRATE_WORKERS", "4"))


SYSTEM_PROMPT = """あなたは病院経営会議向けの要約ライターです。以下を厳守してください。

【厳守事項】
1. 与えられた事実のみを使い、新しい数値や事実を追加しない
2. 本文に具体的な数値を再引用しない（「上昇」「悪化」「目標未達」等の定性語を使う）
3. 推測・仮定・原因断定はしない
4. 出力は指定 JSON スキーマのみ。前置きや説明文を付けない
5. 日本語、簡潔・丁寧・事務的なトーン
6. headline は体言止めの見出し（15字前後）。事実文をそのまま写さず、要点だけを圧縮する
7. action は「何を・どの単位で」動かすかが分かる具体策にする。「検討してください」等の
   一般的な決まり文句だけで終えない。文脈に【打ち手（レバー）】が示されていれば、
   その中から状況に合うものを選んで言い換える

【出力スキーマ】
{
  "headline": "20字以内の見出し（体言止め・15字前後）",
  "body": "事実を述べる本文 60〜90字（理事会で読み上げ可能な丁寧な日本語）",
  "action": "推奨アクション 50〜80字（具体的・実行可能）"
}

【良い例 / 悪い例】（例文は写さず、与えられた事実の言葉で言い換えること）
- 見出し 悪い例:「新入院数が目標を大きく上回っている」← 事実文の丸写しで冗長
  見出し 良い例:「新入院、目標を超過」← 体言止めで要点のみ
- アクション 悪い例:「現状を分析し対策を検討してください」← どの部門にも当てはまる決まり文句
  アクション 良い例:「増加要因を確認し、受け入れ体制を維持する具体策を関係部署と共有する」← 何をするかが分かる"""


def _build_user_prompt(alert: dict) -> str:
    from .eval_rules import build_alert_context
    facts_block = "\n".join(f"- {f}" for f in alert["facts"])
    context = build_alert_context(alert)
    context_block = f"\n\n{context}" if context else ""
    # B4: 継続性（前回レポートにも在ったか）。数値は出さず定性的に伝え、継続課題では
    # action を「前回対策の効果確認・見直し」へエスカレートさせる（narrate_alerts が付与）。
    cont = alert.get("_continuity")
    cont_block = ""
    if cont and not cont.get("is_new"):
        cont_block = ("\n\n【継続性】この課題は前回のレポートでも指摘されており、継続しています。"
                      "action には前回の対策の効果確認と打ち手の見直しを含めてください（回数などの数値は書かない）。")
    elif cont and cont.get("is_new"):
        cont_block = "\n\n【継続性】この課題は今回のレポートが初出です。"
    return f"""以下の確定事実を翻訳し、JSON を1つだけ出力してください。

【アラート種別】{alert['category']}（重要度: {alert['severity']}）

【確定事実】
{facts_block}
{context_block}{cont_block}
【注意】
- headline/body/action の3キーを持つ JSON を出力すること
- 事実にない内容（具体数値、原因、人物）を補わないこと
- JSON 以外の文字（```、前置き、末尾コメント）を出力しないこと"""


def _extract_json(text: str) -> Optional[dict]:
    """LLM 出力から JSON オブジェクトを取り出す（前後のゴミに強い）"""
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0 or end <= start:
        return None
    snippet = text[start:end + 1]
    try:
        obj = json.loads(snippet)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    if not all(k in obj for k in ("headline", "body", "action")):
        return None
    return {
        "headline": str(obj["headline"]).strip(),
        "body": str(obj["body"]).strip(),
        "action": str(obj["action"]).strip(),
    }


# ── B2: 見出し品質の機械ガード（生成後チェック＋1回リトライ）──
# 汎用アラートの headline は「事実文の丸写し」「冗長」が出やすい（A/B実測）。narrate_* 系が
# 持つ _generate_checked 相当の品質床を、この経路にも軽量版で入れる。body/action の数字ガードは
# 事実由来のエコー（「直近4週」等）を誤棄却するため掛けず、見出しの規律に絞る（false-drop 回避）。
_HEADLINE_MAX = 24   # 「20字以内」の指示への許容上限（多少の超過は許すが暴走は弾く）


def _norm_ja(s: str) -> str:
    """比較用の正規化：空白・句読点・記号を除去。"""
    return re.sub(r"[\s、。，．・「」『』（）()：:；;…—\-]", "", str(s or ""))


def _headline_echoes_fact(headline: str, facts: list) -> bool:
    """見出しが事実文のほぼ丸写しか（正規化後に一方が他方へ内包される）。"""
    h = _norm_ja(headline)
    if len(h) < 6:                     # 短い体言止めは元々写しでない
        return False
    for f in facts or []:
        nf = _norm_ja(f)
        if nf and (h in nf or nf in h):
            return True
    return False


def _alert_reject_reason(obj: Optional[dict], alert: dict) -> Optional[str]:
    """{headline,body,action} の機械検査。棄却理由を返す（None=採択）。"""
    if not obj:
        return "parse"
    if not (obj.get("headline", "").strip() and obj.get("body", "").strip()
            and obj.get("action", "").strip()):
        return "empty"
    head = obj["headline"]
    if len(head) > _HEADLINE_MAX:
        return "headline_long"
    if _headline_echoes_fact(head, alert.get("facts")):
        return "headline_echo"
    return None


_ALERT_RETRY_HINT = (
    "\n\n【再出力の注意】前回の見出しが長い/事実文の写しでした。"
    "headline は体言止めで15字前後に圧縮し、action は具体的な打ち手にしてください。")


def _narrate_one(alert: dict, model: str, temperature: float) -> Optional[dict]:
    """単一アラートを LLM で翻訳。品質床（見出し）を機械検査し、外れたら温度を下げて
    1回だけ再試行する。2回とも外れてもパース成功した出力は返す（narrative を落とさない
    fail-soft）。seed はプロンプト内容の CRC32 で決定論化＝同じ事実は同じ文（月次安定）。"""
    system = _resolve_system("narrative_system", SYSTEM_PROMPT)
    user = _build_user_prompt(alert)
    base_seed = zlib.crc32((system + user).encode("utf-8")) & 0x7FFFFFFF
    best = None
    for attempt, (temp, hint) in enumerate(((temperature, ""), (RETRY_TEMPERATURE, _ALERT_RETRY_HINT))):
        try:
            content = chat_json(system=system, user=user + hint, model=model,
                                temperature=temp, max_tokens=DEFAULT_NUM_PREDICT,
                                seed=base_seed + attempt)
        except Exception as e:
            # oMLX 未起動 / openai 未インストール / モデル未取得 = インフラ起因。再試行しない。
            logger.warning(f"oMLX 呼び出し失敗 ({alert['id']}): {e}")
            return best
        obj = _extract_json(content)
        if obj is not None and best is None:
            best = obj              # パースできた最初の出力を保険に保持（品質不足でも None より良い）
        if _alert_reject_reason(obj, alert) is None:
            return obj              # 品質床クリア
    return best                     # 2回とも品質床未達なら best-effort


# ────────────────────────────────────
# エントリポイント
# ────────────────────────────────────

# ────────────────────────────────────
# 週末のびしろ（平準化アクション）の「今週の一手」
# ────────────────────────────────────
# ねらい＝週末(土日)の在院維持＝タイミングの平準化。金曜に集中した退院を平日へ
# 分散し、週末入院で空床を補充する（ベッド回転）。在院日数の延長＝月曜延伸は禁止。
# 危険な提案の機械的ブロック（プロンプト指示の二重化）。「拡大傾向」は事実文から
# 「拡大」を排した後の保険＝出たら「落ち込み拡大→在院が拡大」の意味反転圧縮の強いシグナル。
# 「前年」= leveling には前年比較の事実を渡さない＝出たら捏造（ICUで実例を観測）。
_LEVELING_BANNED = ("延伸", "早期退院", "拡大傾向", "前年")
LEVELING_ACTION_SYSTEM_PROMPT = """あなたは病院の病床管理を支援する要約ライターです。各部門の「週末（土日）に在院が落ち込む状況」への“今週の一手”を、与えられた事実だけから日本語で書きます。以下を厳守してください。

【厳守事項】
1. 与えられた事実のみを使う。新しい数値・原因・固有名を足さない。本文に数値を再引用しない（「集中」「低下」等の定性語を使う）。
2. ねらいは週末の在院維持＝タイミングの平準化。具体策は「金曜に集中している退院を月〜木へ分散」＋「週末（土日）の入院受け入れで空床を補充」。
3. 在院日数の延長（退院を月曜まで遅らせる＝月曜延伸）や早期退院の促進は提案しない（禁止）。狙いはベッド回転であって延伸ではない。
4. 診療科は患者の退院曜日と予定入院の曜日設計がレバー（床は持たない）。病棟は相乗り科の退院曜日の交通整理と週末入院の受け入れがレバー。
5. 事実に含まれる対比（「〜が」「〜ものの」等）はそのまま保つ。現状（在院の落ち込み）と傾向（改善/悪化）が食い違うときは逆接でつなぎ、順接（「〜ており」等）で並べない。
6. 事実が多いときは、最も特徴的な2〜3点に絞って本文をまとめる（全部を羅列しない）。「前回レポートとの比較」が与えられた場合は、その変化（特に改善）を前向きに織り込む。与えられていない場合は前回に言及しない。
7. 【出力例】は言い回しの参考。例文の文をそのまま写さず、与えられた事実の言葉を言い換えて本文を組み立てる。
8. 出力は指定 JSON のみ。前置き・説明・``` を付けない。簡潔・丁寧・事務的なトーン。

【出力スキーマ】
{
  "body": "週末在院の状況を述べる本文 50〜80字（数値を使わない定性的記述）",
  "action": "今週の一手 40〜70字（退院の平日分散＋週末入院補充。延伸は書かない）"
}

【出力例】（状況が変われば本文・一手も変える。文を写さないこと）
状況が「退院が週後半（特に金曜）に強く集中／週末入院での補充がほとんどない」→
{"body":"退院が週の終わりに固まり、土日の入院がごく少ないため、週末のたびに在院が大きくへこんでいます。","action":"金曜に寄った退院の一部を月〜木へ移し、予定入院を週後半に数件置いて週末の底上げを図りましょう。"}
状況が「週末もおおむね保てているが、直近4週はやや崩れてきている／落ち込みは土曜が底で日曜には持ち直す」→
{"body":"これまで週末の在院は安定してきましたが、ここ数週は土曜を底とする小さな崩れが出始めています。","action":"退院曜日の偏りを早めに整え、週末の入院受け入れを緩めないようにしましょう。"}"""


def _q_room(room, max_room):
    """のびしろの相対的な大きさ（全ユニット中の最大値比）。2026-07: 3→5段階
    （P2-a と同じ発想。粗いバケットは同一文の温床）。"""
    frac = (room / max_room) if max_room else 0
    if frac >= 0.7: return "非常に大きい"
    if frac >= 0.45: return "大きい"
    if frac >= 0.25: return "中程度"
    if frac >= 0.1: return "小さめ"
    return "小さい"


def _q_target_gap(actual, target) -> Optional[str]:
    """「実績 ÷ 目標」の達成度を定性語へ（新入院・全麻など「高いほど良い」指標に共通）。
    数値そのものは返さない（LLMプロンプト/定型文の両方から使う "事実" はこの定性語のみ）。

    2026-07: 3段階（達成/やや下回る/明確に下回る）は近い値でも同じバケットに落ち単調
    だったため5段階へ（大きく上回る/達成/わずかに下回る/やや下回る/明確に下回る）。
    """
    if not target or actual is None:
        return None
    ratio = actual / target
    if ratio >= 1.10:
        return "目標を大きく上回っている"
    if ratio >= 1.0:
        return "目標を達成している"
    if ratio >= 0.95:
        return "目標をわずかに下回っている"
    if ratio >= 0.85:
        return "目標をやや下回っている"
    return "目標を明確に下回っている"


def _gap_level_tier(level: str) -> str:
    """_q_target_gap の5状態を trend合成用の内部タグへ（上＝良い順）。"""
    if "大きく上回" in level:
        return "exceed"
    if "達成" in level:
        return "met"
    if "わずか" in level:
        return "close"
    if "やや" in level:
        return "mild"
    return "poor"


def _q_target_gap_trend(actual, target, trend: Optional[str]) -> Optional[str]:
    """「実績÷目標」の達成度(_q_target_gap) × 直近トレンド(上昇/低下/横ばい)を、逆接の
    接続まで含めた1つの事実文へ確定させる。_q_state_trend（週末在院）と同じ設計で、水準と
    傾向が食い違うケースを逆接（〜が）でつなぎ、LLMが順接で誤接続するのを防ぐ。新入院・全麻
    など「高いほど良い」指標に共通。

    trend は _ma_window_trend の戻り値（"上昇"/"低下"/"横ばい"/"—"）を想定。"横ばい"/"—"/
    None（データ不足）のときは水準のみの静的文（=_q_target_gap の従来出力）へ縮退する。
    これにより「傾向を事実として渡した場合に限り方向語を許す」連動緩和が成立する
    （プロンプトにトレンドを入れた場合だけ改善/悪化を書ける）。
    """
    level = _q_target_gap(actual, target)
    if level is None:
        return None
    direction = "up" if trend == "上昇" else "down" if trend == "低下" else "flat"
    if direction == "flat":
        return level   # 傾向なし＝従来どおり水準のみ（方向語の根拠を与えない）
    lv = _gap_level_tier(level)
    table = {
        ("exceed", "up"):   "目標を大きく上回り、直近もさらに伸びている",
        ("exceed", "down"): "目標を大きく上回っているが、直近は伸びが鈍ってきている",
        ("met",    "up"):   "目標を達成し、直近もさらに伸びている",
        # 「目標を達成」(は→を): 定型文が「{指標名}は{この文}状況です」と主題「は」の直後に
        # 埋め込むため、「〜は目標は達成〜」の「は」重複を避ける
        ("met",    "down"): "目標を達成しているが、直近は伸びが鈍ってきている",
        ("close",  "up"):   "目標をわずかに下回るが、直近は改善に向かっている",
        ("close",  "down"): "目標をわずかに下回り、直近もやや落ちている",
        ("mild",   "up"):   "目標をやや下回るが、直近は改善に向かっている",
        ("mild",   "down"): "目標をやや下回り、直近もさらに落ち込んでいる",
        ("poor",   "up"):   "目標を明確に下回るが、直近は改善に向かっている",
        ("poor",   "down"): "目標を明確に下回り、直近もさらに落ち込んでいる",
    }
    return table[(lv, direction)]


def _q_yoy(cur, prev) -> Optional[str]:
    """前年同期との比較（チャートの当年線 cur と前年線 prev の直近7点平均比・±3%は同水準）。

    「目標未達だが前年同期は上回る」等のフェアネス（褒める方向）のための事実。
    B/C チャート（28日MA・前年=364日オフセット）の系列をそのまま受け取り、両方が
    値を持つ位置だけで比較する（前年データ不足・NO_PREVYEAR_WARDS は自然に None）。"""
    pairs = [(c, p) for c, p in zip(cur or [], prev or [])
             if c is not None and p is not None]
    if len(pairs) < 5:
        return None
    tail = pairs[-7:]
    c = sum(x for x, _ in tail) / len(tail)
    p = sum(y for _, y in tail) / len(tail)
    if p <= 0:
        return None
    r = c / p
    if r >= 1.03:
        return "前年同期を上回っている"
    if r <= 0.97:
        return "前年同期を下回っている"
    return "前年同期と同水準で推移している"


def _q_ret_level(retention) -> Optional[str]:
    """週末在院維持率のレベル（good ≥.90 / mild ≥.86 / poor）。_q_state_trend と
    差分ナラティブ（前回レポート比較のバケット遷移）が同じ境界を共有する。"""
    if retention is None:
        return None
    return "good" if retention >= 0.90 else "mild" if retention >= 0.86 else "poor"


def _q_state_trend(retention, room_delta):
    """週末在院の「現状（維持率レベル）」×「傾向（4週Δの向き）」を、対比の接続を
    Python側で確定させた1つの事実文として返す。現状と傾向が食い違うケースを逆接
    （〜が）でつなぐことで、LLMが順接で誤接続するのを防ぐ。
    現状: good(≥.90)/mild(≥.86)/poor(<.86)/unknown。傾向: up(改善 rd<-0.5)/down(悪化 rd>0.5)/flat。
    """
    state = _q_ret_level(retention)
    if state is None:
        return "週末在院の維持状況は不明"
    trend = "up" if (room_delta is not None and room_delta < -0.5) else \
            "down" if (room_delta is not None and room_delta > 0.5) else "flat"
    table = {
        ("good", "up"):   "週末も在院をおおむね保てており、さらに改善している",
        ("good", "flat"): "週末も在院をおおむね保てている",
        ("good", "down"): "週末も在院をおおむね保てているが、直近4週はやや崩れてきている",
        ("mild", "up"):   "週末の在院がやや落ちるが、直近4週は改善に向かっている",
        ("mild", "flat"): "週末の在院がやや落ちる状態が続いている",
        # down側は旧「落ち込みが拡大している」を廃止。8Bが主語を落として「在院が拡大傾向」と
        # 圧縮し、悪化の事実が改善に読める意味の歪みを起こした（産婦人科・血液内科で観測）。
        # 「悪化」は主語が落ちても正負が反転しない語のみ使う。
        ("mild", "down"): "週末の在院がやや落ち、直近4週はさらに悪化している",
        ("poor", "up"):   "週末の在院が明確に落ちているが、直近4週は改善に向かっている",
        ("poor", "flat"): "週末の在院が明確に落ちる状態が続いている",
        ("poor", "down"): "週末の在院が明確に落ち、直近4週はさらに悪化している",
    }
    return table[(state, trend)]


def _q_latewk_discharge(dd) -> Optional[dict]:
    """退院の週後半（木＋金）集中度＋支配曜日（discharge.w8[月..日]＝8週の曜日別日平均）。

    旧 _q_friday は金曜シェアのみ見ており、木曜集中（消化器内科・心臓血管外科等）が
    「比較的平準」に丸められていた。メカニズム（週後半の退院が週末の空床を作る）に
    忠実な 木＋金シェア に一般化し、支配曜日名を文に埋めて科ごとの差を出す。
    しきい値は実データ較正（2026-07-02: シェア0.24〜0.48・中央値≈0.33）。
    返値: {"level": strong/mild/flat, "days": "金曜"等, "text": 事実文} or None。
    """
    if not dd or not dd.get("discharge", {}).get("w8"):
        return None
    avg = dd["discharge"]["w8"]
    tot = sum(avg)
    if tot <= 0:
        return None
    thu, fri = avg[3], avg[4]
    late = (thu + fri) / tot
    days = ("金曜" if fri >= thu * 1.3 else "木曜" if thu >= fri * 1.3 else "木曜・金曜")
    if late >= 0.40:
        return {"level": "strong", "days": days,
                "text": f"退院が週後半（特に{days}）に強く集中している"}
    if late >= 0.33:
        return {"level": "mild", "days": days,
                "text": f"退院が週後半（{days}）にやや寄っている"}
    return {"level": "flat", "days": days, "text": "退院の曜日は比較的平準"}


def _q_weekend_adm(dd) -> Optional[dict]:
    """週末入院での補充（admission.w8 の土日平均 vs 平日平均）。2026-07: 2→3段階
    （実データ較正: 比は0.0〜1.5に分布。0.55/0.25 で ある程度/限定的/ほとんどない に三分）。
    返値: {"level": some/limited/none, "text": 事実文} or None。
    """
    if not dd or not dd.get("admission", {}).get("w8"):
        return None
    a = dd["admission"]["w8"]
    wd = sum(a[0:5]) / 5 if len(a) >= 5 else 0
    we = sum(a[5:7]) / 2 if len(a) >= 7 else 0
    if wd <= 0:
        return None
    r = we / wd
    if r >= 0.55:
        return {"level": "some", "text": "週末入院での補充はある程度ある"}
    if r >= 0.25:
        return {"level": "limited", "text": "週末入院での補充は限られている"}
    return {"level": "none", "text": "週末入院での補充がほとんどない"}


def _q_census_dip(dd) -> Optional[str]:
    """在院の週末ディップの「形」（census.w8 の土日 vs 平日平均）。
    土曜底で日曜持ち直し（日曜入院がある科）／日曜にかけて深くなる（週末入院ゼロ型）／
    土日を通して低い、を書き分ける。平日平均比8%未満のディップは事実として出さない。"""
    cen = (dd or {}).get("census", {}).get("w8")
    if not cen or len(cen) < 7:
        return None
    wda = sum(cen[0:5]) / 5
    if wda <= 0:
        return None
    dsat = 1 - cen[5] / wda
    dsun = 1 - cen[6] / wda
    if max(dsat, dsun) < 0.08:
        return None
    if dsat >= 0.08 and dsun <= dsat * 0.6:
        return "落ち込みは土曜が底で、日曜にはやや持ち直す"
    if dsun >= dsat * 1.4:
        return "日曜にかけて落ち込みが深くなる"
    return "土日を通して在院が低くなる"


def _q_thin_latewk_adm(dd) -> Optional[str]:
    """平日で入院が最も薄い曜日が週末前（木/金）のとき、その曜日名を返す
    （refillレバー「予定入院を週後半へ」を科の実データの曜日名で接地する用）。
    平日平均の5割未満を「薄い」とする。週前半（月〜水）が薄くても週末在院への
    レバーにならないため返さない。"""
    a = (dd or {}).get("admission", {}).get("w8")
    if not a or len(a) < 5:
        return None
    wd_mean = sum(a[0:5]) / 5
    if wd_mean <= 0:
        return None
    i = min(range(5), key=lambda k: a[k])
    if a[i] < wd_mean * 0.5 and i in (3, 4):
        return "木曜" if i == 3 else "金曜"
    return None


def _leveling_levers(entity: str, latewk: Optional[dict], adm: Optional[dict],
                     thin: Optional[str]) -> tuple:
    """平準化レバー文の共通ビルダー。(disperse文, refill文, mode) を返す。
    mode: "disperse"（退院分散を主に）/"refill"（週末補充を主に）/"both"。
    LLMプロンプトの「レバーの軸」と _fallback_move の action の両方が使う
    （文言の乖離を防ぐ）。曜日名は科の実データから（数字を含まないためガードと両立）。"""
    days = (latewk or {}).get("days") or "金曜"
    early = "月〜木" if days == "金曜" else "月〜水" if days == "木曜" else "週前半"
    if entity == "dept":
        disperse = f"{days}に寄った退院を{early}へ分散する"
        tgt = f"週後半（{thin}など）" if thin else "週後半（木金）"
        refill = f"予定入院を{tgt}へ寄せ、週末の空床を埋める"
    else:
        disperse = f"相乗り科の{days}退院を{early}へ分散する"
        refill = "週末の入院受け入れを強化して空床を埋める"
    strong = bool(latewk) and latewk["level"] == "strong"
    weak = bool(adm) and adm["level"] in ("limited", "none")
    mode = ("disperse" if (strong and not weak)
            else "refill" if (weak and not strong) else "both")
    return disperse, refill, mode


def _build_leveling_prompt(unit: dict, entity: str, max_room: float, dd: Optional[dict],
                           peer: Optional[str] = None,
                           delta: Optional[str] = None) -> str:
    label = "診療科" if entity == "dept" else "病棟"
    facts = [
        # 現状×傾向は逆接の接続まで含めて1事実に確定（順接での誤接続を防ぐ）
        f"週末在院の現状と傾向: {_q_state_trend(unit.get('retention'), unit.get('room_delta_4w'))}",
        f"取り戻せる在院（のびしろ）の大きさ: {_q_room(unit.get('room_per_week', 0), max_room)}",
    ]
    latewk = _q_latewk_discharge(dd)
    if latewk: facts.append(f"退院の曜日: {latewk['text']}")
    adm = _q_weekend_adm(dd)
    if adm: facts.append(f"週末入院での空床補充: {adm['text']}")
    dip = _q_census_dip(dd)
    if dip: facts.append(f"在院の落ち込み方: {dip}")
    thin = _q_thin_latewk_adm(dd)
    if thin: facts.append(f"平日の入院の谷: 週末前の{thin}の予定入院が薄い")
    # 診療科軸は「同種の診療科」でなく具体的な群名を渡す（P2 環流・上記 admission と同じ）。
    if peer:
        grp = None
        if entity == "dept":
            try:
                from .eval_rules import dept_group_label
                grp = dept_group_label(unit.get("name"))
            except Exception:  # noqa: BLE001
                grp = None
        facts.append(f"{grp or '同種'}の{label}の中での週末在院の維持: {peer}に位置する")
    if delta: facts.append(f"前回レポートとの比較: {delta}")
    facts_block = "\n".join(f"- {f}" for f in facts)
    # レバーは事実に適応：週後半集中なら退院分散、補充が弱ければ週末入院強化を主にする
    disperse, refill, mode = _leveling_levers(entity, latewk, adm, thin)
    if mode == "disperse":
        lever = f"{disperse}（退院の平準化を主に）。"
    elif mode == "refill":
        lever = f"{refill}（週末入院による補充を主に）。退院曜日は比較的平準なので退院分散は強調しない。"
    else:
        lever = f"{disperse}。あわせて{refill}。"
    return f"""以下の事実から、{label}「{unit['name']}」の“今週の一手”を JSON で1つだけ出力してください。

【対象】{label}: {unit['name']}
【週末(土日)在院の状況】
{facts_block}

【書き方】
- body=週末在院の状況の要約（数値を使わない定性的記述）。
- action=今週の一手。レバーの軸: {lever}
- 月曜延伸・早期退院は禁止。JSON 以外（```・前置き・末尾コメント）を出力しない。"""


def _extract_body_action(text: str) -> Optional[dict]:
    """LLM 出力から {body, action} を取り出す"""
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "body" not in obj or "action" not in obj:
        return None
    return {"body": str(obj["body"]).strip(), "action": str(obj["action"]).strip()}


# ────────────────────────────────────
# ハルシネーション対策（プロンプト指示だけに頼らない機械的ガード）
# ────────────────────────────────────
# 事実プロンプトは定性語のみで構成しているため、出力に半角/全角数字が
# 混ざる＝具体的な数値を勝手に補った(ハルシネーション)強いシグナルとして棄却する。
_DIGIT_RE = re.compile(r"[0-9０-９]")
_MAX_TEXT_LEN = 400   # 想定外の長文出力（暴走）も棄却

# 棄却理由テレメトリ（ビルド1回分の集計）。keys: ok/parse/digit/banned/length/error。
# 「[AI] —」だけでは parse失敗/数字/禁止語のどれで落ちたか分からず、対策の効果測定が
# できないため内訳を取る（scripts/report_comment_diversity.py と build_dept_reports.py が参照）。
REJECT_STATS: Counter = Counter()

# leveling バッチ並列化(NARRATE_WORKERS)向け: REJECT_STATS/_NARR_CACHE_STATS の
# Counter += 1 は read-modify-write で不可分ではなく、進捗 print も並列だと行が
# 混ざるため、この1本のロックで両方を保護する。LLM呼び出し(chat_json)は保持区間に
# 含めない（含めると並列化の意味が消える）。
_STATS_LOCK = threading.Lock()


def reset_reject_stats() -> None:
    REJECT_STATS.clear()


# ── 共有ヘルパー: dept_reports/_state 配下ファイルのアトミック書込み ──────
# narrative_cache・alerts スナップショット等は make系(generate_html.py)と
# reports系(build_dept_reports.py)が同一ファイルを無調停で共有するため、
# 書込み途中の壊れかけJSON/空ファイルを読者に絶対見せない共通ヘルパーが要る
# （D1・spec/暦補正と学習ループ改修プラン.md §5.1）。同一ディレクトリに一時
# ファイルを書いてから os.replace で置き換える（同一ファイルシステム内なら
# POSIXでアトミック＝読者は常に「置換前の完全な旧内容」か「置換後の完全な
# 新内容」のどちらかしか観測できない）。
def _atomic_write_json(path, obj, *, indent=None) -> None:
    """obj を JSON として path へアトミックに書き込む（tmp書き→os.replace）。

    例外時は残った tmp を確実に削除する（掃除漏れでディレクトリが汚れない）。
    呼び出し元の fail-soft（try/except）に委ねるため、ここでは例外を再送出する。"""
    p = Path(path)
    tmp = p.with_name(f"{p.name}.tmp.{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _merge_cache_dicts(disk: dict, memory: dict) -> dict:
    """ディスク側 → メモリ側の順で上書きする和集合（メモリ優先）。

    並行プロセス（別ビルド・--serve 再実行等）が load 後に追記して先に保存した分
    （ディスクにのみ存在するキー）を、この和集合を通すことで保存時に保持する
    （lost-update の根治。save_narrative_cache から使う）。"""
    return {**disk, **memory}


# ── 生成キャッシュ（PDF再作成の高速化）─────────────────────────────
# 「PDF再作成」（overrides.md を直しての再ビルド）は同一データで走るため、
# プロンプト（system+user）は編集していない部門で完全一致する。生成は決定論
# （seed=crc32(system+user)）なので、同じプロンプトの出力を使い回しても再生成と
# バイト単位で同一になる＝キャッシュは意味を変えず時間だけ縮める。ディスクに置くのは
# 再作成が別プロセスで走る（build_reports.sh の --serve → subprocess）ため。
# キーにモデル名・JUDGE有無・禁止語を含めるので、それらが変われば自動で無効化する。
_NARR_CACHE: dict = {}
_NARR_CACHE_ENABLED = False
_NARR_CACHE_STATS: Counter = Counter()
_NARR_CACHE_VERSION = "v1"


def load_narrative_cache(path) -> None:
    """生成キャッシュをディスクから読み込み、以後 _generate_checked が使う（有効化）。

    build_dept_reports.py がビルド開始時に呼ぶ。--no-ai/--no-cache 時は呼ばない。
    読めない/壊れている場合は空で有効化（fail-soft・全再生成に落ちるだけ）。

    読側にロックは不要: save_narrative_cache は os.replace で書き換える（同一
    ファイルシステム内でPOSIX的にアトミック）ため、読者が書込み途中の壊れかけ
    JSONを観測するタイミングは原理的に存在しない（旧内容 or 新内容の完全な
    どちらか一方だけを見る）。"""
    global _NARR_CACHE, _NARR_CACHE_ENABLED
    _NARR_CACHE, _NARR_CACHE_ENABLED = {}, True
    _NARR_CACHE_STATS.clear()
    try:
        p = Path(path)
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _NARR_CACHE = data
    except Exception as e:  # fail-soft
        logger.warning(f"生成キャッシュを読めません（無視して全再生成）: {e}")
        _NARR_CACHE = {}


def save_narrative_cache(path) -> None:
    """生成キャッシュをディスクへ書き出す（ビルド終了時に呼ぶ）。

    make系(generate_html.py)とreports系(build_dept_reports.py)が同一ファイルを
    無調停で共有するため、素朴な「メモリ内容でそのまま上書き」は、先に load した
    側の保存内容を後から保存する側が消してしまう lost-update になる（D1）。
    ここでは同一パスの `.lock` を flock(LOCK_EX) で排他した上でディスクの現内容を
    再読込し、ディスク側→メモリ側の順で上書きした和集合（メモリ優先）を書く＝
    並行プロセスが load 後に追記した分も保存時に保持する。書込み自体は
    _atomic_write_json（tmp書き→os.replace）で行う。"""
    if not _NARR_CACHE_ENABLED:
        return
    p = Path(path)
    lock_path = p.with_name(p.name + ".lock")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w", encoding="utf-8") as lock_f:
            # LOCK_EX はブロッキング取得（保存は一瞬なので timeout は設けない）。
            # with ブロックを抜けて lock_f が close されると OS が自動解放する。
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            disk = {}
            if p.is_file():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    disk = data if isinstance(data, dict) else {}
                except Exception as e:
                    print(f"    [AI] 生成キャッシュ: ディスクの内容を読めません"
                          f"（{{}}扱いで統合）: {e}")
            merged = _merge_cache_dicts(disk, _NARR_CACHE)
            # 防御ガード: 上のマージは和集合なので、正しく実装されていれば
            # len(merged) < len(disk) は構造上起きない（将来マージ実装が壊れた
            # ときの退行検知）。発火時は書かずに警告のみに留める。
            if disk and len(merged) < len(disk):
                logger.warning(
                    "生成キャッシュ: マージ後件数がディスクを下回るため保存を中止 "
                    f"(disk={len(disk)}件 → merged={len(merged)}件)")
                return
            _atomic_write_json(p, merged)
            print(f"    [AI] 生成キャッシュ保存: {len(merged)}件（ディスク{len(disk)}件と統合）")
    except Exception as e:  # fail-soft
        logger.warning(f"生成キャッシュを書けません（無視）: {e}")


def narrative_cache_stats() -> dict:
    return dict(_NARR_CACHE_STATS)


def _cache_key(system: str, user: str, banned: tuple, allow: tuple, model: str) -> str:
    h = hashlib.sha1()
    h.update("\x1f".join([
        _NARR_CACHE_VERSION, model, "1" if JUDGE_ENABLED else "0",
        repr(tuple(banned)), repr(tuple(allow)), system, user,
    ]).encode("utf-8"))
    return h.hexdigest()


# 事実文そのものに含まれる数字入りフレーズ（_q_state_trend の「直近4週」）。
# 「事実をそのまま保て」と指示した文を書いた出力を digit で自爆棄却しないための共通アロー
# （ベースライン計測 2026-07-04: 棄却16件が全て digit で、この自己矛盾と部門名エコーが主因）。
_ALLOW_FACT_PHRASES = ("直近4週",)


def _rejection_reason(obj: Optional[dict], banned: tuple = (),
                      allow: tuple = ()) -> Optional[str]:
    """{body, action} の機械検査。棄却理由を返す（None=採択）。

    - "parse":  空/欠落（JSON抽出失敗を含む）
    - "digit":  数字（具体的な数値の再引用・捏造）を含む。allow=許容フレーズ
                （「4階A」等の対象ユニット名や事実文由来の「直近4週」＝正当なエコーは
                除去してから検査。長い順に除去し部分重複でも取りこぼさない）
    - "banned": トピック固有の禁止フレーズ（安全でない提案）
    - "length": 極端な長文（暴走出力）
    """
    if not obj:
        return "parse"
    text = "".join(str(v) for v in obj.values())
    if not text.strip():
        return "parse"
    scan = text
    for a in sorted((a for a in allow if a), key=len, reverse=True):
        scan = scan.replace(a, "")
    if _DIGIT_RE.search(scan):
        return "digit"
    if any(p in text for p in banned):
        return "banned"
    if len(text) > _MAX_TEXT_LEN:
        return "length"
    return None


def _is_hallucination_free(obj: Optional[dict], banned: tuple = (),
                           allow: tuple = ()) -> bool:
    return _rejection_reason(obj, banned=banned, allow=allow) is None


def _unit_allow(name: str) -> tuple:
    """ユニット名の数字許容セット。「9階B病棟」は「9階B」と略されても許容する。"""
    if not name:
        return ()
    short = name.replace("病棟", "")
    return (name,) if short == name else (name, short)


RETRY_TEMPERATURE = 0.2   # 再試行は堅い方へ寄せる（棄却はサンプリング起因が多い）

# D2: 総所要時間の壁時計上限（ソフト上限＝「次の呼び出しを始めない」）。実行中のコールは
# 中断しない（1コール自体は llm.WAIT_TIMEOUT_SEC + llm.TIMEOUT_SEC で有界）。
# _generate_checked の生成/judge ループが超過すると即 break→定型文フォールバックへ縮退する。
NARR_DEADLINE_SEC = float(os.environ.get("NARR_DEADLINE_SEC", "300"))

# ────────────────────────────────────
# 意味整合の第2パス検査（②-1）
# ────────────────────────────────────
# 数字/禁止語ガードでは検知できない「意味の歪み」クラス（例:「落ち込みが拡大」→
# 「在院が拡大傾向」の反転圧縮、崩れた文）を、生成後にもう1度LLMで検査する。
# fail-open 設計＝検査自体の失敗（モデル未取得等）は採択を妨げない（QA停止で全滅させない）。
# AI_NARRATIVE_JUDGE=0 で無効化。OMLX_JUDGE_MODEL で生成と別モデルの検査も可。
JUDGE_ENABLED = os.environ.get("AI_NARRATIVE_JUDGE", "1") != "0"
JUDGE_MODEL = os.environ.get("OMLX_JUDGE_MODEL", "") or None   # None=生成と同じモデル

JUDGE_SYSTEM_PROMPT = """あなたは検査員です。【資料】（事実と書き方の指示）と【要約】（body/action）を比べ、次の2点だけを検査します。
(a) 要約が資料に無い事実・数値・固有名を足していないか
(b) 資料の事実の方向（改善/悪化・達成/未達・集中/平準・上回る/下回る）を逆にしていないか
言い換え・省略・文体・トーンは問題にしません。action が資料のレバーの範囲の一般的な運用対応であることは許容します。
出力は {"ok": true} または {"ok": false, "reason": "簡潔な理由"} のJSONのみ。前置きを付けない。"""


def _judge_consistency(user_prompt: str, obj: dict, seed: int,
                       model: str, tag: str) -> bool:
    """採択候補 {body, action} が与えた事実と矛盾しないか第2パスで検査する。

    資料には生成プロンプトの事実部分のみを渡す（【書き方】以降の生成指示を含めると
    検査員がそれに引きずられて判定を誤ることを実験で確認済み）。"""
    material = user_prompt.split("【書き方】")[0].strip()
    try:
        content = chat_json(
            system=JUDGE_SYSTEM_PROMPT,
            user=(f"【資料】\n{material}\n\n【要約】\nbody: {obj['body']}\n"
                  f"action: {obj['action']}\n\nJSONで判定してください。"),
            model=model, temperature=0.0, max_tokens=120, seed=seed)
        s, e = content.find("{"), content.rfind("}")
        verdict = json.loads(content[s:e + 1]) if (s >= 0 and e > s) else {}
        return bool(verdict.get("ok", True))
    except Exception as exc:
        logger.warning(f"judge 呼び出し失敗 ({tag}): {exc}")
        with _STATS_LOCK:
            REJECT_STATS["judge_err"] += 1
        return True   # fail-open


def _generate_checked(tag: str, system: str, user: str, banned: tuple,
                      allow: tuple = (), model: str = DEFAULT_MODEL,
                      temperature: float = DEFAULT_TEMPERATURE,
                      quiet: bool = False) -> Optional[dict]:
    """chat_json → {body, action} 抽出 → 機械ガード の共通経路（全 narrate_* が使う）。

    採択時は src:"ai" を付けて返す（レポート側で fallback 定型文と区別し、計測
    スクリプトが fallback 率を算出できるようにする）。棄却/失敗時は None を返し、
    呼び出し側が定型文へフォールバックする。理由は REJECT_STATS に集計する
    （試行単位。再試行成功は "ok@retry" で区別）。

    棄却時は temp を下げて1回だけ再試行する。1回棄却=即定型文だと棄却率がそのまま
    fallback 縮退率になるため（ローカルLLMなので再試行コストは数秒）。例外
    （oMLX未起動等）はインフラ起因なので再試行しない。

    seed はプロンプト内容（system+user）の CRC32 で決定論化する（3-3 月次安定性）。
    量子化済みの事実が同じ月は同じ文になり、事実のバケットが変わった月だけ文が変わる
    （oMLX の seed 対応は実測確認済み・ユニットごとにプロンプトが違うので科間の多様性は
    保たれる）。再試行は seed+1（同じ seed では棄却された同一出力が返るだけのため）。

    D2: NARR_DEADLINE_SEC の壁時計デッドラインを持つ（ソフト上限＝「次の呼び出しを
    始めない」）。実行中の chat_json/judge 呼び出し自体は中断しない（1コールは
    llm.WAIT_TIMEOUT_SEC + llm.TIMEOUT_SEC で有界）。attempt 0 は直後に設定した
    deadline なので実質素通り＝正常経路の挙動は不変。
    """
    allow = tuple(allow) + _ALLOW_FACT_PHRASES
    # 生成キャッシュ: 同一プロンプトの採択済み出力を使い回す（PDF再作成の高速化）。
    # ヒットは再生成とバイト単位で同一（決定論seed）＝意味を変えず時間だけ縮める。
    key = _cache_key(system, user, banned, allow, model) if _NARR_CACHE_ENABLED else None
    if key is not None:
        hit = _NARR_CACHE.get(key)
        if hit and hit.get("body") and hit.get("action"):
            with _STATS_LOCK:
                # キャッシュヒットは「棄却」ではなく採択の一種（定義上は"ok"相当）。
                # REJECT_STATS は棄却理由のテレメトリなので、ヒット計上は
                # _NARR_CACHE_STATS["hit"] のみで行う（重複計上のバグ是正・D0）。
                _NARR_CACHE_STATS["hit"] += 1
                if not quiet:
                    print(f"    [AI] ✓ {tag}（キャッシュ）")
            return {"body": hit["body"], "action": hit["action"], "src": "ai"}
        with _STATS_LOCK:
            _NARR_CACHE_STATS["miss"] += 1
    base_seed = zlib.crc32((system + user).encode("utf-8")) & 0x7FFFFFFF
    result = None
    # D2: 壁時計デッドライン（ソフト上限）。attempt 0 は設定直後なので実質素通り。
    deadline = time.monotonic() + NARR_DEADLINE_SEC
    for attempt, temp in enumerate((temperature, RETRY_TEMPERATURE)):
        if time.monotonic() >= deadline:
            with _STATS_LOCK:
                REJECT_STATS["deadline"] += 1
            break
        try:
            content = chat_json(system=system, user=user, model=model,
                                temperature=temp, max_tokens=DEFAULT_NUM_PREDICT,
                                seed=base_seed + attempt)
        except Exception as e:
            logger.warning(f"oMLX 呼び出し失敗 ({tag}): {e}")
            with _STATS_LOCK:
                REJECT_STATS["error"] += 1
            break
        parsed = _extract_body_action(content)
        reason = _rejection_reason(parsed, banned=banned, allow=allow)
        if reason is None:
            # 機械ガード通過後、意味整合の第2パス（②-1）。矛盾判定なら再試行→fallback。
            if JUDGE_ENABLED:
                if time.monotonic() >= deadline:
                    with _STATS_LOCK:
                        REJECT_STATS["deadline"] += 1
                    break
                if not _judge_consistency(
                        user, parsed, base_seed + 101 + attempt, JUDGE_MODEL or model, tag):
                    with _STATS_LOCK:
                        REJECT_STATS["judge"] += 1
                    continue
            result = {**parsed, "src": "ai"}
            with _STATS_LOCK:
                REJECT_STATS["ok@retry" if attempt else "ok"] += 1
            if key is not None:
                _NARR_CACHE[key] = {"body": parsed["body"], "action": parsed["action"]}
            break
        with _STATS_LOCK:
            REJECT_STATS[reason] += 1
    if not quiet:
        with _STATS_LOCK:
            print(f"    [AI] {'✓' if result else '—'} {tag}")
    return result


def narrate_leveling_actions(weekend_leveling: dict,
                             dow_unit_detail: Optional[dict] = None,
                             top_n: int = 6,
                             model: str = DEFAULT_MODEL,
                             temperature: float = DEFAULT_TEMPERATURE,
                             quiet: bool = False,
                             peers: Optional[dict] = None,
                             deltas: Optional[dict] = None,
                             skip: Optional[set] = None) -> dict:
    """週末のびしろ payload の各エンティティについて、のびしろ上位 top_n ユニットに
    `narrative`={body, action}（or None）を付与する（破壊的更新して返す）。

    - 上位以外は narrative を持たず、フロント側がテンプレート定型文で代替表示する。
    - oMLX 未起動/モデル未取得時は全て None。例外は投げない（無害縮退）。
    - 数値はフロント（KPI/ランキング/曜日棒）が表示し、LLM は定性的な一手のみ。
    - peers: ユニット名→同種内の相対位置（上位/中位/下位）。部門レポート（診療科軸）
      だけが渡す。ポータル（html_builder）は従来どおり未指定＝peer事実なし。
    - deltas: ユニット名→前回レポート比較の事実文（①差分ナラティブ・部門レポートのみ）。
      渡さないユニットは「前回」を禁止語にする（捏造ガードの連動緩和）。
    - skip: 生成を省くユニット名（§6-1 人手オーバーライドで全文差し替え済みの部門）。
      候補選定・max_room は変えず生成だけ省く＝他ユニットのプロンプト（room相対値）を
      変えない（決定論seedの「同じ事実→同じ文」を壊さない）。
    - 生成は NARRATE_WORKERS 並列（ThreadPoolExecutor）で行う。max_room/peers/deltas/
      det はループ前に確定済みで相対値の順序依存は無く、u["narrative"] の代入先も
      ユニット固有の dict なので競合しない。1ユニットの生成失敗は他ユニットへ波及させず
      （narrative 未設定＝呼び出し側の定型文フォールバックへ無害縮退）。
    """
    if not weekend_leveling:
        return weekend_leveling
    for entity in ("dept", "ward"):
        block = weekend_leveling.get(entity) or {}
        units = block.get("units") or []
        if not units:
            continue
        max_room = max((u.get("room_per_week", 0) for u in units), default=1) or 1
        det = (dow_unit_detail or {}).get(entity, {}) or {}
        targets = sorted(units, key=lambda u: u.get("room_per_week", 0), reverse=True)[:top_n]
        candidates = [u for u in targets if u["name"] not in (skip or ())]
        if not candidates:
            continue

        def _one(u, entity=entity, det=det, max_room=max_room):
            delta = (deltas or {}).get(u["name"])
            banned = _LEVELING_BANNED if delta else _LEVELING_BANNED + ("前回",)
            # 病棟軸は紹介・地域医療連携を業務として持たない（診療科の打ち手）。平準化の
            # レバー文自体は病棟向けに書いてあるが、モデルが一般知識から補うことがあるため
            # 新入院トピックと同じ機械ガードを掛ける（棄却時は決定論の定型文へ縮退）。
            if entity == "ward":
                banned = banned + WARD_BANNED_LEVER_TERMS
            # P3: 添削 few-shot（診療科軸のみ・leveling は水準トークンを持たないため topic一致のみ）
            fs = (fewshot.examples_block("leveling", None, banned, u["name"])
                  if (fewshot and entity == "dept") else "")
            return _generate_checked(
                f"leveling {entity}:{u['name']}",
                system=_resolve_system("leveling_action_system", LEVELING_ACTION_SYSTEM_PROMPT),
                user=_build_leveling_prompt(u, entity, max_room, det.get(u["name"]),
                                            peer=(peers or {}).get(u["name"]),
                                            delta=delta) + fs,
                banned=banned, allow=_unit_allow(u["name"]),
                model=model, temperature=temperature, quiet=quiet)

        with concurrent.futures.ThreadPoolExecutor(max_workers=NARRATE_WORKERS) as ex:
            future_to_unit = {ex.submit(_one, u): u for u in candidates}
            for fut in future_to_unit:
                u = future_to_unit[fut]
                try:
                    u["narrative"] = fut.result()
                except Exception as e:
                    # 1ユニットの失敗が全体を落とさない（narrative 未設定＝定型文フォールバック）
                    logger.warning(f"leveling 生成失敗 ({entity}:{u['name']}): {e}")
    return weekend_leveling


# ────────────────────────────────────
# 新入院／全麻の「今週の一手」（病床平準化以外のトピック）
# ────────────────────────────────────
# 部門レポートの「この期間の一手」は従来、週末在院の平準化（病床管理）一辺倒だった。
# 平準化ののびしろが小さい部門では、新入院や全麻(外科系)など他の目標未達を
# 拾って一手にする（dept_report._select_action_topic がトピックを選定し、ここは
# 選ばれたトピックを事実→文章化するだけ＝計算・トピック選定はしない）。
#
# この2トピックは従来 _q_target_gap の「達成度（静的な水準）」しか事実として渡していなかった
# （傾向・病床稼働率・原因は一切与えていない）。実地テストで「紹介患者数が減少傾向にあります」
# 「病床稼働率が高く、受け入れ余地が限られています」のように与えていない傾向・状況を補う出力を
# 確認したため、"傾向"等を機械的に棄却する運用にしていた（数字チェックだけでは検知できない
# ハルシネーションの追加ガード）。
#
# 2026-07: 出力が「達成/やや下回る/明確に下回る」の3状態しかなく単調という指摘を受け、
# _q_target_gap_trend で MA トレンド（上昇/低下/横ばい）を第2軸に追加した（週末在院の
# _q_state_trend と同じレベル×傾向の確定文テーブル方式）。傾向を**実際に事実として渡した
# 場合に限り**、禁止語から "傾向" を外す連動緩和にする（_BANNED_TREND_OK）。渡していない
# 主張（稼働率・逼迫等）は引き続き機械的に棄却する。
_ADMISSION_BANNED_BASE = ("診断", "処方", "投与", "術式", "手術を追加",
                          "稼働", "占有率", "逼迫", "余地が限られ")
_ADMISSION_BANNED = _ADMISSION_BANNED_BASE + ("傾向",)          # 傾向を渡さない場合（従来どおり）
_ADMISSION_BANNED_TREND_OK = _ADMISSION_BANNED_BASE             # 傾向を事実として渡した場合

# 一般病棟（特例でない病棟）は外来・地域連携の窓口を持たないため「紹介」「地域医療連携」を
# 業務として持たない。LLM は一般知識から紹介元への働きかけを補いやすいため、プロンプト指示
# （厳守事項）に加えて生成後の禁止語でも機械的に弾く多重防衛にする
# （WARD_BANNED_LEVER_TERMS = config 側の単一の真実）。
_WARD_ADMISSION_BANNED_BASE = _ADMISSION_BANNED_BASE + WARD_BANNED_LEVER_TERMS
_WARD_ADMISSION_BANNED = _WARD_ADMISSION_BANNED_BASE + ("傾向",)
_WARD_ADMISSION_BANNED_TREND_OK = _WARD_ADMISSION_BANNED_BASE
_SURGERY_BANNED_BASE = ("診断", "処方", "投与", "術式を追加")
_SURGERY_BANNED = _SURGERY_BANNED_BASE + ("傾向",)
_SURGERY_BANNED_TREND_OK = _SURGERY_BANNED_BASE

ADMISSION_ACTION_SYSTEM_PROMPT = """あなたは病院経営を支援する要約ライターです。各部門の「新入院（週間の入院受け入れ）」の状況への“今週の一手”を、与えられた事実だけから日本語で書きます。以下を厳守してください。

【厳守事項】
1. 与えられた事実のみを使う。新しい数値・原因・固有名を足さない。本文に数値を再引用しない（定性語のみ使う）。
2. ねらいは新入院（週間の入院受け入れ件数）を目標水準へ近づけること。具体策は「地域医療連携（紹介元）への働きかけ強化」「予定入院枠の繰り上げ・前倒しの検討」など、運用面の一般的な対応にとどめる。目標未達のときの action は「新入院の患者数増に取り組みましょう」のような直接的な呼びかけで締める。
3. 特定の疾患・術式・患者を名指しした医療行為の指示はしない（臨床判断はしない）。
4. 事実に「直近は改善に向かっている／さらに落ち込んでいる」等の傾向が含まれる場合はそのまま使ってよい（渡された傾向以外を書き足さない）。事実に含まれる対比（「〜が」等）はそのまま保ち、現状と傾向が食い違うときは逆接でつなぐ（順接で誤接続しない）。
5. 出力は指定 JSON のみ。前置き・説明・``` を付けない。簡潔・丁寧・事務的なトーン。

【出力スキーマ】
{
  "body": "新入院の状況を述べる本文 50〜80字（数値を使わない定性的記述）",
  "action": "今週の一手 40〜70字（運用面の対応のみ。臨床判断・具体的な数値は書かない）"
}

【出力例】（状況が変われば本文・一手も変える。丸暗記せず状況に合わせること）
状況「目標をわずかに下回るが、直近は改善に向かっている」→
{"body":"新入院は目標をわずかに下回るものの、直近は持ち直しつつあります。","action":"紹介元への働きかけを続け、予定入院枠の調整でこの流れを確かにしましょう。"}
状況「目標を明確に下回り、直近もさらに落ち込んでいる」→
{"body":"新入院が目標を明確に下回り、足元でも弱含みが続いています。","action":"紹介受け入れの重点化と予定入院枠の前倒しで、患者数増に取り組みましょう。"}"""

# 一般病棟（特例でない＝EMERGENCY_WARDS/CRITICAL_CARE_WARDSでない病棟）向け。
# 病棟は外来・地域連携の窓口を持たないため「紹介元への働きかけ」「地域医療連携の強化」
# 「予定入院枠の前倒し」は病棟の業務として成立しない（いずれも診療科の打ち手）。
# 病棟が動かせる打ち手は UNIT_ROLE_LEVERS["ward"] の4項目（病床管理・受け入れ・
# 転棟/退院タイミング調整・担当診療科との連携）に限定する。文言はプロンプトと
# dept_report の定型文（_fallback_move_ward_admission）で乖離しないよう config を単一の
# 真実にし、ここでは列挙するだけにとどめる。
_WARD_LEVERS_ENUM = "、".join(
    f"({i + 1}) {lever}" for i, lever in enumerate(UNIT_ROLE_LEVERS["ward"])
)
# JSON例に "{" "}" を含むため f-string にはせず、レバー列挙部分だけ通常の文字列連結で挿む
# （f-string にすると出力スキーマ中の波括弧が format プレースホルダとして誤解釈される）。
WARD_ADMISSION_ACTION_SYSTEM_PROMPT = ("""あなたは病院経営を支援する要約ライターです。各病棟の「新規受け入れ（新入院・他病棟からの転入）」の状況への“今週の一手”を、与えられた事実だけから日本語で書きます。以下を厳守してください。

【厳守事項】
1. 与えられた事実のみを使う。新しい数値・原因・固有名を足さない。本文に数値を再引用しない（定性語のみ使う）。
2. ねらいは病棟の新規受け入れ（新入院・他病棟からの転入）を目標水準へ近づけること。具体策は次の4つの範囲に限定する: """
    + _WARD_LEVERS_ENUM +
    """。病棟は外来・地域連携の窓口を持たないため「紹介元への働きかけ」「地域医療連携の強化」「予定入院枠の前倒し」は提案しない（いずれも診療科の打ち手）。目標未達のときの action は「新規の受け入れを増やしましょう」のような直接的な呼びかけで締める。
3. 特定の疾患・術式・患者を名指しした医療行為の指示はしない（臨床判断はしない）。
4. 事実に「直近は改善に向かっている／さらに落ち込んでいる」等の傾向が含まれる場合はそのまま使ってよい（渡された傾向以外を書き足さない）。事実に含まれる対比（「〜が」等）はそのまま保ち、現状と傾向が食い違うときは逆接でつなぐ（順接で誤接続しない）。
5. 出力は指定 JSON のみ。前置き・説明・``` を付けない。簡潔・丁寧・事務的なトーン。

【出力スキーマ】
{
  "body": "新規受け入れの状況を述べる本文 50〜80字（数値を使わない定性的記述）",
  "action": "今週の一手 40〜70字（病床管理・受け入れ・退院/転棟のタイミング調整に限る。紹介/地域医療連携は書かない）"
}

【出力例】（状況が変われば本文・一手も変える。丸暗記せず状況に合わせること）
状況「目標をわずかに下回るが、直近は改善に向かっている」→
{"body":"新規の受け入れは目標をわずかに下回るものの、直近は持ち直しつつあります。","action":"空床の把握を細かく行い、緊急入院・転入の受け入れを続けてこの流れを確かにしましょう。"}
状況「目標を明確に下回り、直近もさらに落ち込んでいる」→
{"body":"新規の受け入れが目標を明確に下回り、足元でも弱含みが続いています。","action":"ベッドコントロールを強化し、退院・転棟のタイミング調整で受け入れ余地を広げましょう。"}""")

# 2026-08: 外科系の「今週の一手」が科によらず「手術枠の稼働確認と症例調整」に均質化していた
# 問題を是正するため、達成度で system prompt を未達版/達成版の2種に分ける。共通骨格は同じで
# 差分は狙い(AIM)・action の締め(CLOSE/CLOSE_EXTRA)・出力例(EXAMPLES)のみ。厳守事項3として
# 締め文を独立項目に分離したのが要点（従来は項目2に同居していて8Bが見落としていた）。
SURGERY_ACTION_SYSTEM_PROMPT = """あなたは病院経営を支援する要約ライターです。各診療科の「全身麻酔手術件数」の状況への“今週の一手”を、与えられた事実だけから日本語で書きます。以下を厳守してください。

【厳守事項】
1. 与えられた事実のみを使う。新しい数値・原因・固有名を足さない。本文に数値を再引用しない（定性語のみ使う）。
2. ねらいは全身麻酔手術件数を目標水準へ近づけること。具体策は、与えられた事実のうち「手術の実施曜日」「予定手術と緊急・臨時の構成」「手術室全体の稼働」に関するものを1つ選び、それに沿った運用面の対応を書く。どの科にも当てはまる一般論（枠の稼働確認と症例調整だけを繰り返す）で済ませない。
3. action の締めは必ず「全身麻酔手術の件数増に専念しましょう」のような直接的な呼びかけにする。
4. 特定の疾患・術式・患者を名指しした医療行為の指示はしない（臨床判断はしない）。
5. 事実に「直近は改善に向かっている／さらに落ち込んでいる」等の傾向が含まれる場合はそのまま使ってよい（渡された傾向以外を書き足さない）。事実に含まれる対比（「〜が」等）はそのまま保ち、現状と傾向が食い違うときは逆接でつなぐ（順接で誤接続しない）。
6. 出力は指定 JSON のみ。前置き・説明・``` を付けない。簡潔・丁寧・事務的なトーン。

【出力スキーマ】
{
  "body": "全身麻酔手術の状況を述べる本文 50〜80字（数値を使わない定性的記述）",
  "action": "今週の一手 40〜70字（運用面の対応のみ。臨床判断・具体的な数値は書かない）"
}

【出力例】（状況が変われば本文・一手も変える。丸暗記せず状況に合わせること）
状況「目標をやや下回るが、直近は改善に向かっている」「手術の実施は特定の曜日に集中している」→
{"body":"全身麻酔手術は目標をやや下回るものの、直近は上向きつつあります。","action":"実施が集中する曜日の枠内で症例を詰め、空く曜日への振り分けも検討して件数増に専念しましょう。"}
状況「目標を明確に下回り、直近もさらに落ち込んでいる」「緊急・臨時手術の割合が高い」→
{"body":"全身麻酔手術は目標を明確に下回り、足元でも弱含みが続いています。","action":"緊急対応の合間に予定症例を確保できるよう、術前準備を前倒しして件数増に専念しましょう。"}"""

# 達成版: 目標を達成/超過している科向け。件数増を迫らず、現状の進め方を肯定して支える。
# 8Bは締め文の型が抜けやすいため出力例を3件に厚くしている。
SURGERY_ACTION_SYSTEM_PROMPT_MET = """あなたは病院経営を支援する要約ライターです。各診療科の「全身麻酔手術件数」の状況への“今週の一手”を、与えられた事実だけから日本語で書きます。以下を厳守してください。

【厳守事項】
1. 与えられた事実のみを使う。新しい数値・原因・固有名を足さない。本文に数値を再引用しない（定性語のみ使う）。
2. ねらいは全身麻酔手術の実施水準を保ち、さらに積み上げること。具体策は、与えられた事実のうち「手術の実施曜日」「予定手術と緊急・臨時の構成」「手術室全体の稼働」に関するものを1つ選び、それに沿った運用面の対応を書く。どの科にも当てはまる一般論（枠の稼働確認と症例調整だけを繰り返す）で済ませない。
3. action の締めは必ず「今のペースを継続しましょう」のような、現状の進め方を肯定して支える呼びかけにする。件数増を迫る言い方（「件数増に専念」「積み増し」「増やす」等）は使わない。
4. 特定の疾患・術式・患者を名指しした医療行為の指示はしない（臨床判断はしない）。
5. 事実に「直近は改善に向かっている／さらに落ち込んでいる」等の傾向が含まれる場合はそのまま使ってよい（渡された傾向以外を書き足さない）。事実に含まれる対比（「〜が」等）はそのまま保ち、現状と傾向が食い違うときは逆接でつなぐ（順接で誤接続しない）。
6. 出力は指定 JSON のみ。前置き・説明・``` を付けない。簡潔・丁寧・事務的なトーン。

【出力スキーマ】
{
  "body": "全身麻酔手術の状況を述べる本文 50〜80字（数値を使わない定性的記述）",
  "action": "今週の一手 40〜70字（運用面の対応のみ。臨床判断・具体的な数値は書かない）"
}

【出力例】（状況が変われば本文・一手も変える。丸暗記せず状況に合わせること）
状況「目標を達成し、直近もさらに伸びている」「予定手術が中心」→
{"body":"全身麻酔手術は目標を達成し、足元でも堅調に推移しています。","action":"予定枠の消化率を保ち、当日に空いた枠も埋め戻しながら、今のペースを継続しましょう。"}
状況「目標を大きく上回っているが、直近は伸びが鈍ってきている」「手術の実施は曜日ごとに分散している」→
{"body":"全身麻酔手術は目標を大きく上回るものの、直近は伸びが鈍りつつあります。","action":"日々の症例を安定して積み上げられるよう関係部署との調整を続け、今のペースを継続しましょう。"}
状況「目標を達成しているが、直近は伸びが鈍ってきている」「手術の実施はやや特定の曜日に寄っている」→
{"body":"全身麻酔手術は目標を達成しているものの、足元では伸びがやや落ち着いています。","action":"実施が寄っている曜日の枠の使い方を確認し、無理のない範囲で今のペースを継続しましょう。"}"""


def _build_admission_prompt(unit_name: str, entity: str, state: str,
                            peer: Optional[str] = None,
                            yoy: Optional[str] = None,
                            delta: Optional[str] = None,
                            mix: Optional[str] = None,
                            holiday: Optional[str] = None) -> str:
    label = "診療科" if entity == "dept" else "病棟"
    lines = [f"- {state}"]
    # 「同種の診療科」は人手 override が例外なく「内科系/外科系診療科」へ書き換えていた
    # （P2 環流）。プロンプト側で最初から具体名を渡す。未知科は従来表現へフォールバック。
    if peer:
        try:
            from .eval_rules import dept_group_label
            grp = dept_group_label(unit_name) if entity == "dept" else None
        except Exception:  # noqa: BLE001
            grp = None
        lines.append(f"- {grp or '同種'}の診療科の中では{peer}に位置する")
    if yoy:     lines.append(f"- 前年同期との比較: {yoy}")
    if delta:   lines.append(f"- 前回レポートとの比較: {delta}")
    if mix:     lines.append(f"- 入院の内訳: {mix}")
    if holiday: lines.append(f"- 補足: {holiday}")
    facts = "\n".join(lines)
    return f"""以下の事実から、{label}「{unit_name}」の新入院に関する“今週の一手”を JSON で1つだけ出力してください。

【対象】{label}: {unit_name}
【新入院（直近7日）の状況】
{facts}

【書き方】
- body=新入院の状況の要約（数値を使わない定性的記述）。相対位置・前年同期比較・前回レポート比較が与えられていれば軽く織り込んでよい（目標未達でも前年や前回を上回るなら、その点は前向きに触れる）。
- 「入院の内訳」が与えられていれば、actionのレバー選びに反映する（緊急中心の部門に紹介・予定枠の一般論を当てない）。
- 「補足」に祝日の記載があれば、水準の低さを断定的に責めず、その影響に軽く触れてよい。
- action=今週の一手（地域医療連携の強化、予定入院枠の調整など運用面の対応）。
- JSON 以外（```・前置き・末尾コメント）を出力しない。"""


def _build_ward_admission_prompt(ward_name: str, state: str,
                                 yoy: Optional[str] = None,
                                 delta: Optional[str] = None,
                                 holiday: Optional[str] = None) -> str:
    # 一般病棟向け。peer/mix は診療科軸専用の事実（同種診療科内の相対位置・予定/緊急の
    # 内訳）で病棟軸では算出していないため渡さない。
    lines = [f"- {state}"]
    if yoy:     lines.append(f"- 前年同期との比較: {yoy}")
    if delta:   lines.append(f"- 前回レポートとの比較: {delta}")
    if holiday: lines.append(f"- 補足: {holiday}")
    facts = "\n".join(lines)
    return f"""以下の事実から、病棟「{ward_name}」の新規受け入れ（新入院・転入）に関する“今週の一手”を JSON で1つだけ出力してください。

【対象】病棟: {ward_name}
【新規受け入れ（直近7日）の状況】
{facts}

【書き方】
- body=新規受け入れの状況の要約（数値を使わない定性的記述）。前年同期比較・前回レポート比較が与えられていれば軽く織り込んでよい（目標未達でも前年や前回を上回るなら、その点は前向きに触れる）。
- 「補足」に祝日の記載があれば、水準の低さを断定的に責めず、その影響に軽く触れてよい。
- action=今週の一手（病床管理（空床の把握・ベッドコントロール）、緊急入院・転入の受け入れ、退院・転棟のタイミング調整など病棟が動かせる運用対応のみ。紹介元への働きかけ・地域医療連携・予定入院枠の前倒しは書かない）。
- JSON 以外（```・前置き・末尾コメント）を出力しない。"""


def _build_surgery_prompt(dept_name: str, state: str,
                          metric_label: str = "全身麻酔手術",
                          peer: Optional[str] = None,
                          yoy: Optional[str] = None,
                          delta: Optional[str] = None,
                          dow_shape: Optional[str] = None,
                          urgency_mix: Optional[str] = None,
                          or_load: Optional[str] = None,
                          holiday: Optional[str] = None) -> str:
    lines = [f"- {state}"]
    if peer:    lines.append(f"- 外科系の診療科の中では{peer}に位置する")
    if yoy:     lines.append(f"- 前年同期との比較: {yoy}")
    if delta:   lines.append(f"- 前回レポートとの比較: {delta}")
    if dow_shape:    lines.append(f"- {dow_shape}")
    if urgency_mix:  lines.append(f"- {urgency_mix}")
    if or_load: lines.append(f"- 手術室全体の稼働: {or_load}")
    if holiday: lines.append(f"- 補足: {holiday}")
    facts = "\n".join(lines)
    return f"""以下の事実から、診療科「{dept_name}」の{metric_label}に関する“今週の一手”を JSON で1つだけ出力してください。

【対象】診療科: {dept_name}
【{metric_label}（直近7日）の状況】
{facts}

【書き方】
- body={metric_label}の状況の要約（数値を使わない定性的記述）。相対位置・前年同期比較・前回レポート比較が与えられていれば軽く織り込んでよい（目標未達でも前年や前回を上回るなら、その点は前向きに触れる）。
- 「手術の実施曜日」「予定手術と緊急・臨時の構成」が与えられていれば、action のレバー選びに最優先で反映する（特定曜日に集中しているならその曜日の枠の使い方、緊急・臨時が多いなら予定症例の確保、予定中心なら予定枠の消化）。
- 「手術室全体の稼働」は補足として使ってよい（空きがあるなら症例の積み増し、埋まっているなら枠の調整・効率化）。
- 「補足」に祝日の記載があれば、水準の低さを断定的に責めず、その影響に軽く触れてよい。
- action=今週の一手（運用面の対応。上で選んだ事実に紐づく具体的な動きを書く）。
- JSON 以外（```・前置き・末尾コメント）を出力しない。"""


def narrate_admission_action(unit_name: str, entity: str, na, na_tgt, trend: Optional[str] = None,
                             peer: Optional[str] = None, yoy: Optional[str] = None,
                             delta: Optional[str] = None, mix: Optional[str] = None,
                             holiday: Optional[str] = None,
                             model: str = DEFAULT_MODEL,
                             temperature: float = DEFAULT_TEMPERATURE,
                             quiet: bool = False) -> Optional[dict]:
    """新入院トピックの「今週の一手」を1ユニット分生成する（oMLX未起動/棄却時は None）。

    trend: 呼び出し側（dept_report._ma_window_trend）が28日MA系列から算出した
    "上昇"/"低下"/"横ばい"/"—"/None。水準と組み合わせて _q_target_gap_trend で
    1つの事実文に確定する。方向語（改善/悪化）を渡した場合のみ禁止語の "傾向" を解除する。
    peer: 同種科内の相対位置（上位/中位/下位・診療科軸のみ）。
    yoy: 前年同期比較（_q_yoy の確定文・BチャートのCur/prevから）。
    delta: 前回レポート比較（①差分ナラティブ）。mix: 予定/緊急の内訳（①-2）。
    holiday: 連休文脈（①-4）。いずれも渡さない場合は対応語を禁止語にする（連動緩和）。
    entity == "ward"（特例でない一般病棟）は紹介・地域医療連携を業務として持たないため、
    専用プロンプト（WARD_ADMISSION_ACTION_SYSTEM_PROMPT）に差し替える。entity == "dept" の
    経路は本分岐追加前とバイト単位で不変（生成キャッシュのキー・決定論 seed を壊さないため）。
    """
    state = _q_target_gap_trend(na, na_tgt, trend)
    if state is None:
        return None
    if entity == "ward":
        banned = _WARD_ADMISSION_BANNED_TREND_OK if trend in ("上昇", "低下") else _WARD_ADMISSION_BANNED
        if yoy is None:
            banned = banned + ("前年",)
        if delta is None:
            banned = banned + ("前回",)
        if holiday is None:
            banned = banned + ("祝日", "連休")
        return _generate_checked(
            f"admission {entity}:{unit_name}",
            system=_resolve_system("ward_admission_action_system", WARD_ADMISSION_ACTION_SYSTEM_PROMPT),
            user=_build_ward_admission_prompt(unit_name, state, yoy=yoy, delta=delta, holiday=holiday),
            banned=banned, allow=_unit_allow(unit_name),
            model=model, temperature=temperature, quiet=quiet)
    banned = _ADMISSION_BANNED_TREND_OK if trend in ("上昇", "低下") else _ADMISSION_BANNED
    if yoy is None:
        # 「傾向」と同じ連動緩和: 前年比較を事実として渡していないのに「前年」を書いたら捏造
        # （ICU=NO_PREVYEAR_WARDSで「前年同期と比較するとほぼ同程度」の実例を観測）
        banned = banned + ("前年",)
    if delta is None:
        banned = banned + ("前回",)
    if holiday is None:
        banned = banned + ("祝日", "連休")
    # P3: 添削 few-shot（診療科軸のみ・病棟は事実の語彙が異なるため対象外）
    fs = (fewshot.examples_block("admission", fewshot.state_token(state), banned, unit_name)
          if (fewshot and entity == "dept") else "")
    return _generate_checked(
        f"admission {entity}:{unit_name}",
        system=_resolve_system("admission_action_system", ADMISSION_ACTION_SYSTEM_PROMPT),
        user=_build_admission_prompt(unit_name, entity, state, peer=peer, yoy=yoy,
                                     delta=delta, mix=mix, holiday=holiday) + fs,
        banned=banned, allow=_unit_allow(unit_name),
        model=model, temperature=temperature, quiet=quiet)


def narrate_surgery_action(dept_name: str, sv, surg_tgt, trend: Optional[str] = None,
                          peer: Optional[str] = None, yoy: Optional[str] = None,
                          delta: Optional[str] = None, or_load: Optional[str] = None,
                          dow_shape: Optional[str] = None, urgency_mix: Optional[str] = None,
                          holiday: Optional[str] = None,
                          model: str = DEFAULT_MODEL,
                          temperature: float = DEFAULT_TEMPERATURE,
                          quiet: bool = False) -> Optional[dict]:
    """全麻トピックの「今週の一手」を1ユニット分生成する（oMLX未起動/棄却時は None）。

    trend: dept_report._ma_window_trend（全麻週次合計の28日MA系列）の戻り値。
    narrate_admission_action と同じレベル×傾向の連動緩和を適用する。
    peer: 外科系診療科内の相対位置（上位/中位/下位）。
    yoy: 前年同期比較（_q_yoy の確定文・CチャートのCur/prevから）。
    delta: 前回レポート比較（①差分ナラティブ）。or_load: 手術室全体の稼働（①-3）。
    dow_shape: 手術実施曜日の集中度（dept_report._q_surg_dow_shape）。
    urgency_mix: 予定/緊急・臨時の構成（dept_report._q_surg_urgency_mix）。いずれも科別の
    レバー選び用の事実で、action の均質化（枠稼働確認と症例調整の一般論への収束）を防ぐ。
    holiday: 連休文脈（①-4）。前回/祝日は渡さない場合に禁止語へ（連動緩和）。

    達成度 tier（_q_target_gap → _gap_level_tier が exceed/met）のときは、件数増を迫らず
    現状の進め方を肯定する SURGERY_ACTION_SYSTEM_PROMPT_MET に切り替える（state は
    _q_target_gap_trend の縮退で tier 情報を失う場合があるため、tier は別途算出する）。
    """
    state = _q_target_gap_trend(sv, surg_tgt, trend)
    if state is None:
        return None
    # trend が横ばい/None のとき _q_target_gap_trend は水準文へ縮退し tier が state から
    # 読み取れなくなるため、達成度 tier は _q_target_gap から別途算出する。
    _lv = _q_target_gap(sv, surg_tgt)
    _met = _gap_level_tier(_lv) in ("exceed", "met") if _lv is not None else False
    # 眼科=全手術 / 他外科系=全身麻酔手術。12科はラベル不変ゆえプロンプトもバイト等価、
    # 眼科のみ「全身麻酔手術」→「全手術」に差し替える。
    label = surgery_metric_label(dept_name)
    system = (_resolve_system("surgery_action_met_system", SURGERY_ACTION_SYSTEM_PROMPT_MET)
              if _met else _resolve_system("surgery_action_system", SURGERY_ACTION_SYSTEM_PROMPT))
    if label != "全身麻酔手術":
        system = system.replace("全身麻酔手術", label)
    banned = _SURGERY_BANNED_TREND_OK if trend in ("上昇", "低下") else _SURGERY_BANNED
    if label != "全身麻酔手術":
        # 眼科=全手術ラベル差し替え時、生成文に「全身麻酔手術」の混入を防ぐ禁止語ガード。
        banned = banned + ("全身麻酔",)
    if yoy is None:
        banned = banned + ("前年",)   # narrate_admission_action と同じ連動緩和
    if delta is None:
        banned = banned + ("前回",)
    if holiday is None:
        banned = banned + ("祝日", "連休")
    if _met:
        # 達成版は件数増を迫らない方針のため、棄却されても _fallback_move_surgery の
        # 達成分岐（「現状の手術枠運用を維持しましょう。」）へ落ちるだけで安全。
        banned = banned + ("件数増", "積み増し", "増やし")
    # P3: 添削 few-shot（全麻トピックは診療科軸に固定なので entity 条件は不要）
    fs = (fewshot.examples_block("surgery", fewshot.state_token(state), banned, dept_name)
          if fewshot else "")
    return _generate_checked(
        f"surgery dept:{dept_name}",
        system=system,
        user=_build_surgery_prompt(dept_name, state, metric_label=label, peer=peer, yoy=yoy,
                                   delta=delta, dow_shape=dow_shape, urgency_mix=urgency_mix,
                                   or_load=or_load, holiday=holiday) + fs,
        banned=banned, allow=_unit_allow(dept_name),
        model=model, temperature=temperature, quiet=quiet)


# ────────────────────────────────────
# 救命救急センター系病棟（4階A/4階C）専用の「今週の一手」
# ────────────────────────────────────
# これらの病棟は緊急入院・院内転棟が中心で、他病棟のような
# 「予定入院の曜日調整」「地域医療連携（紹介元）への働きかけ」という業務前提が
# 成り立たない（需要は救急搬送・院内緊急転棟が中心で予約的にコントロールできない）。
# レバーを「転棟・転出（下り搬送）判断の迅速化による受け入れ余地の確保」
# 「週末含めた受け入れ体制（病床運用）の維持」に置き換えた専用プロンプトを使う
# （トピック選定＝ leveling/admission のどちらが大きいかの判定自体は共通ロジックを流用し、
# 文言だけをこの病棟向けに差し替える）。
# 「前年」「前回」= 救急系には前年比較・前回レポート比較の事実を渡さない＝出たら捏造
# （連動緩和の対象外・常時禁止）
_EMERGENCY_LEVELING_BANNED = ("延伸", "早期退院", "予定入院", "紹介", "前年", "前回")
_EMERGENCY_ADMISSION_BANNED_BASE = ("予定入院", "紹介", "地域医療連携",
                                   "稼働", "占有率", "逼迫", "余地が限られ", "前年", "前回")
_EMERGENCY_ADMISSION_BANNED = _EMERGENCY_ADMISSION_BANNED_BASE + ("傾向",)
_EMERGENCY_ADMISSION_BANNED_TREND_OK = _EMERGENCY_ADMISSION_BANNED_BASE

EMERGENCY_LEVELING_SYSTEM_PROMPT = """あなたは病院の病床管理を支援する要約ライターです。救命救急センター系病棟（緊急入院・院内転棟が中心で予定入院はほぼ無い病棟）の「週末（土日）に在院・稼働が落ち込む状況」への“今週の一手”を、与えられた事実だけから日本語で書きます。以下を厳守してください。

【厳守事項】
1. 与えられた事実のみを使う。新しい数値・原因・固有名を足さない。本文に数値を再引用しない（定性語のみ使う）。
2. この病棟は緊急入院・院内転棟が中心で「予定入院」「地域医療連携（紹介元）」は存在しない。それらは絶対に提案しない。
3. **最終目標は病床稼働率を高く保つこと**。救急受け入れのための空床確保はそのための手段。具体策は「ICU・HCU との連携（転入・転出のタイミング調整）で高稼働を保ちつつ救急受け入れの余地を作る」「転棟・転出（下り搬送）判断を迅速化する」「週末の病床運用・当直体制を平日と同水準に保つ」など運用面の一般的な対応にとどめる。
4. 在院日数の延長や早期退院の促進は提案しない（禁止）。
5. 出力は指定 JSON のみ。前置き・説明・``` を付けない。簡潔・丁寧・事務的なトーン。

【出力スキーマ】
{
  "body": "週末の在院・稼働の状況を述べる本文 50〜80字（数値を使わない定性的記述）",
  "action": "今週の一手 40〜70字（ICU/HCU連携での病床管理・転棟判断の迅速化・週末体制の維持。予定入院/紹介は書かない）"
}"""

EMERGENCY_ADMISSION_SYSTEM_PROMPT = """あなたは病院経営を支援する要約ライターです。救命救急センター系病棟（緊急入院・院内転棟が中心で予定入院・紹介受け入れという概念が無い病棟）の「新規受け入れ（緊急入院・転棟）」の状況への“今週の一手”を、与えられた事実だけから日本語で書きます。以下を厳守してください。

【厳守事項】
1. 与えられた事実のみを使う。新しい数値・原因・固有名を足さない。本文に数値を再引用しない（定性語のみ使う）。
2. この病棟は緊急入院・院内転棟が中心で「予定入院」「地域医療連携（紹介元）」は存在しない。それらは絶対に提案しない。
3. **最終目標は病床稼働率を高く保つこと**。救急・緊急転棟の受け入れはそのための中心手段。具体策は「ICU・HCU との連携で重症患者の転入・転出を円滑にし、高稼働を保ちつつ受け入れ余地を確保する」「後方病床（転棟・転出先）との調整」「病床運用（当直含む）の見直し」など運用面の一般的な対応にとどめる。
4. 特定の疾患・術式・患者を名指しした医療行為の指示はしない（臨床判断はしない）。
5. 事実に「直近は改善に向かっている／さらに落ち込んでいる」等の傾向が含まれる場合はそのまま使ってよい（渡された傾向以外を書き足さない）。事実に含まれる対比（「〜が」等）はそのまま保ち、現状と傾向が食い違うときは逆接でつなぐ（順接で誤接続しない）。
6. 出力は指定 JSON のみ。前置き・説明・``` を付けない。簡潔・丁寧・事務的なトーン。

【出力スキーマ】
{
  "body": "受け入れ・稼働の状況を述べる本文 50〜80字（数値を使わない定性的記述）",
  "action": "今週の一手 40〜70字（ICU/HCU連携での病床管理・後方病床調整・病床運用見直し等。予定入院/紹介/具体的な数値は書かない）"
}"""


def _build_emergency_leveling_prompt(ward_name: str, state: str) -> str:
    return f"""以下の事実から、病棟「{ward_name}」（救命救急センター系・緊急入院/院内転棟が中心）の“今週の一手”を JSON で1つだけ出力してください。

【対象】病棟: {ward_name}（救命救急センター系）
【週末(土日)在院の状況】
- {state}

【書き方】
- body=週末在院の状況の要約（数値を使わない定性的記述）。
- action=今週の一手（転棟・転出判断の迅速化、週末受け入れ体制の維持）。予定入院・紹介は書かない。
- JSON 以外（```・前置き・末尾コメント）を出力しない。"""


def _build_emergency_admission_prompt(ward_name: str, state: str) -> str:
    return f"""以下の事実から、病棟「{ward_name}」（救命救急センター系・緊急入院/院内転棟が中心）の新規受け入れに関する“今週の一手”を JSON で1つだけ出力してください。

【対象】病棟: {ward_name}（救命救急センター系）
【新規受け入れ（緊急入院・転棟、直近7日）の状況】
- {state}

【書き方】
- body=受け入れ状況の要約（数値を使わない定性的記述）。
- action=今週の一手（後方病床との調整、病床運用の見直しなど）。予定入院・紹介・具体的な数値は書かない。
- JSON 以外（```・前置き・末尾コメント）を出力しない。"""


def narrate_emergency_leveling_action(ward_name: str, retention, room_delta,
                                      model: str = DEFAULT_MODEL,
                                      temperature: float = DEFAULT_TEMPERATURE,
                                      quiet: bool = False) -> Optional[dict]:
    """救命救急系病棟(4A/4C)向け・週末在院トピックの「今週の一手」を生成する。"""
    state = _q_state_trend(retention, room_delta)
    return _generate_checked(
        f"emergency-leveling ward:{ward_name}",
        system=_resolve_system("emergency_leveling_system", EMERGENCY_LEVELING_SYSTEM_PROMPT),
        user=_build_emergency_leveling_prompt(ward_name, state),
        banned=_EMERGENCY_LEVELING_BANNED, allow=_unit_allow(ward_name),
        model=model, temperature=temperature, quiet=quiet)


def narrate_emergency_admission_action(ward_name: str, na, na_tgt, trend: Optional[str] = None,
                                       model: str = DEFAULT_MODEL,
                                       temperature: float = DEFAULT_TEMPERATURE,
                                       quiet: bool = False) -> Optional[dict]:
    """救命救急系病棟(4A/4C)向け・新規受け入れトピックの「今週の一手」を生成する。

    trend: dept_report._ma_window_trend の戻り値。narrate_admission_action と同じ
    レベル×傾向の連動緩和を適用する。
    """
    state = _q_target_gap_trend(na, na_tgt, trend)
    if state is None:
        return None
    banned = (_EMERGENCY_ADMISSION_BANNED_TREND_OK if trend in ("上昇", "低下")
             else _EMERGENCY_ADMISSION_BANNED)
    return _generate_checked(
        f"emergency-admission ward:{ward_name}",
        system=_resolve_system("emergency_admission_system", EMERGENCY_ADMISSION_SYSTEM_PROMPT),
        user=_build_emergency_admission_prompt(ward_name, state),
        banned=banned, allow=_unit_allow(ward_name),
        model=model, temperature=temperature, quiet=quiet)


# ────────────────────────────────────
# 重症ケア病棟（ICU=04B / HCU=04D）専用の「今週の一手」
# ────────────────────────────────────
# ICU/HCU は新入院患者数よりも在院患者数・病床稼働率の増が最終目標。院内急変・緊急手術後の
# 受け入れが中心で、予定入院・紹介という業務前提が無い。4A/4C（救急受け入れが主軸）とは
# 目標が異なるため専用プロンプトにする（「救急搬送の受け入れ」ではなく「重症管理での高稼働」）。
_CRITICAL_CARE_LEVELING_BANNED = ("延伸", "早期退院", "予定入院", "紹介", "地域医療連携", "前年", "前回")
_CRITICAL_CARE_ADMISSION_BANNED_BASE = ("予定入院", "紹介", "地域医療連携", "前年", "前回")
_CRITICAL_CARE_ADMISSION_BANNED = _CRITICAL_CARE_ADMISSION_BANNED_BASE + ("傾向",)
_CRITICAL_CARE_ADMISSION_BANNED_TREND_OK = _CRITICAL_CARE_ADMISSION_BANNED_BASE

CRITICAL_CARE_LEVELING_SYSTEM_PROMPT = """あなたは病院の病床管理を支援する要約ライターです。重症ケア病棟（ICU/HCU。院内急変・緊急手術後の重症患者を管理し、予定入院はほぼ無い病棟）の「週末（土日）に在院・稼働が落ち込む状況」への“今週の一手”を、与えられた事実だけから日本語で書きます。以下を厳守してください。

【厳守事項】
1. 与えられた事実のみを使う。新しい数値・原因・固有名を足さない。本文に数値を再引用しない（定性語のみ使う）。
2. この病棟は院内急変・緊急手術後の受け入れが中心で「予定入院」「地域医療連携（紹介元）」は存在しない。それらは絶対に提案しない。
3. **最終目標は在院患者数・病床稼働率を高く保つこと**。具体策は「手術部・救急・一般病棟との連携で院内急変・緊急術後の受け入れを絶やさない」「後方病床（一般病棟）への転棟タイミングを適正化して高稼働を保つ」「週末も重症管理の運用・当直体制を平日と同水準に保つ」など運用面の一般的な対応にとどめる。
4. 在院日数の延長や早期退院の促進は提案しない（禁止）。
5. 出力は指定 JSON のみ。前置き・説明・``` を付けない。簡潔・丁寧・事務的なトーン。

【出力スキーマ】
{
  "body": "週末の在院・稼働の状況を述べる本文 50〜80字（数値を使わない定性的記述）",
  "action": "今週の一手 40〜70字（院内急変・緊急術後受け入れの維持、転棟タイミングの適正化、週末体制の維持。予定入院/紹介は書かない）"
}"""

CRITICAL_CARE_ADMISSION_SYSTEM_PROMPT = """あなたは病院経営を支援する要約ライターです。重症ケア病棟（ICU/HCU。院内急変・緊急手術後の重症患者を管理し、予定入院・紹介という概念が無い病棟）の「新規受け入れ（院内急変・緊急術後・重症化）」の状況への“今週の一手”を、与えられた事実だけから日本語で書きます。以下を厳守してください。

【厳守事項】
1. 与えられた事実のみを使う。新しい数値・原因・固有名を足さない。本文に数値を再引用しない（定性語のみ使う）。
2. この病棟は院内急変・緊急手術後の受け入れが中心で「予定入院」「地域医療連携（紹介元）」は存在しない。それらは絶対に提案しない。
3. **最終目標は在院患者数・病床稼働率を高く保つこと**。受け入れ（院内急変・緊急術後）はそのための中心手段。具体策は「手術部・救急・一般病棟との連携で重症患者の受け入れを円滑にする」「後方病床の調整で高稼働を保ちつつ受け入れ余地を確保する」など運用面の一般的な対応にとどめる。
4. 特定の疾患・術式・患者を名指しした医療行為の指示はしない（臨床判断はしない）。
5. 事実に「直近は改善に向かっている／さらに落ち込んでいる」等の傾向が含まれる場合はそのまま使ってよい（渡された傾向以外を書き足さない）。事実に含まれる対比（「〜が」等）はそのまま保ち、現状と傾向が食い違うときは逆接でつなぐ（順接で誤接続しない）。
6. 出力は指定 JSON のみ。前置き・説明・``` を付けない。簡潔・丁寧・事務的なトーン。

【出力スキーマ】
{
  "body": "受け入れ・稼働の状況を述べる本文 50〜80字（数値を使わない定性的記述）",
  "action": "今週の一手 40〜70字（手術部/救急/一般病棟との連携、後方病床調整。予定入院/紹介/具体的な数値は書かない）"
}"""


def _build_critical_care_leveling_prompt(ward_name: str, state: str) -> str:
    return f"""以下の事実から、病棟「{ward_name}」（重症ケア＝ICU/HCU・院内急変/緊急術後が中心）の“今週の一手”を JSON で1つだけ出力してください。

【対象】病棟: {ward_name}（重症ケア病棟）
【週末(土日)の在院・稼働の状況】
- {state}

【書き方】
- body=週末の在院・稼働の状況の要約（数値を使わない定性的記述）。
- action=今週の一手（院内急変・緊急術後受け入れの維持、後方病床への転棟タイミングの適正化、週末体制の維持）。予定入院・紹介は書かない。
- JSON 以外（```・前置き・末尾コメント）を出力しない。"""


def _build_critical_care_admission_prompt(ward_name: str, state: str) -> str:
    return f"""以下の事実から、病棟「{ward_name}」（重症ケア＝ICU/HCU・院内急変/緊急術後が中心）の新規受け入れに関する“今週の一手”を JSON で1つだけ出力してください。

【対象】病棟: {ward_name}（重症ケア病棟）
【新規受け入れ（院内急変・緊急術後、直近7日）の状況】
- {state}

【書き方】
- body=受け入れ・稼働の状況の要約（数値を使わない定性的記述）。
- action=今週の一手（手術部/救急/一般病棟との連携、後方病床調整で高稼働と受け入れ余地を両立）。予定入院・紹介・具体的な数値は書かない。
- JSON 以外（```・前置き・末尾コメント）を出力しない。"""


def narrate_critical_care_leveling_action(ward_name: str, retention, room_delta,
                                          model: str = DEFAULT_MODEL,
                                          temperature: float = DEFAULT_TEMPERATURE,
                                          quiet: bool = False) -> Optional[dict]:
    """重症ケア病棟(ICU/HCU)向け・週末在院/稼働トピックの「今週の一手」を生成する。"""
    state = _q_state_trend(retention, room_delta)
    return _generate_checked(
        f"critical-care-leveling ward:{ward_name}",
        system=_resolve_system("critical_care_leveling_system", CRITICAL_CARE_LEVELING_SYSTEM_PROMPT),
        user=_build_critical_care_leveling_prompt(ward_name, state),
        banned=_CRITICAL_CARE_LEVELING_BANNED, allow=_unit_allow(ward_name),
        model=model, temperature=temperature, quiet=quiet)


def narrate_critical_care_admission_action(ward_name: str, na, na_tgt, trend: Optional[str] = None,
                                           model: str = DEFAULT_MODEL,
                                           temperature: float = DEFAULT_TEMPERATURE,
                                           quiet: bool = False) -> Optional[dict]:
    """重症ケア病棟(ICU/HCU)向け・新規受け入れトピックの「今週の一手」を生成する。"""
    state = _q_target_gap_trend(na, na_tgt, trend)
    if state is None:
        return None
    banned = (_CRITICAL_CARE_ADMISSION_BANNED_TREND_OK if trend in ("上昇", "低下")
             else _CRITICAL_CARE_ADMISSION_BANNED)
    return _generate_checked(
        f"critical-care-admission ward:{ward_name}",
        system=_resolve_system("critical_care_admission_system", CRITICAL_CARE_ADMISSION_SYSTEM_PROMPT),
        user=_build_critical_care_admission_prompt(ward_name, state),
        banned=banned, allow=_unit_allow(ward_name),
        model=model, temperature=temperature, quiet=quiet)


# ────────────────────────────────────
# 救急科（診療科）専用の「今週の一手」
# ────────────────────────────────────
# 救急科は外科系・内科系いずれにも属さない。北極星は救急受け入れ＝2次・3次救急の受け入れ増、
# 救急車の応需台数増、ER滞在時間の短縮。予定入院・紹介・地域連携という業務前提が無い。
_ER_LEVELING_BANNED = ("延伸", "早期退院", "予定入院", "紹介", "地域医療連携", "前年", "前回")
_ER_ADMISSION_BANNED_BASE = ("予定入院", "紹介", "地域医療連携", "前年", "前回")
_ER_ADMISSION_BANNED = _ER_ADMISSION_BANNED_BASE + ("傾向",)
_ER_ADMISSION_BANNED_TREND_OK = _ER_ADMISSION_BANNED_BASE

ER_ADMISSION_SYSTEM_PROMPT = """あなたは病院経営を支援する要約ライターです。救急科（救急外来ERと救急搬送の受け入れが中心。予定入院・紹介という概念が無い）の「救急受け入れ」の状況への“今週の一手”を、与えられた事実だけから日本語で書きます。以下を厳守してください。

【厳守事項】
1. 与えられた事実のみを使う。新しい数値・原因・固有名を足さない。本文に数値を再引用しない（定性語のみ使う）。
2. 救急科は「予定入院」「地域医療連携（紹介元）」という業務前提が無い。それらは絶対に提案しない。
3. **北極星は救急受け入れ＝2次・3次救急の受け入れ増、救急車の応需台数増、救急外来（ER）滞在時間の短縮**。具体策は「応需率を高める受け入れ判断・病床確保」「入院病棟・後方病床との連携でER滞在時間を短縮する」「救急車受け入れ台数の増加に向けた受け入れ体制の維持・強化」など運用面の一般的な対応にとどめる。
4. 特定の疾患・患者を名指しした医療行為の指示はしない（臨床判断はしない）。
5. 事実に「直近は改善に向かっている／さらに落ち込んでいる」等の傾向が含まれる場合はそのまま使ってよい（渡された傾向以外を書き足さない）。事実に含まれる対比（「〜が」等）はそのまま保ち、現状と傾向が食い違うときは逆接でつなぐ（順接で誤接続しない）。
6. 出力は指定 JSON のみ。前置き・説明・``` を付けない。簡潔・丁寧・事務的なトーン。

【出力スキーマ】
{
  "body": "救急受け入れの状況を述べる本文 50〜80字（数値を使わない定性的記述）",
  "action": "今週の一手 40〜70字（応需率向上・救急車受け入れ増・ER滞在時間短縮・後方病床連携。予定入院/紹介/具体的な数値は書かない）"
}"""

ER_LEVELING_SYSTEM_PROMPT = """あなたは病院の病床管理を支援する要約ライターです。救急科（救急外来ERと救急搬送の受け入れが中心）の「週末（土日）に受け入れ・在院が落ち込む状況」への“今週の一手”を、与えられた事実だけから日本語で書きます。以下を厳守してください。

【厳守事項】
1. 与えられた事実のみを使う。新しい数値・原因・固有名を足さない。本文に数値を再引用しない（定性語のみ使う）。
2. 救急科は「予定入院」「地域医療連携（紹介元）」という業務前提が無い。それらは絶対に提案しない。
3. ねらいは週末も救急受け入れを落とさないこと（北極星＝2次・3次救急の受け入れ、救急車応需台数、ER滞在時間の短縮）。具体策は「週末も応需体制・後方病床を保ち救急受け入れを維持する」「入院病棟との連携でER滞在時間を短縮し受け入れ余地を作る」など運用面の一般的な対応にとどめる。
4. 在院日数の延長や早期退院の促進は提案しない（禁止）。
5. 出力は指定 JSON のみ。前置き・説明・``` を付けない。簡潔・丁寧・事務的なトーン。

【出力スキーマ】
{
  "body": "週末の救急受け入れ・在院の状況を述べる本文 50〜80字（数値を使わない定性的記述）",
  "action": "今週の一手 40〜70字（週末の応需体制維持・後方病床連携でのER滞在短縮。予定入院/紹介は書かない）"
}"""


def _build_er_admission_prompt(dept_name: str, state: str) -> str:
    return f"""以下の事実から、診療科「{dept_name}」（救急外来ER・救急搬送の受け入れが中心）の救急受け入れに関する“今週の一手”を JSON で1つだけ出力してください。

【対象】診療科: {dept_name}（救急科）
【救急受け入れの状況】
- {state}

【書き方】
- body=救急受け入れの状況の要約（数値を使わない定性的記述）。
- action=今週の一手（応需率向上・救急車受け入れ増・ER滞在時間短縮・後方病床連携）。予定入院・紹介・具体的な数値は書かない。
- JSON 以外（```・前置き・末尾コメント）を出力しない。"""


def _build_er_leveling_prompt(dept_name: str, state: str) -> str:
    return f"""以下の事実から、診療科「{dept_name}」（救急外来ER・救急搬送の受け入れが中心）の“今週の一手”を JSON で1つだけ出力してください。

【対象】診療科: {dept_name}（救急科）
【週末(土日)の受け入れ・在院の状況】
- {state}

【書き方】
- body=週末の救急受け入れ・在院の状況の要約（数値を使わない定性的記述）。
- action=今週の一手（週末の応需体制維持、入院病棟との連携でER滞在短縮）。予定入院・紹介は書かない。
- JSON 以外（```・前置き・末尾コメント）を出力しない。"""


def narrate_er_admission_action(dept_name: str, na, na_tgt, trend: Optional[str] = None,
                                model: str = DEFAULT_MODEL,
                                temperature: float = DEFAULT_TEMPERATURE,
                                quiet: bool = False) -> Optional[dict]:
    """救急科向け・救急受け入れトピックの「今週の一手」を生成する。"""
    state = _q_target_gap_trend(na, na_tgt, trend)
    if state is None:
        return None
    banned = (_ER_ADMISSION_BANNED_TREND_OK if trend in ("上昇", "低下")
             else _ER_ADMISSION_BANNED)
    return _generate_checked(
        f"er-admission dept:{dept_name}",
        system=_resolve_system("er_admission_system", ER_ADMISSION_SYSTEM_PROMPT),
        user=_build_er_admission_prompt(dept_name, state),
        banned=banned, allow=_unit_allow(dept_name),
        model=model, temperature=temperature, quiet=quiet)


def narrate_er_leveling_action(dept_name: str, retention, room_delta,
                               model: str = DEFAULT_MODEL,
                               temperature: float = DEFAULT_TEMPERATURE,
                               quiet: bool = False) -> Optional[dict]:
    """救急科向け・週末受け入れ/在院トピックの「今週の一手」を生成する。"""
    state = _q_state_trend(retention, room_delta)
    return _generate_checked(
        f"er-leveling dept:{dept_name}",
        system=_resolve_system("er_leveling_system", ER_LEVELING_SYSTEM_PROMPT),
        user=_build_er_leveling_prompt(dept_name, state),
        banned=_ER_LEVELING_BANNED, allow=_unit_allow(dept_name),
        model=model, temperature=temperature, quiet=quiet)


# ────────────────────────────────────
# 病院全体サマリの「この期間の一手」
# ────────────────────────────────────
# 部門レポートの病院全体サマリ（1ページ目）は従来 move=None（コメント無し）だった。
# 診療科版・病棟版と同じ「この期間の一手」枠を流用し、病院全体の5KPI（在院/新入院/
# 全麻/粗利/週末在院維持率）を レベル×傾向 で量子化した事実から生成する。
#
# アクションのレバー（具体策）は dept_report の単一ユニット向け一手と同じ語彙
# （週末在院の平準化／新入院の連携強化／全麻の枠稼働確認）に統一する＝病院全体でも
# 「打ち手」は現場の一手の延長として理解できるようにする。粗利・通常の在院水準は
# 文脈（body）としては使うが、それ自体を打ち手の起点にはしない（診療系3レバーに
# 対応する運用アクションが無いため）。
_HOSPITAL_SUMMARY_BANNED = ("延伸", "早期退院", "診断", "処方", "投与", "術式",
                           "稼働率", "占有率", "逼迫", "余地が限られ", "前年")

HOSPITAL_SUMMARY_SYSTEM_PROMPT = """あなたは病院経営会議向けの要約ライターです。病院全体のパフォーマンスサマリへの“この期間の一手”を、与えられた事実だけから日本語で書きます。以下を厳守してください。

【厳守事項】
1. 与えられた事実のみを使う。新しい数値・原因を足さない。本文に数値を再引用しない（定性語のみ使う）。
2. 個別の診療科名・病棟名は書かない。病院全体の実績に関する評価のみを記載する。
3. 事実に含まれる対比（「〜が」「〜ものの」等）はそのまま保つ。水準と傾向が食い違うときは逆接でつなぎ、順接（「〜ており」等）で並べない。
4. 複数の指標の事実が与えられた場合、最も注視すべき1点を中心に本文をまとめ、他は軽く触れる程度にとどめる（羅列しない）。
5. action は指定されたレバー（打ち手の方向性）の範囲内で、運用面の一般的な対応として書く。特定の患者・術式を名指しした医療行為の指示はしない。
6. 出力は指定 JSON のみ。前置き・説明・``` を付けない。簡潔・丁寧・事務的な理事会向けトーン。

【出力スキーマ】
{
  "body": "病院全体の状況を述べる本文 60〜100字（数値を使わない定性的記述）",
  "action": "この期間の一手 40〜80字（指定レバーの範囲内・運用面の対応）"
}

【出力例】（レバーの軸ごとに1例。与えられた事実が変われば本文・一手も変える。丸暗記しない）
{"body":"新入院が目標をやや下回り、直近も伸び悩んでいます。全身麻酔手術も目標にわずかに届いていません。","action":"地域医療連携（紹介元）への働きかけを強め、予定入院枠の週後半への調整で新入院の患者数増に取り組みましょう。"}
{"body":"全身麻酔手術が目標を明確に下回り、直近も低調です。週末在院の維持率も目標をわずかに下回っています。","action":"手術枠の稼働状況を確認し、執刀医との症例調整で全身麻酔手術の件数増に専念しましょう。"}
{"body":"週末在院の維持率が目標を下回り、直近も崩れ気味です。新入院・全身麻酔手術も目標にやや届いていません。","action":"金曜に集中しがちな退院を平日へ分散し、週末の入院受け入れを強化して空床を埋めましょう（在院日数は延ばさない）。"}"""


def _build_hospital_summary_prompt(facts: list[str], lever: str) -> str:
    facts_block = "\n".join(f"- {f}" for f in facts)
    return f"""以下の事実から、病院全体の“この期間の一手”を JSON で1つだけ出力してください。

【病院全体の状況（直近の実績）】
{facts_block}

【書き方】
- body=病院全体の状況の要約（最も注視すべき1点を中心に、数値を使わない定性的記述）。
- action=この期間の一手。レバーの軸: {lever}
- JSON 以外（```・前置き・末尾コメント）を出力しない。"""


def narrate_hospital_summary(facts: list[str], lever: str,
                             leader: Optional[str] = None,
                             extra_banned: tuple = (),
                             has_delta: bool = False,
                             has_holiday: bool = False,
                             model: str = DEFAULT_MODEL,
                             temperature: float = DEFAULT_TEMPERATURE,
                             quiet: bool = False) -> Optional[dict]:
    """病院全体サマリの「この期間の一手」を生成する（oMLX未起動/棄却時は None）。

    facts: レベル×傾向で量子化済みの事実文のリスト（在院/新入院/全麻/粗利/週末在院維持率
    のうち算出できたもの＋牽引役）。lever: 選定されたアクショントピック（leveling/
    admission/surgery）に対応する具体策の説明（dept_report 側で確定済み・Python生成）。

    leader: 事実に含めた牽引部門名（2-3・褒める方向のみ）。病棟名の数字を許容するため
    allow に渡す。extra_banned: **leader 以外の全部門名**（渡していない部門名を書いたら
    捏造として棄却＝固有名禁止の連動緩和。「傾向」「前年」と同じパターン）。
    has_delta/has_holiday: 前回レポート比較・祝日補足を事実に含めたか（含めない場合は
    「前回」「祝日」「連休」を禁止語にする連動緩和）。
    """
    if not facts:
        return None
    banned = _HOSPITAL_SUMMARY_BANNED + tuple(extra_banned)
    if not has_delta:
        banned = banned + ("前回",)
    if not has_holiday:
        banned = banned + ("祝日", "連休")
    system = _resolve_system("hospital_summary_system", HOSPITAL_SUMMARY_SYSTEM_PROMPT)
    return _generate_checked(
        "hospital-summary",
        system=system,
        user=_build_hospital_summary_prompt(facts, lever),
        banned=banned,
        allow=_unit_allow(leader or ""),
        model=model, temperature=temperature, quiet=quiet)


# ── B4: アラート継続性の台帳（前回レポートにも在ったか＝継続 / 初出）──
# alert['id'] は run 間で安定（kpi_/dept_admission_/ward_inpatient_/momentum_）。直近の過去
# スナップショットと突き合わせて streak を数え、継続課題では action を「前回対策の効果確認・
# 見直し」へエスカレートさせる（プロンプトで定性的に指示・数値は出さない）。状態は data/ 配下
# （gitignore）に置き、opt-in（state_dir と base_date が揃ったときだけ）。全て fail-soft。

def alert_state_dir() -> Path:
    """継続台帳の既定ディレクトリ（Dashboard/data/_alert_state・gitignore 済）。"""
    return Path(__file__).resolve().parents[2] / "data" / "_alert_state"


def load_prev_alert_streaks(state_dir, base_date_str: str) -> dict:
    """base_date より前の最新スナップショットの {id: streak} を返す。無ければ空 dict。"""
    try:
        p = Path(state_dir)
        if not p.is_dir():
            return {}
        snaps = {f.stem[len("alerts_"):]: f for f in p.glob("alerts_*.json")}
        past = [d for d in snaps if d < base_date_str]   # ISO日付は文字列比較で時系列一致
        if not past:
            return {}
        data = json.loads(snaps[max(past)].read_text(encoding="utf-8"))
        streaks = data.get("streaks", {}) if isinstance(data, dict) else {}
        return streaks if isinstance(streaks, dict) else {}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"アラート継続台帳の読込失敗: {e}")
        return {}


def save_alert_snapshot(state_dir, base_date_str: str, streaks: dict) -> None:
    """今回の {id: streak} を保存（同一基準日は上書き＝再ビルドで増殖しない）。

    書込みは _atomic_write_json（tmp書き→os.replace）を使い、load_prev_alert_streaks
    （同ディレクトリを glob して読む並行/後続プロセス）に書込み途中の壊れかけJSONを
    見せない（D1）。"""
    try:
        p = Path(state_dir)
        p.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(
            p / f"alerts_{base_date_str}.json",
            {"base_date": base_date_str, "streaks": streaks},
            indent=1)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"アラート継続台帳の保存失敗: {e}")


def narrate_alerts(alerts: list[dict],
                    model: str = DEFAULT_MODEL,
                    temperature: float = DEFAULT_TEMPERATURE,
                    quiet: bool = False,
                    state_dir=None,
                    base_date=None) -> list[dict]:
    """
    各アラートに `narrative` フィールド（dict or None）を付与して返す。

    - narrative が None のアラートは、テンプレート側で title_fallback と
      facts を使って代替表示する前提。
    - LLM 未起動時は全て None になるが、例外は投げない。
    - state_dir と base_date を渡すと B4 継続性を有効化：前回レポートにも在った課題は
      streak を数えて action をエスカレートし、`continuity`（{streak,is_new}）を各要素へ付す。
    """
    if not alerts:
        return alerts

    # B4 継続性（opt-in）。base_date は Timestamp/datetime/str いずれも受ける。
    prev, bstr, new_streaks = {}, None, {}
    if state_dir is not None and base_date is not None:
        bstr = base_date.strftime("%Y-%m-%d") if hasattr(base_date, "strftime") else str(base_date)
        prev = load_prev_alert_streaks(state_dir, bstr)

    enriched = []
    for a in alerts:
        a2 = dict(a)
        if bstr is not None:
            aid = a.get("id", "")
            streak = prev.get(aid, 0) + 1
            new_streaks[aid] = streak
            a2["_continuity"] = {"streak": streak, "is_new": prev.get(aid, 0) == 0}
        n = _narrate_one(a2, model=model, temperature=temperature)
        a2["narrative"] = n
        if "_continuity" in a2:
            a2["continuity"] = a2.pop("_continuity")   # 内部→公開（テンプレの継続バッジ等に使える）
        enriched.append(a2)
        if not quiet:
            print(f"    [AI] {'✓' if n else '—'} {a['id']}")

    if bstr is not None:
        save_alert_snapshot(state_dir, bstr, new_streaks)
    return enriched
