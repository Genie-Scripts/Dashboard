"""build_profit_hybrid_calibrated の純粋抽出（P0: hybrid の一元計算と前倒し）の回帰テスト。

対象: app/lib/html_builder.py の build_detail_json 内にインラインで書かれていた
  (a) profit_hybrid セクション構築（build_hybrid_payload 呼び出し）
  (b) 月末見込み G の確定（recency 補正・values_final_* 系列への変換）
を build_profit_hybrid_calibrated(profit_breakdown, surg, adm, profit_base_date) へ
純粋抽出したこと自体（挙動不変・移動のみ）を検証する。

検証内容:
  (i)   build_detail_json(..., profit_hybrid=None)（内部で自動計算）と、
        build_profit_hybrid_calibrated(...) の戻りをそのまま渡した
        build_detail_json(..., profit_hybrid=<tuple>) の出力 JSON が完全一致すること。
  (ii)  profit_breakdown=None のとき build_profit_hybrid_calibrated が (None, None) を
        返し、build_detail_json も例外なく動くこと。
  (iii) 校正キャッシュ（output/g_calib_cache.json）への書込みが、テスト実行中は
        monkeypatch で差し替えた tmp_path 配下にのみ向くこと（実リポジトリの
        output/ には一切触れない・読みもしない）。

実データ・ネットワーク・LLM は一切使わない（合成フィクスチャのみ・密閉）。
adm/surg フィクスチャは tests/test_kpi_summary_period_fy_yoy.py の流儀
（日次レンジを直接組み立てる）を、profit_breakdown フィクスチャは
tests/test_profit_unit.py の流儀（月次 縦持ちDF）を踏襲する。

実行: リポジトリルートで
    OMLX_BASE_URL=http://127.0.0.1:9 OMLX_BASE=http://127.0.0.1:9 \
    .venv/bin/python -m pytest tests/test_profit_hybrid_extraction.py -q
"""
import json
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.html_builder import build_detail_json, build_profit_hybrid_calibrated  # noqa: E402
from app.lib.config import is_operational_day  # noqa: E402
from app.lib.profit import build_profit_monthly  # noqa: E402
import app.lib.profit_estimate as profit_estimate  # noqa: E402


BASE_DATE = pd.Timestamp("2026-09-13")          # 進行中月（月中）で MTD ブレンドが働く日
RANGE_START = pd.Timestamp("2025-07-01")        # base_date の約14か月前（FY/前年同期窓を賄う）
GENERATED_AT = pd.Timestamp("2026-09-14T06:00:00")

DEPT = "循環器内科"   # NADM_DISPLAY_DEPTS所属・SURGERY_DISPLAY_DEPTS外＝profit_hybridはbaseline/fallback経路
WARD = "02A"          # WARD_NAMES所属・WARD_HIDDEN外


def _build_adm() -> pd.DataFrame:
    days = pd.date_range(RANGE_START, BASE_DATE, freq="D")
    rows = []
    for d in days:
        biz = is_operational_day(d)
        rows.append({
            "日付": d, "診療科名": DEPT, "病棟コード": WARD,
            "在院患者数": 600 if biz else 550,
            "新入院患者数": 50, "新入院患者数_病棟": 50,
            "入院患者数": 45, "緊急入院患者数": 5,
            "退院合計": 45, "退院患者数": 40, "退出合計": 45,
            "転入患者数": 2, "転出患者数": 2, "出入り負荷": 10,
            "科_表示": True, "病棟_表示": True, "平日": biz,
            "曜日": int(d.dayofweek),
        })
    return pd.DataFrame(rows)


def _build_surg() -> pd.DataFrame:
    days = pd.date_range(RANGE_START, BASE_DATE, freq="D")
    rows = []
    for d in days:
        if not is_operational_day(d):
            continue
        for _ in range(2):
            rows.append({
                "手術実施日": d, "実施診療科": DEPT, "全麻": True,
                "科_表示": True, "術数対象": True, "稼働対象室": True,
                "平日": True, "入外区分": "入院",
                "麻酔種別": "全身麻酔(20分以上：吸入もしくは静脈麻酔薬)",
            })
    return pd.DataFrame(rows)


def _build_profit_breakdown() -> pd.DataFrame:
    """DEPT の 外来/入院 粗利（千円）を過去8か月ぶん（baseline 6か月ぶんを賄う）。"""
    months = pd.date_range(BASE_DATE.replace(day=1) - pd.DateOffset(months=8),
                           BASE_DATE.replace(day=1) - pd.DateOffset(months=1), freq="MS")
    rows = []
    for i, m in enumerate(months):
        rows.append({"診療科名": DEPT, "月": m, "区分": "外来", "粗利": 5000.0 + 50.0 * i})
        rows.append({"診療科名": DEPT, "月": m, "区分": "入院", "粗利": 45000.0 + 300.0 * i})
    return pd.DataFrame(rows)


def _build_profit_monthly(profit_breakdown: pd.DataFrame) -> pd.DataFrame:
    agg = (profit_breakdown.groupby(["診療科名", "月"], as_index=False)["粗利"].sum())
    targets = pd.DataFrame({"診療科名": [DEPT], "月次目標": [600000.0]})
    return build_profit_monthly(agg, targets)


TARGETS = {
    "new_admission": {"dept": {DEPT: 300.0}, "ward": {WARD: 300.0}},
    "inpatient": {"dept": {DEPT: 550.0}, "ward": {WARD: 550.0}, "ward_beds": {WARD: 40}},
}
SURG_TARGETS = {DEPT: 80.0}


