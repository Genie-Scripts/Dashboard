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
import json
import logging
import re
from typing import Optional

from .llm import DEFAULT_MODEL, chat_json

logger = logging.getLogger(__name__)


# ────────────────────────────────────
# 設定（必要に応じて上書き）
# ────────────────────────────────────
# 使用モデルは llm.DEFAULT_MODEL（環境変数 OMLX_MODEL で deploy.sh と一元管理）
DEFAULT_TEMPERATURE = 0.2
DEFAULT_NUM_PREDICT = 220          # 出力トークン上限


SYSTEM_PROMPT = """あなたは病院経営会議向けの要約ライターです。以下を厳守してください。

【厳守事項】
1. 与えられた事実のみを使い、新しい数値や事実を追加しない
2. 本文に具体的な数値を再引用しない（「上昇」「悪化」「目標未達」等の定性語を使う）
3. 推測・仮定・原因断定はしない
4. 出力は指定 JSON スキーマのみ。前置きや説明文を付けない
5. 日本語、簡潔・丁寧・事務的なトーン

【出力スキーマ】
{
  "headline": "20字以内の見出し（体言止め可）",
  "body": "事実を述べる本文 60〜90字（理事会で読み上げ可能な丁寧な日本語）",
  "action": "推奨アクション 50〜80字（具体的・実行可能）"
}"""


def _build_user_prompt(alert: dict) -> str:
    from .eval_rules import build_alert_context
    facts_block = "\n".join(f"- {f}" for f in alert["facts"])
    context = build_alert_context(alert)
    context_block = f"\n\n{context}" if context else ""
    return f"""以下の確定事実を翻訳し、JSON を1つだけ出力してください。

【アラート種別】{alert['category']}（重要度: {alert['severity']}）

【確定事実】
{facts_block}
{context_block}
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


def _narrate_one(alert: dict, model: str, temperature: float) -> Optional[dict]:
    """単一アラートを LLM で翻訳"""
    try:
        content = chat_json(
            system=SYSTEM_PROMPT,
            user=_build_user_prompt(alert),
            model=model,
            temperature=temperature,
            max_tokens=DEFAULT_NUM_PREDICT,
        )
    except Exception as e:
        # oMLX 未起動 / openai 未インストール / モデル未取得 すべてここで無害に縮退
        logger.warning(f"oMLX 呼び出し失敗 ({alert['id']}): {e}")
        return None

    return _extract_json(content)


# ────────────────────────────────────
# エントリポイント
# ────────────────────────────────────

# ────────────────────────────────────
# 週末のびしろ（平準化アクション）の「今週の一手」
# ────────────────────────────────────
# ねらい＝週末(土日)の在院維持＝タイミングの平準化。金曜に集中した退院を平日へ
# 分散し、週末入院で空床を補充する（ベッド回転）。在院日数の延長＝月曜延伸は禁止。
_LEVELING_BANNED = ("延伸", "早期退院")   # 危険な提案の機械的ブロック（プロンプト指示の二重化）
LEVELING_ACTION_SYSTEM_PROMPT = """あなたは病院の病床管理を支援する要約ライターです。各部門の「週末（土日）に在院が落ち込む状況」への“今週の一手”を、与えられた事実だけから日本語で書きます。以下を厳守してください。

