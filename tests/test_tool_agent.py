"""Tests for the asynchronous LangGraph MCP tool-calling workflow."""
import os
import tempfile
import unittest

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from agent.tool_agent import (
    build_tool_agent,
    build_workspace_server_config,
    get_recursion_limit,
    restrict_tools_for_request,
)


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

    def test_recursion_limit_defaults_to_twenty_five(self):
        self.assertEqual(25, get_recursion_limit({}))

    def test_recursion_limit_accepts_bounded_override(self):
        self.assertEqual(40, get_recursion_limit({"AGENT_RECURSION_LIMIT": "40"}))

    def test_recursion_limit_rejects_out_of_range_and_invalid_values(self):
        for value in ("4", "51", "invalid"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                get_recursion_limit({"AGENT_RECURSION_LIMIT": value})

    def test_planner_server_is_registered_by_default(self):
        servers = build_workspace_server_config({})
        self.assertEqual(servers["planner"]["command"], os.sys.executable)
        self.assertEqual(servers["planner"]["args"], ["-m", "agent.plan_mcp_server"])

    def test_agent_reach_server_is_disabled_by_default(self):
        self.assertNotIn("agent_reach", build_workspace_server_config({}))

    def test_agent_reach_server_requires_installed_executable(self):
        from unittest.mock import patch
        with patch("agent.tool_agent.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "agent-reach executable"):
                build_workspace_server_config({"ENABLE_AGENT_REACH_TOOLS": "true"})

    def test_agent_reach_server_registers_only_when_enabled(self):
        from unittest.mock import patch
        with patch("agent.tool_agent.shutil.which", return_value="/home/user/.local/bin/agent-reach"):
            servers = build_workspace_server_config({
                "ENABLE_AGENT_REACH_TOOLS": "true",
                "AGENT_REACH_COMMAND": "agent-reach",
            })
        self.assertEqual(servers["agent_reach"]["command"], os.sys.executable)
        self.assertEqual(servers["agent_reach"]["args"], ["-m", "agent.agent_reach_mcp_server"])
        self.assertEqual(servers["agent_reach"]["env"]["AGENT_REACH_COMMAND"], "/home/user/.local/bin/agent-reach")

    def test_graphify_server_is_disabled_without_explicit_configuration(self):
        servers = build_workspace_server_config({})
        self.assertNotIn("graphify", servers)

    def test_graphify_server_is_disabled_without_graph_path(self):
        servers = build_workspace_server_config({})
        self.assertNotIn("graphify", servers)

    def test_graphify_server_command_requires_graph_path(self):
        with self.assertRaisesRegex(RuntimeError, "requires GRAPHIFY_GRAPH_PATH"):
            build_workspace_server_config({"GRAPHIFY_MCP_COMMAND": "graphify-mcp"})

    def test_graphify_server_requires_installed_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            graph_path = os.path.join(directory, "graph.json")
            with open(graph_path, "w", encoding="utf-8") as graph_file:
                graph_file.write('{"nodes": [], "links": []}')
            with self.assertRaisesRegex(RuntimeError, "installed executable"):
                build_workspace_server_config({
                    "GRAPHIFY_MCP_COMMAND": "missing-graphify-mcp-binary",
                    "GRAPHIFY_GRAPH_PATH": graph_path,
                })

    def test_graphify_server_is_registered_only_with_valid_local_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            graph_path = os.path.join(directory, "graph.json")
            command_path = os.path.join(directory, "graphify-mcp")
            with open(graph_path, "w", encoding="utf-8") as graph_file:
                graph_file.write('{"nodes": [], "links": []}')
            with open(command_path, "w", encoding="utf-8") as command_file:
                command_file.write("#!/bin/sh\\nexit 0\\n")
            os.chmod(command_path, 0o755)
            servers = build_workspace_server_config({
                "GRAPHIFY_MCP_COMMAND": command_path,
                "GRAPHIFY_GRAPH_PATH": graph_path,
            })
        self.assertEqual(servers["graphify"]["command"], command_path)
        self.assertEqual(servers["graphify"]["args"], [graph_path])

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