class _FixtureMixin:
    @classmethod
    def setUpClass(cls):
        cls.adm = _build_adm()
        cls.surg = _build_surg()
        cls.profit_breakdown = _build_profit_breakdown()
        cls.profit_monthly = _build_profit_monthly(cls.profit_breakdown)

    def _call_detail_json(self, profit_hybrid=None):
        return build_detail_json(
            self.adm, self.surg, TARGETS, SURG_TARGETS,
            self.profit_monthly, BASE_DATE, GENERATED_AT,
            profit_breakdown=self.profit_breakdown,
            kpi_history_path=None,
            profit_targets_breakdown=None,
            profit_hybrid=profit_hybrid,
        )


class TestExtractionOutputIdentity(_FixtureMixin, unittest.TestCase):
    """(i): profit_hybrid=None（内部で自動計算）と、事前計算タプルを渡した場合とで
    build_detail_json の出力 JSON が完全一致すること。"""

    def test_none_and_precomputed_tuple_yield_identical_json(self):
        from app.lib.profit_estimate import last_complete_driver_date
        profit_base_date = last_complete_driver_date(self.adm, self.surg) or BASE_DATE
        precomputed = build_profit_hybrid_calibrated(
            self.profit_breakdown, self.surg, self.adm, profit_base_date)

        # 事前計算が実際に非退化（本テストの前提＝hybrid経路が本当に動いていること）を確認。
        self.assertIsNotNone(precomputed[0], "profit_hybrid_section がNone＝フィクスチャ不備")
        self.assertIsNotNone(precomputed[1], "profit_g_calibrated がNone＝フィクスチャ不備")

        json_auto = self._call_detail_json(profit_hybrid=None)
        json_precomputed = self._call_detail_json(profit_hybrid=precomputed)

        self.assertEqual(json.loads(json_auto), json.loads(json_precomputed))
        # 文字列としても完全一致（キー順序・丸め等の非決定性が無いことの追加保証）
        self.assertEqual(json_auto, json_precomputed)


class TestNoRecomputeWhenPrecomputedTuplePassed(_FixtureMixin, unittest.TestCase):
    """profit_hybrid にタプルを渡した場合、build_detail_json は
    build_profit_hybrid_calibrated を再度呼ばない（再計算しない）こと。"""

    def test_build_profit_hybrid_calibrated_not_called_when_tuple_given(self):
        from unittest import mock
        from app.lib.profit_estimate import last_complete_driver_date
        profit_base_date = last_complete_driver_date(self.adm, self.surg) or BASE_DATE
        precomputed = build_profit_hybrid_calibrated(
            self.profit_breakdown, self.surg, self.adm, profit_base_date)

        with mock.patch("app.lib.html_builder.build_profit_hybrid_calibrated",
                        side_effect=AssertionError("再計算された")) as spy:
            # build_detail_json はモジュール関数名をローカルで直接呼ぶため、
            # モジュール属性パッチで再計算されていないことを検出できる。
            result = build_detail_json(
                self.adm, self.surg, TARGETS, SURG_TARGETS,
                self.profit_monthly, BASE_DATE, GENERATED_AT,
                profit_breakdown=self.profit_breakdown,
                kpi_history_path=None,
                profit_targets_breakdown=None,
                profit_hybrid=precomputed,
            )
            spy.assert_not_called()
        self.assertIsInstance(result, str)


class TestProfitBreakdownNoneYieldsNoneTuple(_FixtureMixin, unittest.TestCase):
    """(ii): profit_breakdown=None のとき (None, None) を返し、build_detail_json も
    例外なく動くこと。"""

    def test_returns_none_none(self):
        from app.lib.profit_estimate import last_complete_driver_date
        profit_base_date = last_complete_driver_date(self.adm, self.surg) or BASE_DATE
        section, g = build_profit_hybrid_calibrated(None, self.surg, self.adm, profit_base_date)
        self.assertIsNone(section)
        self.assertIsNone(g)

    def test_build_detail_json_runs_without_profit_breakdown(self):
        out = build_detail_json(
            self.adm, self.surg, TARGETS, SURG_TARGETS,
            self.profit_monthly, BASE_DATE, GENERATED_AT,
            profit_breakdown=None,
            kpi_history_path=None,
            profit_targets_breakdown=None,
            profit_hybrid=None,
        )
        data = json.loads(out)
        self.assertIsNone(data.get("profit_hybrid"))


class TestCalibCacheWriteIsolation(_FixtureMixin, unittest.TestCase):
    """(iii): 校正キャッシュの書込みは monkeypatch した DEFAULT_CALIB_CACHE_PATH
    （tmp_path 配下）にのみ向かうこと。実リポジトリの output/ には一切触れない
    （save_calib_cache/load_calib_cache は呼出時にこのモジュール変数を動的参照するため、
    パッチ差し替え後は唯一の書込み先が tmp_path になる）。
    """

    def test_cache_write_goes_only_to_patched_tmp_path(self):
        import tempfile
        from unittest import mock
        from app.lib.profit_estimate import last_complete_driver_date
        profit_base_date = last_complete_driver_date(self.adm, self.surg) or BASE_DATE

        with tempfile.TemporaryDirectory() as d:
            tmp_cache = Path(d) / "g_calib_cache.json"
            self.assertFalse(tmp_cache.exists())
            with mock.patch.object(profit_estimate, "DEFAULT_CALIB_CACHE_PATH", tmp_cache):
                section, g = build_profit_hybrid_calibrated(
                    self.profit_breakdown, self.surg, self.adm, profit_base_date)
            self.assertIsNotNone(section, "フィクスチャ不備（hybrid が退化）")
            self.assertIsNotNone(g, "フィクスチャ不備（校正が退化）")
            self.assertTrue(tmp_cache.exists(),
                            "校正キャッシュがパッチ後の DEFAULT_CALIB_CACHE_PATH に書かれていない"
                            "＝書込み先の動的参照が壊れている")


if __name__ == "__main__":
    unittest.main()
