# 全麻一手コメント強化 実装プラン（閾値の緩和＋直接文言化）

- 作成日: 2026-07-06（行アンカーはこの日時点の main = commit 9fea41f 基準。実装時は関数名・定数名で再検索すること）
- 対象サブシステム: 部門別レポートPDF（`scripts/build_dept_reports.py` → `app/lib/dept_report.py`）の
  「この期間の一手」＝ 病院全体サマリ ＋ 診療科/病棟の各1枚
- 状態: **実装済（2026-07-09・未コミット→コミット済）**。本プランどおり実装・実データ検証完了。
  - `app/lib/dept_report.py`: 定数2つ＋`_select_action_topic`(eligibleルール)＋`_select_hospital_topic`新設＋文言直接化。
  - `app/lib/ai_narrative.py`: surgery/admission システムプロンプト2つ更新。
  - `tests/test_dept_report_topic.py` 新規13件。全112テストpass。
  - 実データ検証(基準日2026-07-07): 病院全体=topic surgery＋「件数増に専念」／乳腺外科62%=主・整形94%/一般消化器58%=副併記／内科系に全麻action無し／`--no-ai`フォールバックも新文言／AI-on棄却ゼロ(ok:25・AI率91%＝ベースライン同水準)。
  - ※spec記載の脳神経外科67%は2026-07-05データ。2026-07-07では達成側(133%)へ変化し正しく全麻非言及。

---

## 1. 背景（現状調査の結果・2026-07-05 データで確認済み）

「この期間の一手」は 週末在院平準化（leveling）／新入院（admission）／全麻（surgery）の
3トピックから **目標未達スコア最大のものを1つ** 選ぶ設計になっている。

- スコア定義（`app/lib/dept_report.py` `_select_action_topic`, L414 付近）
  - leveling = `room / max_room`（全ユニット中の相対値）
  - admission = `max(0, 1 - 実績/目標)`（絶対未達率）
  - surgery = `max(0, 1 - 実績/目標)`（**外科系 `SURGERY_DISPLAY_DEPTS` のみ候補**）
- 足切り: `ACTION_TOPIC_MIN_SCORE = 0.12`（L399）。最大スコアがこれ未満なら既定の leveling に落ちる。
  副トピック（「あわせて、〜も…」併記）も同じ 0.12 が条件。
- 病院全体サマリは `build_hospital_overview_context`（L1349〜）内で同型のスコア
  （L1477-1484）＋同じ 0.12 足切り。

**2026-07-05 データでの実測値（病院全体）:**

| トピック | 実績 / 目標 | スコア | 0.12判定 |
|---|---|---|---|
| 全麻 | 19.3件/日 / 21（達成率92%） | 0.081 | 落選 |
| 新入院 | 359件 / 380 | 0.055 | 落選 |
| 週末在院維持率 | 91.2% / 93 | 0.088 | 落選 |

→ 全トピック足切りで既定の leveling が選ばれ、**全麻が92%未達でも病院全体の一手に出ない**。
診療科側は 2026-07-05 ビルドで脳神経外科（達成率67%）のみ全麻が主トピック。

また、選ばれた場合の文言もレバーが「手術枠の稼働状況の確認や、執刀医との症例調整など、
全身麻酔手術を底上げする運用面の対応」（`_HOSPITAL_LEVERS`, L1314-1318）という間接的な
表現に固定されており、「件数増に専念」のような直接的な号令は出ない。

## 2. 要求（2026-07-06 ユーザー指示・確定）

1. **病院全体**でも達成率 **95% 未満**程度の未達で全麻に言及してほしい
2. **外科系診療科**への一手コメントでは達成率 **98% 未満**程度の未達でも全麻に言及してほしい
3. 「**件数増に専念しましょう**」「**患者数増に取り組みましょう**」のような、
   もっと**直接的な文言**にしたい（全麻・新入院の両トピック）

## 3. 変更設計

### 3-1. トピック別の足切り閾値（要求1・2）

一律 `ACTION_TOPIC_MIN_SCORE = 0.12` を、**トピック別の最小スコア**に拡張する。
達成率換算: 病院全体の全麻 = 1 − 0.95 = **0.05**、診療科の全麻 = 1 − 0.98 = **0.02**。

```python
# app/lib/dept_report.py（ACTION_TOPIC_MIN_SCORE の直下に追加）
ACTION_TOPIC_MIN_SCORE = 0.12          # leveling/admission の既定（変更しない）
SURGERY_TOPIC_MIN_SCORE = 0.02         # 外科系診療科: 全麻達成率98%未満で一手候補に
SURGERY_TOPIC_MIN_SCORE_HOSPITAL = 0.05  # 病院全体: 全麻達成率95%未満で一手候補に
```

