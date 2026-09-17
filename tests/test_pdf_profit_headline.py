"""粗利/人日ヘッドラインのPDF展開（P3: 病院PDF P1・P4／部門PDFパーツD note）の回帰テスト。

背景（spec/改修プラン_粗利の単位あたり指標.md P3）: 病院PDF P1に1行帯
（hospital_summary.render_profit_headline_line）、P4診療科テーブルに「粗利/人日
（確報月）」列、部門PDFのパーツD（粗利チャート）note に確報月の粗利/人日・延患者数を
追記する。数値の出所はいずれも既存の profit_unit.build_profit_unit_payload の
by_dept[dept].latest のみ（新しい推計はしない・確報月のみ・ppd は必ず延患者数と
セット）。

密閉（実データ・常駐サーバ・Chrome起動は一切使わない。build_hospital_report.py /
build_dept_reports.py の main() は呼ばない）。対象:
  1. app.lib.hospital_summary.render_profit_headline_line
  2. app.lib.hospital_summary.render_dept_table（粗利/人日列の追加）
  3. app.lib.hospital_summary.build_summary_context の profit_unit kwarg 配線
  4. app.lib.dept_report._build_parts のパーツD note（profit_unit 追記）

実行: リポジトリルートで
    OMLX_BASE_URL=http://127.0.0.1:9 OMLX_BASE=http://127.0.0.1:9 \
    .venv/bin/python -m pytest tests/test_pdf_profit_headline.py -q
"""
import re
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib import dept_report as dr  # noqa: E402
from app.lib import hospital_summary as hs  # noqa: E402


# ═══════════════════════════════════════
# 1. render_profit_headline_line（P1・1行帯）
# ═══════════════════════════════════════
FAKE_PH = {
    "month": "2026-09",
    "as_of": "2026-09-16",
    "nyuin": {
        "ppd": 86713, "patient_days": 12345, "profit_mm": 1461.4, "target_mm": 1400.0,
        "achievement_pct": 104.4, "vs_prev_pct": 2.3, "direction": "up",
    },
    "gairai": {
        "ppd_biz": 5.2, "biz_days": 20, "profit_mm": 351.2,
        "target_mm_nominal": 400.0, "target_mm": 400.0,
        "target_per_biz_day": 5.0, "achievement_pct": 88.1, "vs_prev_pct": -1.1, "direction": "down",
    },
    "total": {"proj_mm": 1812.6, "target_mm": 1800.0, "rate": 103.3,
              "status": {"css": "ok", "shape": "▲", "text": "達成"}},
    "labels": {
        "main_nyuin": "入院 粗利/人日", "main_gairai": "外来 粗利/営業日",
        "period": "2026年9月 月末見込み（9/16時点・診療実績ベース・暫定）",
        "cmp": "前月比",
    },
}


class RenderProfitHeadlineLineTest(unittest.TestCase):
    def test_ppd_and_patient_days_on_same_line(self):
        html = hs.render_profit_headline_line(FAKE_PH)
        self.assertIn("86,713円", html)
        self.assertIn("延患者数 12,345人日", html)
        # 1行テキスト帯＝<div>は1つだけ（別divに分割していない）。
        self.assertEqual(html.count("<div"), 1)
        idx_ppd = html.index("86,713円")
        idx_pd = html.index("延患者数 12,345人日")
        self.assertLess(idx_ppd, idx_pd)

    def test_no_forbidden_markup(self):
        html = hs.render_profit_headline_line(FAKE_PH)
        self.assertNotIn("<script", html)
        self.assertNotIn("<svg", html)
        self.assertNotIn("class=", html)

    def test_contains_gairai_and_period(self):
        html = hs.render_profit_headline_line(FAKE_PH)
        self.assertIn("外来 粗利/営業日", html)
        self.assertIn("5.2百万円/営業日", html)
        self.assertIn("2026年9月 月末見込み", html)

    def test_none_returns_empty(self):
        self.assertEqual(hs.render_profit_headline_line(None), "")

    def test_empty_dict_returns_empty(self):
        self.assertEqual(hs.render_profit_headline_line({}), "")

    def test_null_ppd_omits_nyuin_segment(self):
        ph = {**FAKE_PH, "nyuin": {}}
        html = hs.render_profit_headline_line(ph)
        self.assertNotIn("入院 粗利/人日", html)
        self.assertIn("外来 粗利/営業日", html)


