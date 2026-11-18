"""Tests for the LangGraph MCP tool-calling workflow."""
import unittest

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from agent.tool_agent import build_tool_agent


@tool
def text_stats(text: str) -> dict[str, int]:
    """Return a small set of text statistics."""
    return {"characters": len(text), "words": len(text.split())}


class FakeToolCallingModel:
    def __init__(self):
        self.calls = 0

    def bind_tools(self, tools):
        self.available_tools = tools
        return self

    def invoke(self, messages):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "text_stats",
                        "args": {"text": "CNC plasma"},
                        "id": "call_text_stats",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content="The text has 10 characters and 2 words.")


class ToolAgentTests(unittest.TestCase):
    def test_agent_executes_tool_and_returns_final_answer(self):
        model = FakeToolCallingModel()
        agent = build_tool_agent(model, [text_stats])
        result = agent.invoke(
            {"messages": [HumanMessage(content="Count the words in CNC plasma.")]},
            config={"recursion_limit": 5},
        )

        self.assertEqual(result["messages"][-1].content,
                         "The text has 10 characters and 2 words.")
        self.assertEqual(model.calls, 2)
        self.assertTrue(any(message.type == "tool" for message in result["messages"]))


if __name__ == "__main__":
    unittest.main()
