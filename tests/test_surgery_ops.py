"""
手術オペレーション（Track S: S1〜S7・surgery_ops.py）の回帰防止テスト
（標準ライブラリ unittest・追加依存なし）。

対象:
    _min_of_day / _duration_min      : 時刻パース・日跨ぎ+1440補正
    overtime_ratio (S1)              : 時間外判定（実効退室分 > 17:15）
    turnover_minutes (S2)            : 入替ギャップ 0〜180分のみ採用
    capacity_share (S3)              : 分母=510×11×営業日数（祝日で分母が減る）
    urgent_hour_dow (S4)             : 全室対象（稼働対象室以外も含む）
    planned_actual_ratio (S6)        : 予定手術時間 NaN/0 除外、n<30科の除外
    build_surgery_ops_payload        : 空DataFrame/全行NaN時刻でも例外なくn:0、JSON安全

実行: リポジトリルートで
    python -m pytest tests/test_surgery_ops.py -q
    python -m unittest discover -s tests -v
"""
import json
import sys
import unittest
from pathlib import Path

import pandas as pd

# リポジトリルートを import パスに追加（generate_html.py と同方式）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.surgery_ops import (  # noqa: E402
    _min_of_day,
    _duration_min,
    overtime_ratio,
    turnover_minutes,
    capacity_share,
    urgent_hour_dow,
    or_timeline,
    planned_actual_ratio,
    interrupt_mix,
    build_surgery_ops_payload,
)

BASE = pd.Timestamp("2026-06-03")  # 水曜（既存テストと同じ基準日）

_COLUMNS = [
    "手術実施日", "実施診療科", "手術室", "稼働対象室", "平日",
    "入室時刻", "退室時刻", "予定手術時間", "申込区分", "入外区分",
]


def _row(date, dept="整形外科", room="OP-1", core=True, weekday=True,
         enter="08:00", leave="09:00", planned=60.0, kind="通常", io="入院"):
    return {
        "手術実施日": pd.Timestamp(date), "実施診療科": dept, "手術室": room,
        "稼働対象室": core, "平日": weekday,
        "入室時刻": enter, "退室時刻": leave,
        "予定手術時間": planned, "申込区分": kind, "入外区分": io,
    }


def _empty_surg() -> pd.DataFrame:
    """列は揃っているが0行のDataFrame（本番のpreprocess_surgery出力を模す）。"""
    return pd.DataFrame({
        "手術実施日": pd.Series([], dtype="datetime64[ns]"),
        "実施診療科": pd.Series([], dtype=object),
        "手術室": pd.Series([], dtype=object),
        "稼働対象室": pd.Series([], dtype=bool),
        "平日": pd.Series([], dtype=bool),
        "入室時刻": pd.Series([], dtype=object),
        "退室時刻": pd.Series([], dtype=object),
        "予定手術時間": pd.Series([], dtype=float),
        "申込区分": pd.Series([], dtype=object),
        "入外区分": pd.Series([], dtype=object),
    })


def _rich_surg(base_date=BASE, n_weeks=110):
    """S1〜S7の全経路（複数月・複数科・複数室・日跨ぎ・urgent種別・入外区分）を
    一通り通す密度のある合成データ（JSON安全性テスト等に使う）。"""
    depts = ["整形外科", "泌尿器科", "産婦人科"]
    rooms = ["OP-1", "OP-2", "OP-3"]
    kinds = ["通常", "通常", "通常", "臨時", "緊急"]
    ios = ["入院", "外来"]
    rows = []
    start = (base_date - pd.Timedelta(weeks=n_weeks)).normalize()
    d = start
    i = 0
    while d <= base_date:
        if d.weekday() < 5:
            for dept, room in zip(depts, rooms):
                enter_h = 8 + (i % 8)
                leave_h = enter_h + 1 + (i % 2)
                rows.append(_row(
                    d, dept=dept, room=room,
                    enter=f"{enter_h:02d}:00", leave=f"{leave_h % 24:02d}:00",
                    planned=45.0 + (i % 5) * 10,
                    kind=kinds[i % len(kinds)], io=ios[i % len(ios)],
                ))
                i += 1
            # 日跨ぎ・稼働対象外室のケースも少量混ぜる
            rows.append(_row(d, dept="耳鼻咽喉科", room="OP-4", enter="22:00", leave="01:00"))
            rows.append(_row(d, dept="脳神経外科", room="OP-11A", core=False,
                              enter="10:00", leave="11:00", kind="緊急"))
        d += pd.Timedelta(days=1)
    return pd.DataFrame(rows)


