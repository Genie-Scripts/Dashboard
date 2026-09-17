"""粗利ヘッドライン帯（P2: Comedix回覧板HTML・Comedix PNGカード・週次ダイジェスト）の
回帰テスト。

密閉（実データ・常駐サーバ・LLM・ネットワーク・Chrome起動は一切使わない）。対象:
  1. app.lib.hospital_summary.render_profit_strip / render_profit_strip_text
     （inline styleのみ・<script>/<svg>/class= 無し・ラベル/数値/ガード/回転3指標行の
     順序・バッジが目標比にだけ付くこと）。
  2. build_summary_context() の profit_headline kwarg が additive であること。
  3. scripts/build_comedix_html.py::build_fragment・scripts/build_comedix_card.py::
     build_html（いずれもHTML組み立ての純粋関数）に ctx を渡し、帯の有無が
     profit_headline の有無と一致すること。
  4. scripts/build_weekly_digest.py::render_txt（純関数）への profit_strip_text 差し込み。
  5. app/templates/weekly_digest.html が profit_strip_html 未指定でもレンダーできること。

実行: リポジトリルートで
    OMLX_BASE_URL=http://127.0.0.1:9 OMLX_BASE=http://127.0.0.1:9 \
    .venv/bin/python -m pytest tests/test_comedix_profit_strip.py -q
"""
import re
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib import hospital_summary as hs  # noqa: E402
from app.lib.config import status_display, TARGET_ADMISSION_WEEKLY, TARGET_GA_DAILY  # noqa: E402
from generate_html import _build_jinja_env  # noqa: E402

from scripts.build_comedix_html import build_fragment as chtml_build_fragment  # noqa: E402
from scripts.build_comedix_card import build_html as ccard_build_html  # noqa: E402
from scripts.build_weekly_digest import render_txt  # noqa: E402