【厳守事項】
1. 与えられた事実のみを使う。新しい数値・原因・固有名を足さない。本文に数値を再引用しない（「集中」「低下」等の定性語を使う）。
2. ねらいは週末の在院維持＝タイミングの平準化。具体策は「金曜に集中している退院を月〜木へ分散」＋「週末（土日）の入院受け入れで空床を補充」。
3. 在院日数の延長（退院を月曜まで遅らせる＝月曜延伸）や早期退院の促進は提案しない（禁止）。狙いはベッド回転であって延伸ではない。
4. 診療科は患者の退院曜日と予定入院の曜日設計がレバー（床は持たない）。病棟は相乗り科の退院曜日の交通整理と週末入院の受け入れがレバー。
5. 事実に含まれる対比（「〜が」「〜ものの」等）はそのまま保つ。現状（在院の落ち込み）と傾向（改善/悪化）が食い違うときは逆接でつなぎ、順接（「〜ており」等）で並べない。
6. 出力は指定 JSON のみ。前置き・説明・``` を付けない。簡潔・丁寧・事務的なトーン。

【出力スキーマ】
{
  "body": "週末在院の状況を述べる本文 50〜80字（数値を使わない定性的記述）",
  "action": "今週の一手 40〜70字（金曜退院の平日分散＋週末入院補充。延伸は書かない）"
}"""


def _q_room(room, max_room):
    frac = (room / max_room) if max_room else 0
    if frac >= 0.66: return "大きい"
    if frac >= 0.33: return "中程度"
    return "小さい"


def _q_target_gap(actual, target) -> Optional[str]:
    """「実績 ÷ 目標」の達成度を定性語へ（新入院・全麻など「高いほど良い」指標に共通）。
    数値そのものは返さない（LLMプロンプト/定型文の両方から使う "事実" はこの定性語のみ）。
    """
    if not target or actual is None:
        return None
    ratio = actual / target
    if ratio >= 1.0:
        return "目標を達成している"
    if ratio >= 0.85:
        return "目標をやや下回っている"
    return "目標を明確に下回っている"


def _q_state_trend(retention, room_delta):
    """週末在院の「現状（維持率レベル）」×「傾向（4週Δの向き）」を、対比の接続を
    Python側で確定させた1つの事実文として返す。現状と傾向が食い違うケースを逆接
    （〜が）でつなぐことで、LLMが順接で誤接続するのを防ぐ。
    現状: good(≥.90)/mild(≥.86)/poor(<.86)/unknown。傾向: up(改善 rd<-0.5)/down(悪化 rd>0.5)/flat。
    """
    if retention is None:
        return "週末在院の維持状況は不明"
    state = "good" if retention >= 0.90 else "mild" if retention >= 0.86 else "poor"
    trend = "up" if (room_delta is not None and room_delta < -0.5) else \
            "down" if (room_delta is not None and room_delta > 0.5) else "flat"
    table = {
        ("good", "up"):   "週末も在院をおおむね保てており、さらに改善している",
        ("good", "flat"): "週末も在院をおおむね保てている",
        ("good", "down"): "週末も在院をおおむね保てているが、直近4週はやや崩れてきている",
        ("mild", "up"):   "週末の在院がやや落ちるが、直近4週は改善に向かっている",
        ("mild", "flat"): "週末の在院がやや落ちる状態が続いている",
        ("mild", "down"): "週末の在院がやや落ち、直近4週でさらに落ち込みが拡大している",
        ("poor", "up"):   "週末の在院が明確に落ちているが、直近4週は改善に向かっている",
        ("poor", "flat"): "週末の在院が明確に落ちる状態が続いている",
        ("poor", "down"): "週末の在院が明確に落ち、直近4週でさらに落ち込みが拡大している",
    }
    return table[(state, trend)]


def _q_friday(dd):
    """退院の金曜集中度（dow_unit_detail の discharge.w8[月..日]＝8週平均）"""
    if not dd or not dd.get("discharge", {}).get("w8"):
        return None
    avg = dd["discharge"]["w8"]
    tot = sum(avg)
    if tot <= 0: return None
    share = avg[4] / tot
    if share >= 0.22: return "金曜への退院集中が強い"
    if share >= 0.16: return "金曜にやや集中している"
    return "曜日は比較的平準"


def _q_weekend_adm(dd):
    """週末入院での補充の有無（admission.w8 の土日 vs 平日＝8週平均）"""
    if not dd or not dd.get("admission", {}).get("w8"):
        return None
    a = dd["admission"]["w8"]
    wd = sum(a[0:5]) / 5 if len(a) >= 5 else 0
    we = sum(a[5:7]) / 2 if len(a) >= 7 else 0
    if wd <= 0: return None
    return "週末入院での補充はある程度ある" if we >= wd * 0.6 else "週末入院での補充が乏しい"


def _build_leveling_prompt(unit: dict, entity: str, max_room: float, dd: Optional[dict]) -> str:
    label = "診療科" if entity == "dept" else "病棟"
    facts = [
        # 現状×傾向は逆接の接続まで含めて1事実に確定（順接での誤接続を防ぐ）
        f"週末在院の現状と傾向: {_q_state_trend(unit.get('retention'), unit.get('room_delta_4w'))}",
        f"取り戻せる在院（のびしろ）の大きさ: {_q_room(unit.get('room_per_week', 0), max_room)}",
    ]
    fri = _q_friday(dd)
    if fri: facts.append(f"退院の曜日: {fri}")
    adm = _q_weekend_adm(dd)
    if adm: facts.append(f"週末入院での空床補充: {adm}")
    facts_block = "\n".join(f"- {f}" for f in facts)
    # レバーは事実に適応：金曜集中なら退院分散、補充が乏しければ週末入院強化を主にする
    fri_strong = bool(fri) and "強い" in fri
    adm_weak = bool(adm) and "乏しい" in adm
    disperse = ("金曜に集中した退院を月〜木へ分散する"
                if entity == "dept" else "相乗り科の金曜退院を平日へ分散する")
    refill = ("予定入院を週後半（木金）へ寄せ、週末の空床を埋める"
              if entity == "dept" else "週末の入院受け入れを強化して空床を埋める")
    if fri_strong and not adm_weak:
        lever = f"{disperse}（退院の平準化を主に）。"
    elif adm_weak and not fri_strong:
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


def _is_hallucination_free(obj: Optional[dict], banned: tuple = ()) -> bool:
    """{body, action} が安全か機械的に検査する。

    1. 空/欠落は不可（呼び出し側で None 扱い）
    2. 数字（具体的な数値の再引用・捏造）を含んだら不可
    3. トピック固有の禁止フレーズ（安全でない提案）を含んだら不可
    4. 極端な長文（暴走出力）は不可
    """
    if not obj:
        return False
    text = "".join(str(v) for v in obj.values())
    if not text.strip():
        return False
    if _DIGIT_RE.search(text):
        return False
    if any(p in text for p in banned):
        return False
    if len(text) > _MAX_TEXT_LEN:
        return False
    return True


def narrate_leveling_actions(weekend_leveling: dict,
                             dow_unit_detail: Optional[dict] = None,
                             top_n: int = 6,
                             model: str = DEFAULT_MODEL,
                             temperature: float = DEFAULT_TEMPERATURE,
                             quiet: bool = False) -> dict:
    """週末のびしろ payload の各エンティティについて、のびしろ上位 top_n ユニットに
    `narrative`={body, action}（or None）を付与する（破壊的更新して返す）。

    - 上位以外は narrative を持たず、フロント側がテンプレート定型文で代替表示する。
    - oMLX 未起動/モデル未取得時は全て None。例外は投げない（無害縮退）。
    - 数値はフロント（KPI/ランキング/曜日棒）が表示し、LLM は定性的な一手のみ。
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
        for u in targets:
            try:
                content = chat_json(
                    system=LEVELING_ACTION_SYSTEM_PROMPT,
                    user=_build_leveling_prompt(u, entity, max_room, det.get(u["name"])),
                    model=model, temperature=temperature, max_tokens=DEFAULT_NUM_PREDICT,
                )
                parsed = _extract_body_action(content)
                u["narrative"] = (parsed if _is_hallucination_free(parsed, banned=_LEVELING_BANNED)
                                  else None)
            except Exception as e:
                logger.warning(f"oMLX 呼び出し失敗 (leveling {entity}:{u['name']}): {e}")
                u["narrative"] = None
            if not quiet:
                print(f"    [AI] {'✓' if u.get('narrative') else '—'} leveling {entity}:{u['name']}")
    return weekend_leveling


# ────────────────────────────────────
# 新入院／全麻の「今週の一手」（病床平準化以外のトピック）
# ────────────────────────────────────
# 部門レポートの「この期間の一手」は従来、週末在院の平準化（病床管理）一辺倒だった。
# 平準化ののびしろが小さい部門では、新入院や全麻(外科系)など他の目標未達を
# 拾って一手にする（dept_report._select_action_topic がトピックを選定し、ここは
# 選ばれたトピックを事実→文章化するだけ＝計算・トピック選定はしない）。
#
# この2トピックは _q_target_gap の「達成度（静的な水準）」しか事実として渡さない
# ＝傾向（増加/減少/改善/悪化）・病床稼働率・原因は一切与えていない。実地テストで
# 「紹介患者数が減少傾向にあります」「病床稼働率が高く、受け入れ余地が限られています」
# のように与えていない傾向・状況を補う出力を確認したため、これらを事実にない主張
# として機械的に棄却する（数字チェックだけでは検知できないハルシネーションの追加ガード）。
_ADMISSION_BANNED = ("診断", "処方", "投与", "術式", "手術を追加", "傾向",
                    "稼働", "占有率", "逼迫", "余地が限られ")
_SURGERY_BANNED = ("診断", "処方", "投与", "術式を追加", "傾向")

ADMISSION_ACTION_SYSTEM_PROMPT = """あなたは病院経営を支援する要約ライターです。各部門の「新入院（週間の入院受け入れ）」の状況への“今週の一手”を、与えられた事実だけから日本語で書きます。以下を厳守してください。

【厳守事項】
1. 与えられた事実のみを使う。新しい数値・原因・固有名を足さない。本文に数値を再引用しない（定性語のみ使う）。
2. ねらいは新入院（週間の入院受け入れ件数）を目標水準へ近づけること。具体策は「地域医療連携（紹介元）への働きかけ強化」「予定入院枠の繰り上げ・前倒しの検討」など、運用面の一般的な対応にとどめる。
3. 特定の疾患・術式・患者を名指しした医療行為の指示はしない（臨床判断はしない）。
4. 出力は指定 JSON のみ。前置き・説明・``` を付けない。簡潔・丁寧・事務的なトーン。

