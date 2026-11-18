"""Tests for conservative context budgeting and tool-turn integrity."""
import unittest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.context_manager import (
    compact_messages,
    estimate_tokens,
    estimate_message_tokens,
)


class ContextManagerTests(unittest.TestCase):
    def test_returns_all_messages_when_under_budget(self):
        messages = [HumanMessage(content="hello"), AIMessage(content="world")]
        self.assertEqual(compact_messages(messages, 100), messages)

    def test_drops_oldest_complete_turns_first(self):
        messages = [
            HumanMessage(content="old question " * 30),
            AIMessage(content="old answer " * 30),
            HumanMessage(content="new question"),
            AIMessage(content="new answer"),
        ]
        result = compact_messages(messages, 30)
        self.assertEqual(result, messages[-2:])

    def test_keeps_latest_turn_even_when_it_exceeds_budget(self):
        messages = [
            HumanMessage(content="old"),
            AIMessage(content="old answer"),
            HumanMessage(content="latest " * 100),
        ]
        result = compact_messages(messages, 5)
        self.assertEqual(result, messages[-1:])

    def test_never_separates_tool_call_from_tool_result(self):
        messages = [
            HumanMessage(content="old " * 100),
            AIMessage(content="old response"),
            HumanMessage(content="look this up"),
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "lookup",
                    "args": {"query": "latest"},
                    "id": "call_1",
                    "type": "tool_call",
                }],
            ),
            ToolMessage(content="result", tool_call_id="call_1"),
            AIMessage(content="final answer"),
        ]
        result = compact_messages(messages, 100)
        self.assertEqual(result, messages[-4:])
        self.assertEqual(result[1].tool_calls[0]["id"], result[2].tool_call_id)

    def test_token_estimator_includes_message_overhead(self):
        message = HumanMessage(content="abc")
        self.assertGreaterEqual(estimate_message_tokens(message), 5)
        self.assertEqual(estimate_tokens([message]), estimate_message_tokens(message))

    def test_rejects_invalid_budget(self):
        with self.assertRaises(ValueError):
            compact_messages([HumanMessage(content="hi")], 0)


if __name__ == "__main__":
    unittest.main()
