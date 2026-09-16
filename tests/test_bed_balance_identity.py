"""
test_bed_balance_identity.py — 病床収支恒等式チェック（check_bed_balance）の回帰テスト

恒等式（日付ごとに病院全体で合計してから評価）:
    在院(t) − 在院(t−1) == 新入院患者数(t) + 転入患者数(t) − 退院合計(t) − 転出患者数(t)

将来エクスポート元が「緊急入院を入院の内数」に仕様変更した場合、
毎日ちょうど緊急入院数ぶん不足するので検知できる、というのが導入目的。

密閉テスト: data/ 配下の実データは一切読まない。合成 DataFrame のみで組む。
"""
import sys
import unittest
from pathlib import Path

import pandas as pd

# リポジトリルートを import パスに追加（generate_html.py と同方式）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.lib.validate import check_bed_balance, ValidationResult  # noqa: E402


def _row(d, census, new_adm, transfer_in, discharge, transfer_out):
    """病床収支チェックに必要な列だけを持つ1行分の辞書。"""
    return {
        "日付": pd.Timestamp(d),
        "在院患者数": census,
        "新入院患者数": new_adm,
        "転入患者数": transfer_in,
        "退院合計": discharge,
        "転出患者数": transfer_out,
    }


class TestBedBalanceIdentity(unittest.TestCase):

    def test_identity_holds_at_hospital_level_across_wards(self):
        """病棟単位では崩れていても、病院全体で合計すれば恒等式が成立するデータ
        → warn 0件・info で「M/M 日一致」が出る（日付ごとに全体合計してから評価する仕様の確認）。
        """
        rows = [
            # D0: 基準日（前日が無いため比較対象外）
            _row("2026-06-01", 10, 0, 0, 0, 0),  # 病棟A
            _row("2026-06-01", 15, 0, 0, 0, 0),  # 病棟B
            # D1: 病棟Aは単独では+1過大（実診療報告=+3だが新入院を4と誤登録）、
            #     病棟Bは単独では-1過小（-4と誤登録）だが、合計では相殺され一致する
            _row("2026-06-02", 13, 4, 0, 0, 0),  # 病棟A: diff=3, expected=4（単独ではズレ）
            _row("2026-06-02", 12, 0, 0, 4, 0),  # 病棟B: diff=-3, expected=-4（単独ではズレ）
            # D2: 全体で普通に一致する日
            _row("2026-06-03", 14, 1, 0, 0, 0),  # 病棟A: diff=1, expected=1
            _row("2026-06-03", 12, 0, 0, 0, 0),  # 病棟B: diff=0, expected=0
        ]
        adm = pd.DataFrame(rows)

        result = check_bed_balance(adm)

        self.assertEqual(result.warnings, [])
        self.assertEqual(len(result.errors), 0)
        self.assertTrue(any("2/2 日一致" in msg for msg in result.infos))

    def test_detects_missing_emergency_admission_shortfall(self):
        """「緊急入院が入院の内数」を模した崩れたデータ（毎日 緊急入院数ぶん不足）
        → warn が立ち、不一致日数・最大乖離が正しく報告される。
        """
        emg = 3  # 毎日この人数ぶん新入院患者数が不足する（緊急入院が入院の内数扱いになるケース）
        true_daily_growth = 10  # 実際の在院増（緊急入院込み・退院転出なし）
        recorded_new_adm = true_daily_growth - emg  # 新入院患者数として記録される値（不足）
        rows = [
            _row("2026-06-01", 100, 0, 0, 0, 0),  # D0: 基準日
            _row("2026-06-02", 110, recorded_new_adm, 0, 0, 0),
            _row("2026-06-03", 120, recorded_new_adm, 0, 0, 0),
            _row("2026-06-04", 130, recorded_new_adm, 0, 0, 0),
        ]
        adm = pd.DataFrame(rows)

        result = check_bed_balance(adm)

        self.assertEqual(len(result.errors), 0)
        self.assertEqual(len(result.warnings), 1)
        msg = result.warnings[0]
        self.assertIn("不一致 3/3 日", msg)
        self.assertIn(f"最大乖離 {emg} 人", msg)

    def test_missing_required_columns_warns_without_raising(self):
        """必要列（転出患者数など）が欠けたデータ → warn して return（例外を投げない）。"""
        adm = pd.DataFrame([
            {"日付": pd.Timestamp("2026-06-01"), "在院患者数": 10,
             "新入院患者数": 1, "転入患者数": 0, "退院合計": 0},
            # 転出患者数 列が無い
        ])

        try:
            result = check_bed_balance(adm)
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外を投げてはいけない: {e}")

        self.assertEqual(len(result.errors), 0)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("必須列がありません", result.warnings[0])

    def test_single_day_data_does_not_raise(self):
        """1日分だけのデータ（比較対象が無い）→ 例外を投げない。"""
        adm = pd.DataFrame([_row("2026-06-01", 100, 5, 0, 3, 0)])

        try:
            result = check_bed_balance(adm)
        except Exception as e:  # noqa: BLE001
            self.fail(f"例外を投げてはいけない: {e}")

        self.assertEqual(len(result.errors), 0)

    def test_sporadic_mismatch_below_threshold_is_info_not_warn(self):
        """散発的な不一致（十分な日数のうち1日だけ）→ warn は0件、info に「散発的」が入る。
        日跨ぎの計上タイミング差を模し、1日だけ新入院が実際より1人少なく記録される。
        """
        n_days = 40
        base = pd.Timestamp("2026-01-01")
        rows = [_row(base + pd.Timedelta(days=i), 100 + i, 1, 0, 0, 0)
                for i in range(n_days)]
        mismatch_idx = n_days // 2
        rows[mismatch_idx] = _row(
            rows[mismatch_idx]["日付"], rows[mismatch_idx]["在院患者数"], 0, 0, 0, 0)
        adm = pd.DataFrame(rows)

        result = check_bed_balance(adm)

        self.assertEqual(len(result.warnings), 0)
        self.assertTrue(any("散発的" in msg for msg in result.infos))
        self.assertTrue(any(f"不一致 1/{n_days - 1} 日" in msg for msg in result.infos))

    def test_systematic_mismatch_at_or_above_threshold_is_warn(self):
        """全日が一定量不足する系統的な崩れ → warn が1件で「系統的」が入る。"""
        n_days = 10
        base = pd.Timestamp("2026-02-01")
        # 在院は+2/日で増えるが、新入院は1人分しか記録されない（毎日不足）
        rows = [_row(base + pd.Timedelta(days=i), 100 + i * 2, 1, 0, 0, 0)
                for i in range(n_days)]
        adm = pd.DataFrame(rows)

        result = check_bed_balance(adm)

        self.assertEqual(len(result.warnings), 1)
        self.assertIn("系統的", result.warnings[0])
        self.assertIn(f"不一致 {n_days - 1}/{n_days - 1} 日", result.warnings[0])

    def test_threshold_boundary_switches_branch(self):
        """不一致率がちょうど閾値(10%)前後で分岐が切り替わることを確認する。"""
        base = pd.Timestamp("2026-03-01")

        # ちょうど10%（=1/10）→ 閾値以上なので warn（系統的）
        rows_at = [_row(base + pd.Timedelta(days=i), 100 + i, 1, 0, 0, 0)
                   for i in range(11)]
        rows_at[5] = _row(rows_at[5]["日付"], rows_at[5]["在院患者数"], 0, 0, 0, 0)
        result_at = check_bed_balance(pd.DataFrame(rows_at))

        self.assertEqual(len(result_at.warnings), 1)
        self.assertIn("系統的", result_at.warnings[0])
        self.assertIn("不一致 1/10 日", result_at.warnings[0])

        # 10%未満（1/11 ≈ 9.1%）→ 閾値未満なので info（散発的）
        rows_below = [_row(base + pd.Timedelta(days=i), 100 + i, 1, 0, 0, 0)
                      for i in range(12)]
        rows_below[5] = _row(rows_below[5]["日付"], rows_below[5]["在院患者数"], 0, 0, 0, 0)
        result_below = check_bed_balance(pd.DataFrame(rows_below))

        self.assertEqual(len(result_below.warnings), 0)
        self.assertTrue(any("散発的" in msg for msg in result_below.infos))
        self.assertTrue(any("不一致 1/11 日" in msg for msg in result_below.infos))

    def test_result_accumulates_into_existing_validation_result(self):
        """既存の ValidationResult を渡した場合、そこに追記される（check_admission からの呼び出し想定）。"""
        rows = [
            _row("2026-06-01", 10, 0, 0, 0, 0),
            _row("2026-06-02", 11, 1, 0, 0, 0),
        ]
        adm = pd.DataFrame(rows)
        result = ValidationResult()
        result.info("既存の情報")

        out = check_bed_balance(adm, result)

        self.assertIs(out, result)
        self.assertTrue(any("既存の情報" in msg for msg in result.infos))
        self.assertTrue(any("1/1 日一致" in msg for msg in result.infos))


if __name__ == "__main__":
    unittest.main()
