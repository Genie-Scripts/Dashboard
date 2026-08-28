"""report_edit_telemetry.py（P5-f 編集テレメトリ）のユニットテスト。

⚠️ PUBLIC リポにつき、実在の診療科名・実際のレポート文は書かない。
架空科A・テスト第一科・科イ・科ロ・科ハ等の合成データのみ使う。
LLM(oMLX)・実ビルドは呼ばない。app.lib.report_feedback.capture_edits で合成 edits jsonl を
一時ディレクトリに作り、集計（添削率・編集距離・topic別内訳・トレンド）のみ検証する。
"""
import csv
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.report_feedback import capture_edits
from scripts.report_edit_telemetry import (
    _edit_distance, _dist_stats, compute_by_date, summarize_date, analyze,
    print_report, write_csv,
)


def _ctx(axis, unit, src, body, action, topic="admission"):
    return {"axis": axis, "unit": unit, "_state": {},
            "move": {"src": src, "body": body, "action": action, "topic": topic}}


class TestEditDistance(unittest.TestCase):
    def test_identical_strings_zero_distance(self):
        self.assertEqual(_edit_distance("同じ文章です", "同じ文章です"), 0.0)

    def test_distance_is_one_minus_jaccard(self):
        # "abcde" vs "abcfg" の3-gram: {abc,bcd,cde} vs {abc,bcf,cfg} → 交差1/和集合5
        d = _edit_distance("abcde", "abcfg")
        self.assertAlmostEqual(d, 1 - 1 / 5)

    def test_dist_stats_excludes_pairs_without_ai_baseline(self):
        pairs = [
            {"had_ai": True, "ai_body": "AI本文", "human_body": "人の本文",
             "ai_action": "AI打ち手", "human_action": "AI打ち手"},
            {"had_ai": False, "ai_body": None, "human_body": "手編集本文",
             "ai_action": None, "human_action": "手編集打ち手"},
        ]
        stats = _dist_stats(pairs)
        # had_ai=False のペアは分母(n)から除外される
        self.assertEqual(stats["body_n"], 1)
        self.assertEqual(stats["action_n"], 1)
        self.assertEqual(stats["action_mean"], 0.0)   # 打ち手は無変更


class TestComputeAndSummarize(unittest.TestCase):
    def test_target_corrected_rate(self):
        with tempfile.TemporaryDirectory() as d:
            capture_edits(d, "2026-07-01", [
                _ctx("dept", "架空科A", "ai", "AI本文A", "AI打ち手A", "admission"),
                _ctx("dept", "架空科B", "ai", "AI本文B", "AI打ち手B", "surgery"),
                _ctx("ward", "テスト病棟", "ai", "AI本文C", "AI打ち手C", "leveling"),
            ])
            # 架空科A のみ人が添削（body変更）。架空科B・テスト病棟はAI採択のまま。
            capture_edits(d, "2026-07-01", [
                _ctx("dept", "架空科A", "manual", "添削後の本文A", "AI打ち手A", "admission"),
                _ctx("dept", "架空科B", "ai", "AI本文B", "AI打ち手B", "surgery"),
                _ctx("ward", "テスト病棟", "ai", "AI本文C", "AI打ち手C", "leveling"),
            ])
            from app.lib.report_feedback import load_edits, pair_corrections
            records = load_edits(Path(d))
            pairs = pair_corrections(records)
            by_date = compute_by_date(records, pairs)
            s = summarize_date(by_date["2026-07-01"])

            self.assertEqual(s["target"], 3)
            self.assertEqual(s["corrected"], 1)
            self.assertAlmostEqual(s["rate"], 100 / 3, places=1)
            self.assertEqual(s["body_n"], 1)
            self.assertGreater(s["body_mean"], 0.0)     # body は実際に変わった
            self.assertEqual(s["action_mean"], 0.0)     # action は無変更

            self.assertEqual(s["by_axis"]["dept"]["target"], 2)
            self.assertEqual(s["by_axis"]["dept"]["corrected"], 1)
            self.assertEqual(s["by_axis"]["ward"]["target"], 1)
            self.assertEqual(s["by_axis"]["ward"]["corrected"], 0)

    def test_topic_breakdown(self):
        with tempfile.TemporaryDirectory() as d:
            capture_edits(d, "2026-07-02", [
                _ctx("dept", "架空科A", "ai", "AI本文A", "AI打ち手A", "admission"),
                _ctx("dept", "架空科B", "ai", "AI本文B", "AI打ち手B", "surgery"),
            ])
            capture_edits(d, "2026-07-02", [
                _ctx("dept", "架空科A", "manual", "添削後A", "AI打ち手A", "admission"),
                _ctx("dept", "架空科B", "ai", "AI本文B", "AI打ち手B", "surgery"),
            ])
            from app.lib.report_feedback import load_edits, pair_corrections
            records = load_edits(Path(d))
            pairs = pair_corrections(records)
            by_date = compute_by_date(records, pairs)
            s = summarize_date(by_date["2026-07-02"])

            self.assertEqual(s["by_topic"]["admission"]["target"], 1)
            self.assertEqual(s["by_topic"]["admission"]["corrected"], 1)
            self.assertEqual(s["by_topic"]["surgery"]["target"], 1)
            self.assertEqual(s["by_topic"]["surgery"]["corrected"], 0)

    def test_manual_without_ai_baseline_counts_as_corrected_but_no_distance(self):
        with tempfile.TemporaryDirectory() as d:
            # 同一base_date内にAI原文の記録が無い（手編集直で override）ケース
            capture_edits(d, "2026-07-03", [
                _ctx("dept", "科イ", "manual", "手本文", "手打ち手", "admission"),
            ])
            from app.lib.report_feedback import load_edits, pair_corrections
            records = load_edits(Path(d))
            pairs = pair_corrections(records)
            by_date = compute_by_date(records, pairs)
            s = summarize_date(by_date["2026-07-03"])

            self.assertEqual(s["target"], 0)      # ai/tpl レコードが無い
            self.assertEqual(s["corrected"], 1)   # だが manual ペアは1件
            self.assertIsNone(s["rate"])          # 分母0 → N/A
            self.assertEqual(s["body_n"], 0)      # 編集距離は計算対象外


