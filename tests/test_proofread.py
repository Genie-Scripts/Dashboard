"""proofread（一手 body/action の誤字脱字校正）のユニットテスト。

LLM 呼び出し（chat_json）はモックし、機械ガード（数字列保持・長さ比）と
JSON抽出・正規化の境界のみを検証する。
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib import proofread


def _fake_chat_json(reply_text):
    """chat_json の戻り値（JSON文字列）を固定で返すフェイク。"""
    def _fake(system, user, model, temperature=0.0, max_tokens=256, seed=None):
        return json.dumps({"text": reply_text}, ensure_ascii=False)
    return _fake


class TestProofreadText(unittest.TestCase):
    def setUp(self):
        self._orig = proofread.chat_json

    def tearDown(self):
        proofread.chat_json = self._orig

    def test_digit_change_discarded(self):
        proofread.chat_json = _fake_chat_json("新入院は本日11件でした。")
        out = proofread.proofread_text("新入院は本日10件でした。")
        self.assertEqual(out["text"], "新入院は本日10件でした。")
        self.assertFalse(out["changed"])
        self.assertIsNotNone(out["error"])

    def test_length_ratio_exceeded_discarded(self):
        original = "在院が目標を下回っています。"
        too_long = original + "。" * 40   # 長さ比が1.6を大きく超える
        proofread.chat_json = _fake_chat_json(too_long)
        out = proofread.proofread_text(original)
        self.assertEqual(out["text"], original)
        self.assertFalse(out["changed"])
        self.assertIsNotNone(out["error"])

    def test_normal_correction_changed_true(self):
        original = "新入院の患者数が目標を上回っています。"
        fixed = "新入院の患者数が目標を上回っております。"
        proofread.chat_json = _fake_chat_json(fixed)
        out = proofread.proofread_text(original)
        self.assertEqual(out["text"], fixed)
        self.assertTrue(out["changed"])
        self.assertIsNone(out["error"])

    def test_identical_result_changed_false(self):
        original = "全身麻酔手術の件数増に専念しましょう。"
        proofread.chat_json = _fake_chat_json(original)
        out = proofread.proofread_text(original)
        self.assertEqual(out["text"], original)
        self.assertFalse(out["changed"])
        self.assertIsNone(out["error"])

    def test_llm_exception_returns_original_with_error(self):
        def _raise(*a, **kw):
            raise RuntimeError("oMLX未起動")
        proofread.chat_json = _raise
        original = "週末の在院維持率を確認しましょう。"
        out = proofread.proofread_text(original)
        self.assertEqual(out["text"], original)
        self.assertFalse(out["changed"])
        self.assertIsNotNone(out["error"])

    def test_empty_text_short_circuits(self):
        out = proofread.proofread_text("")
        self.assertEqual(out["text"], "")
        self.assertFalse(out["changed"])
        self.assertIsNotNone(out["error"])

    def test_broken_json_response_discarded(self):
        def _fake(system, user, model, temperature=0.0, max_tokens=256, seed=None):
            return "これはJSONではない"
        proofread.chat_json = _fake
        original = "新入院数の推移を確認します。"
        out = proofread.proofread_text(original)
        self.assertEqual(out["text"], original)
        self.assertFalse(out["changed"])
        self.assertIsNotNone(out["error"])

    def test_newline_and_space_normalized(self):
        original = "新入院の患者数が目標を上回っています。"
        proofread.chat_json = _fake_chat_json("新入院の患者数が\n目標を  上回っています。")
        out = proofread.proofread_text(original)
        self.assertNotIn("\n", out["text"])
        self.assertNotIn("  ", out["text"])


if __name__ == "__main__":
    unittest.main()
