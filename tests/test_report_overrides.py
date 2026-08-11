"""report_overrides（§6-1 一手の人手オーバーライド）のユニットテスト。

パーサの境界と fail-soft、適用（src="manual" 刻印・数値行保持）のみ。
レビューHTML（JS）とビルド配線は実データビルドで検証する。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from app.lib.report_overrides import (apply_override, carry_payload,
                                      default_expires, is_full_override,
                                      parse_overrides)

BASE = pd.Timestamp("2026-07-02")
BASE_S = "2026-07-02"


def _parse(text, base_date=BASE):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "overrides.md"
        p.write_text(text, encoding="utf-8")
        return parse_overrides(p, base_date)


class TestParse(unittest.TestCase):
    def test_missing_file(self):
        ov, carry, notes = parse_overrides(Path("/nonexistent/overrides.md"), BASE)
        self.assertEqual(ov, {})
        self.assertEqual(carry, {})
        self.assertEqual(notes, [])

    def test_basic_block(self):
        ov, carry, notes = _parse(
            "# コメント\n"
            f"[診療科:整形外科] base:{BASE_S} expires:2026-07-16\n"
            "body: 差し替え本文です。\n"
            "action: 差し替え一手です。\n")
        self.assertIn(("dept", "整形外科"), ov)
        blk = ov[("dept", "整形外科")]
        self.assertEqual(blk["body"], "差し替え本文です。")
        self.assertEqual(blk["action"], "差し替え一手です。")
        self.assertEqual(carry, {})
        self.assertEqual(notes, [])

    def test_partial_block_and_ward_axis(self):
        ov, _c, _n = _parse(
            f"[病棟:9階B病棟] base:{BASE_S} expires:2026-07-16\naction: 一手だけ差し替え\n")
        blk = ov[("ward", "9階B病棟")]
        self.assertIsNone(blk["body"])
        self.assertEqual(blk["action"], "一手だけ差し替え")
        self.assertFalse(is_full_override(blk))

    def test_hospital_axis_block(self):
        ov, _c, notes = _parse(
            f"[病院全体:○○病院] base:{BASE_S} expires:2026-07-16\n"
            "body: 病院全体の差し替え本文。\n"
            "action: 病院全体の差し替え一手。\n")
        self.assertIn(("hospital", "○○病院"), ov)
        blk = ov[("hospital", "○○病院")]
        self.assertEqual(blk["body"], "病院全体の差し替え本文。")
        self.assertTrue(is_full_override(blk))
        self.assertEqual(notes, [])

    def test_hospital_axis_default_unit(self):
        # REPORT_HOSPITAL_NAME 未設定時の unit（"病院全体"）でもキーが立つ
        ov, _c, _n = _parse(
            f"[病院全体:病院全体] base:{BASE_S} expires:2026-07-16\naction: 一手だけ\n")
        self.assertIn(("hospital", "病院全体"), ov)

    def test_expired_block_ignored(self):
        ov, carry, notes = _parse(
            f"[診療科:眼科] base:{BASE_S} expires:2026-07-01\nbody: 期限切れ\n")
        self.assertEqual(ov, {})
        self.assertEqual(carry, {})
        self.assertTrue(any(lv == "info" and "期限切れ" in msg for lv, msg in notes))

    def test_expires_on_base_date_still_valid(self):
        ov, _c, _n = _parse(
            f"[診療科:眼科] base:{BASE_S} expires:2026-07-02\nbody: 当日はまだ有効\n")
        self.assertIn(("dept", "眼科"), ov)

    def test_no_expires_accepted_with_warning(self):
        ov, _c, notes = _parse(f"[診療科:眼科] base:{BASE_S}\nbody: 無期限\n")
        self.assertIn(("dept", "眼科"), ov)
        self.assertTrue(any(lv == "warn" and "expires" in msg for lv, msg in notes))

    def test_bad_expires_skips_block_failsoft(self):
        ov, carry, notes = _parse(
            f"[診療科:眼科] base:{BASE_S} expires:07/16\n"
            "body: 壊れた日付\n"
            "\n"
            f"[診療科:皮膚科] base:{BASE_S} expires:2026-07-16\n"
            "body: こちらは生きる\n")
        self.assertNotIn(("dept", "眼科"), ov)
        self.assertNotIn(("dept", "眼科"), carry)
        self.assertIn(("dept", "皮膚科"), ov)
        self.assertTrue(any(lv == "warn" and "不正な日付" in msg for lv, msg in notes))

    def test_empty_block_skipped(self):
        ov, carry, notes = _parse(f"[診療科:眼科] base:{BASE_S} expires:2026-07-16\n")
        self.assertEqual(ov, {})
        self.assertEqual(carry, {})
        self.assertTrue(any("body/action" in msg for _, msg in notes))

    def test_orphan_field_line_ignored(self):
        ov, _c, notes = _parse("body: ヘッダなし\n")
        self.assertEqual(ov, {})
        self.assertTrue(any("ヘッダ行" in msg for _, msg in notes))

    def test_duplicate_unit_last_wins(self):
        ov, _c, notes = _parse(
            f"[診療科:眼科] base:{BASE_S} expires:2026-07-16\nbody: 一つ目\n\n"
            f"[診療科:眼科] base:{BASE_S} expires:2026-07-16\nbody: 二つ目（後勝ち）\n")
        self.assertEqual(ov[("dept", "眼科")]["body"], "二つ目（後勝ち）")
        self.assertTrue(any("複数" in msg for _, msg in notes))

    def test_unknown_line_warned_not_fatal(self):
        ov, _c, notes = _parse(
            f"[診療科:眼科] base:{BASE_S} expires:2026-07-16\n"
            "body: 正常\n"
            "ここに謎の行\n")
        self.assertIn(("dept", "眼科"), ov)
        self.assertTrue(any("解釈できない行" in msg for _, msg in notes))

    def test_body_containing_colon_kept_verbatim(self):
        ov, _c, _n = _parse(f"[診療科:眼科] base:{BASE_S} expires:2026-07-16\n"
                            "body: 本文にコロン: があっても1行まるごと本文\n")
        self.assertEqual(ov[("dept", "眼科")]["body"],
                         "本文にコロン: があっても1行まるごと本文")


class TestBaseReversal(unittest.TestCase):
    """§6-1反転: base一致のみactive・不一致/無しはcarry（前回添削として保持のみ）。"""

    def test_base_matches_current_goes_active(self):
        ov, carry, _n = _parse(
            f"[診療科:整形外科] base:{BASE_S} expires:2026-07-16\n"
            "body: 今回の差し替え\n")
        self.assertIn(("dept", "整形外科"), ov)
        self.assertNotIn(("dept", "整形外科"), carry)

    def test_base_past_goes_carry_not_active(self):
        ov, carry, _n = _parse(
            "[診療科:整形外科] base:2026-06-20 expires:2026-07-16\n"
            "body: 前回の差し替え\n")
        self.assertNotIn(("dept", "整形外科"), ov)
        self.assertIn(("dept", "整形外科"), carry)
        self.assertEqual(carry[("dept", "整形外科")]["base"], pd.Timestamp("2026-06-20"))

    def test_no_base_legacy_goes_carry_with_info_note(self):
        ov, carry, notes = _parse(
            "[診療科:整形外科] expires:2026-07-16\nbody: 旧形式の差し替え\n")
        self.assertNotIn(("dept", "整形外科"), ov)
        self.assertIn(("dept", "整形外科"), carry)
        self.assertTrue(any(lv == "info" and "旧形式" in msg for lv, msg in notes))

    def test_expired_block_neither_active_nor_carry(self):
        ov, carry, _n = _parse(
            "[診療科:整形外科] base:2026-06-01 expires:2026-06-15\n"
            "body: とっくに失効\n")
        self.assertNotIn(("dept", "整形外科"), ov)
        self.assertNotIn(("dept", "整形外科"), carry)

    def test_multiple_carry_candidates_keep_newest_base(self):
        ov, carry, _n = _parse(
            "[診療科:整形外科] base:2026-06-01 expires:2026-07-16\nbody: 一番古い\n\n"
            "[診療科:整形外科] base:2026-06-20 expires:2026-07-16\nbody: 新しい方\n")
        self.assertEqual(ov, {})
        self.assertEqual(carry[("dept", "整形外科")]["body"], "新しい方")
        self.assertEqual(carry[("dept", "整形外科")]["base"], pd.Timestamp("2026-06-20"))

    def test_current_and_past_block_active_wins_carry_excluded(self):
        ov, carry, _n = _parse(
            "[診療科:整形外科] base:2026-06-20 expires:2026-07-16\nbody: 過去\n\n"
            f"[診療科:整形外科] base:{BASE_S} expires:2026-07-16\nbody: 今回\n")
        self.assertEqual(ov[("dept", "整形外科")]["body"], "今回")
        self.assertNotIn(("dept", "整形外科"), carry)

    def test_attr_order_independent(self):
        ov1, _c1, _n1 = _parse(
            f"[診療科:整形外科] expires:2026-07-16 base:{BASE_S}\nbody: 順序が逆\n")
        self.assertIn(("dept", "整形外科"), ov1)
        self.assertEqual(ov1[("dept", "整形外科")]["body"], "順序が逆")

    def test_unknown_attr_warned_and_ignored(self):
        ov, _c, notes = _parse(
            f"[診療科:整形外科] base:{BASE_S} expires:2026-07-16 foo:1\n"
            "body: 未知属性つき\n")
        self.assertIn(("dept", "整形外科"), ov)
        self.assertTrue(any(lv == "warn" and "foo:1" in msg for lv, msg in notes))

    def test_bad_base_date_warns_and_keeps_as_carry(self):
        ov, carry, notes = _parse(
            "[診療科:整形外科] base:07/16 expires:2026-07-16\n"
            "body: base日付が壊れている\n")
        self.assertNotIn(("dept", "整形外科"), ov)
        self.assertIn(("dept", "整形外科"), carry)
        self.assertIsNone(carry[("dept", "整形外科")]["base"])
        self.assertTrue(any(lv == "warn" and "base:07/16" in msg for lv, msg in notes))


class TestCarryPayload(unittest.TestCase):
    def test_keys_and_values(self):
        _ov, carry, _n = _parse(
            "[診療科:整形外科] base:2026-06-20 expires:2026-07-16\n"
            "action: 前回の一手だけ\n")
        payload = carry_payload(carry)
        self.assertIn("診療科:整形外科", payload)
        v = payload["診療科:整形外科"]
        self.assertEqual(v["base"], "2026-06-20")
        self.assertEqual(v["expires"], "2026-07-16")
        self.assertIsNone(v["body"])
        self.assertEqual(v["action"], "前回の一手だけ")


class TestLevelingSkip(unittest.TestCase):
    """§6-1: 全文差し替え部門は leveling バッチでAI生成を呼ばない（候補選定は不変）。"""

    def test_skip_prevents_generation_only_for_skipped(self):
        from app.lib import ai_narrative as an
        called = []
        orig = an._generate_checked

        def fake(tag, **kw):
            called.append(tag)
            return {"body": "b", "action": "a"}

        an._generate_checked = fake
        try:
            wl = {"dept": {"units": [{"name": "A科", "room_per_week": 2.0},
                                     {"name": "B科", "room_per_week": 1.5}]}}
            an.narrate_leveling_actions(wl, {"dept": {}}, top_n=2, quiet=True,
                                        skip={"A科"})
        finally:
            an._generate_checked = orig
        self.assertEqual(len(called), 1)
        self.assertIn("B科", called[0])
        units = {u["name"]: u for u in wl["dept"]["units"]}
        self.assertNotIn("narrative", units["A科"])
        self.assertEqual(units["B科"]["narrative"], {"body": "b", "action": "a"})


class TestStripServeArgv(unittest.TestCase):
    """--serve の「PDF再作成」が自分自身を再帰起動しないための引数除去。"""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "build_dept_reports",
            Path(__file__).resolve().parent.parent / "scripts" / "build_dept_reports.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls.strip = staticmethod(mod._strip_serve_argv)

    def test_removes_serve_and_port(self):
        self.assertEqual(
            self.strip(["--no-ai", "--serve", "--port", "9000", "--keep-html"]),
            ["--no-ai", "--keep-html"])

    def test_removes_port_equals_form(self):
        self.assertEqual(self.strip(["--serve", "--port=9000", "--axes", "dept"]),
                         ["--axes", "dept"])

    def test_removes_serve_timeout_both_forms(self):
        self.assertEqual(self.strip(["--serve", "--serve-timeout", "30", "--no-ai"]),
                         ["--no-ai"])
        self.assertEqual(self.strip(["--serve", "--serve-timeout=30", "--no-ai"]),
                         ["--no-ai"])

    def test_noop_without_serve(self):
        self.assertEqual(self.strip(["--no-ai"]), ["--no-ai"])


class TestApply(unittest.TestCase):
    MOVE = {"body": "AI本文", "action": "AI一手", "src": "ai",
            "topic": "leveling", "surg_line": "全麻：直近7日 25件"}

    def test_full_override(self):
        ov = {"body": "手動本文", "action": "手動一手"}
        out = apply_override(self.MOVE, ov)
        self.assertEqual(out["body"], "手動本文")
        self.assertEqual(out["action"], "手動一手")
        self.assertEqual(out["src"], "manual")
        self.assertEqual(out["ov_fields"], ["body", "action"])
        # 数値行・トピックはデータ由来のため保持
        self.assertEqual(out["surg_line"], "全麻：直近7日 25件")
        self.assertEqual(out["topic"], "leveling")
        self.assertTrue(is_full_override(ov))

    def test_partial_override_keeps_other_field(self):
        out = apply_override(self.MOVE, {"body": None, "action": "手動一手だけ"})
        self.assertEqual(out["body"], "AI本文")
        self.assertEqual(out["action"], "手動一手だけ")
        self.assertEqual(out["ov_fields"], ["action"])
        self.assertFalse(is_full_override({"body": None, "action": "手動一手だけ"}))

    def test_original_move_not_mutated(self):
        apply_override(self.MOVE, {"body": "x", "action": "y"})
        self.assertEqual(self.MOVE["src"], "ai")

    def test_default_expires(self):
        self.assertEqual(default_expires(BASE), "2026-07-16")

    def test_ai_body_ai_action_preserved(self):
        # AI/定型文をレビューHTMLで見比べるための退避フィールド（§改修）。
        # apply_override は body/action を差し替えるが、退避済みの ai_body/ai_action は
        # 上書き・削除しない。
        move = {**self.MOVE, "ai_body": "AI本文(退避)", "ai_action": "AI一手(退避)"}
        out = apply_override(move, {"body": "手動本文", "action": "手動一手"})
        self.assertEqual(out["body"], "手動本文")
        self.assertEqual(out["action"], "手動一手")
        self.assertEqual(out["ai_body"], "AI本文(退避)")
        self.assertEqual(out["ai_action"], "AI一手(退避)")


if __name__ == "__main__":
    unittest.main()
