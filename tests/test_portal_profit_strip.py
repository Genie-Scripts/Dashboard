"""P1（portal 帯の配線＋detail JSON への profit_headline 同梱）の回帰テスト。

密閉（実データ・常駐サーバ・LLM・ネットワークは一切使わない）。対象:
  1. build_portal_context() の新kwargs（profit_hybrid/profit_breakdown/
     profit_targets_breakdown）が全部Noneのとき、戻り値の構造が従来と同一であること
     （additive・既存キー不変）。
  2. portal.html の粗利ヘッドライン帯（.profit-strip）がテンプレート断面レンダーで
     正しく出る／profit_headline=None なら一切出ないこと。
  3. 480pxメディアクエリのブロック数が変わっていないこと（新規ブロックを作らない規律）。
  4. build_detail_json() の出力に profit_headline が同梱され、profit_unit /
     month_projection と数値が一致すること。dept.html 用（strip_detail_only_json）には
     含まれないこと。

adm/surg/profit_breakdown フィクスチャは tests/test_profit_hybrid_extraction.py を
流用する（同一ディレクトリの兄弟テストモジュールを直接 import）。

実行: リポジトリルートで
    OMLX_BASE_URL=http://127.0.0.1:9 OMLX_BASE=http://127.0.0.1:9 \
    .venv/bin/python -m pytest tests/test_portal_profit_strip.py -q
"""
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jinja2 import Environment, ChainableUndefined

from generate_html import _build_jinja_env
from app.lib.html_builder import (
    build_portal_context, build_detail_json, build_profit_hybrid_calibrated,
    strip_detail_only_json,
)
from app.lib.profit_estimate import last_complete_driver_date

import test_profit_hybrid_extraction as fx  # noqa: E402  フィクスチャ流用

PORTAL_HTML = Path(__file__).resolve().parent.parent / "app" / "templates" / "portal.html"


def _template_env() -> Environment:
    base_env = _build_jinja_env()
    env = Environment(loader=base_env.loader, undefined=ChainableUndefined,
                      autoescape=base_env.autoescape)
    env.filters.update(base_env.filters)
    return env


def _render_portal(**extra) -> str:
    ctx = {
        "base_date": "2026-09-16", "generated_at": "2026/09/16 09:00",
        "headline": {"level": "ok", "icon": "🏥", "text": "テスト見出し"},
        "kpi_cards": [],
    }
    ctx.update(extra)
    tmpl = _template_env().get_template("portal.html")
    return tmpl.render(**ctx)


FAKE_PROFIT_HEADLINE = {
    "month": "2026-09",
    "as_of": "2026-09-16",
    "nyuin": {
        "ppd": 34567, "patient_days": 12345, "profit_mm": 1461.4, "target_mm": 1400.0,
        "achievement_pct": 103.6, "vs_prev_pct": 2.3, "direction": "up",
    },
    "gairai": {
        "ppd_biz": 5.23, "biz_days": 20, "profit_mm": 351.2,
        "target_mm_nominal": 400.0, "target_mm": 400.0,
        "target_per_biz_day": 5.0, "achievement_pct": 88.1, "vs_prev_pct": -1.1, "direction": "down",
    },
    "total": {
        "proj_mm": 1812.6, "target_mm": 1800.0, "rate": 104.3,
        "status": {"css": "ok", "shape": "▲", "text": "達成"},
    },
    "latest": None,
    "prev_month_pending": None,
    "identity_ok": True,
    "target_identity_ok": True,
    "labels": {
        "main_nyuin": "入院 粗利/人日", "main_gairai": "外来 粗利/営業日",
        "target": "月次目標", "target_gairai": "月次目標(補正)", "tolerance": "（±2%）",
        "note": "TESTNOTE_粗利ヘッドライン注記マーカー",
        "guard": "TESTGUARD_ガードマーカー",
        "scope_nyuin": "入院のみ・全科", "scope_gairai": "外来のみ・全科",
        "period": "2026年9月 月末見込み（9/16時点・診療実績ベース・暫定）",
        "cmp": "前月比",
    },
}


# ═══════════════════════════════════════
# 1. build_portal_context の additive 確認
# ═══════════════════════════════════════

class BuildPortalContextAdditiveTest(fx._FixtureMixin, unittest.TestCase):
    def _call(self, **kwargs):
        return build_portal_context(
            self.adm, self.surg, fx.TARGETS, fx.SURG_TARGETS, fx.BASE_DATE,
            fx.GENERATED_AT, include_ai_alerts=False, include_triage=False,
            profit_monthly=self.profit_monthly, kpi_history_path=None,
            **kwargs,
        )

    def test_omitted_and_explicit_none_kwargs_are_identical(self):
        ctx_omitted = self._call()
        ctx_explicit_none = self._call(
            profit_hybrid=None, profit_breakdown=None, profit_targets_breakdown=None,
        )
        self.assertEqual(ctx_omitted, ctx_explicit_none)

    def test_profit_headline_is_none_when_kwargs_omitted(self):
        ctx = self._call()
        self.assertIn("profit_headline", ctx)
        self.assertIsNone(ctx["profit_headline"])
        # 既存キーは不変（数の目安として代表キーの存在を確認）
        for key in ("headline", "kpi_cards", "triage", "attention", "improvement",
                   "ai_alerts", "changes", "calendar_preview", "freshness"):
            self.assertIn(key, ctx)
        self.assertEqual(len(ctx["kpi_cards"]), 3)

    def test_other_keys_unaffected_when_profit_hybrid_populated(self):
        profit_base_date = last_complete_driver_date(self.adm, self.surg) or fx.BASE_DATE
        hybrid = build_profit_hybrid_calibrated(
            self.profit_breakdown, self.surg, self.adm, profit_base_date)
        self.assertIsNotNone(hybrid[0], "フィクスチャ不備（hybrid が退化）")

        ctx_none = self._call()
        ctx_populated = self._call(
            profit_hybrid=hybrid, profit_breakdown=self.profit_breakdown,
            profit_targets_breakdown=None,
        )
        # profit_headline 以外のキーはバイト単位で不変
        keys_a = set(ctx_none) - {"profit_headline"}
        keys_b = set(ctx_populated) - {"profit_headline"}
        self.assertEqual(keys_a, keys_b)
        for k in keys_a:
            self.assertEqual(ctx_none[k], ctx_populated[k], f"key={k} が profit_hybrid 付与で変化した")
        # profit_headline 自体は非None化する（フィクスチャが非退化なら）
        self.assertIsNotNone(ctx_populated["profit_headline"])