**選定ルール（新）**: 「トピックごとの最小スコアを満たすもの（eligible）の中から
生スコア最大を主トピックに。eligible が無ければ従来どおり leveling 既定」。
副トピックも同じトピック別閾値で判定する。

```python
def _select_action_topic(type_key, room, max_room, na, na_tgt, sv, surg_tgt,
                         *, surgery_min: float = SURGERY_TOPIC_MIN_SCORE):
    scores = {"leveling": (room / max_room) if max_room else 0.0,
              "admission": _admission_gap_score(na, na_tgt)}
    mins = {"leveling": ACTION_TOPIC_MIN_SCORE, "admission": ACTION_TOPIC_MIN_SCORE}
    if type_key == "surgical":
        scores["surgery"] = _surgery_gap_score(sv, surg_tgt)
        mins["surgery"] = surgery_min
    eligible = {k: v for k, v in scores.items() if v >= mins[k]}
    primary = max(eligible, key=eligible.get) if eligible else "leveling"
    sec = {k: v for k, v in scores.items() if k != primary and v >= mins[k]}
    secondary = max(sec, key=sec.get) if sec else None
    return primary, secondary, scores
```

- 既存呼び出し（診療科/病棟ループ内 L1088）は**変更不要**（デフォルト引数で 0.02 が効く。
  内科系・病棟は scores に surgery キー自体が入らないため従来と完全同一挙動）。
- 病院全体側（L1482-1484 の `h_topic = max(...)` ＋ 0.12 足切り）を同じルールに差し替える:

```python
h_mins = {"leveling": ACTION_TOPIC_MIN_SCORE, "admission": ACTION_TOPIC_MIN_SCORE,
          "surgery": SURGERY_TOPIC_MIN_SCORE_HOSPITAL}
h_eligible = {k: v for k, v in topic_scores.items() if v >= h_mins[k]}
h_topic = max(h_eligible, key=h_eligible.get) if h_eligible else "leveling"
```

**この設計で満たされる挙動（意図した仕様として明記）:**

- 病院全体・現データ（全麻0.081 / leveling0.088 / admission0.055）では
  全麻だけが eligible（0.081 ≥ 0.05）→ **主トピック=全麻** になる。
  leveling のスコアの方がわずかに大きいのに全麻が選ばれるのは「全麻を優先的に
  言及してほしい」という要求どおりの意図的な非対称（leveling は KPI カード・facts で
  引き続き言及される）。
- 外科系診療科では、全麻が主トピックを取れなくても（例: leveling 0.5 が主）、
  達成率98%未満なら副トピック eligible となり
  `_secondary_clause`（L469）の「あわせて、全身麻酔手術も〜状況です。」が**必ず併記**される。
  ＝「98%程度の未達でも言及」は主または副のどちらかで常に成立する。
- 90%台後半の状態文は `_q_target_gap`（`app/lib/ai_narrative.py` L168、95〜100%=
  「目標をわずかに下回っている」バケット）が既に定義済みで、追加の状態文実装は不要。

### 3-2. 直接的な文言への変更（要求3）

方針: **未達時の action を「〜増に専念/取り組みましょう」の直接的な呼びかけで締める**。
達成時（維持系）の文言と、名指し批判をしない・在院日数延伸を提案しない等の
既存ガードは変えない（職員発信トーンの枠内での意図的なトーン強化）。

変更箇所は「定型フォールバック」「病院全体レバー」「LLMシステムプロンプト」の3層
（AI文はレバー/プロンプト経由、oMLX 停止時・棄却時は定型文経由のため、**両方**変える）。

#### (a) `app/lib/dept_report.py` — 定型フォールバック

- `_fallback_move_surgery`（L358-368）の未達分岐 action:
  - 旧: `"手術枠の稼働状況を確認し、執刀医と症例の積み増しを調整しましょう。"`
  - 新: `"手術枠の稼働状況の確認と執刀医との症例調整で、全身麻酔手術の件数増に専念しましょう。"`
- `_fallback_move_admission`（L344-355）の未達分岐 action:
  - 旧: `"地域医療連携での紹介受け入れ強化や、予定入院枠の調整を検討しましょう。"`
  - 新: `"地域医療連携での紹介受け入れ強化や予定入院枠の調整で、新入院の患者数増に取り組みましょう。"`
- `_HOSPITAL_LEVERS`（L1314-1318）:
  - admission 旧: `"地域医療連携（紹介元）への働きかけ強化や、予定入院枠の週後半への調整など、新入院を底上げする運用対応。"`
  - admission 新: `"地域医療連携（紹介元）への働きかけ強化や、予定入院枠の週後半への調整などで、新入院の患者数増に取り組む。"`
  - surgery 旧: `"手術枠の稼働状況の確認や、執刀医との症例調整など、全身麻酔手術を底上げする運用面の対応。"`
  - surgery 新: `"手術枠の稼働状況の確認や、執刀医との症例調整などで、全身麻酔手術の件数増に専念する。"`
  - ※ このレバー文は `narrate_hospital_summary` にそのまま渡り AI 文の action を規定する
    （`_build_hospital_summary_prompt` の「レバーの軸: {lever}」）。ここを変えるだけで
    病院全体の AI 文も直接化される。
