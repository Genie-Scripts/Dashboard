"""
test_data_loader.py — 入院データのマージ（重複除去）ロジック

看護師の手入力に由来する「ファイル内の正当な同一行（分割入力）」を保持しつつ、
「ファイル間の真の重複（日付範囲の重なり）」のみを除去することを検証する。
"""
import sys
import unittest
from pathlib import Path

import pandas as pd

# リポジトリルートを import パスに追加（generate_html.py と同方式）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.lib.data_loader import _merge_admission_files  # noqa: E402


def _row(d, ward, dept, census, **extra):
    base = {
        "日付": pd.Timestamp(d), "病棟コード": ward, "診療科名": dept,
        "在院患者数": census, "入院患者数": 0, "緊急入院患者数": 0,
        "転入患者数": 0, "退院患者数": 0, "転出患者数": 0, "死亡患者数": 0,
    }
    base.update(extra)
    return base


class TestMergeAdmissionFiles(unittest.TestCase):
    D1 = "2026-06-11"
    D0 = "2026-04-07"  # ファイル間で重なる日付

    def test_single_frame_passthrough(self):
        """1フレームならそのまま返す（重複除去しない＝手入力の多重行を保持）。"""
        a = pd.DataFrame([_row(self.D1, "04C", "整形外科", 1),
                          _row(self.D1, "04C", "整形外科", 1)])
        out = _merge_admission_files([a])
        self.assertEqual(len(out), 2)
        self.assertEqual(int(out["在院患者数"].sum()), 2)

    def test_intra_file_identical_rows_preserved(self):
        """ファイル内の完全一致する正当な複数行は両方保持される（合算される）。"""
        recent = pd.DataFrame([
            _row(self.D1, "04C", "整形外科", 1),   # 患者A
            _row(self.D1, "04C", "整形外科", 1),   # 患者B（全列一致だが別行＝正当）
            _row(self.D1, "05A", "内科", 10),
        ])
        year = pd.DataFrame([
            _row(self.D0, "03A", "外科", 5),       # 重なり外の日付
        ])
        out = _merge_admission_files([year, recent])

        orth = out[(out["病棟コード"] == "04C") & (out["診療科名"] == "整形外科")]
        self.assertEqual(len(orth), 2, "04C整形外科の正当な2行が保持されること")
        self.assertEqual(int(orth["在院患者数"].sum()), 2)
        # 6/11 全体は 1+1+10 = 12（過少計上が起きない）
        d1 = out[out["日付"] == pd.Timestamp(self.D1)]
        self.assertEqual(int(d1["在院患者数"].sum()), 12)

    def test_cross_file_true_duplicate_removed(self):
        """ファイル間の真の重複（同一行が両ファイルに存在）は1つに集約される。"""
        year = pd.DataFrame([
            _row(self.D0, "05A", "内科", 10),
            _row(self.D0, "06B", "外科", 7),
        ])
        recent = pd.DataFrame([
            _row(self.D0, "05A", "内科", 10),   # year と完全一致（重なり日の二重出力）
            _row(self.D0, "06B", "外科", 7),    # 同上
        ])
        out = _merge_admission_files([year, recent])
        d0 = out[out["日付"] == pd.Timestamp(self.D0)]
        # 二重計上されず 10+7 = 17、行数も2
        self.assertEqual(len(d0), 2)
        self.assertEqual(int(d0["在院患者数"].sum()), 17)

    def test_cross_file_duplicate_but_intra_file_legit_both_handled(self):
        """重なり日に、ファイル間の真の重複とファイル内の正当な多重行が混在しても、
        各ファイルが同じ多重度を持つなら多重度が保たれる（二重化も過少化もしない）。"""
        # 両ファイルとも 04C整形外科=1 を2行ずつ持つ（重なり日の二重出力で多重度も一致）
        rows = [_row(self.D0, "04C", "整形外科", 1),
                _row(self.D0, "04C", "整形外科", 1)]
        year = pd.DataFrame(rows)
        recent = pd.DataFrame(rows)
        out = _merge_admission_files([year, recent])
        orth = out[(out["病棟コード"] == "04C") & (out["診療科名"] == "整形外科")]
        # 真の重複ぶんは集約され、正当な多重度2は保たれる
        self.assertEqual(len(orth), 2)
        self.assertEqual(int(orth["在院患者数"].sum()), 2)


if __name__ == "__main__":
    unittest.main()
