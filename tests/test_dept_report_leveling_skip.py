"""部門レポート「この期間の一手」: leveling バッチ生成の無駄打ち省略(skip)のテスト。

narrate_leveling_actions はバッチで先に全ユニットへ narrative を付けるが、per-unit
ループで実際に u["narrative"] が読まれるのは「非救急×topic=leveling×room>0.5」の
ときだけ（救急病棟／topicがadmission・surgeryに決まる／room<=0.5 のユニットは
後段で生成結果を読まずに捨てる）。本テストは build_dept_report_contexts を
重い前処理・LLM呼び出しをすべてフェイクに差し替えて呼び、

  1. skip されるユニット集合が正しいこと（救急・admission/surgery・room<=0.5・
     既存の人手オーバーライドの4条件）
  2. 「生成だけ省く」変更の前後でレポート本文（move の body/action/src/topic）が
     一切変わらないこと（skip を尊重するフェイク vs 尊重せず無駄打ちするフェイクの
     2通りで比較し一致を確認する）

を検証する。LLM呼び出しはしない（フェイクのみ）。

実行: リポジトリルートで
    python -m pytest tests/ -q
"""
import contextlib
import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib import dept_report as dr  # noqa: E402

BASE_DATE = pd.Timestamp("2026-07-19")

# 診療科軸: 循環器内科=leveling(通常・skipされない) / 呼吸器内科=admission確定 /
# 整形外科(外科系)=surgery確定 / 消化器内科=room<=0.5(のびしろ無し) / 腎臓内科=人手オーバーライド。
DEPT_UNITS = [
    {"name": "循環器内科", "room_per_week": 10.0, "retention": 0.85, "room_delta_4w": 1.0},
    {"name": "呼吸器内科", "room_per_week": 3.0, "retention": 0.90, "room_delta_4w": 0.5},
    {"name": "整形外科", "room_per_week": 0.6, "retention": 0.95, "room_delta_4w": 0.1},
    {"name": "消化器内科", "room_per_week": 0.3, "retention": 0.98, "room_delta_4w": 0.0},
    {"name": "腎臓内科", "room_per_week": 8.0, "retention": 0.80, "room_delta_4w": 1.5},
]
# 病棟軸: 04A=救急病棟(EMERGENCY_WARDS) / 09B病棟=leveling(通常・skipされない) /
# 10A病棟=admission確定。
WARD_UNITS = [
    {"name": "04A", "room_per_week": 5.0, "retention": 0.70, "room_delta_4w": 2.0},
    {"name": "09B病棟", "room_per_week": 4.0, "retention": 0.75, "room_delta_4w": 0.8},
    {"name": "10A病棟", "room_per_week": 3.0, "retention": 0.60, "room_delta_4w": 1.0},
]

CAND = {
    "dept": [(u["name"], u["name"]) for u in DEPT_UNITS],
    "ward": [(u["name"], u["name"]) for u in WARD_UNITS],
}

TARGETS = {
    "new_admission": {"dept": {"呼吸器内科": 20}, "ward": {"10A病棟": 20}},
    "inpatient": {"dept": {}, "ward": {}, "ward_beds": {}},
}
SURG_TARGETS = {"整形外科": 10}

R7_INP = {"by_dept": {}, "by_ward": {}}
R7_NADM = {"by_dept": {"呼吸器内科": 5}, "by_ward": {"10A病棟": 5}}
R7_SURG = {"by_dept": {"整形外科": 1}}

# §6-1 人手オーバーライド（全文差し替え＝既存の full_ov skip 対象）
OVERRIDES = {("dept", "腎臓内科"): {"body": "手動本文差し替え", "action": "手動一手差し替え"}}

FAKE_PART = {"kind": "A", "name": "ダミー", "badge": None, "note": "", "is_dow": False,
             "_data": {"cur": [], "prev": [], "proj": None}, "_ref": 0, "_ref_label": "",
             "_unit": "", "_win": 1, "_color": "#000"}


def _wl(units):
    return {"units": copy.deepcopy(units), "total": {"retention": 0.8}}


def _make_leveling_fake(respect_skip: bool, skip_log: list):
    """narrate_leveling_actions のフェイク。

    respect_skip=True: 本物と同じ「上位top_n選定→skip対象は生成しない」を模す
      （今回の実装が実際に通す経路）。
    respect_skip=False: skip を無視して top_n 全件に生成する（改修前の「捨てられる
      生成も律儀に行っていた」旧挙動を模した対照）。
    """
    def fake(weekend_leveling, dow_unit_detail=None, top_n=6, model=None,
             temperature=None, quiet=False, peers=None, deltas=None, skip=None):
        for entity, wl in weekend_leveling.items():
            eff_skip = set(skip or ()) if respect_skip else set()
            skip_log.append((entity, set(skip or ())))
            targets = sorted(wl["units"], key=lambda u: u.get("room_per_week", 0) or 0,
                              reverse=True)[:top_n]
            for u in targets:
                if u["name"] in eff_skip:
                    continue
                u["narrative"] = {"body": f"LEVAI::{u['name']}",
                                  "action": f"LEVAI-ACT::{u['name']}", "src": "ai"}
        return weekend_leveling
    return fake


