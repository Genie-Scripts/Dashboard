"""ai_narrative.py の _state ファイル書込みアトミック化のテスト（D1）。

正本: spec/暦補正と学習ループ改修プラン.md §5.1 Track D。監査で確定した事実
（save_narrative_cache は排他なし・非アトミックの一発上書きで、make系(generate_html.py)と
reports系(build_dept_reports.py)が同一ファイルを無調停共有→lost-update と
「書込み窓read→壊れJSON→fail-soft空→全損」シナリオがある）の是正を検証する。

対象:
  - _atomic_write_json: 正常書込み・tmp残骸なし・例外時のtmp掃除
  - save_narrative_cache: ディスクとメモリのマージ保持（lost-update根治）・
    壊れJSONからのfail-soft復旧・件数急減ガードの発火
  - REJECT_STATS: キャッシュヒットが「棄却」として二重計上されないこと（D0是正）

LLM・常駐サーバは一切呼ばない（chat_json はモックで差し替える。ファイルI/Oはすべて
tempfile 配下）。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib import ai_narrative as an  # noqa: E402


class _NarrCacheStateMixin:
    """_NARR_CACHE 系 + REJECT_STATS のグローバル状態を退避/復元する共通土台
    （test_generate_html_narrative_cache.py の TestCacheLoadPreventsLiveLLMCall と
    同じ流儀）。"""

    def setUp(self):
        self._orig_cache = an._NARR_CACHE
        self._orig_enabled = an._NARR_CACHE_ENABLED
        self._orig_stats = dict(an._NARR_CACHE_STATS)
        self._orig_reject_stats = dict(an.REJECT_STATS)

    def tearDown(self):
        an._NARR_CACHE = self._orig_cache
        an._NARR_CACHE_ENABLED = self._orig_enabled
        an._NARR_CACHE_STATS.clear()
        an._NARR_CACHE_STATS.update(self._orig_stats)
        an.REJECT_STATS.clear()
        an.REJECT_STATS.update(self._orig_reject_stats)


class TestAtomicWriteJson(unittest.TestCase):
    """_atomic_write_json: 正常書込み・tmp残骸なし・例外時のtmp掃除。"""

    def test_normal_write_round_trips_content(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "out.json"
            an._atomic_write_json(path, {"a": 1, "b": {"c": 2}})
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"a": 1, "b": {"c": 2}})

    def test_no_tmp_leftover_after_success(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "out.json"
            an._atomic_write_json(path, {"a": 1})
            leftovers = list(Path(d).glob("out.json.tmp.*"))
            self.assertEqual(leftovers, [])
            # ディレクトリに存在するのは最終ファイルのみ
            self.assertEqual([p.name for p in Path(d).iterdir()], ["out.json"])

    def test_indent_kwarg_is_honored(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "out.json"
            an._atomic_write_json(path, {"a": 1}, indent=1)
            self.assertIn("\n", path.read_text(encoding="utf-8"))

    def test_exception_during_dump_cleans_up_tmp_and_reraises(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "out.json"
            # set は json シリアライズ不可＝ json.dump が TypeError を送出する
            with self.assertRaises(TypeError):
                an._atomic_write_json(path, {"bad": {1, 2, 3}})
            self.assertFalse(path.exists())  # os.replace まで到達していない
            leftovers = list(Path(d).glob("out.json.tmp.*"))
            self.assertEqual(leftovers, [])  # tmp は例外時に確実に消される


class TestSaveNarrativeCacheMergeRetention(_NarrCacheStateMixin, unittest.TestCase):
    """save のマージ保持: load→ディスクに別プロセス相当の追記→save→両方残ること。"""

    def test_concurrent_disk_addition_and_own_new_entry_both_survive(self):
        with tempfile.TemporaryDirectory() as d:
            cache_path = Path(d) / "narrative_cache_2026-08-29.json"
            cache_path.write_text(
                json.dumps({"keyA": {"body": "Aの本文", "action": "Aのアクション"}}),
                encoding="utf-8")

            an.load_narrative_cache(cache_path)
            self.assertEqual(set(an._NARR_CACHE), {"keyA"})

            # 別プロセス相当: 我々の load 後にディスク側だけへ keyB が追記された状態を模する
            cache_path.write_text(
                json.dumps({
                    "keyA": {"body": "Aの本文", "action": "Aのアクション"},
                    "keyB": {"body": "Bの本文", "action": "Bのアクション"},
                }), encoding="utf-8")

            # 自プロセスは keyA を知ったまま新規に keyC を生成した状態を模する
            an._NARR_CACHE["keyC"] = {"body": "Cの本文", "action": "Cのアクション"}

            an.save_narrative_cache(cache_path)

            saved = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(set(saved), {"keyA", "keyB", "keyC"})
            self.assertEqual(saved["keyB"]["body"], "Bの本文")  # 素朴な上書きなら消えていた
            self.assertEqual(saved["keyC"]["body"], "Cの本文")


class TestSaveNarrativeCacheCorruptedDisk(_NarrCacheStateMixin, unittest.TestCase):
    """ディスク壊れJSONでも save 成功（in-memory 全量で復旧書き込み）。"""

    def test_corrupted_disk_json_does_not_raise_and_recovers_from_memory(self):
        with tempfile.TemporaryDirectory() as d:
            an.load_narrative_cache(Path(d) / "does-not-exist.json")  # fail-soft有効化
            an._NARR_CACHE["keyX"] = {"body": "Xの本文", "action": "Xのアクション"}

            cache_path = Path(d) / "narrative_cache_2026-08-29.json"
            cache_path.write_text("{not valid json,,,", encoding="utf-8")

            an.save_narrative_cache(cache_path)  # 例外を投げないこと

            saved = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(saved, {"keyX": {"body": "Xの本文", "action": "Xのアクション"}})


class TestSaveNarrativeCacheShrinkGuard(_NarrCacheStateMixin, unittest.TestCase):
    """急減ガード: マージ後件数がディスクを下回るなら書かずに警告のみ。

    {**disk, **memory} という和集合の性質上、正しい実装では
    len(merged) < len(disk) は構造上起きない（退行検知用ガード）。実際に発火させる
    には _merge_cache_dicts をモックして「マージ実装が壊れた」状態を人工的に作る
    （ディスク内容だけを大きく見せても和集合の性質上ガードは発火しないため）。
    """

    def test_shrunk_merge_result_aborts_write_and_warns(self):
        with tempfile.TemporaryDirectory() as d:
            cache_path = Path(d) / "narrative_cache_2026-08-29.json"
            original = {
                "keyA": {"body": "A", "action": "a"},
                "keyB": {"body": "B", "action": "b"},
                "keyC": {"body": "C", "action": "c"},
            }
            cache_path.write_text(json.dumps(original), encoding="utf-8")

            an.load_narrative_cache(Path(d) / "does-not-exist.json")
            an._NARR_CACHE["keyD"] = {"body": "D", "action": "d"}

            with mock.patch.object(an, "_merge_cache_dicts",
                                   return_value={"only-one": {}}), \
                 mock.patch.object(an.logger, "warning") as warn:
                an.save_narrative_cache(cache_path)

            warn.assert_called_once()
            # ディスク内容は書き換えられていない(元のまま)
            self.assertEqual(json.loads(cache_path.read_text(encoding="utf-8")), original)


class TestRejectStatsCacheHitNotDoubleCounted(_NarrCacheStateMixin, unittest.TestCase):
    """D0: キャッシュヒットは REJECT_STATS["cache"] に計上しない
    （_NARR_CACHE_STATS["hit"] のみで計上する）。"""

    def test_cache_hit_does_not_increment_reject_stats_cache(self):
        an.reset_reject_stats()
        system, user, banned, allow, model = (
            "system-prompt-fixture", "user-prompt-fixture", ("延伸",), (), an.DEFAULT_MODEL)
        full_allow = tuple(allow) + an._ALLOW_FACT_PHRASES
        key = an._cache_key(system, user, banned, full_allow, model)

        with tempfile.TemporaryDirectory() as d:
            cache_path = Path(d) / "narrative_cache_2026-08-29.json"
            cache_path.write_text(
                json.dumps({key: {"body": "既存の本文です。", "action": "既存のアクションです。"}}),
                encoding="utf-8")

            an.load_narrative_cache(cache_path)

            with mock.patch.object(an, "chat_json") as mocked_chat_json:
                result = an._generate_checked(
                    "tag-fixture", system=system, user=user, banned=banned,
                    allow=allow, model=model, quiet=True)

            mocked_chat_json.assert_not_called()
        self.assertEqual(result["src"], "ai")
        self.assertEqual(an.REJECT_STATS.get("cache", 0), 0)
        self.assertNotIn("cache", an.REJECT_STATS)
        self.assertEqual(an._NARR_CACHE_STATS["hit"], 1)


if __name__ == "__main__":
    unittest.main()
