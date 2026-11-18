"""Tests for the initial LangGraph workflow."""
import unittest

from agent.graph import run_agent


class AgentGraphTests(unittest.TestCase):
    def test_research_intent(self):
        result = run_agent("Research LangGraph")
        self.assertEqual(result["intent"], "research")
        self.assertIn("workflow executed successfully", result["response"])

    def test_technical_intent(self):
        result = run_agent("Debug this Python code")
        self.assertEqual(result["intent"], "technical")

    def test_general_intent(self):
        result = run_agent("Explain this concept")
        self.assertEqual(result["intent"], "general")

    def test_empty_request_is_rejected(self):
        with self.assertRaises(ValueError):
            run_agent("   ")


if __name__ == "__main__":
    unittest.main()
