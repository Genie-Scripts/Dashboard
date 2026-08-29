"""
Track K（profit_translate.py: K1あと何件換算 / K2前年差ウォーターフォール / K3トルネード）
の回帰防止テスト（標準ライブラリ unittest・追加依存なし）。

対象:
    _k1_item / _k1_dept_row  : 信頼度ガードレール（G1 r2 / G2 n / G3 係数符号 / G4 上限キャップ）
    build_k2                 : 前年差ウォーターフォール（恒等式・adm期間外ガード）
    build_k3                 : トルネード（上位8+下位8+その他 集約後も恒等式維持）
    build_translate_payload  : エントリポイント（None入力・JSON安全性）

実行: リポジトリルートで
    python -m pytest tests/test_profit_translate.py -q
    python -m unittest discover -s tests -v
"""
import json
import sys
import unittest
from pathlib import Path

import pandas as pd

# リポジトリルートを import パスに追加（generate_html.py と同方式）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.profit_translate import (  # noqa: E402
    _k1_item,
    _k1_dept_row,
    build_k2,
    build_k3,
    build_translate_payload,
    REASON_LOW_FIT,
    REASON_NEG_COEF,
    REASON_OVER_CAP,
    REASON_ACHIEVED,
)
from app.lib.config import biz_days_in_month  # noqa: E402
from app.lib.html_builder import _json_safe  # noqa: E402


# ════════════════════════════════════════
# 合成データヘルパー
# ════════════════════════════════════════

def _surg_rows(dept, month, n_in, n_out):
    rows = []
    for _ in range(n_in):
        rows.append({"手術実施日": pd.Timestamp(month), "実施診療科": dept, "入外区分": "入院"})
    for _ in range(n_out):
        rows.append({"手術実施日": pd.Timestamp(month), "実施診療科": dept, "入外区分": "外来"})
    return rows


def _gen_dept_rows(dept, months_with_i, alpha, beta, d, e, f):
    """粗利 = alpha*営業日数+beta*外来手術件数（外来式）/ d*入院手術件数+e*新入院+f*純在院延べ
    （入院式）に厳密一致する合成データ（残差ゼロ=r2=1.0）を生成する。
    """
    pb_rows, adm_rows, surg_rows = [], [], []
    for i, m in months_with_i:
        m = pd.Timestamp(m)
        bizdays = biz_days_in_month(m)
        outop = 5 + i
        inop = 4 + 2 * i
        newadm = 8 + 3 * i
        purebed = 40 + 5 * i
        bed_total = purebed + newadm
        gairai_profit = alpha * bizdays + beta * outop
        nyuin_profit = d * inop + e * newadm + f * purebed
        pb_rows.append({"診療科名": dept, "月": m, "区分": "外来", "粗利": gairai_profit})
        pb_rows.append({"診療科名": dept, "月": m, "区分": "入院", "粗利": nyuin_profit})
        adm_rows.append({"日付": m, "診療科名": dept, "新入院患者数": newadm, "在院患者数": bed_total})
        surg_rows.extend(_surg_rows(dept, m, inop, outop))
    return pb_rows, adm_rows, surg_rows


# ════════════════════════════════════════
# ①②④⑤⑥: _k1_item の信頼度ガードレール（G1/G2/G3/G4）
# ════════════════════════════════════════

