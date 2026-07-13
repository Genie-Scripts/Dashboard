"""portal_history（A4継続日数バッジ ＋ B4変化点バナーの基盤）のユニットテスト。

triage.score_departments / score_wards は決定論だが実データ依存のため、
合成データで検証するには monkeypatch する。portal_history は
`triage.score_departments(...)` の形（属性参照）で呼ぶので、
unittest.mock.patch.object(triage, "score_departments", ...) で差し替え可能。

対象:
  - build_attention_history: streak（連続日数）／entered・exited（出入り）
  - kpi_status_changes: 達成バケット遷移／履歴欠落時の縮退

実行: リポジトリルートで
    python -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib import portal_history, triage, weekly_story

BASE = pd.Timestamp("2026-07-13")


def _rec(name, status_kind, entity_type="dept"):
    """score_departments/score_wards の戻り値レコードの最小構成
    （portal_history が参照するのは name / status_kind のみ）。"""
    return {"name": name, "entity_type": entity_type, "status_kind": status_kind}


class BuildAttentionHistoryTest(unittest.TestCase):
    def _patch_scores(self, dept_by_date: dict, ward_by_date: dict):
        def fake_dept(adm, surg, targets, surg_targets, profit_monthly, base_date):
            return dept_by_date.get(base_date.strftime("%Y-%m-%d"), [])

        def fake_ward(adm, targets, base_date):
            return ward_by_date.get(base_date.strftime("%Y-%m-%d"), [])

        p1 = patch.object(triage, "score_departments", side_effect=fake_dept)
        p2 = patch.object(triage, "score_wards", side_effect=fake_ward)
        p1.start(); self.addCleanup(p1.stop)
        p2.start(); self.addCleanup(p2.stop)

    def test_streak_counts_consecutive_days(self):
        # 内科A: 今日・昨日・一昨日 連続below → streak=3。3日前で途切れる。
        dept_by_date = {
            "2026-07-13": [_rec("内科A", "below")],
            "2026-07-12": [_rec("内科A", "below")],
            "2026-07-11": [_rec("内科A", "below")],
            "2026-07-10": [],
        }
        self._patch_scores(dept_by_date, {})
        hist = portal_history.build_attention_history(
            None, None, {}, {}, None, BASE, days=5)
        self.assertEqual(hist["streaks"][("dept", "内科A")], 3)

    def test_streak_single_day_is_one(self):
        dept_by_date = {"2026-07-13": [_rec("内科B", "watch")]}
        self._patch_scores(dept_by_date, {})
        hist = portal_history.build_attention_history(
            None, None, {}, {}, None, BASE, days=5)
        self.assertEqual(hist["streaks"][("dept", "内科B")], 1)

    def test_streak_hits_window_edge(self):
        # 3日窓すべて below → streak == days（呼び出し側が streak_capped 判定に使う値）
        dates = ["2026-07-13", "2026-07-12", "2026-07-11"]
        dept_by_date = {d: [_rec("外科A", "below")] for d in dates}
        self._patch_scores(dept_by_date, {})
        hist = portal_history.build_attention_history(
            None, None, {}, {}, None, BASE, days=3)
        self.assertEqual(hist["streaks"][("dept", "外科A")], 3)

    def test_below_and_watch_both_count_toward_streak(self):
        # below/watch のどちらでも「対象」であることに変わりはない → 連続日数に通算
        dept_by_date = {
            "2026-07-13": [_rec("内科C", "below")],
            "2026-07-12": [_rec("内科C", "watch")],
        }
        self._patch_scores(dept_by_date, {})
        hist = portal_history.build_attention_history(
            None, None, {}, {}, None, BASE, days=5)
        self.assertEqual(hist["streaks"][("dept", "内科C")], 2)

    def test_entered_and_exited(self):
        # 外科B: 今日のみ対象（entered）。整形C: 昨日のみ対象（exited）。
        dept_by_date = {
            "2026-07-13": [_rec("外科B", "below")],
            "2026-07-12": [_rec("整形C", "watch")],
        }
        self._patch_scores(dept_by_date, {})
        hist = portal_history.build_attention_history(
            None, None, {}, {}, None, BASE, days=5)
        self.assertEqual(hist["prev_date"], "2026-07-12")
        entered_names = [it["name"] for it in hist["entered"]]
        exited_names = [it["name"] for it in hist["exited"]]
        self.assertIn("外科B", entered_names)
        self.assertIn("整形C", exited_names)
        self.assertNotIn("外科B", exited_names)
        self.assertNotIn("整形C", entered_names)
        # item-lite のスキーマ確認（href は triage.pick_targets と同じ規約）
        entered_item = next(it for it in hist["entered"] if it["name"] == "外科B")
        self.assertEqual(entered_item["entity"], "dept")
        self.assertEqual(entered_item["href"], "dept.html#外科B")
        self.assertEqual(entered_item["status_kind"], "below")

    def test_unchanged_unit_is_neither_entered_nor_exited(self):
        dept_by_date = {
            "2026-07-13": [_rec("内科A", "below")],
            "2026-07-12": [_rec("内科A", "below")],
        }
        self._patch_scores(dept_by_date, {})
        hist = portal_history.build_attention_history(
            None, None, {}, {}, None, BASE, days=5)
        self.assertEqual(hist["entered"], [])
        self.assertEqual(hist["exited"], [])

    def test_ward_href_convention(self):
        ward_by_date = {"2026-07-13": [_rec("9階B病棟", "below", entity_type="ward")]}
        self._patch_scores({}, ward_by_date)
        hist = portal_history.build_attention_history(
            None, None, {}, {}, None, BASE, days=2)
        self.assertEqual(hist["streaks"][("ward", "9階B病棟")], 1)

    def test_no_prior_day_data_keeps_entered_exited_empty(self):
        # データ初日相当: adm_min == base_date のため k=1 の再計算が回らない
        dept_by_date = {"2026-07-13": [_rec("内科A", "below")]}
        self._patch_scores(dept_by_date, {})
        adm = pd.DataFrame({"日付": [BASE]})
        hist = portal_history.build_attention_history(
            adm, None, {}, {}, None, BASE, days=5)
        self.assertEqual(hist["entered"], [])
        self.assertEqual(hist["exited"], [])
        self.assertIsNone(hist["prev_date"])
        # 今日分の streak は 1 として計算される（縮退であって欠落ではない）
        self.assertEqual(hist["streaks"][("dept", "内科A")], 1)


class KpiStatusChangesTest(unittest.TestCase):
    def _snap(self, base_date, inp_avg7, adm_rate7, op_rate):
        return {
            "base_date": base_date,
            "inpatient": {"avg_7d": inp_avg7, "rate": None},
            "admission": {"actual_7d": None, "planned_7d": None,
                         "emergency_7d": None, "rate_7d": adm_rate7},
            "operation": {"week_total": None, "rate": op_rate, "or_util_7d": None},
            "profit_top": [],
        }

    def test_bucket_transition_detected_for_all_three_kpi(self):
        # 在院: 未達(85.8%)→達成(102.9%) / 新入院: 接近(95%)→達成(105%) / 全麻: 達成(101%)→未達(88%)
        prior = self._snap("2026-07-12", inp_avg7=500, adm_rate7=95, op_rate=101)
        current = self._snap("2026-07-13", inp_avg7=600, adm_rate7=105, op_rate=88)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "last_kpi.json"
            weekly_story.save_history(path, [prior, current])
            changes = portal_history.kpi_status_changes(path, BASE)
        by_label = {c["label"]: c for c in changes}
        self.assertEqual(set(by_label), {"在院", "新入院", "全麻"})
        self.assertEqual(by_label["在院"]["from"], "未達")
        self.assertEqual(by_label["在院"]["to"], "達成")
        self.assertTrue(by_label["在院"]["improved"])
        self.assertEqual(by_label["新入院"]["from"], "接近")
        self.assertEqual(by_label["新入院"]["to"], "達成")
        self.assertTrue(by_label["新入院"]["improved"])
        self.assertEqual(by_label["全麻"]["from"], "達成")
        self.assertEqual(by_label["全麻"]["to"], "未達")
        self.assertFalse(by_label["全麻"]["improved"])

    def test_same_bucket_is_not_reported(self):
        # 在院: 未達→未達（水準は変わっても達成バケットは同じ）は変化なし扱い
        prior = self._snap("2026-07-12", inp_avg7=490, adm_rate7=95, op_rate=101)
        current = self._snap("2026-07-13", inp_avg7=500, adm_rate7=95, op_rate=101)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "last_kpi.json"
            weekly_story.save_history(path, [prior, current])
            changes = portal_history.kpi_status_changes(path, BASE)
        self.assertEqual(changes, [])

    def test_missing_history_file_degrades_to_empty(self):
        changes = portal_history.kpi_status_changes(
            Path("/nonexistent/last_kpi.json"), BASE)
        self.assertEqual(changes, [])

    def test_current_snapshot_missing_degrades_to_empty(self):
        prior = self._snap("2026-07-12", inp_avg7=500, adm_rate7=95, op_rate=101)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "last_kpi.json"
            weekly_story.save_history(path, [prior])   # base_date当日分が無い
            changes = portal_history.kpi_status_changes(path, BASE)
        self.assertEqual(changes, [])

    def test_no_prior_snapshot_degrades_to_empty(self):
        current = self._snap("2026-07-13", inp_avg7=600, adm_rate7=105, op_rate=88)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "last_kpi.json"
            weekly_story.save_history(path, [current])  # 過去分が無い
            changes = portal_history.kpi_status_changes(path, BASE)
        self.assertEqual(changes, [])

    def test_malformed_json_degrades_to_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "last_kpi.json"
            path.write_text("{not valid json", encoding="utf-8")
            changes = portal_history.kpi_status_changes(path, BASE)
        self.assertEqual(changes, [])


if __name__ == "__main__":
    unittest.main()