class TestHelpers(unittest.TestCase):
    """_min_of_day / _duration_min の単体挙動。"""

    def test_min_of_day_parses_and_handles_unparseable(self):
        s = pd.Series(["08:45", "17:05", None, "invalid", float("nan")])
        out = _min_of_day(s)
        self.assertEqual(out.iloc[0], 525.0)
        self.assertEqual(out.iloc[1], 1025.0)
        self.assertTrue(pd.isna(out.iloc[2]))
        self.assertTrue(pd.isna(out.iloc[3]))
        self.assertTrue(pd.isna(out.iloc[4]))

    def test_duration_min_day_crossing_adds_1440(self):
        df = pd.DataFrame({"入室時刻": ["22:00", "13:00"], "退室時刻": ["01:00", "14:30"]})
        out = _duration_min(df)
        self.assertEqual(out.iloc[0], 180.0)  # 22:00→翌01:00 = 180分
        self.assertEqual(out.iloc[1], 90.0)   # 通常（日跨ぎなし）


class TestOvertimeRatio(unittest.TestCase):
    """S1: ①日跨ぎ+1440補正が時間外に算入されること／②17:15境界。"""

    def test_overnight_crossing_counts_as_overtime(self):
        rows = [
            _row(BASE, enter="22:00", leave="01:00"),  # 日跨ぎ: 実効退室25:00(1500)>17:15 → 時間外
            _row(BASE, enter="13:00", leave="14:00"),  # 通常: 実効退室14:00 → 時間外でない
        ]
        surg = pd.DataFrame(rows)
        out = overtime_ratio(surg, BASE)
        s1 = out["s1"]
        self.assertEqual(s1["n"], 2)
        self.assertEqual(s1["chart"]["traces"][0]["y"], [50.0])
        self.assertIn("s1b", out)

    def test_leave_exactly_1715_excluded_1716_included(self):
        rows = [
            _row(BASE, enter="16:00", leave="17:15"),  # ちょうど17:15 → 時間外に入らない
            _row(BASE, enter="16:00", leave="17:16"),  # 17:16 → 時間外に入る
        ]
        surg = pd.DataFrame(rows)
        out = overtime_ratio(surg, BASE)
        self.assertEqual(out["s1"]["chart"]["traces"][0]["y"], [50.0])


class TestTurnoverMinutes(unittest.TestCase):
    """S2: ③入替ギャップ 負値/181分は除外、0分は採用。"""

    def test_gap_boundaries(self):
        d = BASE
        rows = [
            # OP-1: 前退室10:00 → 次入室09:50（負値ギャップ=-10分）→ 除外
            _row(d, room="OP-1", enter="08:00", leave="10:00"),
            _row(d, room="OP-1", enter="09:50", leave="11:00"),
            # OP-2: ギャップ=181分 → 除外
            _row(d, room="OP-2", enter="08:00", leave="09:00"),
            _row(d, room="OP-2", enter="12:01", leave="13:00"),
            # OP-3: ギャップ=0分 → 採用
            _row(d, room="OP-3", enter="08:00", leave="09:00"),
            _row(d, room="OP-3", enter="09:00", leave="10:00"),
        ]
        surg = pd.DataFrame(rows)
        out = turnover_minutes(surg, BASE)
        by_room = {r["room"]: r for r in out["rooms"]}
        self.assertEqual(by_room["OP-1"]["n"], 0)
        self.assertEqual(by_room["OP-2"]["n"], 0)
        self.assertEqual(by_room["OP-3"]["n"], 1)
        self.assertEqual(by_room["OP-3"]["median"], 0.0)
        self.assertEqual(out["n"], 1)


