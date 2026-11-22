"""Tutor reply generation: prompt wiring and graceful degradation."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend import tutor
from backend.deepseek_client import ChatResult, DeepSeekError

# 当前模型由 llm_provider 统一路由，测试里用替身模拟"可用/不可用"。
WITH_KEY = SimpleNamespace(
    available=lambda: True,
    current=lambda: "deepseek",
    model_name=lambda: "your-chat-model",
)
NO_KEY = SimpleNamespace(
    available=lambda: False,
    current=lambda: "deepseek",
    model_name=lambda: "your-chat-model",
)


class TutorReplyTest(unittest.TestCase):
    def setUp(self):
        # Reset the circuit breaker between tests.
        tutor._cooldown_until = 0.0
        tutor._cooldown_reason = ""

    def test_uses_the_configured_model_and_keeps_the_scene_prompt(self):
        captured = {}

        async def fake_chat(messages, **kwargs):
            captured["messages"] = messages
            return ChatResult("That sounds fun! What did your team build?", "your-chat-model", "mock")

        # The prompt follows the generated scene rather than a fixed topic table,
        # so the test carries a scene like the real session does.
        session = {
            "topic": "campus",
            "grade": "高一",
            "turns": [],
            "scene": {
                "title_zh": "社团招新",
                "role": "Ms. Hale",
                "setting": "校园社团招新摊位前",
                "goal": "说出你想加入的社团和理由",
            },
        }
        with patch("backend.tutor.llm_provider", WITH_KEY), patch("backend.tutor.chat", fake_chat):
            text, mode = asyncio.run(tutor.reply(session, "I joined the science club."))

        self.assertEqual(mode, "deepseek")
        self.assertIn("science club", captured["messages"][-1]["content"])
        self.assertIn("Ms. Hale", captured["messages"][0]["content"])
        self.assertIn("社团招新", captured["messages"][0]["content"])
        self.assertIn("team build", text)

    def test_falls_back_when_balance_is_empty_and_trips_breaker(self):
        calls = []

        async def failing_chat(messages, **kwargs):
            calls.append(1)
            raise DeepSeekError("余额不足", status=402, code="insufficient_balance", hint="充值")

        session = {"topic": "campus", "turns": []}
        with patch("backend.tutor.llm_provider", WITH_KEY), patch("backend.tutor.chat", failing_chat):
            text, mode = asyncio.run(tutor.reply(session, "I like English."))

        self.assertEqual(mode, "fallback")
        self.assertTrue(text)
        self.assertEqual(len(calls), 1)
        self.assertGreater(tutor.status()["cooldown_seconds"], 0)

        # The next turn must not call the paid endpoint again.
        with patch("backend.tutor.llm_provider", WITH_KEY), patch("backend.tutor.chat", failing_chat):
            _, second_mode = asyncio.run(tutor.reply(session, "I also like music."))
        self.assertEqual(len(calls), 1)
        self.assertEqual(second_mode, "demo")

    def test_without_model_config_stays_deterministic(self):
        session = {"topic": "campus", "turns": []}
        with patch("backend.tutor.llm_provider", NO_KEY), patch("backend.tutor.chat") as mocked:
            text, mode = asyncio.run(tutor.reply(session, "I like English."))
            mocked.assert_not_called()
        self.assertEqual(mode, "demo")
        self.assertTrue(text)


if __name__ == "__main__":
    unittest.main()
