"""test_llm_d2_deadline.py — Track D2「LLM呼び出しの総所要時間に上限を持たせる」の回帰テスト。

対象: app/lib/llm.py（OpenAI クライアント設定・response_format フォールバック・
genie_llm.session の wait_timeout 明示）と app/lib/ai_narrative.py の
_generate_checked（壁時計デッドライン）。

常駐サーバー(:8000等)は一切呼ばない。openai クライアント・genie_llm.session・
chat_json・time.monotonic はすべてモック/スタブに差し替える。
"""
import contextlib
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import httpx
from openai import APIConnectionError, BadRequestError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.lib import ai_narrative as an  # noqa: E402
from app.lib import llm  # noqa: E402


def _bad_request_error(message: str = "response_format not supported") -> BadRequestError:
    request = httpx.Request("POST", "http://test.invalid/v1/chat/completions")
    response = httpx.Response(400, request=request, json={"error": message})
    return BadRequestError(message, response=response, body=None)


def _connection_error() -> APIConnectionError:
    request = httpx.Request("POST", "http://test.invalid/v1/chat/completions")
    return APIConnectionError(request=request)


def _fake_response(content: str):
    """client.chat.completions.create(...) の戻り値相当（.choices[0].message.content）。"""
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))])


class _FakeClient:
    """`_get_client()` の戻り値を模す最小スタブ（chat.completions.create のみ）。"""

    def __init__(self, create_mock):
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=create_mock))


class TestGetClientMaxRetries(unittest.TestCase):
    """(1) OpenAI(max_retries=0) が明示されていること。"""

    def setUp(self):
        self._orig_client = llm._client
        llm._client = None

    def tearDown(self):
        llm._client = self._orig_client

    def test_max_retries_zero(self):
        captured = {}

        class _FakeOpenAI:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        with mock.patch("openai.OpenAI", _FakeOpenAI):
            client = llm._get_client()
        self.assertEqual(captured.get("max_retries"), 0)
        self.assertIsInstance(client, _FakeOpenAI)


class _ChatJsonTestBase(unittest.TestCase):
    """chat_json 系テストの共通セットアップ: genie_llm をスタブへ差し替える。"""

    def setUp(self):
        self._orig_genie_llm = llm.genie_llm
        self.session_calls = []

        def _fake_session(model, priority="interactive", *, wait_timeout=None, load=True):
            self.session_calls.append({
                "model": model, "priority": priority,
                "wait_timeout": wait_timeout, "load": load,
            })
            return contextlib.nullcontext()

        llm.genie_llm = types.SimpleNamespace(session=_fake_session)

    def tearDown(self):
        llm.genie_llm = self._orig_genie_llm


class TestChatJsonResponseFormatFallback(_ChatJsonTestBase):
    """(2) BadRequestError（HTTP 400）に限り response_format 無しで1回だけ再試行する。"""

    def test_bad_request_falls_back_once(self):
        ok_response = _fake_response('{"ok": true}')
        fake_create = mock.Mock(side_effect=[_bad_request_error(), ok_response])
        fake_client = _FakeClient(fake_create)
        with mock.patch.object(llm, "_get_client", return_value=fake_client):
            content = llm.chat_json("sys", "usr", model="test-model", seed=1)
        self.assertEqual(content, '{"ok": true}')
        self.assertEqual(fake_create.call_count, 2)
        second_kwargs = fake_create.call_args_list[1].kwargs
        self.assertNotIn("response_format", second_kwargs)
        first_kwargs = fake_create.call_args_list[0].kwargs
        self.assertIn("response_format", first_kwargs)

    def test_connection_error_raises_immediately(self):
        """(3) BadRequestError 以外（接続断等）は即送出・2回目の呼び出しはしない。"""
        fake_create = mock.Mock(side_effect=[_connection_error()])
        fake_client = _FakeClient(fake_create)
        with mock.patch.object(llm, "_get_client", return_value=fake_client):
            with self.assertRaises(APIConnectionError):
                llm.chat_json("sys", "usr", model="test-model", seed=1)
        self.assertEqual(fake_create.call_count, 1)


