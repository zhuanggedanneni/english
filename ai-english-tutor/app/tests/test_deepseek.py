"""Tests for the DeepSeek adapter's classification and offline behaviour."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from backend.deepseek_client import DeepSeekError, _classify_status, chat, probe
from backend.deepseek_grader import PhotoGraderError, grade_handwriting

NO_KEY = SimpleNamespace(
    deepseek_api_key="",
    deepseek_base_url="https://your-llm-endpoint.example.com",
    deepseek_vision_model="your-chat-model",
)
WITH_KEY = SimpleNamespace(
    deepseek_api_key="sk-test-key",
    deepseek_base_url="https://your-llm-endpoint.example.com",
    deepseek_vision_model="your-chat-model",
    deepseek_configured=True,
)


class StatusClassificationTest(unittest.TestCase):
    def test_insufficient_balance_is_distinct_from_auth_failure(self):
        code, message, hint = _classify_status(402)
        self.assertEqual(code, "insufficient_balance")
        self.assertIn("余额不足", message)
        self.assertTrue(hint)

    def test_auth_failure(self):
        self.assertEqual(_classify_status(401)[0], "auth_failed")
        self.assertEqual(_classify_status(403)[0], "auth_failed")

    def test_unknown_endpoint_and_server_error(self):
        self.assertEqual(_classify_status(404)[0], "endpoint_not_found")
        self.assertEqual(_classify_status(503)[0], "server_error")


class OfflineBehaviourTest(unittest.TestCase):
    def test_probe_without_key_does_not_call_network(self):
        with patch("backend.deepseek_client.settings", NO_KEY):
            result = asyncio.run(probe())
        self.assertFalse(result.configured)
        self.assertEqual(result.verdict, "unconfigured")
        self.assertIsNone(result.http_status)

    def test_chat_without_key_raises_actionable_error(self):
        with patch("backend.deepseek_client.settings", NO_KEY):
            with self.assertRaises(DeepSeekError) as context:
                asyncio.run(chat([{"role": "user", "content": "hi"}]))
        self.assertEqual(context.exception.code, "unconfigured")
        self.assertIn("DEEPSEEK_API_KEY", context.exception.hint)


REAL_ASYNC_CLIENT = httpx.AsyncClient


def _mock_client(handler):
    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))

    return factory


class GraderPipelineTest(unittest.TestCase):
    """Exercise the real request/parse path without touching the network."""

    def _run(self, handler, expectations):
        with patch("backend.deepseek_client.settings", WITH_KEY), \
             patch("backend.deepseek_grader.settings", WITH_KEY), \
             patch("backend.deepseek_client.httpx.AsyncClient", _mock_client(handler)):
            return asyncio.run(grade_handwriting(b"\xff\xd8\xff", "image/jpeg", expectations))

    def test_image_is_sent_as_data_url_and_result_is_compared_locally(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            captured["body"] = body
            content = body["messages"][0]["content"]
            self.assertEqual(body["model"], "your-chat-model")
            self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
            return httpx.Response(200, json={
                "model": "your-chat-model",
                "choices": [{"message": {"content": '{"items":[{"index":1,"recognized":"acheive","correct":true}]}'}}],
            })

        result = self._run(handler, [{"id": "w1", "word": "achieve"}])
        self.assertEqual(result.status, "graded")
        # The model claimed correct, but local comparison must override it.
        self.assertFalse(result.items[0]["correct"])
        self.assertEqual(captured["body"]["response_format"], {"type": "json_object"})

    def test_insufficient_balance_surfaces_as_actionable_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(402, json={"error": {"message": "Insufficient Balance"}})

        with self.assertRaises(PhotoGraderError) as context:
            self._run(handler, [{"id": "w1", "word": "achieve"}])
        self.assertEqual(context.exception.code, "insufficient_balance")
        self.assertIn("余额不足", str(context.exception))
        self.assertTrue(context.exception.hint)

    def test_unreadable_page_is_not_recorded_as_wrong(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "model": "your-chat-model",
                "choices": [{"message": {"content": '{"items":[{"index":1,"recognized":null,"correct":null}]}'}}],
            })

        result = self._run(handler, [{"id": "w1", "word": "achieve"}])
        self.assertEqual(result.status, "unreadable")
        self.assertIsNone(result.items[0]["correct"])

    def test_pending_without_key_does_not_fake_a_score(self):
        pending_settings = SimpleNamespace(deepseek_configured=False, deepseek_vision_model="your-chat-model")
        with patch("backend.deepseek_grader.settings", pending_settings):
            result = asyncio.run(grade_handwriting(b"image", "image/jpeg", [{"id": "w1", "word": "achieve"}]))
        self.assertEqual(result.status, "pending")
        self.assertIsNone(result.items[0]["correct"])
        self.assertIn("照片已保存", result.message)


if __name__ == "__main__":
    unittest.main()
