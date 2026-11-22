import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.main import app
from backend.providers import DemoProvider
import backend.main as main_module


class LearningFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls._original_evaluator = main_module.speech_evaluator
        main_module.speech_evaluator = DemoProvider()
        # Never spend real DeepSeek credits from unit tests.
        cls._tutor_patch = patch(
            "backend.tutor.settings",
            SimpleNamespace(deepseek_configured=False, deepseek_vision_model="your-chat-model"),
        )
        cls._tutor_patch.start()

    @classmethod
    def tearDownClass(cls):
        main_module.speech_evaluator = cls._original_evaluator
        cls._tutor_patch.stop()

    def test_content_is_shared(self):
        response = self.client.get("/api/content", params={"topic": "campus", "grade": "高二"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["words"])
        self.assertTrue(data["sentences"])
        self.assertTrue(all(word["topic"] == "campus" for word in data["words"]))
        self.assertEqual(data["grade"], "高二")
        self.assertTrue(data["vocabulary"])
        self.assertTrue(all(word["grade"] == "高二" for word in data["vocabulary"]))
        self.assertTrue(data["scenes"])

    def test_vocabulary_grade_mismatch_is_rejected(self):
        response = self.client.get("/api/content", params={"grade": "大一"})
        self.assertEqual(response.status_code, 422)

    def test_conversation_saves_audio_then_finishes(self):
        created = self.client.post("/api/conversation/session", json={"topic": "campus", "duration_minutes": 5})
        self.assertEqual(created.status_code, 201)
        session_id = created.json()["id"]
        turn = self.client.post(
            f"/api/conversation/{session_id}/turn",
            data={"transcript": "I enjoyed the science club this week.", "duration_seconds": "2.4"},
            files={"audio": ("turn.webm", io.BytesIO(b"demo-audio" * 200), "audio/webm")},
        )
        self.assertEqual(turn.status_code, 201)
        self.assertIn("/media/", turn.json()["turn"]["audio_url"])
        finished = self.client.post(f"/api/conversation/{session_id}/finish", json={"elapsed_seconds": 30})
        self.assertEqual(finished.status_code, 200)
        report = finished.json()["report"]
        self.assertTrue(report["demo"])
        self.assertEqual(report["audio_turn_count"], 1)
        self.assertIsNone(report["pronunciation"])

    def test_pronunciation_requires_audio_and_target_only(self):
        response = self.client.post(
            "/api/pronunciation/evaluate",
            data={"target_text": "achieve", "content_type": "word", "content_id": "w01"},
            files={"audio": ("student.webm", io.BytesIO(b"demo-audio" * 500), "audio/webm")},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["target_text"], "achieve")
        self.assertTrue(response.json()["assessment"]["demo"])

    def test_dictation_and_review_update_same_word(self):
        wrong = self.client.post("/api/dictation/attempt", json={"word_id": "w01", "answer": "acheve"})
        self.assertEqual(wrong.status_code, 200)
        self.assertFalse(wrong.json()["correct"])
        review = self.client.get("/api/progress").json()["review_ids"]
        self.assertIn("w01", review)
        known = self.client.post("/api/vocabulary/w01/review", json={"known": True})
        self.assertTrue(known.json()["state"]["known"])

    def test_quiz_attempt_and_graded_vocabulary_lookup(self):
        word = self.client.get("/api/content", params={"grade": "高一"}).json()["vocabulary"][0]
        answer = self.client.post(f"/api/vocabulary/{word['id']}/quiz", json={"correct": True})
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.json()["state"]["last_mode"], "quiz")
        self.assertIn("next_review", answer.json()["state"])
        wrong = self.client.post("/api/dictation/attempt", json={"word_id": word["id"], "answer": "definitely-not-it"})
        self.assertFalse(wrong.json()["correct"])

    def test_coach_plan_uses_current_grade_and_evidence(self):
        response = self.client.get("/api/plan", params={"grade": "高二"})
        self.assertEqual(response.status_code, 200)
        plan = response.json()
        self.assertEqual(plan["grade"], "高二")
        self.assertIn("message", plan["coach"])
        self.assertTrue(plan["steps"])
        self.assertIn("mastery", plan["stats"])
        self.assertIn("speaking_seconds", plan["stats"])
        self.assertEqual(plan["focus"]["id"], plan["steps"][0]["id"])

    def test_paper_dictation_returns_pending_without_deepseek_key(self):
        from backend.config import settings

        if settings.deepseek_configured:
            self.skipTest("DeepSeek key is configured; the live grader would be called")
        vocabulary = self.client.get("/api/content", params={"grade": "高一"}).json()["vocabulary"]
        ids = [word["id"] for word in vocabulary[:5]]
        response = self.client.post(
            "/api/dictation/photo",
            data={"word_ids": json.dumps(ids)},
            files={"image": ("paper.jpg", io.BytesIO(b"fake-jpeg-bytes"), "image/jpeg")},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "pending")
        self.assertEqual(len(data["items"]), 5)
        self.assertIsNone(data["items"][0]["correct"])


if __name__ == "__main__":
    unittest.main()
