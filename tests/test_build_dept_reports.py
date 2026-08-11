"""build_dept_reports の生成キャッシュ引き継ぎ（find_narr_cache_seed）のユニットテスト。

決定論の担保が LLM の seed から生成キャッシュへ移ったことに伴い、基準日のキャッシュが
無い場合は日付をまたいで直近の過去キャッシュを種として読み込む。その選択ロジックのみを
対象とする（実際の読み込み/書き出しは app.lib.ai_narrative 側・実データビルドで検証する）。

実行: リポジトリルートで
    python -m pytest tests/ -q
"""
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.build_dept_reports import _pin_base_date, find_narr_cache_seed


class TestFindNarrCacheSeed(unittest.TestCase):
    def _mkfiles(self, d, names):
        for name in names:
            (Path(d) / name).write_text("{}", encoding="utf-8")

    def test_base_date_file_exists_is_not_seeded(self):
        # 基準日のファイルが存在する場合、呼び出し側は find_narr_cache_seed を
        # 呼ばずにそれをそのまま使う想定だが、仮に呼ばれても exclude 指定で
        # 過去ファイルの方は選ばれない（=引き継ぎは起きない）ことを確認する。
        with tempfile.TemporaryDirectory() as d:
            self._mkfiles(d, [
                "narrative_cache_2026-07-19.json",
                "narrative_cache_2026-07-12.json",
            ])
            base_date = datetime(2026, 7, 19)
            exclude = Path(d) / "narrative_cache_2026-07-19.json"
            seed = find_narr_cache_seed(Path(d), base_date, exclude)
            self.assertEqual(seed, Path(d) / "narrative_cache_2026-07-12.json")

    def test_seeds_from_latest_past_file_when_base_missing(self):
        with tempfile.TemporaryDirectory() as d:
            self._mkfiles(d, [
                "narrative_cache_2026-07-05.json",
                "narrative_cache_2026-07-12.json",
            ])
            base_date = datetime(2026, 7, 19)
            exclude = Path(d) / "narrative_cache_2026-07-19.json"  # 存在しない
            seed = find_narr_cache_seed(Path(d), base_date, exclude)
            self.assertEqual(seed, Path(d) / "narrative_cache_2026-07-12.json")

    def test_future_files_never_selected(self):
        with tempfile.TemporaryDirectory() as d:
            self._mkfiles(d, [
                "narrative_cache_2026-07-05.json",
                "narrative_cache_2026-07-26.json",  # base_date より後
            ])
            base_date = datetime(2026, 7, 19)
            exclude = Path(d) / "narrative_cache_2026-07-19.json"
            seed = find_narr_cache_seed(Path(d), base_date, exclude)
            self.assertEqual(seed, Path(d) / "narrative_cache_2026-07-05.json")

    def test_no_candidates_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            base_date = datetime(2026, 7, 19)
            exclude = Path(d) / "narrative_cache_2026-07-19.json"
            self.assertIsNone(find_narr_cache_seed(Path(d), base_date, exclude))

    def test_missing_state_dir_returns_none(self):
        base_date = datetime(2026, 7, 19)
        missing = Path("/nonexistent/_state_dir_for_test")
        seed = find_narr_cache_seed(missing, base_date, missing / "narrative_cache_2026-07-19.json")
        self.assertIsNone(seed)

    def test_malformed_filenames_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            self._mkfiles(d, [
                "narrative_cache_2026-07-12.json",
                "narrative_cache_not-a-date.json",
                "narrative_cache_.json",
                "other_file.json",
            ])
            base_date = datetime(2026, 7, 19)
            exclude = Path(d) / "narrative_cache_2026-07-19.json"
            seed = find_narr_cache_seed(Path(d), base_date, exclude)
            self.assertEqual(seed, Path(d) / "narrative_cache_2026-07-12.json")


class TestPinBaseDate(unittest.TestCase):
    """§6-1 基準日ピン留め: レビューUIの「PDF再作成」がレビュー開始時の基準日で回るように。"""

    def test_appends_when_unspecified(self):
        self.assertEqual(
            _pin_base_date(["--no-ai", "--keep-html"], "2026-05-31"),
            ["--no-ai", "--keep-html", "--base-date", "2026-05-31"])

    def test_noop_when_already_specified_space_form(self):
        argv = ["--base-date", "2026-05-31", "--no-ai"]
        self.assertEqual(_pin_base_date(argv, "2026-05-31"), argv)

    def test_noop_when_already_specified_equals_form(self):
        argv = ["--base-date=2026-05-31", "--no-ai"]
        self.assertEqual(_pin_base_date(argv, "2026-05-31"), argv)


if __name__ == "__main__":
    unittest.main()