class _Pipeline:
    """build_dept_report_contexts の重い前処理・LLM呼び出し関数をフェイクに
    差し替えて呼ぶハーネス。adm/surg の実データは一切使わない。

    admission_fake/surgery_fake/em_admission_fake/em_leveling_fake: 個々の narrate_*
    をこのハーネス既定のフェイク（呼び出し記録のみ）から差し替えたいテスト
    （例外・sleep 挿入など）向けの任意オーバーライド（省略時は既定のまま＝後方互換）。
    """

    def __init__(self, leveling_fake, admission_fake=None, surgery_fake=None,
                 em_admission_fake=None, em_leveling_fake=None):
        self.leveling_fake = leveling_fake
        self.admission_calls = []
        self.surgery_calls = []
        self.em_admission_calls = []
        self.em_leveling_calls = []
        self._admission_fake_override = admission_fake
        self._surgery_fake_override = surgery_fake
        self._em_admission_fake_override = em_admission_fake
        self._em_leveling_fake_override = em_leveling_fake

    def _fake_wcr(self, adm, base_date, entity=None, weeks=8):
        return _wl(DEPT_UNITS if entity == "dept" else WARD_UNITS)

    def _fake_cand(self, entity):
        return "col", CAND[entity]

    def _fake_det(self, adm, base_date, entity, report_units):
        return {}

    def _fake_r7_inp(self, adm, base_date):
        return copy.deepcopy(R7_INP)

    def _fake_r7_nadm(self, adm, base_date):
        return copy.deepcopy(R7_NADM)

    def _fake_r7_surg(self, surg, base_date):
        return copy.deepcopy(R7_SURG)

    def _fake_ranking(self, *args, **kwargs):
        return pd.DataFrame()

    def _fake_build_parts(self, *args, **kwargs):
        return {"A": dict(FAKE_PART)}

    def _fake_render_svg(self, *args, **kwargs):
        return ""

    def _fake_none(self, *args, **kwargs):
        return None

    def _fake_admission(self, name, *a, **kw):
        self.admission_calls.append(name)
        return {"body": f"ADM::{name}", "action": f"ADM-ACT::{name}", "src": "ai"}

    def _fake_surgery(self, name, *a, **kw):
        self.surgery_calls.append(name)
        return {"body": f"SURG::{name}", "action": f"SURG-ACT::{name}", "src": "ai"}

    def _fake_em_admission(self, name, *a, **kw):
        self.em_admission_calls.append(name)
        return {"body": f"EMADM::{name}", "action": f"EMADM-ACT::{name}", "src": "ai"}

    def _fake_em_leveling(self, name, *a, **kw):
        self.em_leveling_calls.append(name)
        return {"body": f"EMLEV::{name}", "action": f"EMLEV-ACT::{name}", "src": "ai"}

    def run(self):
        admission_fake = self._admission_fake_override or self._fake_admission
        surgery_fake = self._surgery_fake_override or self._fake_surgery
        em_admission_fake = self._em_admission_fake_override or self._fake_em_admission
        em_leveling_fake = self._em_leveling_fake_override or self._fake_em_leveling
        patches = [
            mock.patch.object(dr, "weekend_census_retention", self._fake_wcr),
            mock.patch.object(dr, "_dow_unit_candidates", self._fake_cand),
            mock.patch.object(dr, "build_dow_unit_detail", self._fake_det),
            mock.patch.object(dr, "rolling7_inpatient_avg", self._fake_r7_inp),
            mock.patch.object(dr, "rolling7_new_admission", self._fake_r7_nadm),
            mock.patch.object(dr, "rolling7_surgery", self._fake_r7_surg),
            mock.patch.object(dr, "build_dept_ranking", self._fake_ranking),
            mock.patch.object(dr, "build_surgery_ranking", self._fake_ranking),
            mock.patch.object(dr, "_build_parts", self._fake_build_parts),
            mock.patch.object(dr, "render_trend_svg", self._fake_render_svg),
            mock.patch.object(dr, "_unit_profit_series", self._fake_none),
            mock.patch.object(dr, "_q_planned_mix", self._fake_none),
            mock.patch.object(dr, "_q_or_load", self._fake_none),
            mock.patch.object(dr, "_q_holiday_week", self._fake_none),
            mock.patch.object(dr, "narrate_leveling_actions", self.leveling_fake),
            mock.patch.object(dr, "narrate_admission_action", admission_fake),
            mock.patch.object(dr, "narrate_surgery_action", surgery_fake),
            mock.patch.object(dr, "narrate_emergency_admission_action", em_admission_fake),
            mock.patch.object(dr, "narrate_emergency_leveling_action", em_leveling_fake),
        ]
        with contextlib.ExitStack() as st:
            for p in patches:
                st.enter_context(p)
            return dr.build_dept_report_contexts(
                adm=pd.DataFrame(), surg=pd.DataFrame(),
                targets=TARGETS, surg_targets=SURG_TARGETS,
                profit_monthly=pd.DataFrame(),
                base_date=BASE_DATE, generated_at=BASE_DATE,
                hospital_name="テスト病院", with_ai=True, axes=("dept", "ward"), quiet=True,
                profit_breakdown=None, delta_anchor=None, overrides=OVERRIDES)


