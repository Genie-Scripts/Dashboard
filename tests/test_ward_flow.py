"""
病棟フロー（Track W: W1緊急受入シェア/W2転入依存度・純転棟収支/W3利用率×回転率象限マップ/
W4週内変動係数・ward_flow.py）の回帰防止テスト（標準ライブラリ unittest・追加依存なし）。

対象:
    emergency_share (W1)              : 特例4病棟の除外・分母<30のexcluded
    transfer_dependency (W2a)         : 分母0週のNone化（線を切る）・例外なし
    transfer_balance (W2b)            : 符号と色（受け手=緑/送り手=赤）の対応
    utilization_turnover_quadrant(W3) : ward_beds未設定病棟の除外・y基準線=中央値
    weekday_cv (W4)                   : 平日のみ使用・μ==0/平日<20日のexcluded
    build_ward_flow_payload           : 母集団規約（特例4病棟・WARD_HIDDEN・beds欠損）横断確認、
                                         adm/targets空でNone、JSON安全性

実行: リポジトリルートで
    OMLX_BASE_URL=http://127.0.0.1:1 .venv/bin/python -m pytest tests/test_ward_flow.py -q
"""
import json
import sys
import unittest
from pathlib import Path

import pandas as pd

# リポジトリルートを import パスに追加（generate_html.py と同方式）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.preprocess import preprocess_admission  # noqa: E402
from app.lib.html_builder import _json_safe  # noqa: E402
from app.lib.ward_flow import (  # noqa: E402
    build_ward_flow_payload,
    emergency_share,
    transfer_dependency,
    transfer_balance,
    utilization_turnover_quadrant,
    weekday_cv,
    _UP,
    _DOWN,
)

BASE = pd.Timestamp("2026-06-03")  # 水曜（既存テストと同じ基準日）


def _rows_for_ward(ward, start, end, census=10, admission=1, emergency=0,
                    transfer_in=0, transfer_out=0, discharge=1,
                    weekend_overrides=None):
    """指定病棟の日次行リスト（生列のみ）。weekend_overrides で土日だけ値を差し替え可能。"""
    rows = []
    for d in pd.date_range(start, end, freq="D"):
        vals = dict(census=census, admission=admission, emergency=emergency,
                    transfer_in=transfer_in, transfer_out=transfer_out, discharge=discharge)
        if weekend_overrides is not None and d.weekday() >= 5:
            vals.update(weekend_overrides)
        rows.append({
            "日付": d, "病棟コード": ward, "診療科名": "内科",
            "在院患者数": vals["census"], "入院患者数": vals["admission"],
            "緊急入院患者数": vals["emergency"], "転入患者数": vals["transfer_in"],
            "転出患者数": vals["transfer_out"], "退院患者数": vals["discharge"],
            "死亡患者数": 0,
        })
    return rows


