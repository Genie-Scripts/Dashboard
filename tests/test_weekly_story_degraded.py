"""weekly_story 欠落時の縮退表示（D3裁定(a)=表示する）のユニットテスト。

⚠️ PUBLIC リポにつき、実在の診療科名・実際のレポート文は書かない。合成データのみ。
LLM(oMLX)・常駐サーバは一切呼ばない（jinja2 のテンプレートレンダーのみ・
generate_html.py の main() は呼ばず `_build_jinja_env()` を再利用するだけ）。

★訴求力強化 Phase 2 B1（週次ストーリー昇格）でstory/縮退文は sb-detail の外
（sb-row直下・常時表示）へ移設した。トグル「▼詳細」とその中身 id="sb-detail" は
diffs のみに限定（story/縮退文はもはやトグルの中に無い）。判定マトリクスの表示先を
更新（トグル自体の有無は diffs の有無のみで決まる）:
  story あり                      → 要約本文を常時表示（sb-detail外・縮退文なし）
  diffs あり + story なし         → 縮退1行を常時表示（LLM要約失敗の可視化・sb-detail外）
  failed=True（生成例外）         → 縮退1行を常時表示（sb-detail外。diffsが無ければトグル自体も出ない）
  weekly_story=None / 初回 / 差分なし週 → 縮退文・トグルとも出ない（正当な欠落＝失敗ではない）
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jinja2 import Environment, ChainableUndefined

from generate_html import _build_jinja_env

_DEGRADED_TEXT = "今週のストーリー要約は生成できませんでした"
# B1以降 class="sb-story sb-degraded sb-story-top" となるため末尾を固定しない部分一致にする
_DEGRADED_MARKER = 'sb-story sb-degraded'
_DETAIL_MARKER = 'id="sb-detail"'


def _template_env() -> Environment:
    base_env = _build_jinja_env()
    env = Environment(loader=base_env.loader, undefined=ChainableUndefined,
                      autoescape=base_env.autoescape)
    env.filters.update(base_env.filters)
    return env


def _render_portal(weekly_story=None) -> str:
    tmpl = _template_env().get_template("portal.html")
    ctx = {
        "base_date": "2026-08-01", "generated_at": "2026/08/01 09:00",
        "headline": {"level": "ok", "icon": "🏥", "text": "テスト見出し"},
        "kpi_cards": [],
        "weekly_story": weekly_story,
    }
    return tmpl.render(**ctx)


class TestWeeklyStoryDegradedDisplay(unittest.TestCase):
    def test_story_present_renders_story_without_degraded(self):
        html = _render_portal({"base_date": "2026-08-01", "prior_date": "2026-07-25",
                               "diffs": ["合成差分1"], "story": "合成の週次要約文。"})
        self.assertIn("合成の週次要約文。", html)
        self.assertNotIn(_DEGRADED_TEXT, html)
        self.assertNotIn(_DEGRADED_MARKER, html)

    def test_diffs_without_story_shows_degraded_line(self):
        """LLM要約だけ失敗（narrate が None）→ 縮退1行が出る。"""
        html = _render_portal({"base_date": "2026-08-01", "prior_date": "2026-07-25",
                               "diffs": ["合成差分1"], "story": None})
        self.assertIn(_DEGRADED_TEXT, html)
        self.assertIn(_DEGRADED_MARKER, html)

    def test_failed_marker_shows_degraded_line(self):
        """build_weekly_story 全体が例外（generate_html の except 経由）→ 縮退1行が出る。
        B1以降、縮退文は sb-detail の外(常時表示)。diffsが空ならトグル自体も出ない。"""
        html = _render_portal({"base_date": None, "prior_date": None,
                               "diffs": [], "story": None, "failed": True})
        self.assertIn(_DEGRADED_TEXT, html)
        self.assertNotIn(_DETAIL_MARKER, html)  # diffsが空＝折り畳む中身が無いためトグル自体なし

    def test_failed_marker_with_diffs_also_shows_toggle(self):
        """failed=True かつ diffs もあれば、縮退文(常時表示)とは別にdiffsのトグルも出る。"""
        html = _render_portal({"base_date": None, "prior_date": None,
                               "diffs": ["合成差分1"], "story": None, "failed": True})
        self.assertIn(_DEGRADED_TEXT, html)
        self.assertIn(_DETAIL_MARKER, html)

    def test_none_weekly_story_renders_nothing(self):
        """weekly_story 未生成（そもそも呼ばれない構成）→ セクションなし・縮退文なし。"""
        html = _render_portal(None)
        self.assertNotIn(_DEGRADED_TEXT, html)
        self.assertNotIn(_DETAIL_MARKER, html)

    def test_legitimate_absence_renders_nothing(self):
        """初回（prior無し）/ 差分なし週 = diffs空・failed無し → 正当な欠落は縮退表示しない。"""
        html = _render_portal({"base_date": "2026-08-01", "prior_date": None,
                               "diffs": [], "story": None})
        self.assertNotIn(_DEGRADED_TEXT, html)
        self.assertNotIn(_DETAIL_MARKER, html)


if __name__ == "__main__":
    unittest.main()