class TestK1ItemGuard(unittest.TestCase):

    def test_zero_coefficient_no_crash_hidden_with_reason(self):
        """① 係数0 → ZeroDivisionError無しで shown:False + reason。"""
        out = _k1_item("k", "l", "件", 0.0, 0.9, 20, 10.0, 1e6, 5, True)
        self.assertFalse(out["shown"])
        self.assertIsNone(out["value"])
        self.assertEqual(out["reason"], REASON_NEG_COEF)

    def test_none_coefficient_no_crash_hidden_with_reason(self):
        """② 係数None/欠損 → 同様に shown:False + reason（例外なし）。"""
        out = _k1_item("k", "l", "件", None, 0.9, 20, 10.0, 1e6, 5, True)
        self.assertFalse(out["shown"])
        self.assertIsNone(out["value"])
        self.assertEqual(out["reason"], REASON_NEG_COEF)

    def test_r2_below_070_hidden_at_070_shown(self):
        """④ r2=0.69 は非表示、r2=0.70 は表示（境界は >=）。"""
        low = _k1_item("k", "l", "件", 100.0, 0.69, 20, 1.0, 1e6, 5, True)
        ok  = _k1_item("k", "l", "件", 100.0, 0.70, 20, 1.0, 1e6, 5, True)
        self.assertFalse(low["shown"])
        self.assertEqual(low["reason"], REASON_LOW_FIT)
        self.assertTrue(ok["shown"])

    def test_n_below_10_hidden_at_10_shown(self):
        """⑤ n=9 は非表示、n=10 は表示（境界は >=）。"""
        low = _k1_item("k", "l", "件", 100.0, 0.9, 9, 1.0, 1e6, 5, True)
        ok  = _k1_item("k", "l", "件", 100.0, 0.9, 10, 1.0, 1e6, 5, True)
        self.assertFalse(low["shown"])
        self.assertEqual(low["reason"], REASON_LOW_FIT)
        self.assertTrue(ok["shown"])

    def test_g4_cap_exceeded_degrades(self):
        """⑥ 換算結果が直近12ヶ月ドライバー月平均×1.0を超えると縮退。"""
        # value = 100(百万円)*1000/1(千円/件) = 100000件 >> avg 50件 → G4失敗
        out = _k1_item("k", "l", "件", 1.0, 0.9, 20, 100.0, 50.0, 5, True)
        self.assertFalse(out["shown"])
        self.assertIsNone(out["value"])
        self.assertEqual(out["reason"], REASON_OVER_CAP)

    def test_g4_cap_within_limit_shows(self):
        """G4を超えなければ表示される（境界外のnegativeケース対照）。"""
        # value = 1(百万円)*1000/1000(千円/件) = 1件 <= avg 50件 → 通過
        out = _k1_item("k", "l", "件", 1000.0, 0.9, 20, 1.0, 50.0, 5, True)
        self.assertTrue(out["shown"])
        self.assertEqual(out["value"], 1.0)


# ════════════════════════════════════════
# ③⑦⑧⑨: _k1_dept_row （項目単位の非表示・ペース併記・達成済み文言）
# ════════════════════════════════════════

