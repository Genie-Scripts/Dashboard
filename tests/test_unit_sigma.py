"""unit_sigma.py（P2: ユニット別σ の実測値と閾値変換）の回帰テスト。

対象:
  - sigma_for         : kind×unit → σ(%)。未知ユニットは None
  - arrow_threshold   : max(floor, 1.5σ)。σ不明は floor
  - UNIT_SIGMA        : 3キー(dept_census/ward_census/dept_surgery)が揃っていること

σの数値そのもの（scripts/measure_unit_sigma.py の実測値）は年1回の再計測で変わる想定
のため、`sigma_for`/`arrow_threshold` の計算ロジックは合成辞書（unittest.mock.patch.dict）
で検証し、実測値には依存しない（密閉）。

実行: リポジトリルートで
    .venv/bin/python -m pytest -q tests/test_unit_sigma.py
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib import unit_sigma as us  # noqa: E402
from app.lib.unit_sigma import (  # noqa: E402
    CENSUS_FLOOR, SURGERY_FLOOR,
    UNIT_SIGMA, sigma_for, arrow_threshold,
)

_SYNTHETIC = {
    "dept_census": {"テスト内科": 10.0, "テスト外科": 1.0},
    "ward_census": {"01A": 2.0},
    "dept_surgery": {"テスト外科": 20.0, "テスト眼科": 0.1},
}


class TestSigmaFor(unittest.TestCase):
    @mock.patch.dict(us.UNIT_SIGMA, _SYNTHETIC, clear=True)
    def test_known_unit_returns_measured_value(self):
        self.assertEqual(sigma_for("dept_census", "テスト内科"), 10.0)
        self.assertEqual(sigma_for("dept_surgery", "テスト外科"), 20.0)

    @mock.patch.dict(us.UNIT_SIGMA, _SYNTHETIC, clear=True)
    def test_unknown_unit_returns_none(self):
        self.assertIsNone(sigma_for("dept_census", "存在しない科"))

    @mock.patch.dict(us.UNIT_SIGMA, _SYNTHETIC, clear=True)
    def test_unknown_kind_returns_none(self):
        self.assertIsNone(sigma_for("unknown_kind", "テスト内科"))


class TestArrowThreshold(unittest.TestCase):
    @mock.patch.dict(us.UNIT_SIGMA, _SYNTHETIC, clear=True)
    def test_large_sigma_exceeds_census_floor(self):
        # 1.5 * 10.0 = 15.0 > CENSUS_FLOOR(3.0)
        self.assertEqual(arrow_threshold("dept_census", "テスト内科"), 15.0)

    @mock.patch.dict(us.UNIT_SIGMA, _SYNTHETIC, clear=True)
    def test_small_sigma_stays_at_census_floor(self):
        # 1.5 * 1.0 = 1.5 < CENSUS_FLOOR(3.0)
        self.assertEqual(arrow_threshold("dept_census", "テスト外科"), CENSUS_FLOOR)

    @mock.patch.dict(us.UNIT_SIGMA, _SYNTHETIC, clear=True)
    def test_large_sigma_exceeds_surgery_floor(self):
        # 1.5 * 20.0 = 30.0 > SURGERY_FLOOR(15.0)
        self.assertEqual(arrow_threshold("dept_surgery", "テスト外科"), 30.0)

    @mock.patch.dict(us.UNIT_SIGMA, _SYNTHETIC, clear=True)
    def test_small_sigma_stays_at_surgery_floor(self):
        # 1.5 * 0.1 = 0.15 < SURGERY_FLOOR(15.0)（眼科型: 全麻ほぼ0想定のσ小ケース）
        self.assertEqual(arrow_threshold("dept_surgery", "テスト眼科"), SURGERY_FLOOR)

    @mock.patch.dict(us.UNIT_SIGMA, _SYNTHETIC, clear=True)
    def test_unknown_unit_falls_back_to_census_floor(self):
        self.assertEqual(arrow_threshold("dept_census", "新設病棟の科"), CENSUS_FLOOR)

    @mock.patch.dict(us.UNIT_SIGMA, _SYNTHETIC, clear=True)
    def test_unknown_unit_falls_back_to_surgery_floor(self):
        self.assertEqual(arrow_threshold("dept_surgery", "新設外科"), SURGERY_FLOOR)

    @mock.patch.dict(us.UNIT_SIGMA, {}, clear=True)
    def test_unknown_ward_unit_falls_back_to_census_floor(self):
        self.assertEqual(arrow_threshold("ward_census", "99Z"), CENSUS_FLOOR)


class TestUnitSigmaStructure(unittest.TestCase):
    def test_three_keys_present(self):
        self.assertEqual(set(UNIT_SIGMA.keys()), {"dept_census", "ward_census", "dept_surgery"})

    def test_all_values_are_positive_floats(self):
        for kind, d in UNIT_SIGMA.items():
            for unit, sigma in d.items():
                self.assertIsInstance(sigma, float, f"{kind}/{unit}")
                self.assertGreater(sigma, 0.0, f"{kind}/{unit}")


if __name__ == "__main__":
    unittest.main()