# ═══════════════════════════════════════
# 2. render_dept_table（P4・粗利/人日列）
# ═══════════════════════════════════════
def _row(name, dtype="内科", ppd_latest=None, ppd_month=None):
    return {
        "name": name, "type": dtype, "exempt": False,
        "inp_actual": None, "inp_target": None, "inp_rate": None,
        "nadm_actual": None, "nadm_target": None, "nadm_rate": None,
        "surg_actual": None, "surg_target": None, "surg_rate": None,
        "flow": {"in": 0, "out": 0, "net": 0},
        "profit_proj": None,
        "ppd_latest": ppd_latest, "ppd_month": ppd_month,
    }


class RenderDeptTableProfitPpdColumnTest(unittest.TestCase):
    def test_seven_columns_and_full_width(self):
        rows = [_row("内科A", ppd_latest=86713, ppd_month="2026-08")]
        html = hs.render_dept_table(rows)
        self.assertEqual(html.count("<col "), 7)
        widths = [float(w) for w in re.findall(r'width:([\d.]+)%', html)]
        self.assertEqual(len(widths), 7)
        self.assertAlmostEqual(sum(widths), 100.0, places=3)

    def test_ppd_cell_value_and_header_month(self):
        rows = [_row("内科A", ppd_latest=86713, ppd_month="2026-08")]
        html = hs.render_dept_table(rows)
        self.assertIn("86,713円", html)
        self.assertIn("粗利/人日（確報月）", html)
        self.assertIn("8月", html)

    def test_none_row_renders_dash(self):
        rows = [_row("内科A", ppd_latest=None, ppd_month=None)]
        html = hs.render_dept_table(rows)
        self.assertIn("<td>—</td>", html)

    def test_grp_row_colspan_matches_seven_columns(self):
        rows = [_row("内科A", ppd_latest=86713, ppd_month="2026-08")]
        html = hs.render_dept_table(rows)
        self.assertIn('colspan="7"', html)


# ═══════════════════════════════════════
# 3. build_summary_context の profit_unit kwarg 配線
# ═══════════════════════════════════════
BASE_DATE = pd.Timestamp("2026-09-16")
DISPLAY_DEPT = "循環器内科"  # NADM_DISPLAY_DEPTS 所属の実在科（medical系）
CTX_TARGETS = {
    "new_admission": {"dept": {}, "ward": {}},
    "inpatient": {"dept": {}, "ward": {}, "ward_beds": {}},
}
CTX_SURG_TARGETS = {}


def _run_build_summary_context(profit_unit):
    """build_summary_context の重い依存をフェイクに差し替え、profit_unit 配線だけを
    検証する（test_hospital_summary_profit_projection_wiring.py と同じ手法）。"""
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
            pd.DataFrame(), pd.DataFrame(), CTX_TARGETS, CTX_SURG_TARGETS, BASE_DATE,
            profit_unit=profit_unit)


def _dept_row(ctx, dept=DISPLAY_DEPT):
    return next((r for r in ctx["dept_rows"] if r["name"] == dept), None)


class BuildSummaryContextProfitUnitWiringTest(unittest.TestCase):
    def test_ppd_latest_wired_into_dept_rows(self):
        profit_unit = {"by_dept": {DISPLAY_DEPT: {
            "latest": {"ppd": 86713, "patient_days": 12345, "month": "2026-08"}}}}
        ctx = _run_build_summary_context(profit_unit)
        row = _dept_row(ctx)
        self.assertIsNotNone(row)
        self.assertEqual(row["ppd_latest"], 86713)
        self.assertEqual(row["ppd_month"], "2026-08")

    def test_profit_unit_unspecified_is_none(self):
        ctx = _run_build_summary_context(None)
        row = _dept_row(ctx)
        self.assertIsNotNone(row)
        self.assertIsNone(row["ppd_latest"])
        self.assertIsNone(row["ppd_month"])

    def test_dept_without_profit_unit_entry_is_none(self):
        profit_unit = {"by_dept": {"別科": {"latest": {"ppd": 1, "patient_days": 1, "month": "2026-08"}}}}
        ctx = _run_build_summary_context(profit_unit)
        row = _dept_row(ctx)
        self.assertIsNotNone(row)
        self.assertIsNone(row["ppd_latest"])


