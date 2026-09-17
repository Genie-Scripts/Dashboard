"""
tests/test_profit_kpi_units_template_guard.py — detail.html 側の単位併存改修
(spec/改修プラン_粗利の単位あたり指標.md §9 #8) の文言・キー名を機械検知する。

`tests/test_profit_headline_wording.py` と同じ手法（detail.html をテキストとして
読み込み、正規表現/部分一致で照合する。Jinja レンダリングも LLM・常駐サーバも
一切使わない）。

検査内容:
  - 旧単位表記（万/営業日・万円/営業日・万/暦日・万円/暦日）が detail.html から
    一掃されていること。
    ※ 新単位「百万円/営業日」「百万円/暦日」はそれぞれ部分文字列として
    「万円/営業日」「万円/暦日」を含む（百+万円/...）ため、直前が「百」の
    ものは新単位の一部として除外し、それ以外（旧単位そのもの）だけを検知する。
  - 新キー daily_pace_mm が detail.html 側で使われていること。

実行: リポジトリルートで
    .venv/bin/python -m unittest tests/test_profit_kpi_units_template_guard.py -v
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DETAIL_HTML_PATH = Path(__file__).resolve().parent.parent / "app" / "templates" / "detail.html"

# 単純部分一致でよいもの（「百万円/...」の部分文字列にはならない）。
_OLD_UNIT_LITERALS_PLAIN = ("万/営業日", "万/暦日")
# 「百万円/...」の部分文字列と衝突するため、直前が「百」ではないものだけを検知する。
_OLD_UNIT_LITERALS_LOOKBEHIND = ("万円/営業日", "万円/暦日")


class TestDetailHtmlUnitGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = DETAIL_HTML_PATH.read_text(encoding="utf-8")

    def test_old_unit_literals_absent(self):
        for lit in _OLD_UNIT_LITERALS_PLAIN:
            self.assertNotIn(lit, self.html, f"旧単位表記 {lit!r} が detail.html に残存")
        for lit in _OLD_UNIT_LITERALS_LOOKBEHIND:
            pattern = re.compile(r"(?<!百)" + re.escape(lit))
            m = pattern.search(self.html)
            self.assertIsNone(m, f"旧単位表記 {lit!r}（百万円の一部ではない単独出現）が detail.html に残存: {m}")

    def test_new_key_present(self):
        self.assertIn("daily_pace_mm", self.html,
                       "新キー daily_pace_mm が detail.html で使われていない")


if __name__ == "__main__":
    unittest.main()
