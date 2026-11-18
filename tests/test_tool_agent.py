"""Tests for the asynchronous LangGraph MCP tool-calling workflow."""
import unittest

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from agent.tool_agent import build_tool_agent, restrict_tools_for_request


@tool
async def text_stats(text: str) -> dict[str, int]:
    """Return a small set of text statistics asynchronously."""
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



@tool
async def browser_inspect(url: str) -> str:
    """Inspect a page with a real browser."""
    return url


@tool
async def web_search(query: str) -> str:
    """Search the web."""
    return query


@tool
async def scrape_url(url: str) -> str:
    """Scrape a page."""
    return url

class ToolAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicitly_forbidden_tools_are_removed_before_model_binding(self):
        approved = restrict_tools_for_request(
            [browser_inspect, web_search, scrape_url, text_stats],
            "Use browser_inspect. Do not use web_search or scrape_url.",
        )
        self.assertEqual([item.name for item in approved], ["browser_inspect", "text_stats"])
    async def test_agent_executes_async_tool_and_returns_final_answer(self):
        model = FakeToolCallingModel()
        agent = build_tool_agent(model, [text_stats])
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content="Count the words in CNC plasma.")]},
            config={"recursion_limit": 5},
        )

        self.assertEqual(
            result["messages"][-1].content,
            "The text has 10 characters and 2 words.",
        )
        self.assertEqual(model.calls, 2)
        self.assertTrue(any(message.type == "tool" for message in result["messages"]))


if __name__ == "__main__":
    unittest.main()