class TestK1DeptRow(unittest.TestCase):

    def test_negative_coefficient_hides_only_that_item(self):
        """③ 負の係数はその項目だけ非表示。式が共有するr2/nは他の項目に影響しない。"""
        est = {
            "gairai": {"alpha": 100.0, "beta": 200.0, "r2": 0.9, "n": 20},
            "nyuin":  {"d": -50.0, "e": 30.0, "f": 5.0, "r2": 0.9, "n": 20},
        }
        driver_avgs = {"Dept": {"入院手術件数": 1e6, "外来手術件数": 1e6,
                                 "新入院": 1e6, "純在院延べ": 1e6}}
        row = _k1_dept_row("Dept", 10.0, est, driver_avgs, rem_biz=5)
        by_key = {it["key"]: it for it in row["items"]}
        self.assertFalse(by_key["nyuin_op"]["shown"])
        self.assertEqual(by_key["nyuin_op"]["reason"], REASON_NEG_COEF)
        self.assertTrue(by_key["new_adm"]["shown"])
        self.assertTrue(by_key["bed_days"]["shown"])
        self.assertTrue(by_key["gairai_op"]["shown"])
        self.assertTrue(row["shown"])

    def test_pace_only_for_surgery_items(self):
        """⑦⑧ ペースは入院手術/外来手術のみ併記。新入院/在院はpace:None
        （在院はunit=人日でpace_unitも常にNone）。残営業日0ならペース自体もNone。"""
        est = {
            "gairai": {"alpha": 10.0, "beta": 20.0, "r2": 0.9, "n": 20},
            "nyuin":  {"d": 30.0, "e": 5.0, "f": 2.0, "r2": 0.9, "n": 20},
        }
        driver_avgs = {"Dept": {"入院手術件数": 1e6, "外来手術件数": 1e6,
                                 "新入院": 1e6, "純在院延べ": 1e6}}
        row = _k1_dept_row("Dept", 5.0, est, driver_avgs, rem_biz=10)
        by_key = {it["key"]: it for it in row["items"]}
        self.assertIsNotNone(by_key["nyuin_op"]["pace"])
        self.assertIsNotNone(by_key["gairai_op"]["pace"])
        self.assertIsNotNone(by_key["nyuin_op"]["pace_unit"])
        self.assertIsNone(by_key["new_adm"]["pace"])
        self.assertIsNone(by_key["new_adm"]["pace_unit"])
        self.assertIsNone(by_key["bed_days"]["pace"])
        self.assertIsNone(by_key["bed_days"]["pace_unit"])
        self.assertEqual(by_key["bed_days"]["unit"], "人日")

        # 残営業日0 → 入院手術/外来手術のペースもNoneに縮退
        row0 = _k1_dept_row("Dept", 5.0, est, driver_avgs, rem_biz=0)
        by_key0 = {it["key"]: it for it in row0["items"]}
        self.assertIsNone(by_key0["nyuin_op"]["pace"])
        self.assertIsNone(by_key0["gairai_op"]["pace"])

    def test_gap_non_positive_marks_achieved(self):
        """⑨ gap<=0（達成済み）は換算せず専用文言、items=[]。"""
        row = _k1_dept_row("Dept", 0.0, None, {}, rem_biz=5)
        self.assertFalse(row["shown"])
        self.assertEqual(row["reason"], REASON_ACHIEVED)
        self.assertEqual(row["items"], [])

        row2 = _k1_dept_row("Dept", -3.0, None, {}, rem_biz=5)
        self.assertFalse(row2["shown"])
        self.assertEqual(row2["reason"], REASON_ACHIEVED)


# ════════════════════════════════════════
# ⑩⑪: build_k2（前年差ウォーターフォール）
# ════════════════════════════════════════

def _k2_value_map(k2):
    """3トレース（増加/減少/実績・その他）を label -> value の単一dictに統合する。"""
    out = {}
    for tr in k2["chart"]["traces"]:
        for x, y in zip(tr["x"], tr["y"]):
            out[x] = y
    return out