# ═══════════════════════════════════════
# 4. dept_report._build_parts のパーツD note（部門PDF）
# ═══════════════════════════════════════
DEPT_NAME = "呼吸器内科"  # SURGERY_EVAL_DEPTS 外＝内科系（C:全麻パーツを持たない）
FAKE_PROFIT_SERIES = {
    "dates": ["8月"], "cur": [12.3], "prev": [None], "ref": 10.0, "rate": 100.0,
    "latest": pd.Timestamp("2026-08-01"), "proj": None, "proj_month": None,
    "prev_adjusted": False,
}
D_TARGETS = {
    "new_admission": {"dept": {}, "ward": {}},
    "inpatient": {"dept": {}, "ward": {}, "ward_beds": {}},
}
D_SURG_TARGETS = {}


def _adm_df(base_date, dept=DEPT_NAME, n=8):
    dates = pd.date_range(base_date - pd.Timedelta(days=6), base_date, freq="D")
    rows = []
    for i in range(n):
        d = dates[i % len(dates)]
        rows.append({"日付": d, "新入院患者数": 1, "新入院患者数_病棟": 1, "在院患者数": 5,
                    "科_表示": True, "診療科名": dept,
                    "病棟_表示": True, "病棟コード": "04A"})
    return pd.DataFrame(rows)


def _parts(base_date, profit_unit=None, name=DEPT_NAME):
    adm = _adm_df(base_date, dept=name)
    return dr._build_parts(adm, pd.DataFrame(), base_date, "dept", name, name,
                           dd=None, r7_inp={"by_dept": {}, "by_ward": {}},
                           r7_nadm={"by_dept": {name: 8}, "by_ward": {}},
                           r7_surg={"by_dept": {}}, targets=D_TARGETS, surg_targets=D_SURG_TARGETS,
                           profit_series=FAKE_PROFIT_SERIES, profit_unit=profit_unit)


class BuildPartsProfitUnitNoteTest(unittest.TestCase):
    BASE_DATE = pd.Timestamp("2026-09-16")

    def test_note_includes_ppd_and_patient_days_when_both_present(self):
        profit_unit = {"by_dept": {DEPT_NAME: {
            "latest": {"month": "2026-08", "ppd": 86713, "patient_days": 12345}}}}
        note = _parts(self.BASE_DATE, profit_unit=profit_unit)["D"]["note"]
        self.assertIn("粗利/人日", note)
        self.assertIn("延患者数 12,345人日", note)
        self.assertIn("86,713円", note)
        self.assertIn("8月確報", note)

    def test_note_unchanged_without_profit_unit(self):
        note = _parts(self.BASE_DATE, profit_unit=None)["D"]["note"]
        self.assertNotIn("粗利/人日", note)
        self.assertNotIn("延患者数", note)

    def test_note_unchanged_when_patient_days_missing(self):
        # ppd はあるが延患者数が無い（片方だけ）→ 必ずセットの原則により追記しない。
        profit_unit = {"by_dept": {DEPT_NAME: {
            "latest": {"month": "2026-08", "ppd": 86713, "patient_days": None}}}}
        note = _parts(self.BASE_DATE, profit_unit=profit_unit)["D"]["note"]
        self.assertNotIn("粗利/人日", note)

    def test_note_unchanged_when_dept_not_in_profit_unit(self):
        profit_unit = {"by_dept": {"別科": {
            "latest": {"month": "2026-08", "ppd": 1, "patient_days": 1}}}}
        note = _parts(self.BASE_DATE, profit_unit=profit_unit)["D"]["note"]
        self.assertNotIn("粗利/人日", note)


if __name__ == "__main__":
    unittest.main()