- `_fallback_move_hospital`（L1322）は admission/surgery を上記フォールバックに委譲済みのため
  変更不要。leveling 分岐の文言も変更しない。

#### (b) `app/lib/ai_narrative.py` — LLMプロンプト（診療科向け）

- `SURGERY_ACTION_SYSTEM_PROMPT`（L780）:
  - 厳守事項2に追記: `目標未達のときの action は「全身麻酔手術の件数増に専念しましょう」のような直接的な呼びかけで締める。`
  - 出力例の未達側 action を差し替え:
    - 旧: `"手術枠の稼働状況を確認し、執刀医と症例調整を進めてこの流れを後押ししましょう。"`
    - 新: `"手術枠の稼働確認と執刀医との症例調整で、件数増に専念しましょう。"`
  - 達成側の例（現状維持系）は変更しない。
- `ADMISSION_ACTION_SYSTEM_PROMPT`（L759）:
  - 厳守事項2に追記: `目標未達のときの action は「新入院の患者数増に取り組みましょう」のような直接的な呼びかけで締める。`
  - 出力例の未達側 action を差し替え:
    - 旧: `"地域医療連携での紹介受け入れを重点化し、予定入院枠の前倒しも検討しましょう。"`
    - 新: `"紹介受け入れの重点化と予定入院枠の前倒しで、患者数増に取り組みましょう。"`
- `HOSPITAL_SUMMARY_SYSTEM_PROMPT`（L1056）は変更不要（action はレバー文に従う設計）。
  ただし出力例の action が旧トーンのままだと引っ張られる恐れがあるため、
  出力例末尾を「〜空床を埋めましょう（在院日数は延ばさない）。」のまま維持しつつ、
  実装後の実出力を確認して単調・間接化するようなら例文も直接化する（任意）。

#### (c) 変更しないもの（明記）

- 救命救急系病棟（4A/4C）の専用プロンプト/定型文（予定入院・紹介の概念が無い）— 対象外。
- `_secondary_clause`（あわせて併記）の文言 — action は主トピック集中の設計（P3）を維持。
- 外科系常設の数値行 `_surg_highlight`（`全麻：直近7日 X件／週目標Y（Z%）…`）— 変更なし。
- 内科系に全麻トピックを出さない制約（`type_key == "surgical"` 限定）— 維持。
  眼科は `SURGERY_DISPLAY_DEPTS` 所属なら外科系扱い（現状の分類に従う。変更しない）。
- Comedix 週次カード/HTML の「今週の一手」（`今週の一手.md` 手動運用）、portal の
  週末平準化タブの LLM 一手（`narrate_leveling_actions`）— 別サブシステム・対象外。
- 人手オーバーライド機構（`reports/overrides.md` → `apply_override`）— move 確定後に
  適用されるため無関係。既存オーバーライドはそのまま効く。

### 3-3. 禁止語・検証ガードとの整合（実装時に必ず確認）

- 新文言に含まれる語が `_SURGERY_BANNED` / `_ADMISSION_BANNED` /
  `_HOSPITAL_SUMMARY_BANNED` に**含まれていないこと**を確認する
  （2026-07-06 時点: 「件数」「専念」「患者数」は禁止語に無い。「傾向」「前年」「前回」
  等の連動緩和ロジックは変更しない）。
- `_generate_checked` の JSON スキーマ（action 40〜70字/40〜80字）に収まる例文にする。
- LLM は Swallow-8B（oMLX, `llm.chat_json` 入口）。プロンプト変更後に棄却率が
  上がっていないか `scripts/report_comment_diversity.py` で確認する（§5）。

## 4. 実装手順（そのまま実行できるチェックリスト）

1. `app/lib/dept_report.py`: 定数2つ追加（§3-1）。
2. 同: `_select_action_topic` を新ルールへ書き換え（§3-1 のコードどおり。docstring も
   「トピック別最小スコア」の説明に更新）。
3. 同: 病院全体ブロック（`build_hospital_overview_context` 内 `h_topic` 決定部）を差し替え。
   テスタビリティのため `_select_hospital_topic(topic_scores) -> str` として関数に切り出すのを推奨。
4. 同: `_fallback_move_surgery` / `_fallback_move_admission` / `_HOSPITAL_LEVERS` の文言変更（§3-2a）。
5. `app/lib/ai_narrative.py`: 2つのシステムプロンプト更新（§3-2b）。
6. テスト追加（§5-1）。
7. `make test` → ビルド検証（§5-2）。
8. コミット（例: `feat(reports): 全麻一手の閾値緩和(病院95%/外科系98%)＋件数増の直接文言化`）。

