"""weekly_digest.html（B12: PDF・掲示の完全週固定）のテンプレート断面レンダーテスト。

tests/test_ai_alerts_display.py と同じ手法（generate_html._build_jinja_env() を再利用し
テンプレート単体をレンダー）。実データ・ファイルI/O・LLMには依存しない。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from generate_html import _build_jinja_env


def _render_digest(**overrides) -> str:
    ctx = {
        "hospital_name": "",
        "base_date": "2026-08-30",
        "week_start": "2026-08-24",
        "week_end": "2026-08-30",
        "period_heading": "対象週 8/24(月)〜8/30(日)｜比較: その前の週 8/17〜8/23",
        "generated_at": "2026/08/31 07:39",
        "story": None,
        "diffs": [],
        "kpi_rows": [],
        "month_projection": [],
        "attention": {"dept_count": 0, "ward_count": 0, "worst3": []},
        "improvement": {"dept_internal": [], "dept_surgery": [], "ward": []},
        "calendar_preview": None,
        "qr_svg": None,
        "public_base_url": "https://hospital-dashboard-6ow.pages.dev/",
    }
    ctx.update(overrides)
    tmpl = _build_jinja_env().get_template("weekly_digest.html")
    return tmpl.render(**ctx)


class WeeklyDigestHeadingTest(unittest.TestCase):
    def test_period_heading_renders(self):
        html = _render_digest()
        self.assertIn("対象週 8/24(月)〜8/30(日)｜比較: その前の週 8/17〜8/23", html)

    def test_rolling7_wording_is_gone(self):
        html = _render_digest()
        self.assertNotIn("直近7日", html)


if __name__ == "__main__":
    unittest.main()
