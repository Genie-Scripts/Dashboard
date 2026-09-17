"""
profit_headline.py（粗利ヘッドライン正本モジュール）のユニットテスト（合成データのみ）。

対象:
  - build_profit_headline の nyuin/total ブロックが、既存関数
    （profit_unit.build_profit_unit_payload / month_projection.build_month_projection_payload）
    をそのまま呼んだ結果と1:1で一致すること（新しい推計器を作っていないことの確認）
  - 恒等式: total.proj_mm ≈ nyuin.profit_mm + gairai.profit_mm（±0.1百万円）→ identity_ok
  - 外来（gairai）ブロック: ppd_biz・target_per_biz_day・前月比・direction のデッドバンド
  - fail-soft: hybrid=(None, None) / meta欠落 / 外来目標なし
  - hybrid=None のときの遅延 import 経路（html_builder 未完成でも壊れないよう monkeypatch
    で「呼ばれること」だけを確認し、実計算はしない）

実行: リポジトリルートで
    OMLX_BASE_URL=http://127.0.0.1:9 .venv/bin/python -m pytest tests/test_profit_headline.py -q
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib.profit_headline import build_profit_headline  # noqa: E402
from app.lib.profit_unit import build_profit_unit_payload, _PROJECTION_DEADBAND_PCT  # noqa: E402
from app.lib.month_projection import build_month_projection_payload  # noqa: E402
from app.lib.config import biz_days_in_month, STD_BIZ_DAYS_PER_MONTH  # noqa: E402
import app.lib.html_builder as html_builder  # noqa: E402


DEPT = "科A"
BASE_DATE = pd.Timestamp("2026-09-13")
META = {
    "latest_final_nyuin": 60.0,
    "latest_final_gairai": 25.0,
    "latest_final_total": 85.0,
    "window_end": "2026-09-13",
}
G_MILLION = 85.0  # = nyuin(60.0) + gairai(25.0)（恒等式が成立するよう組んだフィクスチャ）


def _adm_daily(dept: str, start: str, end: str, census: float, disch: float,
              nadm: float = 10.0) -> pd.DataFrame:
    idx = pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="D")
    return pd.DataFrame({
        "日付": idx, "診療科名": dept, "在院患者数": census,
        "退院合計": disch, "新入院患者数": nadm, "科_表示": True,
    })


def _empty_surg() -> pd.DataFrame:
    return pd.DataFrame({
        "手術実施日": pd.Series([], dtype="datetime64[ns]"),
        "実施診療科": pd.Series([], dtype=str),
        "全麻": pd.Series([], dtype=bool),
        "術数対象": pd.Series([], dtype=bool),
    })


def _pb_row(dept: str, month: str, kubun: str, profit_千円: float) -> dict:
    return {"診療科名": dept, "月": pd.Timestamp(month), "区分": kubun, "粗利": profit_千円}


def _base_fixture():
    """anchor月(2026-08)確報・当月(2026-09, 13日経過)進行中。入院/外来とも hybrid meta あり。"""
    adm = _adm_daily(DEPT, "2026-07-01", "2026-09-13", census=500.0, disch=40.0)
    pb = pd.DataFrame([
        _pb_row(DEPT, "2026-08-01", "入院", 47000.0),
        _pb_row(DEPT, "2026-08-01", "外来", 18000.0),
    ])
    targets_bd = pd.DataFrame([
        {"診療科名": DEPT, "区分": "入院", "月次目標": 55000.0},
        {"診療科名": DEPT, "区分": "外来", "月次目標": 20300.0},
    ])
    pm = pd.DataFrame({"診療科名": [DEPT], "月": [pd.Timestamp("2026-09-01")], "月次目標": [70000.0]})
    surg = _empty_surg()
    section = {"meta": dict(META), "hospital_series": {}}
    hybrid = (section, G_MILLION)
    return adm, pb, targets_bd, pm, surg, hybrid


class TestHappyPathPassthroughAndIdentity(unittest.TestCase):
    """既存関数をそのまま呼んだ結果と1:1一致すること＋恒等式。"""

    def setUp(self):
        self.adm, self.pb, self.targets_bd, self.pm, self.surg, self.hybrid = _base_fixture()

    def _call(self):
        return build_profit_headline(
            self.adm, self.surg, self.pm, self.pb, self.targets_bd, BASE_DATE,
            hybrid=self.hybrid,
        )

    def test_top_level_keys_present(self):
        out = self._call()
        self.assertIsNotNone(out)
        for key in ("month", "as_of", "nyuin", "gairai", "total", "latest",
                    "prev_month_pending", "identity_ok", "target_identity_ok", "labels"):
            self.assertIn(key, out)

    def test_month_and_as_of(self):
        out = self._call()
        self.assertEqual(out["month"], "2026-09")
        self.assertEqual(out["as_of"], "2026-09-13")

    def test_nyuin_block_matches_profit_unit_projection(self):
        out = self._call()
        section, _g = self.hybrid
        ref = build_profit_unit_payload(
            self.pb, self.adm, BASE_DATE,
            profit_targets_breakdown=self.targets_bd,
            profit_hybrid_meta=section["meta"],
            hospital_series=section.get("hospital_series"),
        )
        proj = ref["global"]["projection"]
        self.assertIsNotNone(proj, "フィクスチャ不備（projection が組めていない）")
        self.assertEqual(out["nyuin"], {
            "ppd": proj["ppd"], "patient_days": proj["patient_days"],
            "profit_mm": proj["profit_mm"], "target_mm": proj["target_mm"],
            "achievement_pct": proj["achievement_pct"],
            "vs_prev_pct": proj["vs_prev_pct"], "direction": proj["direction"],
        })
        self.assertEqual(out["latest"], ref["global"]["latest"])
        self.assertEqual(out["prev_month_pending"], ref["global"]["prev_month_pending"])

    def test_total_block_matches_month_projection(self):
        out = self._call()
        section, g = self.hybrid
        ref = build_month_projection_payload(
            adm=self.adm, surg=self.surg, profit_monthly=self.pm,
            profit_hybrid_meta=section["meta"],
            profit_hybrid_hospital_series=section.get("hospital_series"),
            base_date=BASE_DATE, profit_hybrid_g_override=g,
        )
        tile = ref["profit"]
        self.assertIsNotNone(tile, "フィクスチャ不備（profit tile が組めていない）")
        self.assertEqual(out["total"], {
            "proj_mm": tile["projection"], "target_mm": tile["target"], "rate": tile["rate"],
            "status": {"css": tile["status_css"], "shape": tile["status_shape"],
                      "text": tile["status_text"]},
        })

    def test_identity_holds(self):
        out = self._call()
        self.assertTrue(out["identity_ok"])
        self.assertLessEqual(
            abs(out["total"]["proj_mm"] - (out["nyuin"]["profit_mm"] + out["gairai"]["profit_mm"])),
            0.1)

    def test_gairai_block(self):
        out = self._call()
        biz_days = biz_days_in_month(pd.Timestamp("2026-09-01"))
        prev_biz_days = biz_days_in_month(pd.Timestamp("2026-08-01"))
        self.assertEqual(out["gairai"]["biz_days"], biz_days)
        self.assertEqual(out["gairai"]["profit_mm"], 25.0)
        exact_ppd_biz = 25.0 / biz_days
        expected_ppd_biz = round(exact_ppd_biz, 1)
        self.assertEqual(out["gairai"]["ppd_biz"], expected_ppd_biz)
        # 外来目標（フィクスチャ=20300千円）: 補正前(名目)と、営業日補正後（profit.py の
        # 外来補正目標＝目標×当月営業日数/STD_BIZ_DAYS_PER_MONTHと同式）。
        target_mm_nominal = round(20300.0 / 1000.0, 1)
        expected_target_mm = round(20300.0 * biz_days / STD_BIZ_DAYS_PER_MONTH / 1000.0, 1)
        self.assertEqual(out["gairai"]["target_mm_nominal"], target_mm_nominal)
        self.assertEqual(out["gairai"]["target_mm"], expected_target_mm)
        self.assertEqual(out["gairai"]["target_per_biz_day"], round(expected_target_mm / biz_days, 1))
        self.assertEqual(out["gairai"]["achievement_pct"], round(25.0 / expected_target_mm * 100, 1))
        prev_ppd_biz = 18.0 / prev_biz_days
        expected_vs_prev = round((exact_ppd_biz - prev_ppd_biz) / prev_ppd_biz * 100, 1)
        self.assertEqual(out["gairai"]["vs_prev_pct"], expected_vs_prev)

    def test_gairai_ppd_biz_rounded_to_one_decimal(self):
        """ppd_biz / target_per_biz_day は小数1桁に丸める（2桁にはしない）。"""
        out = self._call()
        biz_days = biz_days_in_month(pd.Timestamp("2026-09-01"))
        self.assertEqual(out["gairai"]["ppd_biz"], round(25.0 / biz_days, 1))
        self.assertNotEqual(out["gairai"]["ppd_biz"], round(25.0 / biz_days, 2))
        expected_target_mm = round(20300.0 * biz_days / STD_BIZ_DAYS_PER_MONTH / 1000.0, 1)
        self.assertEqual(out["gairai"]["target_per_biz_day"], round(expected_target_mm / biz_days, 1))
        self.assertNotEqual(out["gairai"]["target_per_biz_day"], round(expected_target_mm / biz_days, 2))

    def test_gairai_target_per_biz_day_is_month_invariant(self):
        """target_per_biz_day は名目目標/STD_BIZ_DAYS_PER_MONTHと一致する（営業日数に
        よらず月不変。9月＝営業日19日の月で確認）。"""
        out = self._call()
        biz_days = biz_days_in_month(pd.Timestamp("2026-09-01"))
        self.assertEqual(biz_days, 19, "フィクスチャ前提（背景記載の9月=営業日19日）が崩れている")
        target_mm_nominal = out["gairai"]["target_mm_nominal"]
        self.assertEqual(out["gairai"]["target_per_biz_day"],
                         round(target_mm_nominal / STD_BIZ_DAYS_PER_MONTH, 1))

    def test_labels_present(self):
        out = self._call()
        labels = out["labels"]
        self.assertEqual(labels["main_nyuin"], "入院 粗利/人日")
        self.assertEqual(labels["main_gairai"], "外来 粗利/営業日")
        self.assertEqual(labels["target"], "月次目標")
        self.assertEqual(labels["target_gairai"], "月次目標(補正)")
        self.assertEqual(labels["tolerance"], "（±2%）")
        self.assertIn("見込みの誤差は月間", labels["note"])
        self.assertIn("回転を上げると上がります", labels["guard"])
        self.assertEqual(labels["scope_nyuin"], "入院のみ・全科")
        self.assertEqual(labels["scope_gairai"], "外来のみ・全科")
        self.assertEqual(labels["cmp"], "前月比")  # S1（確報遅れなし）
        self.assertTrue(labels["period"].startswith("2026年9月"))
        self.assertIn("月末見込み（", labels["period"])
        self.assertIn("9/13時点・診療実績ベース・暫定）", labels["period"])


class TestTargetIdentity(unittest.TestCase):
    """total.target_mm ≈ nyuin.target_mm + gairai.target_mm（両方とも日数補正済み目標。
    ±0.1百万円）→ target_identity_ok。profit_monthly に 外来目標/入院目標 列を持たせ
    （month_projection.profit_target_for_month の has_bd 分岐）、profit_targets_breakdown
    と同じ値にすると、入院=暦日補正・外来=営業日補正の各目標の和と、合計目標（同じ2式の
    和）が一致するフィクスチャになる。"""

    def test_target_identity_ok_when_breakdown_matches(self):
        adm = _adm_daily(DEPT, "2026-07-01", "2026-09-13", census=500.0, disch=40.0)
        pb = pd.DataFrame([
            _pb_row(DEPT, "2026-08-01", "入院", 47000.0),
            _pb_row(DEPT, "2026-08-01", "外来", 18000.0),
        ])
        # 73000/20000 は Sept(暦日30, 営業日19)で cal/biz 補正後にちょうど整数千円になる
        # よう逆算した値（入院=73000×30/(365/12)=72000, 外来=20000×19/20=19000）。
        # 丸め誤差を挟まず恒等式が厳密に成立するフィクスチャにする。
        targets_bd = pd.DataFrame([
            {"診療科名": DEPT, "区分": "入院", "月次目標": 73000.0},
            {"診療科名": DEPT, "区分": "外来", "月次目標": 20000.0},
        ])
        pm = pd.DataFrame({
            "診療科名": [DEPT], "月": [pd.Timestamp("2026-09-01")],
            "外来目標": [20000.0], "入院目標": [73000.0],
        })
        surg = _empty_surg()
        section = {"meta": dict(META), "hospital_series": {}}
        hybrid = (section, G_MILLION)

        out = build_profit_headline(adm, surg, pm, pb, targets_bd, BASE_DATE, hybrid=hybrid)

        self.assertIsNotNone(out)
        self.assertIsNotNone(out["total"]["target_mm"])
        self.assertIsNotNone(out["nyuin"]["target_mm"])
        self.assertIsNotNone(out["gairai"]["target_mm"])
        self.assertLessEqual(
            abs(out["total"]["target_mm"] - (out["nyuin"]["target_mm"] + out["gairai"]["target_mm"])),
            0.1)
        self.assertTrue(out["target_identity_ok"])


class TestGairaiDirectionDeadband(unittest.TestCase):
    """外来 direction: profit_unit と同じ ±1.5% デッドバンド。"""

    def _make(self, prev_gairai_千円: float, cur_gairai_mm: float):
        adm = _adm_daily(DEPT, "2026-07-01", "2026-09-13", census=500.0, disch=40.0)
        pb = pd.DataFrame([
            _pb_row(DEPT, "2026-08-01", "入院", 47000.0),
            _pb_row(DEPT, "2026-08-01", "外来", prev_gairai_千円),
        ])
        targets_bd = pd.DataFrame([
            {"診療科名": DEPT, "区分": "入院", "月次目標": 55000.0},
            {"診療科名": DEPT, "区分": "外来", "月次目標": 20000.0},
        ])
        pm = pd.DataFrame({"診療科名": [DEPT], "月": [pd.Timestamp("2026-09-01")], "月次目標": [70000.0]})
        surg = _empty_surg()
        meta = dict(META)
        meta["latest_final_gairai"] = cur_gairai_mm
        meta["latest_final_total"] = meta["latest_final_nyuin"] + cur_gairai_mm
        section = {"meta": meta, "hospital_series": {}}
        g = meta["latest_final_total"]
        return build_profit_headline(adm, surg, pm, pb, targets_bd, BASE_DATE, hybrid=(section, g))

    def test_flat_within_deadband(self):
        biz_days_sep = biz_days_in_month(pd.Timestamp("2026-09-01"))
        biz_days_aug = biz_days_in_month(pd.Timestamp("2026-08-01"))
        prev_千円 = 18000.0
        prev_ppd_biz = prev_千円 / 1000.0 / biz_days_aug
        cur_mm = round(prev_ppd_biz * biz_days_sep, 4)  # ほぼ同水準 → flat
        out = self._make(prev_千円, cur_mm)
        self.assertIsNotNone(out)
        self.assertEqual(out["gairai"]["direction"], "flat")

    def test_up_beyond_deadband(self):
        biz_days_sep = biz_days_in_month(pd.Timestamp("2026-09-01"))
        biz_days_aug = biz_days_in_month(pd.Timestamp("2026-08-01"))
        prev_千円 = 18000.0
        prev_ppd_biz = prev_千円 / 1000.0 / biz_days_aug
        cur_mm = prev_ppd_biz * 1.10 * biz_days_sep  # +10%
        out = self._make(prev_千円, cur_mm)
        self.assertIsNotNone(out)
        self.assertEqual(out["gairai"]["direction"], "up")
        self.assertGreaterEqual(out["gairai"]["vs_prev_pct"], _PROJECTION_DEADBAND_PCT)

    def test_down_beyond_deadband(self):
        biz_days_sep = biz_days_in_month(pd.Timestamp("2026-09-01"))
        biz_days_aug = biz_days_in_month(pd.Timestamp("2026-08-01"))
        prev_千円 = 18000.0
        prev_ppd_biz = prev_千円 / 1000.0 / biz_days_aug
        cur_mm = prev_ppd_biz * 0.90 * biz_days_sep  # -10%
        out = self._make(prev_千円, cur_mm)
        self.assertIsNotNone(out)
        self.assertEqual(out["gairai"]["direction"], "down")
        self.assertLessEqual(out["gairai"]["vs_prev_pct"], -_PROJECTION_DEADBAND_PCT)


class TestFailSoft(unittest.TestCase):
    def setUp(self):
        self.adm, self.pb, self.targets_bd, self.pm, self.surg, self.hybrid = _base_fixture()

    def test_hybrid_none_none_returns_none(self):
        out = build_profit_headline(self.adm, self.surg, self.pm, self.pb, self.targets_bd,
                                    BASE_DATE, hybrid=(None, None))
        self.assertIsNone(out)

    def test_empty_meta_returns_none(self):
        section = {"meta": {}, "hospital_series": {}}
        out = build_profit_headline(self.adm, self.surg, self.pm, self.pb, self.targets_bd,
                                    BASE_DATE, hybrid=(section, 85.0))
        self.assertIsNone(out)

    def test_missing_gairai_key_returns_none(self):
        section, g = self.hybrid
        meta = dict(section["meta"])
        del meta["latest_final_gairai"]
        section2 = {"meta": meta, "hospital_series": {}}
        out = build_profit_headline(self.adm, self.surg, self.pm, self.pb, self.targets_bd,
                                    BASE_DATE, hybrid=(section2, g))
        self.assertIsNone(out)

    def test_missing_gairai_target_keeps_others_alive(self):
        targets_bd_no_gairai = pd.DataFrame([
            {"診療科名": DEPT, "区分": "入院", "月次目標": 55000.0},
        ])
        out = build_profit_headline(self.adm, self.surg, self.pm, self.pb, targets_bd_no_gairai,
                                    BASE_DATE, hybrid=self.hybrid)
        self.assertIsNotNone(out)
        self.assertIsNone(out["gairai"]["target_mm_nominal"])
        self.assertIsNone(out["gairai"]["target_mm"])
        self.assertIsNone(out["gairai"]["target_per_biz_day"])
        self.assertIsNone(out["gairai"]["achievement_pct"])
        self.assertIsNone(out["target_identity_ok"])
        self.assertIsNotNone(out["gairai"]["profit_mm"])
        self.assertIsNotNone(out["gairai"]["ppd_biz"])


class TestLazyHybridComputation(unittest.TestCase):
    """hybrid=None のときだけ html_builder を遅延 import して計算すること（呼ばれることのみ確認）。"""

    def test_hybrid_none_triggers_lazy_import_and_uses_driver_date(self):
        adm, pb, targets_bd, pm, surg, _hybrid = _base_fixture()
        dummy_section = {"meta": dict(META), "hospital_series": {}}
        driver_date = pd.Timestamp("2026-09-10")
        calls = {}

        def _stub_hybrid(pb_arg, surg_arg, adm_arg, base_arg):
            calls["hybrid_args"] = (pb_arg, surg_arg, adm_arg, base_arg)
            return (dummy_section, 85.0)

        with patch.object(html_builder, "build_profit_hybrid_calibrated",
                          side_effect=_stub_hybrid) as mock_hybrid, \
             patch.object(html_builder, "last_complete_driver_date",
                          return_value=driver_date) as mock_driver:
            out = build_profit_headline(adm, surg, pm, pb, targets_bd, BASE_DATE)

        self.assertEqual(mock_hybrid.call_count, 1)
        self.assertEqual(mock_driver.call_count, 1)
        driver_call_args = mock_driver.call_args.args
        self.assertIs(driver_call_args[0], adm)
        self.assertIs(driver_call_args[1], surg)

        pb_arg, surg_arg, adm_arg, base_arg = calls["hybrid_args"]
        self.assertIs(pb_arg, pb)
        self.assertIs(surg_arg, surg)
        self.assertIs(adm_arg, adm)
        self.assertEqual(base_arg, driver_date)
        self.assertIsNotNone(out)

    def test_driver_date_none_falls_back_to_base_date(self):
        adm, pb, targets_bd, pm, surg, _hybrid = _base_fixture()
        dummy_section = {"meta": dict(META), "hospital_series": {}}
        calls = {}

        def _stub_hybrid(pb_arg, surg_arg, adm_arg, base_arg):
            calls["hybrid_args"] = (pb_arg, surg_arg, adm_arg, base_arg)
            return (dummy_section, 85.0)

        with patch.object(html_builder, "build_profit_hybrid_calibrated",
                          side_effect=_stub_hybrid) as mock_hybrid, \
             patch.object(html_builder, "last_complete_driver_date", return_value=None):
            build_profit_headline(adm, surg, pm, pb, targets_bd, BASE_DATE)

        self.assertEqual(mock_hybrid.call_count, 1)
        _pb_arg, _surg_arg, _adm_arg, base_arg = calls["hybrid_args"]
        self.assertEqual(base_arg, BASE_DATE)


if __name__ == "__main__":
    unittest.main()
