"""Track B P3「事実注入」: ①-6 census driver（在院の増減が入口/出口どちらで動いて
いるか）と ①-7 next_week_calendar（来週の暦構造）、および turn_m のパス1 hoist の
テスト（合成データのみ・密閉。実データ/常駐サーバは一切使わない）。

対象:
  - app.lib.dept_report._q_census_driver   （S1: inlet/outlet/both/None の4分岐）
  - app.lib.dept_report._q_next_week_calendar（S5: calendar_preview.build_week_preview の
    ラップ＝数字なし文への作り直し）
  - app.lib.dept_report.build_dept_report_contexts（S2: turn_m のパス1 hoist で
    move["turn_line"] が turnover_metrics 直呼びと同値であること。S4: driver/drivers の
    配線が narrate_admission_action/narrate_surgery_action/narrate_leveling_actions の
    kwargs に届いていること）
  - Track B 未配線分の本線接続（次週暦→一手プロンプト・平準化レバー分岐）:
    S9: build_dept_report_contexts が算出する next_week_fact/force_disperse が
    narrate_admission_action/narrate_surgery_action/narrate_leveling_actions の kwargs
    に届くこと（DriverWiringTest 拡張）。S10: dept_report._fallback_move(force_disperse=)
    が _leveling_levers を disperse に固定すること。S11: _generate_checked まで実プロンプト
    を通し、base_date=2026-09-18（5連休直前）で全ユニットの実プロンプトに next_week_fact
    が載り leveling が disperse に固定されること／base_date=2026-07-03（通常週）では
    next_week 関連の文言が一切載らないこと。

実行: リポジトリルートで
    .venv/bin/python -m pytest tests/test_dept_report_census_driver.py -q
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
from app.lib import ai_narrative as an  # noqa: E402
from app.lib.dept_report import _q_census_driver, _q_next_week_calendar  # noqa: E402
from app.lib.metrics import turnover_metrics, format_turn_line  # noqa: E402


def _mk_adm(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if "科_表示" not in df.columns:
        df["科_表示"] = True
    if "病棟_表示" not in df.columns:
        df["病棟_表示"] = True
    return df


def _period_rows(start, days, *, dept=None, ward=None, nadm, tin, disch, tout, census):
    idx = pd.date_range(start, periods=days, freq="D")
    rows = []
    for d in idx:
        rows.append({
            "日付": d, "診療科名": dept or "内科A", "病棟コード": ward or "W1",
            "新入院患者数": nadm, "転入患者数": tin,
            "退院合計": disch, "転出患者数": tout,
            "在院患者数": census,
        })
    return rows


BASE = pd.Timestamp("2026-07-19")
# ①-1 窓: cur=[base-27, base]=2026-06-22..2026-07-19（28日）
#         prev=[base-55, base-28]=2026-05-25..2026-06-21（28日）
CUR_START = BASE - pd.Timedelta(days=27)
PREV_START = BASE - pd.Timedelta(days=55)


class QCensusDriverUnitTest(unittest.TestCase):
    """_q_census_driver 単体（診療科軸・turn_m は直接与える）。"""

    def _adm(self, *, prev, cur, group_col="診療科名", unit="内科A"):
        rows = (_period_rows(PREV_START, 28, dept=unit, **prev)
               + _period_rows(CUR_START, 28, dept=unit, **cur))
        return _mk_adm(rows)

    def test_inlet_only(self):
        # 新入院+50%（56→84）・退院合計は不変（56→56）→ inlet
        adm = self._adm(prev=dict(nadm=2, tin=0, disch=2, tout=0, census=30),
                        cur=dict(nadm=3, tin=0, disch=2, tout=0, census=30))
        q = _q_census_driver(adm, BASE, "診療科名", "内科A", turn_m={"alos_28d": 10.0, "alos_prev_28d": 10.0})
        self.assertEqual(q["level"], "inlet")
        self.assertEqual(q["alos"], "flat")
        self.assertIn("入口", q["text"])
        self.assertNotIn("出口", q["text"])

    def test_outlet_only(self):
        # 退院合計+50%（56→84）・新入院は不変 → outlet
        adm = self._adm(prev=dict(nadm=2, tin=0, disch=2, tout=0, census=40),
                        cur=dict(nadm=2, tin=0, disch=3, tout=0, census=40))
        q = _q_census_driver(adm, BASE, "診療科名", "内科A", turn_m=None)
        self.assertEqual(q["level"], "outlet")
        self.assertIn("出口", q["text"])
        self.assertNotIn("入口", q["text"])

    def test_both(self):
        # 新入院+50%・退院合計+50% の両方 → both
        adm = self._adm(prev=dict(nadm=2, tin=0, disch=2, tout=0, census=30),
                        cur=dict(nadm=3, tin=0, disch=3, tout=0, census=30))
        q = _q_census_driver(adm, BASE, "診療科名", "内科A", turn_m=None)
        self.assertEqual(q["level"], "both")
        self.assertIn("入口", q["text"])
        self.assertIn("出口", q["text"])

    def test_none_when_both_under_threshold(self):
        # 変化ゼロ → None
        adm = self._adm(prev=dict(nadm=2, tin=0, disch=2, tout=0, census=30),
                        cur=dict(nadm=2, tin=0, disch=2, tout=0, census=30))
        self.assertIsNone(_q_census_driver(adm, BASE, "診療科名", "内科A", turn_m=None))

    def test_n_lt_20_guard(self):
        # 前窓 IN/OUT が n<20（1日あたり0.5人×28日=14人）→ 変化率は大きくても None
        adm = self._adm(prev=dict(nadm=0.5, tin=0, disch=0.5, tout=0, census=10),
                        cur=dict(nadm=5, tin=0, disch=0.5, tout=0, census=10))
        self.assertIsNone(_q_census_driver(adm, BASE, "診療科名", "内科A", turn_m=None))

    def test_ward_axis_transfers_avoid_misclassification(self):
        """07B相当の病棟型ケース: 新入院・退院それぞれ単独では+150%だが、転入・転出が
        逆方向に同じだけ動くため恒等式の純増減はほぼゼロ（＝IN/OUTフル計は0%）。
        転入・転出を落として新入院/退院だけで判定すると誤って both になる
        （両方とも|Δ|=150%≥5%のため）が、正しい実装は None を返すこと。"""
        rows = (
            _period_rows(PREV_START, 28, ward="07B", nadm=2, tin=18, disch=2, tout=18, census=100)
            + _period_rows(CUR_START, 28, ward="07B", nadm=5, tin=15, disch=5, tout=15, census=100)
        )
        adm = _mk_adm(rows)
        # 誤実装（転入出を落とす）だと inlet も outlet も"検出"されてしまうことを示す対照
        naive_in_prev, naive_in_cur = 2 * 28, 5 * 28
        naive_out_prev, naive_out_cur = 2 * 28, 5 * 28
        self.assertGreaterEqual(abs((naive_in_cur - naive_in_prev) / naive_in_prev * 100), 5)
        self.assertGreaterEqual(abs((naive_out_cur - naive_out_prev) / naive_out_prev * 100), 5)
        # 正しい実装（転入出込み）は IN=OUT=560（両窓とも不変）→ None
        self.assertIsNone(_q_census_driver(adm, BASE, "病棟コード", "07B", turn_m=None))

    def test_alos_up_and_down_and_none_degeneration(self):
        base_adm = self._adm(prev=dict(nadm=2, tin=0, disch=2, tout=0, census=30),
                             cur=dict(nadm=3, tin=0, disch=2, tout=0, census=30))
        up = _q_census_driver(base_adm, BASE, "診療科名", "内科A",
                              turn_m={"alos_28d": 12.0, "alos_prev_28d": 10.0})  # +20%
        self.assertEqual(up["alos"], "up")
        self.assertIn("長くなっている", up["text"])

        down = _q_census_driver(base_adm, BASE, "診療科名", "内科A",
                                turn_m={"alos_28d": 8.0, "alos_prev_28d": 10.0})  # -20%
        self.assertEqual(down["alos"], "down")
        self.assertIn("短くなっている", down["text"])

        # turn_m が None（＝turnover_metrics 側もデータ不足）のときは flat に縮退し
        # 在院日数の言及を文に出さない。
        none_turn = _q_census_driver(base_adm, BASE, "診療科名", "内科A", turn_m=None)
        self.assertEqual(none_turn["alos"], "flat")
        self.assertNotIn("在院日数", none_turn["text"])

    def test_text_has_no_digits(self):
        """_generate_checked の digit ガード（ai_narrative._DIGIT_RE）に弾かれないよう、
        text は常に数字を含まない。"""
        import re
        adm = self._adm(prev=dict(nadm=2, tin=0, disch=2, tout=0, census=30),
                        cur=dict(nadm=3, tin=0, disch=3, tout=0, census=30))
        q = _q_census_driver(adm, BASE, "診療科名", "内科A",
                             turn_m={"alos_28d": 12.0, "alos_prev_28d": 10.0})
        self.assertIsNone(re.search(r"[0-9０-９]", q["text"]))


class QNextWeekCalendarTest(unittest.TestCase):
    """_q_next_week_calendar（calendar_preview.build_week_preview のラップ）。"""

    def test_silver_week_run5(self):
        q = _q_next_week_calendar(pd.Timestamp("2026-09-14"))
        self.assertIsNotNone(q)
        self.assertEqual(q["run_len"], 5)
        self.assertIn("連休", q["text"])
        self.assertNotRegex(q["text"], r"[0-9０-９]")

    def test_ordinary_week_none(self):
        self.assertIsNone(_q_next_week_calendar(pd.Timestamp("2026-07-03")))

    def test_golden_week_next_week_ordinary_is_none(self):
        # GW休日当日(2026-05-04)基準・翌週(05-11〜17)は平常週 → None
        self.assertIsNone(_q_next_week_calendar(pd.Timestamp("2026-05-04")))

    def test_new_year_season_next_week_ordinary_is_none(self):
        # 成人の日通過後(2026-01-18)・翌週(01-19〜25)は平常週 → None
        self.assertIsNone(_q_next_week_calendar(pd.Timestamp("2026-01-18")))

    def test_obon_next_week_ordinary_is_none(self):
        self.assertIsNone(_q_next_week_calendar(pd.Timestamp("2026-08-14")))

    def test_short_biz_days_branch_text(self):
        # 2026-02-02基準・翌週は営業日4日だが連休(run_len>=3)は掛からない
        q = _q_next_week_calendar(pd.Timestamp("2026-02-02"))
        self.assertIsNotNone(q)
        self.assertLess(q["run_len"], 3)
        self.assertEqual(q["biz_days"], 4)
        self.assertIn("営業日が通常より少ない", q["text"])
        self.assertNotRegex(q["text"], r"[0-9０-９]")


# ════════════════════════════════════════════════════════════
# S2 + S4: build_dept_report_contexts 経由の統合テスト（turn_m hoist parity・
# driver/drivers 配線）。weekend_census_retention 等の重い前処理と narrate_* は
# フェイクに差し替え、adm だけ実データ相当の合成 DataFrame を渡す。
# ════════════════════════════════════════════════════════════
DEPT_UNITS = [
    {"name": "循環器内科", "room_per_week": 10.0, "retention": 0.85, "room_delta_4w": 1.0},
    {"name": "呼吸器内科", "room_per_week": 3.0, "retention": 0.90, "room_delta_4w": 0.5},
    {"name": "整形外科", "room_per_week": 0.6, "retention": 0.95, "room_delta_4w": 0.1},
]
CAND = [(u["name"], u["name"]) for u in DEPT_UNITS]
TARGETS = {
    "new_admission": {"dept": {"呼吸器内科": 20}, "ward": {}},
    "inpatient": {"dept": {}, "ward": {}, "ward_beds": {}},
}
SURG_TARGETS = {"整形外科": 10}
R7_INP = {"by_dept": {}, "by_ward": {}}
R7_NADM = {"by_dept": {"呼吸器内科": 5}, "by_ward": {}}
R7_SURG = {"by_dept": {"整形外科": 1}}

FAKE_PART = {"kind": "A", "name": "ダミー", "badge": None, "note": "", "is_dow": False,
             "_data": {"cur": [], "prev": [], "proj": None}, "_ref": 0, "_ref_label": "",
             "_unit": "", "_win": 1, "_color": "#000"}


def _wl(units):
    return {"units": copy.deepcopy(units), "total": {"retention": 0.8}}


def _build_census_adm():
    """3科・56日分（直近28日 vs 前28日）の合成 adm。
    呼吸器内科=inlet／整形外科=変化なし(None)／循環器内科=outlet になるよう設計
    （在院患者数は alos が flat のまま保たれるよう窓ごとに退院数と比例させる）。
    """
    rows = []
    # 呼吸器内科: 新入院 56→84(+50%)・退院合計 56→56(0%) → inlet
    rows += _period_rows(PREV_START, 28, dept="呼吸器内科", nadm=2, tin=0, disch=2, tout=0, census=30)
    rows += _period_rows(CUR_START, 28, dept="呼吸器内科", nadm=3, tin=0, disch=2, tout=0, census=30)
    # 整形外科: 新入院・退院とも不変 → None
    rows += _period_rows(PREV_START, 28, dept="整形外科", nadm=2, tin=0, disch=2, tout=0, census=20)
    rows += _period_rows(CUR_START, 28, dept="整形外科", nadm=2, tin=0, disch=2, tout=0, census=20)
    # 循環器内科: 新入院 56→56(0%)・退院合計 56→84(+50%) → outlet（alosはflatに保つよう
    # 在院患者数を退院数と同じ比率(1.5倍)で動かす: 40*28/56=20.0, 60*28/84=20.0）
    rows += _period_rows(PREV_START, 28, dept="循環器内科", nadm=2, tin=0, disch=2, tout=0, census=40)
    rows += _period_rows(CUR_START, 28, dept="循環器内科", nadm=2, tin=0, disch=3, tout=0, census=60)
    return _mk_adm(rows)


class _Recorder:
    def __init__(self):
        self.admission_kwargs = {}
        self.surgery_kwargs = {}
        self.leveling_drivers = None
        self.leveling_next_week = None
        self.leveling_force_disperse = None

    def fake_leveling(self, weekend_leveling, dow_unit_detail=None, top_n=6, model=None,
                      temperature=None, quiet=False, peers=None, deltas=None, skip=None,
                      drivers=None, next_week=None, force_disperse=False):
        self.leveling_drivers = dict(drivers or {})
        self.leveling_next_week = next_week
        self.leveling_force_disperse = force_disperse
        for entity, wl in weekend_leveling.items():
            eff_skip = set(skip or ())
            targets_sorted = sorted(wl["units"], key=lambda u: u.get("room_per_week", 0) or 0,
                                    reverse=True)[:top_n]
            for u in targets_sorted:
                if u["name"] in eff_skip:
                    continue
                u["narrative"] = {"body": f"LEVAI::{u['name']}", "action": "act", "src": "ai"}
        return weekend_leveling

    def fake_admission(self, name, entity, na, na_tgt, **kwargs):
        self.admission_kwargs[name] = kwargs
        return {"body": f"ADM::{name}", "action": "act", "src": "ai"}

    def fake_surgery(self, name, sv, surg_tgt, **kwargs):
        self.surgery_kwargs[name] = kwargs
        return {"body": f"SURG::{name}", "action": "act", "src": "ai"}


def _run_pipeline(adm, recorder, base_date=BASE):
    def _fake_wcr(adm, base_date, entity=None, weeks=8):
        return _wl(DEPT_UNITS)

    def _fake_cand(entity):
        return "col", CAND

    def _fake_det(adm, base_date, entity, report_units):
        return {}

    def _fake_r7_inp(adm, base_date):
        return copy.deepcopy(R7_INP)

    def _fake_r7_nadm(adm, base_date):
        return copy.deepcopy(R7_NADM)

    def _fake_r7_surg(surg, base_date):
        return copy.deepcopy(R7_SURG)

    def _fake_ranking(*args, **kwargs):
        return pd.DataFrame()

    def _fake_build_parts(*args, **kwargs):
        return {"A": dict(FAKE_PART)}

    def _fake_render_svg(*args, **kwargs):
        return ""

    def _fake_none(*args, **kwargs):
        return None

    patches = [
        mock.patch.object(dr, "weekend_census_retention", _fake_wcr),
        mock.patch.object(dr, "_dow_unit_candidates", _fake_cand),
        mock.patch.object(dr, "build_dow_unit_detail", _fake_det),
        mock.patch.object(dr, "rolling7_inpatient_avg", _fake_r7_inp),
        mock.patch.object(dr, "rolling7_new_admission", _fake_r7_nadm),
        mock.patch.object(dr, "rolling7_surgery", _fake_r7_surg),
        mock.patch.object(dr, "build_dept_ranking", _fake_ranking),
        mock.patch.object(dr, "build_surgery_ranking", _fake_ranking),
        mock.patch.object(dr, "_build_parts", _fake_build_parts),
        mock.patch.object(dr, "render_trend_svg", _fake_render_svg),
        mock.patch.object(dr, "_unit_profit_series", _fake_none),
        mock.patch.object(dr, "_q_planned_mix", _fake_none),
        mock.patch.object(dr, "_q_or_load", _fake_none),
        mock.patch.object(dr, "_q_surg_dow_shape", _fake_none),
        mock.patch.object(dr, "_q_surg_urgency_mix", _fake_none),
        mock.patch.object(dr, "_q_holiday_week", _fake_none),
        mock.patch.object(dr, "narrate_leveling_actions", recorder.fake_leveling),
        mock.patch.object(dr, "narrate_admission_action", recorder.fake_admission),
        mock.patch.object(dr, "narrate_surgery_action", recorder.fake_surgery),
    ]
    with contextlib.ExitStack() as st:
        for p in patches:
            st.enter_context(p)
        return dr.build_dept_report_contexts(
            adm=adm, surg=pd.DataFrame(),
            targets=TARGETS, surg_targets=SURG_TARGETS,
            profit_monthly=pd.DataFrame(),
            base_date=base_date, generated_at=base_date,
            hospital_name="テスト病院", with_ai=True, axes=("dept",), quiet=True,
            profit_breakdown=None, delta_anchor=None, overrides=None)


class TurnMHoistParityTest(unittest.TestCase):
    """S2: turn_line の値が hoist 前後で同値（turnover_metrics 直呼びと一致）。"""

    def setUp(self):
        self.adm = _build_census_adm()
        self.recorder = _Recorder()
        self.contexts = _run_pipeline(self.adm, self.recorder)
        self.moves = {c["unit"]: c["move"] for c in self.contexts}

    def test_turn_line_matches_direct_turnover_metrics_call(self):
        for name in ("呼吸器内科", "整形外科", "循環器内科"):
            expected_m = turnover_metrics(self.adm, BASE, None,
                                          group_col="診療科名", unit=name, los_df=None)
            expected_line = format_turn_line(expected_m)
            self.assertEqual(self.moves[name]["turn_line"], expected_line,
                             f"{name} の turn_line がturnover_metrics直呼びと不一致")


class DriverWiringTest(unittest.TestCase):
    """S4: driver/drivers が narrate_admission_action / narrate_surgery_action /
    narrate_leveling_actions の kwargs に正しく届くこと。"""

    def setUp(self):
        self.adm = _build_census_adm()
        self.recorder = _Recorder()
        self.contexts = _run_pipeline(self.adm, self.recorder)

    def test_admission_receives_inlet_driver(self):
        # 呼吸器内科はtopic=admission確定（na_tgt=20・r7_nadm=5で大きく未達）
        kwargs = self.recorder.admission_kwargs["呼吸器内科"]
        self.assertIn("driver", kwargs)
        self.assertIsNotNone(kwargs["driver"])
        self.assertIn("入口", kwargs["driver"])

    def test_surgery_receives_none_driver_when_flat(self):
        # 整形外科はtopic=surgery確定・IN/OUTとも変化なしのため driver=None
        kwargs = self.recorder.surgery_kwargs["整形外科"]
        self.assertIn("driver", kwargs)
        self.assertIsNone(kwargs["driver"])

    def test_leveling_batch_drivers_dict_excludes_none_and_includes_outlet(self):
        # 循環器内科(topic=leveling・非skip)はoutlet、Noneの整形外科は除外される
        self.assertIn("循環器内科", self.recorder.leveling_drivers)
        self.assertIn("出口", self.recorder.leveling_drivers["循環器内科"])
        self.assertNotIn("整形外科", self.recorder.leveling_drivers)

    def test_admission_receives_next_week_fact(self):
        # S9: BASE(2026-07-19)の次週は海の日連休(run_len=3,is_eve=True)がかかる週
        # ＝ next_week_fact が全ユニット共通で kwargs に届く。
        expected = _q_next_week_calendar(BASE)["text"]
        kwargs = self.recorder.admission_kwargs["呼吸器内科"]
        self.assertIn("next_week", kwargs)
        self.assertEqual(kwargs["next_week"], expected)

    def test_surgery_receives_next_week_fact(self):
        expected = _q_next_week_calendar(BASE)["text"]
        kwargs = self.recorder.surgery_kwargs["整形外科"]
        self.assertEqual(kwargs["next_week"], expected)

    def test_leveling_batch_receives_next_week_and_force_disperse(self):
        nw = _q_next_week_calendar(BASE)
        self.assertEqual(self.recorder.leveling_next_week, nw["text"])
        self.assertEqual(self.recorder.leveling_force_disperse,
                         bool(nw["run_len"] >= 3 or nw["is_eve"]))
        self.assertTrue(self.recorder.leveling_force_disperse)


class OrdinaryWeekNoNextWeekWiringTest(unittest.TestCase):
    """S9続き: 通常週（次週が連休にかからない）では next_week_fact=None・
    force_disperse=False が narrate_* の kwargs に届くこと。"""

    def setUp(self):
        self.adm = _build_census_adm()
        self.recorder = _Recorder()
        self.ordinary_base = pd.Timestamp("2026-07-03")
        self.assertIsNone(_q_next_week_calendar(self.ordinary_base))
        self.contexts = _run_pipeline(self.adm, self.recorder, base_date=self.ordinary_base)

    def test_admission_and_surgery_receive_none(self):
        self.assertIsNone(self.recorder.admission_kwargs["呼吸器内科"]["next_week"])
        self.assertIsNone(self.recorder.surgery_kwargs["整形外科"]["next_week"])

    def test_leveling_batch_receives_none_and_force_disperse_false(self):
        self.assertIsNone(self.recorder.leveling_next_week)
        self.assertFalse(self.recorder.leveling_force_disperse)


class FallbackMoveForceDisperseTest(unittest.TestCase):
    """S10: dept_report._fallback_move(force_disperse=) が _leveling_levers を
    disperse に固定すること（LLMプロンプト側 _build_leveling_prompt と同じ値渡しで
    両経路の整合を取る＝定型フォールバック経路でも連休直前週は disperse になる）。"""

    def _unit(self):
        return {"retention": 0.6, "room_delta_4w": -1.0, "room_per_week": 5.0}

    def test_default_false_matches_no_kwarg(self):
        without_kwarg = dr._fallback_move(self._unit(), None, "dept")
        with_false = dr._fallback_move(self._unit(), None, "dept", force_disperse=False)
        self.assertEqual(without_kwarg, with_false)

    def test_true_forces_disperse_wording(self):
        default_move = dr._fallback_move(self._unit(), None, "dept")
        self.assertIn("あわせて", default_move["action"])   # dd=None→既定 mode="both"
        forced_move = dr._fallback_move(self._unit(), None, "dept", force_disperse=True)
        self.assertIn("（退院の平準化を主に）", forced_move["action"])
        self.assertNotIn("あわせて", forced_move["action"])


def _run_pipeline_real_prompts(adm, base_date, captured):
    """S11: narrate_leveling_actions/narrate_admission_action/narrate_surgery_action を
    フェイクに差し替えず実プロンプト生成コードを走らせ、LLM呼び出しの境界
    （ai_narrative._generate_checked）だけをフェイクに差し替えて (tag, user) を captured
    に積む。データは _build_census_adm の合成 adm のみ・常駐サーバへの実リクエストなし。"""

    def _fake_wcr(adm, base_date, entity=None, weeks=8):
        return _wl(DEPT_UNITS)

    def _fake_cand(entity):
        return "col", CAND

    def _fake_det(adm, base_date, entity, report_units):
        return {}

    def _fake_r7_inp(adm, base_date):
        return copy.deepcopy(R7_INP)

    def _fake_r7_nadm(adm, base_date):
        return copy.deepcopy(R7_NADM)

    def _fake_r7_surg(surg, base_date):
        return copy.deepcopy(R7_SURG)

    def _fake_ranking(*args, **kwargs):
        return pd.DataFrame()

    def _fake_build_parts(*args, **kwargs):
        return {"A": dict(FAKE_PART)}

    def _fake_render_svg(*args, **kwargs):
        return ""

    def _fake_none(*args, **kwargs):
        return None

    def _fake_generate_checked(tag, system, user, banned, allow=(), model=None,
                               temperature=None, quiet=False):
        captured.append((tag, user))
        return None   # oMLX未起動相当（無害縮退）。呼ばれた事実だけを見る。

    patches = [
        mock.patch.object(dr, "weekend_census_retention", _fake_wcr),
        mock.patch.object(dr, "_dow_unit_candidates", _fake_cand),
        mock.patch.object(dr, "build_dow_unit_detail", _fake_det),
        mock.patch.object(dr, "rolling7_inpatient_avg", _fake_r7_inp),
        mock.patch.object(dr, "rolling7_new_admission", _fake_r7_nadm),
        mock.patch.object(dr, "rolling7_surgery", _fake_r7_surg),
        mock.patch.object(dr, "build_dept_ranking", _fake_ranking),
        mock.patch.object(dr, "build_surgery_ranking", _fake_ranking),
        mock.patch.object(dr, "_build_parts", _fake_build_parts),
        mock.patch.object(dr, "render_trend_svg", _fake_render_svg),
        mock.patch.object(dr, "_unit_profit_series", _fake_none),
        mock.patch.object(dr, "_q_planned_mix", _fake_none),
        mock.patch.object(dr, "_q_or_load", _fake_none),
        mock.patch.object(dr, "_q_surg_dow_shape", _fake_none),
        mock.patch.object(dr, "_q_surg_urgency_mix", _fake_none),
        mock.patch.object(dr, "_q_holiday_week", _fake_none),
        mock.patch.object(an, "_generate_checked", _fake_generate_checked),
    ]
    with contextlib.ExitStack() as st:
        for p in patches:
            st.enter_context(p)
        return dr.build_dept_report_contexts(
            adm=adm, surg=pd.DataFrame(),
            targets=TARGETS, surg_targets=SURG_TARGETS,
            profit_monthly=pd.DataFrame(),
            base_date=base_date, generated_at=base_date,
            hospital_name="テスト病院", with_ai=True, axes=("dept",), quiet=True,
            profit_breakdown=None, delta_anchor=None, overrides=None)


class RealPromptIntegrationTest(unittest.TestCase):
    """S11: base_date=2026-09-18（5連休(9/19-23)の直前）で全ユニット（admission/surgery/
    leveling の3トピック）の実プロンプトに next_week_fact が載り、leveling は disperse に
    固定されること。base_date=2026-07-03（通常週）ではプロンプトに来週関連の文言が
    一切載らないこと（next_week=None のバイト不変の間接確認）。"""

    def setUp(self):
        self.adm = _build_census_adm()

    def test_holiday_eve_week_injects_next_week_and_forces_disperse(self):
        base_date = pd.Timestamp("2026-09-18")
        nw = _q_next_week_calendar(base_date)
        self.assertIsNotNone(nw)
        self.assertTrue(nw["run_len"] >= 3 or nw["is_eve"])
        captured = []
        contexts = _run_pipeline_real_prompts(self.adm, base_date, captured)
        # 3ユニットとも topic が admission/surgery/leveling に確定し、いずれかを
        # _generate_checked 経由で呼んでいること（無駄打ちで捨てられていないこと）。
        topics = {c["unit"]: c["move"]["topic"] for c in contexts}
        self.assertEqual(topics.get("呼吸器内科"), "admission")
        self.assertEqual(topics.get("整形外科"), "surgery")
        self.assertEqual(topics.get("循環器内科"), "leveling")
        self.assertGreaterEqual(len(captured), 3)
        for tag, user in captured:
            self.assertIn(nw["text"], user, f"{tag} のプロンプトに next_week_fact が無い")
        lev_user = next(user for tag, user in captured if tag.startswith("leveling"))
        self.assertIn("（退院の平準化を主に）", lev_user)

    def test_ordinary_week_omits_next_week_from_all_prompts(self):
        base_date = pd.Timestamp("2026-07-03")
        self.assertIsNone(_q_next_week_calendar(base_date))
        captured = []
        _run_pipeline_real_prompts(self.adm, base_date, captured)
        self.assertGreaterEqual(len(captured), 3)
        for tag, user in captured:
            self.assertNotIn("来週は", user, f"{tag} のプロンプトに来週関連の文言が混入")


if __name__ == "__main__":
    unittest.main()
