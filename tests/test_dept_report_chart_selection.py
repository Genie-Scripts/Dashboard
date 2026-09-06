"""部門レポートPDF「B10 1枚1メッセージ」: 3図選定アルゴリズムの単体テスト。

裁定4（改修プラン_訴求力強化.md §0-4）で PDF の図を 5枚→3枚（ヒーロー1＋全幅2）に
絞る。選定規則（設計_訴求力強化_B_表示系.md B10）:
  per-unit（surgical/internal/ward）: ①ヒーロー(先頭)は必ず残す②一手のトピックに
  対応する図（_TOPIC_CHART_KIND）があれば必ず残す③残り1枠は元の優先順(TYPE_ORDER)
  で最も高いもの。3枚以下（ward）なら絞り込まない。
  hospital（病院全体サマリ）: ①ヒーロー(A在院)②D粗利は必須固定③一手のトピック対応図。

選定ロジックは dept_report.py の per-unit/hospital 両ループから呼べるよう
_pick_top3_charts / _pick_hospital_charts として切り出してある（合成の {"kind": ...}
辞書のみで検証・実データ不要）。

実行: リポジトリルートで
    python -m pytest tests/test_dept_report_chart_selection.py -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib import dept_report as dr  # noqa: E402


def _kinds(charts):
    return [c["kind"] for c in charts]


class PickTop3ChartsPerUnitTest(unittest.TestCase):
    """surgical: TYPE_ORDER=[C,D,E,B,A]（5枚）。"""

    def _ordered(self):
        return [{"kind": k} for k in dr.TYPE_ORDER["surgical"]]

    def test_surgery_topic_keeps_hero_plus_priority_order(self):
        # ヒーロー=C自体がトピック対応図を兼ねる → 残り2枠は優先順そのまま(D,E)
        picked = dr._pick_top3_charts(self._ordered(), "surgery")
        self.assertEqual(_kinds(picked), ["C", "D", "E"])

    def test_admission_topic_pulls_b_into_picked(self):
        # 一手=admission → Bチャート(新入院)を必ず含める
        picked = dr._pick_top3_charts(self._ordered(), "admission")
        self.assertEqual(_kinds(picked), ["C", "B", "D"])
        self.assertIn("B", _kinds(picked))

    def test_leveling_topic_pulls_e_into_picked(self):
        # 一手=leveling → Eチャート(曜日)を必ず含める
        picked = dr._pick_top3_charts(self._ordered(), "leveling")
        self.assertIn("E", _kinds(picked))
        self.assertEqual(picked[0]["kind"], "C")  # ヒーローは常に先頭
        self.assertLessEqual(len(picked), 3)

    def test_internal_admission_topic(self):
        # internal: TYPE_ORDER=[A,D,B,E] → ヒーローA、トピックadmission→{A,B,D}
        ordered = [{"kind": k} for k in dr.TYPE_ORDER["internal"]]
        picked = dr._pick_top3_charts(ordered, "admission")
        self.assertEqual(set(_kinds(picked)), {"A", "B", "D"})
        self.assertEqual(picked[0]["kind"], "A")

    def test_internal_leveling_topic(self):
        # internal: トピックleveling→{A,D,E}
        ordered = [{"kind": k} for k in dr.TYPE_ORDER["internal"]]
        picked = dr._pick_top3_charts(ordered, "leveling")
        self.assertEqual(set(_kinds(picked)), {"A", "D", "E"})

    def test_ward_three_or_fewer_is_untouched(self):
        # ward: TYPE_ORDER=[A,B,E]（3枚）は絞り込み対象外＝そのまま返す
        ordered = [{"kind": k} for k in dr.TYPE_ORDER["ward"]]
        picked = dr._pick_top3_charts(ordered, "leveling")
        self.assertEqual(_kinds(picked), ["A", "B", "E"])

    def test_empty_ordered_returns_empty(self):
        self.assertEqual(dr._pick_top3_charts([], "admission"), [])

    def test_result_always_at_most_three(self):
        for topic in ("admission", "surgery", "leveling", None):
            picked = dr._pick_top3_charts(self._ordered(), topic)
            self.assertLessEqual(len(picked), 3)
            self.assertEqual(picked[0]["kind"], "C")


class PickHospitalChartsTest(unittest.TestCase):
    """hospital: 常に {A(ヒーロー), D(粗利固定), トピック対応図} の最大3枚。"""

    def _charts(self):
        return [{"kind": k} for k in ("A", "B", "C", "D", "E")]

    def test_admission_topic_includes_b(self):
        picked = dr._pick_hospital_charts(self._charts(), "admission")
        self.assertEqual(set(_kinds(picked)), {"A", "D", "B"})

    def test_surgery_topic_includes_c(self):
        picked = dr._pick_hospital_charts(self._charts(), "surgery")
        self.assertEqual(set(_kinds(picked)), {"A", "D", "C"})

    def test_leveling_topic_includes_e(self):
        picked = dr._pick_hospital_charts(self._charts(), "leveling")
        self.assertEqual(set(_kinds(picked)), {"A", "D", "E"})

    def test_profit_chart_always_present_regardless_of_topic(self):
        # 病院全体サマリだけは D粗利 を財務の定点観測として毎回必ず残す
        for topic in ("admission", "surgery", "leveling"):
            picked = dr._pick_hospital_charts(self._charts(), topic)
            self.assertIn("D", _kinds(picked))

    def test_missing_profit_chart_degrades_to_two(self):
        # 粗利データが無い（D不在）月は2枚に減るだけで例外は出ない
        charts = [{"kind": k} for k in ("A", "B", "C", "E")]
        picked = dr._pick_hospital_charts(charts, "admission")
        self.assertEqual(set(_kinds(picked)), {"A", "B"})
        self.assertLessEqual(len(picked), 3)

    def test_topic_with_no_matching_chart_kind(self):
        # _TOPIC_CHART_KIND に無いトピック（該当図なし）→ ヒーロー＋Dの2枚のみ
        picked = dr._pick_hospital_charts(self._charts(), "unknown-topic")
        self.assertEqual(set(_kinds(picked)), {"A", "D"})

    def test_duplicate_kind_is_deduplicated(self):
        # 念のための重複除去（設計コメント通り、実運用では h_topic が D 自身になることは
        # 無いが、防御的に kind で去重されることを確認する）
        with mock.patch.dict(dr._TOPIC_CHART_KIND, {"custom": "D"}):
            picked = dr._pick_hospital_charts(self._charts(), "custom")
        self.assertEqual(_kinds(picked), ["A", "D"])  # D が2回選ばれても1枚だけ残る

    def test_result_always_at_most_three(self):
        for topic in ("admission", "surgery", "leveling", "unknown-topic"):
            picked = dr._pick_hospital_charts(self._charts(), topic)
            self.assertLessEqual(len(picked), 3)


class DeptReportTemplateGridTest(unittest.TestCase):
    """B10: 2列 `.row` grid をテンプレから撤去し、全幅の縦積みへ統一したことの回帰テスト。"""

    def setUp(self):
        self.html = Path(__file__).resolve().parent.parent.joinpath(
            "app", "templates", "dept_report.html").read_text(encoding="utf-8")

    def test_no_two_column_chart_grid_css(self):
        self.assertNotIn("grid-template-columns:1fr 1fr", self.html)

    def test_no_hero_wrap_or_move_wrap_classes(self):
        self.assertNotIn("hero-wrap", self.html)
        self.assertNotIn("move-wrap", self.html)

    def test_move_is_rendered_before_charts_in_grid(self):
        # 「この期間の一手」がチャート群より上（設計判断2: 最下段→上段）
        grid_start = self.html.index('<div class="grid">')
        move_pos = self.html.index("movecard(s.move, s)", grid_start)
        chart_pos = self.html.index("card(c, loop.first)", grid_start)
        self.assertLess(move_pos, chart_pos)


if __name__ == "__main__":
    unittest.main()
