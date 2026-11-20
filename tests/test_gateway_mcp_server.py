import unittest
from unittest.mock import AsyncMock

from mcp.types import Tool, TextContent
from agent.gateway_mcp_server import (
    PUBLIC_TOOLS,
    UpstreamRouter,
    public_tool_name,
    validate_gateway_host,
)


class GatewayPolicyTests(unittest.TestCase):
    def test_allowlist_is_explicit_and_read_only(self):
        self.assertEqual(PUBLIC_TOOLS["workspace"], {"text_stats", "workspace_status"})
        self.assertEqual(PUBLIC_TOOLS["tasks"], {"list_tasks"})
        self.assertNotIn("graphify", PUBLIC_TOOLS)
        self.assertNotIn("enqueue_task", PUBLIC_TOOLS["tasks"])
        self.assertNotIn("remember", PUBLIC_TOOLS["memory"])
        self.assertNotIn("create_plan", PUBLIC_TOOLS["planner"])

    def test_tool_names_are_namespaced(self):
        self.assertEqual(public_tool_name("workspace", "workspace_status"),
                         "workspace__workspace_status")

    def test_loopback_hosts_are_accepted(self):
        for host in ("localhost", "127.0.0.1", "::1"):
            self.assertEqual(validate_gateway_host(host), host)

    def test_non_loopback_hosts_are_rejected(self):
        for host in ("0.0.0.0", "192.168.1.10", "example.com"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                validate_gateway_host(host)

    def test_tool_schema_is_preserved_when_renamed(self):
        original = Tool(
            name="workspace_status",
            description="Runtime status",
            inputSchema={"type": "object", "properties": {}, "required": []},
        )
        renamed = original.model_copy(update={"name": public_tool_name("workspace", original.name)})
        self.assertEqual(renamed.name, "workspace__workspace_status")
        self.assertEqual(renamed.inputSchema, original.inputSchema)


class RouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_tool_is_rejected(self):
        router = UpstreamRouter()
        with self.assertRaisesRegex(ValueError, "not exposed"):
            await router.call_tool("tasks__enqueue_task", {})
        await router.close()

    async def test_allowed_tool_routes_to_upstream_name(self):
        router = UpstreamRouter()
        session = type("Session", (), {})()
        session.call_tool = AsyncMock(return_value={"content": []})
        router._sessions["workspace"] = session
        router._tools["workspace__workspace_status"] = (
            "workspace", "workspace_status",
            Tool(name="workspace__workspace_status",
                 inputSchema={"type": "object", "properties": {}}),
        )
        await router.call_tool("workspace__workspace_status", {})
        session.call_tool.assert_awaited_once_with("workspace_status", {})


if __name__ == "__main__":
    unittest.main()
