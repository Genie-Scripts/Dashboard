"""部門レポート per-unit ループの3パス分割（narrate_* 並列化）のテスト。

build_dept_report_contexts の per-unit ループは、①LLM呼び出し直前までの中間結果
（parts・トレンド・topic選定など）を積む逐次パス、②記録した narrate_*（admission/
surgery/emergency_admission/emergency_leveling）呼び出しを NARRATE_WORKERS 並列で
実行するパス、③move確定〜contexts組み立ての逐次パス、の3パスに分かれている
（narrate_leveling_actions のバッチ呼び出しは対象外＝既存どおり別経路）。

  1. ゴールデン同一性: 改修前コード（git stash で一時的に復元して生成した
     tests/test_dept_report_leveling_skip.py 経由のフェイク合成データ）の
     contexts と、改修後コードの contexts が完全一致すること。
  2. NARRATE_WORKERS=1 と 4 で contexts が完全一致すること。
  3. 1ユニットの narrate_* 例外が他ユニット・contexts の件数/順序へ波及しないこと
     （フォールバック定型文へ無害縮退・ログに残る）。
  4. 並列に走っていること（sleep入りフェイクで実時間が直列合計より明確に短い）。

LLM呼び出しはしない（フェイクのみ）。実行: リポジトリルートで
    python -m pytest tests/ -q
"""
import copy
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib import dept_report as dr  # noqa: E402
from tests.test_dept_report_leveling_skip import (  # noqa: E402
    _Pipeline, _make_leveling_fake,
)

