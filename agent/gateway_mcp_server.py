"""Loopback-only Streamable HTTP gateway for selected local MCP tools."""
import asyncio
import ipaddress
import os
from contextlib import AsyncExitStack

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, ListToolsResult
from mcp.server.fastmcp import FastMCP

from agent.tool_agent import build_workspace_server_config

PUBLIC_TOOLS = {
    "workspace": {"text_stats", "workspace_status"},
    "memory": {"recall", "recent_memories"},
    "research": {"web_search", "scrape_url", "research_provider_status"},
    "tasks": {"list_tasks"},
    "planner": {"get_plan", "list_plans"},
    "browser": {"browser_inspect"},
}
PUBLIC_SERVER_NAMES = tuple(PUBLIC_TOOLS)


def validate_gateway_host(host: str) -> str:
    """Reject network-facing binds; this phase is local-only."""
    if host == "localhost":
        return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("MCP Gateway must bind to localhost or a loopback IP.") from exc
    if not address.is_loopback:
        raise ValueError("MCP Gateway must bind to localhost or a loopback IP.")
    return host


def public_tool_name(server_name: str, tool_name: str) -> str:
    return f"{server_name}__{tool_name}"


class UpstreamRouter:
    """Own persistent upstream STDIO sessions and expose an explicit tool allowlist."""

    def __init__(self):
        self._stack = AsyncExitStack()
        self._sessions = {}
        self._tools = {}
        self._semaphore = asyncio.Semaphore(4)

    async def start(self):
        load_dotenv()
        configs = build_workspace_server_config()
        for server_name in PUBLIC_SERVER_NAMES:
            config = configs.get(server_name)
            if config is None:
                continue
            parameters = StdioServerParameters(
                command=config["command"],
                args=config.get("args", []),
                env=config.get("env"),
            )
            read, write = await self._stack.enter_async_context(stdio_client(parameters))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._sessions[server_name] = session
            result = await session.list_tools()
            for tool in result.tools:
                if tool.name not in PUBLIC_TOOLS[server_name]:
                    continue
                name = public_tool_name(server_name, tool.name)
                self._tools[name] = (server_name, tool.name, tool.model_copy(update={"name": name}))
        return self

    async def list_tools(self) -> list:
        return [entry[2] for entry in self._tools.values()]

    async def call_tool(self, name: str, arguments: dict) -> CallToolResult:
        entry = self._tools.get(name)
        if entry is None:
            raise ValueError(f"Tool is not exposed by this gateway: {name}")
        server_name, upstream_name, _ = entry
        async with self._semaphore:
            return await self._sessions[server_name].call_tool(upstream_name, arguments)

    async def close(self):
        await self._stack.aclose()


def create_gateway(router: UpstreamRouter, host: str = "127.0.0.1", port: int = 8765):
    """Adapt upstream MCP tools to a Streamable HTTP MCP server."""
    validate_gateway_host(host)
    gateway = FastMCP(
        "AI Workspace Universal MCP Gateway",
        host=host,
        port=port,
    )

    @gateway._mcp_server.list_tools()
    async def list_tools() -> ListToolsResult:
        return ListToolsResult(tools=await router.list_tools())

    @gateway._mcp_server.call_tool(validate_input=True)
    async def call_tool(name: str, arguments: dict) -> CallToolResult:
        return await router.call_tool(name, arguments)

    return gateway


async def run_gateway():
    host = validate_gateway_host(os.getenv("MCP_GATEWAY_HOST", "127.0.0.1"))
    port = int(os.getenv("MCP_GATEWAY_PORT", "8765"))
    router = await UpstreamRouter().start()
    gateway = create_gateway(router, host=host, port=port)
    try:
        await gateway.run_streamable_http_async()
    finally:
        await router.close()


if __name__ == "__main__":
    asyncio.run(run_gateway())
