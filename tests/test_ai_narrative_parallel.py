"""narrate_leveling_actions の並列化(NARRATE_WORKERS)のテスト。

LLM呼び出し(chat_json)はテストしない。境界を _generate_checked に置き、フェイクへ
差し替えて narrate_leveling_actions を駆動する:
  1. 出力不変: NARRATE_WORKERS=1 と 4 で各ユニットの narrative が完全一致
  2. 並列に走っている: sleep入りフェイクで実時間が直列合計より明確に短い
  3. 1件の例外が全体を落とさない: 例外ユニットは narrative 未設定、他は正常
"""
import copy
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib import ai_narrative as an  # noqa: E402

UNITS = [
    {"name": "循環器内科", "room_per_week": 10.0, "retention": 0.85, "room_delta_4w": 1.0},
    {"name": "呼吸器内科", "room_per_week": 8.0, "retention": 0.90, "room_delta_4w": 0.5},
    {"name": "整形外科", "room_per_week": 6.0, "retention": 0.80, "room_delta_4w": 1.5},
    {"name": "消化器内科", "room_per_week": 4.0, "retention": 0.95, "room_delta_4w": 0.0},
    {"name": "腎臓内科", "room_per_week": 2.0, "retention": 0.70, "room_delta_4w": 2.0},
]


def _wl(units):
    return {"dept": {"units": copy.deepcopy(units)}}


def _by_name(weekend_leveling, entity="dept"):
    return {u["name"]: u for u in weekend_leveling[entity]["units"]}


def _fake_generate_checked(tag, system, user, banned, allow=(), model=None,
                           temperature=None, quiet=False):
    """入力(tag)だけから決まる純関数フェイク。実行順序に依存しない。"""
    return {"body": f"B::{tag}", "action": f"A::{tag}", "src": "ai"}


class TestLevelingParallelOutputUnchanged(unittest.TestCase):
    """1並列(直列相当)と4並列で narrative が完全一致すること。"""

    def _run(self, workers, skip=None):
        with mock.patch.object(an, "_generate_checked", _fake_generate_checked), \
             mock.patch.object(an, "NARRATE_WORKERS", workers):
            return an.narrate_leveling_actions(_wl(UNITS), top_n=5, quiet=True, skip=skip)

    def test_narrative_identical_across_worker_counts(self):
        seq = _by_name(self._run(1))
        par = _by_name(self._run(4))
        self.assertEqual(set(seq), set(UNITS[i]["name"] for i in range(len(UNITS))))
        for name in seq:
            self.assertEqual(seq[name]["narrative"], par[name]["narrative"])

    def test_skip_produces_no_narrative(self):
        skip = {"整形外科"}
        for workers in (1, 4):
            units = _by_name(self._run(workers, skip=skip))
            self.assertNotIn("narrative", units["整形外科"])
            for name in units:
                if name != "整形外科":
                    self.assertIn("narrative", units[name])
                    self.assertIsNotNone(units[name]["narrative"])


class TestLevelingParallelIsConcurrent(unittest.TestCase):
    """sleep入りフェイクで、実時間が直列合計より明確に短いこと。"""

    def test_wall_clock_shorter_than_sequential(self):
        sleep_s = 0.15
        n = 8
        units = [{"name": f"科{i}", "room_per_week": float(n - i),
                 "retention": 0.85, "room_delta_4w": 0.5} for i in range(n)]

        def slow_fake(tag, system, user, banned, allow=(), model=None,
                     temperature=None, quiet=False):
            time.sleep(sleep_s)
            return {"body": "b", "action": "a", "src": "ai"}

        with mock.patch.object(an, "_generate_checked", slow_fake), \
             mock.patch.object(an, "NARRATE_WORKERS", 4):
            t0 = time.monotonic()
            an.narrate_leveling_actions(_wl(units), top_n=n, quiet=True)
            elapsed = time.monotonic() - t0

        sequential_estimate = sleep_s * n   # 1.2s
        # 4並列・8件なら理論値は概ね2バッチ分(≈0.3s+overhead)。flakyにならない
        # よう直列推定の6割未満という緩い閾値で判定する。
        self.assertLess(elapsed, sequential_estimate * 0.6)


class TestLevelingParallelFailureIsolation(unittest.TestCase):
    """1ユニットの例外が他ユニットの生成結果に波及しないこと。"""

    def test_one_failure_does_not_break_others(self):
        fail_name = "整形外科"

        def maybe_fail_fake(tag, system, user, banned, allow=(), model=None,
                            temperature=None, quiet=False):
            if fail_name in tag:
                raise RuntimeError("boom")
            return {"body": f"B::{tag}", "action": f"A::{tag}", "src": "ai"}

        with mock.patch.object(an, "_generate_checked", maybe_fail_fake), \
             mock.patch.object(an, "NARRATE_WORKERS", 4):
            result = an.narrate_leveling_actions(_wl(UNITS), top_n=5, quiet=True)

        units = _by_name(result)
        self.assertNotIn("narrative", units[fail_name])
        for name, u in units.items():
            if name != fail_name:
                self.assertIn("narrative", u)
                self.assertIsNotNone(u["narrative"])


if __name__ == "__main__":
    unittest.main()
