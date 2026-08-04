"""添削フィードバックループ P3（few-shot 注入）のユニットテスト。

⚠️ PUBLIC リポにつき、実在の診療科名・実際のレポート文は書かない。
架空科A・テスト第一科・科イ・科ロ・科ハ等の合成データのみ使う。
LLM(oMLX)は呼ばない（narrate_admission_action の配線テストは _generate_checked を
スタブ差し替えして境界を切る）。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.report_feedback import capture_edits
from app.lib import fewshot


def _ctx(unit, src, body, action, topic, facts):
    return {"axis": "dept", "unit": unit, "_state": facts,
            "move": {"src": src, "body": body, "action": action, "topic": topic}}


class TestRebuildCorpusPairs(unittest.TestCase):
    def test_rebuild_corpus_pairs(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            capture_edits(d, "2026-07-01", [
                _ctx("架空科A", "ai", "AI本文A", "AI打ち手A", "admission", {"na": "close"})])
            capture_edits(d, "2026-07-01", [
                _ctx("架空科A", "manual", "人の本文A", "人の打ち手A", "admission", {"na": "close"})])
            capture_edits(d, "2026-07-08", [
                _ctx("テスト第一科", "ai", "AI本文B", "AI打ち手B", "surgery", {"surg": "met"})])
            capture_edits(d, "2026-07-08", [
                _ctx("テスト第一科", "manual", "人の本文B", "人の打ち手B", "surgery", {"surg": "met"})])

            n = fewshot.rebuild_corpus(d)
            self.assertEqual(n, 2)
            rows = fewshot.load_corpus(d)
            self.assertEqual(len(rows), 2)

            # 冪等: 再構築しても件数は変わらない（重複しない・全再構築で上書き）
            n2 = fewshot.rebuild_corpus(d)
            self.assertEqual(n2, 2)
            self.assertEqual(len(fewshot.load_corpus(d)), 2)


class TestStateTokenMapping(unittest.TestCase):
    def test_state_token_mapping(self):
        self.assertEqual(fewshot.state_token("目標を大きく上回っている"), "exceed")
        self.assertEqual(fewshot.state_token("目標を達成している"), "met")
        self.assertEqual(fewshot.state_token("目標をわずかに下回っている"), "close")
        self.assertEqual(fewshot.state_token("目標をやや下回っている"), "mild")
        self.assertEqual(fewshot.state_token("目標を明確に下回っている"), "poor")
        # 傾向節付き（_q_target_gap_trend が付ける「〜が、直近は…」等）
        self.assertEqual(
            fewshot.state_token("目標をやや下回るが、直近は改善に向かっている"), "mild")
        self.assertEqual(
            fewshot.state_token("目標を大きく上回り、直近もさらに伸びている"), "exceed")
        # 未知文/空
        self.assertIsNone(fewshot.state_token("よくわからない状態です"))
        self.assertIsNone(fewshot.state_token(""))
        self.assertIsNone(fewshot.state_token(None))


class TestBannedFilter(unittest.TestCase):
    def test_banned_filter_is_per_field(self):
        """禁止語が body 側だけに在る例は、捨てずに action ペアだけを採用する。

        コーパスを祝日週に採取すると大半の body が「祝日」を含むため、例を丸ごと
        除外すると非祝日週に一切効かなくなる（実測で admission/surgery が選択0件）。
        プロンプトに載る文字列自体を検査しているので安全性は落ちない。
        """
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            capture_edits(d, "2026-07-01", [
                _ctx("架空科A", "ai", "祝日を含むAI本文", "AI打ち手", "admission", {"na": "close"})])
            capture_edits(d, "2026-07-01", [
                _ctx("架空科A", "manual", "祝日を含む人の本文", "人の打ち手",
                     "admission", {"na": "close"})])
            fewshot.rebuild_corpus(d)

            blk = fewshot.examples_block("admission", "close", ("祝日", "連休"),
                                         "架空科B", state_dir=d)
            self.assertIn("AI案", blk)                 # 例ごと捨てない
            self.assertNotIn("祝日", blk)              # 禁止語はブロックに載らない
            self.assertIn("人の打ち手", blk)            # clean な action は採用される
            self.assertNotIn("人の本文", blk)           # 汚染された body は載せない

            # 禁止語指定が無ければ body も載る
            opened = fewshot.examples_block("admission", "close", (), "架空科B", state_dir=d)
            self.assertIn("人の本文", opened)

    def test_banned_filter_excludes_when_all_fields_dirty(self):
        """body・action の両方が禁止語を含む例は完全に除外する。"""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            capture_edits(d, "2026-07-01", [
                _ctx("架空科A", "ai", "祝日のAI本文", "祝日のAI打ち手",
                     "admission", {"na": "close"})])
            capture_edits(d, "2026-07-01", [
                _ctx("架空科A", "manual", "祝日の人の本文", "祝日の人の打ち手",
                     "admission", {"na": "close"})])
            fewshot.rebuild_corpus(d)

            blk = fewshot.examples_block("admission", "close", ("祝日",), "架空科B", state_dir=d)
            self.assertEqual(blk, "")

    def test_header_has_no_banned_literals(self):
        """ヘッダに禁止語の literal を書かない（プロンプトがモデルを誘発しないため）。"""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            capture_edits(d, "2026-07-01", [
                _ctx("架空科A", "ai", "AI本文", "AI打ち手", "admission", {"na": "close"})])
            capture_edits(d, "2026-07-01", [
                _ctx("架空科A", "manual", "人の本文", "人の打ち手", "admission", {"na": "close"})])
            fewshot.rebuild_corpus(d)

            blk = fewshot.examples_block("admission", "close", (), "架空科B", state_dir=d)
            self.assertNotEqual(blk, "")
            for word in ("祝日", "連休", "前年", "前回"):
                self.assertNotIn(word, blk)


class TestScoringPrefersExactToken(unittest.TestCase):
    def test_scoring_prefers_exact_token(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            capture_edits(d, "2026-07-01", [
                _ctx("科イ", "ai", "AI本文イ", "AI打ち手イ", "admission", {"na": "close"})])
            capture_edits(d, "2026-07-01", [
                _ctx("科イ", "manual", "人本文イ", "人打ち手イ", "admission", {"na": "close"})])
            capture_edits(d, "2026-07-08", [
                _ctx("科ロ", "ai", "AI本文ロ", "AI打ち手ロ", "admission", {"na": "mild"})])
            capture_edits(d, "2026-07-08", [
                _ctx("科ロ", "manual", "人本文ロ", "人打ち手ロ", "admission", {"na": "mild"})])
            fewshot.rebuild_corpus(d)

            # token="mild" は科ロと完全一致・科イ(close)は隣接階級（距離1）
            block = fewshot.examples_block("admission", "mild", (), "科ハ", state_dir=d, k=1)
            self.assertIn("人本文ロ", block)
            self.assertNotIn("人本文イ", block)


class TestMasking(unittest.TestCase):
    def test_masking(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            capture_edits(d, "2026-07-01", [
                _ctx("架空科A", "ai", "架空科Aの新入院はやや不調です", "架空科Aへの対応",
                    "admission", {"na": "mild"})])
            capture_edits(d, "2026-07-01", [
                _ctx("架空科A", "manual",
                    "架空科Aの新入院はやや不調でしたが、テスト第一科と比べても遜色ない水準です。",
                    "架空科Aへの対応を継続", "admission", {"na": "mild"})])
            capture_edits(d, "2026-07-08", [
                _ctx("テスト第一科", "ai", "テスト第一科本文", "テスト第一科打ち手",
                    "surgery", {"surg": "met"})])
            capture_edits(d, "2026-07-08", [
                _ctx("テスト第一科", "manual", "テスト第一科本文改", "テスト第一科打ち手改",
                    "surgery", {"surg": "met"})])
            fewshot.rebuild_corpus(d)

            block = fewshot.examples_block("admission", "mild", (), "架空科B", state_dir=d)
            self.assertIn("当科", block)
            self.assertIn("他科", block)
            self.assertNotIn("架空科A", block)
            self.assertNotIn("テスト第一科", block)


class TestEmptyCorpus(unittest.TestCase):
    def test_empty_corpus_returns_empty_string(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                fewshot.examples_block("admission", "mild", (), "架空科A", state_dir=Path(d)), "")

    def test_disabled_returns_empty_string(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            capture_edits(d, "2026-07-01", [
                _ctx("架空科A", "ai", "本文", "打ち手", "admission", {"na": "mild"})])
            capture_edits(d, "2026-07-01", [
                _ctx("架空科A", "manual", "本文2", "打ち手2", "admission", {"na": "mild"})])
            fewshot.rebuild_corpus(d)
            with mock.patch.dict(os.environ, {"GENIE_FEWSHOT": "0"}):
                self.assertEqual(
                    fewshot.examples_block("admission", "mild", (), "架空科B", state_dir=d), "")


class TestLevelingTopicOnlyK1(unittest.TestCase):
    def test_leveling_topic_only_k1(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            capture_edits(d, "2026-07-01", [
                _ctx("科イ", "ai", "AI本文イ", "AI打ち手イ", "leveling", {"ret": "good"})])
            capture_edits(d, "2026-07-01", [
                _ctx("科イ", "manual", "人本文イ", "人打ち手イ", "leveling", {"ret": "good"})])
            capture_edits(d, "2026-07-08", [
                _ctx("科ロ", "ai", "AI本文ロ", "AI打ち手ロ", "leveling", {"ret": "poor"})])
            capture_edits(d, "2026-07-08", [
                _ctx("科ロ", "manual", "人本文ロ", "人打ち手ロ", "leveling", {"ret": "poor"})])
            fewshot.rebuild_corpus(d)

            # token=None(leveling) は k=5 を指定してもk=1に制限される
            block = fewshot.examples_block("leveling", None, (), "科ハ", state_dir=d, k=5)
            self.assertEqual(block.count("AI案"), 1)


class TestRebuildCorpusDedupHumanAction(unittest.TestCase):
    def test_same_topic_duplicate_human_action_keeps_latest_only(self):
        """人手オーバーライドの expires=+14日 再捕捉で同一の添削文が複数ユニット・
        複数base_dateに渡って複製される自己強化ループを断つ。同一トピック内で
        human_actionが完全一致する行は base_date が最新の1件だけ残す。"""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            dup_action = "件数増に専念しましょう。"
            capture_edits(d, "2026-07-01", [
                _ctx("科イ", "ai", "AI本文イ", "AI打ち手イ", "surgery", {"surg": "poor"})])
            capture_edits(d, "2026-07-01", [
                _ctx("科イ", "manual", "人本文イ", dup_action, "surgery", {"surg": "poor"})])
            capture_edits(d, "2026-07-08", [
                _ctx("科ロ", "ai", "AI本文ロ", "AI打ち手ロ", "surgery", {"surg": "poor"})])
            capture_edits(d, "2026-07-08", [
                _ctx("科ロ", "manual", "人本文ロ", dup_action, "surgery", {"surg": "poor"})])
            capture_edits(d, "2026-07-15", [
                _ctx("科ハ", "ai", "AI本文ハ", "AI打ち手ハ", "surgery", {"surg": "poor"})])
            capture_edits(d, "2026-07-15", [
                _ctx("科ハ", "manual", "人本文ハ", dup_action, "surgery", {"surg": "poor"})])
            # 別トピックの重複は対象外（トピック横断では排除しない）
            capture_edits(d, "2026-07-01", [
                _ctx("架空科A", "ai", "AI本文A", "AI打ち手A", "admission", {"na": "close"})])
            capture_edits(d, "2026-07-01", [
                _ctx("架空科A", "manual", "人の本文A", "人の打ち手A", "admission", {"na": "close"})])

            n = fewshot.rebuild_corpus(d)
            self.assertEqual(n, 2)   # surgeryの3件は1件に圧縮 + admissionの1件
            rows = fewshot.load_corpus(d)
            surg_rows = [r for r in rows if r["topic"] == "surgery"]
            self.assertEqual(len(surg_rows), 1)
            self.assertEqual(surg_rows[0]["unit"], "科ハ")        # base_dateが最新のものが残る
            self.assertEqual(surg_rows[0]["base_date"], "2026-07-15")


class TestExamplesBlockDiversitySelection(unittest.TestCase):
    def test_second_pick_prefers_dissimilar_human_action(self):
        """1件目は現行どおり（token一致・base_date最新）。2件目は1件目と3-gram
        Jaccard類似度が最小の候補を選ぶ（MMR的多様性選択）。似た文言の候補ではなく、
        最も異なる文言の候補が採用されることを確認する。"""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            near_dup = "全麻手術件数増に専念しましょう。"
            near_dup_variant = near_dup + "ぜひお願いします。"
            distinct = "地域医療連携先への紹介強化と予定入院枠の調整を進めましょう。"
            capture_edits(d, "2026-07-15", [
                _ctx("科イ", "ai", "AI本文イ", "AI打ち手イ", "admission", {"na": "poor"})])
            capture_edits(d, "2026-07-15", [
                _ctx("科イ", "manual", "人本文イ", near_dup, "admission", {"na": "poor"})])
            capture_edits(d, "2026-07-08", [
                _ctx("科ロ", "ai", "AI本文ロ", "AI打ち手ロ", "admission", {"na": "poor"})])
            capture_edits(d, "2026-07-08", [
                _ctx("科ロ", "manual", "人本文ロ", near_dup_variant, "admission", {"na": "poor"})])
            capture_edits(d, "2026-07-01", [
                _ctx("科ハ", "ai", "AI本文ハ", "AI打ち手ハ", "admission", {"na": "poor"})])
            capture_edits(d, "2026-07-01", [
                _ctx("科ハ", "manual", "人本文ハ", distinct, "admission", {"na": "poor"})])
            fewshot.rebuild_corpus(d)

            block = fewshot.examples_block("admission", "poor", (), "架空科Z", state_dir=d, k=2)
            self.assertIn(near_dup, block)             # 1件目=最新(科イ)は現行どおり
            self.assertIn(distinct, block)              # 2件目=最も異なる文言(科ハ)が選ばれる
            self.assertNotIn(near_dup_variant, block)    # 似た文言(科ロ)は選ばれない


class TestPromptBytesIdenticalWhenNoExamples(unittest.TestCase):
    """fewshot 無効時、ai_narrative の配線が user 以外を触っていないことの回帰
    （追記が空文字列＝従来プロンプトとバイト単位で同一）。"""

    def test_prompt_bytes_identical_when_no_examples(self):
        from app.lib import ai_narrative as an

        captured = {}

        def _fake_generate_checked(tag, system, user, banned, allow=(), model=None,
                                   temperature=None, quiet=False):
            captured["user"] = user
            return {"body": "b", "action": "a", "src": "ai"}

        with mock.patch.dict(os.environ, {"GENIE_FEWSHOT": "0"}), \
             mock.patch.object(an, "_generate_checked", _fake_generate_checked):
            an.narrate_admission_action("架空科A", "dept", 10, 12, trend="上昇", quiet=True)

        state = an._q_target_gap_trend(10, 12, "上昇")
        expected = an._build_admission_prompt("架空科A", "dept", state)
        self.assertEqual(captured["user"], expected)


if __name__ == "__main__":
    unittest.main()