def _moves_by_unit(contexts):
    return {(c["axis"], c["unit"]): c["move"] for c in contexts}


class TestLevelingBatchSkipSet(unittest.TestCase):
    """skip されるユニット集合が正しいこと（3条件＋既存の人手オーバーライド）。"""

    def setUp(self):
        skip_log = []
        pipeline = _Pipeline(_make_leveling_fake(respect_skip=True, skip_log=skip_log))
        self.contexts = pipeline.run()
        self.pipeline = pipeline
        self.skip_by_entity = dict(skip_log)

    def test_dept_skip_set(self):
        # 呼吸器内科=admission確定／整形外科=surgery確定／消化器内科=room<=0.5／
        # 腎臓内科=人手オーバーライド(既存) → いずれもskip。循環器内科(leveling・room>0.5)は非skip。
        self.assertEqual(self.skip_by_entity["dept"],
                         {"呼吸器内科", "整形外科", "消化器内科", "腎臓内科"})

    def test_ward_skip_set(self):
        # 04A=救急病棟／10A病棟=admission確定 → skip。09B病棟(leveling・room>0.5)は非skip。
        self.assertEqual(self.skip_by_entity["ward"], {"04A", "10A病棟"})

    def test_leveling_narrative_used_only_for_non_skipped(self):
        moves = _moves_by_unit(self.contexts)
        self.assertEqual(moves[("dept", "循環器内科")]["body"], "LEVAI::循環器内科")
        self.assertEqual(moves[("ward", "09B病棟")]["body"], "LEVAI::09B病棟")

    def test_admission_surgery_emergency_routes_bypass_leveling_narrative(self):
        # 呼吸器内科は副トピック=leveling(P3の軽い併記)が本文末尾に付くため startswith で判定
        moves = _moves_by_unit(self.contexts)
        self.assertTrue(moves[("dept", "呼吸器内科")]["body"].startswith("ADM::呼吸器内科"))
        self.assertTrue(moves[("dept", "整形外科")]["body"].startswith("SURG::整形外科"))
        self.assertTrue(moves[("ward", "04A")]["body"].startswith("EMLEV::04A"))
        self.assertTrue(moves[("ward", "10A病棟")]["body"].startswith("ADM::10A病棟"))
        # room<=0.5 は topic=leveling でも _fallback_move（現状維持の定型文）で
        # narrative は読まれない
        self.assertNotIn("LEVAI::", moves[("dept", "消化器内科")]["body"])

    def test_full_override_dept_gets_manual_text(self):
        moves = _moves_by_unit(self.contexts)
        self.assertEqual(moves[("dept", "腎臓内科")]["body"], "手動本文差し替え")
        self.assertEqual(moves[("dept", "腎臓内科")]["src"], "manual")

    def test_narrate_action_call_counts(self):
        self.assertEqual(self.pipeline.admission_calls, ["呼吸器内科", "10A病棟"])
        self.assertEqual(self.pipeline.surgery_calls, ["整形外科"])
        self.assertEqual(self.pipeline.em_leveling_calls, ["04A"])
        self.assertEqual(self.pipeline.em_admission_calls, [])


class TestOutputUnchangedBySkipOptimization(unittest.TestCase):
    """「生成だけ省く」変更でレポート本文が変わらないこと。

    skip を尊重して生成をスキップするフェイクと、skip を無視して律儀に全件生成する
    （＝捨てられる生成も惜しまず行っていた旧挙動を模した）フェイクの2通りで
    build_dept_report_contexts を呼び、両者の move（body/action/src/topic）が
    完全一致することを確認する。"""

    def test_moves_identical_regardless_of_wasted_generation(self):
        respecting = _Pipeline(_make_leveling_fake(respect_skip=True, skip_log=[])).run()
        ignoring = _Pipeline(_make_leveling_fake(respect_skip=False, skip_log=[])).run()

        moves_a = _moves_by_unit(respecting)
        moves_b = _moves_by_unit(ignoring)
        self.assertEqual(set(moves_a), set(moves_b))
        for key in moves_a:
            for field in ("body", "action", "src", "topic"):
                self.assertEqual(moves_a[key].get(field), moves_b[key].get(field),
                                 f"{key} の {field} が skip 有無で変わった")


if __name__ == "__main__":
    unittest.main()
