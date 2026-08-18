"""C1 ナラティブ品質測定ハーネス（app.lib.narrative_eval）のユニットテスト。

⚠️ PUBLIC リポにつき、実在の診療科名・実際のレポート文は書かない。架空科A/架空科B/
架空科C/架空外科/架空科Z 等の合成データのみ使う。LLM(oMLX)・常駐サーバは一切呼ばない
（narrative_eval は純ロジックで、report_feedback.capture_edits はローカルファイル書き込み
のみ）。既存規約（test_fewshot.py・test_report_feedback.py）に合わせ、unittest +
TemporaryDirectory + capture_edits で合成台帳を作る。keep_ratio 等の値を厳密に固定したい
テストのみ、プリミティブ関数を直接呼ぶ（capture_edits は changed=[]の同値ペアを距離計算の
対象にしないため＝distance_stats に完全一致の組は原理的に現れない）。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.report_feedback import capture_edits, load_edits
from app.lib import ai_narrative, triage
from app.lib import narrative_eval as ne

_GOLDEN = json.loads(
    (Path(__file__).resolve().parent / "golden" / "alert_headlines.json")
    .read_text(encoding="utf-8"))


def _ctx(axis, unit, src, body, action, topic="admission", facts=None):
    return {"axis": axis, "unit": unit, "_state": facts or {},
            "move": {"src": src, "body": body, "action": action, "topic": topic}}


# ════════════════════════════════════════════════════════════
# is_taigen_dome
# ════════════════════════════════════════════════════════════
class TestIsTaigenDome(unittest.TestCase):
    def test_taigen_examples(self):
        # golden fixture（合成の見出し10件）
        for case in _GOLDEN["taigen_cases"]:
            with self.subTest(headline=case["headline"]):
                self.assertEqual(ne.is_taigen_dome(case["headline"]), case["expected"], case["note"])

        # ai_narrative.py SYSTEM_PROMPT の良い例/悪い例（見出し）を期待値固定
        self.assertIn("新入院、目標を超過", ai_narrative.SYSTEM_PROMPT)
        self.assertTrue(ne.is_taigen_dome("新入院、目標を超過"))
        self.assertIn("新入院数が目標を大きく上回っている", ai_narrative.SYSTEM_PROMPT)
        self.assertFalse(ne.is_taigen_dome("新入院数が目標を大きく上回っている"))

        # triage.py の fallback 見出し（643/651/657行）を実際に生成させて期待値固定
        watch = triage._make_fallback_narrative({
            "priority": "中", "entity_type": "dept", "status_kind": "watch",
            "improving": False, "primary_is_fallback": False,
            "primary_kpi": "inp", "name": "架空科A"})
        self.assertEqual(watch["headline"], "在院患者数が悪化傾向（達成中）")
        self.assertTrue(ne.is_taigen_dome(watch["headline"]))

        improving = triage._make_fallback_narrative({
            "priority": "中", "entity_type": "dept", "status_kind": None,
            "improving": True, "primary_is_fallback": False,
            "primary_kpi": "op", "name": "架空外科"})
        self.assertEqual(improving["headline"], "全身麻酔手術は改善傾向（なお未達）")
        self.assertTrue(ne.is_taigen_dome(improving["headline"]))

        unmet = triage._make_fallback_narrative({
            "priority": "中", "entity_type": "dept", "status_kind": None,
            "improving": False, "primary_is_fallback": False,
            "primary_kpi": "inp", "name": "架空科A"})
        self.assertEqual(unmet["headline"], "在院患者数が目標未達")
        self.assertTrue(ne.is_taigen_dome(unmet["headline"]))

        leveling = triage._make_leveling_fallback({"priority": "中", "improving": False})
        self.assertEqual(leveling["headline"], "退院が週後半に偏在")
        self.assertTrue(ne.is_taigen_dome(leveling["headline"]))

    def test_taigen_dangling_particle(self):
        self.assertFalse(ne.is_taigen_dome("新入院を"))
        self.assertFalse(ne.is_taigen_dome("病床稼働率は"))
        self.assertFalse(ne.is_taigen_dome(""))

    def test_taigen_strips_parenthetical(self):
        self.assertTrue(ne.is_taigen_dome("全身麻酔手術は改善傾向（なお未達）"))
        # 括弧を剥がした後の末尾が述語なら、剥がしても False のまま
        self.assertFalse(ne.is_taigen_dome("全身麻酔手術は改善しています（なお未達）"))


# ════════════════════════════════════════════════════════════
# score_alert_narrative / aggregate_alert_scores
# ════════════════════════════════════════════════════════════
class TestScoreAlertNarrative(unittest.TestCase):
    def test_score_alert_matches_production_guard(self):
        seen_reasons = set()
        for case in _GOLDEN["alert_scoring_cases"]:
            with self.subTest(case=case["note"]):
                score = ne.score_alert_narrative(case["alert"], case["narrative"])
                self.assertEqual(score["reject_reason"], case["expected_reject_reason"])
                # 二重実装ではなく本番ガードそのものを再利用していることの確認
                self.assertEqual(
                    score["reject_reason"],
                    ai_narrative._alert_reject_reason(case["narrative"], case["alert"]))
                seen_reasons.add(score["reject_reason"])
        # golden fixture が4理由(parse/empty/headline_long/headline_echo)+採択を網羅していること
        self.assertEqual(seen_reasons, {None, "parse", "empty", "headline_long", "headline_echo"})

    def test_alert_aggregate_rates(self):
        cases = _GOLDEN["alert_scoring_cases"]
        rows = [ne.score_alert_narrative(c["alert"], c["narrative"]) for c in cases]
        agg = ne.aggregate_alert_scores(rows)
        self.assertEqual(agg["n"], len(rows))
        expected_accept = sum(1 for c in cases if c["expected_reject_reason"] is None)
        self.assertAlmostEqual(agg["accept_rate"], expected_accept / len(rows))
        self.assertIn("reject_reasons", agg)
        self.assertEqual(sum(agg["reject_reasons"].values()), len(rows))
        self.assertIsNotNone(agg["headline_len_median"])


# ════════════════════════════════════════════════════════════
# edit_stats
# ════════════════════════════════════════════════════════════
class TestEditStats(unittest.TestCase):
    def test_edit_rate_denominator_is_all_units(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            capture_edits(d, "2026-07-01", [
                _ctx("dept", "架空科A", "ai", "AI本文A", "AI打ち手A"),
                _ctx("dept", "架空科B", "ai", "AI本文B", "AI打ち手B"),
                _ctx("dept", "架空科C", "ai", "AI本文C", "AI打ち手C"),
            ])
            capture_edits(d, "2026-07-01", [
                _ctx("dept", "架空科A", "manual", "AI本文A", "添削後の打ち手A"),
            ])
            recs = load_edits(d)
            s = ne.edit_stats(recs)["2026-07-01"]
            self.assertEqual(s["units_total"], 3)
            self.assertEqual(s["manual_units"], 1)
            self.assertAlmostEqual(s["manual_rate"], 1 / 3)

    def test_true_edit_rate_excludes_verbatim_reapply(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            capture_edits(d, "2026-07-02", [
                _ctx("dept", "架空科A", "ai", "AI本文A", "AI打ち手A"),
                _ctx("dept", "架空科B", "ai", "AI本文B", "AI打ち手B"),
            ])
            # 架空科A: 完全一致の再確定（reapply） / 架空科B: 実際に打ち手が変化（true edit）
            capture_edits(d, "2026-07-02", [
                _ctx("dept", "架空科A", "manual", "AI本文A", "AI打ち手A"),
                _ctx("dept", "架空科B", "manual", "AI本文B", "添削後の打ち手B"),
            ])
            recs = load_edits(d)
            s = ne.edit_stats(recs)["2026-07-02"]
            self.assertEqual(s["units_total"], 2)
            self.assertEqual(s["true_edit_units"], 1)
            self.assertEqual(s["reapply_units"], 1)
            self.assertAlmostEqual(s["true_edit_rate"], 0.5)
            self.assertAlmostEqual(s["reapply_rate"], 0.5)


# ════════════════════════════════════════════════════════════
# distance_stats
# ════════════════════════════════════════════════════════════
class TestDistanceStats(unittest.TestCase):
    def test_had_ai_false_excluded_from_distance(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            # AI原文の記録が無いユニット（手編集直で override＝偽ペア）
            capture_edits(d, "2026-07-03", [
                _ctx("dept", "架空科Z", "manual", "手本文Z", "手打ち手Z"),
            ])
            recs = load_edits(d)
            dist = ne.distance_stats(recs)
            self.assertEqual(dist["body"]["n"], 0)
            self.assertEqual(dist["action"]["n"], 0)
            s = ne.edit_stats(recs)["2026-07-03"]
            self.assertEqual(s["manual_only_units"], 1)
            self.assertEqual(s["paired_units"], 0)

    def test_inverted_pair_excluded(self):
        """列 manual("H")→ai("A")＝時系列逆転（人が書いた後にAIが再生成された偽ペア）。
        「最後の manual より前」に ai/tpl が無いので有効ペアにしない（距離対象外・
        true_edit にも数えない）。manual_only（AI記録が全く無い）とは区別する。"""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            capture_edits(d, "2026-07-13", [
                _ctx("dept", "架空科H", "manual", "人の本文H", "人の打ち手H"),
            ])
            capture_edits(d, "2026-07-13", [
                _ctx("dept", "架空科H", "ai", "AI本文H", "AI打ち手H"),
            ])
            recs = load_edits(d)
            dist = ne.distance_stats(recs)
            self.assertEqual(dist["body"]["n"], 0)
            self.assertEqual(dist["action"]["n"], 0)
            s = ne.edit_stats(recs)["2026-07-13"]
            self.assertEqual(s["inverted_units"], 1)
            self.assertEqual(s["manual_only_units"], 0)
            self.assertEqual(s["paired_units"], 0)
            self.assertEqual(s["true_edit_units"], 0)
            self.assertEqual(s["manual_units"], 1)

    def test_before_is_prior_to_final_manual(self):
        """列 ai("A")→manual→ai("B")→manual(最終) ⇒ AI原文="A"（最終manualより前の
        最初のai。B は最終manualより後なので使わない）。"""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            capture_edits(d, "2026-07-14", [
                _ctx("dept", "架空科Q", "ai", "AI本文A", "AI打ち手A"),
            ])
            capture_edits(d, "2026-07-14", [
                _ctx("dept", "架空科Q", "manual", "添削本文1", "添削打ち手1"),
            ])
            capture_edits(d, "2026-07-14", [
                _ctx("dept", "架空科Q", "ai", "AI本文B", "AI打ち手B"),
            ])
            capture_edits(d, "2026-07-14", [
                _ctx("dept", "架空科Q", "manual", "添削本文2（最終）", "添削打ち手2（最終）"),
            ])
            recs = load_edits(d)
            pairs = ne._classified_pairs(recs)
            p = next(x for x in pairs if x["unit"] == "架空科Q")
            self.assertEqual(p["kind"], "valid")
            self.assertEqual(p["ai_body"], "AI本文A")
            self.assertEqual(p["ai_action"], "AI打ち手A")
            self.assertEqual(p["human_body"], "添削本文2（最終）")
            self.assertEqual(p["human_action"], "添削打ち手2（最終）")

    def test_before_src_tpl_separated(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            capture_edits(d, "2026-07-04", [
                _ctx("dept", "架空科A", "ai", "AI本文A", "AI打ち手A"),
                _ctx("dept", "架空科B", "tpl", "定型本文B", "定型打ち手B"),
            ])
            capture_edits(d, "2026-07-04", [
                _ctx("dept", "架空科A", "manual", "添削本文A", "添削打ち手A"),
                _ctx("dept", "架空科B", "manual", "添削本文B", "添削打ち手B"),
            ])
            recs = load_edits(d)
            dist = ne.distance_stats(recs)
            by_src = dist["body"]["by_before_src"]
            self.assertEqual(by_src["ai"]["n"], 1)
            self.assertEqual(by_src["tpl"]["n"], 1)
            self.assertEqual(dist["body"]["n"], 2)

    def test_distance_values_pinned(self):
        # 完全一致 = 1.0（keep）/ 0.0（edit_strength）
        self.assertEqual(ne.keep_ratio("同じ文がここにある", "同じ文がここにある"), 1.0)
        self.assertEqual(ne.edit_strength("同じ文がここにある", "同じ文がここにある"), 0.0)
        # 語彙が一切重ならない ≈ 0.0
        self.assertEqual(ne.keep_ratio("aaaa", "bbbb"), 0.0)
        self.assertEqual(ne.edit_strength("aaaa", "bbbb"), 1.0)


# ════════════════════════════════════════════════════════════
# churn_stats
# ════════════════════════════════════════════════════════════
class TestChurnStats(unittest.TestCase):
    def test_churn_detects_consecutive_ai_change(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            # 連続する ai→ai の変化 = 1件
            capture_edits(d, "2026-07-06", [
                _ctx("dept", "架空科A", "ai", "AI本文A", "AI打ち手A"),
            ])
            capture_edits(d, "2026-07-06", [
                _ctx("dept", "架空科A", "ai", "AI本文A改訂版", "AI打ち手A改訂版"),
            ])
            recs = load_edits(d)
            c = ne.churn_stats(recs)["2026-07-06"]
            self.assertEqual(c["ai_churn_units"], 1)
            self.assertAlmostEqual(c["ai_churn_rate"], 1.0)

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            # ai→manual→ai は churn に数えない（間に人の承認が挟まる）
            capture_edits(d, "2026-07-06", [
                _ctx("dept", "架空科A", "ai", "AI本文A", "AI打ち手A"),
            ])
            capture_edits(d, "2026-07-06", [
                _ctx("dept", "架空科A", "manual", "添削本文A", "添削打ち手A"),
            ])
            capture_edits(d, "2026-07-06", [
                _ctx("dept", "架空科A", "ai", "AI本文A改訂版", "AI打ち手A改訂版"),
            ])
            recs = load_edits(d)
            c = ne.churn_stats(recs)["2026-07-06"]
            self.assertEqual(c["ai_churn_units"], 0)

    def test_churn_zero_when_single_build(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            capture_edits(d, "2026-07-07", [
                _ctx("dept", "架空科A", "ai", "AI本文A", "AI打ち手A"),
                _ctx("dept", "架空科B", "ai", "AI本文B", "AI打ち手B"),
            ])
            recs = load_edits(d)
            c = ne.churn_stats(recs)["2026-07-07"]
            self.assertEqual(c["ai_churn_units"], 0)
            self.assertEqual(c["builds"], 1)
            self.assertEqual(c["churn_per_extra_build"], 0.0)


# ════════════════════════════════════════════════════════════
# style_stats
# ════════════════════════════════════════════════════════════
class TestStyleStats(unittest.TestCase):
    def test_style_stats_reuses_rejection_reason(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            capture_edits(d, "2026-07-09", [
                _ctx("dept", "架空科A", "ai", "本文に3件という数字が入っています", "AI打ち手A",
                    topic="admission"),
            ])
            recs = load_edits(d)
            s = ne.style_stats(recs, src="ai")
            self.assertEqual(s["n"], 1)
            self.assertEqual(s["reasons"].get("digit"), 1)


# ════════════════════════════════════════════════════════════
# banned_for
# ════════════════════════════════════════════════════════════
class TestBannedFor(unittest.TestCase):
    def test_banned_map_covers_observed_topics(self):
        topics = ["leveling", "surgery", "admission",
                 "critical_care-leveling", "emergency-leveling", "emergency-admission",
                 "critical_care-admission", "er_dept-admission", "er_dept-leveling"]
        for axis in ("dept", "ward", "hospital"):
            for topic in topics:
                with self.subTest(axis=axis, topic=topic):
                    result = ne.banned_for(axis, topic)
                    self.assertIsInstance(result, tuple)

        self.assertIn("延伸", ne.banned_for("dept", "leveling"))
        # 病棟の平準化は診療科専用レバー語(紹介/地域医療連携/紹介元)も混入する
        self.assertIn("紹介", ne.banned_for("ward", "leveling"))
        # 病院全体サマリは h_topic によらず単一の禁止語集合
        self.assertEqual(ne.banned_for("hospital", "admission"), ne.banned_for("hospital", "surgery"))
        self.assertEqual(ne.banned_for("hospital", "leveling"), ne.banned_for("hospital", "admission"))
        # 未知の(axis, topic)はKeyErrorにせず空タプル
        self.assertEqual(ne.banned_for("dept", None), ())
        self.assertEqual(ne.banned_for("dept", "no_such_topic"), ())


# ════════════════════════════════════════════════════════════
# レポート組み立て（空/smoke）
# ════════════════════════════════════════════════════════════
class TestEmptyAndSmoke(unittest.TestCase):
    def test_empty_state_dir_is_safe(self):
        with tempfile.TemporaryDirectory() as d:
            recs = load_edits(Path(d))
            self.assertEqual(recs, [])
            report = ne.build_eval_report(recs)
            self.assertEqual(report["n_records"], 0)
            self.assertEqual(report["dates"], [])
            md = ne.build_eval_md(report)
            self.assertIn("まだ台帳がありません", md)

    def test_build_eval_md_smoke(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            capture_edits(d, "2026-07-10", [
                _ctx("dept", "架空科A", "ai", "AI本文A", "AI打ち手A"),
            ])
            capture_edits(d, "2026-07-10", [
                _ctx("dept", "架空科A", "manual", "添削本文A", "添削打ち手A"),
            ])
            recs = load_edits(d)
            alert_rows = [{"alert": c["alert"], "narrative": c["narrative"]}
                         for c in _GOLDEN["alert_scoring_cases"]]
            report = ne.build_eval_report(recs, alert_rows=alert_rows)
            md = ne.build_eval_md(report)
            self.assertIn("ナラティブ品質評価", md)
            self.assertIn("アラート見出し品質", md)
            cmp_md = ne.compare_reports(report, report)
            self.assertIn("比較", cmp_md)


if __name__ == "__main__":
    unittest.main()