class TestBuildK2(unittest.TestCase):

    def setUp(self):
        self.curr = pd.Timestamp("2025-06-01")
        self.prev = self.curr - pd.DateOffset(months=12)
        self.estimators = {
            "外科A": {
                "gairai": {"alpha": 50.0, "beta": 80.0, "r2": 0.95, "n": 12},
                "nyuin":  {"d": 40.0, "e": 30.0, "f": 5.0, "r2": 0.92, "n": 12},
            },
        }

    def test_identity_components_plus_residual_equals_actual_delta(self):
        """⑩ 恒等式: 成分和(暦+外来手術+入院手術+新入院+在院)+残差 = 実測Δ粗利（1e-6）。"""
        pb = pd.DataFrame([
            {"診療科名": "外科A", "月": self.prev, "区分": "外来", "粗利": 1000.0},
            {"診療科名": "外科A", "月": self.prev, "区分": "入院", "粗利": 2000.0},
            {"診療科名": "外科A", "月": self.curr, "区分": "外来", "粗利": 1500.0},
            {"診療科名": "外科A", "月": self.curr, "区分": "入院", "粗利": 2600.0},
        ])
        adm = pd.DataFrame([
            {"日付": self.prev, "診療科名": "外科A", "新入院患者数": 10, "在院患者数": 110},
            {"日付": self.curr, "診療科名": "外科A", "新入院患者数": 15, "在院患者数": 165},
        ])
        surg = pd.DataFrame(_surg_rows("外科A", self.prev, 20, 8)
                             + _surg_rows("外科A", self.curr, 25, 10))

        result = build_k2(pb, adm, surg, self.estimators)
        self.assertIsNotNone(result)

        curr_total = float(pb[pb["月"] == self.curr]["粗利"].sum())
        prev_total = float(pb[pb["月"] == self.prev]["粗利"].sum())
        expected_delta = round((curr_total - prev_total) / 1000.0, 1)

        vmap = _k2_value_map(result)
        mid_sum = (vmap["暦"] + vmap["外来手術"] + vmap["入院手術"]
                   + vmap["新入院"] + vmap["在院"] + vmap["その他"])
        self.assertAlmostEqual(mid_sum, expected_delta, delta=1e-6)
        self.assertIn("months", result)
        self.assertEqual(result["months"]["current"], "2025-06")
        self.assertEqual(result["months"]["prev"], "2024-06")

    def test_prev_year_month_outside_adm_range_returns_none(self):
        """⑪ 前年同月がadmデータ期間外なら k2=None。"""
        pb = pd.DataFrame([
            {"診療科名": "外科A", "月": self.prev, "区分": "外来", "粗利": 1000.0},
            {"診療科名": "外科A", "月": self.prev, "区分": "入院", "粗利": 2000.0},
            {"診療科名": "外科A", "月": self.curr, "区分": "外来", "粗利": 1500.0},
            {"診療科名": "外科A", "月": self.curr, "区分": "入院", "粗利": 2600.0},
        ])
        # adm最古日が prev より後 → ガードで None
        adm_late = pd.DataFrame([
            {"日付": self.curr - pd.DateOffset(months=3), "診療科名": "外科A",
             "新入院患者数": 10, "在院患者数": 100},
            {"日付": self.curr, "診療科名": "外科A", "新入院患者数": 15, "在院患者数": 150},
        ])
        surg = pd.DataFrame(_surg_rows("外科A", self.curr, 10, 5))

        result = build_k2(pb, adm_late, surg, self.estimators)
        self.assertIsNone(result)


# ════════════════════════════════════════
# ⑫: build_k3（トルネード・上位8+下位8+その他 集約）
# ════════════════════════════════════════

class TestBuildK3(unittest.TestCase):

    def test_other_bucket_preserves_total_identity(self):
        """⑫ 恒等式: その他集約後も 寄与総和 = Σ(actual-adj_target)[有効科] + Σactual[target無し科]。"""
        ranking = []
        for i in range(20):
            actual = 10.0 + i
            adj_target = 8.0 + i * 0.5
            ranking.append({"name": f"科{i:02d}", "actual": round(actual, 1),
                             "target": round(adj_target, 1), "adj_target": round(adj_target, 1)})
        # target無し科（その他へ actual がそのまま合算される）
        ranking.append({"name": "科None1", "actual": 5.0, "target": None, "adj_target": None})
        ranking.append({"name": "科None2", "actual": -3.0, "target": None, "adj_target": None})

        profit_section = {"kpi": {"base_month": "2025-06"}, "ranking": ranking}
        result = build_k3(profit_section)
        self.assertIsNotNone(result)

        expected = 0.0
        for r in ranking:
            if r["adj_target"] is None:
                expected += r["actual"]
            else:
                expected += round(r["actual"] - r["adj_target"], 1)

        self.assertTrue(
            result["chart"]["layout"]["yaxis"].get("automargin"),
            "科名（y軸ラベル）が既定余白(l=50px)で見切れる（実機フィードバック 2026-08-29）")

        trace = result["chart"]["traces"][0]
        # 横棒（orientation:"h"）: x=値, y=科名
        actual_sum = sum(trace["x"])
        self.assertAlmostEqual(actual_sum, expected, delta=1e-6)
        # 22科 > 16 → 上位8+下位8+その他1本 = 17本のはず
        self.assertEqual(len(trace["x"]), 17)
        self.assertIn("2025年6月", result["caption"])

    def test_empty_ranking_returns_none(self):
        result = build_k3({"kpi": {"base_month": "2025-06"}, "ranking": []})
        self.assertIsNone(result)


