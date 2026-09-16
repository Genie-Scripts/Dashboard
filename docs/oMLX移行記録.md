# oMLX 移行記録（要約LLM: Ollama → oMLX）

**日付**: 2026-06-07
**対象**: 診療ダッシュボードの AI ナラティブ生成（アラート自然言語化 / トリアージ / 週次ストーリー）
**結論**: 要約LLMのバックエンドを **Ollama から oMLX（OpenAI互換 /v1）へ統一**。アーキテクチャ・フォールバック挙動は不変。実機テストまで合格。

---

## 1. 背景・目的

- 同マシン（Apple Silicon / 64GB unified memory）で `ai-apps/WebSearch`（Perplexica + OpenWebUI）が **oMLX** を要約/チャットLLMに使用。
- Dashboard だけ別途 **Ollama** を使うと、**別モデルを二重常駐させてメモリ不足**になりやすい。
- LLM基盤を **oMLX に一本化**することで、モデル常駐を1系統に集約し、運用とメモリを単純化する。

> oMLX は OpenAI 互換 API を `127.0.0.1:8000/v1` で提供。Dashboard は **非Docker・ホストの python 実行**なので、エンドポイントは `localhost:8000`（WebSearch コンテナ群の `host.docker.internal` とは異なる）。

---

## 2. 変更点（ファイル一覧）

| ファイル | 変更内容 |
|---|---|
| **`app/lib/llm.py`**（新規） | oMLX 共通クライアント `chat_json()`。`openai` パッケージで `localhost:8000/v1` に接続。`OMLX_MODEL`/`OMLX_BASE_URL`/`OMLX_API_KEY`/`OMLX_TIMEOUT` を環境変数で一元管理。`response_format=json_object`＋非対応時の自動フォールバック付き |
| `app/lib/ai_narrative.py` | `ollama.chat`→`chat_json` に置換。`import os` 撤去。モデルは `llm.DEFAULT_MODEL` を import して集約 |
| `app/lib/weekly_story.py` | 同上。ハードコードだった `DEFAULT_MODEL` を `llm` から import に統一 |
| `app/lib/triage.py` | 同上＋docstring の「Ollama」→「oMLX」 |
| `deploy.sh` | `ollama serve` / `ollama pull` ブロックを **oMLX 起動確認＋モデル存在チェック**に置換。APIキーは `~/.omlx/settings.json` から取得 |
| `requirements.txt` | `ollama>=0.4.0` → `openai>=1.30.0` |
| `config/evaluation_rules.yaml` | コメント「ローカルLLM（Ollama）」→「（oMLX）」 |
| `.venv` | `openai`（2.41.0）導入済み |

### API マッピング（Ollama → OpenAI互換）

| Ollama | oMLX(OpenAI互換) |
|---|---|
| `ollama.chat(model, messages)` | `client.chat.completions.create(model, messages)` |
| `options={"temperature","num_predict"}` | `temperature=`, `max_tokens=` |
| `format="json"` | `response_format={"type":"json_object"}`（非対応時は自動で外して再試行） |
| `keep_alive="5m"` | 廃止（oMLX がモデル寿命を自前管理） |
| `res["message"]["content"]` | `res.choices[0].message.content` |

---

## 3. 構成・設定

要約LLMの設定は **`app/lib/llm.py` と `deploy.sh` の環境変数で一元管理**。

| 環境変数 | 既定値 | 意味 |
|---|---|---|
| `OMLX_MODEL` | `Llama-3.1-Swallow-8B-Instruct-v0.5` | 使用モデル |
| `OMLX_BASE_URL` | `http://localhost:8000/v1` | oMLX エンドポイント |
| `OMLX_API_KEY` | `~/.omlx/settings.json` の `auth.api_key`（無ければ既定キー） | 認証 |
| `OMLX_TIMEOUT` | `60` | 1リクエストの上限秒 |

