"""P2 極小窓ガード（avg=None／営業日0窓）の下流表示が安全であることを固定する回帰テスト。

`dept_report.py` は並行編集中のため触らないが、`_surgery_gap_score` 等の
既存の None ガードが安全であることは読み取り専用のテストとして固定しておく
（暦補正と学習ループ改修プラン.md §2「avg=Noneの下流表示も同時に点検」）。

対象:
  - config.status_label / status_display : achievement=None → "neutral"（危険側に倒れない）
  - metrics.achievement_rate              : target=None → None（0除算・誤達成表示なし）
  - dept_report._surgery_gap_score        : sv=None → 0.0（トピックスコアに悪影響を与えない）
  - dept_report._admission_gap_score      : na=None → 0.0（同上・対称であることの確認）

実行: リポジトリルートで
    .venv/bin/python -m pytest -q tests/test_p2_downstream_none_safety.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib import config as cfg  # noqa: E402
from app.lib import metrics as mt  # noqa: E402
from app.lib import dept_report as dr  # noqa: E402


class StatusLabelNoneSafetyTest(unittest.TestCase):
    def test_status_label_none_is_neutral(self):
        self.assertEqual(cfg.status_label(None), "neutral")

    def test_status_display_none_is_neutral_and_has_safe_defaults(self):
        disp = cfg.status_display(None)
        self.assertEqual(disp["css"], "mu")
        self.assertEqual(disp["text"], "—")


class AchievementRateNoneSafetyTest(unittest.TestCase):
    def test_none_target_returns_none(self):
        self.assertIsNone(mt.achievement_rate(100, None))

    def test_zero_target_returns_none(self):
        self.assertIsNone(mt.achievement_rate(100, 0))


class GapScoreNoneSafetyTest(unittest.TestCase):
    """極小窓ガードで avg/actual が None になった場合でも、トピックスコアが
    例外を出さず 0.0（=非対象）に縮退すること。"""

    def test_surgery_gap_score_none_value_is_zero(self):
        self.assertEqual(dr._surgery_gap_score(None, 21.0), 0.0)

    def test_surgery_gap_score_none_target_is_zero(self):
        self.assertEqual(dr._surgery_gap_score(18.0, None), 0.0)

    def test_admission_gap_score_none_value_is_zero(self):
        self.assertEqual(dr._admission_gap_score(None, 20.0), 0.0)

    def test_admission_gap_score_none_target_is_zero(self):
        self.assertEqual(dr._admission_gap_score(16.0, None), 0.0)


if __name__ == "__main__":
    unittest.main()