class TestPlannedActualRatio(unittest.TestCase):
    """S6: ④予定手術時間NaN/0は母数から除外／⑤n<30の科はdeptsに出ずexcludedへ。"""

    def test_nan_and_zero_planned_excluded_from_denominator(self):
        rows = [
            _row(BASE, planned=60.0, enter="08:00", leave="09:00"),   # 有効: dur=60,ratio=1.0
            _row(BASE, planned=float("nan"), enter="08:00", leave="09:00"),  # NaN除外
            _row(BASE, planned=0.0, enter="08:00", leave="09:00"),    # 0除外
        ]
        surg = pd.DataFrame(rows)
        out = planned_actual_ratio(surg, BASE)
        self.assertEqual(out["n"], 1)

    def test_dept_below_min_n_excluded(self):
        rows = []
        for i in range(30):
            rows.append(_row(BASE - pd.Timedelta(days=(i % 5)), dept="整形外科"))
        for i in range(29):
            rows.append(_row(BASE - pd.Timedelta(days=(i % 5)), dept="泌尿器科"))
        surg = pd.DataFrame(rows)
        out = planned_actual_ratio(surg, BASE)
        depts = {d["dept"] for d in out["depts"]}
        self.assertIn("整形外科", depts)
        self.assertNotIn("泌尿器科", depts)
        self.assertIn("泌尿器科", out["excluded"])


class TestCapacityShare(unittest.TestCase):
    """S3: ⑥分母=510×11×営業日数（operational_days_betweenと同じ計算）。祝日を含む窓では分母が減る。"""

    def test_denominator_matches_operational_days_between(self):
        from app.lib.config import operational_days_between
        surg = _empty_surg()
        for base in (pd.Timestamp("2025-11-05"), pd.Timestamp("2026-01-21")):
            out = capacity_share(surg, base)
            win_start = base - pd.Timedelta(weeks=12) + pd.Timedelta(days=1)
            expected_biz_days = operational_days_between(win_start, base)
            self.assertEqual(out["biz_days"], expected_biz_days)
            self.assertEqual(out["denom_minutes"], 510 * 11 * expected_biz_days)

    def test_denominator_shrinks_when_window_includes_new_year_holidays(self):
        surg = _empty_surg()
        # 2026-01-21基準の直近12週窓（年末年始12/29-1/3等を含む）は、
        # 2025-11-05基準の12週窓より営業日数が少ない（実測: 52 < 56）。
        fewer = capacity_share(surg, pd.Timestamp("2026-01-21"))
        more = capacity_share(surg, pd.Timestamp("2025-11-05"))
        self.assertLess(fewer["biz_days"], more["biz_days"])


class TestRoomAndWeekdayExclusion(unittest.TestCase):
    """⑦OP-11A/外手セ/心カテはS1/S2/S3/S6に入らずS4には入る／⑧土日祝行はS1/S2/S3/S6の母数外。"""

    def test_non_active_room_excluded_from_core_but_present_in_s4(self):
        rows = [
            _row(BASE, room="OP-11A", core=False, enter="10:00", leave="11:00", kind="緊急"),
            _row(BASE, room="外手セ", core=False, enter="10:00", leave="11:00", kind="緊急"),
            _row(BASE, room="心カテ", core=False, enter="10:00", leave="11:00", kind="緊急"),
        ]
        surg = pd.DataFrame(rows)
        self.assertEqual(overtime_ratio(surg, BASE)["s1"]["n"], 0)
        self.assertEqual(turnover_minutes(surg, BASE)["n"], 0)
        self.assertEqual(capacity_share(surg, BASE)["n"], 0)
        self.assertEqual(planned_actual_ratio(surg, BASE)["n"], 0)
        self.assertEqual(urgent_hour_dow(surg, BASE)["n"], 3)  # 全室対象なのでS4には入る

    def test_weekend_or_holiday_rows_excluded_from_core_metrics(self):
        rows = [_row(BASE, core=True, weekday=False, enter="10:00", leave="11:00")]
        surg = pd.DataFrame(rows)
        self.assertEqual(overtime_ratio(surg, BASE)["s1"]["n"], 0)
        self.assertEqual(turnover_minutes(surg, BASE)["n"], 0)
        self.assertEqual(capacity_share(surg, BASE)["n"], 0)
        self.assertEqual(planned_actual_ratio(surg, BASE)["n"], 0)


