"""B11（白黒生存性）: 達成ゾーンの斜線ハッチ・KPI記号（▲/―）併記のテスト。

改修プラン_訴求力強化.md §6-B B11／設計_訴求力強化_B_表示系.md B11 節。
  1. render_trend_svg（app/lib/hospital_summary.py）: 未達ゾーンの塗りが
     `url(#wrHatch)` になり、`<defs><pattern id="wrHatch">` がSVG冒頭に1回だけ出る
     （達成ゾーンは従来どおり OK_FILL の無地）。
  2. dept_report.html の KPI 値: `k.ok=True` で▲、`k.ok=False` で―、
     `k.ok is None`（目標未設定）は記号なしで出る（現行 k.ok は真偽値2値のみのため
     ▼危険は対象外。設計注記のとおり）。

実行: リポジトリルートで
    python -m pytest tests/test_hospital_summary_wr_hatch.py -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib import hospital_summary as hs  # noqa: E402

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "app" / "templates"


class RenderTrendSvgHatchTest(unittest.TestCase):
    def setUp(self):
        # seg0 (10,12) avg11>=9 → 達成(OK)／seg1 (12,5) avg8.5<9 → 未達(WR・ハッチ)
        self.data = {"dates": ["2026-01-01", "2026-01-08", "2026-01-15"],
                     "cur": [10, 12, 5], "prev": [9, 9, 9]}
        self.svg = hs.render_trend_svg(self.data, 9, "目標9", "人", "テスト窓")

    def test_defs_and_pattern_appear_exactly_once(self):
        self.assertEqual(self.svg.count("<defs>"), 1)
        self.assertEqual(self.svg.count(f'<pattern id="{hs.WR_HATCH_ID}"'), 1)

    def test_wr_zone_fill_is_hatch_pattern(self):
        self.assertIn(f'fill="url(#{hs.WR_HATCH_ID})"', self.svg)

    def test_ok_zone_fill_is_unchanged_solid(self):
        self.assertIn(f'fill="{hs.OK_FILL}"', self.svg)

    def test_wr_fill_color_only_used_inside_pattern_definition(self):
        # 未達ポリゴン自体の fill 属性値としては WR_FILL の生値が直接出ない
        # （pattern の <rect> 内でのみ使う）＝ハッチに完全に置き換わっていることの確認。
        self.assertEqual(self.svg.count(hs.WR_FILL), 1)


class DeptReportKpiSymbolTest(unittest.TestCase):
    """dept_report.html の実テンプレートを Jinja でレンダーし、記号併記を確認する。"""

    @classmethod
    def setUpClass(cls):
        from jinja2 import Environment, FileSystemLoader
        cls.env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=False)

    def _render(self, ok):
        from app.lib.dept_report import _kpi
        tmpl = self.env.get_template("dept_report.html")
        sheet = {
            "hospital_name": None, "unit": "テスト科", "type_label": "内科系・診療科版",
            "subtitle": "診療科パフォーマンスレポート", "base_date": "2026/09/06",
            "generated_at": "2026/09/07", "qr_svg": None, "prio_text": "A 在院",
            "axis": "dept",
            "kpis": [_kpi("在院患者数", "直近7日平均", "42.0", "人", lead=True,
                          tgt="目標 40", ok=ok)],
            "charts": [{"kind": "A", "name": "在院患者数", "badge": None, "note": "",
                       "is_dow": False, "svg": "<svg>FAKE</svg>", "priority": 1}],
            "move": {"body": "テスト本文", "action": "テストアクション"},
        }
        return tmpl.render(review=False, sheets=[sheet], extra_pages=[])

    def test_ok_true_renders_achieved_shape(self):
        html = self._render(True)
        self.assertIn('<span class="k-shape">▲</span>', html)

    def test_ok_false_renders_warn_shape(self):
        html = self._render(False)
        self.assertIn('<span class="k-shape">―</span>', html)

    def test_ok_none_renders_no_shape(self):
        html = self._render(None)
        # CSS定義（.k-shape{...}）自体は常に出るため、記号span要素の不在で判定する。
        self.assertNotIn('<span class="k-shape">', html)


if __name__ == "__main__":
    unittest.main()
