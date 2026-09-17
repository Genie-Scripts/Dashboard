"""粗利ヘッドライン §7 改修（入院 粗利/人日を主役へ）のテンプレ断面検査。

`tests/test_chart_caption_placeholders.py` と同じ手法（Jinjaで detail.html を素のまま
レンダーし、文言・関数呼び出し回数をソースごと検査する）。LLM(oMLX)・常駐サーバは
一切呼ばない。DATA.profit_headline は本改修時点ではビルド未配線（後続ワーカーが配線
予定）のため、参照は必ず `DATA.profit_headline &&` の存在チェック付きであることも検査
する（fail-soft の密閉検査）。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from generate_html import _build_jinja_env

ROOT = Path(__file__).resolve().parent.parent
DETAIL_HTML = ROOT / "app" / "templates" / "detail.html"


def _render_detail_html() -> str:
    env = _build_jinja_env()
    return env.get_template("detail.html").render()


class PpdModeLiteralsTest(unittest.TestCase):
    """'ppd' 分岐（入院 粗利/人日が主役）のリテラルが出ること。"""

    def test_main_label_literal(self):
        html = _render_detail_html()
        self.assertIn("入院 粗利/人日", html)

    def test_rate_label_literal(self):
        html = _render_detail_html()
        self.assertIn("目標比（入院粗利）", html)

    def test_period_note_scope_literal(self):
        html = _render_detail_html()
        self.assertIn("・入院のみ・全科", html)

    def test_tab_unit_literal(self):
        html = _render_detail_html()
        self.assertIn("円/人日", html)

    def test_tab_strip_short_subline_literal(self):
        # チップ幅で右端が切れていた副行を短縮形に変更済みであること。
        src = DETAIL_HTML.read_text(encoding="utf-8")
        self.assertIn("📍 合計 ", src)


class TotalAndConfirmedModeLiteralsKeptTest(unittest.TestCase):
    """'total'／'confirmed' 分岐の現行リテラルが残っていること。"""

    def test_total_confirmed_label_literal_present(self):
        html = _render_detail_html()
        self.assertIn("粗利（全科合計）", html)

    def test_total_confirmed_label_appears_twice(self):
        # 'total' 分岐の main.label と 'confirmed' 分岐の main.label の両方が残る。
        src = DETAIL_HTML.read_text(encoding="utf-8")
        self.assertEqual(src.count("label:'粗利（全科合計）'"), 2)


class ProfitHeadlineModeSharedHelperTest(unittest.TestCase):
    """モード判定ヘルパーが renderSummary・renderTabs の両方から参照されていること。"""

    def test_helper_defined(self):
        src = DETAIL_HTML.read_text(encoding="utf-8")
        self.assertIn("function profitHeadlineMode()", src)

    def test_helper_referenced_at_least_three_times(self):
        # 定義1 + renderTabs呼び出し1 + renderSummary呼び出し1 = 最低3回。
        src = DETAIL_HTML.read_text(encoding="utf-8")
        self.assertGreaterEqual(src.count("profitHeadlineMode("), 3)


class ProfitHeadlineGuardedAccessTest(unittest.TestCase):
    """DATA.profit_headline への参照は必ず存在チェック付きであること（fail-soft密閉）。"""

    def test_profit_headline_access_is_guarded(self):
        src = DETAIL_HTML.read_text(encoding="utf-8")
        self.assertIn("DATA.profit_headline &&", src)
        # DATA.profit_headline を参照している行は、必ず同じ行に
        # `DATA.profit_headline &&` の存在チェックを伴うこと（`a && a.b` 形含む）。
        lines_with_ref = [ln for ln in src.splitlines() if "DATA.profit_headline" in ln]
        self.assertTrue(lines_with_ref, "DATA.profit_headline の参照が見つからない")
        for ln in lines_with_ref:
            with self.subTest(line=ln):
                self.assertIn("DATA.profit_headline &&", ln)


if __name__ == "__main__":
    unittest.main()


class TestOutpatientRefUsesCanonicalRate(unittest.TestCase):
    """外来 粗利/営業日 の参照カードは、丸め後の curr/refTarget から率を再計算せず
    正本（profit_headline.gairai.achievement_pct）の率を refRate で渡す（portal と率が揃う）。"""

    def test_ref_rate_override_wired(self):
        src = (ROOT / "app" / "templates" / "detail.html").read_text(encoding="utf-8")
        self.assertIn("refRate:gairai.achievement_pct", src)
        self.assertIn("r.refRate != null ? r.refRate", src)

    def test_ref_target_label_is_corrected(self):
        # 外来 ref は営業日補正後の target_per_biz_day を渡すため、ラベルも
        # 補正時の既存文言「補正目標」（confirmedRef の pAdj!=null 分岐と同じ）に揃える。
        src = (ROOT / "app" / "templates" / "detail.html").read_text(encoding="utf-8")
        self.assertIn(
            "curr:gairai.ppd_biz, refTarget:gairai.target_per_biz_day, refTargetLabel:'補正目標',",
            src,
        )
