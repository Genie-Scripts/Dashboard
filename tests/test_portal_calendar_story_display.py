"""B1（portal 到達距離短縮）のテンプレート断面レンダーテスト。

`tests/test_ai_alerts_display.py` と同じ手法（`_build_jinja_env()` + `ChainableUndefined` で
portal.html を部分レンダーし文字列アサーション）。常駐サーバ・LLMは一切呼ばない。

対象:
  - calendar_preview.early/week の chip が視覚チップ（class="chg-chip warn"）として出る
  - weekly_story.story が id="sb-detail"（折り畳み）の外側に常時表示される
  - weekly_story.diffs が空のとき ▼ 詳細 ボタン自体が出ない
  - 木・金（来週の暦が発火する週）でも来週チップが変化点バナーより前に出る

実行: リポジトリルートで
    python -m pytest tests/test_portal_calendar_story_display.py -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jinja2 import Environment, ChainableUndefined

from generate_html import _build_jinja_env


def _template_env() -> Environment:
    base_env = _build_jinja_env()
    env = Environment(loader=base_env.loader, undefined=ChainableUndefined,
                      autoescape=base_env.autoescape)
    env.filters.update(base_env.filters)
    return env


def _base_ctx(**overrides) -> dict:
    ctx = {
        "base_date": "2026-08-01", "generated_at": "2026/08/01 09:00",
        "headline": {"level": "ok", "icon": "🏥", "text": "テスト見出し"},
        "kpi_cards": [],
        "ai_alerts": [],
    }
    ctx.update(overrides)
    return ctx


def _render(**overrides) -> str:
    tmpl = _template_env().get_template("portal.html")
    return tmpl.render(**_base_ctx(**overrides))


class CalendarChipDisplayTest(unittest.TestCase):
    def test_early_chip_renders_as_visual_badge(self):
        html = _render(calendar_preview={
            "early": {"text": "連休が近づいています", "chip": ["連休", "9/19-23"]},
        })
        self.assertIn('class="chg-chip warn"', html)
        self.assertIn("9/19-23", html)

    def test_week_chip_renders_as_visual_badge(self):
        html = _render(calendar_preview={
            "week": {"text": "来週は営業日が4日です", "chip": ["来週", "営業日4日"]},
        })
        self.assertIn('class="chg-chip warn"', html)
        self.assertIn("営業日4日", html)

    def test_week_chip_precedes_changes_banner(self):
        """木・金は来週の暦帯が発火する。変化点バナーより前に出ること。"""
        html = _render(
            calendar_preview={"week": {"text": "来週は営業日が4日です", "chip": ["来週", "営業日4日"]}},
            changes={"quiet": True, "prev_date": "7/31", "triage_in": [], "triage_out": [], "kpi": []},
        )
        idx_week = html.find("営業日4日")
        idx_changes = html.find("昨日から大きな変化はありません")
        self.assertGreater(idx_week, 0)
        self.assertGreater(idx_changes, 0)
        self.assertLess(idx_week, idx_changes)


class WeeklyStoryPromotionTest(unittest.TestCase):
    def test_story_text_outside_sb_detail_when_present(self):
        html = _render(weekly_story={"story": "今週はテストの週次要約です。",
                                     "diffs": ["手術室稼働率が上昇"]})
        idx_story = html.find("今週はテストの週次要約です。")
        idx_detail = html.find('id="sb-detail"')
        self.assertGreater(idx_story, 0)
        self.assertGreater(idx_detail, 0)
        self.assertLess(idx_story, idx_detail,
                        "週次ストーリー本文はsb-detail(折り畳み)の外側に出ていなければならない")

    def test_no_toggle_button_when_diffs_empty_and_story_present(self):
        html = _render(weekly_story={"story": "今週はテストの週次要約です。", "diffs": []})
        self.assertNotIn('id="sb-toggle-btn"', html)
        self.assertNotIn('id="sb-detail"', html)

    def test_toggle_button_present_when_diffs_exist(self):
        html = _render(weekly_story={"story": "本文", "diffs": ["手術室稼働率が上昇"]})
        self.assertIn('id="sb-toggle-btn"', html)
        self.assertIn('id="sb-detail"', html)
        # diffsは折り畳みの中（storyより後ろ）
        idx_story = html.find("本文")
        idx_diff = html.find("手術室稼働率が上昇")
        self.assertLess(idx_story, idx_diff)

    def test_degraded_message_shown_outside_fold_when_failed(self):
        html = _render(weekly_story={"failed": True, "diffs": []})
        self.assertIn("今週のストーリー要約は生成できませんでした", html)
        idx_msg = html.find("今週のストーリー要約は生成できませんでした")
        idx_detail = html.find('id="sb-detail"')
        self.assertGreater(idx_msg, 0)
        self.assertEqual(idx_detail, -1)  # diffsも無いので折り畳み自体が出ない


if __name__ == "__main__":
    unittest.main()