class TestEmptyAndAllNaN(unittest.TestCase):
    """⑨空DataFrame・全行NaN時刻でも例外なく n:0。⑩payloadがjson.dumps可能。"""

    def test_empty_dataframe_all_n_zero(self):
        surg = _empty_surg()
        payload = build_surgery_ops_payload(surg, BASE)
        for key in ("s1", "s1b", "s2", "s3", "s4", "s5", "s6", "s7"):
            self.assertEqual(payload[key]["n"], 0, msg=f"{key}.n が0でない")

    def test_all_nan_times_all_n_zero_no_exception(self):
        rows = [_row(BASE, enter=None, leave=None) for _ in range(5)]
        surg = pd.DataFrame(rows)
        payload = build_surgery_ops_payload(surg, BASE)
        self.assertEqual(payload["s1"]["n"], 0)
        self.assertEqual(payload["s2"]["n"], 0)
        self.assertEqual(payload["s5"]["n"], 0)
        self.assertEqual(payload["s6"]["n"], 0)

    def test_payload_json_dumps_ok_for_empty_input(self):
        surg = _empty_surg()
        payload = build_surgery_ops_payload(surg, BASE)
        dumped = json.dumps(payload, ensure_ascii=False)
        self.assertIsInstance(dumped, str)

    def test_payload_json_dumps_ok_for_rich_input(self):
        surg = _rich_surg()
        payload = build_surgery_ops_payload(surg, BASE)
        dumped = json.dumps(payload, ensure_ascii=False)
        self.assertIsInstance(dumped, str)
        reloaded = json.loads(dumped)
        self.assertEqual(set(reloaded.keys()),
                          {"s1", "s1b", "s2", "s3", "s4", "s5", "s6", "s7", "meta"})


class TestPayloadShape(unittest.TestCase):
    """S5(days/day_labels)・S7(3チャート)の追加shapeを軽く確認する。"""

    def test_s5_has_7_days_and_labels(self):
        surg = _rich_surg()
        out = or_timeline(surg, BASE)
        self.assertEqual(len(out["day_labels"]), 7)
        self.assertEqual(set(out["days"].keys()), {d for d, _lbl in out["day_labels"]})
        for d_key, chart in out["days"].items():
            self.assertIn("traces", chart)
            self.assertIn("layout", chart)

    def test_s7_has_three_charts(self):
        surg = _rich_surg()
        out = interrupt_mix(surg, BASE)
        self.assertIn("chart", out)
        self.assertIn("mix_kind", out)
        self.assertIn("mix_io", out)
        self.assertEqual(len(out["mix_kind"]["traces"]), 3)  # 通常/臨時/緊急
        self.assertEqual(len(out["mix_io"]["traces"]), 2)    # 入院/外来


class TestStripDetailOnlyJson(unittest.TestCase):
    """dept.html 用 JSON から charts.surgery_ops / charts.profit_translate（detail専用）
    だけが除かれることを確認する（旧 strip_surgery_ops_json → strip_detail_only_json 改名）。"""

    def test_strip_removes_only_surgery_ops(self):
        from app.lib.html_builder import strip_detail_only_json
        src = json.dumps(
            {"charts": {"dow_heatmaps": {"a": 1}, "surgery_ops": {"s1": {"n": 0}}},
             "trend": [1, 2, {"値": "日本語"}]},
            ensure_ascii=False)
        out = json.loads(strip_detail_only_json(src))
        self.assertNotIn("surgery_ops", out["charts"])
        self.assertEqual(out["charts"]["dow_heatmaps"], {"a": 1})
        self.assertEqual(out["trend"], [1, 2, {"値": "日本語"}])

    def test_strip_without_key_is_noop(self):
        from app.lib.html_builder import strip_detail_only_json
        src = json.dumps({"charts": {"x": 1}}, ensure_ascii=False)
        out = json.loads(strip_detail_only_json(src))
        self.assertEqual(out["charts"], {"x": 1})

    def test_strip_removes_profit_translate_too(self):
        from app.lib.html_builder import strip_detail_only_json
        src = json.dumps(
            {"charts": {"dow_heatmaps": {"a": 1}, "surgery_ops": {"s1": {"n": 0}},
                        "profit_translate": {"k1": {"hospital": {}}}},
             "trend": [1, 2, {"値": "日本語"}]},
            ensure_ascii=False)
        out = json.loads(strip_detail_only_json(src))
        self.assertNotIn("surgery_ops", out["charts"])
        self.assertNotIn("profit_translate", out["charts"])
        self.assertEqual(out["charts"]["dow_heatmaps"], {"a": 1})
        self.assertEqual(out["trend"], [1, 2, {"値": "日本語"}])


if __name__ == "__main__":
    unittest.main()
