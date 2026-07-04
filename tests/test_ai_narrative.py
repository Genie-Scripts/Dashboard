"""ai_narrative の機械ガード（棄却理由・数字アローリスト）のユニットテスト。

LLM呼び出し（chat_json）はテストしない。純関数の境界のみ:
  - _rejection_reason: parse/digit/banned/length の判定と allow（許容フレーズ）除去
  - _unit_allow: 病棟名の「病棟」抜き略称の許容
  - _extract_body_action: JSON抽出の頑健性
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.ai_narrative import (_extract_body_action, _is_hallucination_free,
                                  _rejection_reason, _unit_allow,
                                  _ALLOW_FACT_PHRASES,
                                  _q_latewk_discharge, _q_weekend_adm,
                                  _q_census_dip, _q_thin_latewk_adm,
                                  _q_room, _leveling_levers,
                                  _q_state_trend, _q_yoy)


def _dd(discharge=None, admission=None, census=None):
    """build_dow_unit_detail の w8 形（月..日）を最小構成で作る。"""
    out = {}
    if discharge is not None:
        out["discharge"] = {"w8": discharge}
    if admission is not None:
        out["admission"] = {"w8": admission}
    if census is not None:
        out["census"] = {"w8": census}
    return out


class TestRejectionReason(unittest.TestCase):
    def _ok(self, body="週末在院は保てています。", action="現状維持しましょう。"):
        return {"body": body, "action": action}

    def test_parse_none_and_empty(self):
        self.assertEqual(_rejection_reason(None), "parse")
        self.assertEqual(_rejection_reason({"body": "", "action": "  "}), "parse")

    def test_clean_output_accepted(self):
        self.assertIsNone(_rejection_reason(self._ok()))
        self.assertTrue(_is_hallucination_free(self._ok()))

    def test_digit_rejected(self):
        self.assertEqual(_rejection_reason(self._ok(body="あと3件で目標です。")), "digit")
        self.assertEqual(_rejection_reason(self._ok(action="５件積み増しを。")), "digit")  # 全角

    def test_allow_ward_name_digits(self):
        obj = self._ok(body="4階Aは週末も在院を保てています。")
        self.assertEqual(_rejection_reason(obj), "digit")                      # 許容なし→棄却
        self.assertIsNone(_rejection_reason(obj, allow=("4階A",)))             # 名前は許容
        # 名前以外の数字は許容後も検出する
        obj2 = self._ok(body="4階Aはあと5件で目標です。")
        self.assertEqual(_rejection_reason(obj2, allow=("4階A",)), "digit")

    def test_allow_longest_first(self):
        # 「9階B病棟」を許容していれば、重複する短い「9階B」表記の除去とも干渉しない
        obj = self._ok(body="9階B病棟では週末の補充が乏しい状況です。")
        self.assertIsNone(_rejection_reason(obj, allow=("9階B", "9階B病棟")))

    def test_allow_fact_phrase_recent4w(self):
        # 事実文由来の「直近4週」は _generate_checked が共通アローで許容する
        obj = self._ok(body="直近4週はやや崩れてきています。")
        self.assertEqual(_rejection_reason(obj), "digit")
        self.assertIsNone(_rejection_reason(obj, allow=_ALLOW_FACT_PHRASES))

    def test_banned_phrase(self):
        obj = self._ok(action="在院日数の延伸を検討しましょう。")
        self.assertEqual(_rejection_reason(obj, banned=("延伸",)), "banned")

    def test_length_runaway(self):
        obj = self._ok(body="あ" * 300, action="い" * 200)
        self.assertEqual(_rejection_reason(obj), "length")


class TestUnitAllow(unittest.TestCase):
    def test_ward_name_with_suffix(self):
        self.assertEqual(_unit_allow("9階B病棟"), ("9階B病棟", "9階B"))

    def test_dept_name_without_suffix(self):
        self.assertEqual(_unit_allow("消化器内科"), ("消化器内科",))

    def test_empty(self):
        self.assertEqual(_unit_allow(""), ())


class TestLevelingQuantizers(unittest.TestCase):
    """1-1 で追加した平準化の事実quantizer（しきい値は実データ較正 2026-07-02）。"""

    def test_latewk_strong_friday(self):
        # 金曜が支配的で週後半シェア≥0.40 → strong・金曜
        q = _q_latewk_discharge(_dd(discharge=[1, 1, 1, 1.5, 3, 1, 1.5]))
        self.assertEqual((q["level"], q["days"]), ("strong", "金曜"))
        self.assertIn("特に金曜", q["text"])

    def test_latewk_thursday_dominant(self):
        # 旧 _q_friday では「平準」に丸められた木曜集中（消化器内科型）を拾う
        q = _q_latewk_discharge(_dd(discharge=[1, 1, 1, 3, 1.2, 0.5, 0.5]))
        self.assertEqual(q["days"], "木曜")
        self.assertNotEqual(q["level"], "flat")

    def test_latewk_flat_and_none(self):
        q = _q_latewk_discharge(_dd(discharge=[1, 1, 1, 1, 1, 1, 1]))
        self.assertEqual(q["level"], "flat")
        self.assertIsNone(_q_latewk_discharge(None))
        self.assertIsNone(_q_latewk_discharge(_dd(discharge=[0] * 7)))

    def test_weekend_adm_three_levels(self):
        wd = [2, 2, 2, 2, 2]
        self.assertEqual(_q_weekend_adm(_dd(admission=wd + [1.5, 1.5]))["level"], "some")     # 0.75
        self.assertEqual(_q_weekend_adm(_dd(admission=wd + [0.8, 0.8]))["level"], "limited")  # 0.4
        self.assertEqual(_q_weekend_adm(_dd(admission=wd + [0.1, 0.1]))["level"], "none")     # 0.05
        self.assertIsNone(_q_weekend_adm(None))

    def test_census_dip_shapes(self):
        wd = [10, 10, 10, 10, 10]
        # 土曜底→日曜持ち直し（産婦人科・泌尿器科型）
        self.assertIn("土曜が底", _q_census_dip(_dd(census=wd + [8, 9.5])))
        # 日曜にかけて深くなる（消化器内科・皮膚科型）
        self.assertIn("日曜にかけて", _q_census_dip(_dd(census=wd + [9, 7.5])))
        # 土日通して低い
        self.assertIn("土日を通して", _q_census_dip(_dd(census=wd + [8.5, 8.4])))
        # ディップ8%未満は事実として出さない
        self.assertIsNone(_q_census_dip(_dd(census=wd + [9.5, 9.4])))

    def test_thin_latewk_adm(self):
        # 金曜が平日平均の5割未満 → 「金曜」
        self.assertEqual(_q_thin_latewk_adm(_dd(admission=[3, 3, 3, 3, 0.5, 1, 1])), "金曜")
        self.assertEqual(_q_thin_latewk_adm(_dd(admission=[3, 3, 3, 0.5, 3, 1, 1])), "木曜")
        # 週前半（月〜水）の谷は週末レバーにならない → None
        self.assertIsNone(_q_thin_latewk_adm(_dd(admission=[0.5, 3, 3, 3, 3, 1, 1])))
        self.assertIsNone(_q_thin_latewk_adm(None))

    def test_room_five_levels(self):
        self.assertEqual(_q_room(10, 10), "非常に大きい")
        self.assertEqual(_q_room(5, 10), "大きい")
        self.assertEqual(_q_room(3, 10), "中程度")
        self.assertEqual(_q_room(1.5, 10), "小さめ")
        self.assertEqual(_q_room(0.5, 10), "小さい")


class TestLevelingLevers(unittest.TestCase):
    def test_disperse_primary(self):
        latewk = {"level": "strong", "days": "金曜"}
        adm = {"level": "some"}
        d, r, mode = _leveling_levers("dept", latewk, adm, None)
        self.assertEqual(mode, "disperse")
        self.assertIn("金曜に寄った退院を月〜木へ", d)

    def test_refill_primary_with_thin_day(self):
        latewk = {"level": "flat", "days": "木曜"}
        adm = {"level": "none"}
        d, r, mode = _leveling_levers("dept", latewk, adm, "金曜")
        self.assertEqual(mode, "refill")
        self.assertIn("金曜など", r)   # 薄い曜日が refill 文に接地される

    def test_both_and_ward_wording(self):
        latewk = {"level": "strong", "days": "木曜"}
        adm = {"level": "limited"}
        d, r, mode = _leveling_levers("ward", latewk, adm, None)
        self.assertEqual(mode, "both")
        self.assertIn("相乗り科の木曜退院を月〜水へ", d)
        self.assertIn("週末の入院受け入れ", r)


class TestStateTrendVocabulary(unittest.TestCase):
    """意味の歪み対策①: 事実文に「拡大」を使わない（8Bが「在院が拡大傾向」と圧縮し
    悪化が改善に読める事故の再発防止。backlog §4）。"""

    def test_down_states_use_akka_not_kakudai(self):
        for retention in (0.88, 0.80):   # mild / poor
            s = _q_state_trend(retention, room_delta=1.0)   # down（のびしろ拡大方向）
            self.assertIn("悪化", s)
            self.assertNotIn("拡大", s)

    def test_all_states_kakudai_free(self):
        for retention in (None, 0.95, 0.88, 0.80):
            for rd in (None, -1.0, 0.0, 1.0):
                self.assertNotIn("拡大", _q_state_trend(retention, rd))


class TestYoyBannedLinkage(unittest.TestCase):
    """yoy を渡していないトピックで「前年」を書いたら捏造として棄却（傾向と同じ連動緩和）。"""

    def test_banned_tuples_include_zennen(self):
        from app.lib import ai_narrative as an
        for banned in (an._LEVELING_BANNED, an._EMERGENCY_LEVELING_BANNED,
                       an._EMERGENCY_ADMISSION_BANNED_BASE, an._HOSPITAL_SUMMARY_BANNED):
            self.assertIn("前年", banned)
        # admission/surgery の base には入れない（yoy を渡した時は書いてよい）
        self.assertNotIn("前年", an._ADMISSION_BANNED_BASE)
        self.assertNotIn("前年", an._SURGERY_BANNED_BASE)

    def test_fabricated_zennen_rejected(self):
        obj = {"body": "前年同期と比較するとほぼ同程度の水準です。", "action": "対応します。"}
        self.assertEqual(_rejection_reason(obj, banned=("前年",)), "banned")
        self.assertIsNone(_rejection_reason(obj))   # yoy を渡した場合は banned に入らず通る


class TestQYoy(unittest.TestCase):
    def test_above_below_same(self):
        cur10 = [10.0] * 10
        self.assertEqual(_q_yoy(cur10, [9.0] * 10), "前年同期を上回っている")
        self.assertEqual(_q_yoy(cur10, [11.0] * 10), "前年同期を下回っている")
        self.assertEqual(_q_yoy(cur10, [10.1] * 10), "前年同期と同水準で推移している")

    def test_insufficient_or_missing_prev(self):
        self.assertIsNone(_q_yoy([10.0] * 10, [None] * 10))       # NO_PREVYEAR_WARDS 型
        self.assertIsNone(_q_yoy([10.0] * 4, [9.0] * 4))          # ペア5点未満
        self.assertIsNone(_q_yoy(None, None))
        self.assertIsNone(_q_yoy([10.0] * 10, [0.0] * 10))        # 前年ゼロ

    def test_compares_tail_window(self):
        # 直近7点のみで比較する（前半の乖離は影響しない）
        cur = [100.0] * 5 + [10.0] * 7
        prev = [1.0] * 5 + [10.0] * 7
        self.assertEqual(_q_yoy(cur, prev), "前年同期と同水準で推移している")


class TestPeerTierBuffer(unittest.TestCase):
    """3-3: 境界±の緩衝帯では None（毎月の中位↔下位フリップ防止・状態ファイル無し）。"""

    def _tiers(self, n):
        from app.lib.dept_report import _peer_tier
        names = [f"科{i}" for i in range(n)]
        ratio = {name: 1.0 - i * 0.01 for i, name in enumerate(names)}   # 添字順=降順
        return [_peer_tier(name, ratio, names) for name in names]

    def test_eleven_units_with_buffers(self):
        tiers = self._tiers(11)   # frac = 0.0, 0.1, ..., 1.0
        self.assertEqual(tiers[:3], ["上位"] * 3)          # ≤0.28
        self.assertIsNone(tiers[3])                        # 0.3 緩衝帯
        self.assertEqual(tiers[4:7], ["中位"] * 3)         # 0.4〜0.6
        self.assertIsNone(tiers[7])                        # 0.7 緩衝帯
        self.assertEqual(tiers[8:], ["下位"] * 3)          # ≥0.73

    def test_small_cohort_none(self):
        from app.lib.dept_report import _peer_tier
        self.assertIsNone(_peer_tier("A", {"A": 1.0, "B": 0.9}, ["A", "B"]))   # 母数<3


class TestNadmHighlight(unittest.TestCase):
    def test_gap_phrases(self):
        from app.lib.dept_report import _nadm_highlight
        line = _nadm_highlight(20, 25, {"cur": [20.0 / 7] * 40})
        self.assertIn("あと約5件/週で目標", line)
        self.assertIn("週目標25", line)
        line2 = _nadm_highlight(30, 25, None)
        self.assertIn("目標を5件/週上回る", line2)
        self.assertIn("ほぼ目標どおり", _nadm_highlight(25, 25, None))

    def test_no_target_returns_none(self):
        from app.lib.dept_report import _nadm_highlight
        self.assertIsNone(_nadm_highlight(20, None, None))
        self.assertIsNone(_nadm_highlight(20, 0, None))


class TestExtractBodyAction(unittest.TestCase):
    def test_json_with_surrounding_noise(self):
        text = '出力します。{"body": "状況です。", "action": "対応します。"} 以上'
        self.assertEqual(_extract_body_action(text),
                         {"body": "状況です。", "action": "対応します。"})

    def test_missing_key_returns_none(self):
        self.assertIsNone(_extract_body_action('{"body": "本文のみ"}'))
        self.assertIsNone(_extract_body_action("JSONなし"))
        self.assertIsNone(_extract_body_action(""))


if __name__ == "__main__":
    unittest.main()