# ────────────────────────────────────
# 1. ゴールデン同一性
# ────────────────────────────────────
# 2026-07-19 に、per-unit ループを3パス分割する改修の直前のコード（git stash で
# app/lib/dept_report.py のみ一時復元）へ tests/test_dept_report_leveling_skip.py の
# _Pipeline(フェイク合成データのみ・実データ無し)を通して生成した contexts の
# スナップショット。charts は render_trend_svg をフェイクで空文字にしているため
# kind/priority のみへ間引いてある。
# 2026-08: ai_body/ai_action（override適用前のAI/定型文の退避）を追加した改修に合わせて
# ゴールデンを更新（本文自体の変化は無い＝新規キーの追加のみ）。腎臓内科（人手オーバーライド）は
# 全文差し替えでもAI生成そのものは止めない。narrate_leveling_actions のbatch skip対象からも
# 外れたため（2026-08 追加改修）、ai_body/ai_action には差し替え前の実AI文
# （leveling バッチのフェイク生成 "LEVAI::腎臓内科"）が入る。
# 2026-09-02: 副トピック定型文の事実化（_secondary_clause）により、週末文は per-unit の
# 実測 retention が目標比 mild/poor のときだけ付く。本フィクスチャの units は retention を
# 持たない診療科（呼吸器内科）には付かず、retention が poor の 10A病棟にだけ実測ベースの文が付く。
_GOLDEN = [
 {"axis": "dept", "type_key": "internal", "order": 0, "unit": "循環器内科",
  "total_retention_pct": 80.0,
  "charts": [{"kind": "A", "priority": 1}],
  "move": {"body": "LEVAI::循環器内科", "action": "LEVAI-ACT::循環器内科",
           "src": "ai", "topic": "leveling", "delta": None,
           "ai_body": "LEVAI::循環器内科", "ai_action": "LEVAI-ACT::循環器内科"}},
 {"axis": "dept", "type_key": "internal", "order": 1, "unit": "呼吸器内科",
  "total_retention_pct": 80.0,
  "charts": [{"kind": "A", "priority": 1}],
  "move": {"body": "ADM::呼吸器内科",
           "action": "ADM-ACT::呼吸器内科", "src": "ai", "topic": "admission",
           "delta": None,
           "ai_body": "ADM::呼吸器内科",
           "ai_action": "ADM-ACT::呼吸器内科",
           "nadm_line": "新入院：直近7日 5件／週目標20（25%）。28日線は—／あと約15件/週で目標"}},
 {"axis": "dept", "type_key": "surgical", "order": 2, "unit": "整形外科",
  "total_retention_pct": 80.0,
  "charts": [{"kind": "A", "priority": 1}],
  "move": {"body": "SURG::整形外科", "action": "SURG-ACT::整形外科", "src": "ai",
           "topic": "surgery", "delta": None,
           "ai_body": "SURG::整形外科", "ai_action": "SURG-ACT::整形外科",
           "surg_line": "全麻：直近7日 1件／週目標10（10%）。28日線は—／あと約9件/週で目標"}},
 {"axis": "dept", "type_key": "internal", "order": 3, "unit": "消化器内科",
  "total_retention_pct": 80.0,
  "charts": [{"kind": "A", "priority": 1}],
  "move": {"body": "週末も平日とほぼ同じ在院を保てています。今の入退院のリズムが手本になっています。",
           # 添削フィードバックループ P2 で定型文を更新（「現状維持。」で完結させない）。
           # 本ゴールデンは「3パス分割の前後で出力が変わらないこと」を守るためのもので、
           # 文言の意図的変更にあわせて追随させる。
           "action": "週末の入退院リズムはこのまま継続しつつ、在院水準のさらなる底上げを図りましょう。",
           "topic": "leveling", "src": "tpl", "delta": None,
           "ai_body": "週末も平日とほぼ同じ在院を保てています。今の入退院のリズムが手本になっています。",
           "ai_action": "週末の入退院リズムはこのまま継続しつつ、在院水準のさらなる底上げを図りましょう。"}},
 {"axis": "dept", "type_key": "internal", "order": 4, "unit": "腎臓内科",
  "total_retention_pct": 80.0,
  "charts": [{"kind": "A", "priority": 1}],
  "move": {"body": "手動本文差し替え", "action": "手動一手差し替え", "topic": "leveling",
           "src": "manual", "delta": None, "ov_fields": ["body", "action"],
           "ai_body": "LEVAI::腎臓内科", "ai_action": "LEVAI-ACT::腎臓内科"}},
 {"axis": "ward", "type_key": "ward", "order": 0, "unit": "04A",
  "total_retention_pct": 80.0,
  "charts": [{"kind": "A", "priority": 1}],
  "move": {"body": "EMLEV::04A", "action": "EMLEV-ACT::04A", "src": "ai",
           "topic": "emergency-leveling", "delta": None,
           "ai_body": "EMLEV::04A", "ai_action": "EMLEV-ACT::04A"}},
 {"axis": "ward", "type_key": "ward", "order": 1, "unit": "09B病棟",
  "total_retention_pct": 80.0,
  "charts": [{"kind": "A", "priority": 1}],
  "move": {"body": "LEVAI::09B病棟", "action": "LEVAI-ACT::09B病棟", "src": "ai",
           "topic": "leveling", "delta": None,
           "ai_body": "LEVAI::09B病棟", "ai_action": "LEVAI-ACT::09B病棟"}},
 {"axis": "ward", "type_key": "ward", "order": 2, "unit": "10A病棟",
  "total_retention_pct": 80.0,
  "charts": [{"kind": "A", "priority": 1}],
  "move": {"body": "ADM::10A病棟 なお、週末在院の維持率は目標を明確に下回っている状況です。",
           "action": "ADM-ACT::10A病棟", "src": "ai", "topic": "admission",
           "delta": None,
           "ai_body": "ADM::10A病棟 なお、週末在院の維持率は目標を明確に下回っている状況です。",
           "ai_action": "ADM-ACT::10A病棟"}},
]


def _thin(contexts):
    """golden と比較しやすいよう charts を kind/priority のみへ間引き、比較対象の
    キーだけを抜き出す（kpis/period_* 等の日付・書式は今回の改修と無関係なので対象外）。"""
    out = []
    for c in contexts:
        out.append({
            "axis": c["axis"], "type_key": c["type_key"], "order": c["order"],
            "unit": c["unit"], "total_retention_pct": c["total_retention_pct"],
            "charts": [{"kind": ch.get("kind"), "priority": ch.get("priority")}
                       for ch in c["charts"]],
            "move": c["move"],
        })
    return out


class TestGoldenIdentity(unittest.TestCase):
    """改修（3パス分割）前後で contexts が完全一致すること。"""

    def test_matches_pre_split_golden(self):
        contexts = _Pipeline(_make_leveling_fake(respect_skip=True, skip_log=[])).run()
        self.assertEqual(_thin(contexts), _GOLDEN)


