"""添削フィードバックループ（P0 capture / P1 pairing）のユニットテスト。

LLM・実ビルドは呼ばない。捕捉の追記/dedup と、突き合わせの ai→manual ペア復元のみ検証する。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.report_feedback import (capture_edits, load_edits,
                                     pair_corrections, build_digest_md)


def _ctx(axis, unit, src, body, action, topic="admission", state=None):
    return {"axis": axis, "unit": unit, "_state": state or {"k": "v"},
            "move": {"src": src, "body": body, "action": action, "topic": topic}}


class TestCaptureP0(unittest.TestCase):
    def test_append_and_dedup(self):
        with tempfile.TemporaryDirectory() as d:
            ctxs = [_ctx("診療科", "整形外科", "ai", "AI本文", "AI打ち手")]
            p1 = capture_edits(d, "2026-07-20", ctxs)
            self.assertIsNotNone(p1)
            # 同一状態の再ビルド → 追記されない（None）
            self.assertIsNone(capture_edits(d, "2026-07-20", ctxs))
            recs = load_edits(Path(d))
            self.assertEqual(len(recs), 1)

    def test_state_change_appends(self):
        with tempfile.TemporaryDirectory() as d:
            capture_edits(d, "2026-07-20", [_ctx("診療科", "整形外科", "ai", "AI本文", "AI打ち手")])
            # 人が override → src/body 変化 → 追記される
            capture_edits(d, "2026-07-20", [_ctx("診療科", "整形外科", "manual", "人の本文", "人の打ち手")])
            recs = load_edits(Path(d))
            self.assertEqual(len(recs), 2)
            self.assertEqual([r["src"] for r in recs], ["ai", "manual"])

    def test_hospital_ctx_included(self):
        with tempfile.TemporaryDirectory() as d:
            hosp = _ctx("病院全体", "サマリ", "ai", "全体本文", "全体打ち手")
            capture_edits(d, "2026-07-20", [], hosp_ctx=hosp)
            recs = load_edits(Path(d))
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["axis"], "病院全体")

    def test_skips_moveless_and_is_failsoft(self):
        with tempfile.TemporaryDirectory() as d:
            # move 無し・unit 無しは無視。壊れた入力でも例外を投げない
            self.assertIsNone(capture_edits(d, "2026-07-20", [{"axis": "診療科"}, "not-a-dict"]))


class TestPairingP1(unittest.TestCase):
    def _mk(self, d):
        capture_edits(d, "2026-07-20", [_ctx("診療科", "整形外科", "ai", "AI本文", "AI打ち手")])
        capture_edits(d, "2026-07-20", [_ctx("診療科", "整形外科", "manual", "AI本文", "人の打ち手")])
        # override が無いユニット（AI採択のまま）→ ペアにならない
        capture_edits(d, "2026-07-20", [_ctx("病棟", "9階B病棟", "ai", "病棟本文", "病棟打ち手")])

    def test_pairs_only_edited_units(self):
        with tempfile.TemporaryDirectory() as d:
            self._mk(d)
            pairs = pair_corrections(load_edits(Path(d)))
            self.assertEqual(len(pairs), 1)
            p = pairs[0]
            self.assertEqual(p["unit"], "整形外科")
            self.assertEqual(p["changed"], ["action"])       # body 同一・action だけ変化
            self.assertEqual(p["ai_action"], "AI打ち手")
            self.assertEqual(p["human_action"], "人の打ち手")
            self.assertTrue(p["had_ai"])

    def test_manual_without_ai_still_pairs(self):
        with tempfile.TemporaryDirectory() as d:
            # AI 記録が別 run で無い（手編集直で override）ケースでも manual があればペア化
            capture_edits(d, "2026-07-21", [_ctx("診療科", "眼科", "manual", "手本文", "手打ち手")])
            pairs = pair_corrections(load_edits(Path(d)))
            self.assertEqual(len(pairs), 1)
            self.assertFalse(pairs[0]["had_ai"])

    def test_digest_has_levers_section(self):
        with tempfile.TemporaryDirectory() as d:
            self._mk(d)
            md = build_digest_md(pair_corrections(load_edits(Path(d))))
            self.assertIn("levers 候補", md)
            self.assertIn("人の打ち手", md)

    def test_empty_digest(self):
        self.assertIn("まだ添削信号がありません", build_digest_md([]))


class TestReversalFlow(unittest.TestCase):
    """§6-1反転後: 新base_dateの初回ビルドは全ユニットai既定 → レビュー保存後の
    再ビルドでmanualに変わる、という往復でも ai→manual ペアが復元できることを確認する
    （report_feedback.py 自体はコード変更なし・回帰確認）。"""

    def test_new_base_date_first_build_ai_then_saved_manual_pairs(self):
        with tempfile.TemporaryDirectory() as d:
            # 新しい基準日の初回ビルド: overrideは反転によりどのユニットにも適用されない
            # ＝全ユニット src="ai"（AI文が既定）。
            capture_edits(d, "2026-08-11", [
                _ctx("診療科", "整形外科", "ai", "AI本文", "AI打ち手"),
            ])
            # レビュー画面で「前回の添削を使う」または手直しして保存 → 同一base_dateで
            # 再ビルド（PDF再作成）すると src="manual" に遷移する。
            capture_edits(d, "2026-08-11", [
                _ctx("診療科", "整形外科", "manual", "AI本文", "添削後の打ち手"),
            ])
            pairs = pair_corrections(load_edits(Path(d)))
            self.assertEqual(len(pairs), 1)
            p = pairs[0]
            self.assertTrue(p["had_ai"])
            self.assertIsNotNone(p["ai_body"])
            self.assertEqual(p["ai_body"], "AI本文")
            self.assertEqual(p["human_action"], "添削後の打ち手")


if __name__ == "__main__":
    unittest.main()
