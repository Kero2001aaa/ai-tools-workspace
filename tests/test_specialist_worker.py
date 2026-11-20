"""Tests for bounded, isolated LLM specialist workers."""
import unittest
from unittest.mock import patch

from agent.specialist_worker import run_specialist, validate_delegation


class SpecialistWorkerTests(unittest.TestCase):
    def test_accepts_only_three_specialist_roles(self):
        for role in ("research", "coding", "review"):
            self.assertEqual("A task", validate_delegation(role, " A task ")[0])
        with self.assertRaisesRegex(ValueError, "specialist must be"):
            validate_delegation("shell", "A task")

    def test_rejects_empty_or_oversized_task(self):
        for task in ("", "  ", "x" * 4001):
            with self.subTest(length=len(task)), self.assertRaises(ValueError):
                validate_delegation("coding", task)

    def test_rejects_non_text_or_oversized_context(self):
        for context in (None, 123, "x" * 8001):
            with self.subTest(context_type=type(context).__name__), self.assertRaises(ValueError):
                validate_delegation("review", "Review this", context)

    def test_offline_mode_fails_without_model(self):
        with patch("agent.specialist_worker.load_chat_model", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "offline mode"):
                run_specialist("research", "Analyze this")

    def test_specialist_returns_bounded_model_analysis(self):
        class Response:
            content = "analysis"
        class Model:
            def invoke(self, messages):
                self.messages = messages
                return Response()
        model = Model()
        with patch("agent.specialist_worker.load_chat_model", return_value=model):
            result = run_specialist("review", "Review this")
        self.assertEqual({"specialist": "review", "analysis": "analysis", "truncated": False}, result)
        self.assertEqual(2, len(model.messages))


if __name__ == "__main__":
    unittest.main()
