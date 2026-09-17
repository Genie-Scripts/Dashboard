"""§9 #13是正: build_comedix_card.load_profit_headline の hybrid 二重計算排除の
回帰テスト（標準ライブラリ unittest・追加依存なし）。

背景（spec/改修プラン_粗利の単位あたり指標.md §9 #13）: 以前は
load_profit_headline が build_profit_hybrid_calibrated を内部で計算した上で
ph（粗利ヘッドライン payload）だけを返し、build_hospital_report.py が
build_profit_unit_payload 用の meta/hospital_series を得るために同じ
build_profit_hybrid_calibrated をもう一度呼んでいた（二重計算）。
改修後は load_profit_headline が (ph, hybrid) のタプルを返し、hybrid
（build_profit_hybrid_calibrated の戻り値そのもの）を呼び出し側が使い回せる。

密閉（実データ・常駐サーバは使わない）。対象:
  - scripts.build_comedix_card.load_profit_headline

実行: リポジトリルートで
    python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.build_comedix_card import load_profit_headline  # noqa: E402

BASE_DATE = pd.Timestamp("2026-09-13")
FAKE_SECTION = {
    "meta": {"latest_final_nyuin": 60.0, "latest_final_gairai": 25.0,
             "latest_final_total": 85.0, "window_end": "2026-09-13"},
    "hospital_series": {"dates": [], "cur": [], "prev": []},
}
FAKE_G_MILLION = 85.0


def _empty_adm():
    return pd.DataFrame({
        "日付": pd.Series([], dtype="datetime64[ns]"),
        "診療科名": pd.Series([], dtype=str),
        "在院患者数": pd.Series([], dtype=float),
        "新入院患者数": pd.Series([], dtype=float),
        "科_表示": pd.Series([], dtype=bool),
    })


def _empty_surg():
    return pd.DataFrame({
        "手術実施日": pd.Series([], dtype="datetime64[ns]"),
        "実施診療科": pd.Series([], dtype=str),
        "全麻": pd.Series([], dtype=bool),
        "術数対象": pd.Series([], dtype=bool),
    })


class LoadProfitHeadlineHybridCallCountTest(unittest.TestCase):
    """build_profit_hybrid_calibrated が1回だけ呼ばれ、その戻り値がそのまま
    hybrid として呼び出し側へ返ること（build_hospital_report.py 側の再計算不要）。"""

    def _counting_hybrid(self, section=FAKE_SECTION, g=FAKE_G_MILLION):
        calls = {"n": 0}

        def _wrapper(*args, **kwargs):
            calls["n"] += 1
            return (section, g)
        return _wrapper, calls

    def test_return_shape_and_single_call(self):
        wrapper, calls = self._counting_hybrid()
        # data_dir を実在しないパスにして load_profit_targets_breakdown を
        # fail-soft側（folder.exists()==False → None）で早期に無害化する。
        with mock.patch("app.lib.html_builder.build_profit_hybrid_calibrated", wrapper):
            ph, hybrid = load_profit_headline(
                "/nonexistent/data_dir", _empty_adm(), _empty_surg(),
                profit_monthly=None, profit_breakdown=None, base_date=BASE_DATE)

        self.assertEqual(calls["n"], 1)
        self.assertEqual(hybrid, (FAKE_SECTION, FAKE_G_MILLION))
        # ph は build_profit_headline の戻り値そのもの（fail-soft で None もあり得るが、
        # ここでの主眼は hybrid の二重計算排除＝1回だけ呼ばれること）。
        self.assertTrue(ph is None or isinstance(ph, dict))

    def test_hybrid_calc_failure_still_returns_none_none_and_ph_none(self):
        def _raising(*args, **kwargs):
            raise RuntimeError("boom")

        with mock.patch("app.lib.html_builder.build_profit_hybrid_calibrated", _raising):
            ph, hybrid = load_profit_headline(
                "/nonexistent/data_dir", _empty_adm(), _empty_surg(),
                profit_monthly=None, profit_breakdown=None, base_date=BASE_DATE)

        self.assertIsNone(ph)
        self.assertEqual(hybrid, (None, None))


if __name__ == "__main__":
    unittest.main()
