"""generate_html.py へのナラティブ生成キャッシュ配線のテスト。

対象:
  - 基準日→キャッシュパス解決と種探索ロジック
    （_resolve_narr_cache_seed / _find_narr_cache_seed / _count_narr_cache_entries）
  - load 後は app.lib.ai_narrative._NARR_CACHE_ENABLED が真になり、キー一致プロンプト
    では _generate_checked が chat_json（ライブLLM呼び出し）に至らないこと
    （境界は narrate_leveling_actions ではなく _generate_checked。
    test_ai_narrative_parallel.py と同じ「chat_json はテストしない」流儀）

LLM・常駐サーバは一切呼ばない（chat_json はモックで差し替える）。

実行: リポジトリルートで
    .venv/bin/python3 -m pytest tests/ -q
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import generate_html as gh  # noqa: E402
from app.lib import ai_narrative as an  # noqa: E402


class TestResolveNarrCacheSeed(unittest.TestCase):
    """_resolve_narr_cache_seed: 基準日ファイルあり/過去ファイルのみ/空 の3ケース。"""

    def _mkfiles(self, d, names):
        for name in names:
            (Path(d) / name).write_text("{}", encoding="utf-8")

    def test_base_date_file_exists_is_used_as_is(self):
        with tempfile.TemporaryDirectory() as d:
            self._mkfiles(d, [
                "narrative_cache_2026-08-27.json",
                "narrative_cache_2026-08-20.json",
            ])
            base_date = datetime(2026, 8, 27)
            path, seed = gh._resolve_narr_cache_seed(Path(d), base_date)
            self.assertEqual(path, Path(d) / "narrative_cache_2026-08-27.json")
            self.assertEqual(seed, path)  # 過去ファイルへは引き継がない

    def test_seeds_from_latest_past_file_when_base_missing(self):
        with tempfile.TemporaryDirectory() as d:
            self._mkfiles(d, [
                "narrative_cache_2026-08-13.json",
                "narrative_cache_2026-08-20.json",
            ])
            base_date = datetime(2026, 8, 27)
            path, seed = gh._resolve_narr_cache_seed(Path(d), base_date)
            self.assertEqual(path, Path(d) / "narrative_cache_2026-08-27.json")
            self.assertFalse(path.is_file())
            self.assertEqual(seed, Path(d) / "narrative_cache_2026-08-20.json")

    def test_no_candidates_falls_back_to_base_date_path(self):
        with tempfile.TemporaryDirectory() as d:
            base_date = datetime(2026, 8, 27)
            path, seed = gh._resolve_narr_cache_seed(Path(d), base_date)
            self.assertEqual(path, Path(d) / "narrative_cache_2026-08-27.json")
            self.assertEqual(seed, path)
            self.assertFalse(seed.is_file())

    def test_missing_state_dir_falls_back_to_base_date_path(self):
        missing = Path("/nonexistent/_state_dir_for_generate_html_test")
        base_date = datetime(2026, 8, 27)
        path, seed = gh._resolve_narr_cache_seed(missing, base_date)
        self.assertEqual(path, missing / "narrative_cache_2026-08-27.json")
        self.assertEqual(seed, path)

    def test_future_files_never_selected(self):
        with tempfile.TemporaryDirectory() as d:
            self._mkfiles(d, [
                "narrative_cache_2026-08-13.json",
                "narrative_cache_2026-09-03.json",  # base_date より後
            ])
            base_date = datetime(2026, 8, 27)
            _, seed = gh._resolve_narr_cache_seed(Path(d), base_date)
            self.assertEqual(seed, Path(d) / "narrative_cache_2026-08-13.json")

    def test_malformed_filenames_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            self._mkfiles(d, [
                "narrative_cache_2026-08-20.json",
                "narrative_cache_not-a-date.json",
                "narrative_cache_.json",
                "other_file.json",
            ])
            base_date = datetime(2026, 8, 27)
            _, seed = gh._resolve_narr_cache_seed(Path(d), base_date)
            self.assertEqual(seed, Path(d) / "narrative_cache_2026-08-20.json")


class TestCountNarrCacheEntries(unittest.TestCase):
    def test_missing_file_is_zero(self):
        self.assertEqual(
            gh._count_narr_cache_entries(Path("/nonexistent/_x_for_test.json")), 0)

    def test_counts_top_level_keys(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text(json.dumps({"a": {}, "b": {}, "c": {}}), encoding="utf-8")
            self.assertEqual(gh._count_narr_cache_entries(p), 3)

    def test_malformed_json_is_zero(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text("not json", encoding="utf-8")
            self.assertEqual(gh._count_narr_cache_entries(p), 0)


class TestCacheLoadPreventsLiveLLMCall(unittest.TestCase):
    """generate_html.py が呼ぶのと同じ load_narrative_cache を通した後、キー一致
    プロンプトが _generate_checked（narrate_leveling_actions の内部境界）で
    ライブ chat_json 呼び出しに至らないことを確認する。"""

    def setUp(self):
        # _NARR_CACHE 系グローバル状態を退避（テストが他テストへ波及しないように）
        self._orig_cache = an._NARR_CACHE
        self._orig_enabled = an._NARR_CACHE_ENABLED
        self._orig_stats = dict(an._NARR_CACHE_STATS)

    def tearDown(self):
        an._NARR_CACHE = self._orig_cache
        an._NARR_CACHE_ENABLED = self._orig_enabled
        an._NARR_CACHE_STATS.clear()
        an._NARR_CACHE_STATS.update(self._orig_stats)
        an.reset_reject_stats()

    def test_cache_hit_skips_live_chat_json_call(self):
        system = "system-prompt-fixture"
        user = "user-prompt-fixture"
        banned = ("延伸",)
        allow = ()
        model = an.DEFAULT_MODEL
        # _generate_checked は allow に _ALLOW_FACT_PHRASES を足してからキーを引く
        full_allow = tuple(allow) + an._ALLOW_FACT_PHRASES
        key = an._cache_key(system, user, banned, full_allow, model)

        with tempfile.TemporaryDirectory() as d:
            cache_path = Path(d) / "narrative_cache_2026-08-27.json"
            cache_path.write_text(
                json.dumps({key: {"body": "既存の本文です。", "action": "既存のアクションです。"}}),
                encoding="utf-8")

            # generate_html.py の load 配線と同一の関数・同一の呼び方
            from app.lib.ai_narrative import load_narrative_cache
            load_narrative_cache(cache_path)
            self.assertTrue(an._NARR_CACHE_ENABLED)

            with mock.patch.object(an, "chat_json") as mocked_chat_json:
                result = an._generate_checked(
                    "tag-fixture", system=system, user=user, banned=banned,
                    allow=allow, model=model, quiet=True)

            mocked_chat_json.assert_not_called()
            self.assertEqual(result["body"], "既存の本文です。")
            self.assertEqual(result["action"], "既存のアクションです。")
            self.assertEqual(result["src"], "ai")

    def test_cache_miss_still_calls_live_path(self):
        """対照実験: 同じ load 後でもキー不一致（未知プロンプト）なら chat_json 境界へ
        到達すること（キャッシュが常にヒットへ縮退する壊れ方をしていないことの確認）。"""
        with tempfile.TemporaryDirectory() as d:
            cache_path = Path(d) / "narrative_cache_2026-08-27.json"
            cache_path.write_text(json.dumps({}), encoding="utf-8")

            from app.lib.ai_narrative import load_narrative_cache
            load_narrative_cache(cache_path)
            self.assertTrue(an._NARR_CACHE_ENABLED)

            with mock.patch.object(an, "chat_json") as mocked_chat_json:
                mocked_chat_json.side_effect = RuntimeError("live call blocked in test")
                result = an._generate_checked(
                    "tag-fixture-miss", system="unseen-system", user="unseen-user",
                    banned=(), allow=(), model=an.DEFAULT_MODEL, quiet=True)

            self.assertTrue(mocked_chat_json.called)
            self.assertIsNone(result)  # 例外はインフラ起因として fail-soft で None


if __name__ == "__main__":
    unittest.main()
