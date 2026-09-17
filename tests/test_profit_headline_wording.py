"""
tests/test_profit_headline_wording.py — profit_headline.py の labels が
app/templates/detail.html の既存文言と一致しているか（写経であって新語彙を作って
いないか）を正規表現で機械検知する。

period / cmp は detail.html 側で JS テンプレートリテラルに変数が挟まっているため、
完全一致ではなく固定断片で照合する。

"入院 粗利/人日"・"外来 粗利/営業日"・"入院のみ・全科" の3語は、並行ワーカーが
detail.html に今まさに追加中（P1）のため、無いときは fail ではなく skip にする。
それ以外の語は現時点の detail.html に存在するはずなので fail 扱い（存在しない場合は
実装側で勝手に言い換えず、fail のまま報告する）。

例外（detail.html との照合対象から外す2語）:
  - guard: 正本は detail.html ではなく、改修プラン §3「回覧板に入れる1文
    （ユーザー承認済み）」。detail.html には出さない文言のため、ここでは
    _LABELS_STATIC["guard"] が spec §3 の承認済み1文と一致することだけを検査する。
  - scope_gairai: detail.html に外来の母集団注記は無い（外来行は P1 で追加予定。
    文言は portal/Comedix 側で使う）。対応箇所が無いため detail.html との照合
    対象から外す（恒久 skip はノイズになるため、skip ではなく対象外＝テスト無し）。

実行: リポジトリルートで
    .venv/bin/python -m pytest tests/test_profit_headline_wording.py -q
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.profit_headline import _LABELS_STATIC  # noqa: E402

DETAIL_HTML_PATH = Path(__file__).resolve().parent.parent / "app" / "templates" / "detail.html"

# 並行ワーカーが detail.html に「今まさに追加中」の3語（P1 detail 反映待ち）。
# 無いときは fail ではなく skip にする。
_P1_PENDING_KEYS = {"main_nyuin", "main_gairai", "scope_nyuin"}

# guard の正本（改修プラン §3「回覧板に入れる1文」・ユーザー承認済み）。
# detail.html には出さない文言なので、detail.html とは照合せずこの定数とだけ照合する。
_GUARD_APPROVED_TEXT = "※ 在院患者を減らして上げる数字ではありません。同じ病床で回転を上げると上がります。"

# period / cmp は JS テンプレートリテラルに変数が挟まるため、完全一致ではなく
# 固定断片で照合する。
_PERIOD_FRAGMENTS = ("月末見込み（", "時点・診療実績ベース・暫定）")
_CMP_FRAGMENTS = ("前月比", "前月(見込み)比")


class TestLabelsMatchDetailHtml(unittest.TestCase):
    """labels の各リテラルが detail.html 内に存在すること（文言乖離の機械検知）。"""

    @classmethod
    def setUpClass(cls):
        cls.html = DETAIL_HTML_PATH.read_text(encoding="utf-8")

    def _assert_literal_in_html(self, key: str, text: str):
        pattern = re.escape(text)
        found = re.search(pattern, self.html) is not None
        if not found and key in _P1_PENDING_KEYS:
            self.skipTest(f"P1 detail 反映待ち: labels['{key}']={text!r} が detail.html に未追加")
        self.assertTrue(found, f"labels['{key}']={text!r} が detail.html に見つからない（文言乖離）")

    def test_main_nyuin(self):
        self._assert_literal_in_html("main_nyuin", _LABELS_STATIC["main_nyuin"])

    def test_main_gairai(self):
        self._assert_literal_in_html("main_gairai", _LABELS_STATIC["main_gairai"])

    def test_target(self):
        self._assert_literal_in_html("target", _LABELS_STATIC["target"])

    def test_tolerance(self):
        self._assert_literal_in_html("tolerance", _LABELS_STATIC["tolerance"])

    def test_note(self):
        self._assert_literal_in_html("note", _LABELS_STATIC["note"])

    def test_guard_matches_spec_approved_sentence(self):
        # guard は detail.html からの写経ではない（正本は spec §3 の承認済み1文）。
        # detail.html とは照合しない。
        self.assertEqual(_LABELS_STATIC["guard"], _GUARD_APPROVED_TEXT,
                         "guard の正本は改修プラン §3 の承認済み1文。detail.html とは照合しない")

    def test_scope_nyuin(self):
        self._assert_literal_in_html("scope_nyuin", _LABELS_STATIC["scope_nyuin"])

    # scope_gairai（外来のみ・全科）は detail.html に対応箇所が無い（外来行は P1 で
    # 追加予定・文言は portal/Comedix 側で使う）ため、detail.html との照合対象から
    # 外す（skip ではなく対象外＝テストメソッド無し。恒久 skip はノイズになるため）。

    def test_period_fixed_fragments(self):
        for frag in _PERIOD_FRAGMENTS:
            self.assertIn(frag, self.html, f"period の固定断片 {frag!r} が detail.html に見つからない")

    def test_cmp_fixed_fragments(self):
        for frag in _CMP_FRAGMENTS:
            self.assertIn(frag, self.html, f"cmp の固定断片 {frag!r} が detail.html に見つからない")


if __name__ == "__main__":
    unittest.main()