【出力スキーマ】
{
  "body": "新入院の状況を述べる本文 50〜80字（数値を使わない定性的記述）",
  "action": "今週の一手 40〜70字（運用面の対応のみ。臨床判断・具体的な数値は書かない）"
}"""

SURGERY_ACTION_SYSTEM_PROMPT = """あなたは病院経営を支援する要約ライターです。各診療科の「全身麻酔手術件数」の状況への“今週の一手”を、与えられた事実だけから日本語で書きます。以下を厳守してください。

【厳守事項】
1. 与えられた事実のみを使う。新しい数値・原因・固有名を足さない。本文に数値を再引用しない（定性語のみ使う）。
2. ねらいは全身麻酔手術件数を目標水準へ近づけること。具体策は「手術枠の稼働状況の確認」「執刀医との症例調整」など、運用面の一般的な対応にとどめる。
3. 特定の疾患・術式・患者を名指しした医療行為の指示はしない（臨床判断はしない）。
4. 出力は指定 JSON のみ。前置き・説明・``` を付けない。簡潔・丁寧・事務的なトーン。

【出力スキーマ】
{
  "body": "全身麻酔手術の状況を述べる本文 50〜80字（数値を使わない定性的記述）",
  "action": "今週の一手 40〜70字（運用面の対応のみ。臨床判断・具体的な数値は書かない）"
}"""


def _build_admission_prompt(unit_name: str, entity: str, state: str) -> str:
    label = "診療科" if entity == "dept" else "病棟"
    return f"""以下の事実から、{label}「{unit_name}」の新入院に関する“今週の一手”を JSON で1つだけ出力してください。

【対象】{label}: {unit_name}
【新入院（直近7日）の状況】
- {state}

【書き方】
- body=新入院の状況の要約（数値を使わない定性的記述）。
- action=今週の一手（地域医療連携の強化、予定入院枠の調整など運用面の対応）。
- JSON 以外（```・前置き・末尾コメント）を出力しない。"""


def _build_surgery_prompt(dept_name: str, state: str) -> str:
    return f"""以下の事実から、診療科「{dept_name}」の全身麻酔手術に関する“今週の一手”を JSON で1つだけ出力してください。