# ═══════════════════════════════════════
# 偽の profit_headline dict
# （portal の tests/test_portal_profit_strip.py::FAKE_PROFIT_HEADLINE と同じ形。
#  nyuin.ppd だけ「82,601円」の書式検証がしやすい値に差し替える）
# ═══════════════════════════════════════
FAKE_PROFIT_HEADLINE = {
    "month": "2026-09",
    "as_of": "2026-09-16",
    "nyuin": {
        "ppd": 82601, "patient_days": 12345, "profit_mm": 1461.4, "target_mm": 1400.0,
        "achievement_pct": 104.4, "vs_prev_pct": 2.3, "direction": "up",
    },
    "gairai": {
        "ppd_biz": 5.23, "biz_days": 20, "profit_mm": 351.2,
        "target_mm_nominal": 400.0, "target_mm": 400.0,
        "target_per_biz_day": 5.0, "achievement_pct": 88.1, "vs_prev_pct": -1.1, "direction": "down",
    },
    "total": {
        "proj_mm": 1812.6, "target_mm": 1800.0, "rate": 103.3,
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

TURN_LINE = "回転：在院 572／目標575・新入院/日 51.6／必要55.3（あと3.7）・期間III超え 48人"

_BADGE_RE = re.compile(r'<span style="color:#[0-9a-f]{6};font-weight:700">')


# ═══════════════════════════════════════
# 1. render_profit_strip / render_profit_strip_text
# ═══════════════════════════════════════
class RenderProfitStripHtmlTest(unittest.TestCase):
    def setUp(self):
        self.html = hs.render_profit_strip(FAKE_PROFIT_HEADLINE, TURN_LINE)

    def test_no_forbidden_markup(self):
        self.assertNotIn("<script", self.html)
        self.assertNotIn("<svg", self.html)
        self.assertNotIn("class=", self.html)

    def test_labels_and_values_present(self):
        self.assertIn("入院 粗利/人日", self.html)
        self.assertIn("82,601円", self.html)
        self.assertIn("外来 粗利/営業日", self.html)
        self.assertIn("TESTGUARD_ガードマーカー", self.html)
        self.assertIn("TESTNOTE_粗利ヘッドライン注記マーカー", self.html)

    def test_turn_line_between_sub_row_and_guard(self):
        idx_sub = self.html.index("延患者数")
        idx_turn = self.html.index(TURN_LINE)
        idx_guard = self.html.index("TESTGUARD_ガードマーカー")
        self.assertLess(idx_sub, idx_turn)
        self.assertLess(idx_turn, idx_guard)

    def test_badges_only_on_targets_not_on_ppd(self):
        # バッジは合計の目標比にだけ付く（入院粗利・外来粗利には付けない）。
        self.assertEqual(len(_BADGE_RE.findall(self.html)), 1)
        # ppd の数値直後（次の40文字）にバッジ（▲/―/▼）が現れない。
        idx_ppd = self.html.index("82,601円")
        window = self.html[idx_ppd:idx_ppd + 40]
        self.assertNotIn("▲", window)
        self.assertNotIn("▼", window)
        self.assertNotIn("―", window)


class RenderProfitStripEdgeCaseTest(unittest.TestCase):
    def test_none_returns_empty_html(self):
        self.assertEqual(hs.render_profit_strip(None), "")

    def test_none_returns_empty_text(self):
        self.assertEqual(hs.render_profit_strip_text(None), "")

    def test_empty_dict_returns_empty(self):
        self.assertEqual(hs.render_profit_strip({}), "")


class RenderProfitStripTextTest(unittest.TestCase):
    def test_text_contains_labels_and_turn_line_in_order(self):
        txt = hs.render_profit_strip_text(FAKE_PROFIT_HEADLINE, TURN_LINE)
        self.assertIn("入院 粗利/人日", txt)
        self.assertIn("82,601円", txt)
        self.assertIn("TESTGUARD_ガードマーカー", txt)
        self.assertIn("TESTNOTE_粗利ヘッドライン注記マーカー", txt)
        idx_turn = txt.index(TURN_LINE)
        idx_guard = txt.index("TESTGUARD_ガードマーカー")
        self.assertLess(idx_turn, idx_guard)


# ═══════════════════════════════════════
# 2. build_summary_context の additive 確認
# ═══════════════════════════════════════
TARGETS = {
    "new_admission": {"dept": {}, "ward": {}},
    "inpatient": {"dept": {}, "ward": {}, "ward_beds": {}},
}
SURG_TARGETS = {}
BASE_DATE = pd.Timestamp("2026-09-16")


def _run_build_summary_context(**extra):
    """test_hospital_summary_profit_projection_wiring.py と同じ手法（重い依存を
    フェイクへ差し替え、配線だけを検証する）。profit_breakdown を渡さないため
    fit_profit_estimators は呼ばれない（モック不要）。"""
    patches = [
        mock.patch.object(hs, "build_hero_text", lambda *a, **k: {"headline": "", "body": "", "chips": []}),
        mock.patch.object(hs.metrics, "build_kpi_summary", lambda *a, **k: {}),
        mock.patch.object(hs, "_ma_series", lambda *a, **k: {"dates": [], "cur": [], "prev": []}),
        mock.patch.object(hs, "_surg_series", lambda *a, **k: {"dates": [], "cur": [], "prev": []}),
        mock.patch.object(hs.metrics, "build_ward_ranking", lambda *a, **k: pd.DataFrame()),
        mock.patch.object(hs.metrics, "weekend_census_retention", lambda *a, **k: {"units": [], "total": {}}),
        mock.patch.object(hs, "_flow_7d", lambda *a, **k: {}),
        mock.patch.object(hs.metrics, "build_dept_ranking", lambda *a, **k: pd.DataFrame()),
        mock.patch.object(hs.metrics, "build_surgery_ranking", lambda *a, **k: pd.DataFrame()),
        mock.patch.object(hs.metrics, "discharge_dow_profile", lambda *a, **k: {"redistribution": None}),
    ]
    with ExitStack() as st:
        for p in patches:
            st.enter_context(p)
        return hs.build_summary_context(
            pd.DataFrame(), pd.DataFrame(), TARGETS, SURG_TARGETS, BASE_DATE, **extra)


class BuildSummaryContextAdditiveTest(unittest.TestCase):
    def test_profit_headline_is_none_when_omitted(self):
        ctx = _run_build_summary_context()
        self.assertIn("profit_headline", ctx)
        self.assertIsNone(ctx["profit_headline"])

    def test_omitted_and_explicit_none_are_identical(self):
        ctx_omitted = _run_build_summary_context()
        ctx_explicit_none = _run_build_summary_context(profit_headline=None)
        self.assertEqual(ctx_omitted, ctx_explicit_none)

    def test_other_keys_unaffected_when_profit_headline_populated(self):
        ctx_none = _run_build_summary_context()
        ctx_populated = _run_build_summary_context(profit_headline=FAKE_PROFIT_HEADLINE)
        keys_a = set(ctx_none) - {"profit_headline"}
        keys_b = set(ctx_populated) - {"profit_headline"}
        self.assertEqual(keys_a, keys_b)
        for k in keys_a:
            self.assertEqual(ctx_none[k], ctx_populated[k], f"key={k} が profit_headline 付与で変化した")
        self.assertEqual(ctx_populated["profit_headline"], FAKE_PROFIT_HEADLINE)

    def test_turn_line_key_present_and_string(self):
        ctx = _run_build_summary_context()
        self.assertIn("turn_line", ctx)
        self.assertIsInstance(ctx["turn_line"], str)


# ═══════════════════════════════════════
# 3. Comedix HTML/PNGカードの組み立て純粋関数
# ═══════════════════════════════════════
FAKE_KPI = {
    "inpatient_avg_7d_wd": 560.0, "inpatient_avg_7d_hd": 540.0,
    "admission_actual_7d": 60, "admission_gap": 5.0, "admission_status": status_display(95),
    "admission_rate_7d": 95.0, "admission_target_weekly_adj": TARGET_ADMISSION_WEEKLY,
    "operation_daily_avg": 10.0, "operation_gap": -1.0, "operation_status": status_display(90),
    "operation_rate": 90.0,
}


def _fake_ctx(profit_headline=None, turn_line=None):
    return {
        "base_date": BASE_DATE,
        "hero": {"chips": []},
        "kpi": FAKE_KPI,
        "trends": {"inpatient": {"dates": ["09/01", "09/08"], "cur": [550.0, 560.0], "prev": [None, None]}},
        "ward_rows": [],
        "dept_rows": [],
        "profit_headline": profit_headline,
        "turn_line": turn_line,
    }


class ComedixHtmlFragmentTest(unittest.TestCase):
    """scripts/build_comedix_html.py::build_fragment（inline-only断片の組み立て純粋関数）。"""

    def test_no_strip_when_profit_headline_none(self):
        frag = chtml_build_fragment(_fake_ctx(None), "見出し", "本文", "<div>trend</div>")
        self.assertNotIn("入院 粗利/人日", frag)

    def test_strip_present_when_profit_headline_given(self):
        frag = chtml_build_fragment(
            _fake_ctx(FAKE_PROFIT_HEADLINE, TURN_LINE), "見出し", "本文", "<div>trend</div>")
        self.assertIn("入院 粗利/人日", frag)
        self.assertIn("82,601円", frag)
        self.assertIn(TURN_LINE, frag)
        # 帯そのものに <script>/class= が無いこと（<svg> はComedix説明コメント中に元々
        # 出現するため、この断片全体では検査しない。帯単体の検査は
        # RenderProfitStripHtmlTest.test_no_forbidden_markup が担う）。
        strip_html = hs.render_profit_strip(FAKE_PROFIT_HEADLINE, TURN_LINE)
        self.assertIn(strip_html, frag)
        self.assertNotIn("<script", strip_html)
        self.assertNotIn("class=", strip_html)


class ComedixCardHtmlTest(unittest.TestCase):
    """scripts/build_comedix_card.py::build_html（PNG化前のHTML組み立て純粋関数）。"""

    def test_no_strip_when_profit_headline_none(self):
        html = ccard_build_html(_fake_ctx(None), "見出し", "本文", 760)
        self.assertNotIn("入院 粗利/人日", html)

    def test_strip_present_when_profit_headline_given(self):
        html = ccard_build_html(_fake_ctx(FAKE_PROFIT_HEADLINE, TURN_LINE), "見出し", "本文", 760)
        self.assertIn("入院 粗利/人日", html)
        self.assertIn("82,601円", html)
        self.assertIn(TURN_LINE, html)


# ═══════════════════════════════════════
# 4. scripts/build_weekly_digest.py::render_txt（純関数）
# ═══════════════════════════════════════
def _digest_txt_ctx(profit_strip_text=None):
    return {
        "week_start": pd.Timestamp("2026-08-24"), "week_end": pd.Timestamp("2026-08-30"),
        "base_date": pd.Timestamp("2026-08-30"),
        "story": None, "kpi_rows": [],
        "profit_strip_text": profit_strip_text,
        "attention": {"dept_count": 0, "ward_count": 0, "worst3": []},
        "calendar_preview": None,
        "improvement": {"dept_internal": [], "dept_surgery": [], "ward": []},
        "public_base_url": "https://hospital-dashboard-6ow.pages.dev/",
    }


class WeeklyDigestRenderTxtTest(unittest.TestCase):
    def test_no_marker_when_profit_strip_text_none(self):
        txt = render_txt(_digest_txt_ctx(None))
        self.assertNotIn("PROFIT_STRIP_TEXT_MARKER", txt)

    def test_marker_between_kpi_and_attention(self):
        txt = render_txt(_digest_txt_ctx("PROFIT_STRIP_TEXT_MARKER"))
        idx_kpi = txt.index("■ KPI")
        idx_marker = txt.index("PROFIT_STRIP_TEXT_MARKER")
        idx_attn = txt.index("■ 要注視")
        self.assertLess(idx_kpi, idx_marker)
        self.assertLess(idx_marker, idx_attn)


# ═══════════════════════════════════════
# 5. weekly_digest.html テンプレート断面レンダー
# ═══════════════════════════════════════
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


class WeeklyDigestTemplateProfitStripTest(unittest.TestCase):
    def test_renders_without_profit_strip_html_key(self):
        # main() を通さない直接レンダー。profit_strip_html キー自体が無くても例外にならない。
        html = _render_digest()
        self.assertNotIn("PROFIT_STRIP_HTML_MARKER", html)

    def test_renders_with_profit_strip_html_none(self):
        html = _render_digest(profit_strip_html=None)
        self.assertNotIn("PROFIT_STRIP_HTML_MARKER", html)

    def test_profit_strip_html_inserted_when_present(self):
        html = _render_digest(profit_strip_html='<div style="color:#000">PROFIT_STRIP_HTML_MARKER</div>')
        self.assertIn("PROFIT_STRIP_HTML_MARKER", html)


if __name__ == "__main__":
    unittest.main()
