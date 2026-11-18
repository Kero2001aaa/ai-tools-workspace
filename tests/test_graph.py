"""Tests for the LangGraph workflow."""
import unittest
from types import SimpleNamespace

from agent.graph import run_agent


class FakeChatModel:
    def invoke(self, messages):
        self.messages = messages
        return SimpleNamespace(content="Generated response from fake model.")


class AgentGraphTests(unittest.TestCase):
    def test_research_intent(self):
        result = run_agent("Research LangGraph")
        self.assertEqual(result["intent"], "research")

    def test_technical_intent(self):
        result = run_agent("Debug this Python code")
        self.assertEqual(result["intent"], "technical")

    def test_general_intent(self):
        result = run_agent("Explain this concept")
        self.assertEqual(result["intent"], "general")

    def test_empty_request_is_rejected(self):
        with self.assertRaises(ValueError):
            run_agent("   ")

    def test_offline_mode_does_not_call_a_model(self):
        result = run_agent("Explain this concept")
        self.assertIn("Offline mode", result["response"])
        self.assertIn("No LLM or external service was called", result["response"])

    def test_injected_model_generates_response(self):
        model = FakeChatModel()
        result = run_agent("Explain LangGraph", model=model)
        self.assertEqual(result["response"], "Generated response from fake model.")
        self.assertEqual(len(model.messages), 2)


if __name__ == "__main__":
    unittest.main()