【対象】診療科: {dept_name}
【全身麻酔手術（直近7日）の状況】
- {state}

【書き方】
- body=全身麻酔手術の状況の要約（数値を使わない定性的記述）。
- action=今週の一手（手術枠の稼働確認、執刀医との症例調整など運用面の対応）。
- JSON 以外（```・前置き・末尾コメント）を出力しない。"""


def narrate_admission_action(unit_name: str, entity: str, na, na_tgt,
                             model: str = DEFAULT_MODEL,
                             temperature: float = DEFAULT_TEMPERATURE,
                             quiet: bool = False) -> Optional[dict]:
    """新入院トピックの「今週の一手」を1ユニット分生成する（oMLX未起動/棄却時は None）。"""
    state = _q_target_gap(na, na_tgt)
    if state is None:
        return None
    result = None
    try:
        content = chat_json(
            system=ADMISSION_ACTION_SYSTEM_PROMPT,
            user=_build_admission_prompt(unit_name, entity, state),
            model=model, temperature=temperature, max_tokens=DEFAULT_NUM_PREDICT,
        )
        parsed = _extract_body_action(content)
        result = parsed if _is_hallucination_free(parsed, banned=_ADMISSION_BANNED) else None
    except Exception as e:
        logger.warning(f"oMLX 呼び出し失敗 (admission {entity}:{unit_name}): {e}")
    if not quiet:
        print(f"    [AI] {'✓' if result else '—'} admission {entity}:{unit_name}")
    return result


def narrate_surgery_action(dept_name: str, sv, surg_tgt,
                          model: str = DEFAULT_MODEL,
                          temperature: float = DEFAULT_TEMPERATURE,
                          quiet: bool = False) -> Optional[dict]:
    """全麻トピックの「今週の一手」を1ユニット分生成する（oMLX未起動/棄却時は None）。"""
    state = _q_target_gap(sv, surg_tgt)
    if state is None:
        return None
    result = None
    try:
        content = chat_json(
            system=SURGERY_ACTION_SYSTEM_PROMPT,
            user=_build_surgery_prompt(dept_name, state),
            model=model, temperature=temperature, max_tokens=DEFAULT_NUM_PREDICT,
        )
        parsed = _extract_body_action(content)
        result = parsed if _is_hallucination_free(parsed, banned=_SURGERY_BANNED) else None
    except Exception as e:
        logger.warning(f"oMLX 呼び出し失敗 (surgery {dept_name}): {e}")
    if not quiet:
        print(f"    [AI] {'✓' if result else '—'} surgery dept:{dept_name}")
    return result


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
_EMERGENCY_LEVELING_BANNED = ("延伸", "早期退院", "予定入院", "紹介")
_EMERGENCY_ADMISSION_BANNED = ("予定入院", "紹介", "地域医療連携", "傾向",
                              "稼働", "占有率", "逼迫", "余地が限られ")

EMERGENCY_LEVELING_SYSTEM_PROMPT = """あなたは病院の病床管理を支援する要約ライターです。救命救急センター系病棟（緊急入院・院内転棟が中心で予定入院はほぼ無い病棟）の「週末（土日）に在院が落ち込む状況」への“今週の一手”を、与えられた事実だけから日本語で書きます。以下を厳守してください。

【厳守事項】
1. 与えられた事実のみを使う。新しい数値・原因・固有名を足さない。本文に数値を再引用しない（定性語のみ使う）。
2. この病棟は緊急入院・院内転棟が中心で「予定入院」「地域医療連携（紹介元）」は存在しない。それらは絶対に提案しない。
3. ねらいは週末も受け入れ体制を維持し在院を保つこと。具体策は「転棟・転出（下り搬送）判断を迅速化して受け入れ余地を確保する」「週末の受け入れ体制（病床運用・当直）を平日と同水準に保つ」など、運用面の一般的な対応にとどめる。
4. 在院日数の延長や早期退院の促進は提案しない（禁止）。
5. 出力は指定 JSON のみ。前置き・説明・``` を付けない。簡潔・丁寧・事務的なトーン。