class TestAnalyzeTrend(unittest.TestCase):
    def _seed(self, d, date, corrected: bool):
        capture_edits(d, date, [_ctx("dept", "科ロ", "ai", "AI本文", "AI打ち手", "admission")])
        if corrected:
            capture_edits(d, date, [_ctx("dept", "科ロ", "manual", "添削後本文", "AI打ち手", "admission")])

    def test_recent_vs_prior_pool(self):
        with tempfile.TemporaryDirectory() as d:
            # 前半2日は添削なし、直近2日は添削ありにして recent_n=2 で分離できるか確認
            self._seed(d, "2026-06-01", corrected=False)
            self._seed(d, "2026-06-02", corrected=False)
            self._seed(d, "2026-06-03", corrected=True)
            self._seed(d, "2026-06-04", corrected=True)

            result = analyze(Path(d), recent_n=2)
            self.assertEqual(result["dates"], ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04"])
            self.assertEqual(result["recent_dates"], ["2026-06-03", "2026-06-04"])
            self.assertEqual(result["prior_dates"], ["2026-06-01", "2026-06-02"])
            self.assertEqual(result["recent_pool"]["rate"], 100.0)
            self.assertEqual(result["prior_pool"]["rate"], 0.0)

    def test_all_dates_used_as_recent_when_fewer_than_n(self):
        with tempfile.TemporaryDirectory() as d:
            self._seed(d, "2026-06-01", corrected=True)
            result = analyze(Path(d), recent_n=4)
            self.assertEqual(result["recent_dates"], ["2026-06-01"])
            self.assertEqual(result["prior_dates"], [])
            self.assertIsNone(result["prior_pool"])

    def test_no_signal_returns_empty_dates(self):
        with tempfile.TemporaryDirectory() as d:
            result = analyze(Path(d), recent_n=4)
            self.assertEqual(result["dates"], [])
            self.assertEqual(result["n_records"], 0)


class TestOutput(unittest.TestCase):
    def test_print_report_smoke(self):
        with tempfile.TemporaryDirectory() as d:
            capture_edits(d, "2026-06-10", [_ctx("dept", "科ハ", "ai", "AI本文", "AI打ち手", "admission")])
            capture_edits(d, "2026-06-10", [_ctx("dept", "科ハ", "manual", "添削後", "AI打ち手", "admission")])
            result = analyze(Path(d), recent_n=4)
            buf = io.StringIO()
            with redirect_stdout(buf):
                print_report(result, recent_n=4)
            out = buf.getvalue()
            self.assertIn("2026-06-10", out)
            self.assertIn("トレンド要約", out)

    def test_write_csv_long_format(self):
        with tempfile.TemporaryDirectory() as d:
            capture_edits(d, "2026-06-10", [_ctx("dept", "科ハ", "ai", "AI本文", "AI打ち手", "admission")])
            capture_edits(d, "2026-06-10", [_ctx("dept", "科ハ", "manual", "添削後", "AI打ち手", "admission")])
            result = analyze(Path(d), recent_n=4)
            csv_path = Path(d) / "out.csv"
            write_csv(csv_path, result, recent_n=4)
            rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
            self.assertTrue(any(r["scope"] == "overall" and r["base_date"] == "2026-06-10" for r in rows))
            self.assertTrue(any(r["scope"] == "topic" and r["key"] == "admission" for r in rows))
            self.assertTrue(any(r["scope"] == "trend_bucket" and r["key"] == "recent" for r in rows))


if __name__ == "__main__":
    unittest.main()