# ════════════════════════════════════════
# ⑬⑭: build_translate_payload（エントリポイント）
# ════════════════════════════════════════

class TestBuildTranslatePayload(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        months_with_i = [(-1, pd.Timestamp("2024-12-01"))] + [
            (i, pd.Timestamp("2025-01-01") + pd.DateOffset(months=i)) for i in range(12)
        ]
        pb_rows, adm_rows, surg_rows = _gen_dept_rows(
            "外科A", months_with_i, alpha=10.0, beta=1000.0, d=1000.0, e=500.0, f=100.0)
        cls.profit_breakdown = pd.DataFrame(pb_rows)
        cls.adm = pd.DataFrame(adm_rows)
        cls.surg = pd.DataFrame(surg_rows)
        cls.profit_section = {
            "kpi": {"base_month": "2025-12"},
            "ranking": [
                {"name": "外科A", "actual": 35.0, "target": 40.0, "adj_target": 40.0},
                {"name": "内科B", "actual": 15.0, "target": 15.0, "adj_target": 15.0},
            ],
        }
        cls.base_date = pd.Timestamp("2025-12-20")

    def test_none_inputs_return_none(self):
        """⑭ profit_section / profit_breakdown が None なら None。"""
        self.assertIsNone(build_translate_payload(
            None, self.adm, self.surg, self.profit_section, self.base_date))
        self.assertIsNone(build_translate_payload(
            self.profit_breakdown, self.adm, self.surg, None, self.base_date))

    def test_payload_json_dumps_ok_no_numpy_leak(self):
        """⑬ json.dumps（defaultなし）が例外なく通る=numpy型が残っていない。
        default=_json_safe（本番の呼び出し方）でも通ることも合わせて確認。"""
        result = build_translate_payload(
            self.profit_breakdown, self.adm, self.surg, self.profit_section, self.base_date)
        self.assertIsNotNone(result)
        self.assertEqual(set(result.keys()), {"k1", "k2", "k3", "meta"})

        dumped = json.dumps(result, ensure_ascii=False)
        self.assertIsInstance(dumped, str)
        reloaded = json.loads(dumped)
        self.assertEqual(reloaded["meta"]["depts_total"], 2)

        dumped2 = json.dumps(result, ensure_ascii=False, default=_json_safe)
        self.assertIsInstance(dumped2, str)

    def test_happy_path_k1_items_shown_for_well_fit_dept(self):
        """ボーナス: 厳密線形合成データはr2=1.0/n=12でG1-G4を通過し、
        入院手術・外来手術の項目が実際に表示される。"""
        result = build_translate_payload(
            self.profit_breakdown, self.adm, self.surg, self.profit_section, self.base_date)
        dept_row = next(r for r in result["k1"]["depts"] if r["name"] == "外科A")
        self.assertTrue(dept_row["shown"])
        by_key = {it["key"]: it for it in dept_row["items"]}
        self.assertTrue(by_key["nyuin_op"]["shown"])
        self.assertTrue(by_key["gairai_op"]["shown"])


class TestK1AllAchieved(unittest.TestCase):
    """全科が目標達成（gap<=0）のとき、縮退文言が「ばらつき」ではなく達成文になる。"""

    def test_all_achieved_hospital_reason_and_caption(self):
        from app.lib.profit_translate import _build_k1
        profit_section = {"ranking": [
            {"name": "整形外科", "actual": 10.0, "target": 8.0, "adj_target": 8.0},
            {"name": "皮膚科", "actual": 5.0, "target": 4.0, "adj_target": 4.0},
        ]}
        k1, shown, total = _build_k1(profit_section, {}, {}, 5)
        self.assertEqual(shown, 0)
        self.assertFalse(k1["hospital"]["shown"])
        self.assertEqual(k1["hospital"]["reason"], REASON_ACHIEVED)
        self.assertIn("上回っています", k1["caption"])
        self.assertNotIn("ばらつき", k1["caption"])


if __name__ == "__main__":
    unittest.main()