# ═══════════════════════════════════════
# 2. portal.html テンプレート断面レンダー
# ═══════════════════════════════════════

class PortalProfitStripTemplateTest(unittest.TestCase):
    def test_strip_renders_main_values_and_labels(self):
        html = _render_portal(profit_headline=FAKE_PROFIT_HEADLINE)
        self.assertIn('class="profit-strip"', html)
        self.assertIn("入院 粗利/人日", html)
        self.assertIn("外来 粗利/営業日", html)
        self.assertIn("2026年9月 月末見込み（9/16時点・診療実績ベース・暫定）", html)
        self.assertIn("TESTNOTE_粗利ヘッドライン注記マーカー", html)
        self.assertIn("34,567", html)          # nyuin.ppd の3桁カンマ
        self.assertIn("5.23", html)            # gairai.ppd_biz

    def test_profit_sub_values_have_thousands_separator(self):
        html = _render_portal(profit_headline=FAKE_PROFIT_HEADLINE)
        self.assertIn("1,461.4", html)   # 入院粗利（profit_mm）の3桁カンマ・小数1桁
        self.assertIn("351.2", html)     # 外来粗利（profit_mm、3桁未満はカンマ無し）
        self.assertIn("1,812.6", html)   # 合計（total.proj_mm）の3桁カンマ・小数1桁

    def test_guard_label_is_not_rendered_on_portal(self):
        html = _render_portal(profit_headline=FAKE_PROFIT_HEADLINE)
        self.assertNotIn("TESTGUARD_ガードマーカー", html)

    def test_badges_only_on_nyuin_and_gairai_achievement(self):
        html = _render_portal(profit_headline=FAKE_PROFIT_HEADLINE)
        # nyuin.achievement_pct=103.6(>=100→ok達成) / gairai.achievement_pct=88.1(<90→dr未達)
        self.assertIn('class="profit-badge ok">▲達成', html)
        self.assertIn('class="profit-badge dr">▼未達', html)
        self.assertEqual(html.count('class="profit-badge'), 2)

    def test_no_markup_when_profit_headline_none(self):
        html = _render_portal(profit_headline=None)
        self.assertNotIn('class="profit-strip"', html)

    def test_no_markup_when_profit_headline_absent(self):
        html = _render_portal()
        self.assertNotIn('class="profit-strip"', html)


# ═══════════════════════════════════════
# 3. 480px メディアクエリのブロック数不変
# ═══════════════════════════════════════

class Mobile480BlockCountUnchangedTest(unittest.TestCase):
    def test_only_one_480px_media_query_block(self):
        src = PORTAL_HTML.read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"@media\(max-width:480px\)\{", src)), 1)

    def test_480px_block_still_matches_existing_regex_pattern(self):
        # tests/test_portal_kpi_mobile_layout.py と同じ非貪欲マッチ
        src = PORTAL_HTML.read_text(encoding="utf-8")
        m = re.search(r"@media\(max-width:480px\)\{(.*?)\n\}", src, re.S)
        self.assertIsNotNone(m)
        self.assertIn(".kpi{grid-template-columns:1fr}", m.group(1))


# ═══════════════════════════════════════
# 4. build_detail_json への profit_headline 同梱
# ═══════════════════════════════════════

class BuildDetailJsonProfitHeadlineTest(fx._FixtureMixin, unittest.TestCase):
    def test_profit_headline_present_and_consistent_with_profit_unit_and_month_projection(self):
        raw = self._call_detail_json()
        data = json.loads(raw)

        self.assertIn("profit_headline", data)
        ph = data["profit_headline"]
        self.assertIsNotNone(ph, "フィクスチャ不備（profit_headline が退化）")

        pu_projection = data["profit_unit"]["global"]["projection"]
        self.assertEqual(ph["nyuin"]["ppd"], pu_projection["ppd"])

        mp_profit = data["month_projection"]["profit"]
        self.assertEqual(ph["total"]["proj_mm"], mp_profit["projection"])

    def test_profit_headline_absent_from_dept_json(self):
        raw = self._call_detail_json()
        dept_raw = strip_detail_only_json(raw)
        dept_data = json.loads(dept_raw)
        self.assertNotIn("profit_headline", dept_data)
        # detail 側には引き続き存在すること（剥がし対象を取り違えていないことの対照確認）
        self.assertIn("profit_headline", json.loads(raw))


if __name__ == "__main__":
    unittest.main()