class TestWorkerCountOutputUnchanged(unittest.TestCase):
    """NARRATE_WORKERS=1（実質直列）と 4（並列）で contexts が完全一致すること。"""

    def _run(self, workers):
        with mock.patch.object(dr, "NARRATE_WORKERS", workers):
            return _Pipeline(_make_leveling_fake(respect_skip=True, skip_log=[])).run()

    def test_identical_across_worker_counts(self):
        seq = self._run(1)
        par = self._run(4)
        self.assertEqual(_thin(seq), _thin(par))


class TestOneFailureDoesNotBreakBuild(unittest.TestCase):
    """1ユニットの narrate_* 例外が他ユニット・contexts の件数/順序に波及しないこと。"""

    def test_failing_unit_falls_back_others_unaffected(self):
        def failing_surgery(name, *a, **kw):
            raise RuntimeError("boom: 整形外科の生成失敗")

        pipeline = _Pipeline(_make_leveling_fake(respect_skip=True, skip_log=[]),
                             surgery_fake=failing_surgery)
        with self.assertLogs("app.lib.dept_report", level="WARNING") as cm:
            contexts = pipeline.run()
        self.assertTrue(any("整形外科" in m for m in cm.output))

        # 件数・順序は正常時のゴールデンと同じ（8件・axis/unit の並びを保つ）
        thinned = _thin(contexts)
        self.assertEqual([(c["axis"], c["unit"]) for c in thinned],
                         [(c["axis"], c["unit"]) for c in _GOLDEN])

        moves = {(c["axis"], c["unit"]): c["move"] for c in contexts}
        # 失敗した整形外科はAIマーカーを含まずフォールバック（定型文）へ縮退
        surg_move = moves[("dept", "整形外科")]
        self.assertNotIn("SURG::", surg_move["body"])
        self.assertEqual(surg_move["topic"], "surgery")
        self.assertNotEqual(surg_move.get("src"), "ai")
        # 他の narrate_* 呼び出しユニットは正常どおりAI文言のまま
        self.assertEqual(moves[("dept", "呼吸器内科")]["body"],
                         "ADM::呼吸器内科")
        self.assertEqual(moves[("ward", "04A")]["body"], "EMLEV::04A")
        self.assertEqual(moves[("ward", "10A病棟")]["body"],
                         "ADM::10A病棟 なお、週末在院の維持率は目標を明確に下回っている状況です。")


class TestParallelIsConcurrent(unittest.TestCase):
    """sleep入りフェイクで、実時間が直列合計より明確に短いこと。

    このテストのフィクスチャでは narrate_* 呼び出し（leveling バッチ経由ではなく
    per-unit で直接呼ばれるもの）が dept軸=2件（呼吸器内科・整形外科）、
    ward軸=2件（04A・10A病棟）の計4件発生する。entity(dept/ward)ループ自体は
    逐次のため、期待される実時間は「dept軸のパス2(≒sleepぶん)＋ward軸のパス2
    (≒sleepぶん)」＝直列4件合計の約半分。"""

    def test_wall_clock_shorter_than_sequential(self):
        sleep_s = 0.2

        def slow_admission(name, *a, **kw):
            time.sleep(sleep_s)
            return {"body": f"ADM::{name}", "action": f"ADM-ACT::{name}", "src": "ai"}

        def slow_surgery(name, *a, **kw):
            time.sleep(sleep_s)
            return {"body": f"SURG::{name}", "action": f"SURG-ACT::{name}", "src": "ai"}

        def slow_em_leveling(name, *a, **kw):
            time.sleep(sleep_s)
            return {"body": f"EMLEV::{name}", "action": f"EMLEV-ACT::{name}", "src": "ai"}

        pipeline = _Pipeline(_make_leveling_fake(respect_skip=True, skip_log=[]),
                             admission_fake=slow_admission, surgery_fake=slow_surgery,
                             em_leveling_fake=slow_em_leveling)
        with mock.patch.object(dr, "NARRATE_WORKERS", 4):
            t0 = time.monotonic()
            pipeline.run()
            elapsed = time.monotonic() - t0

        n_calls = 4   # 呼吸器内科・整形外科・04A・10A病棟
        sequential_estimate = sleep_s * n_calls
        # 2軸×2並列なら理論値は概ね sleep_s*2。flaky にならないよう
        # 直列推定の7割未満という緩い閾値で判定する。
        self.assertLess(elapsed, sequential_estimate * 0.7)


if __name__ == "__main__":
    unittest.main()