## 5. テストと受け入れ基準

### 5-1. ユニットテスト（新規 `tests/test_dept_report_topic.py`）

```python
from app.lib.dept_report import (_select_action_topic, _fallback_move_surgery,
                                 _fallback_move_admission)

# 1) 外科系: 全麻97%(スコア0.03)のみ eligible → 主トピック=surgery
#    room=0.05*max_room, na=目標達成
assert _select_action_topic("surgical", 0.05, 1.0, 10, 10, 9.7, 10)[0] == "surgery"
# 2) 外科系: 全麻99%(0.01) は足切り → leveling 既定
assert _select_action_topic("surgical", 0.05, 1.0, 10, 10, 9.9, 10)[0] == "leveling"
# 3) 外科系: leveling 0.5 が主でも 全麻96%(0.04) は副トピックで言及される
p, s, _ = _select_action_topic("surgical", 0.5, 1.0, 10, 10, 9.6, 10)
assert (p, s) == ("leveling", "surgery")
# 4) 内科系: 全麻データがあっても候補にならない（回帰）
p, s, sc = _select_action_topic("internal", 0.05, 1.0, 10, 10, 5, 10)
assert p == "leveling" and "surgery" not in sc
# 5) 病院全体: 2026-07-05 実測値の再現（切り出した _select_hospital_topic で）
#    {"leveling": 0.088, "admission": 0.055, "surgery": 0.081} → "surgery"
# 6) 文言: 未達 action に直接表現が入る
assert "件数増に専念" in _fallback_move_surgery("目標をやや下回っている")["action"]
assert "患者数増に取り組" in _fallback_move_admission("目標をやや下回っている")["action"]
```

（既存テストは topic 選定を直接カバーしていない＝2026-07-06 時点で
`tests/*.py` に `_select_action_topic` への参照なし。既存 pass 数の維持を確認する。）

### 5-2. ビルド検証（実データ・受け入れ基準）

`make reports`（レビュー運用サーバ）または `python3 scripts/build_dept_reports.py` で再ビルドし、
`dept_reports/{基準日}/レビュー_{基準日}.html` と 病院全体サマリPDF を確認:

1. **病院全体サマリ**: 全麻達成率が92%前後（<95%）のままなら一手のトピックが全麻になり、
   action に「件数増に専念」系の文言が入ること。
2. **外科系診療科**: 全麻達成率<98% の科すべてで、一手本文（主トピック時）または
   「あわせて、全身麻酔手術も〜」（副トピック時）の言及があること。
   脳神経外科（67%）は引き続き主トピック=全麻であること。
3. **回帰**: 内科系・病棟の一手に全麻の action が出ないこと。救命救急系（4A/4C）の文言不変。
   外科系の数値行（`全麻：直近7日…`）が従来どおり付くこと。
4. oMLX **停止**状態で再ビルド → フォールバック定型文に新文言が出ること。
   oMLX **起動**状態で `scripts/report_comment_diversity.py` を実行し、
   surgery トピックの fallback率（=AI棄却率）が変更前と同水準であること
   （プロンプト変更で禁止語棄却が増えていないかの確認）。

## 6. リスク・留意点

- **単調化**: 病院全体で全麻が達成率90〜95%で推移する限り毎回全麻テーマになる。
  これは要求どおりの意図的挙動。変化は差分ナラティブ（前回比）・牽引役の名指しで担保。
  単調が問題化したら `SURGERY_TOPIC_MIN_SCORE_HOSPITAL` の1定数で調整できる。
- **週次フリップ**: 診療科の98%閾値近傍で主/副が週ごとに入れ替わり得る。副トピック言及は
  続くため情報は途切れない。気になる場合は `_peer_tier` の緩衝帯（±0.06）方式の
  ヒステリシスを後日追加（本プランのスコープ外）。
- **トーン**: 「専念しましょう」は従来の職員発信トーンより強い。名指し批判なし・
  在院日数延伸の提案禁止・褒める方向の牽引役のみ名指し、という既存ガードは全て維持する。
- **leveling より全麻優先の非対称**（§3-1）: eligible 判定の閾値差により、スコアが
  僅差で leveling の方が大きくても全麻が主トピックになるケースがある。意図的仕様。

## 7. ロールバック

文言は git revert。挙動だけ戻す場合は `SURGERY_TOPIC_MIN_SCORE(_HOSPITAL)` を
0.12 に設定すれば選定ロジックは従来と等価（eligible ルールは 0.12 一律なら旧実装と同じ
結果になる。ただし旧実装は「全体最大が閾値未満なら leveling」、新実装は「eligible 内最大」
なので、複数トピックが 0.12 以上のとき同一。0.12 未満同士の順位は両者とも leveling）。
