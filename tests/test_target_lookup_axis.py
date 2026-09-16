"""
test_target_lookup_axis.py — build_target_lookup の軸あいまいさ解消の回帰テスト

病院全体の目標行は病棟軸・診療科軸の両方に重複登録されうる（部門コード="全体"）。
病院全体の実績（在院・新入院とも転入を含まない診療科軸と同一基準）に合わせて、
部門種別="診療科" の全体行を明示的に優先する。値が食い違う場合は警告を出す。
診療科軸の全体行が無いときは従来どおり病棟軸へフォールバックする。

密閉テスト: data/ 配下の実データは一切読まない。合成 DataFrame のみで組む。
"""
import sys
import unittest
import warnings
from pathlib import Path

import pandas as pd

# リポジトリルートを import パスに追加（generate_html.py と同方式）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.lib.preprocess import build_target_lookup  # noqa: E402


def _row(部門コード, 部門名, 部門種別, 指標タイプ, 期間区分, 目標値, 病床数=None):
    return {
        "部門コード": 部門コード, "部門名": 部門名, "部門種別": 部門種別,
        "指標タイプ": 指標タイプ, "期間区分": 期間区分, "目標値": 目標値,
        "病床数": 病床数,
    }


def _hospital_rows_diverged():
    """病棟軸と診療科軸で値が異なる病院全体行（在院・新入院とも）。"""
    return [
        # 在院: 病棟軸（診療科軸と異なる値）
        _row("全体", "病院全体", "病棟", "日平均在院患者数", "全日", 999.0),
        _row("全体", "病院全体", "病棟", "日平均在院患者数", "平日", 1000.0),
        _row("全体", "病院全体", "病棟", "日平均在院患者数", "休日", 900.0),
        # 在院: 診療科軸（採用されるべき値）
        _row("全体", "病院全体", "診療科", "日平均在院患者数", "全日", 582.8),
        _row("全体", "病院全体", "診療科", "日平均在院患者数", "平日", 600.0),
        _row("全体", "病院全体", "診療科", "日平均在院患者数", "休日", 550.0),
        # 新入院: 病棟軸（診療科軸と異なる値）
        _row("全体", "病院全体", "病棟", "週間新入院患者数", "全日", 111.0),
        # 新入院: 診療科軸（採用されるべき値）
        _row("全体", "病院全体", "診療科", "週間新入院患者数", "全日", 379.2),
    ]


def _hospital_rows_same():
    """現行の実データ相当（病棟軸・診療科軸が同値）。"""
    return [
        _row("全体", "病院全体", "病棟", "日平均在院患者数", "全日", 582.8),
        _row("全体", "病院全体", "病棟", "日平均在院患者数", "平日", 600.0),
        _row("全体", "病院全体", "病棟", "日平均在院患者数", "休日", 550.0),
        _row("全体", "病院全体", "診療科", "日平均在院患者数", "全日", 582.8),
        _row("全体", "病院全体", "診療科", "日平均在院患者数", "平日", 600.0),
        _row("全体", "病院全体", "診療科", "日平均在院患者数", "休日", 550.0),
        _row("全体", "病院全体", "病棟", "週間新入院患者数", "全日", 379.2),
        _row("全体", "病院全体", "診療科", "週間新入院患者数", "全日", 379.2),
    ]


class TestTargetLookupAxis(unittest.TestCase):

    def test_dept_axis_adopted_when_axes_diverge(self):
        """病棟軸・診療科軸で値が異なる全体行 → 在院・新入院とも診療科軸の値が採用される。"""
        df = pd.DataFrame(_hospital_rows_diverged())
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            targets = build_target_lookup(df)

        self.assertEqual(
            targets["inpatient"]["hospital"],
            {"全日": 582.8, "平日": 600.0, "休日": 550.0},
        )
        self.assertEqual(targets["new_admission"]["hospital"], {"全日": 379.2})

    def test_warns_when_axes_diverge(self):
        """値が異なるとき警告が出る。"""
        df = pd.DataFrame(_hospital_rows_diverged())
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            build_target_lookup(df)

        msgs = [str(w.message) for w in caught]
        self.assertTrue(any("病棟軸と診療科軸で異なります" in m for m in msgs),
                         f"警告が出ていない: {msgs}")

    def test_falls_back_to_ward_axis_when_dept_axis_missing(self):
        """診療科軸の全体行が無いとき病棟軸へフォールバックする。"""
        rows = [
            _row("全体", "病院全体", "病棟", "日平均在院患者数", "全日", 582.8),
            _row("全体", "病院全体", "病棟", "日平均在院患者数", "平日", 600.0),
            _row("全体", "病院全体", "病棟", "日平均在院患者数", "休日", 550.0),
            _row("全体", "病院全体", "病棟", "週間新入院患者数", "全日", 379.2),
        ]
        df = pd.DataFrame(rows)
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # 診療科軸が無い場合は警告が出ないはず
            targets = build_target_lookup(df)

        self.assertEqual(
            targets["inpatient"]["hospital"],
            {"全日": 582.8, "平日": 600.0, "休日": 550.0},
        )
        self.assertEqual(targets["new_admission"]["hospital"], {"全日": 379.2})

    def test_no_regression_when_axes_match_current_data(self):
        """現行の実データ相当（両軸同値）→ 戻り値が現行実装と同じ（回帰なし）。"""
        df = pd.DataFrame(_hospital_rows_same())
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # 同値なら警告は出ないはず
            targets = build_target_lookup(df)

        self.assertEqual(
            targets["inpatient"]["hospital"],
            {"全日": 582.8, "平日": 600.0, "休日": 550.0},
        )
        self.assertEqual(targets["new_admission"]["hospital"], {"全日": 379.2})

    def test_new_admission_hospital_uses_zenjitsu_row_regardless_of_order(self):
        """新入院の全体行に 全日/平日/休日 があり平日が先頭でも、
        "全日"キーの値は行順に依存せず全日行の値になる。
        """
        rows = [
            _row("全体", "病院全体", "診療科", "週間新入院患者数", "平日", 420.0),
            _row("全体", "病院全体", "診療科", "週間新入院患者数", "休日", 300.0),
            _row("全体", "病院全体", "診療科", "週間新入院患者数", "全日", 379.2),
        ]
        df = pd.DataFrame(rows)
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # 病棟軸が無いので警告は出ないはず
            targets = build_target_lookup(df)

        self.assertEqual(targets["new_admission"]["hospital"], {"全日": 379.2})

    def test_new_admission_hospital_falls_back_when_no_zenjitsu_row(self):
        """全日行が無く平日行だけのとき、従来どおりフォールバックして例外にならない。"""
        rows = [
            _row("全体", "病院全体", "診療科", "週間新入院患者数", "平日", 420.0),
        ]
        df = pd.DataFrame(rows)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            targets = build_target_lookup(df)

        self.assertEqual(targets["new_admission"]["hospital"], {"全日": 420.0})


if __name__ == "__main__":
    unittest.main()