【出力スキーマ】
{
  "body": "週末在院の状況を述べる本文 50〜80字（数値を使わない定性的記述）",
  "action": "今週の一手 40〜70字（転棟判断の迅速化・週末受け入れ体制の維持。予定入院/紹介は書かない）"
}"""

EMERGENCY_ADMISSION_SYSTEM_PROMPT = """あなたは病院経営を支援する要約ライターです。救命救急センター系病棟（緊急入院・院内転棟が中心で予定入院・紹介受け入れという概念が無い病棟）の「新規受け入れ（緊急入院・転棟）」の状況への“今週の一手”を、与えられた事実だけから日本語で書きます。以下を厳守してください。

【厳守事項】
1. 与えられた事実のみを使う。新しい数値・原因・固有名を足さない。本文に数値を再引用しない（定性語のみ使う）。
2. この病棟は緊急入院・院内転棟が中心で「予定入院」「地域医療連携（紹介元）」は存在しない。それらは絶対に提案しない。
3. ねらいは受け入れ件数（緊急入院・転棟）を目標水準へ近づけること。具体策は「後方病床（転棟・転出先）との調整による受け入れ余地の確保」「病床運用（当直含む）の見直し」など、運用面の一般的な対応にとどめる。
4. 特定の疾患・術式・患者を名指しした医療行為の指示はしない（臨床判断はしない）。
5. 出力は指定 JSON のみ。前置き・説明・``` を付けない。簡潔・丁寧・事務的なトーン。

【出力スキーマ】
{
  "body": "受け入れ状況を述べる本文 50〜80字（数値を使わない定性的記述）",
  "action": "今週の一手 40〜70字（後方病床調整・病床運用見直し等。予定入院/紹介/具体的な数値は書かない）"
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
    result = None
    try:
        content = chat_json(
            system=EMERGENCY_LEVELING_SYSTEM_PROMPT,
            user=_build_emergency_leveling_prompt(ward_name, state),
            model=model, temperature=temperature, max_tokens=DEFAULT_NUM_PREDICT,
        )
        parsed = _extract_body_action(content)
        result = parsed if _is_hallucination_free(parsed, banned=_EMERGENCY_LEVELING_BANNED) else None
    except Exception as e:
        logger.warning(f"oMLX 呼び出し失敗 (emergency-leveling ward:{ward_name}): {e}")
    if not quiet:
        print(f"    [AI] {'✓' if result else '—'} emergency-leveling ward:{ward_name}")
    return result


def narrate_emergency_admission_action(ward_name: str, na, na_tgt,
                                       model: str = DEFAULT_MODEL,
                                       temperature: float = DEFAULT_TEMPERATURE,
                                       quiet: bool = False) -> Optional[dict]:
    """救命救急系病棟(4A/4C)向け・新規受け入れトピックの「今週の一手」を生成する。"""
    state = _q_target_gap(na, na_tgt)
    if state is None:
        return None
    result = None
    try:
        content = chat_json(
            system=EMERGENCY_ADMISSION_SYSTEM_PROMPT,
            user=_build_emergency_admission_prompt(ward_name, state),
            model=model, temperature=temperature, max_tokens=DEFAULT_NUM_PREDICT,
        )
        parsed = _extract_body_action(content)
        result = parsed if _is_hallucination_free(parsed, banned=_EMERGENCY_ADMISSION_BANNED) else None
    except Exception as e:
        logger.warning(f"oMLX 呼び出し失敗 (emergency-admission ward:{ward_name}): {e}")
    if not quiet:
        print(f"    [AI] {'✓' if result else '—'} emergency-admission ward:{ward_name}")
    return result


def narrate_alerts(alerts: list[dict],
                    model: str = DEFAULT_MODEL,
                    temperature: float = DEFAULT_TEMPERATURE,
                    quiet: bool = False) -> list[dict]:
    """
    各アラートに `narrative` フィールド（dict or None）を付与して返す。

    - narrative が None のアラートは、テンプレート側で title_fallback と
      facts を使って代替表示する前提。
    - LLM 未起動時は全て None になるが、例外は投げない。
    """
    if not alerts:
        return alerts

    enriched = []
    for a in alerts:
        n = _narrate_one(a, model=model, temperature=temperature)
        a2 = dict(a)
        a2["narrative"] = n
        enriched.append(a2)
        if not quiet:
            status = "✓" if n else "—"
            print(f"    [AI] {status} {a['id']}")
    return enriched