- **モデル切替は `OMLX_MODEL` を変えるだけ**（コード変更不要）。
- **フォールバック挙動は完全維持**: oMLX 未起動 / `openai` 未導入 / モデル未取得 は、すべて従来どおり `narrative=None`（triage は Python 定型文）に無害縮退。例外は投げない。
- **auto-pull は廃止**: oMLX はローカルのモデルファイル前提。未取得時は `deploy.sh` が警告するだけで AI 生成はスキップ。

---

## 4. モデル選定（Swallow-8B）と A/B 比較

### 選定理由
Dashboard の生成は「Python が確定した事実 → 短い JSON（headline 20字 / body 60〜90字 / action 50〜80字）への翻訳」という**制約の強い軽タスク**。低温度・厳格プロンプト・日本語・事務的トーン。→ 日本語特化の軽量 instruct で十分と判断し **Llama-3.1-Swallow-8B-Instruct-v0.5** を既定に採用。

### A/B 比較: Swallow-8B vs Qwen2.5-14B-Instruct-8bit（実機・temp0.2）

| 観点 | Swallow-8B | Qwen2.5-14B-8bit |
|---|---|---|
| ディスク/メモリ | 15GB（**fp16＝無量子化**） | 15GB（8bit） |
| 応答速度（代表2件） | 6.8s / 7.3s | 8.7s / 6.7s |
| 日本語の自然さ・丁寧体 | **やや有利**（終始です・ます体で要件に忠実） | 1ケースで「〜である」体に滑った |
| 事実カバー・JSON 構造 | 良好 | 良好（互角） |
| 数値の再引用抑制 | どちらも完全には守らず（引き分け） | 同左 |

**結論**:
- メモリ・速度は**ほぼ互角**（両方 ~15GB / ~7秒）。トークン生成はメモリ帯域律速のため、サイズが同程度なら 14B でも遅くならない。
- 本タスクは易しく**サイズ差が品質差に直結しない**。むしろ日本語特化の Swallow が丁寧体・語感で僅差で上に出る場面があった。
- → **Qwen2.5-14B へ乗り換える積極的理由は薄い。Swallow-8B を既定として据え置き。**

> ⚠️ メモリに関する訂正メモ: 当初 Swallow-8B を「~5-6GB」と見積もったが、実体は **fp16 で 15GB**。Qwen2.5-14B-8bit（15GB）と同等。「Swallow は省メモリ」は誤りだったので記録しておく。

### 品質を上げたい場合の選択肢
8B で物足りない場合の引き上げ先は Qwen2.5-14B ではなく、**導入済みのより新しい大型モデル**が効くはず:
`Qwen3.6-27B` 系 / `gemma-4-26B-A4B-it` / `Qwen3.6-35B-A3B`（A3B/MoE で速度も確保）。切替は `OMLX_MODEL` のみ。

---

## 5. 検証結果（合格）

- 3モジュール + 新 `llm.py` の **import OK**（`DEFAULT_MODEL = Llama-3.1-Swallow-8B-Instruct-v0.5`）。
- コード本体に **`ollama` 参照ゼロ**（残るのは `llm.py`/`deploy.sh` の移行メモのみ）。
- `deploy.sh` **構文チェック OK**。
- **end-to-end スモークテスト合格**: Swallow-8B 経由でアラート→きれいな JSON を生成、既存 `_extract_json` で正しくパース（約7秒）。

---

## 6. 運用メモ

- **前提サービス**: oMLX アプリが起動していること（`deploy.sh` が未起動なら `open -a oMLX` で起動し最大30秒待機）。
- **モデルの事前ダウンロード**が必要（auto-pull なし）。`OMLX_MODEL` のモデルが oMLX に無いと AI 生成はスキップ（警告のみ、デプロイは継続）。
- **WebSearch との共存**: 同じ oMLX を共有。Dashboard はデプロイ時のバッチ実行なので同時要求が起きにくく、モデル切替の影響は実用上ほぼ無視できる。ただし WebSearch 側で 80B 等の大型を常駐させたまま Dashboard が別モデルを要求すると、oMLX 側でモデル再ロードが発生しうる点は留意。
- **モデル切替例**: `OMLX_MODEL=Qwen3.6-27B-UD-MLX-4bit ./deploy.sh`