def _collect_strings(obj, acc):
    """payload内の文字列を再帰的に集める（「特定の名前がどこにも出ない」ことの確認用）。"""
    if isinstance(obj, dict):
        for v in obj.values():
            _collect_strings(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_strings(v, acc)
    elif isinstance(obj, str):
        acc.add(obj)


class TestPopulationRules(unittest.TestCase):
    """母集団規約: 特例4病棟・WARD_HIDDEN(03B)・ward_beds欠損の扱いがWごとに正しいこと。"""

    def test_special_wards_excluded_from_w1_w3_present_in_w2b(self):
        start = BASE - pd.Timedelta(weeks=30)
        rows = []
        for ward in ("05A", "04A", "04B", "04C", "04D"):
            rows += _rows_for_ward(ward, start, BASE, census=20, admission=2,
                                    emergency=1, transfer_in=1, transfer_out=1)
        adm = preprocess_admission(pd.DataFrame(rows))
        targets = {"inpatient": {"ward_beds": {
            "05A": 40, "04A": 30, "04B": 20, "04C": 30, "04D": 20}}}

        w1 = emergency_share(adm, BASE)
        w3 = utilization_turnover_quadrant(adm, BASE, targets)
        w2b = transfer_balance(adm, BASE)

        # W1/W3: 一般病棟(05A)だけが対象。特例4病棟は表示にも除外リストにも出ない。
        self.assertEqual(w1["chart"]["traces"][0]["y"], ["5階A病棟"])
        self.assertEqual(w1["excluded"], [])
        self.assertEqual(w3["chart"]["traces"][0]["text"], ["5階A"])
        self.assertEqual(w3["excluded"], [])

        # W2b: 特例4病棟を含む全表示病棟が対象。
        self.assertEqual(
            set(w2b["chart"]["traces"][0]["y"]),
            {"5階A病棟", "4階A病棟", "ICU", "4階C病棟", "HCU"},
        )

    def test_overnight_emergency_07b_excluded_from_w1_w3(self):
        """07B は config.EMERGENCY_WARDS 昇格（2026-08-29裁定）により W1/W3 から除外・
        W2b には残り・W4 では別掲側（淡色トレース）に載る。"""
        start = BASE - pd.Timedelta(weeks=30)
        rows = []
        for ward in ("05A", "07B"):
            rows += _rows_for_ward(ward, start, BASE, census=20, admission=2,
                                    emergency=1, transfer_in=1, transfer_out=1)
        adm = preprocess_admission(pd.DataFrame(rows))
        targets = {"inpatient": {"ward_beds": {"05A": 40, "07B": 34}}}

        w1 = emergency_share(adm, BASE)
        w3 = utilization_turnover_quadrant(adm, BASE, targets)
        w2b = transfer_balance(adm, BASE)
        w4 = weekday_cv(adm, BASE)

        self.assertEqual(w1["chart"]["traces"][0]["y"], ["5階A病棟"])
        self.assertIn("7階B病棟", w1["caption"])  # 対象外の断り書きに病棟名が載る
        self.assertEqual(w3["chart"]["traces"][0]["text"], ["5階A"])
        self.assertIn("7階B病棟", set(w2b["chart"]["traces"][0]["y"]))
        self.assertIn("7階B病棟", w4["chart"]["traces"][1]["y"])
        self.assertNotIn("7階B病棟", w4["chart"]["traces"][0]["y"])

    def test_ward_hidden_03b_dropped_from_all_w(self):
        start = BASE - pd.Timedelta(weeks=30)
        rows = (_rows_for_ward("05A", start, BASE, census=20, admission=2,
                                emergency=1, transfer_in=1, transfer_out=1)
                + _rows_for_ward("03B", start, BASE, census=20, admission=2,
                                  emergency=1, transfer_in=1, transfer_out=1))
        adm = preprocess_admission(pd.DataFrame(rows))
        targets = {"inpatient": {"ward_beds": {"05A": 40, "03B": 40}}}

        payload = build_ward_flow_payload(adm, targets, BASE)
        self.assertIsNotNone(payload)

        all_strings = set()
        _collect_strings(payload, all_strings)
        self.assertNotIn("3階B病棟", all_strings)  # WARD_NAMES["03B"]
        self.assertNotIn("3階B", all_strings)      # 短縮名
        self.assertIn("5階A病棟", all_strings)      # フィクスチャが機能している確認

    def test_ward_beds_none_excluded_only_from_w3(self):
        start = BASE - pd.Timedelta(weeks=30)
        rows = (_rows_for_ward("05A", start, BASE, census=20, admission=2,
                                emergency=1, transfer_in=1, transfer_out=1)
                + _rows_for_ward("06A", start, BASE, census=20, admission=2,
                                  emergency=1, transfer_in=1, transfer_out=1))
        adm = preprocess_admission(pd.DataFrame(rows))
        # 06A は ward_beds に存在しない（未設定病棟を模す）。
        targets = {"inpatient": {"ward_beds": {"05A": 40}}}

        w1 = emergency_share(adm, BASE)
        w2a = transfer_dependency(adm, BASE)
        w2b = transfer_balance(adm, BASE)
        w3 = utilization_turnover_quadrant(adm, BASE, targets)
        w4 = weekday_cv(adm, BASE)

        self.assertEqual(set(w1["chart"]["traces"][0]["y"]), {"5階A病棟", "6階A病棟"})
        self.assertEqual(
            {m["name"] for m in w2a["wards"]}, {"5階A病棟", "6階A病棟"})
        self.assertEqual(set(w2b["chart"]["traces"][0]["y"]), {"5階A病棟", "6階A病棟"})
        w4_names = set(w4["chart"]["traces"][0]["y"]) | set(w4["chart"]["traces"][1]["y"])
        self.assertEqual(w4_names, {"5階A病棟", "6階A病棟"})

        # W3のみ 06A(beds未設定)が除外され、05Aだけが表示される。
        self.assertEqual(w3["chart"]["traces"][0]["text"], ["5階A"])
        self.assertEqual(w3["excluded"], ["6階A病棟"])


class TestW1EmergencyShare(unittest.TestCase):
    def test_denominator_below_30_excluded(self):
        start = BASE - pd.Timedelta(weeks=30)
        rows = (_rows_for_ward("05A", start, BASE, admission=0, emergency=0)  # 分母0<30
                + _rows_for_ward("06A", start, BASE, admission=2, emergency=1))
        adm = preprocess_admission(pd.DataFrame(rows))

        out = emergency_share(adm, BASE)
        self.assertIn("5階A病棟", out["excluded"])
        self.assertNotIn("5階A病棟", out["chart"]["traces"][0]["y"])
        self.assertIn("6階A病棟", out["chart"]["traces"][0]["y"])


class TestW2aTransferDependency(unittest.TestCase):
    def test_zero_denominator_week_becomes_none_no_exception(self):
        start = BASE - pd.Timedelta(weeks=30)
        monday = BASE - pd.Timedelta(days=BASE.weekday())
        zero_week_start = monday - pd.Timedelta(weeks=1)  # 直近完全週（week_starts[-1]と一致）
        zero_week_end = zero_week_start + pd.Timedelta(days=6)

        rows = []
        for d in pd.date_range(start, BASE, freq="D"):
            zeroed = zero_week_start <= d <= zero_week_end
            rows.append({
                "日付": d, "病棟コード": "04B", "診療科名": "内科",
                "在院患者数": 15, "入院患者数": 0 if zeroed else 1,
                "緊急入院患者数": 0, "転入患者数": 0 if zeroed else 1,
                "転出患者数": 1, "退院患者数": 1, "死亡患者数": 0,
            })
        adm = preprocess_admission(pd.DataFrame(rows))

        out = transfer_dependency(adm, BASE)  # 例外なく完了すること自体も確認対象
        icu_trace = next(t for t in out["chart"]["traces"] if "ICU" in t["name"])
        zero_idx = icu_trace["x"].index(zero_week_start.strftime("%Y-%m-%d"))
        self.assertIsNone(icu_trace["y"][zero_idx])
        # 全週分母0でない他の週は値が入っている（線が全部消えたわけではない）
        self.assertTrue(any(v is not None for v in icu_trace["y"]))


class TestW2bTransferBalance(unittest.TestCase):
    def test_sign_and_color_correspondence(self):
        start = BASE - pd.Timedelta(weeks=20)
        rows = (_rows_for_ward("05A", start, BASE, transfer_in=3, transfer_out=1)  # 受け手(正)
                + _rows_for_ward("06A", start, BASE, transfer_in=1, transfer_out=3))  # 送り手(負)
        adm = preprocess_admission(pd.DataFrame(rows))

        out = transfer_balance(adm, BASE)
        trace = out["chart"]["traces"][0]
        by_name = dict(zip(trace["y"], zip(trace["x"], trace["marker"]["color"])))

        recv_val, recv_color = by_name["5階A病棟"]
        send_val, send_color = by_name["6階A病棟"]
        self.assertGreater(recv_val, 0)
        self.assertLess(send_val, 0)
        self.assertEqual(recv_color, _UP)
        self.assertEqual(send_color, _DOWN)


class TestW3Quadrant(unittest.TestCase):
    def test_y_reference_line_is_median_of_shown_wards(self):
        start = BASE - pd.Timedelta(weeks=20)
        rows = (_rows_for_ward("05A", start, BASE, census=5, admission=1)
                + _rows_for_ward("06A", start, BASE, census=5, admission=2)
                + _rows_for_ward("07A", start, BASE, census=5, admission=3))
        adm = preprocess_admission(pd.DataFrame(rows))
        targets = {"inpatient": {"ward_beds": {"05A": 10, "06A": 10, "07A": 10}}}

        out = utilization_turnover_quadrant(adm, BASE, targets)
        ys = out["chart"]["traces"][0]["y"]
        expected_median = round(float(pd.Series(ys).median()), 2)
        self.assertEqual(out["thresholds"]["turnover_median"], expected_median)
        self.assertAlmostEqual(out["thresholds"]["turnover_median"], 1.4, places=6)
        self.assertEqual(out["thresholds"]["utilization"], 85.0)


class TestW4WeekdayCv(unittest.TestCase):
    def test_mu_zero_no_zerodivision_excluded(self):
        start = BASE - pd.Timedelta(weeks=8)
        rows = _rows_for_ward("05A", start, BASE, census=0)
        adm = preprocess_admission(pd.DataFrame(rows))

        out = weekday_cv(adm, BASE)  # ZeroDivisionErrorが出ないこと自体も確認対象
        self.assertIn("5階A病棟", out["excluded"])
        all_names = set(out["chart"]["traces"][0]["y"]) | set(out["chart"]["traces"][1]["y"])
        self.assertNotIn("5階A病棟", all_names)

    def test_weekend_only_uses_weekday_data(self):
        start = BASE - pd.Timedelta(weeks=8)
        rows = _rows_for_ward("05A", start, BASE, census=50,
                               weekend_overrides={"census": 999})
        adm = preprocess_admission(pd.DataFrame(rows))

        out = weekday_cv(adm, BASE)
        general = out["chart"]["traces"][0]
        idx = general["y"].index("5階A病棟")
        self.assertEqual(general["x"][idx], 0.0)  # 平日は一定値50 → CV=0（週末の極端値は無視）

    def test_weekday_count_below_20_excluded(self):
        short_start = BASE - pd.Timedelta(weeks=3)  # 平日約15日 < 20日
        rows = _rows_for_ward("05A", short_start, BASE, census=10)
        adm = preprocess_admission(pd.DataFrame(rows))

        out = weekday_cv(adm, BASE)
        self.assertIn("5階A病棟", out["excluded"])
        all_names = set(out["chart"]["traces"][0]["y"]) | set(out["chart"]["traces"][1]["y"])
        self.assertNotIn("5階A病棟", all_names)


class TestPayloadSafety(unittest.TestCase):
    def test_empty_adm_or_targets_returns_none(self):
        self.assertIsNone(build_ward_flow_payload(pd.DataFrame(), {"inpatient": {}}, BASE))
        self.assertIsNone(build_ward_flow_payload(None, {"inpatient": {}}, BASE))

        rows = _rows_for_ward("05A", BASE - pd.Timedelta(weeks=30), BASE)
        adm = preprocess_admission(pd.DataFrame(rows))
        self.assertIsNone(build_ward_flow_payload(adm, {}, BASE))
        self.assertIsNone(build_ward_flow_payload(adm, None, BASE))

    def test_json_dumps_with_json_safe_no_exception_no_numpy_leak(self):
        start = BASE - pd.Timedelta(weeks=30)
        rows = []
        for ward in ("05A", "06A", "04A", "04B", "04C", "04D", "03B"):
            rows += _rows_for_ward(ward, start, BASE, census=20, admission=2,
                                    emergency=1, transfer_in=1, transfer_out=1)
        adm = preprocess_admission(pd.DataFrame(rows))
        targets = {"inpatient": {"ward_beds": {"05A": 40, "06A": 35}}}

        payload = build_ward_flow_payload(adm, targets, BASE)
        self.assertIsNotNone(payload)

        wrapper = {"charts": {"ward_flow": payload}}
        dumped = json.dumps(wrapper, ensure_ascii=False, default=_json_safe)
        self.assertIsInstance(dumped, str)
        self.assertNotIn("numpy", dumped)

        reloaded = json.loads(dumped)
        self.assertEqual(
            set(reloaded["charts"]["ward_flow"].keys()),
            {"w1", "w2a", "w2b", "w3", "w4", "meta"},
        )


if __name__ == "__main__":
    unittest.main()
