import unittest
from unittest.mock import AsyncMock

from mcp.types import Tool, TextContent
from agent.gateway_mcp_server import (
    PUBLIC_TOOLS,
    PLANNER_ACTION_TOOLS,
    allowed_tools_for_environment,
    UpstreamRouter,
    public_tool_name,
    validate_gateway_host,
)


class GatewayPolicyTests(unittest.TestCase):
    def test_allowlist_is_explicit_and_sensitive_tools_are_excluded(self):
        self.assertEqual(PUBLIC_TOOLS["workspace"], {"text_stats", "workspace_status"})
        self.assertEqual(PUBLIC_TOOLS["tasks"], {"list_tasks"})
        self.assertNotIn("graphify", PUBLIC_TOOLS)
        self.assertNotIn("enqueue_task", PUBLIC_TOOLS["tasks"])
        self.assertNotIn("remember", PUBLIC_TOOLS["memory"])
        self.assertNotIn("create_plan", PUBLIC_TOOLS["planner"])
        self.assertEqual(PUBLIC_TOOLS["specialists"], {"delegate_specialist", "supervise_request"})
        self.assertFalse(PLANNER_ACTION_TOOLS.intersection(PUBLIC_TOOLS["planner"]))
        self.assertEqual(PUBLIC_TOOLS["terminal"], {"run_workspace_task", "read_project_file", "write_project_file"})

    def test_planner_mutations_require_explicit_opt_in(self):
        default = allowed_tools_for_environment({})
        self.assertFalse(PLANNER_ACTION_TOOLS.intersection(default["planner"]))
        enabled = allowed_tools_for_environment({"ENABLE_GATEWAY_PLANNER_ACTIONS": "true"})
        self.assertEqual(PLANNER_ACTION_TOOLS.intersection(enabled["planner"]), PLANNER_ACTION_TOOLS)
        self.assertNotIn("retry_failed_plan_step", enabled["planner"])

    def test_specialists_are_only_available_when_server_is_configured(self):
        from agent.tool_agent import build_workspace_server_config
        self.assertNotIn("specialists", build_workspace_server_config({}))
        self.assertIn("specialists", build_workspace_server_config({"ENABLE_SPECIALIST_AGENTS": "true"}))

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
