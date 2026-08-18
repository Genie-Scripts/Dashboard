"""test_llm_call_hygiene.py — chat_json 呼び出しに決定論 seed が必ず付いているか検査する（WP2）。

ai_narrative.py の生成経路（_narrate_one/_generate_checked/_judge_consistency）は
seed=CRC32(system+user)ベースで決定論化済み（3-3 月次安定性）。triage.py/weekly_story.py は
同じ流儀の seed 指定が漏れていた（同じ事実でも呼ぶたびに文が変わる）ため、app/lib 配下の
全 chat_json 呼び出しに seed keyword があることを ast（構文解析のみ）で検査する。
LLM・常駐サーバは一切呼ばない。
"""
import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

LIB_DIR = Path(__file__).resolve().parent.parent / "app" / "lib"


def _chat_json_calls_without_seed(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else None)
        if name != "chat_json":
            continue
        has_seed = any(kw.arg == "seed" for kw in node.keywords)
        if not has_seed:
            missing.append(f"{path.name}:{node.lineno}")
    return missing


class TestChatJsonSeedHygiene(unittest.TestCase):
    def test_all_chat_json_calls_have_seed_kwarg(self):
        offenders = []
        for path in sorted(LIB_DIR.glob("*.py")):
            offenders.extend(_chat_json_calls_without_seed(path))
        self.assertEqual(offenders, [],
                         f"seed未指定の chat_json 呼び出し（決定論が崩れる）: {offenders}")


if __name__ == "__main__":
    unittest.main()