class TestChatJsonWaitTimeout(_ChatJsonTestBase):
    """(3) genie_llm.session に WAIT_TIMEOUT_SEC が明示的に渡ること。"""

    def test_wait_timeout_passed_to_session(self):
        fake_create = mock.Mock(return_value=_fake_response('{"ok": true}'))
        fake_client = _FakeClient(fake_create)
        with mock.patch.object(llm, "_get_client", return_value=fake_client):
            llm.chat_json("sys", "usr", model="test-model", seed=1)
        self.assertEqual(len(self.session_calls), 1)
        call = self.session_calls[0]
        self.assertEqual(call["model"], "test-model")
        self.assertEqual(call["priority"], "batch")
        self.assertEqual(call["wait_timeout"], llm.WAIT_TIMEOUT_SEC)


class TestGenerateCheckedDeadline(unittest.TestCase):
    """(4) _generate_checked の壁時計デッドライン（ソフト上限）。"""

    def setUp(self):
        an.reset_reject_stats()

    def test_deadline_breaks_before_second_attempt(self):
        """1回目の呼び出し中に時計が進み、2回目attempt前のループ先頭チェックで
        break する（判定は機械ガードで棄却＝judge直前チェックには到達しない経路）。"""
        fake_chat_json = mock.Mock(return_value='{"body": "3件あります", "action": "対応する"}')
        with mock.patch.object(an, "chat_json", fake_chat_json), \
             mock.patch.object(an, "NARR_DEADLINE_SEC", 100.0), \
             mock.patch("time.monotonic", side_effect=[0.0, 1.0, 400.0]):
            result = an._generate_checked("tag", "sys", "usr", banned=(), quiet=True)
        self.assertIsNone(result)
        self.assertEqual(fake_chat_json.call_count, 1)
        self.assertEqual(an.REJECT_STATS["deadline"], 1)

    def test_deadline_breaks_before_judge_call(self):
        """機械ガード通過後・judge呼び出し直前のチェックでも break する
        （_judge_consistency は一度も呼ばれない）。"""
        fake_chat_json = mock.Mock(
            return_value='{"body": "順調です", "action": "経過観察を継続"}')
        fake_judge = mock.Mock(return_value=True)
        with mock.patch.object(an, "chat_json", fake_chat_json), \
             mock.patch.object(an, "_judge_consistency", fake_judge), \
             mock.patch.object(an, "JUDGE_ENABLED", True), \
             mock.patch.object(an, "NARR_DEADLINE_SEC", 100.0), \
             mock.patch("time.monotonic", side_effect=[0.0, 1.0, 400.0]):
            result = an._generate_checked("tag", "sys", "usr", banned=(), quiet=True)
        self.assertIsNone(result)
        self.assertEqual(fake_chat_json.call_count, 1)
        fake_judge.assert_not_called()
        self.assertEqual(an.REJECT_STATS["deadline"], 1)

    def test_normal_success_path_unaffected(self):
        """デッドライン内の正常経路は従来どおり採択される（回帰なし）。"""
        fake_chat_json = mock.Mock(
            return_value='{"body": "順調です", "action": "経過観察を継続"}')
        with mock.patch.object(an, "chat_json", fake_chat_json), \
             mock.patch.object(an, "_judge_consistency", return_value=True):
            result = an._generate_checked("tag", "sys", "usr", banned=(), quiet=True)
        self.assertIsNotNone(result)
        self.assertEqual(result["body"], "順調です")
        self.assertEqual(result["action"], "経過観察を継続")
        self.assertEqual(result["src"], "ai")
        self.assertEqual(an.REJECT_STATS["ok"], 1)
        self.assertEqual(an.REJECT_STATS.get("deadline", 0), 0)


if __name__ == "__main__":
    unittest.main()
